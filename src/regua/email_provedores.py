"""
Provedores de envio. Todos têm o mesmo formato: enviar(email) -> message_id.

  console -> não envia; só registra no log (para testar o fluxo)
  brevo   -> API HTTP do Brevo (usado nos testes até a AWS ser liberada)
  ses     -> Amazon SES v2 via API/boto3 (precisa de chave de acesso AWS)
  smtp    -> qualquer servidor SMTP; usado com a interface SMTP do Amazon SES
             (email-smtp.<regiao>.amazonaws.com + usuário/senha SMTP)

Trocar de provedor = trocar PROVEDOR_EMAIL no ambiente. O resto do código não muda.
"""

from __future__ import annotations

import logging
import smtplib
import ssl
import uuid
from email.message import EmailMessage
from email.utils import formataddr, formatdate, make_msgid
from dataclasses import dataclass, field

import httpx
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential

from .config import Config
from .logs import mascarar_email

log = logging.getLogger("regua.email")


@dataclass
class Email:
    para_email: str
    para_nome: str
    assunto: str
    html: str
    texto: str
    tags: list[str] = field(default_factory=list)


class ErroTemporario(Exception):
    """Falha passageira (limite de taxa, instabilidade): vale tentar de novo."""


class ErroPermanente(Exception):
    """Falha que não se resolve tentando de novo (e-mail inválido, remetente não verificado...)."""


_tentar_de_novo = retry(
    retry=retry_if_exception_type(ErroTemporario),
    stop=stop_after_attempt(4),
    wait=wait_exponential(multiplier=1, min=2, max=30),
    reraise=True,
)


class ConsoleProvedor:
    nome = "console"

    def enviar(self, email: Email) -> str:
        log.info("[console] e-mail NÃO enviado (simulação): para=%s assunto=%r",
                 mascarar_email(email.para_email), email.assunto)
        return f"console-{uuid.uuid4()}"


class BrevoProvedor:
    nome = "brevo"
    URL = "https://api.brevo.com/v3/smtp/email"

    def __init__(self, api_key: str, remetente_email: str, remetente_nome: str,
                 responder_para: str | None = None, client: httpx.Client | None = None):
        self.remetente = {"email": remetente_email, "name": remetente_nome}
        self.responder_para = responder_para
        self.client = client or httpx.Client(timeout=30)
        self.headers = {"api-key": api_key, "accept": "application/json", "content-type": "application/json"}

    @_tentar_de_novo
    def enviar(self, email: Email) -> str:
        payload = {
            "sender": self.remetente,
            "to": [{"email": email.para_email, "name": email.para_nome}],
            "subject": email.assunto,
            "htmlContent": email.html,
            "textContent": email.texto,
            "tags": email.tags,
        }
        if self.responder_para:
            payload["replyTo"] = {"email": self.responder_para}
        try:
            resp = self.client.post(self.URL, json=payload, headers=self.headers)
        except httpx.TransportError as exc:
            raise ErroTemporario(f"Brevo indisponível: {exc}") from exc

        if resp.status_code in (200, 201, 202):
            return resp.json().get("messageId", "")
        detalhe = resp.text[:300]
        if resp.status_code == 429 or resp.status_code >= 500:
            raise ErroTemporario(f"Brevo {resp.status_code}: {detalhe}")
        raise ErroPermanente(f"Brevo {resp.status_code}: {detalhe}")


