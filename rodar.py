"""
Atalho para rodar a régua sem Docker:

    python rodar.py                 # executa uma vez
    python rodar.py --limpar-teste  # apaga os registros de MODO=teste
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "src"))

from regua.main import main  # noqa: E402

sys.exit(main())
