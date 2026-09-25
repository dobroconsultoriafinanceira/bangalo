# -*- coding: utf-8 -*-
"""Lançamentos do fluxo a partir do extrato do Itaú.

Primeira linha migrada: **retirada da sócia**. O banco mostra só "PIX ENVIADO
BARBARA MEND"; a regra da consultoria separa o que é pró-labore (valor fixo por
mês) do que é distribuição de lucros (o que passar disso no mês).

Cada pagamento vira um ou dois lançamentos, na data em que saiu do banco:
enquanto não completar o pró-labore do mês, o valor entra como pró-labore; o
excedente entra como distribuição de lucros. Mês em que se pagou menos que o
pró-labore fica todo como pró-labore (nunca gera lucro negativo).

Idempotente: `origem='itau'` + `origem_id='<id do movimento>:pro|luc'`.
Cada lançamento já nasce conciliado com o movimento que o originou.
"""
import re
import unicodedata
from datetime import date
from decimal import Decimal

from app.extensions import db
from app.models.banco import ConciliacaoItem, MovimentoBancario
from app.models.fluxo import Categoria, Lancamento
from app.services import auditoria
from app.utils.datas import agora_sp

ORIGEM = "itau"
ZERO = Decimal("0.00")

CATEGORIA_PRO_LABORE = "Pro Labore"
CATEGORIA_LUCROS = "Distribuição de Lucros"
GRUPO_SOCIOS = "Demais Salários"

# Valor do pró-labore por vigência (o resto do que a sócia recebe é lucro).
# Conferido contra o extrato: fev/26 = 8.157,41; de março em diante, 8.475,55.
PRO_LABORE_VIGENCIAS = [
    (date(2026, 1, 1), Decimal("8157.41")),
    (date(2026, 3, 1), Decimal("8475.55")),
]

# categoria bancária dos pagamentos à sócia
CATEGORIAS_SOCIA = ("soc_a_detalhar", "soc_retirada")


def pro_labore_do_mes(mes: date) -> Decimal:
    valor = PRO_LABORE_VIGENCIAS[0][1]
    for inicio, v in PRO_LABORE_VIGENCIAS:
        if mes >= inicio:
            valor = v
    return valor


def _categoria(nome: str, grupo: str) -> Categoria:
    cat = db.session.execute(
        db.select(Categoria).filter_by(nome=nome, tipo="saida")
    ).scalar_one_or_none()
    if not cat:
        cat = Categoria(nome=nome, tipo="saida", grupo=grupo, ativo=True)
        db.session.add(cat)
        db.session.flush()
    return cat


def _upsert(movimento: MovimentoBancario, sufixo: str, categoria: Categoria,
            valor: Decimal, descricao: str, usuario_id: int | None) -> tuple[Lancamento, bool]:
    origem_id = f"{movimento.id}:{sufixo}"
    lanc = db.session.execute(
        db.select(Lancamento).filter_by(origem=ORIGEM, origem_id=origem_id)
    ).scalar_one_or_none()
    novo = lanc is None
    if novo:
        lanc = Lancamento(origem=ORIGEM, origem_id=origem_id)
        db.session.add(lanc)
    lanc.data = movimento.data
    lanc.categoria_id = categoria.id
    lanc.valor = valor
    lanc.descricao = descricao
    lanc.usuario_id = usuario_id
    db.session.flush()
    return lanc, novo


def _conciliar(movimento: MovimentoBancario, lancamentos: list[Lancamento]) -> None:
    """O movimento que gerou os lançamentos já nasce conciliado com eles."""
    ligados = {i.lancamento_id for i in movimento.itens_conciliacao}
    for lanc in lancamentos:
        if lanc.id not in ligados:
            movimento.itens_conciliacao.append(
                ConciliacaoItem(lancamento_id=lanc.id, automatica=True))
    db.session.flush()
    soma = sum((Decimal(i.lancamento.valor) for i in movimento.itens_conciliacao if i.lancamento), ZERO)
    movimento.conciliado_em = agora_sp().replace(tzinfo=None)
    movimento.diferenca_conciliacao = (Decimal(movimento.valor) - soma).quantize(Decimal("0.01"))


