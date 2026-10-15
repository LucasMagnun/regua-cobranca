"""
Atalho para rodar a régua sem Docker:

    python rodar.py                 # executa uma vez
    python rodar.py --limpar-teste  # apaga os registros de MODO=teste
    python rodar.py --previa        # envia 1 carta de cada tipo para EMAIL_DESTINO_TESTE (dados fictícios)
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "src"))

from regua.main import main  # noqa: E402

sys.exit(main())
