"""Envio SMTP contra um servidor local que imita as respostas do Amazon SES."""

import email
from email import policy

import pytest

aiosmtpd = pytest.importorskip("aiosmtpd")
from aiosmtpd.controller import Controller  # noqa: E402

from regua.email_provedores import Email, ErroPermanente, SmtpProvedor  # noqa: E402


class ServidorFalsoSES:
    def __init__(self):
        self.recebidas = []
        self.rcpt_respostas = []   # fila de respostas forçadas para RCPT TO

    async def handle_RCPT(self, server, session, envelope, address, rcpt_options):
        if self.rcpt_respostas:
            return self.rcpt_respostas.pop(0)
        envelope.rcpt_tos.append(address)
        return "250 OK"

    async def handle_DATA(self, server, session, envelope):
        self.recebidas.append(email.message_from_bytes(envelope.content, policy=policy.default))
        return "250 Ok 0100019a-ses-message-id-000000"


@pytest.fixture
def servidor():
    import socket

    with socket.socket() as s:            # porta livre
        s.bind(("127.0.0.1", 0))
        porta = s.getsockname()[1]
    handler = ServidorFalsoSES()
    ctrl = Controller(handler, hostname="127.0.0.1", port=porta)
    ctrl.start()
    yield handler, porta
    ctrl.stop()


def _prov(porta):
    return SmtpProvedor("127.0.0.1", porta, None, None, "nenhuma", "cobranca@exemplo.com", "INAS", "resp@exemplo.com")


def _email(para="dest@exemplo.com"):
    return Email(para, "Fulano de Tal", "Regularização de Pendência", "<p>Olá <b>teste</b></p>", "Olá teste", ["aviso"])


def test_html_com_linhas_longas_e_aceito(servidor):
    """O SMTP limita linhas a 1000 caracteres; os templates reais geram linhas maiores."""
    handler, porta = servidor
    prov = _prov(porta)
    longo = "<p>" + ("Texto do e-mail com acentuação. " * 200) + "</p>"
    assert prov.enviar(Email("d@exemplo.com", "Fulano", "Assunto", longo, longo, ["aviso"]))
    prov.fechar()
    html = handler.recebidas[0].get_body(("html",)).get_content()
    assert html.rstrip("\r\n") == longo   # o MIME acrescenta a quebra de linha final


def test_envia_html_e_texto_e_devolve_id_do_ses(servidor):
    handler, porta = servidor
    prov = _prov(porta)
    assert prov.enviar(_email()) == "0100019a-ses-message-id-000000"
    assert prov.enviar(_email("outro@exemplo.com"))            # reaproveita a conexão
    prov.fechar()

    msg = handler.recebidas[0]
    assert msg["Subject"] == "Regularização de Pendência"       # acento preservado
    assert msg["From"] == "INAS <cobranca@exemplo.com>"
    assert msg["Reply-To"] == "resp@exemplo.com"
    tipos = [p.get_content_type() for p in msg.iter_parts()]
    assert tipos == ["text/plain", "text/html"]
    assert len(handler.recebidas) == 2


def test_destinatario_rejeitado_e_erro_permanente(servidor):
    handler, porta = servidor
    handler.rcpt_respostas = ["554 Message rejected: Email address is not verified."]
    prov = _prov(porta)
    with pytest.raises(ErroPermanente, match="554"):
        prov.enviar(_email())
    assert prov.enviar(_email()) == "0100019a-ses-message-id-000000"   # conexão continua utilizável
    prov.fechar()


def test_limite_temporario_tenta_de_novo(servidor):
    handler, porta = servidor
    handler.rcpt_respostas = ["454 Throttling failure: Maximum sending rate exceeded."]
    prov = _prov(porta)
    assert prov.enviar(_email()) == "0100019a-ses-message-id-000000"
    prov.fechar()


def test_senha_errada_e_erro_permanente_sem_insistir(monkeypatch):
    """O SES responde 535 para usuário/senha SMTP inválidos: não deve ficar tentando."""
    import smtplib

    chamadas = []

    def conectar_recusado(self):
        chamadas.append(1)
        raise smtplib.SMTPAuthenticationError(535, b"Authentication Credentials Invalid")

    monkeypatch.setattr(SmtpProvedor, "_conectar", conectar_recusado)
    prov = SmtpProvedor("email-smtp.sa-east-1.amazonaws.com", 587, "u", "errada", "starttls", "c@exemplo.com", "INAS")
    with pytest.raises(ErroPermanente, match="usuário/senha recusados"):
        prov.enviar(_email())
    assert len(chamadas) == 1


def test_servidor_sem_suporte_a_auth_e_erro_de_configuracao(servidor):
    _, porta = servidor
    prov = SmtpProvedor("127.0.0.1", porta, "usuario", "x", "nenhuma", "cobranca@exemplo.com", "INAS")
    with pytest.raises(ErroPermanente):
        prov.enviar(_email())


def test_servidor_fora_do_ar_e_erro_temporario(monkeypatch):
    from regua.email_provedores import ErroTemporario
    prov = _prov(1)   # porta 1: ninguém escutando
    with pytest.raises(ErroTemporario):
        prov.enviar(_email())
