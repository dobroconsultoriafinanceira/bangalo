# -*- coding: utf-8 -*-
"""Importa todos os models para registro no metadata (migrations/login)."""
from app.models.auditoria import LogAuditoria  # noqa: F401
from app.models.fluxo import (  # noqa: F401
    AplicacaoFinanceira,
    Categoria,
    ConfigSistema,
    Fornecedor,
    Lancamento,
)
from app.models.gorjetas import (  # noqa: F401
    Colaborador,
    ComissaoDiaria,
    FechamentoGorjeta,
    Funcao,
    ParticipacaoPeriodo,
    PeriodoGorjeta,
    Presenca,
    Setor,
)
from app.models.metas import (  # noqa: F401
    FaturamentoDiario,
    FaturamentoHistorico,
    PremissaMeta,
)
from app.models.usuario import Usuario  # noqa: F401
