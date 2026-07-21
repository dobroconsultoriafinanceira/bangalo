# Bangalô — Dashboard Financeiro

Sistema web que unifica **fluxo de caixa**, **calculadora de gorjetas** e
**metas/planejamento de faturamento** do Restaurante Bangalô (Rio de Janeiro),
substituindo as planilhas operadas manualmente. Desenvolvido pela **Dobro
Consultoria Financeira**.

- **Stack:** Python 3.12 · Flask 3 · SQLAlchemy 2 · PostgreSQL 16 (SQLite em dev)
  · Jinja2 + Tailwind (CDN) + Chart.js · Gunicorn · Docker + Caddy.
- **Domínio alvo:** `bangalo.dobroconsultoriafinanceira.com.br`
- **Cálculos financeiros:** 100% em `Decimal` / `Numeric(14,2)`. As engines de
  gorjeta e meta são **validadas centavo a centavo contra as planilhas reais**
  (ver `tests/`).

> **Aviso LGPD:** o sistema guarda nomes de funcionários, salários, gorjetas e
> finanças do restaurante — dados pessoais e sensíveis. Acesso é mínimo por
> perfil, toda alteração de folha/gorjeta/lançamento é auditada, e os backups
> devem ser protegidos (ver seção Backup). Defina responsável e política de
> retenção antes de ir a produção.

---

