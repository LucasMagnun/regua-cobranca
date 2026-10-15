"""
Backup diário do banco de controle quando ele é SQLite.

Usa a API de backup do próprio SQLite (cópia consistente, mesmo com o arquivo em uso)
e guarda em BACKUP_DIR (padrão: <projeto>/backups), mantendo os últimos BACKUP_DIAS (padrão 30).
Se o banco de controle for PostgreSQL, não faz nada (o backup é responsabilidade do banco).
"""

from __future__ import annotations

import os
import sqlite3
import sys
import time
from datetime import date
from pathlib import Path

PROJETO = Path(__file__).resolve().parents[1]

try:
    from dotenv import load_dotenv
    load_dotenv(PROJETO / ".env")
except ImportError:
    pass


def main() -> int:
    url = os.getenv("CONTROLE_DB_URL", "")
    if not url.startswith("sqlite:///"):
        print("Banco de controle não é SQLite: backup ignorado")
        return 0
    origem = Path(url[len("sqlite:///"):])
    if not origem.is_absolute():
        origem = PROJETO / origem
    if not origem.exists():
        print(f"Banco de controle não encontrado: {origem}")
        return 1

    destino_dir = Path(os.getenv("BACKUP_DIR") or PROJETO / "backups")
    destino_dir.mkdir(parents=True, exist_ok=True)
    destino = destino_dir / f"{origem.stem}-{date.today():%Y-%m-%d}.db"

    with sqlite3.connect(origem) as src, sqlite3.connect(destino) as dst:
        src.backup(dst)
    os.chmod(destino, 0o600)

    dias = int(os.getenv("BACKUP_DIAS", "30"))
    limite = time.time() - dias * 86400
    for antigo in destino_dir.glob(f"{origem.stem}-*.db"):
        if antigo.stat().st_mtime < limite:
            antigo.unlink()

    print(f"Backup do banco de controle: {destino}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
