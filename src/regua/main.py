"""
Ponto de entrada do job.

  python -m regua.main                 -> executa a régua (uma vez) e termina
  python -m regua.main --limpar-teste  -> apaga os registros de MODO=teste do banco de controle

Código de saída: 0 = tudo certo | 1 = houve e-mails com erro | 2 = falha geral
(o Cloud Run Jobs marca a execução como falha quando o código é diferente de 0).
"""

from __future__ import annotations

import argparse
import logging
import sys
import time
from datetime import date, datetime
from zoneinfo import ZoneInfo

from . import config as config_mod
from . import consultas, exemplo, mensagens, regras
from .controle import Controle
from .email_provedores import criar_provedor
from .logs import configurar_logs, mascarar_cpf, mascarar_email

log = logging.getLogger("regua")


def hoje_brasilia() -> date:
    # O container roda em UTC; a régua conta dias no horário de Brasília.
    return datetime.now(ZoneInfo("America/Sao_Paulo")).date()


def executar(cfg: config_mod.Config, controle: Controle, provedor=None, hoje: date | None = None,
             dados: consultas.DadosOrigem | None = None) -> dict:
    hoje = hoje or hoje_brasilia()
    provedor = provedor or criar_provedor(cfg)
    marcos = {m.dias: m for m in cfg.marcos}
    totais = {"candidatos": 0, "enviados": 0, "erros": 0, "sem_email": 0, "ignorados": 0}

    log.info("Iniciando régua | modo=%s provedor=%s fonte=%s hoje=%s marcos=%s",
             cfg.modo, provedor.nome, cfg.fonte_dados, hoje.isoformat(),
             ",".join(f"{m.dias}:{m.tipo}" for m in cfg.marcos))
    if cfg.em_teste and cfg.email_destino_teste:
        log.info("MODO TESTE: todos os e-mails vão para %s (limite de %s envios)",
                 mascarar_email(cfg.email_destino_teste), cfg.max_envios or "sem limite")

    controle.aplicar_schema()
    execucao_id = controle.iniciar_execucao(cfg.modo_registro, provedor.nome, cfg.fonte_dados)

    try:
        if dados is None:
            dados = exemplo.gerar(hoje) if cfg.fonte_dados == "exemplo" else consultas.carregar_do_banco(cfg.movimentos)
        candidatos = regras.calcular_candidatos(
            dados, cfg.marcos, hoje, cfg.situacoes_plano_ignorar, cfg.dias_atraso_maximo, cfg.situacao_exigida
        )
        candidatos = regras.proxima_carta(
            candidatos, controle.ultima_carta_enviada(cfg.modo_registro), cfg.marcos, cfg.intervalo_entre_cartas
        )
        totais["candidatos"] = len(candidatos)

        # Mais atrasados primeiro: se houver limite, eles são os priorizados.
        candidatos = candidatos.sort_values("dias_atraso", ascending=False).reset_index(drop=True)
        com_email = candidatos["email"].map(regras.email_valido) if not candidatos.empty else candidatos

        for c in candidatos[~com_email].to_dict("records") if not candidatos.empty else []:
            controle.registrar_sem_email(execucao_id, cfg.modo_registro, c, provedor.nome)
            totais["sem_email"] += 1
        if totais["sem_email"]:
            log.warning("Beneficiários sem e-mail válido (registrados como SEM_EMAIL): %d", totais["sem_email"])

        fila = candidatos[com_email].to_dict("records") if not candidatos.empty else []
        if cfg.max_envios is not None and len(fila) > cfg.max_envios:
            log.info("Limite MAX_ENVIOS_POR_EXECUCAO=%d: %d ficam para a próxima execução",
                     cfg.max_envios, len(fila) - cfg.max_envios)
            fila = fila[: cfg.max_envios]

        sem_boleto = 0
        for c in fila:
            # Os textos aprovados citam o boleto e seu vencimento: em produção, sem boleto não envia
            # (e não consome tentativa; envia assim que o boleto estiver disponível).
            if not cfg.em_teste and not c.get("boleto"):
                sem_boleto += 1
                continue
            destino = cfg.email_destino_teste if cfg.em_teste and cfg.email_destino_teste else c["email"]
            notif_id = controle.registrar_pendente(
                execucao_id, cfg.modo_registro, c, provedor.nome, destino, cfg.max_tentativas
            )
            if notif_id is None:
                totais["ignorados"] += 1
                continue

            try:
                email = mensagens.montar(cfg, marcos[c["marco_dias"]], c, destino)
                message_id = provedor.enviar(email)
            except Exception as exc:  # noqa: BLE001 - qualquer falha de envio vira ERRO registrado
                controle.marcar_erro(notif_id, f"{type(exc).__name__}: {exc}")
                totais["erros"] += 1
                log.error("Falha no envio | cpf=%s marco=%s erro=%s", mascarar_cpf(c["cpf"]), c["marco_tipo"], exc)
            else:
                controle.marcar_enviado(notif_id, message_id)
                totais["enviados"] += 1
                log.info("Enviado | cpf=%s marco=%s dias=%s para=%s",
                         mascarar_cpf(c["cpf"]), c["marco_tipo"], c["dias_atraso"], mascarar_email(destino))
            time.sleep(cfg.pausa_entre_envios)

        if sem_boleto:
            log.warning("Não enviados por falta de boleto (ficam para a próxima execução): %d", sem_boleto)
            totais["ignorados"] += sem_boleto
        status = "OK" if totais["erros"] == 0 else "COM_ERROS"
        controle.finalizar_execucao(execucao_id, status, totais)
        log.info("Execução %d finalizada: %s | %s", execucao_id, status, totais)
        return {**totais, "status": status, "execucao_id": execucao_id}

    except Exception as exc:
        controle.finalizar_execucao(execucao_id, "FALHOU", totais, f"{type(exc).__name__}: {exc}"[:2000])
        raise


def main(argv: list[str] | None = None) -> int:
    configurar_logs()
    parser = argparse.ArgumentParser(description="Régua de cobrança por e-mail")
    parser.add_argument("--limpar-teste", action="store_true", help="apaga registros de MODO=teste e sai")
    args = parser.parse_args(argv)

    try:
        cfg = config_mod.carregar()
    except config_mod.ConfigError as exc:
        log.error("Configuração inválida: %s", exc)
        return 2

    controle = Controle(cfg.controle_db_url)
    if args.limpar_teste:
        controle.aplicar_schema()
        log.info("Registros de teste apagados: %d", controle.limpar_testes())
        return 0

    provedor = criar_provedor(cfg)
    try:
        resultado = executar(cfg, controle, provedor)
    except Exception:
        log.exception("Falha geral na execução")
        return 2
    finally:
        if hasattr(provedor, "fechar"):
            provedor.fechar()
    return 0 if resultado["erros"] == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
