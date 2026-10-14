import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from regua.email_provedores import BrevoProvedor, SesProvedor, SmtpProvedor  # noqa: E402


@pytest.fixture(autouse=True)
def sem_espera_nas_retentativas(monkeypatch):
    """As retentativas esperam segundos entre tentativas; nos testes, zero."""
    for metodo in (BrevoProvedor.enviar, SesProvedor.enviar, SmtpProvedor.enviar):
        monkeypatch.setattr(metodo.retry, "sleep", lambda _s: None)


@pytest.fixture(params=["sqlite", "postgresql"])
def controle_url(request, tmp_path):
    """Os testes de fluxo rodam no SQLite (sempre) e no PostgreSQL (se CONTROLE_DB_URL_TESTE estiver definido)."""
    if request.param == "sqlite":
        return f"sqlite:///{tmp_path / 'controle_teste.db'}"
    url = os.getenv("CONTROLE_DB_URL_TESTE")
    if not url:
        pytest.skip("defina CONTROLE_DB_URL_TESTE (Postgres descartável) para testar também no PostgreSQL")
    return url
