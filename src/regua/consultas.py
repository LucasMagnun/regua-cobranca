"""
Consultas nos bancos de origem (reaproveitadas do script de extração de boletos).

  df_contrato_api.segurado                  -> titulares inadimplentes (nome, cpf, situação)
  banco pessoa: tabela pessoa               -> e-mail, buscado pelo cpf_cnpj
  df_financeiro_api.cobranca                -> cobranças (nosso_numero, valor_total, competencia_referencia)
  df_financeiro_api.arquivo_retorno_detalhe -> vencimento de cada título (data_vencimeto)
"""

from __future__ import annotations

import logging
import os
import time
from contextlib import ExitStack
from dataclasses import dataclass

import pandas as pd
from sqlalchemy import text

from . import db

log = logging.getLogger("regua.consultas")

FUSO = "America/Sao_Paulo"
TIPOS_NUMERICOS = {"smallint", "integer", "bigint", "numeric"}

COLUNA_SITUACAO_PLANO = os.getenv("COLUNA_SITUACAO_PLANO", "situacao")
# E-mail vem do banco PESSOA (tabela pessoa), cruzando pelo CPF
PESSOA_TABELA = os.getenv("PESSOA_TABELA", "pessoa")
PESSOA_COLUNA_CPF = os.getenv("PESSOA_COLUNA_CPF", "cpf_cnpj")
COLUNA_EMAIL = os.getenv("COLUNA_EMAIL", "email")
COLUNA_VENCIMENTO = os.getenv("COLUNA_VENCIMENTO", "data_vencimeto")
BLOCO_VARREDURA = int(os.getenv("BLOCO_VARREDURA", "50000"))


@dataclass
class DadosOrigem:
    segurados: pd.DataFrame     # cpf, nome, email, situacao_plano
    cobrancas: pd.DataFrame     # cpf, nosso_numero, valor_total, competencia_referencia
    vencimentos: pd.DataFrame   # chave_titulo, data_vencimento


# --------------------------------------------------------------------------- #
# Normalizações
# --------------------------------------------------------------------------- #
def normalizar_cpf(serie: pd.Series) -> pd.Series:
    return serie.astype(str).str.replace(r"\D", "", regex=True).str.zfill(11)


def formatar_cpf(cpf: str) -> str:
    return f"{cpf[:3]}.{cpf[3:6]}.{cpf[6:9]}-{cpf[9:]}" if len(cpf) == 11 else cpf


def chave_titulo(serie: pd.Series) -> pd.Series:
    """'00000174295' e '174295' viram '174295' (cruza nosso_numero com numero_titulo)."""
    return serie.astype("string").str.replace(r"\D", "", regex=True).str.lstrip("0")


def _sem_fuso(serie: pd.Series) -> pd.Series:
    if isinstance(serie.dtype, pd.DatetimeTZDtype):
        return serie.dt.tz_convert(FUSO).dt.tz_localize(None)
    return serie


def converter_data(serie: pd.Series, nome: str = "data") -> pd.Series:
    """
    Converte para data sem fuso. Aceita date/timestamp/timestamptz, 'AAAA-MM-DD[...]',
    'AAAA-MM', 'DD/MM/AAAA', 'MM/AAAA', 'DDMMAA', 'DDMMAAAA'.
    """
    if pd.api.types.is_datetime64_any_dtype(serie):
        return _sem_fuso(serie).astype("datetime64[ns]")

    resultado = pd.Series(pd.NaT, index=serie.index, dtype="datetime64[ns]")
    eh_data = serie.map(lambda v: hasattr(v, "year") and hasattr(v, "month"))
    com_fuso = eh_data & serie.map(lambda v: getattr(v, "tzinfo", None) is not None)
    sem_fuso = eh_data & ~com_fuso
    if com_fuso.any():
        conv = pd.to_datetime(serie[com_fuso], utc=True, errors="coerce")
        resultado[com_fuso] = conv.dt.tz_convert(FUSO).dt.tz_localize(None)
    if sem_fuso.any():
        resultado[sem_fuso] = pd.to_datetime(serie[sem_fuso].map(lambda v: pd.Timestamp(v)), errors="coerce")

    texto = serie.where(~eh_data).astype("string").str.strip()
    texto = texto.where(texto.ne(""))
    padroes = [
        (r"\d{6}", lambda t: pd.to_datetime(t, format="%d%m%y", errors="coerce")),
        (r"\d{8}", lambda t: pd.to_datetime(t, format="%d%m%Y", errors="coerce")),
        (r"\d{4}-\d{2}-\d{2}.*", lambda t: pd.to_datetime(t.str[:10], format="%Y-%m-%d", errors="coerce")),
        (r"\d{4}-\d{2}", lambda t: pd.to_datetime(t, format="%Y-%m", errors="coerce")),
        (r"\d{2}/\d{2}/\d{4}.*", lambda t: pd.to_datetime(t.str[:10], format="%d/%m/%Y", errors="coerce")),
        (r"\d{2}/\d{4}", lambda t: pd.to_datetime(t, format="%m/%Y", errors="coerce")),
    ]
    for regex, conversor in padroes:
        alvo = texto.str.fullmatch(regex).fillna(False) & resultado.isna()
        if alvo.any():
            resultado[alvo] = conversor(texto[alvo])

    falhas = texto.notna() & resultado.isna()
    if falhas.any():
        exemplos = texto[falhas].drop_duplicates().head(3).tolist()
        log.warning("%s: %d valores em formato não reconhecido (ex.: %s)", nome, int(falhas.sum()), exemplos)
    return resultado


