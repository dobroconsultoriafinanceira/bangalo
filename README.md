# Bangalô — Dashboard Financeiro

Sistema web que unifica **fluxo de caixa**, **calculadora de gorjetas** e
**metas/planejamento de faturamento** do Restaurante Bangalô (Rio de Janeiro),
substituindo as planilhas operadas manualmente. Desenvolvido pela **Dobro
Consultoria Financeira**.

- **Stack:** Python 3.12 · Flask 3 · SQLAlchemy 2 · PostgreSQL 16 (SQLite em dev)
  · Jinja2 + Tailwind (CDN) + Chart.js · Gunicorn · Docker + Caddy.
- **Domínio alvo:** `bangalo.dobroconsultoria.com.br`
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
- **Fluxo:** só a aba `2026`. Estrutura real confirmada célula a célula:
  - Cada mês = colunas de **dia** + 1 coluna de **subtotal mensal**. A coluna de
    dia tem "Saldo Inicial" preenchido; a de subtotal, vazio. Importamos só os
    dias (senão o mês contaria em dobro).
  - O ano do cabeçalho está errado (2023/2024/2026 misturados) → usamos mês/dia
    e forçamos **2026**. 29/02 não existe em 2026 e é pulado (na planilha esse
    dia não tem movimento real).
  - "Total Saídas" da planilha **não inclui impostos** (DAS/ICMS) — eles entram
    só na Receita Líquida. A conferência trata os três blocos separados
    (Entradas / Impostos / Saídas operacionais).
  - Conferência contra a **soma diária** da planilha: Entradas e Impostos batem
    à vírgula. Nas saídas, o import é **mais completo** que o total da planilha
    (a fórmula de total dela omite algumas linhas de despesa, ex.: "Assessoria
    Financeira" em ago/26 — os valores estão nas células, mas fora do somatório).
  - Importa também o bloco **Saldo Aplicação** (entradas/rendimentos/resgates).
- **Gorjetas:** a aba `Histórico` vira snapshots imutáveis (`fechamento_gorjeta`).
- **Metas:** `Faturamento Histórico` (2022–2026) + `Registro Diário 2026` + premissas.

Integração com o PDV **F-Rest** fica como etapa futura — há um adapter isolado
(`importers/frest_adapter.py`) para importação de CSV/Excel exportado, sem
acoplar o sistema agora.

## Previsão de saídas (despesas previstas)

O extrato do Itaú **só mostra o que já saiu**: pagamento agendado no internet
banking não aparece na API (testado em 24/09/2026 — janela futura devolve zero
lançamentos e não há campo de agendamento). Por isso a projeção de saídas vem de
`services/despesas_previstas.py` + tela **Caixa › Previstas**:

- a equipe cadastra o que está combinado (fornecedor, valor, data prevista);
- vira um lançamento `origem='previsto'` na data prevista — é o que aparece no
  "Saldo previsto" do Caixa;
- quando o pagamento cai no extrato, `baixar_automatico()` casa os dois (mesmo
  valor, janela de -5/+10 dias e, se houver fornecedor, o mesmo fornecedor),
  apaga a previsão e marca a despesa como baixada, avisando na tela;
- a baixa roda a cada sincronização do Itaú (15 em 15 minutos).

## Integração Stone (Conciliação)

**A Stone é a fonte das entradas de cartão** (decisão da consultoria em
23/09/2026): Visa Crédito, Master Card Crédito, ELO Crédito, Amex Crédito,
ELO Débito, Visa Electron Débito e Maestro Débito **não vêm mais da planilha** —
o importador do fluxo ignora essas linhas (`fluxo_importer.LINHAS_DA_STONE`) e
elas nascem dos **repasses** da API de Conciliação.

**Contrato da API** (confirmado na doc oficial e contra a conta real em
23/09/2026 — ver `importers/stone_adapter.py`):

```
GET https://conciliation.stone.com.br/v2/merchant/{stoneCode}/conciliation-file/{AAAAMMDD}?layout=XML2_2
Authorization: Basic <chave>:      # a chave é o usuário, senha vazia
x-user-type: client
```

- A chave é gerada pelo titular no portal Stone (Perfil › Chaves de
  autenticação › **API de conciliação Stone**), por Stone Code. O lojista **não
  precisa** de `ClientApplicationKey` — isso é do fluxo de conciliadora parceira.
- O arquivo de um dia só fica pronto **depois das 5h do dia seguinte**.
- Resposta em XML (layout 2.2). O parser lê dois containers:
  - `FinancialTransactions` → vendas capturadas (bruto, líquido, MDR por parcela);
  - `Payments` → **repasses do dia** por `WalletTypeId`, que é o que cai no banco
    e o que vira lançamento de entrada (`WALLET_TYPE` mapeia para o plano de contas).

**No sistema** (Administração › Integrações › Stone):
- **Importar repasses** do período: cria/atualiza as entradas de cartão
  (idempotente por `origem='stone'`, `origem_id='pg:<id do pagamento>'`) e
  remove nesse intervalo as linhas de cartão que tenham vindo da planilha.
- **Só conferir**: compara Stone × fluxo dia a dia, sem gravar — foi assim que
  apareceram Visa/Master trocados na planilha em 17 e 18/09 e um Amex não lançado.

**Agenda de recebíveis**: cada venda traz a data prevista de pagamento da
parcela. `importar_agenda()` soma as parcelas ainda não liquidadas e grava a
previsão no caixa (origem `stone_agenda`, categoria "Recebíveis de cartão
(previsto)"), substituindo a previsão de cartão que vinha da planilha. Confere
com a tela "Recebimentos" do app da Stone.

**PIX da Stone**: o arquivo de conciliação NÃO traz PIX. O extrato mostra a
diferença: quando a Stone paga por bandeira ("STONE VISA CD ...") o crédito casa
com o arquivo; quando paga consolidado ("PIX TRANSF BANGALO") vem cartão + PIX
juntos, e a sobra é lançada como `Pagamento em PIX` (R$ 287 mil em 2026). O
arquivo oficial de PIX tem endpoint e chave próprios e ainda não está integrado.

**Automático**: com `STONE_SYNC_ENABLED=true`, o agendador busca todo dia às
`STONE_SYNC_HORA` (padrão 6h) os repasses dos últimos dias e reescreve a agenda.

**Configuração** (`.env`, fora do git):

```
STONE_BASE_URL=https://conciliation.stone.com.br
STONE_SECRET_KEY=sk_...        # chave da API de conciliação
STONE_CODES=181345124          # Stone Code da loja
STONE_LAYOUT=XML2_2
```

## Sincronização automática com o Google Sheets (fluxo)

O fluxo de caixa pode se manter atualizado a partir da planilha do Google
(preenchida diariamente), **sem digitação no sistema**. O servidor lê a
planilha com uma **conta de serviço** (só leitura) e reimporta o fluxo:

- **Automático:** a cada `GOOGLE_SYNC_INTERVAL_MIN` minutos (padrão 15), via
  APScheduler, quando `GOOGLE_SYNC_ENABLED=true`.
- **Manual:** botão **"Sincronizar agora"** em *Admin › Importação* (mostra a
  hora da última sincronização). Também via `flask google-sincronizar`.

Import idempotente e resiliente: substitui os lançamentos de 2026 e detecta o
layout pelos rótulos (imune a inserção de linhas na planilha).

**Passo a passo para habilitar (uma vez):**
1. No [Google Cloud Console](https://console.cloud.google.com), crie um projeto
   e **habilite a Google Drive API**.
2. Crie uma **Conta de Serviço** e gere uma **chave JSON**. Guarde o arquivo na
   VPS **fora do repositório** (ex.: `/srv/bangalo/secrets/gsa.json`).
3. Copie o **e-mail da conta de serviço** (algo como
   `bangalo-sync@projeto.iam.gserviceaccount.com`) e, na planilha do Google,
   **Compartilhar → Leitor** com esse e-mail.
4. No `.env`:
   ```
   GOOGLE_SYNC_ENABLED=true
   GOOGLE_SERVICE_ACCOUNT_JSON=/srv/bangalo/secrets/gsa.json
   GOOGLE_SHEETS_FLUXO_ID=1COW3BTVTLIosPCtobxNvDxLm6wt45AieTV6UzB33XV0
   GOOGLE_SYNC_INTERVAL_MIN=15
   ```
5. Reinicie o `web`. Confira em *Admin › Importação* (selo "conectada") e clique
   em "Sincronizar agora".

> **Múltiplos workers:** rode o agendador em **um** processo (ex.: um serviço
> `web` com `--workers 1` dedicado, ou um container só para o scheduler). O
> sync é idempotente e tem guarda de intervalo, mas evite N workers disparando
> em paralelo. A conta de serviço tem acesso **somente leitura** — o sistema
> nunca edita nem apaga a planilha.

## Integração Itaú PJ (API de Extrato Conta Corrente)

Consome a **API de Extrato Conta Corrente** do Itaú (`account-statement v1`),
contratada via Implantação Cash, e **concilia** com o fluxo.

- `importers/itau_adapter.py`: `ItauConfig` (env), `ItauClient` (certificado
  dinâmico, access token OAuth2 client_credentials + mTLS, extrato) e funções
  **puras**: `normalizar_conta`, `gerar_chave_e_csr`,
  `separar_resposta_certificado`, `normalizar_extrato`.
- `services/conciliacao_bancaria.py`: motor **puro** que casa transações do
  banco com lançamentos do fluxo por tipo + valor + data (janela de
  tolerância), separando *conciliado / só no banco / só no fluxo*.

| Etapa | Endpoint (produção) | Autenticação |
|---|---|---|
| Certificado | `POST https://sts.itau.com.br/seguranca/v1/certificado/solicitacao` (CSR em texto) | Bearer token temporário (5 min) |
| Token | `POST https://sts.itau.com.br/api/oauth/token` | mTLS (.crt + .key) |
| Extrato | `GET https://account-statement.api.itau.com/account-statement/v1/statements/{agência}00{conta}{DAC}?type=current_account&start_date=AAAA-MM-DD&page=1&page_size=1000` | Bearer + mTLS |

Contrato do extrato (confirmado com a conta real em 2026-09-15):
- `type=current_account` e `start_date` são **obrigatórios**. `end_date` não é
  estrito, então o sistema busca desde `start_date` e filtra o período pela
  **data contábil**.
- **Paginação não confiável**: `total_pages`/`total_elements` mudam entre
  chamadas idênticas e páginas de 100 se sobrepõem e **perdem lançamentos**.
  O cliente usa `page_size=1000` (o período vem numa página só e estável) e,
  se houver mais, pagina até página vazia/HTTP 422, deduplicando pelo `id`.
- Resposta: `data[].events[]` (lançamentos, mais recentes primeiro: `id`,
  `operation` C/D, `reversal`, `date.accounting`, `amount.value` com sinal,
  `literal.complete`, `origin`, `counterpart`) e `data[].balances[]`
  (`saldo_disponivel`, `saldo_bloqueado`, `saldo_aplic_aut`).
- Lotes **SISPAG** (ex.: salários) vêm como `type=agrupamento`, **sem `id`**,
  com `code` estável (`2026-09-12SALARIOS`) — usado como id (`agr:<code>`).

Homologação: `sts.rdhi.com.br` e `account-statement.api.hom.itau.com`
(`ITAU_AMBIENTE=homologacao`).

**Passo a passo:**
1. **Credenciais** (administrador do Devportal): em
   [devportal.itau.com.br/baas/#/credentials](https://devportal.itau.com.br/baas/#/credentials)
   gerar a credencial. Guardar o **client secret** (aparece uma única vez) e
   colocar `ITAU_CLIENT_ID` / `ITAU_CLIENT_SECRET` no `.env`.
2. **Certificado dinâmico**: gerar o **token temporário** no Devportal e, em
   até 5 minutos, rodar:
   ```bash
   flask itau-certificado --ou bangalo --cidade "Sao Paulo" --uf SP
   ```
   O comando gera chave + CSR (CN = client_id, RSA 2048, SHA-512), envia ao
   Itaú e grava `itau.key`, `itau.csr` e `itau.crt` em `instance/itau/` (fora
   do git). Se a resposta trouxer client secret, ele fica em
   `client_secret.txt`: passe para o `.env` e apague o arquivo. O certificado
   vale **1 ano**; para renovar, repita com `--forcar`.
3. **Enviar o ClientId ao Itaú** (Implantação Cash). Credenciais e escopos são
   liberados em até **2 dias úteis**, sem confirmação.
4. **Contas**: `ITAU_CONTAS=1234-12345-6` (agência-conta-DAC; do mesmo CNPJ
   da credencial).
5. **Primeira chamada**:
   ```bash
   flask itau-testar
   ```
   Obtém o token, chama a 1ª página do extrato (padrão: últimos 7 dias,
   `--desde AAAA-MM-DD` para mudar), mostra saldo e totais e grava o JSON
   bruto em `instance/itau/respostas/` (fora do git — contém dados reais).
   Para um período completo: `flask itau-sincronizar 2026-09-01 2026-09-15`.
6. **Produção (VPS)**: copiar `instance/itau/` para a VPS (montado no
   container pelo `docker-compose.yml`) e renovar antes de 1 ano.

**Classificação gerencial — visão CFO** (`services/classificacao_bancaria.py`):
cada movimento do extrato cai em um grupo, separando o resultado do
restaurante do que só movimenta dinheiro:

| Grupo | Exemplos |
|---|---|
| Operação do restaurante | repasse Stone (vendas no cartão), iFood/99, eventos, PIX de clientes; fornecedores, folha, FGTS, tributos, utilidades, fatura do cartão, músicos, serviços, tarifas |
| Sócios e financiamentos | retiradas/lucro, "sócios – a detalhar", empréstimos pagos ou recebidos |
| Tesouraria | aplicação/resgate automático, CDB, rendimentos |
| Transferências entre contas próprias | mesmo CNPJ (`EMPRESA_CNPJ_RAIZ`), exceto o repasse da Stone |

- Prioridade: classificação manual > regras do usuário
  (`regra_classificacao_bancaria`: documento, nome ou descrição) > regras
  padrão > "a classificar" (marcado para revisão).
- Na tela, "alterar" reclassifica um movimento; marcando "sempre para …" cria
  a regra e reaplica em todos os movimentos não manuais.
- A conciliação com o fluxo ignora tesouraria e transferências.
- O Itaú mostra o **recebido**, não o **vendido**: o repasse da Stone vem
  líquido de taxas e com prazo. Faturamento real vem da Stone/PDV.

**Gravação e conciliação** (`models/banco.py` + `services/itau_sync.py`):
- O extrato é gravado em `movimento_bancario` (idempotente por banco + conta +
  id do Itaú) e o saldo em `saldo_bancario` a cada sincronização. Ele **não**
  vira `Lancamento`: o fluxo vem da planilha, e gravar o extrato como
  lançamento contaria o mesmo dinheiro duas vezes.
- Após gravar, o motor casa cada movimento pendente com um lançamento livre do
  fluxo (mesmo tipo, mesmo valor, até 3 dias de diferença) e guarda o par em
  `movimento_bancario.lancamento_id`. Se o banco alterar data/valor de um
  movimento, ele volta a ficar pendente.
- Tela **Conciliação Itaú** (menu lateral): saldo, totais do extrato no
  período, movimentos conciliados/pendentes (com "desfazer") e lançamentos do
  fluxo sem par. Sincronizar é só para o perfil consultoria.
- Dashboard: card **Saldo Itaú** com o último saldo e linhas "Recebido/Pago no
  Itaú (operação)" nos cards de Entradas e Saídas — só a operação, sem sócios,
  aplicações nem transferências (os totais principais seguem do fluxo).
- CLI: `flask itau-sincronizar 2026-09-01` (fim opcional).
- Automático: `ITAU_SYNC_ENABLED=true` busca os últimos `ITAU_SYNC_DIAS` dias a
  cada `ITAU_SYNC_INTERVAL_MIN` minutos (rodar o agendador em um só processo).

Erros da API: [devportal › account statement](https://devportal.itau.com.br/nossas-apis/itau-vw9-api-account-statement-v1-externo).

> Alternativa mais rápida (se o acesso direto demorar): um **agregador de Open
> Finance** (Pluggy/Belvo/TecnoSpeed) entrega saldo/extrato/cartão/investimento
> por uma API única — bastaria um segundo adapter no mesmo padrão, reusando o
> motor de conciliação.

## Testes

```bash
python -m pytest
```
Cobrem as engines com os **números reais** das planilhas (gorjeta Maio/26 centavo
a centavo; pools por setor; meta JAN/2026 = R$ 457.432,27; metas diárias por peso
de dia da semana; setor vazio redistribui), consolidação de fluxo com saldos
derivados, e permissões por perfil.

## Deploy na VPS (Docker + Caddy)

A VPS (Hostinger, `root@<ip>`) já hospeda o stack do sistema interno da
consultoria em `/root/dobro`, cujo **Caddy é dono das portas 80/443** e serve
`dobroconsultoria.com.br` (site) e `dashboard.dobroconsultoria.com.br`
(sistema interno). O Bangalô entra ao lado, em `/root/bangalo`, **sem subir
Caddy próprio**: o container `bangalo_web` participa da rede do Caddy do Dobro
e recebe o tráfego por reverse proxy.

### Primeira instalação

```bash
# 1. DNS: registro A `bangalo` -> IP da VPS (antes de tudo; ver seção DNS)

# 2. chave de deploy da VPS -> cadastre a pública como Deploy key no GitHub
ssh-keygen -t ed25519 -C "vps-bangalo" -f ~/.ssh/bangalo_deploy -N ""
cat ~/.ssh/bangalo_deploy.pub
printf 'Host github.com\n  IdentityFile ~/.ssh/bangalo_deploy\n  IdentitiesOnly yes\n' >> ~/.ssh/config

# 3. clone + .env de produção
git clone git@github.com:dobroconsultoriafinanceira/bangalo.git /root/bangalo
cd /root/bangalo
cp .env.example .env && nano .env      # SECRET_KEY, POSTGRES_PASSWORD, SMTP, ADMIN_*
mkdir -p instance/itau instance/arquivos

# 4. sobe (o entrypoint roda `flask db upgrade` e `flask seed` sozinho)
make up && make logs

# 5. publica o domínio: copie o bloco do Caddyfile deste repo para
#    /root/dobro/Caddyfile e recarregue
make caddy-reload

# 6. deploy automático a cada git pull
make setup
```

### Dia a dia

`git pull` — o hook `post-merge` detecta o que mudou e refaz o build do `web`
com cache. Equivalente manual: `make deploy`. Outros alvos: `make logs`,
`make ps`, `make restart`, `make backup`, `make db-shell`, `make build-clean`.

### Estrutura do stack

`docker-compose.yml` tem `web` (Gunicorn na 8000, entrypoint aplica migrations
e seed no start), `db` (postgres:16-alpine com volume `bangalo_pgdata`) e
`caddy` (só no profile `com-caddy`, para uma VPS sem Caddy central).
Healthcheck da app em `/healthz`. `GUNICORN_WORKERS` padrão 2 (VPS de 1 vCPU).

> **Nome da rede externa:** o compose espera `dobro_frontend`, que é como o
> Compose nomeia a rede `frontend` do projeto em `/root/dobro`. Confirme com
> `docker network ls` antes do primeiro `up` e ajuste se divergir.

> **Agendador:** com mais de um worker, cada processo sobe seu próprio
> APScheduler. Os jobs são idempotentes e têm guarda de intervalo, mas se for
> ligar `SCHEDULER_ENABLED`/`*_SYNC_ENABLED` em produção, prefira 1 worker ou
> um serviço `worker` dedicado.

## DNS / subdomínio

Antes do primeiro deploy, crie no DNS um registro **A** para
`bangalo.dobroconsultoria.com.br` apontando para o IP da VPS. Sem isso a
emissão automática do certificado TLS (Let's Encrypt) falha e o Caddy fica
tentando em loop.

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
