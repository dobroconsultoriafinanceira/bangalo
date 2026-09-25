#!/usr/bin/env bash
# Backup diário do banco (pg_dump). Dados financeiros e de folha EXIGEM backup.
# Uso na VPS (cron diário às 03:00):
#   0 3 * * * cd /srv/bangalo && ./scripts/backup_db.sh >> /var/log/bangalo_backup.log 2>&1
set -euo pipefail

DATA=$(date +%Y-%m-%d_%H%M)
DESTINO="${BANGALO_BACKUP_DIR:-./backups}"
RETENCAO_DIAS="${BANGALO_BACKUP_RETENCAO:-30}"
mkdir -p "$DESTINO"

ARQUIVO="$DESTINO/bangalo_${DATA}.sql.gz"

# Roda o pg_dump dentro do container do Postgres do compose
docker compose exec -T db pg_dump -U "${POSTGRES_USER:-bangalo}" "${POSTGRES_DB:-bangalo}" \
  | gzip > "$ARQUIVO"

echo "Backup gerado: $ARQUIVO"

# Remove backups mais antigos que a retenção configurada
find "$DESTINO" -name 'bangalo_*.sql.gz' -mtime "+${RETENCAO_DIAS}" -delete

# LGPD: proteja o diretório de backups (dados pessoais/sensíveis).
#   chmod 700 "$DESTINO"  e restrinja acesso; considere cifrar em repouso.
