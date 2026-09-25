# -*- coding: utf-8 -*-
"""Regras de classificação do extrato: listar, prever o alcance, salvar e excluir.

Uma regra vale para os movimentos NÃO manuais — inclusive os anteriores: ao
salvar ou excluir, `itau_sync.reaplicar_regras()` reclassifica tudo. Por isso a
tela mostra a prévia (quantos movimentos casam e quantos mudam de categoria)
antes de gravar. Não faz commit.
"""
from dataclasses import dataclass, field

from app.extensions import db
from app.models.banco import CAMPOS_REGRA, MovimentoBancario, RegraClassificacaoBancaria
from app.services import auditoria, itau_sync
from app.services import classificacao_bancaria as cls

ROTULOS_CAMPO = {
    "contraparte_documento": "CPF/CNPJ da contraparte é",
    "contraparte_nome": "Nome da contraparte contém",
    "descricao": "Descrição contém",
}
TIPOS = {"": "Créditos e débitos", "credito": "Só créditos (entradas)", "debito": "Só débitos (saídas)"}


@dataclass
class Previa:
    casam: int = 0            # movimentos não manuais em que a regra seria a vencedora
    mudam: int = 0            # desses, quantos trocariam de categoria
    manuais: int = 0          # manuais que casam, mas não são alterados
    exemplos: list = field(default_factory=list)


def _movimentos():
    return db.session.execute(
        db.select(MovimentoBancario).filter_by(banco=itau_sync.BANCO)
        .order_by(MovimentoBancario.data.desc(), MovimentoBancario.id.desc())
    ).scalars().all()


def validar(campo: str, valor: str, tipo: str, categoria: str) -> tuple[str, str, str | None, str]:
    if campo not in CAMPOS_REGRA:
        raise ValueError("Escolha o critério da regra.")
    valor = (valor or "").strip()
    if campo == "contraparte_documento":
        valor = cls.digitos(valor)
        if len(valor) not in (11, 14):
            raise ValueError("Informe um CPF (11 dígitos) ou CNPJ (14 dígitos).")
    elif len(cls.normalizar(valor)) < 3:
        raise ValueError("O texto da regra precisa ter pelo menos 3 letras.")
    if tipo not in TIPOS:
        raise ValueError("Tipo de movimento inválido.")
    if categoria not in cls.CATEGORIAS:
        raise ValueError("Escolha uma categoria bancária.")
    return campo, valor[:160], (tipo or None), categoria


def previa(campo: str, valor: str, tipo: str | None, categoria: str, regra_id: int | None = None,
           limite_exemplos: int = 8) -> Previa:
    """Simula a classificação com a regra nova/editada na posição em que ficaria."""
    ativas = db.session.execute(
        db.select(RegraClassificacaoBancaria).filter_by(ativo=True).order_by(RegraClassificacaoBancaria.id)
    ).scalars().all()
    candidata = RegraClassificacaoBancaria(id=regra_id, campo=campo, valor=valor, tipo=tipo,
                                           categoria=categoria, revisar=False, ativo=True)
    regras = [r for r in ativas if r.id != regra_id]
    if regra_id:  # editar mantém a posição (ordem de criação)
        pos = next((i for i, r in enumerate(regras) if r.id > regra_id), len(regras))
        regras.insert(pos, candidata)
    else:
        regras.append(candidata)

    raiz = itau_sync._cnpj_raiz()
    resultado = Previa()
    for mov in _movimentos():
        if not cls.regra_casa(candidata, mov):
            continue
        if mov.classificacao_manual:
            resultado.manuais += 1
            continue
        vencedora = next((r for r in regras if r.categoria in cls.CATEGORIAS and cls.regra_casa(r, mov)), None)
        if vencedora is not candidata:
            continue
        r = cls.classificar(mov, regras, raiz)
        resultado.casam += 1
        if r.categoria != mov.categoria_gerencial:
            resultado.mudam += 1
            if len(resultado.exemplos) < limite_exemplos:
                resultado.exemplos.append((mov, cls.CATEGORIAS.get(mov.categoria_gerencial)))
    return resultado


def listar() -> list[dict]:
    """Regras com o alcance atual (movimentos que cada uma classifica hoje)."""
    regras = db.session.execute(
        db.select(RegraClassificacaoBancaria).order_by(RegraClassificacaoBancaria.ativo.desc(),
                                                       RegraClassificacaoBancaria.id)
    ).scalars().all()
    movimentos = [m for m in _movimentos() if not m.classificacao_manual]
    ativas = [r for r in regras if r.ativo and r.categoria in cls.CATEGORIAS]
    alcance = {r.id: 0 for r in regras}
    for mov in movimentos:
        vencedora = next((r for r in ativas if cls.regra_casa(r, mov)), None)
        if vencedora:
            alcance[vencedora.id] += 1
    return [{"regra": r, "categoria": cls.CATEGORIAS.get(r.categoria), "alcance": alcance[r.id],
             "criterio": ROTULOS_CAMPO.get(r.campo, r.campo), "tipo": TIPOS.get(r.tipo or "", r.tipo)}
            for r in regras]


def salvar(campo: str, valor: str, tipo: str, categoria: str, *, regra_id: int | None = None,
           ativo: bool = True, revisar: bool = False, observacao: str | None = None) -> tuple[RegraClassificacaoBancaria, int]:
    campo, valor, tipo_ok, categoria = validar(campo, valor, tipo, categoria)
    duplicada = db.session.execute(
        db.select(RegraClassificacaoBancaria).filter_by(campo=campo, valor=valor, tipo=tipo_ok)
    ).scalar_one_or_none()
    if duplicada and duplicada.id != regra_id:
        raise ValueError("Já existe uma regra com esse critério. Edite a regra existente.")

    if regra_id:
        regra = db.session.get(RegraClassificacaoBancaria, regra_id)
        if regra is None:
            raise LookupError("Regra não encontrada.")
        antes = {"campo": regra.campo, "valor": regra.valor, "tipo": regra.tipo,
                 "categoria": regra.categoria, "ativo": regra.ativo}
    else:
        regra, antes = RegraClassificacaoBancaria(), None
        db.session.add(regra)
    regra.campo, regra.valor, regra.tipo, regra.categoria = campo, valor, tipo_ok, categoria
    regra.ativo, regra.revisar = ativo, revisar
    regra.observacao = (observacao or "").strip()[:255] or None
    db.session.flush()
    alterados = itau_sync.reaplicar_regras()
    auditoria.registrar("update" if antes else "create", "regra_classificacao_bancaria", regra.id, antes=antes,
                        depois={"campo": campo, "valor": valor, "tipo": tipo_ok, "categoria": categoria,
                                "ativo": ativo, "movimentos_alterados": alterados})
    return regra, alterados


def excluir(regra: RegraClassificacaoBancaria) -> int:
    auditoria.registrar("delete", "regra_classificacao_bancaria", regra.id,
                        antes={"campo": regra.campo, "valor": regra.valor, "categoria": regra.categoria})
    db.session.delete(regra)
    db.session.flush()
    return itau_sync.reaplicar_regras()
