#!/bin/sh
# Inicialização do container web: espera o Postgres, aplica migrations, roda o
# seed (idempotente) e sobe o Gunicorn. Mesmo fluxo do sistema interno da
# consultoria — assim `git pull` na VPS já deixa o banco no schema certo.
set -e

echo "========================================"
echo " Bangalô — inicializando aplicacao"
echo "========================================"

echo "--> Aguardando banco de dados..."
python - <<'PYEOF'
import os, sys, time

url = os.environ.get("DATABASE_URL", "")
if "postgres" not in url:
    print("Sem Postgres na DATABASE_URL; pulando verificacao.")
    sys.exit(0)

import psycopg2

for tentativa in range(1, 61):
    try:
        psycopg2.connect(url).close()
        print(f"Banco disponivel (tentativa {tentativa}).")
        sys.exit(0)
    except Exception as erro:  # noqa: BLE001
        print(f"Tentativa {tentativa}/60: aguardando... ({erro})")
        time.sleep(2)

print("ERRO: banco nao ficou disponivel em 120s.")
sys.exit(1)
PYEOF

# Comando avulso (`docker compose run --rm web <cmd>`): roda so ele, depois de o
# banco estar de pe. Sem isto, tarefas pontuais como `flask db upgrade` ou o
# script de migracao disparariam tambem o seed e o Gunicorn por cima.
if [ "$#" -gt 0 ]; then
    echo "--> Comando avulso: $*"
    exec "$@"
fi

echo "--> Rodando flask db upgrade..."
flask db upgrade

echo "--> Rodando flask seed (idempotente)..."
flask seed

echo "--> Iniciando Gunicorn..."
exec gunicorn \
    --bind 0.0.0.0:8000 \
    --workers "${GUNICORN_WORKERS:-2}" \
    --timeout "${GUNICORN_TIMEOUT:-120}" \
    --access-logfile - \
    --error-logfile - \
    wsgi:app
