"""
Conexão com os bancos de ORIGEM (contrato, pessoa e financeiro), somente leitura.
Mesmas variáveis de ambiente do script de extração:
  SSH_* / CONTRATO_SSH_* / PESSOA_SSH_* / FINANCEIRO_SSH_*   -> túnel pelo jump
  DB_* / CONTRATO_DB_* / PESSOA_DB_* / FINANCEIRO_DB_*       -> host, porta, nome, usuário, senha, sslmode
"""

from __future__ import annotations

import logging
import os
import time
from contextlib import ExitStack

import pandas as pd
from sqlalchemy import bindparam, create_engine, text
from sqlalchemy.engine import URL

log = logging.getLogger("regua.db")

CHUNK = int(os.getenv("CHUNK", "500"))
PAUSA_ENTRE_LOTES = float(os.getenv("PAUSA_ENTRE_LOTES", "0.2"))
STATEMENT_TIMEOUT_MS = int(os.getenv("STATEMENT_TIMEOUT_MS", "60000"))


def _env(prefixo: str, chave: str, padrao: str | None = None) -> str | None:
    """Busca PREFIXO_DB_CHAVE e, se não existir, cai para DB_CHAVE."""
    return os.getenv(f"{prefixo}_DB_{chave}") or os.getenv(f"DB_{chave}", padrao)


def _ssh_env(prefixo: str, chave: str, padrao: str | None = None) -> str | None:
    """Busca PREFIXO_SSH_CHAVE e, se não existir, cai para SSH_CHAVE."""
    return os.getenv(f"{prefixo}_SSH_{chave}") or os.getenv(f"SSH_{chave}", padrao)


def abrir_tunel(stack: ExitStack, cache: dict, prefixo: str) -> tuple[str, int]:
    """
    Se SSH_HOST estiver configurado, abre um túnel até o banco e devolve o endereço
    local (127.0.0.1, porta). Senão, devolve DB_HOST/DB_PORT direto.
    """
    db_host = _env(prefixo, "HOST", "localhost")
    db_port = int(_env(prefixo, "PORT", "5432"))
    ssh_host = _ssh_env(prefixo, "HOST")
    if not ssh_host:
        return db_host, db_port

    ssh_port = int(_ssh_env(prefixo, "PORT", "22"))
    ssh_user = _ssh_env(prefixo, "USER")
    chave = (ssh_host, ssh_port, ssh_user, db_host, db_port)
    if chave in cache:
        return cache[chave]

    from sshtunnel import SSHTunnelForwarder

    auth: dict = {}
    key_path = _ssh_env(prefixo, "KEY_PATH")
    if key_path:
        auth["ssh_pkey"] = os.path.expanduser(key_path)
        passphrase = _ssh_env(prefixo, "KEY_PASSPHRASE")
        if passphrase:
            auth["ssh_private_key_password"] = passphrase
    else:
        auth["ssh_password"] = _ssh_env(prefixo, "PASSWORD")

    tunel = SSHTunnelForwarder(
        (ssh_host, ssh_port),
        ssh_username=ssh_user,
        remote_bind_address=(db_host, db_port),
        local_bind_address=("127.0.0.1", 0),
        set_keepalive=30,
        **auth,
    )
    stack.enter_context(tunel)
    log.info("Túnel SSH aberto: %s -> %s:%d", ssh_host, db_host, db_port)
    cache[chave] = ("127.0.0.1", tunel.local_bind_port)
    return cache[chave]


def criar_engine(database: str, prefixo: str, host: str, port: int):
    database = os.getenv(f"{prefixo}_DB_NAME") or database
    sslmode = _env(prefixo, "SSLMODE")
    url = URL.create(
        "postgresql+psycopg2",
        username=_env(prefixo, "USER"),
        password=_env(prefixo, "PASSWORD"),
        host=host,
        port=port,
        database=database,
        query={"sslmode": sslmode} if sslmode else {},
    )
    log.info("Conectando em %s (ssl=%s)", database, sslmode or "padrão")
    return create_engine(
        url,
        pool_size=1,
        max_overflow=0,
        pool_pre_ping=True,
        connect_args={
            "application_name": "regua_cobranca",
            "options": f"-c statement_timeout={STATEMENT_TIMEOUT_MS} -c default_transaction_read_only=on",
        },
    )


def tipo_coluna(engine, tabela: str, coluna: str) -> str:
    sql = text(
        "SELECT data_type FROM information_schema.columns "
        "WHERE table_name = :tabela AND column_name = :coluna LIMIT 1"
    )
    with engine.connect() as conn:
        tipo = conn.execute(sql, {"tabela": tabela, "coluna": coluna}).scalar()
    return (tipo or "text").lower()


def consultar_em_lotes(engine, sql: str, param: str, valores: list, extras: dict | None = None) -> pd.DataFrame:
    """Executa uma query com IN :param em lotes, na mesma conexão, com pausa entre lotes."""
    stmt = text(sql).bindparams(bindparam(param, expanding=True))
    partes = []
    total = (len(valores) + CHUNK - 1) // CHUNK
    with engine.connect() as conn:
        for n, i in enumerate(range(0, len(valores), CHUNK), start=1):
            params = {param: valores[i : i + CHUNK], **(extras or {})}
            partes.append(pd.read_sql(stmt, conn, params=params))
            if n % 20 == 0 or n == total:
                log.info("  lote %d/%d", n, total)
            if n < total:
                time.sleep(PAUSA_ENTRE_LOTES)
    return pd.concat(partes, ignore_index=True) if partes else pd.DataFrame()