def importar_socios(inicio: date, fim: date, usuario_id: int | None = None,
                    substituir_planilha: bool = True) -> dict:
    """Separa pró-labore e distribuição de lucros dos pagamentos à sócia."""
    movimentos = db.session.execute(
        db.select(MovimentoBancario).filter(
            MovimentoBancario.tipo == "debito",
            MovimentoBancario.categoria_gerencial.in_(CATEGORIAS_SOCIA),
            MovimentoBancario.data >= inicio,
            MovimentoBancario.data <= fim,
        ).order_by(MovimentoBancario.data, MovimentoBancario.id)
    ).scalars().all()

    cat_pro = _categoria(CATEGORIA_PRO_LABORE, GRUPO_SOCIOS)
    cat_luc = _categoria(CATEGORIA_LUCROS, GRUPO_SOCIOS)

    inseridos = atualizados = 0
    total_pro = total_luc = ZERO
    pago_no_mes: dict[tuple[int, int], Decimal] = {}

    for mov in movimentos:
        mes = (mov.data.year, mov.data.month)
        teto = pro_labore_do_mes(date(mov.data.year, mov.data.month, 1))
        ja_pago = pago_no_mes.get(mes, ZERO)
        valor = Decimal(mov.valor)

        # o pró-labore do mês é preenchido primeiro; o que passar é lucro
        parte_pro = max(ZERO, min(valor, teto - ja_pago))
        parte_luc = valor - parte_pro
        pago_no_mes[mes] = ja_pago + valor

        lancs = []
        if parte_pro > 0:
            lanc, novo = _upsert(mov, "pro", cat_pro, parte_pro,
                                 "Pró-labore da sócia · Itaú", usuario_id)
            lancs.append(lanc)
            inseridos, atualizados = inseridos + novo, atualizados + (not novo)
            total_pro += parte_pro
        if parte_luc > 0:
            lanc, novo = _upsert(mov, "luc", cat_luc, parte_luc,
                                 "Distribuição de lucros · Itaú", usuario_id)
            lancs.append(lanc)
            inseridos, atualizados = inseridos + novo, atualizados + (not novo)
            total_luc += parte_luc
        _conciliar(mov, lancs)

    removidos = _remover_da_planilha(inicio, fim) if substituir_planilha else 0
    db.session.flush()
    auditoria.registrar("import", "lancamento", None, depois={
        "origem": ORIGEM, "tipo": "socios", "inicio": str(inicio), "fim": str(fim),
        "pro_labore": str(total_pro), "lucros": str(total_luc), "removidos_da_planilha": removidos,
    })
    return {
        "movimentos": len(movimentos), "inseridos": inseridos, "atualizados": atualizados,
        "pro_labore": total_pro, "lucros": total_luc, "removidos": removidos,
    }


def _remover_da_planilha(inicio: date, fim: date) -> int:
    """Tira a linha antiga "Pro Labore/Lucro" da planilha no período migrado."""
    ids_cat = db.session.execute(
        db.select(Categoria.id).filter(Categoria.nome == "Pro Labore/Lucro")
    ).scalars().all()
    if not ids_cat:
        return 0
    ids = db.session.execute(
        db.select(Lancamento.id).filter(
            Lancamento.categoria_id.in_(ids_cat), Lancamento.origem != ORIGEM,
            Lancamento.data >= inicio, Lancamento.data <= fim)
    ).scalars().all()
    if not ids:
        return 0
    db.session.execute(db.delete(ConciliacaoItem).where(ConciliacaoItem.lancamento_id.in_(ids)))
    db.session.execute(db.delete(Lancamento).where(Lancamento.id.in_(ids)))
    return len(ids)


