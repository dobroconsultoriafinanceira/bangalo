# -*- coding: utf-8 -*-
"""Layout da grade mensal no vocabulário da planilha.

Só a tela "Caixa › Grade mensal" usa este módulo. O plano de contas do sistema
continua como está (categoria › subcategoria, em `seeds.py`); aqui ele é
reescrito nos nomes, nos agrupamentos e na ordem em que a Bárbara e a Manu leem
a planilha — inclusive as grafias sem acento que elas conhecem.

Três junções acontecem só aqui:
  - "Pix Itau" + "Pix Stone" viram uma linha "Pagamento em PIX";
  - "Pro Labore" + "Distribuição de Lucros" viram "Pro Labore/Lucro";
  - o bloco "Aplicação" repete valores que já aparecem em Outras Despesas
    (Aplicação) e Outras Entradas (Resgate). Ele é só referência: fica fora dos
    totais, senão o mesmo dinheiro contaria duas vezes — que é justamente o
    defeito da fórmula SALDO TOTAL da planilha.
"""
from decimal import Decimal

ZERO = Decimal("0.00")

GRUPO_MEMORIA = "Aplicação"
GRUPO_COMPRAS = "Compras"
GRUPO_A_CLASSIFICAR = "A classificar"

# categoria do sistema (Categoria.grupo) -> bloco da planilha
BLOCO = {
    "Vendas - Repasse Stone": "Entradas",
    "Pix Itau": "Entradas",
    "IFOOD": "Entradas",
    "99 FOOD": "Entradas",
    "Dinheiro": "Entradas",
    "Outros/Acertos": "Entradas",
    "Patrocínio": "Outras Entradas",
    "Empréstimo": "Outras Entradas",
    "Resgate": "Outras Entradas",
    "RENDIMENTO": GRUPO_MEMORIA,
    "Impostos": "Impostos",
    "Folha/Salários": "Salarios",
    "Demais Salários": "Demais Salarios",
    "Compras": GRUPO_COMPRAS,
    "Despesas Fixas": "Despesas Fixas",
    "Outras despesas": "Outras Despesas",
    "A classificar": GRUPO_A_CLASSIFICAR,
}

# nome do sistema -> nome na planilha (só onde difere)
APELIDO = {
    "Pix Stone": "Pagamento em PIX",
    "Pix Itau": "Pagamento em PIX",
    "Outros/Acertos": "Outros/Acerto",
    "Empréstimo": "Empréstimos",
    "Pro Labore": "Pro Labore/Lucro",
    "Distribuição de Lucros": "Pro Labore/Lucro",
    "Músicos": "MUSICOS",
    "Técnico Som": "TECNICO SOM",
    "Vale Transporte": "Vale transporte",
    "DARFS (Funcionários + sócia)": "DARFS (Funcionarios +socia)",
    "Ações/Acordos Trabalhistas": "Açoes/Acordos trabalhistas",
    "Cartão de Crédito 15 Itaú": "Cartão de Crédito 15 Itau",
    "Cartão de Crédito 26 Itaú": "Cartão de Crédito 26 Itau",
    "Cartão de Crédito 5 Itaú": "Cartão de Crédito 5 Itau",
    "Enincêndio": "Enincendio",
    "Darf iFood": "Darf Ifood",
    "Fiel Limpeza Caixa d'Água": "Fiel Limpeza Caixa D'agua",
    "Aster (Dedetização)": "Aster - Dedetização",
    "Emantec/iNova": "Emantec/iNOVA",
    "Clauwan/C.Villela": "Clauwan/C villela",
    "Taxa Incêndio": "Taxa Incendio",
    "Manutenção/Obras/Equip.": "Manutençao, Obras e Equipam.",
    "Falae/Experiência B2S": "Falae/Experiencia B25",
    "Empréstimos Heitor": "Emprestimos Heitor",
    "Dívidas Receita (Simples/PERT)": "Dividas Receita (simples/Pert)",
    "Despesas Bancárias": "Despesas bancárias",
    "Outros Acertos": "Outros acertos",
}

