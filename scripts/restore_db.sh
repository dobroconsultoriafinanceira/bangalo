#!/usr/bin/env bash
# Restauração de um backup gerado por backup_db.sh.
# Uso: ./scripts/restore_db.sh backups/bangalo_2026-07-21_0300.sql.gz
set -euo pipefail

ARQUIVO="${1:?Informe o arquivo .sql.gz do backup}"
echo "ATENÇÃO: isto sobrescreve o banco atual. Ctrl+C para abortar."
sleep 5

gunzip -c "$ARQUIVO" | docker compose exec -T db psql -U "${POSTGRES_USER:-bangalo}" -d "${POSTGRES_DB:-bangalo}"
echo "Restauração concluída de $ARQUIVO"