# ─────────────────────────── de-para do extrato ───────────────────────────
#
# Cada movimento do banco já chega com uma categoria gerencial (visão CFO).
# Aqui ela vira a linha do fluxo de caixa. O que não tem tradução clara NÃO
# vira lançamento: fica na fila de revisão para a Manu classificar e criar a
# regra — é assim que o sistema aprende (ex.: pagamento a "CRBS" → Ambev).

# categorias do banco que ficam na linha "A classificar" do caixa até alguém revisar
A_REVISAR = {"a_classificar_entrada", "a_classificar_saida"}

# não viram lançamento: ou já entram por outra fonte, ou não são do fluxo
FORA_DO_FLUXO = {
    "rec_stone": "repasse da Stone — já lançado pela integração da Stone",
    "rec_cartao": "cartão — já lançado pela integração da Stone",
    "trf_entre_contas": "transferência entre contas próprias",
    "soc_a_detalhar": "retirada da sócia — lançada pela regra de pró-labore/lucros",
    "soc_retirada": "retirada da sócia — lançada pela regra de pró-labore/lucros",
    "pag_prolabore": "pró-labore — lançado pela regra de pró-labore/lucros",
    "a_classificar_entrada": "sem classificação: a Manu revisa",
    "a_classificar_saida": "sem classificação: a Manu revisa",
}

# categoria bancária -> linha do fluxo
MAPA_CATEGORIA = {
    # entradas
    "rec_ifood": "IFOOD",
    "rec_99food": "99 FOOD",
    "rec_pix_clientes": "Pix Itau",
    "rec_eventos": "Patrocínio",
    "tes_resgate": "Resgate",
    "tes_rendimento": "RENDIMENTO",
    "fin_emprestimo_recebido": "Empréstimo",
    # saídas
    "fin_emprestimo_pagamento": "Empréstimos Heitor",
    "pag_folha": "Salários",
    "pag_encargos": "FGTS",
    "pag_artistas": "Músicos",
    "pag_tarifas": "Despesas Bancárias",
    "pag_aluguel": "Aluguel",
    "pag_manutencao": "Manutenção/Obras/Equip.",
    "pag_reembolso_socio": "Reembolso Barbara",
    "pag_fornecedores": "Compras",   # + fornecedor pela contraparte
    "tes_aplicacao": "Aplicação",    # dinheiro que sai da conta para a aplicação
}

# quando uma categoria do banco cobre várias linhas do fluxo, a contraparte decide
POR_CONTRAPARTE = [
    (r"\bIFOOD\b", "IFOOD"),
    (r"\b99 ?FOOD\b", "99 FOOD"),
    (r"\bLIGHT\b", "Light"),
    (r"\bCEG\b", "CEG"),
    (r"\bIGUA\b|SAEMJA", "IGUA"),
    (r"\bNET\b", "Net"),
    (r"SIMPLES NACIONAL", "DAS"),
    (r"SEFAZ|DARJ|ICMS", "ICMS"),
    (r"CONTABIL", "Contabilidade"),
    (r"ADVOGAD|JURIDIC", "Jurídico"),
]
CATEGORIAS_AMBIGUAS = {"pag_utilidades", "pag_tributos", "pag_servicos", "pag_outros"}


def _texto(mov) -> str:
    bruto = f"{mov.contraparte or ''} {mov.descricao or ''}".upper()
    return "".join(c for c in unicodedata.normalize("NFKD", bruto) if not unicodedata.combining(c))


