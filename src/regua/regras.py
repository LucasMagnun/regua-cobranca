"""
Regras da régua de cobrança.

Por BENEFICIÁRIO (não por boleto):
  - considera as cobranças com vencimento já passado;
  - dias de atraso = hoje - vencimento MAIS ANTIGO em aberto (vencimento_base);
  - o marco é o MAIOR marco atingido (ex.: 35 dias -> 30/suspensão), desde que a situação
    do plano no ERP confirme o que o e-mail afirma (30 dias -> SUSPENSO, 60 -> CANCELADO);
    se o ERP ainda não aplicou, vale o maior marco permitido e o seguinte sai depois;
  - recebe só esse marco, se ainda não recebeu ele (ou um maior) para o mesmo vencimento_base.

O vencimento_base identifica o "ciclo" de inadimplência: se a pessoa quita e volta a
atrasar depois, o vencimento mais antigo muda e a régua recomeça do primeiro aviso.

Regra por "atingiu" (>=), não por "igual": se o job falhar um dia, ninguém fica sem aviso.
"""

from __future__ import annotations

import logging
import re
from datetime import date

import pandas as pd

from .config import Marco
from .consultas import DadosOrigem, chave_titulo, converter_data

log = logging.getLogger("regua.regras")

_EMAIL_VALIDO = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def email_valido(email) -> bool:
    return isinstance(email, str) and bool(_EMAIL_VALIDO.match(email))


def calcular_candidatos(
    dados: DadosOrigem,
    marcos: tuple[Marco, ...],
    hoje: date,
    situacoes_ignorar: tuple[str, ...] = (),
    dias_atraso_maximo: int | None = None,
    situacao_exigida: dict | None = None,
) -> pd.DataFrame:
    colunas = [
        "cpf", "nome", "email", "situacao_plano", "vencimento_base", "dias_atraso",
        "qtd_cobrancas", "valor_total", "marco_dias", "marco_tipo", "itens",
    ]
    if dados.segurados.empty or dados.cobrancas.empty:
        return pd.DataFrame(columns=colunas)

    hoje_ts = pd.Timestamp(hoje)
    segurados = dados.segurados
    if situacoes_ignorar:
        ignorar = segurados["situacao_plano"].fillna("").isin(situacoes_ignorar)
        if ignorar.any():
            log.info("Ignorados pela situação do plano (%s): %d", ",".join(situacoes_ignorar), int(ignorar.sum()))
        segurados = segurados[~ignorar]

    base = dados.cobrancas.merge(segurados, on="cpf", how="inner")
    base["chave_titulo"] = chave_titulo(base["nosso_numero"])
    base = base.merge(dados.vencimentos, on="chave_titulo", how="left")
    base["data_vencimento"] = converter_data(base["data_vencimento"], "data_vencimento")
    base["valor_total"] = pd.to_numeric(base["valor_total"], errors="coerce").fillna(0.0)

    vencidas = base[base["data_vencimento"].notna() & (base["data_vencimento"].dt.normalize() < hoje_ts)].copy()
    if vencidas.empty:
        return pd.DataFrame(columns=colunas)
    vencidas["dias_item"] = (hoje_ts - vencidas["data_vencimento"].dt.normalize()).dt.days

    exigida = situacao_exigida or {}
    linhas, aguardando_erp = [], 0
    for cpf, grupo in vencidas.groupby("cpf", sort=False):
        grupo = grupo.sort_values("data_vencimento")
        vencimento_base = grupo["data_vencimento"].iloc[0].normalize()
        dias = int((hoje_ts - vencimento_base).days)
        atingidos = [m for m in marcos if dias >= m.dias]
        if not atingidos:
            continue
        if dias_atraso_maximo is not None and dias > dias_atraso_maximo:
            continue
        primeiro = grupo.iloc[0]
        # Marcos que afirmam suspensão/cancelamento só valem se o ERP já aplicou a situação.
        situacao = str(primeiro["situacao_plano"] or "").strip().upper()
        permitidos = [m for m in atingidos if m.tipo not in exigida or exigida[m.tipo] == situacao]
        if not permitidos:
            aguardando_erp += 1
            continue
        marco = permitidos[-1]
        if marco is not atingidos[-1]:
            aguardando_erp += 1
        linhas.append({
            "cpf": cpf,
            "nome": primeiro["nome"],
            "email": primeiro["email"],
            "situacao_plano": primeiro["situacao_plano"],
            "vencimento_base": vencimento_base.date(),
            "dias_atraso": dias,
            "qtd_cobrancas": len(grupo),
            "valor_total": round(float(grupo["valor_total"].sum()), 2),
            "marco_dias": marco.dias,
            "marco_tipo": marco.tipo,
            "itens": [
                {
                    "nosso_numero": str(r.nosso_numero),
                    "competencia": str(r.competencia_referencia or ""),
                    "vencimento": r.data_vencimento.date().isoformat(),
                    "dias_atraso": int(r.dias_item),
                    "valor": round(float(r.valor_total), 2),
                }
                for r in grupo.itertuples()
            ],
        })

    if aguardando_erp:
        log.info("Atingiram um marco que exige situação no ERP ainda não aplicada (%s): %d "
                 "(recebem o maior marco permitido; o seguinte sai quando o ERP atualizar)",
                 ", ".join(f"{k}={v}" for k, v in exigida.items()), aguardando_erp)
    df = pd.DataFrame(linhas, columns=colunas)
    if not df.empty:
        resumo = df.groupby("marco_tipo").size().to_dict()
        log.info("Beneficiários que atingiram algum marco: %d %s", len(df), resumo)
    return df


def filtrar_ja_notificados(candidatos: pd.DataFrame, maior_marco_enviado: dict) -> pd.DataFrame:
    """
    Remove quem já recebeu este marco (ou um maior) para o mesmo vencimento_base.
    maior_marco_enviado: {(cpf, vencimento_base): maior marco_dias com status ENVIADO}
    """
    if candidatos.empty:
        return candidatos
    ja = candidatos.apply(
        lambda r: maior_marco_enviado.get((r["cpf"], r["vencimento_base"]), -1) >= r["marco_dias"], axis=1
    )
    if ja.any():
        log.info("Já notificados anteriormente (ignorados): %d", int(ja.sum()))
    return candidatos[~ja].reset_index(drop=True)
