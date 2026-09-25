# -*- coding: utf-8 -*-
"""Seeds de configuração: admin, setores/funções, plano de contas, premissas.

Idempotente: rodar de novo não duplica (upsert por chave natural).
"""
from decimal import Decimal

from flask import current_app

from app.extensions import db
from app.models.banco import RegraClassificacaoBancaria
from app.models.fluxo import Categoria, Fornecedor
from app.models.gorjetas import Colaborador, Funcao, Setor
from app.models.metas import PremissaMeta
from app.models.usuario import Usuario

# ---- Setores (percentual de rateio deve somar 1) ----
SETORES = [
    ("Cozinha", Decimal("0.25")),
    ("Salão", Decimal("0.73")),
    ("Caixa", Decimal("0.02")),
]

# ---- Funções e pontos (pesos reais da planilha) ----
# Tabela da calculadora de 16/09/2026: o auxiliar virou dois níveis de
# senioridade (1 = mais experiente) e o ASG subiu de 0,5 para 1 ponto.
FUNCOES = [
    ("Cozinheiro", "Cozinha", Decimal("2")),
    ("Auxiliar de Cozinha 1", "Cozinha", Decimal("1.5")),
    ("Auxiliar de Cozinha 2", "Cozinha", Decimal("1")),
    ("ASG", "Cozinha", Decimal("1")),
    ("Cozinheiro em experiência", "Cozinha", Decimal("1")),
    ("Garçom", "Salão", Decimal("2")),
    ("Garçom em experiência", "Salão", Decimal("1")),
    ("Gerente", "Salão", Decimal("2")),
    ("Barman", "Salão", Decimal("2")),
    ("Caixa", "Caixa", Decimal("1")),
]

# ---- Plano de contas: (nome, tipo, categoria) ----
# "categoria" é o 1º seletor da tela e "nome" o 2º (subcategoria). Categorias sem
# subcategoria repetem o próprio nome; em Compras a subcategoria é o fornecedor.
CATEGORIAS_ENTRADA = [
    *[(n, "entrada", "Vendas - Repasse Stone") for n in [
        "Visa Crédito", "Master Card Crédito", "ELO Crédito", "Amex Crédito",
        "ELO Débito", "Visa Eletron Débito", "Maestro Débito", "Pix Stone",
    ]],
    ("Pix Itau", "entrada", "Pix Itau"),
    ("Outros/Acertos", "entrada", "Outros/Acertos"),
    ("Patrocínio", "entrada", "Patrocínio"),
    ("Empréstimo", "entrada", "Empréstimo"),
    ("Resgate", "entrada", "Resgate"),
    ("IFOOD", "entrada", "IFOOD"),
    ("99 FOOD", "entrada", "99 FOOD"),
    ("Dinheiro", "entrada", "Dinheiro"),
    ("RENDIMENTO", "entrada", "RENDIMENTO"),
]

CATEGORIAS_SAIDA = [
    *[(n, "saida", "Impostos") for n in ["DAS", "ICMS"]],
    *[(n, "saida", "Folha/Salários") for n in [
        "Salários", "Funcionário por fora", "Administrativo/Financeiro",
        "DARFS (Funcionários + sócia)", "13º Salário", "FGTS", "Férias",
        "Rescisão", "Dobras", "Comissão", "Vale Transporte", "Vales",
        "Plano de saude Assim", "Medicina Trabalho", "Sigaban", "Sindirefeições",
        "Life Card/Shalon", "Funcionário Extra", "Ações/Acordos Trabalhistas",
    ]],
    *[(n, "saida", "Demais Salários") for n in [
        "Pro Labore", "Distribuição de Lucros", "Músicos", "Técnico Som", "Segurança",
    ]],
    ("Compras", "saida", "Compras"),
    *[(n, "saida", "Despesas Fixas") for n in [
        "Abrasel", "Aluguel", "Assessoria Financeira", "Aster (Dedetização)",
        "Atto Service", "Cartão de Crédito 15 Itaú", "Cartão de Crédito 26 Itaú",
        "Cartão de Crédito 5 Itaú", "CEG", "Clauwan/C.Villela", "Condomínio",
        "Contabilidade", "Control ID", "Darf iFood", "Ecad", "Emantec/iNova",
        "Enincêndio", "Falae/Experiência B2S", "Fiel Limpeza Caixa d'Água",
        "Força Ambiental", "Gedisa (luz)", "GRDJ/FREST", "IGUA", "IPTU", "Jurídico",
        "Light", "Manutenção/Obras/Equip.", "Marketing/Fotografias/Gráfica", "Net",
        "NixConsultoria", "Nutricionista", "Outros DARJ/DARM/DIFAL", "Seguro",
        "Taxa Incêndio", "Taxa Inspeção Sanitária", "Tuap", "Versatily toldo",
        "Virtual Market",
    ]],
    *[(n, "saida", "Outras despesas") for n in [
        "Empréstimos Heitor", "Dívidas Receita (Simples/PERT)", "Aplicação",
        "Despesas Bancárias", "Reembolso Barbara", "Multas", "Outros Acertos",
    ]],
]