def destino(mov) -> tuple[str | None, str | None, bool]:
    """(linha do fluxo, motivo, fora_do_fluxo).

    `fora_do_fluxo=True` = esse dinheiro não é do caixa ou já entra por outra
    fonte; não vira lançamento. `False` com linha vazia = precisa de revisão e
    fica na linha "A classificar" para o saldo continuar batendo com o banco.

    A descrição do banco vem antes da categoria gerencial: muito repasse da
    Stone e muito resgate automático chegam sem contraparte e acabariam na fila
    de revisão sem necessidade.
    """
    texto = _texto(mov)
    # a Stone vem antes até da escolha manual: esse dinheiro já entrou no caixa
    # pelo arquivo de conciliação dela, e lançar de novo contaria duas vezes
    if mov.tipo == "credito" and "STONE" in texto:
        return None, "crédito da Stone — já lançado pela integração da Stone", True
    escolhida = linha_escolhida(mov)
    if escolhida is not None:
        return escolhida.nome, None, False
    if re.search(r"APLIC", texto) and mov.tipo == "debito":
        return "Aplicação", None, False
    if re.search(r"\bRESGATE\b", texto) and mov.tipo == "credito":
        return "Resgate", None, False
    if re.search(r"REND(IMENTO)? PAGO|RENDIMENTO", texto) and mov.tipo == "credito":
        return "RENDIMENTO", None, False
    if mov.categoria_gerencial in FORA_DO_FLUXO:
        return None, FORA_DO_FLUXO[mov.categoria_gerencial], mov.categoria_gerencial not in A_REVISAR
    for padrao, categoria in POR_CONTRAPARTE:
        if re.search(padrao, texto):
            return categoria, None, False
    if mov.categoria_gerencial in CATEGORIAS_AMBIGUAS:
        return None, "a contraparte não diz qual linha do fluxo: a Manu classifica", False
    nome = MAPA_CATEGORIA.get(mov.categoria_gerencial)
    if nome:
        return nome, None, False
    return None, "sem tradução para o fluxo: a Manu classifica", False


def linha_escolhida(mov):
    """Linha do caixa que uma pessoa escolheu na tela (ou que a regra fixou).

    Vem antes de qualquer heurística: quem revisa conhece a planilha melhor do
    que a descrição do banco. Só vale se o tipo bater (entrada/saída).
    """
    from app.models.fluxo import Categoria

    if not getattr(mov, "categoria_id", None):
        return None
    cat = db.session.get(Categoria, mov.categoria_id)
    if cat is None:
        return None
    return cat if cat.tipo == ("entrada" if mov.tipo == "credito" else "saida") else None


def _fornecedor_da_regra(mov):
    """Fornecedor fixado por regra: "pagamento a CRBS é a Ambev"."""
    from app.models.banco import RegraClassificacaoBancaria
    from app.services import classificacao_bancaria as cls

    campo, valor = cls.chave_regra(mov)
    if not valor:
        return None
    regra = db.session.execute(
        db.select(RegraClassificacaoBancaria).filter_by(
            campo=campo, valor=valor, ativo=True).filter(
            RegraClassificacaoBancaria.fornecedor_id.isnot(None))
    ).scalars().first()
    return regra.fornecedor if regra else None


def _fornecedor_da_contraparte(mov, cache: dict):
    """Fornecedor do movimento: primeiro a regra, depois o nome do cadastro."""
    from app.models.fluxo import Fornecedor

    da_regra = _fornecedor_da_regra(mov)
    if da_regra is not None:
        return da_regra
    nome = (mov.contraparte or "").strip().upper()
    if not nome:
        return None
    if not cache:
        for f in db.session.execute(db.select(Fornecedor)).scalars():
            cache[f.nome.strip().upper()] = f
    if nome in cache:
        return cache[nome]
    for cadastrado, forn in cache.items():  # nomes truncados pelo banco
        if len(cadastrado) >= 8 and (nome.startswith(cadastrado[:12]) or cadastrado.startswith(nome[:12])):
            return forn
    return None


