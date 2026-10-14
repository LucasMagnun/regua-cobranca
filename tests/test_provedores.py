import json

import httpx
import pytest

from regua.email_provedores import BrevoProvedor, Email, ErroPermanente


def _email():
    return Email("dest@exemplo.com", "Fulano", "Assunto", "<p>oi</p>", "oi", ["aviso"])


def _brevo(respostas):
    chamadas = []

    def handler(request):
        chamadas.append(json.loads(request.content))
        status, corpo = respostas.pop(0)
        return httpx.Response(status, json=corpo)

    cliente = httpx.Client(transport=httpx.MockTransport(handler))
    return BrevoProvedor("chave", "rem@exemplo.com", "Financeiro", "resp@exemplo.com", client=cliente), chamadas


def test_brevo_envia_payload_correto_e_devolve_message_id():
    prov, chamadas = _brevo([(201, {"messageId": "<abc@brevo>"})])
    assert prov.enviar(_email()) == "<abc@brevo>"
    p = chamadas[0]
    assert p["sender"] == {"email": "rem@exemplo.com", "name": "Financeiro"}
    assert p["to"] == [{"email": "dest@exemplo.com", "name": "Fulano"}]
    assert p["replyTo"] == {"email": "resp@exemplo.com"}
    assert p["htmlContent"] == "<p>oi</p>" and p["textContent"] == "oi"


def test_brevo_tenta_de_novo_em_limite_de_taxa():
    prov, chamadas = _brevo([(429, {"message": "rate"}), (503, {}), (201, {"messageId": "ok"})])
    assert prov.enviar(_email()) == "ok"
    assert len(chamadas) == 3


def test_brevo_nao_insiste_em_erro_permanente():
    prov, chamadas = _brevo([(400, {"message": "sender not valid"})])
    with pytest.raises(ErroPermanente, match="400"):
        prov.enviar(_email())
    assert len(chamadas) == 1
