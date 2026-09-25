# -*- coding: utf-8 -*-
"""APScheduler: backup diário (SCHEDULER_ENABLED), sincronização do fluxo com
o Google Sheets (GOOGLE_SYNC_ENABLED, a cada GOOGLE_SYNC_INTERVAL_MIN) e do
extrato Itaú (ITAU_SYNC_ENABLED, a cada ITAU_SYNC_INTERVAL_MIN).

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

    if app.config.get("STONE_SYNC_ENABLED"):
        hora_stone = int(app.config.get("STONE_SYNC_HORA", 6))

        def job_stone():
            """A Stone libera o arquivo do dia anterior depois das 5h."""
            from datetime import timedelta

            from app.extensions import db
            from app.services import stone_import
            from app.utils.datas import hoje_sp
            with app.app_context():
                try:
                    hoje = hoje_sp()
                    inicio = hoje - timedelta(days=3)
                    repasses = stone_import.importar_repasses(inicio, hoje,
                                                              substituir_planilha=False)
                    agenda = stone_import.importar_agenda(hoje)
                    # o "Pix Stone" de cada dia é o que sobra do crédito consolidado
                    # depois de abater as bandeiras: com o arquivo novo em mãos, essa
                    # conta precisa ser refeita, senão o dia conta duas vezes
                    from app.services import itau_fluxo

                    pix = itau_fluxo.importar_periodo(inicio, hoje, substituir_planilha=False)
                    db.session.commit()
                    log.info("Stone: %s repasses, agenda de %s dias, PIX refeito em %s dia(s).",
                             repasses["inseridos"], agenda["dias"], pix["pix_stone"]["dias"])
                except Exception:  # noqa: BLE001
                    db.session.rollback()
                    log.exception("Falha na sincronização da Stone")

        scheduler.add_job(job_stone, "cron", hour=hora_stone, minute=10,
                          id="stone_sync", max_instances=1, coalesce=True)

    if app.config.get("ITAU_SYNC_ENABLED"):
        intervalo_itau = int(app.config.get("ITAU_SYNC_INTERVAL_MIN", 60))
        dias_itau = int(app.config.get("ITAU_SYNC_DIAS", 35))

        def job_itau_sync():
            from datetime import timedelta

            from app.extensions import db
            from app.services import itau_sync
            from app.utils.datas import hoje_sp
            with app.app_context():
                try:
                    rel = itau_sync.sincronizar(hoje_sp() - timedelta(days=dias_itau))
                    db.session.commit()
                    log.info("Sync Itaú: %s novos, %s conciliados.",
                             rel["inseridos"], rel["conciliacao"]["conciliados"])
                except Exception:  # noqa: BLE001
                    db.session.rollback()
                    log.exception("Falha na sincronização do extrato Itaú.")

        scheduler.add_job(job_itau_sync, "interval", minutes=intervalo_itau,
                          id="itau_sync_extrato", max_instances=1, coalesce=True)

    if scheduler.get_jobs():
        scheduler.start()
        app.extensions["apscheduler"] = scheduler