class SesProvedor:
    nome = "ses"
    _TEMPORARIOS = {"Throttling", "ThrottlingException", "TooManyRequestsException",
                    "ServiceUnavailable", "InternalFailure", "LimitExceededException"}

    def __init__(self, region: str, remetente_email: str, remetente_nome: str,
                 responder_para: str | None = None, configuration_set: str | None = None, client=None):
        import boto3  # importado só aqui: quem usa Brevo não precisa de credencial AWS

        # Credenciais: AWS_ACCESS_KEY_ID / AWS_SECRET_ACCESS_KEY (ou outra forma padrão do boto3)
        self.client = client or boto3.client("sesv2", region_name=region)
        self.remetente = f"{remetente_nome} <{remetente_email}>"
        self.responder_para = responder_para
        self.configuration_set = configuration_set

    @_tentar_de_novo
    def enviar(self, email: Email) -> str:
        from botocore.exceptions import ClientError, EndpointConnectionError

        params = {
            "FromEmailAddress": self.remetente,
            "Destination": {"ToAddresses": [email.para_email]},
            "Content": {
                "Simple": {
                    "Subject": {"Data": email.assunto, "Charset": "UTF-8"},
                    "Body": {
                        "Html": {"Data": email.html, "Charset": "UTF-8"},
                        "Text": {"Data": email.texto, "Charset": "UTF-8"},
                    },
                }
            },
            "EmailTags": [{"Name": "marco", "Value": t} for t in email.tags[:1]],
        }
        if self.responder_para:
            params["ReplyToAddresses"] = [self.responder_para]
        if self.configuration_set:
            params["ConfigurationSetName"] = self.configuration_set
        try:
            return self.client.send_email(**params)["MessageId"]
        except EndpointConnectionError as exc:
            raise ErroTemporario(f"SES indisponível: {exc}") from exc
        except ClientError as exc:
            codigo = exc.response.get("Error", {}).get("Code", "")
            msg = f"SES {codigo}: {exc.response.get('Error', {}).get('Message', '')}"
            if codigo in self._TEMPORARIOS:
                raise ErroTemporario(msg) from exc
            raise ErroPermanente(msg) from exc