# --------------------------------------------------------------------------- #
# Consultas
# --------------------------------------------------------------------------- #
def buscar_inadimplentes(engine) -> pd.DataFrame:
    sql = f"""
        SELECT nome,
               cpf_cnpj AS cpf,
               {COLUNA_SITUACAO_PLANO} AS situacao_plano
        FROM segurado
        WHERE tipo_segurado ILIKE 'TITULAR'
          AND situacao_financeira ILIKE 'INADIMPLENTE'
    """
    df = pd.read_sql(text(sql), engine)
    df["cpf"] = normalizar_cpf(df["cpf"])
    df["nome"] = df["nome"].astype("string").str.strip()
    df["situacao_plano"] = df["situacao_plano"].astype("string").str.strip().str.upper()
    df = df.drop_duplicates(subset="cpf")
    log.info("Titulares inadimplentes: %d", len(df))
    return df


def valores_cpf(engine, tabela: str, coluna: str, cpfs: list[str]) -> list:
    """Adapta a lista de CPFs ao tipo da coluna: número, ou texto com e sem máscara."""
    tipo = db.tipo_coluna(engine, tabela, coluna)
    return [int(c) for c in cpfs] if tipo in TIPOS_NUMERICOS else cpfs + [formatar_cpf(c) for c in cpfs]


def buscar_emails(engine, cpfs: list[str]) -> pd.DataFrame:
    """
    E-mail de cada CPF na tabela pessoa. Se o CPF tiver mais de uma linha, usa o
    primeiro e-mail preenchido (e avisa no log quantos CPFs tinham e-mails diferentes).
    """
    colunas = ["cpf", "email"]
    if not cpfs:
        return pd.DataFrame(columns=colunas)
    sql = f"""
        SELECT {PESSOA_COLUNA_CPF} AS cpf, {COLUNA_EMAIL} AS email
        FROM {PESSOA_TABELA}
        WHERE {PESSOA_COLUNA_CPF} IN :cpfs
    """
    valores = valores_cpf(engine, PESSOA_TABELA, PESSOA_COLUNA_CPF, cpfs)
    df = db.consultar_em_lotes(engine, sql, "cpfs", valores)
    if df.empty:
        log.info("E-mails encontrados em %s: 0 de %d CPFs", PESSOA_TABELA, len(cpfs))
        return pd.DataFrame(columns=colunas)

    df["cpf"] = normalizar_cpf(df["cpf"])
    df["email"] = df["email"].astype("string").str.strip().str.lower()
    df = df[df["email"].notna() & df["email"].ne("")]

    distintos = df.groupby("cpf")["email"].nunique()
    if (distintos > 1).any():
        log.warning("%d CPFs com mais de um e-mail em %s; usado o primeiro encontrado",
                    int((distintos > 1).sum()), PESSOA_TABELA)
    df = df.drop_duplicates(subset="cpf")[colunas]
    log.info("E-mails encontrados em %s: %d de %d CPFs", PESSOA_TABELA, len(df), len(cpfs))
    return df


