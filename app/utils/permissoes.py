# -*- coding: utf-8 -*-
"""Papéis e permissões do sistema.

- Todo usuário logado VÊ as telas operacionais (início, movimentações, caixa,
  DRE, fechamento, gorjetas, metas, histórico, cadastros).
- O que muda dados, gera PDF ou administra exige uma permissão desta lista.
- `consultoria` (Administrador total) tem todas, sempre — não dá para tirar.
- Os demais papéis partem de um padrão e o admin pode ajustar por usuário,
  exceto as permissões marcadas como exclusivas do administrador total.
"""
from functools import wraps

from flask import abort, current_app
from flask_login import current_user

# chave: (rótulo, explicação curta)
PERMISSOES: dict[str, tuple[str, str]] = {
    "caixa.editar": ("Lançar no caixa", "Criar, editar e excluir lançamentos do fluxo de caixa."),
    "movimentos.classificar": ("Classificar movimentações", "Revisar e classificar os movimentos do extrato Itaú."),
    "movimentos.sincronizar": ("Sincronizar o Itaú", "Buscar o extrato do banco na hora."),
    "gorjetas.editar": ("Montar gorjetas", "Criar quinzenas, lançar presença, descontos e extras."),
    "gorjetas.fechar": ("Fechar quinzena", "Fechar a quinzena de gorjeta (valores ficam congelados)."),
    "gorjetas.excluir": ("Reabrir ou excluir quinzena",
                         "Reabrir uma quinzena fechada para corrigir e excluir quinzenas."),
    "cadastros.editar": ("Editar cadastros", "Equipe, funções, setores, fornecedores e categorias."),
    "fechamento.conferir": ("Conferir fechamento", "Informar faturamento contábil, anexar e conferir documentos."),
    "fechamento.aprovar": ("Aprovar fechamento", "Aprovar o fechamento contábil do mês."),
    "fechamento.importar_razao": ("Importar razão", "Substituir o razão da contabilidade do mês."),
    "metas.editar": ("Configurar metas", "Premissas das metas, registro diário e histórico de faturamento."),
    "relatorios.gerar": ("Gerar relatórios", "Central de relatórios e PDFs (inclusive os de gorjeta)."),
    "admin.acessar": ("Administração", "Usuários, permissões, integrações, regras bancárias, importação e auditoria."),
}

# nunca concedidas fora do papel de administrador total
SO_ADMIN = frozenset({"admin.acessar"})

PAPEIS: dict[str, dict] = {
    "consultoria": {
        "rotulo": "Administrador total",
        "descricao": "Vê e faz tudo, inclusive administração. Permissões não podem ser reduzidas.",
        "padrao": frozenset(PERMISSOES),
    },
    "gerencia": {
        "rotulo": "Gerência",
        "descricao": "Opera o dia a dia. Não gera relatórios, não administra e só visualiza metas e histórico.",
        "padrao": frozenset({
            "caixa.editar", "movimentos.classificar", "gorjetas.editar", "gorjetas.fechar",
            "cadastros.editar", "fechamento.conferir",
        }),
    },
    "leitura": {
        "rotulo": "Somente leitura",
        "descricao": "Só visualiza as telas operacionais. Não altera nada.",
        "padrao": frozenset(),
    },
}


def efetivas(papel: str, personalizadas: list[str] | None) -> frozenset[str]:
    """Permissões em vigor para um papel + ajuste por usuário (None = padrão do papel)."""
    if papel == "consultoria":
        return frozenset(PERMISSOES)
    if papel not in PAPEIS:
        return frozenset()
    base = PAPEIS[papel]["padrao"] if personalizadas is None else frozenset(personalizadas)
    return frozenset(p for p in base if p in PERMISSOES and p not in SO_ADMIN)


def requer(permissao: str):
    """Exige login + a permissão. Sem login → tela de login; sem permissão → 403 auditado."""
    if permissao not in PERMISSOES:
        raise ValueError(f"Permissão desconhecida: {permissao}")

    def decorator(fn):
        @wraps(fn)
        def wrapper(*args, **kwargs):
            if not current_user.is_authenticated:
                return current_app.login_manager.unauthorized()
            if not current_user.pode(permissao):
                _registrar_negado(permissao)
                abort(403)
            return fn(*args, **kwargs)

        wrapper.permissao = permissao
        return wrapper

    return decorator


def _registrar_negado(permissao: str) -> None:
    from flask import request

    from app.extensions import db
    from app.services import auditoria

    try:
        auditoria.registrar("acesso_negado", "permissao", None,
                            depois={"permissao": permissao, "rota": request.path, "metodo": request.method})
        db.session.commit()
    except Exception:  # noqa: BLE001 — a auditoria nunca pode trocar o 403 por 500
        db.session.rollback()
        current_app.logger.exception("Falha ao auditar acesso negado")
