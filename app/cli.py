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

    @app.cli.command("itau-certificado")
    @click.option("--ou", prompt="Site ou app (OU)", default="bangalo",
                  help="Identificador do site/app, sem caracteres especiais.")
    @click.option("--cidade", prompt="Cidade", help="Cidade, sem acentos.")
    @click.option("--uf", prompt="UF (sigla)", help="Estado, ex.: SP.")
    @click.option("--forcar", is_flag=True, help="Renova mesmo se já houver certificado.")
    @click.option("--token", prompt="Token temporário do Devportal (vale 5 min)",
                  hide_input=True, help="Token temporário gerado junto com as credenciais.")
    def itau_certificado(ou, cidade, uf, forcar, token):
        """Gera chave + CSR e obtém o certificado dinâmico do Itaú (etapa 2).

        Gere o token temporário no Devportal logo antes de rodar: ele vale 5 min.
        Arquivos gravados em ITAU_CERT_DIR (padrão instance/itau, fora do git).
        """
        from app.importers.itau_adapter import ItauClient, ItauConfig, gerar_chave_e_csr

        cfg = ItauConfig.from_app(app)
        if not cfg.client_id:
            raise click.ClickException("Preencha ITAU_CLIENT_ID no .env antes (gerado no Devportal).")
        cert, chave = Path(cfg.cert_path), Path(cfg.key_path)
        if cert.exists() and chave.exists() and not forcar:
            raise click.ClickException(
                f"Já existe certificado em {cert}. Use --forcar para renovar."
            )

        chave_pem, csr_pem = gerar_chave_e_csr(cfg.client_id, ou, cidade, uf)
        if len(csr_pem.decode().strip().splitlines()) <= 15:
            click.echo("  Aviso: CSR com 15 linhas ou menos (o Itaú pede mais de 15).")

        secret, cert_pem = ItauClient(cfg).solicitar_certificado(token, csr_pem)

        # só grava depois do sucesso — não sobrescreve um certificado válido à toa
        cert.parent.mkdir(parents=True, exist_ok=True)
        chave.write_bytes(chave_pem)
        chave.chmod(0o600)
        chave.with_suffix(".csr").write_bytes(csr_pem)
        cert.write_text(cert_pem, encoding="utf-8")
        click.echo(f"  Certificado: {cert}")
        click.echo(f"  Chave privada: {chave}")
        if secret:
            arq_secret = cert.parent / "client_secret.txt"
            arq_secret.write_text(secret + "\n", encoding="utf-8")
            arq_secret.chmod(0o600)
            click.echo(f"  Client secret retornado ({secret[:4]}…) salvo em {arq_secret}")
            click.echo("  -> copie para ITAU_CLIENT_SECRET no .env e apague o arquivo.")
        click.echo("Certificado dinâmico emitido (validade de 1 ano).")

    @app.cli.command("itau-testar")
    @click.option("--conta", help="agência-conta-DAC; padrão: todas de ITAU_CONTAS.")
    @click.option("--desde", help="AAAA-MM-DD; padrão: 7 dias atrás.")
    def itau_testar(conta, desde):
        """Token + 1ª página do extrato e saldo, gravando o JSON bruto (etapas 3-4)."""
        import json
        from datetime import date, datetime, timedelta

        from app.importers.itau_adapter import (
            ItauClient, ItauConfig, ItauErroAPI, normalizar_conta, normalizar_extrato,
            normalizar_saldo,
        )

        cfg = ItauConfig.from_app(app)
        contas = [conta] if conta else cfg.contas
        if not contas:
            raise click.ClickException("Informe --conta ou preencha ITAU_CONTAS no .env.")
        inicio = date.fromisoformat(desde) if desde else date.today() - timedelta(days=7)
        client = ItauClient(cfg)
        try:
            client.obter_token()
            click.echo(f"  Token OK ({cfg.ambiente}).")
            for c in contas:
                conta_id = normalizar_conta(c)
                bruto = client.extrato_bruto(conta_id, inicio)
                pasta = Path(cfg.cert_path).parent / "respostas"
                pasta.mkdir(parents=True, exist_ok=True)
                arq = pasta / f"extrato_{conta_id}_{datetime.now():%Y%m%d_%H%M%S}.json"
                arq.write_text(json.dumps(bruto, ensure_ascii=False, indent=2), encoding="utf-8")
                txns = normalizar_extrato(bruto, conta=conta_id)
                saldo = normalizar_saldo(bruto, conta=conta_id)
                pag = bruto.get("pagination") or {}
                click.echo(f"  conta {conta_id}: {pag.get('total_elements')} lançamentos desde "
                           f"{inicio} ({pag.get('total_pages')} páginas); 1ª página: {len(txns)} reconhecidos")
                click.echo(f"  saldo disponível R$ {saldo.saldo} | bloqueado R$ {saldo.saldo_bloqueado} "
                           f"| aplic. automática R$ {saldo.saldo_aplicacao_automatica}")
                for t in txns[:5]:
                    click.echo(f"    {t.data} {t.tipo:7} {t.valor:>12} {t.descricao[:40]}")
                click.echo(f"  JSON bruto: {arq}")
        except ItauErroAPI as exc:
            click.echo(f"  {exc}")
            if exc.status in (401, 403):
                click.echo("  Dica: credenciais/escopos são liberados em até 2 dias úteis; "
                           "confira também se o certificado é o do mesmo client_id.")
        except (RuntimeError, ValueError) as exc:
            raise click.ClickException(str(exc))

    @app.cli.command("itau-sincronizar")
    @click.argument("inicio")                  # AAAA-MM-DD
    @click.argument("fim", required=False)     # AAAA-MM-DD (padrão: hoje)
    def itau_sincronizar(inicio, fim):
        """Grava o extrato Itaú desde INICIO e concilia com o fluxo.

        Ex.: flask itau-sincronizar 2026-09-01
        """
        from datetime import date

        from app.extensions import db
        from app.services import itau_sync

        try:
            rel = itau_sync.sincronizar(
                date.fromisoformat(inicio), date.fromisoformat(fim) if fim else None
            )
        except (RuntimeError, ValueError) as exc:
            db.session.rollback()
            raise click.ClickException(str(exc))
        db.session.commit()
        for conta, info in rel["contas"].items():
            click.echo(f"  conta {conta}: {info}")
        click.echo(f"  conciliação: {rel['conciliacao']}")
        click.echo("Sincronização Itaú concluída.")