## Índice
1. [Arquitetura](#arquitetura)
2. [Setup local (SQLite)](#setup-local-sqlite)
3. [Variáveis de ambiente](#variáveis-de-ambiente)
4. [Migrations](#migrations)
5. [Seed e importação das planilhas](#seed-e-importação-das-planilhas)
6. [Testes](#testes)
7. [Deploy na VPS (Docker + Caddy)](#deploy-na-vps-docker--caddy)
8. [DNS / subdomínio](#dns--subdomínio)
9. [Backup e restauração](#backup-e-restauração)
10. [Perfis de acesso](#perfis-de-acesso)
11. [Decisões de negócio confirmadas](#decisões-de-negócio-confirmadas)
12. [Branches sugeridas](#branches-sugeridas)

---

## Arquitetura

```
app/
  __init__.py        # application factory + headers de segurança + error pages
  config.py          # Dev/Prod/Testing via .env
  extensions.py      # db, migrate, login_manager, mail, csrf
  cli.py             # comandos: flask seed / flask importar
  seeds.py           # admin, setores/funções, plano de contas, premissas
  models/            # usuario, fluxo, gorjetas, metas, auditoria
  services/          # ENGINES puras e testáveis (gorjetas, metas, fluxo_caixa)
  blueprints/        # auth, dashboard, fluxo_caixa, gorjetas, metas, cadastros, admin
  templates/         # base.html + telas por domínio (Jinja, sem SPA)
  static/img/        # logo.svg, favicon.svg
  importers/         # ETL one-shot das 3 planilhas + stub F-Rest
  utils/             # filtros Jinja (BRL/data), datas, decorators de role
migrations/          # Alembic
scripts/             # backup_db.sh, restore_db.sh
tests/               # engines validadas + auth/permissões
```

Regras de negócio ficam em `services/` (funções puras que recebem dados e
retornam `Decimal`), **fora** das views e models. Saldos diários e subtotais do
fluxo são **derivados por consulta**, nunca armazenados.

## Setup local (SQLite)

```bash
python -m venv .venv && source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -r requirements-dev.txt
cp .env.example .env        # gere um SECRET_KEY forte
export FLASK_APP=wsgi.py FLASK_ENV=development   # Windows PowerShell: $env:FLASK_APP="wsgi.py"

python -m flask db upgrade  # cria o schema (prefira migrations mesmo em dev)
python -m flask seed        # admin + configuração inicial
python -m flask run         # http://127.0.0.1:5000
```

Login inicial: `ADMIN_EMAIL` / `ADMIN_PASSWORD` do `.env` (troque no primeiro acesso).

## Variáveis de ambiente

Ver `.env.example`. Principais: `SECRET_KEY`, `DATABASE_URL`, `POSTGRES_*`,
`MAIL_*` (SMTP Hostinger — deixe em branco para desabilitar recuperação de
senha por e-mail), `ADMIN_EMAIL`/`ADMIN_PASSWORD`, `TZ=America/Sao_Paulo`,
`SCHEDULER_ENABLED`.

## Migrations

```bash
python -m flask db migrate -m "descrição"   # gera migration a partir dos models
python -m flask db upgrade                   # aplica
```
Em produção rode **sempre** `flask db upgrade` antes de subir a nova versão.
Não use `create_all` em produção.

## Seed e importação das planilhas

O fluxo confirmado é: **importar o histórico uma única vez** e depois lançar
tudo pelo sistema (CRUD). Ordem sugerida:

```bash
python -m flask seed
python -m flask importar metas    "Metas de Faturamento 2026.xlsx"
python -m flask importar fluxo    "FLUXO DE CAIXA BANGALO ok .xlsx"
python -m flask importar gorjetas "Calculadora de Gorjetas (2026).xlsx"
```

Ou pela tela **Admin › Importação** (upload). Cada importer é **idempotente**
(rodar 2× não duplica) e emite **relatório de conferência** (totais importados
× planilha). Notas:
- **Fluxo:** só a aba `2026`. O cabeçalho traz ano visualmente errado → usamos
  mês/dia e forçamos 2026; 29/02 (inexistente em 2026) é pulado com aviso.
- **Gorjetas:** a aba `Histórico` vira snapshots imutáveis (`fechamento_gorjeta`).
- **Metas:** `Faturamento Histórico` (2022–2026) + `Registro Diário 2026` + premissas.

Integração com o PDV **F-Rest** fica como etapa futura — há um adapter isolado
(`importers/frest_adapter.py`) para importação de CSV/Excel exportado, sem
acoplar o sistema agora.

## Testes

```bash
python -m pytest
```
Cobrem as engines com os **números reais** das planilhas (gorjeta Maio/26 centavo
a centavo; pools por setor; meta JAN/2026 = R$ 457.432,27; metas diárias por peso
de dia da semana; setor vazio redistribui), consolidação de fluxo com saldos
derivados, e permissões por perfil.

## Deploy na VPS (Docker + Caddy)

```bash
git pull
docker compose build web
docker compose run --rm web flask db upgrade        # migrations ANTES de subir
docker compose up -d
docker compose run --rm web flask seed              # 1ª vez apenas
```

`docker-compose.yml` tem 3 serviços: `web` (Gunicorn, 3 workers), `db`
(postgres:16-alpine com volume persistente) e `caddy` (só no profile
`com-caddy`). Healthcheck em `/healthz`.

**Coexistência com o `dashboard.` já existente (cenário A, recomendado):** a VPS
já roda um Caddy central. Não suba o `caddy` daqui — em vez disso:
1. crie uma rede docker compartilhada: `docker network create proxy`;
2. conecte o Caddy central a essa rede;
3. copie o bloco do `Caddyfile` deste repo para o Caddyfile central e
   `docker exec <caddy> caddy reload`.

**Cenário B (VPS sem Caddy):** `docker compose --profile com-caddy up -d`.

## DNS / subdomínio

Antes do primeiro deploy, crie no DNS um registro **A** (ou CNAME) para
`bangalo.dobroconsultoriafinanceira.com.br` apontando para o IP da VPS. Sem isso
a emissão automática do certificado TLS (Let's Encrypt) falha.

## Backup e restauração

```bash
./scripts/backup_db.sh                    # pg_dump datado -> ./backups/*.sql.gz
./scripts/restore_db.sh backups/bangalo_AAAA-MM-DD_HHMM.sql.gz
```
Configure cron diário (exemplo no topo do script). **Proteja `./backups`**
(dados sensíveis): permissão restrita e, idealmente, cifra em repouso.
Opcionalmente `SCHEDULER_ENABLED=true` liga o backup via APScheduler dentro do app.

## Perfis de acesso

- **consultoria** (admin/master): tudo, incluindo usuários, auditoria,
  importação e premissas de meta.
- **gerencia** (operação diária): opera lançamentos, quinzenas e registro
  diário; **não** gerencia usuários nem edita premissas de meta. Fechamento de
  quinzena é irreversível e auditado.

## Decisões de negócio confirmadas

1. **Realizado das metas** = soma do Registro Diário do mês (via
   `faturamento_historico` do ano p/ meses sem registro diário). Fonte única.
2. **Rateio de gorjeta com setor vazio:** pool é **redistribuído** aos demais
   setores proporcionalmente.
3. **Descontos de gorjeta:** motivos **Perda, Avaria, Vale, Descontos**.
4. **Comissão bruta:** sempre input manual (12% do faturamento/couvert do PDV).
5. **Histórico do fluxo:** só **2026**.
6. **Caddy:** adicionar ao Caddy central existente.
7. **Exportações (Excel/PDF):** etapa futura.

## Branches sugeridas

`feat/infra` · `feat/auth-ui` · `feat/cadastros` · `feat/fluxo-caixa` ·
`feat/gorjetas` · `feat/metas` · `feat/dashboard` · `feat/seguranca`.
