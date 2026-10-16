"""
Configuração da régua, lida de variáveis de ambiente (ou do arquivo .env).

As variáveis de conexão com os bancos de origem (SSH_*, CONTRATO_DB_*, PESSOA_DB_*,
FINANCEIRO_DB_*, COLUNA_*) são as mesmas do script de extração e são lidas em db.py / consultas.py.
"""

from __future__ import annotations

import os
from dataclasses import dataclass

try:
    from dotenv import load_dotenv

    load_dotenv()
except ImportError:  # pragma: no cover
    pass


class ConfigError(Exception):
    """Configuração inválida: o job para antes de fazer qualquer coisa."""


@dataclass(frozen=True)
class Marco:
    dias: int          # a partir de quantos dias de atraso
    tipo: str          # aviso | suspensao | cancelamento
    assunto: str

    @property
    def template(self) -> str:
        return f"{self.tipo}.html"


# Assuntos conforme os documentos aprovados (notificacao_15/30/60_dias_versao_final).
ASSUNTOS = {
    "aviso": "Regularização de Pendência",
    # Documento: "Regularização de Pendência e Reativação do Plano de Saúde". Ajustado porque o texto
    # passou a avisar a suspensão (futuro) e "reativação" pressupõe que já houve. Confirmar com a PO.
    "suspensao": "Regularização de Pendência e Aviso de Suspensão do Plano de Saúde",
    # Conforme o documento notificacao_60_dias_versao_final_1.docx
    "cancelamento": "Aviso de Cancelamento e de Necessidade de Regularização de Débito do Plano GDF Saúde",
}

MODOS = {"teste", "producao"}
PROVEDORES = {"console", "brevo", "ses", "smtp"}
FONTES = {"banco", "exemplo"}


def _texto(nome: str, padrao: str | None = None) -> str | None:
    valor = os.getenv(nome)
    if valor is None or valor.strip() == "":
        return padrao
    return valor.strip()


def _inteiro(nome: str, padrao: int | None = None) -> int | None:
    valor = _texto(nome)
    if valor is None:
        return padrao
    try:
        return int(valor)
    except ValueError as exc:
        raise ConfigError(f"{nome} deve ser um número inteiro (recebido: {valor!r})") from exc


def _lista(nome: str, padrao: str) -> tuple[str, ...]:
    return tuple(p.strip().upper() for p in (_texto(nome, padrao) or "").split(",") if p.strip())


def _marcos(texto: str) -> tuple[Marco, ...]:
    """'15:aviso,30:suspensao,60:cancelamento' -> (Marco(15,...), ...)."""
    marcos = []
    for parte in texto.split(","):
        try:
            dias, tipo = parte.split(":")
            dias_i, tipo_s = int(dias), tipo.strip().lower()
        except ValueError as exc:
            raise ConfigError(f"MARCOS inválido: {parte!r} (use o formato 15:aviso,30:suspensao)") from exc
        if tipo_s not in ASSUNTOS:
            raise ConfigError(f"MARCOS: tipo {tipo_s!r} desconhecido (use {', '.join(ASSUNTOS)})")
        marcos.append(Marco(dias_i, tipo_s, ASSUNTOS[tipo_s]))
    marcos.sort(key=lambda m: m.dias)
    if len({m.dias for m in marcos}) != len(marcos):
        raise ConfigError("MARCOS: há dias repetidos")
    return tuple(marcos)


def _situacao_exigida(texto: str) -> dict[str, str]:
    """
    'suspensao:SUSPENSO,cancelamento:CANCELADO' -> {'suspensao': 'SUSPENSO', ...}
    Os e-mails de 30 e 60 dias AFIRMAM que o plano foi suspenso/cancelado, então só saem
    quando a situação do plano no ERP confirma isso.
    """
    exigida = {}
    for parte in filter(None, (p.strip() for p in texto.split(","))):
        try:
            tipo, situacao = parte.split(":")
        except ValueError as exc:
            raise ConfigError(f"SITUACAO_EXIGIDA inválida: {parte!r} (use suspensao:SUSPENSO)") from exc
        if tipo.strip().lower() not in ASSUNTOS:
            raise ConfigError(f"SITUACAO_EXIGIDA: tipo {tipo!r} desconhecido")
        exigida[tipo.strip().lower()] = situacao.strip().upper()
    return exigida


@dataclass(frozen=True)
class Config:
    modo: str
    email_destino_teste: str | None
    provedor_email: str
    remetente_email: str
    remetente_nome: str
    responder_para: str | None
    nome_empresa: str
    email_atendimento: str | None
    brevo_api_key: str | None
    smtp_host: str | None
    smtp_porta: int
    smtp_usuario: str | None
    smtp_senha: str | None
    smtp_seguranca: str          # starttls | ssl | nenhuma
    aws_region: str
    ses_configuration_set: str | None
    controle_db_url: str
    fonte_dados: str
    marcos: tuple[Marco, ...]
    movimentos: tuple[str, ...]
    situacoes_plano_ignorar: tuple[str, ...]
    situacao_exigida: dict
    max_envios: int | None
    dias_atraso_maximo: int | None
    pausa_entre_envios: float
    max_tentativas: int
    # Dias mínimos entre uma carta e a seguinte. None = a distância natural entre os marcos (15, depois 30).
    intervalo_entre_cartas: int | None = None
    # Portal onde o beneficiário emite o boleto atualizado (Cobrança > Dívidas Atualizadas)
    portal_url: str = "https://www.gdfsaude.df.gov.br/"

    @property
    def em_teste(self) -> bool:
        return self.modo == "teste"

    @property
    def modo_registro(self) -> str:
        """Valor gravado na coluna 'modo' do controle (separa testes de envios reais)."""
        return "TESTE" if self.em_teste else "PRODUCAO"