class SmtpProvedor:
    """
    Envio por SMTP, mantendo UMA conexão aberta durante a execução (reconecta se cair).
    Com o Amazon SES, o servidor responde '250 Ok <message-id>': esse id é guardado no controle.
    """

    nome = "smtp"

    def __init__(self, host: str, porta: int, usuario: str | None, senha: str | None, seguranca: str,
                 remetente_email: str, remetente_nome: str, responder_para: str | None = None, timeout: int = 30):
        self.host, self.porta, self.usuario, self.senha = host, porta, usuario, senha
        self.seguranca = seguranca
        self.remetente_email = remetente_email
        self.remetente = formataddr((remetente_nome, remetente_email))
        self.responder_para = responder_para
        self.timeout = timeout
        self._conexao: smtplib.SMTP | None = None

    def _conectar(self) -> smtplib.SMTP:
        contexto = ssl.create_default_context()
        if self.seguranca == "ssl":
            conexao = smtplib.SMTP_SSL(self.host, self.porta, timeout=self.timeout, context=contexto)
        else:
            conexao = smtplib.SMTP(self.host, self.porta, timeout=self.timeout)
        try:
            if self.seguranca != "ssl":
                conexao.ehlo()
                if self.seguranca == "starttls":
                    conexao.starttls(context=contexto)
                    conexao.ehlo()
            if self.usuario:
                conexao.login(self.usuario, self.senha or "")
        except Exception:
            conexao.close()   # não deixa a conexão aberta se TLS ou login falharem
            raise
        log.info("Conectado ao servidor SMTP %s:%s (%s)", self.host, self.porta, self.seguranca)
        return conexao

    def _montar(self, email: Email) -> EmailMessage:
        msg = EmailMessage()
        msg["From"] = self.remetente
        msg["To"] = formataddr((email.para_nome, email.para_email))
        msg["Subject"] = email.assunto
        msg["Date"] = formatdate(localtime=True)
        msg["Message-ID"] = make_msgid(domain=self.remetente_email.split("@")[-1])
        if self.responder_para:
            msg["Reply-To"] = self.responder_para
        if email.tags:
            msg["X-SES-MESSAGE-TAGS"] = ", ".join(f"marco={t}" for t in email.tags[:1])
        # quoted-printable: o SMTP limita cada linha a 1000 caracteres, e o HTML montado passa disso.
        msg.set_content(email.texto, cte="quoted-printable")
        msg.add_alternative(email.html, subtype="html", cte="quoted-printable")
        return msg

    @_tentar_de_novo
    def enviar(self, email: Email) -> str:
        msg = self._montar(email)
        try:
            if self._conexao is None:
                self._conexao = self._conectar()
            c = self._conexao
            c.ehlo_or_helo_if_needed()
            codigo, resp = c.mail(self.remetente_email)
            if codigo != 250:
                raise smtplib.SMTPSenderRefused(codigo, resp, self.remetente_email)
            codigo, resp = c.rcpt(email.para_email)
            if codigo not in (250, 251):
                c.rset()
                raise smtplib.SMTPRecipientsRefused({email.para_email: (codigo, resp)})
            # O SMTP exige CRLF; o smtplib não converte quando recebe bytes.
            codigo, resp = c.data(msg.as_bytes(policy=msg.policy.clone(linesep="\r\n")))
            if codigo != 250:
                raise smtplib.SMTPDataError(codigo, resp)
        # Ordem importa: as exceções do smtplib herdam de OSError, então as específicas vêm antes.
        except (smtplib.SMTPServerDisconnected, smtplib.SMTPConnectError) as exc:
            self.fechar()
            raise ErroTemporario(f"SMTP indisponível: {exc}") from exc
        except smtplib.SMTPAuthenticationError as exc:
            self.fechar()
            raise ErroPermanente(f"SMTP: usuário/senha recusados ({exc.smtp_code})") from exc
        except smtplib.SMTPRecipientsRefused as exc:
            codigo, resp = next(iter(exc.recipients.values()))
            raise (ErroTemporario if 400 <= codigo < 500 else ErroPermanente)(f"SMTP {codigo}: {resp!r}") from exc
        except smtplib.SMTPResponseException as exc:
            # 4xx = temporário (ex.: 454 limite de envio do SES); 5xx = permanente (ex.: 554 não verificado)
            if 400 <= exc.smtp_code < 500:
                raise ErroTemporario(f"SMTP {exc.smtp_code}: {exc.smtp_error!r}") from exc
            raise ErroPermanente(f"SMTP {exc.smtp_code}: {exc.smtp_error!r}") from exc
        except smtplib.SMTPException as exc:   # demais erros do protocolo (ex.: servidor sem AUTH/STARTTLS): configuração
            self.fechar()
            raise ErroPermanente(f"SMTP: {exc}") from exc
        except (ConnectionError, TimeoutError, OSError) as exc:   # rede/TLS fora do smtplib
            self.fechar()
            raise ErroTemporario(f"SMTP indisponível: {exc}") from exc

        texto = resp.decode(errors="replace") if isinstance(resp, bytes) else str(resp)
        # SES: "Ok 010f01...-000000"; outros servidores: usa o Message-ID do cabeçalho
        return texto.split()[-1] if texto.lower().startswith("ok ") else msg["Message-ID"]

    def fechar(self) -> None:
        if self._conexao is not None:
            try:
                self._conexao.quit()
            except Exception:  # noqa: BLE001
                pass
            self._conexao = None


def criar_provedor(cfg: Config):
    if cfg.provedor_email == "smtp":
        return SmtpProvedor(cfg.smtp_host or "", cfg.smtp_porta, cfg.smtp_usuario, cfg.smtp_senha,
                            cfg.smtp_seguranca, cfg.remetente_email, cfg.remetente_nome, cfg.responder_para)
    if cfg.provedor_email == "brevo":
        return BrevoProvedor(cfg.brevo_api_key or "", cfg.remetente_email, cfg.remetente_nome, cfg.responder_para)
    if cfg.provedor_email == "ses":
        return SesProvedor(cfg.aws_region, cfg.remetente_email, cfg.remetente_nome,
                           cfg.responder_para, cfg.ses_configuration_set)
    return ConsoleProvedor()
