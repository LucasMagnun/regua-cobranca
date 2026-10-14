"""
Dados FICTÍCIOS para testar a régua sem acesso aos bancos (FONTE_DADOS=exemplo).
As datas são relativas a hoje, então cada beneficiário cai sempre no mesmo marco.
"""

from __future__ import annotations

from datetime import date, timedelta

import pandas as pd

from .consultas import DadosOrigem


def gerar(hoje: date) -> DadosOrigem:
    def venc(dias_atras: int) -> pd.Timestamp:
        return pd.Timestamp(hoje - timedelta(days=dias_atras))

    #                cpf            nome                         email                    situação   [(titulo, dias de atraso, valor)]
    pessoas = [
        ("00000000191", "Beneficiária Teste Aviso",       "teste.aviso@exemplo.com",       "ATIVO",     [("101", 18, 350.50)]),
        ("00000000272", "Beneficiário Teste Suspensão",   "teste.suspensao@exemplo.com",   "SUSPENSO",  [("201", 35, 410.00), ("202", 5, 410.00)]),
        ("00000000353", "Beneficiária Teste Cancelamento", "teste.cancelamento@exemplo.com", "CANCELADO", [("301", 72, 298.90), ("302", 42, 298.90)]),
        ("00000000434", "Beneficiário Teste Em Dia",      "teste.emdia@exemplo.com",       "ATIVO",     [("401", 6, 199.00)]),
        ("00000000515", "Beneficiária Teste Sem Email",   None,                            "ATIVO",     [("501", 20, 199.00)]),
        # 40 dias, mas o ERP ainda não suspendeu: recebe o aviso, não a suspensão
        ("00000000606", "Beneficiário Teste Aguardando ERP", "teste.aguardando@exemplo.com", "ATIVO",     [("601", 40, 199.00)]),
    ]

    segurados, cobrancas, vencimentos = [], [], []
    for cpf, nome, email, situacao, titulos in pessoas:
        segurados.append({"cpf": cpf, "nome": nome, "email": email, "situacao_plano": situacao})
        for titulo, dias, valor in titulos:
            v = venc(dias)
            cobrancas.append({
                "cpf": cpf,
                "nosso_numero": titulo.zfill(11),
                "valor_total": valor,
                "competencia_referencia": v.strftime("%m/%Y"),
            })
            vencimentos.append({"chave_titulo": titulo, "data_vencimento": v})

    return DadosOrigem(
        segurados=pd.DataFrame(segurados).astype({"email": "string", "nome": "string", "situacao_plano": "string"}),
        cobrancas=pd.DataFrame(cobrancas).astype({"nosso_numero": "string"}),
        vencimentos=pd.DataFrame(vencimentos).astype({"chave_titulo": "string"}),
    )
