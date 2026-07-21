# -*- coding: utf-8 -*-
"""APScheduler opcional (SCHEDULER_ENABLED=true): backup diário do banco.

A importação recorrente de planilhas (F-Rest) fica como TODO futuro —
ver importers/frest_adapter.py.
"""
import logging
import subprocess

from apscheduler.schedulers.background import BackgroundScheduler

log = logging.getLogger(__name__)


def iniciar_agendador(app):
    scheduler = BackgroundScheduler(timezone=app.config["TIMEZONE"])

    def job_backup():
        # Em produção o backup roda via scripts/backup_db.sh (pg_dump).
        try:
            subprocess.run(["bash", "scripts/backup_db.sh"], check=True, timeout=300)
            log.info("Backup diário concluído.")
        except Exception:  # noqa: BLE001 — falha de backup não pode derrubar o app
            log.exception("Falha no backup diário.")

    scheduler.add_job(job_backup, "cron", hour=4, minute=0, id="backup_diario")
    scheduler.start()
    app.extensions["apscheduler"] = scheduler
