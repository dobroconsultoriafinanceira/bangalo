# -*- coding: utf-8 -*-
"""APScheduler: backup diário (SCHEDULER_ENABLED) e sincronização do fluxo com
o Google Sheets (GOOGLE_SYNC_ENABLED, a cada GOOGLE_SYNC_INTERVAL_MIN).

Jobs rodam em thread própria — cada um empurra o app_context.
Observação: com vários workers do Gunicorn, rode o agendador em um único
processo (ver README) — o sync ainda é idempotente e tem guarda de intervalo.
"""
import logging
import subprocess

from apscheduler.schedulers.background import BackgroundScheduler

log = logging.getLogger(__name__)


def iniciar_agendador(app):
    scheduler = BackgroundScheduler(timezone=app.config["TIMEZONE"])

    if app.config.get("SCHEDULER_ENABLED"):
        def job_backup():
            try:
                subprocess.run(["bash", "scripts/backup_db.sh"], check=True, timeout=300)
                log.info("Backup diário concluído.")
            except Exception:  # noqa: BLE001 — falha de backup não derruba o app
                log.exception("Falha no backup diário.")

        scheduler.add_job(job_backup, "cron", hour=4, minute=0, id="backup_diario")

    if app.config.get("GOOGLE_SYNC_ENABLED"):
        intervalo = int(app.config.get("GOOGLE_SYNC_INTERVAL_MIN", 15))

        def job_google_sync():
            from app.services import google_sync
            with app.app_context():
                try:
                    rel = google_sync.sincronizar_fluxo(app, forcar=False)
                    if not rel.get("pulado"):
                        log.info("Sync Google: %s", rel.get("relatorio", [""])[0])
                except Exception:  # noqa: BLE001
                    log.exception("Falha na sincronização Google do fluxo.")

        scheduler.add_job(job_google_sync, "interval", minutes=intervalo,
                          id="google_sync_fluxo", max_instances=1, coalesce=True)

    if scheduler.get_jobs():
        scheduler.start()
        app.extensions["apscheduler"] = scheduler