def carregar() -> Config:
    modo = (_texto("MODO", "teste") or "").lower()
    if modo not in MODOS:
        raise ConfigError(f"MODO deve ser 'teste' ou 'producao' (recebido: {modo!r})")

    provedor = (_texto("PROVEDOR_EMAIL", "console") or "").lower()
    if provedor not in PROVEDORES:
        raise ConfigError(f"PROVEDOR_EMAIL deve ser um de {sorted(PROVEDORES)} (recebido: {provedor!r})")

    fonte = (_texto("FONTE_DADOS", "banco") or "").lower()
    if fonte not in FONTES:
        raise ConfigError(f"FONTE_DADOS deve ser 'banco' ou 'exemplo' (recebido: {fonte!r})")

    email_teste = _texto("EMAIL_DESTINO_TESTE")
    if modo == "teste" and not email_teste and provedor != "console":
        raise ConfigError("Em MODO=teste, EMAIL_DESTINO_TESTE é obrigatório: todos os e-mails vão para ele.")

    if modo == "producao":
        if provedor == "console":
            raise ConfigError("MODO=producao exige um provedor real (brevo ou ses), não 'console'.")
        if fonte == "exemplo":
            raise ConfigError("MODO=producao não pode usar FONTE_DADOS=exemplo.")
        if (_texto("CONFIRMAR_PRODUCAO") or "").lower() != "sim":
            raise ConfigError("MODO=producao envia para beneficiários reais. Defina CONFIRMAR_PRODUCAO=sim para liberar.")

    if modo == "producao" and not _texto("EMAIL_ATENDIMENTO"):
        raise ConfigError("EMAIL_ATENDIMENTO é obrigatório em produção (aparece no texto dos e-mails).")

    remetente = _texto("REMETENTE_EMAIL")
    if provedor in {"brevo", "ses", "smtp"} and not remetente:
        raise ConfigError("REMETENTE_EMAIL é obrigatório para envio real (precisa estar verificado no provedor).")

    brevo_key = _texto("BREVO_API_KEY")
    smtp_porta = _inteiro("SMTP_PORTA", 587) or 587
    smtp_seguranca = (_texto("SMTP_SEGURANCA") or ("ssl" if smtp_porta == 465 else "starttls")).lower()
    if smtp_seguranca not in {"starttls", "ssl", "nenhuma"}:
        raise ConfigError("SMTP_SEGURANCA deve ser starttls, ssl ou nenhuma")
    if provedor == "smtp":
        faltando = [n for n in ("SMTP_HOST", "SMTP_USUARIO", "SMTP_SENHA") if not _texto(n)]
        if faltando and not (smtp_seguranca == "nenhuma" and _texto("SMTP_HOST")):
            raise ConfigError(f"PROVEDOR_EMAIL=smtp exige {', '.join(faltando)}")

    if provedor == "brevo" and not brevo_key:
        raise ConfigError("PROVEDOR_EMAIL=brevo exige BREVO_API_KEY.")

    controle_url = _texto("CONTROLE_DB_URL")
    if not controle_url:
        raise ConfigError("CONTROLE_DB_URL é obrigatório (banco onde ficam os registros de envio).")

    max_envios = _inteiro("MAX_ENVIOS_POR_EXECUCAO")
    if max_envios is None and modo == "teste":
        max_envios = 5  # em teste, nunca inunda a caixa de teste por acidente

    try:
        pausa = float(_texto("PAUSA_ENTRE_ENVIOS", "0.2") or "0.2")
    except ValueError as exc:
        raise ConfigError("PAUSA_ENTRE_ENVIOS deve ser um número (segundos)") from exc

    return Config(
        modo=modo,
        email_destino_teste=email_teste,
        provedor_email=provedor,
        remetente_email=remetente or "nao-responda@exemplo.local",
        remetente_nome=_texto("REMETENTE_NOME", "Financeiro") or "Financeiro",
        responder_para=_texto("RESPONDER_PARA"),
        nome_empresa=_texto("NOME_EMPRESA", "INAS") or "INAS",
        email_atendimento=_texto("EMAIL_ATENDIMENTO"),
        brevo_api_key=brevo_key,
        smtp_host=_texto("SMTP_HOST"),
        smtp_porta=smtp_porta,
        smtp_usuario=_texto("SMTP_USUARIO"),
        smtp_senha=_texto("SMTP_SENHA"),
        smtp_seguranca=smtp_seguranca,
        aws_region=_texto("AWS_REGION", "sa-east-1") or "sa-east-1",
        ses_configuration_set=_texto("SES_CONFIGURATION_SET"),
        controle_db_url=controle_url,
        fonte_dados=fonte,
        marcos=_marcos(_texto("MARCOS", "15:aviso,30:suspensao,60:cancelamento") or ""),
        movimentos=_lista("MOVIMENTOS", "EM_NEGOCIACAO"),
        situacoes_plano_ignorar=_lista("SITUACOES_PLANO_IGNORAR", ""),
        situacao_exigida=_situacao_exigida(_texto("SITUACAO_EXIGIDA", "") or ""),
        max_envios=max_envios,
        dias_atraso_maximo=_inteiro("DIAS_ATRASO_MAXIMO"),
        pausa_entre_envios=pausa,
        max_tentativas=_inteiro("MAX_TENTATIVAS", 3) or 3,
        intervalo_entre_cartas=_inteiro("INTERVALO_ENTRE_CARTAS_DIAS"),
        portal_url=_texto("PORTAL_URL", "https://www.gdfsaude.df.gov.br/") or "https://www.gdfsaude.df.gov.br/",
    )
