"""Logging: JSON no Cloud Run (lido pelo Cloud Logging), texto legível na máquina local."""

from __future__ import annotations

import logging
import os
import sys


def configurar_logs() -> None:
    formato = os.getenv("LOG_FORMATO", "texto").lower()
    nivel = os.getenv("LOG_NIVEL", "INFO").upper()

    handler = logging.StreamHandler(sys.stdout)
    if formato == "json":
        try:
            from pythonjsonlogger.json import JsonFormatter  # python-json-logger >= 3
        except ImportError:  # pragma: no cover
            from pythonjsonlogger.jsonlogger import JsonFormatter  # type: ignore
        # 'severity' é o campo que o Cloud Logging usa para o nível do log
        handler.setFormatter(
            JsonFormatter(
                "%(asctime)s %(levelname)s %(name)s %(message)s",
                rename_fields={"levelname": "severity", "asctime": "timestamp"},
            )
        )
    else:
        handler.setFormatter(logging.Formatter("%(asctime)s | %(levelname)-7s | %(message)s"))

    raiz = logging.getLogger()
    raiz.handlers[:] = [handler]
    raiz.setLevel(nivel)
    for ruidoso in ("paramiko", "sshtunnel", "botocore", "boto3", "urllib3", "httpx", "httpcore"):
        logging.getLogger(ruidoso).setLevel(logging.WARNING)


def mascarar_cpf(cpf: str | None) -> str:
    """'00006273130' -> '000.***.***-30' (LGPD: logs não carregam CPF completo)."""
    if not cpf or len(cpf) != 11:
        return "***"
    return f"{cpf[:3]}.***.***-{cpf[-2:]}"


def mascarar_email(email: str | None) -> str:
    """'lucas.castro@empresa.com' -> 'lu***@empresa.com'."""
    if not email or "@" not in email:
        return "***"
    usuario, dominio = email.split("@", 1)
    return f"{usuario[:2]}***@{dominio}"
