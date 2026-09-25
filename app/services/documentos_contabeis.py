# -*- coding: utf-8 -*-
"""Documentos do fechamento contábil: guardar, listar e conferir/aprovar.

Fluxo: a contabilidade envia o razão (.xls ou .xlsx) e o DRE em PDF; a gerência
confere contra a previsão do sistema e a consultoria aprova. Cada passo fica
na auditoria. Não faz commit — quem chama decide.
"""
from datetime import datetime
from pathlib import Path

from flask_login import current_user

from app.extensions import db
from app.models.documentos import SITUACOES_DOCUMENTO, DocumentoContabil
from app.services import arquivos, auditoria

EXTENSOES = {"razao": {".xls", ".xlsx"}, "dre_pdf": {".pdf"}, "outro": {".pdf", ".xls", ".xlsx", ".csv"}}


def _usuario_id():
    return current_user.id if getattr(current_user, "is_authenticated", False) else None


def listar(ano: int, mes: int) -> list[DocumentoContabil]:
    return db.session.execute(
        db.select(DocumentoContabil).filter_by(ano=ano, mes=mes)
        .order_by(DocumentoContabil.recebido_em.desc(), DocumentoContabil.id.desc())
    ).scalars().all()


def validar_extensao(tipo: str, nome_arquivo: str) -> None:
    permitidas = EXTENSOES.get(tipo)
    if permitidas is None:
        raise ValueError("Tipo de documento inválido.")
    if Path(nome_arquivo or "").suffix.lower() not in permitidas:
        raise ValueError(f"Formato não aceito para este documento. Use {', '.join(sorted(permitidas))}.")


# assinatura dos formatos aceitos: a extensão sozinha não prova o que é o arquivo
ASSINATURAS = {
    ".pdf": (b"%PDF-",),
    ".xls": (b"\xd0\xcf\x11\xe0",),  # OLE2 (Excel 97-2003)
    ".xlsx": (b"PK\x03\x04",),
}


def validar_conteudo(nome_arquivo: str, conteudo: bytes) -> None:
    sufixo = Path(nome_arquivo or "").suffix.lower()
    if not conteudo:
        raise ValueError("O arquivo está vazio.")
    assinaturas = ASSINATURAS.get(sufixo)
    if assinaturas and not conteudo.lstrip()[:8].startswith(assinaturas):
        raise ValueError(f"O conteúdo do arquivo não é um {sufixo} válido.")


def registrar(ano: int, mes: int, tipo: str, nome_original: str, *, conteudo: bytes | None = None,
              origem: Path | None = None, observacao: str | None = None) -> DocumentoContabil:
    """Guarda o arquivo e cria o registro. Razão novo marca o anterior como substituído."""
    validar_extensao(tipo, nome_original)
    if conteudo is not None:
        validar_conteudo(nome_original, conteudo)
    subpasta = f"contabil/{ano}-{mes:02d}"
    if conteudo is not None:
        caminho, tamanho = arquivos.guardar_bytes(conteudo, subpasta, nome_original)
    else:
        caminho, tamanho = arquivos.guardar_arquivo(origem, subpasta, nome_original)

    if tipo == "razao":
        agora = datetime.utcnow()
        for anterior in db.session.execute(
            db.select(DocumentoContabil).filter_by(ano=ano, mes=mes, tipo="razao", substituido_em=None)
        ).scalars():
            anterior.substituido_em = agora

    doc = DocumentoContabil(
        ano=ano, mes=mes, tipo=tipo, nome_arquivo=nome_original[:200], caminho=caminho,
        tamanho=tamanho, observacao=(observacao or "").strip() or None, enviado_por_id=_usuario_id(),
    )
    db.session.add(doc)
    db.session.flush()
    auditoria.registrar("create", "documento_contabil", doc.id,
                        depois={"ano": ano, "mes": mes, "tipo": tipo, "arquivo": doc.nome_arquivo})
    return doc


def mudar_situacao(doc: DocumentoContabil, situacao: str, *, pode_aprovar: bool) -> None:
    """recebido → conferido (quem confere) → aprovado (quem aprova).

    Mexer num documento já aprovado também exige a permissão de aprovar.
    """
    if situacao not in SITUACOES_DOCUMENTO:
        raise ValueError("Situação inválida.")
    if (situacao == "aprovado" or doc.situacao == "aprovado") and not pode_aprovar:
        raise PermissionError("Só quem aprova o fechamento pode aprovar ou reabrir um documento aprovado.")
    antes = doc.situacao
    agora = datetime.utcnow()
    doc.situacao = situacao
    if situacao == "recebido":
        doc.conferido_por_id = doc.conferido_em = doc.aprovado_por_id = doc.aprovado_em = None
    elif situacao == "conferido":
        doc.conferido_por_id, doc.conferido_em = _usuario_id(), agora
        doc.aprovado_por_id = doc.aprovado_em = None
    else:
        doc.aprovado_por_id, doc.aprovado_em = _usuario_id(), agora
        if not doc.conferido_em:
            doc.conferido_por_id, doc.conferido_em = _usuario_id(), agora
    auditoria.registrar("update", "documento_contabil", doc.id,
                        antes={"situacao": antes}, depois={"situacao": situacao})