def importar_periodo(inicio: date, fim: date, usuario_id: int | None = None,
                     previa: bool = False, substituir_planilha: bool = True) -> dict:
    """Traz do extrato tudo o que tem tradução para o fluxo.

    `previa=True` não grava nada — só devolve os números do que seria feito.
    """
    from app.models.fluxo import Fornecedor  # noqa: F401  (usado pelo cache)

    movimentos = db.session.execute(
        db.select(MovimentoBancario).filter(
            MovimentoBancario.data >= inicio, MovimentoBancario.data <= fim
        ).order_by(MovimentoBancario.data, MovimentoBancario.id)
    ).scalars().all()

    cache_forn: dict = {}
    cache_cat: dict = {}
    criados = atualizados = sem_fornecedor = 0
    por_linha: dict[str, dict] = {}
    fila: dict[str, dict] = {}

    for mov in movimentos:
        nome_cat, motivo, fora = destino(mov)
        tipo = "entrada" if mov.tipo == "credito" else "saida"
        if nome_cat is None:
            d = fila.setdefault(motivo, {"n": 0, "total": ZERO, "fora": fora})
            d["n"] += 1
            d["total"] += Decimal(mov.valor)
            if fora:
                continue
            # entra no caixa para o saldo bater com o banco, mas numa linha
            # "A classificar" até a Manu dizer qual é
            nome_cat = CATEGORIA_A_CLASSIFICAR[tipo]
            if not previa:
                mov.revisar = True
        if (nome_cat, tipo) not in cache_cat:
            from app.models.fluxo import Categoria

            cache_cat[(nome_cat, tipo)] = db.session.execute(
                db.select(Categoria).filter_by(nome=nome_cat, tipo=tipo)
            ).scalar_one_or_none()
        categoria = cache_cat[(nome_cat, tipo)]
        if categoria is None and nome_cat in CATEGORIA_A_CLASSIFICAR.values():
            from app.models.fluxo import Categoria

            categoria = Categoria(nome=nome_cat, tipo=tipo, grupo=GRUPO_A_CLASSIFICAR, ativo=True)
            db.session.add(categoria)
            db.session.flush()
            cache_cat[(nome_cat, tipo)] = categoria
        if categoria is None:
            d = fila.setdefault(f"categoria “{nome_cat}” não existe no plano de contas", {"n": 0, "total": ZERO})
            d["n"] += 1
            d["total"] += Decimal(mov.valor)
            continue

        fornecedor = None
        if mov.categoria_gerencial == "pag_fornecedores":
            fornecedor = _fornecedor_da_contraparte(mov, cache_forn)
            if fornecedor is None:
                sem_fornecedor += 1
                if not previa:
                    mov.revisar = True   # a Manu liga ao fornecedor e cria a regra

        linha = por_linha.setdefault(nome_cat, {"n": 0, "total": ZERO})
        linha["n"] += 1
        linha["total"] += Decimal(mov.valor)

        if previa:
            continue
        lanc, novo = _upsert(mov, "mov", categoria, Decimal(mov.valor),
                             f"{mov.contraparte or mov.descricao or 'Itaú'} · Itaú", usuario_id)
        lanc.fornecedor_id = fornecedor.id if fornecedor else None
        criados, atualizados = criados + novo, atualizados + (not novo)
        _conciliar(mov, [lanc])

    pix = _pix_da_stone(inicio, fim, usuario_id, previa)

    removidos = 0
    if not previa and substituir_planilha:
        removidos = _remover_planilha_do_periodo(inicio, fim)
    if not previa:
        db.session.flush()
        auditoria.registrar("import", "lancamento", None, depois={
            "origem": ORIGEM, "tipo": "extrato", "inicio": str(inicio), "fim": str(fim),
            "criados": criados, "removidos_da_planilha": removidos,
        })

    return {
        "movimentos": len(movimentos), "criados": criados, "atualizados": atualizados,
        "removidos": removidos, "sem_fornecedor": sem_fornecedor,
        "pix_stone": pix, "por_linha": por_linha, "fila": fila,
    }


GRUPO_A_CLASSIFICAR = "A classificar"
CATEGORIA_A_CLASSIFICAR = {"entrada": "A classificar (entradas)", "saida": "A classificar (saídas)"}

# linhas que NÃO vêm do banco: dinheiro é lançado à mão pela Manu
CATEGORIAS_MANUAIS = ("Dinheiro",)


