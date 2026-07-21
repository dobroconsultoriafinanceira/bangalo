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