def buscar_cobrancas(engine, cpfs: list[str], movimentos: tuple[str, ...]) -> pd.DataFrame:
    colunas = ["cpf", "nosso_numero", "valor_total", "competencia_referencia"]
    valores = valores_cpf(engine, "cobranca", "cpf", cpfs)

    sql = """
        SELECT cpf, nosso_numero, valor_total, competencia_referencia
        FROM cobranca
        WHERE cpf IN :cpfs
          AND movimento = ANY(:movimentos)
    """
    df = db.consultar_em_lotes(engine, sql, "cpfs", valores, {"movimentos": list(movimentos)})
    if df.empty:
        return pd.DataFrame(columns=colunas)
    df["cpf"] = normalizar_cpf(df["cpf"])
    df["nosso_numero"] = df["nosso_numero"].astype("string").str.strip()
    df["nosso_numero"] = df["nosso_numero"].where(df["nosso_numero"].ne(""))
    com_numero = df[df["nosso_numero"].notna()].drop_duplicates(subset=["cpf", "nosso_numero"])
    df = pd.concat([com_numero, df[df["nosso_numero"].isna()]], ignore_index=True)
    log.info("Cobranças (%s): %d", ",".join(movimentos), len(df))
    return df


def buscar_vencimentos(engine, numeros: list[str]) -> pd.DataFrame:
    """
    numero_titulo não tem índice: a tabela é lida UMA vez, em blocos, e o cruzamento
    é feito no Python. Título repetido em vários arquivos de retorno -> menor vencimento.
    """
    colunas = ["chave_titulo", "data_vencimento"]
    chaves = set(chave_titulo(pd.Series(numeros, dtype="string")).dropna()) - {""}
    if not chaves:
        return pd.DataFrame(columns=colunas)

    sql = text(f"SELECT numero_titulo, {COLUNA_VENCIMENTO} AS data_vencimento FROM arquivo_retorno_detalhe")
    partes, lidas = [], 0
    with engine.connect().execution_options(stream_results=True, max_row_buffer=BLOCO_VARREDURA) as conn:
        for bloco in pd.read_sql(sql, conn, chunksize=BLOCO_VARREDURA):
            lidas += len(bloco)
            bloco["chave_titulo"] = chave_titulo(bloco["numero_titulo"])
            bloco = bloco[bloco["chave_titulo"].isin(chaves)]
            if not bloco.empty:
                partes.append(bloco[colunas])
            time.sleep(db.PAUSA_ENTRE_LOTES)
    log.info("arquivo_retorno_detalhe: %d linhas lidas", lidas)
    if not partes:
        return pd.DataFrame(columns=colunas)

    df = pd.concat(partes, ignore_index=True)
    df["data_vencimento"] = converter_data(df["data_vencimento"], COLUNA_VENCIMENTO)
    df = df.dropna(subset=["data_vencimento"]).groupby("chave_titulo", as_index=False)["data_vencimento"].min()
    log.info("Títulos com vencimento encontrado: %d", len(df))
    return df


def carregar_do_banco(movimentos: tuple[str, ...]) -> DadosOrigem:
    with ExitStack() as stack:
        tuneis: dict = {}
        engines = []

        def conectar(nome_padrao: str, prefixo: str):
            eng = db.criar_engine(nome_padrao, prefixo, *db.abrir_tunel(stack, tuneis, prefixo))
            engines.append(eng)
            return eng

        try:
            eng_contrato = conectar("df_contrato_api", "CONTRATO")
            segurados = buscar_inadimplentes(eng_contrato)
            if segurados.empty:
                segurados["email"] = pd.Series(dtype="string")
                return DadosOrigem(segurados, pd.DataFrame(), pd.DataFrame())

            cpfs = segurados["cpf"].tolist()
            emails = buscar_emails(conectar("df_pessoa_api", "PESSOA"), cpfs)
            segurados = segurados.merge(emails, on="cpf", how="left")

            eng_financeiro = conectar("df_financeiro_api", "FINANCEIRO")
            cobrancas = buscar_cobrancas(eng_financeiro, cpfs, movimentos)
            numeros = cobrancas["nosso_numero"].dropna().unique().tolist() if not cobrancas.empty else []
            vencimentos = buscar_vencimentos(eng_financeiro, numeros)
            return DadosOrigem(segurados, cobrancas, vencimentos)
        finally:
            for eng in engines:
                eng.dispose()