def _remover_planilha_do_periodo(inicio: date, fim: date) -> int:
    """Tira do período os lançamentos da planilha que agora vêm do banco."""
    from app.models.fluxo import Categoria

    # só o que veio da importação da planilha (sem autor). O que alguém digitou
    # na tela tem usuário e fica — inclusive dinheiro, que nunca passa no banco.
    ids = db.session.execute(
        db.select(Lancamento.id).join(Categoria, Categoria.id == Lancamento.categoria_id).filter(
            Lancamento.origem == "manual",
            Lancamento.usuario_id.is_(None),
            Lancamento.data >= inicio, Lancamento.data <= fim,
            Categoria.nome.not_in(CATEGORIAS_MANUAIS),
        )
    ).scalars().all()
    if not ids:
        return 0
    db.session.execute(db.delete(ConciliacaoItem).where(ConciliacaoItem.lancamento_id.in_(ids)))
    db.session.execute(db.delete(Lancamento).where(Lancamento.id.in_(ids)))
    return len(ids)


def _pix_da_stone(inicio: date, fim: date, usuario_id: int | None, previa: bool) -> dict:
    """O PIX da maquininha, que o arquivo de conciliação não traz.

    A Stone deposita de duas formas, e o extrato mostra a diferença:
      - por bandeira ("STONE VISA CD ..."): é repasse de cartão e casa com o
        arquivo — mesmo quando cai um dia depois;
      - consolidado ("PIX TRANSF BANGALO"): vem cartão + PIX no mesmo crédito.

    Então o PIX do dia é o que sobra do crédito consolidado depois de cobrir os
    repasses de cartão que ainda não vieram por bandeira. Sem isso, esse
    dinheiro (R$ 305 mil em 2026) sumiria do caixa.
    """
    from app.models.fluxo import Categoria

    por_dia: dict[date, dict] = {}
    for mov in db.session.execute(
        db.select(MovimentoBancario).filter(
            MovimentoBancario.tipo == "credito",
            MovimentoBancario.data >= inicio, MovimentoBancario.data <= fim,
        ).order_by(MovimentoBancario.data, MovimentoBancario.id)
    ).scalars():
        texto = _texto(mov)
        eh_stone = mov.categoria_gerencial in ("rec_stone", "rec_cartao") or "STONE" in texto
        if not eh_stone:
            continue
        d = por_dia.setdefault(mov.data, {"bandeira": ZERO, "consolidado": ZERO, "movs": []})
        if re.search(r"STONE\s+\S+\s+(CD|DB)", texto):
            d["bandeira"] += Decimal(mov.valor)      # repasse por bandeira
        else:
            d["consolidado"] += Decimal(mov.valor)   # cartão + PIX juntos
            d["movs"].append(mov)

    categoria = db.session.execute(
        db.select(Categoria).filter_by(nome="Pix Stone", tipo="entrada")
    ).scalar_one_or_none()
    if categoria is None:
        return {"dias": 0, "total": ZERO}

    dias, total = 0, ZERO
    for dia, d in sorted(por_dia.items()):
        do_arquivo = Decimal(db.session.execute(
            db.select(db.func.coalesce(db.func.sum(Lancamento.valor), 0))
            .filter(Lancamento.origem == "stone", Lancamento.data == dia)
        ).scalar_one())
        # o crédito consolidado cobre primeiro o cartão que não veio por bandeira
        cartao_no_consolidado = max(ZERO, do_arquivo - d["bandeira"])
        pix = (d["consolidado"] - cartao_no_consolidado).quantize(Decimal("0.01"))

        # apaga sempre antes de gravar: o "movimento principal" do dia pode mudar
        # entre execuções e sobraria um PIX duplicado no caixa
        if not previa:
            _apagar_pix_do_dia(dia)
        if pix <= Decimal("0.01") or not d["movs"]:
            continue
        dias += 1
        total += pix
        if previa:
            continue
        principal = d["movs"][0]
        lanc, _ = _upsert(principal, "pix", categoria, pix,
                          "PIX da maquininha (Stone) · diferença do repasse", usuario_id)
        do_dia = db.session.execute(
            db.select(Lancamento).filter(
                db.or_(Lancamento.origem == "stone", Lancamento.id == lanc.id),
                Lancamento.data == dia)
        ).scalars().all()
        for m in d["movs"]:
            _limpar_itens(m)
        _conciliar(principal, do_dia)
    return {"dias": dias, "total": total}