# ordem dos blocos e das linhas dentro de cada bloco, como na planilha
LAYOUT = (
    ("Entradas", ("Visa Crédito", "Master Card Crédito", "ELO Crédito", "Amex Crédito",
                  "ELO Débito", "Visa Eletron Débito", "Maestro Débito", "Dinheiro",
                  "IFOOD", "99 FOOD", "Pagamento em PIX", "Outros/Acerto")),
    ("Outras Entradas", ("Patrocínio", "Empréstimos", "Resgate")),
    ("Impostos", ("DAS", "ICMS")),
    ("Salarios", ("Salários", "Funcionário por fora", "Administrativo/Financeiro",
                  "DARFS (Funcionarios +socia)", "13º Salário", "FGTS", "Férias", "Rescisão",
                  "Dobras", "Comissão", "Vale transporte", "Vales", "Plano de saude Assim",
                  "Medicina Trabalho", "Sigaban", "Sindirefeições", "Life Card/Shalon",
                  "Funcionário Extra", "Açoes/Acordos trabalhistas")),
    ("Demais Salarios", ("Pro Labore/Lucro", "MUSICOS", "TECNICO SOM", "Segurança")),
    (GRUPO_COMPRAS, ()),          # linhas por fornecedor, na ordem do cadastro
    ("Despesas Fixas", ("Contabilidade", "Cartão de Crédito 15 Itau", "Cartão de Crédito 26 Itau",
                        "Cartão de Crédito 5 Itau", "IGUA", "Condomínio", "Aluguel", "IPTU",
                        "Light", "Gedisa (luz)", "Net", "Enincendio", "Darf Ifood",
                        "Fiel Limpeza Caixa D'agua", "CEG", "Aster - Dedetização", "Atto Service",
                        "GRDJ/FREST", "Emantec/iNOVA", "Ecad", "Clauwan/C villela", "Seguro",
                        "Força Ambiental", "Marketing/Fotografias/Gráfica",
                        "Taxa Inspeção Sanitária", "Taxa Incendio", "Tuap",
                        "Manutençao, Obras e Equipam.", "Nutricionista", "NixConsultoria",
                        "Versatily toldo", "Virtual Market", "Control ID", "Abrasel",
                        "Assessoria Financeira", "Falae/Experiencia B25", "Jurídico",
                        "Outros DARJ/DARM/DIFAL")),
    ("Outras Despesas", ("Emprestimos Heitor", "Dividas Receita (simples/Pert)", "Aplicação",
                         "Despesas bancárias", "Reembolso Barbara", "Multas", "Outros acertos")),
    (GRUPO_MEMORIA, ("ENTRADA", "RENDIMENTO", "RESGATE")),
    (GRUPO_A_CLASSIFICAR, ()),    # linhas-ônibus: aparecem só quando têm valor
)

LINHAS_FIXAS = dict(LAYOUT)

# o bloco de referência copia linhas que já estão somadas em outros blocos
MEMORIA = {
    "ENTRADA": ("Outras Despesas", "Aplicação"),
    "RESGATE": ("Outras Entradas", "Resgate"),
}

TIPO_DO_BLOCO = {
    "Entradas": "entrada", "Outras Entradas": "entrada", GRUPO_MEMORIA: "entrada",
}


def _linha_nova(nome: str) -> dict:
    return {"nome": nome, "por_dia": {}, "total": ZERO}


def _somar(destino: dict, origem: dict) -> None:
    for dia, valor in origem["por_dia"].items():
        destino["por_dia"][dia] = destino["por_dia"].get(dia, ZERO) + valor
    destino["total"] += origem["total"]


def aplicar(grupos: dict, ordem_fornecedores=None) -> dict:
    """Reescreve a grade do sistema no layout da planilha."""
    destino = {}
    for bloco, linhas in LAYOUT:
        destino[bloco] = {
            "tipo": TIPO_DO_BLOCO.get(bloco, "saida"),
            "memoria": bloco == GRUPO_MEMORIA,
            "categorias": {nome: _linha_nova(nome) for nome in linhas},
            "total_por_dia": {}, "total": ZERO,
        }

    sobras = {}
    for nome_grupo, g in grupos.items():
        bloco = BLOCO.get(nome_grupo)
        if bloco is None:                      # categoria nova: não some da tela
            alvo = sobras.setdefault(nome_grupo, {"tipo": g["tipo"], "memoria": False,
                                                  "categorias": {}, "total_por_dia": {},
                                                  "total": ZERO})
        else:
            alvo = destino[bloco]
        for linha in g["categorias"].values():
            # fornecedor entra pelo próprio nome; o resto pela linha da planilha
            nome = APELIDO.get(linha["nome"], linha["nome"])
            _somar(alvo["categorias"].setdefault(nome, _linha_nova(nome)), linha)

    # bloco de referência: repete o que já está somado em outros blocos
    memoria = destino[GRUPO_MEMORIA]
    for nome, (bloco_origem, linha_origem) in MEMORIA.items():
        origem = destino[bloco_origem]["categorias"].get(linha_origem)
        if origem:
            memoria["categorias"][nome] = {"nome": nome, "por_dia": dict(origem["por_dia"]),
                                           "total": origem["total"]}

    ordem = {nome: i for i, nome in enumerate(ordem_fornecedores or [])}
    saida = {}
    for bloco, _linhas in LAYOUT:
        g = destino[bloco]
        if bloco == GRUPO_COMPRAS:
            g["categorias"] = dict(sorted(g["categorias"].items(),
                                          key=lambda kv: (ordem.get(kv[0], len(ordem)), kv[0])))
        # linha fora da planilha só aparece quando tem valor no mês
        fixas = LINHAS_FIXAS[bloco]
        g["categorias"] = {k: v for k, v in g["categorias"].items()
                           if k in fixas or v["total"] or v["por_dia"]}
        if not g["categorias"] and bloco in (GRUPO_COMPRAS, GRUPO_A_CLASSIFICAR):
            continue
        for linha in g["categorias"].values():
            for dia, valor in linha["por_dia"].items():
                g["total_por_dia"][dia] = g["total_por_dia"].get(dia, ZERO) + valor
            g["total"] += linha["total"]
        saida[bloco] = g
    saida.update(sobras)
    return saida