# ~90 fornecedores da aba 2026 (linhas ~51–144)
FORNECEDORES = [
    "Aquacenter/Multiaguas", "Arena 1/Sofisa", "Arnaldo Brownie", "Ambev/CRBS",
    "Barra Carnes", "Brasil Seafood/BRSF", "Barroso", "Beirao da Serra", "Bernobre",
    "Bmg Foods", "Bom Porto/Brascod", "Brasil Típico", "Café Três Corações",
    "Casa Flora", "Casa Nunes", "Central/Multiplas", "CkBr Bebidas (Heineken)",
    "Casa Pedro", "Cipar", "Cia do Uniformes", "CRC Distribuidora /Riggar",
    "Constrular", "CCM Distribuidora bebida", "Dois amores",
    "Dmic Descartaveis/Controlnick", "DPL/Limpeza descartaveis", "Echopp",
    "Experiencia B2S/Falae", "Food Service/Temperos",
    "Fazenda das Antas/Irmaos Faustino", "Friganso", "Frigocenter",
    "GRF/Supervinhos", "Get Distribuidora", "GC Doces", "Grafica/Papelaria",
    "J.Araujo/ Arcofoods", "JAE Descart", "KR Soluções/ bobinas",
    "King Descartaveis", "Klonex/Sanda/WS/Globalmix",
    "LM Distribuidor/Nos Senhora/Bernobre",
    "Louçamar/Mario Louças/Chame/Vitoria Louças", "Laminado Comercio",
    "Lr Uniforme", "Lider", "Sertanorte", "Meat Hunter", "Massas Carneiro",
    "Maranata", "Marechal/Credit Facil", "MJM VinhosRaiz/Pagcerto/Cave", "Mundial",
    "Nogmix", "Nutrymax", "Nobredo", "Nova Geração", "Peixaria Oms Comercio",
    "Peixaria Farol da Ilha/ Reginaldo Soares",
    "Peixaria Word Fish /AA n da Silva/Marisco/2 Amores/Fish shed",
    "Peixaria Boelhe", "Peixaria Leme", "Peixaria Copa Fish",
    "Pescarimarcar/Mastermares", "Peixaria Flor do Mar", "Peixaria RK/GMH",
    "Peixaria Faifs", "Peixaria Mairmeids/Davi Comercio", "Peixaria Oceanica/Supremo",
    "Peixaria Lufran", "Peixaria Vimar", "Peixaria Klebio",
    "Portefrio/Multiplike/Delimita", "Polipac", "Quimate",
    "Rei Distribuidora/Força Td", "Real Carnes", "Revolution alimentos", "Rio Frio",
    "Rio Quality", "Rio Meat", "Royal", "Sicoob/Cipar", "Star alimentos",
    "Tempero Carioca", "Triflexo Etiquetas", "Tc Grafica", "Top Alto",
    "Uniformes Brasil", "Umehara Alimentos", "Vellocity/Oxdez",
    "Vanessa Loureira - gas do chopp", "Principado de Asturias Louças",
    "OUTROS GASTOS",
]

# ---- Regras de classificação do extrato bancário (definidas com a cliente em 15/09/2026) ----
# (campo, valor, tipo, categoria gerencial, revisar, observação)
REGRAS_CLASSIFICACAO_BANCARIA = [
    ("contraparte_nome", "BARBARA MENDES GONCALVES", "debito", "soc_a_detalhar", True,
     "Sócia: os PIX misturam pró-labore, retirada e reembolso — detalhar cada um"),
    ("contraparte_nome", "HEITOR MENDES GONCALVES", "debito", "fin_emprestimo_pagamento", False,
     "Empréstimos Heitor"),
    ("contraparte_nome", "MANOELA MENDES GONCALVES", "debito", "pag_folha", False,
     "Administrativo/Financeiro"),
    ("contraparte_nome", "A BRAS CIENCIAS MECANICAS", "credito", "rec_eventos", False,
     "Evento/reserva"),
]