def _apagar_pix_do_dia(dia: date) -> None:
    """Dia que deixou de ter PIX (ex.: repasse chegou por bandeira um dia depois)."""
    ids = db.session.execute(
        db.select(Lancamento.id).filter(
            Lancamento.origem == ORIGEM, Lancamento.data == dia,
            Lancamento.origem_id.like("%:pix"))
    ).scalars().all()
    if ids:
        db.session.execute(db.delete(ConciliacaoItem).where(ConciliacaoItem.lancamento_id.in_(ids)))
        db.session.execute(db.delete(Lancamento).where(Lancamento.id.in_(ids)))


def _limpar_itens(movimento: MovimentoBancario) -> None:
    movimento.itens_conciliacao.clear()
    db.session.flush()


def sincronizar_movimento(mov: MovimentoBancario, usuario_id: int | None = None,
                          fornecedor_id: int | None = None) -> str:
    """Refaz o lançamento de UM movimento depois que a classificação mudou.

    É o que fecha o ciclo de aprendizado: a Manu diz que aquele PIX é a Ambev,
    a regra passa a valer, e o lançamento sai de "A classificar" e cai em
    Compras com o fornecedor certo — sem ninguém mexer no caixa à mão.
    """
    from app.models.fluxo import Categoria

    nome_cat, _motivo, fora = destino(mov)
    tipo = "entrada" if mov.tipo == "credito" else "saida"
    existentes = db.session.execute(
        db.select(Lancamento).filter(
            Lancamento.origem == ORIGEM,
            Lancamento.origem_id.in_([f"{mov.id}:mov", f"{mov.id}:pro", f"{mov.id}:luc"]))
    ).scalars().all()

    if fora or (nome_cat is None and mov.categoria_gerencial in CATEGORIAS_SOCIA):
        for lanc in existentes:
            db.session.execute(db.delete(ConciliacaoItem).where(
                ConciliacaoItem.lancamento_id == lanc.id))
            db.session.delete(lanc)
        db.session.flush()
        return "removido"

    if nome_cat is None:
        # sem tradução: entra em "A classificar" para o saldo bater; quem revisa
        # decide depois (não remarcamos o movimento — a pessoa acabou de olhar)
        nome_cat = CATEGORIA_A_CLASSIFICAR[tipo]

    categoria = db.session.execute(
        db.select(Categoria).filter_by(nome=nome_cat, tipo=tipo)
    ).scalar_one_or_none()
    if categoria is None:
        categoria = _categoria_ou_cria(nome_cat, tipo)

    from app.models.fluxo import Fornecedor

    fornecedor = db.session.get(Fornecedor, fornecedor_id) if fornecedor_id else None
    if fornecedor is None and mov.categoria_gerencial == "pag_fornecedores":
        fornecedor = _fornecedor_da_contraparte(mov, {})

    lanc, novo = _upsert(mov, "mov", categoria, Decimal(mov.valor),
                         f"{mov.contraparte or mov.descricao or 'Itaú'} · Itaú", usuario_id)
    lanc.fornecedor_id = fornecedor.id if fornecedor else None
    _conciliar(mov, [lanc])
    return "criado" if novo else "atualizado"


def _categoria_ou_cria(nome: str, tipo: str):
    from app.models.fluxo import Categoria

    grupo = GRUPO_A_CLASSIFICAR if nome in CATEGORIA_A_CLASSIFICAR.values() else "Outras despesas"
    cat = Categoria(nome=nome, tipo=tipo, grupo=grupo, ativo=True)
    db.session.add(cat)
    db.session.flush()
    return cat
