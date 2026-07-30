# -*- coding: utf-8 -*-
"""Comandos CLI: `flask seed`, `flask importar`."""
from pathlib import Path

import click
from flask import current_app


def registrar_cli(app):
    @app.cli.command("seed")
    def seed():
        """Semeia admin, setores/funções, plano de contas e premissas."""
        from app.seeds import rodar_tudo

        for linha in rodar_tudo():
            click.echo("  " + linha)
        click.echo("Seed concluído.")

    @app.cli.command("importar")
    @click.argument("tipo", type=click.Choice(["metas", "fluxo", "gorjetas"]))
    @click.argument("caminho", type=click.Path(exists=True))
    def importar(tipo, caminho):
        """Importa uma planilha (one-shot). Ex.: flask importar metas arquivo.xlsx"""
        from app.extensions import db
        from app.importers import fluxo_importer, gorjetas_importer, metas_importer

        importadores = {
            "metas": metas_importer,
            "fluxo": fluxo_importer,
            "gorjetas": gorjetas_importer,
        }
        relatorio = importadores[tipo].importar(Path(caminho))
        db.session.commit()
        for linha in relatorio:
            click.echo("  " + linha)
        click.echo(f"Importação '{tipo}' concluída.")

    @app.cli.command("stone-importar")
    @click.argument("inicio")  # AAAA-MM-DD
    @click.argument("fim")     # AAAA-MM-DD
    def stone_importar(inicio, fim):
        """Baixa a conciliação Stone do período e importa como entradas.

        Ex.: flask stone-importar 2026-07-01 2026-07-15
        Exige STONE_* configurados no .env.
        """
        from datetime import date

        from app.extensions import db
        from app.services import stone_import

        rel = stone_import.importar_periodo(
            date.fromisoformat(inicio), date.fromisoformat(fim)
        )
        db.session.commit()
        for k, v in rel.items():
            click.echo(f"  {k}: {v}")
        click.echo("Importação Stone concluída.")

    @app.cli.command("stone-importar-csv")
    @click.argument("caminho", type=click.Path(exists=True))
    def stone_importar_csv(caminho):
        """Importa um CSV de conciliação Stone já exportado."""
        from app.extensions import db
        from app.services import stone_import

        with open(caminho, "rb") as fh:
            rel = stone_import.importar_arquivo_csv(fh.read())
        db.session.commit()
        for k, v in rel.items():
            click.echo(f"  {k}: {v}")
        click.echo("Importação Stone (CSV) concluída.")

    @app.cli.command("google-sincronizar")
    def google_sincronizar():
        """Sincroniza o fluxo com a planilha do Google (Service Account)."""
        from app.services import google_sync

        rel = google_sync.sincronizar_fluxo(app, forcar=True)
        for linha in rel.get("relatorio", []):
            click.echo("  " + linha)
        click.echo("OK" if rel.get("ok") else "Não sincronizado.")

    @app.cli.command("itau-sincronizar")
    @click.argument("inicio")  # AAAA-MM-DD
    @click.argument("fim")     # AAAA-MM-DD
    def itau_sincronizar(inicio, fim):
        """Sincroniza extrato Itaú do período (exige credenciais/certificado).

        Scaffold: a estrutura está pronta; a chamada real é habilitada quando
        os certificados mTLS e client_id/secret estiverem no .env.
        """
        from datetime import date

        from app.importers.itau_adapter import ItauClient, ItauConfig

        cfg = ItauConfig.from_app(app)
        if not cfg.configurada:
            click.echo("Credenciais Itaú ausentes — configure ITAU_* no .env (ver README).")
            return
        client = ItauClient(cfg)
        try:
            for conta in cfg.contas or [""]:
                txns = client.extrato(conta, date.fromisoformat(inicio), date.fromisoformat(fim))
                click.echo(f"  conta {conta}: {len(txns)} transações")
        except (RuntimeError, NotImplementedError) as exc:
            click.echo(f"  {exc}")