def seed_admin() -> str:
    email = current_app.config["ADMIN_EMAIL"]
    senha = current_app.config.get("ADMIN_PASSWORD")
    existente = db.session.execute(
        db.select(Usuario).filter_by(email=email)
    ).scalar_one_or_none()
    if existente:
        return f"Admin já existe: {email}"
    gerada = not senha
    if gerada:
        # nunca uma senha padrão conhecida: gera uma aleatória e mostra uma única vez
        import secrets

        senha = secrets.token_urlsafe(18)
    else:
        from app.utils.seguranca import problema_na_senha

        problema = problema_na_senha(senha, email=email)
        if problema:
            raise ValueError(f"ADMIN_PASSWORD recusada: {problema}")
    admin = Usuario(nome="Consultoria Dobro", email=email, role="consultoria", ativo=True)
    admin.definir_senha(senha)
    db.session.add(admin)
    if gerada:
        return f"Admin criado: {email} · senha provisória (anote agora, não aparece de novo): {senha}"
    return f"Admin criado: {email} (troque a senha no primeiro login em Perfil › Trocar minha senha)"


def seed_setores_funcoes() -> list[str]:
    rel = []
    setores = {}
    for nome, pct in SETORES:
        s = db.session.execute(db.select(Setor).filter_by(nome=nome)).scalar_one_or_none()
        if not s:
            s = Setor(nome=nome, percentual_rateio=pct, ativo=True)
            db.session.add(s)
            db.session.flush()
        setores[nome] = s
    for nome, setor_nome, pontos in FUNCOES:
        setor = setores[setor_nome]
        f = db.session.execute(
            db.select(Funcao).filter_by(nome=nome, setor_id=setor.id)
        ).scalar_one_or_none()
        if not f:
            db.session.add(Funcao(nome=nome, setor_id=setor.id, pontos_padrao=pontos))
        elif f.pontos_padrao != pontos:
            f.pontos_padrao = pontos  # a tabela de pontos muda com o tempo
    rel.append(f"Setores: {len(SETORES)} · Funções: {len(FUNCOES)}")
    return rel


def seed_plano_contas() -> str:
    total = 0
    for ordem, (nome, tipo, grupo) in enumerate(CATEGORIAS_ENTRADA + CATEGORIAS_SAIDA):
        existe = db.session.execute(
            db.select(Categoria).filter_by(nome=nome, grupo=grupo)
        ).scalar_one_or_none()
        if not existe:
            db.session.add(Categoria(nome=nome, tipo=tipo, grupo=grupo, ativo=True,
                                     ordem=ordem))
            total += 1
    return f"Plano de contas: {total} categorias novas"


def seed_fornecedores() -> str:
    total = 0
    for nome in FORNECEDORES:
        existe = db.session.execute(
            db.select(Fornecedor).filter_by(nome=nome)
        ).scalar_one_or_none()
        if not existe:
            db.session.add(Fornecedor(nome=nome, ativo=True))
            total += 1
    return f"Fornecedores: {total} novos"


def seed_premissas() -> str:
    ano = 2026
    existe = db.session.execute(
        db.select(PremissaMeta).filter_by(ano=ano)
    ).scalar_one_or_none()
    if existe:
        return f"Premissa {ano} já existe"
    db.session.add(PremissaMeta(ano=ano))
    return f"Premissa {ano} criada (60/40, crescimento 8%)"


def seed_regras_classificacao() -> str:
    """Cria as regras iniciais; não altera regras já existentes (podem ter sido ajustadas na tela)."""
    total = 0
    for campo, valor, tipo, categoria, revisar, observacao in REGRAS_CLASSIFICACAO_BANCARIA:
        existe = db.session.execute(
            db.select(RegraClassificacaoBancaria).filter_by(campo=campo, valor=valor, tipo=tipo)
        ).scalar_one_or_none()
        if not existe:
            db.session.add(RegraClassificacaoBancaria(
                campo=campo, valor=valor, tipo=tipo, categoria=categoria,
                revisar=revisar, observacao=observacao,
            ))
            total += 1
    return f"Regras de classificação bancária: {total} novas"


def rodar_tudo() -> list[str]:
    rel = [seed_admin()]
    rel += seed_setores_funcoes()
    rel.append(seed_plano_contas())
    rel.append(seed_fornecedores())
    rel.append(seed_premissas())
    rel.append(seed_regras_classificacao())
    db.session.commit()
    return rel
