# -*- coding: utf-8 -*-
"""Sincronização do Fluxo de Caixa a partir do Google Sheets (só leitura).

O servidor lê a planilha com uma **conta de serviço** (Service Account) do
Google, à qual a planilha é compartilhada como *Leitor*. Nenhuma edição é
feita — apenas exportamos a aba e rodamos o importador de fluxo (idempotente,
resiliente a mudança de layout).

Duas formas de disparo:
  - agendada (APScheduler, a cada GOOGLE_SYNC_INTERVAL_MIN) — ver utils/agendador
  - manual (botão "Sincronizar agora" no Admin)

As bibliotecas do Google são importadas de forma preguiçosa: o app roda sem
elas até a sincronização ser efetivamente usada.
"""
import tempfile
from datetime import datetime, timedelta
from pathlib import Path

from flask import current_app

from app.extensions import db
from app.importers import fluxo_importer
from app.models.fluxo import ConfigSistema
from app.utils.datas import agora_sp

CHAVE_ULTIMA_SYNC = "fluxo_ultima_sincronizacao"     # ISO datetime
CHAVE_ULTIMO_RELATORIO = "fluxo_ultimo_relatorio_sync"
XLSX_MIME = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


def configurado(app=None) -> bool:
    app = app or current_app
    sa = app.config.get("GOOGLE_SERVICE_ACCOUNT_JSON", "")
    sheet = app.config.get("GOOGLE_SHEETS_FLUXO_ID", "")
    return bool(sa and sheet and Path(sa).is_file())


def ultima_sincronizacao() -> datetime | None:
    txt = ConfigSistema.obter(CHAVE_ULTIMA_SYNC)
    if not txt:
        return None
    try:
        return datetime.fromisoformat(txt)
    except ValueError:
        return None


def _exportar_xlsx(app) -> bytes:
    """Exporta a planilha (aba inteira) como XLSX usando a Service Account."""
    try:
        from google.oauth2 import service_account
        from googleapiclient.discovery import build
    except ImportError as exc:  # noqa: BLE001
        raise RuntimeError(
            "Bibliotecas do Google ausentes. Instale google-api-python-client e "
            "google-auth (já estão no requirements.txt)."
        ) from exc

    cred = service_account.Credentials.from_service_account_file(
        app.config["GOOGLE_SERVICE_ACCOUNT_JSON"],
        scopes=["https://www.googleapis.com/auth/drive.readonly"],
    )
    drive = build("drive", "v3", credentials=cred, cache_discovery=False)
    return drive.files().export(
        fileId=app.config["GOOGLE_SHEETS_FLUXO_ID"], mimeType=XLSX_MIME
    ).execute()


def sincronizar_fluxo(app=None, forcar: bool = False) -> dict:
    """Puxa a planilha e reimporta o fluxo. Retorna {ok, quando, relatorio}.

    `forcar=False` (agendador) respeita o intervalo mínimo para evitar
    sincronizações repetidas quando há mais de um worker/processo.
    """
    app = app or current_app
    if not configurado(app):
        return {"ok": False, "relatorio": [
            "Sincronização Google não configurada. Defina GOOGLE_SERVICE_ACCOUNT_JSON "
            "e GOOGLE_SHEETS_FLUXO_ID no .env e compartilhe a planilha com a conta de "
            "serviço (ver README › Sincronização Google Sheets)."
        ]}

    if not forcar:
        intervalo = int(app.config.get("GOOGLE_SYNC_INTERVAL_MIN", 15))
        ult = ultima_sincronizacao()
        if ult and agora_sp().replace(tzinfo=None) - ult < timedelta(minutes=intervalo - 1):
            return {"ok": True, "pulado": True, "relatorio": ["Sincronização recente — pulada."]}

    conteudo = _exportar_xlsx(app)
    with tempfile.NamedTemporaryFile(suffix=".xlsx", delete=False) as tmp:
        tmp.write(conteudo)
        caminho = Path(tmp.name)
    try:
        relatorio = fluxo_importer.importar(caminho)
        quando = agora_sp().replace(tzinfo=None)
        ConfigSistema.definir(CHAVE_ULTIMA_SYNC, quando.isoformat())
        ConfigSistema.definir(CHAVE_ULTIMO_RELATORIO, "\n".join(relatorio))
        db.session.commit()
    finally:
        caminho.unlink(missing_ok=True)

    return {"ok": True, "quando": quando, "relatorio": relatorio}
