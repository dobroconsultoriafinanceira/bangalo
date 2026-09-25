# -*- coding: utf-8 -*-
"""Copia TODOS os dados do SQLite de desenvolvimento para o Postgres de produção.

Premissa: o Postgres já está com o schema criado por `flask db upgrade`, na
mesma revisão do Alembic que o SQLite de origem. O script não cria nem altera
schema — só move linhas.

Por que usar o metadata da aplicação em vez de ler o SQLite "cru": os tipos
(Numeric, Boolean, Date) são reconstruídos pelo SQLAlchemy na leitura e
gravados com o tipo certo no Postgres. Ler com sqlite3 puro traria Decimal
como float e data como texto — em sistema financeiro isso é perda silenciosa.

Uso (dentro do container web, com o .db copiado para dentro):
    python scripts/migrar_sqlite_para_postgres.py --sqlite /tmp/bangalo_dev.db
    python scripts/migrar_sqlite_para_postgres.py --sqlite /tmp/x.db --conferir

`--conferir` só compara as contagens dos dois lados, sem escrever nada.
"""
import argparse
import sys
from pathlib import Path

# Rodado como `python scripts/...`, o sys.path aponta para scripts/ e o pacote
# `app` da raiz nao seria encontrado.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import create_engine, func, inspect, select, text  # noqa: E402

# Tabelas que NÃO atravessam: a versão do Alembic é responsabilidade do
# `flask db upgrade`, e tabelas `_alembic_tmp_*` são sobras de migrations
# interrompidas (batch mode do SQLite), sem valor de dado.
IGNORAR = {"alembic_version"}
LOTE = 500


def _tabelas_da_aplicacao():
    from app import create_app
    from app.extensions import db

    app = create_app()
    with app.app_context():
        # sorted_tables = ordem topológica das FKs (pais antes dos filhos)
        return app, [t for t in db.metadata.sorted_tables if t.name not in IGNORAR]


def _revisao(engine):
    with engine.connect() as con:
        try:
            return con.execute(text("select version_num from alembic_version")).scalar()
        except Exception:  # noqa: BLE001 — tabela pode não existir
            return None


def _contagens(engine, tabelas):
    contagens = {}
    with engine.connect() as con:
        existentes = set(inspect(engine).get_table_names())
        for tabela in tabelas:
            if tabela.name in existentes:
                contagens[tabela.name] = con.execute(
                    select(func.count()).select_from(tabela)).scalar()
            else:
                contagens[tabela.name] = None
    return contagens


def _ressincronizar_sequences(destino, tabelas):
    """Sem isto, o primeiro INSERT do app estoura chave duplicada: a sequence
    continua em 1 enquanto os IDs copiados já vão até N."""
    ajustadas = 0
    with destino.begin() as con:
        for tabela in tabelas:
            for coluna in tabela.columns:
                if not (coluna.primary_key and coluna.autoincrement is not False):
                    continue
                seq = con.execute(
                    text("select pg_get_serial_sequence(:t, :c)"),
                    {"t": tabela.name, "c": coluna.name},
                ).scalar()
                if not seq:
                    continue
                con.execute(text(
                    f'select setval(:s, coalesce((select max("{coluna.name}") '
                    f'from "{tabela.name}"), 0) + 1, false)'), {"s": seq})
                ajustadas += 1
    return ajustadas


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--sqlite", required=True, help="caminho do .db de origem")
    parser.add_argument("--postgres", help="URL de destino (padrão: DATABASE_URL do app)")
    parser.add_argument("--conferir", action="store_true",
                        help="só compara contagens, não escreve nada")
    args = parser.parse_args()

    app, tabelas = _tabelas_da_aplicacao()
    destino_url = args.postgres or app.config["SQLALCHEMY_DATABASE_URI"]
    if "postgres" not in destino_url:
        sys.exit(f"ERRO: destino não é Postgres: {destino_url}")

    origem = create_engine(f"sqlite:///{args.sqlite}")
    destino = create_engine(destino_url)

    rev_origem, rev_destino = _revisao(origem), _revisao(destino)
    print(f"Alembic origem : {rev_origem}")
    print(f"Alembic destino: {rev_destino}")
    if rev_origem != rev_destino:
        sys.exit("ERRO: revisões diferentes. Rode `flask db upgrade` no destino "
                 "e/ou na origem antes de migrar.")

    if args.conferir:
        return _relatorio(origem, destino, tabelas)

    ocupadas = [n for n, c in _contagens(destino, tabelas).items() if c]
    if ocupadas:
        sys.exit("ERRO: o destino já tem dados em: " + ", ".join(ocupadas) +
                 "\nEste script só roda sobre banco vazio (evita duplicar).")

    print("\n--- copiando ---")
    total = 0
    # Tudo numa transacao so: se qualquer tabela falhar, nada fica pela metade.
    with origem.connect() as con_origem, destino.begin() as con_destino:
        for tabela in tabelas:
            linhas_tab = [dict(r) for r in con_origem.execute(select(tabela)).mappings()]
            print(f"{len(linhas_tab):>8}  {tabela.name}")
            for i in range(0, len(linhas_tab), LOTE):
                con_destino.execute(tabela.insert(), linhas_tab[i:i + LOTE])
            total += len(linhas_tab)

    ajustadas = _ressincronizar_sequences(destino, tabelas)
    print(f"\n{total} linhas copiadas; {ajustadas} sequences ressincronizadas.")
    _relatorio(origem, destino, tabelas)


def _relatorio(origem, destino, tabelas):
    print("\n--- conferência (origem x destino) ---")
    antes, depois = _contagens(origem, tabelas), _contagens(destino, tabelas)
    divergentes = []
    for nome in sorted(antes):
        a, d = antes[nome], depois[nome]
        marca = "  OK" if a == d else "  <<< DIVERGE"
        if a != d:
            divergentes.append(nome)
        print(f"{str(a):>8} {str(d):>8}  {nome}{marca}")
    if divergentes:
        sys.exit("\nFALHOU: divergência em " + ", ".join(divergentes))
    print("\nTodas as tabelas conferem.")


if __name__ == "__main__":
    main()
