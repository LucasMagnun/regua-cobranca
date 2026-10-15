#!/usr/bin/env bash
# Executa a régua uma vez. Feito para o cron da VM (sem Docker).
#   - roda de dentro da pasta do projeto (é onde fica o .env e o banco de controle SQLite)
#   - usa o Python do .venv
#   - flock: se a execução anterior ainda estiver rodando, esta não começa (evita envio duplicado)
#   - log de cada dia em logs/regua-AAAA-MM-DD.log; apaga logs com mais de 90 dias
#   - backup diário do banco de controle SQLite em backups/ (últimos 30 dias)
set -uo pipefail

PROJETO="$(cd "$(dirname "$0")/.." && pwd)"
cd "$PROJETO" || exit 2
mkdir -p logs

LOG="logs/regua-$(date +%F).log"
exec >>"$LOG" 2>&1

echo "===== $(date '+%F %T') início ====="
flock -n -E 75 /tmp/regua-cobranca.lock "$PROJETO/.venv/bin/python" rodar.py "$@"
CODIGO=$?
echo "===== $(date '+%F %T') fim (código $CODIGO: 0=ok, 1=algum e-mail com erro, 2=falha geral, 75=já havia uma execução rodando) ====="

# Backup do banco de controle (SQLite), mesmo se a execução falhou
"$PROJETO/.venv/bin/python" scripts/backup_controle.py

find logs -name 'regua-*.log' -mtime +90 -delete
exit "$CODIGO"
