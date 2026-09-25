.PHONY: setup up down restart build build-clean deploy logs ps shell db-shell backup seed caddy-reload

# ── Primeira vez no servidor ────────────────────────────────────────────────
# Instala o git hook que faz deploy automatico a cada "git pull"
setup:
	@cp scripts/post-merge .git/hooks/post-merge
	@chmod +x .git/hooks/post-merge
	@echo "Git hook instalado. Proximos 'git pull' com mudancas farão deploy automatico."

# ── Operacoes do dia a dia ──────────────────────────────────────────────────
up:
	docker compose up -d

down:
	docker compose down

restart:
	docker compose restart web

# Reconstroi com cache (rapido — so reinstala se requirements.txt mudou)
build:
	docker compose build web
	docker compose up -d web

# Rebuild forcado sem cache (use quando suspeitar de pacote corrompido)
build-clean:
	docker compose build --no-cache web
	docker compose up -d web

# Deploy manual completo (pull + rebuild com cache)
deploy:
	git pull
	docker compose build web
	docker compose up -d web

# ── Logs ────────────────────────────────────────────────────────────────────
logs:
	docker compose logs -f web

logs-db:
	docker compose logs -f db

# ── Utilitarios ─────────────────────────────────────────────────────────────
ps:
	docker compose ps

shell:
	docker compose exec web sh

db-shell:
	docker compose exec db psql -U $${POSTGRES_USER:-bangalo} -d $${POSTGRES_DB:-bangalo}

# O entrypoint ja roda migrations + seed a cada start; este alvo e para rodar avulso
seed:
	docker compose exec web flask seed

backup:
	docker compose exec -T db pg_dump -U $${POSTGRES_USER:-bangalo} -d $${POSTGRES_DB:-bangalo} \
		> bangalo_backup_$$(date +%Y%m%d_%H%M%S).sql
	@echo "Backup salvo em bangalo_backup_*.sql"

# Recarrega o Caddy do stack do Dobro (depois de editar /root/dobro/Caddyfile)
caddy-reload:
	docker compose -f /root/dobro/docker-compose.yml exec caddy \
		caddy reload --config /etc/caddy/Caddyfile
