# -*- coding: utf-8 -*-
"""Adapter (stub) para o PDV F-Rest.

O restaurante usa o F-Rest, sem API pública confirmada. A integração de
vendas fica como etapa FUTURA: aqui vai a interface para importar um CSV/
Excel exportado do F-Rest, isolando o sistema desse acoplamento.

TODO (futuro):
  - definir o layout de exportação do F-Rest (colunas de venda/couvert);
  - mapear vendas diárias -> FaturamentoDiario e/ou entradas do fluxo;
  - derivar a comissão bruta (12%) das quinzenas automaticamente.
"""
from pathlib import Path


class FRestAdapter:
    """Contrato mínimo para uma futura importação de vendas do F-Rest."""

    def importar_vendas(self, caminho: Path) -> list[dict]:
        raise NotImplementedError(
            "Integração F-Rest ainda não implementada — importação manual por enquanto."
        )
