"""Testes de integração: rodam o fluxo inteiro contra um Postgres de controle descartável."""

from dataclasses import replace
from datetime import date, timedelta

import pytest
from sqlalchemy import text

from regua import config as config_mod
from regua import exemplo
from regua.controle import Controle
from regua.email_provedores import ErroPermanente
from regua.main import executar

HOJE = date(2026, 10, 8)
DADOS = exemplo.gerar(HOJE)  # dados fixos: avançar o 'hoje' aumenta os dias de atraso


class Captura:
    nome = "captura"

    def __init__(self, falhar_para=()):
        self.enviados = []
        self.falhar_para = set(falhar_para)

    def enviar(self, email):
        if email.para_nome in self.falhar_para:
            raise ErroPermanente("falha simulada")
        self.enviados.append(email)
        return f"id-{len(self.enviados)}"


@pytest.fixture
def ambiente(controle_url, monkeypatch):
    for k, v in {
        "MODO": "teste", "PROVEDOR_EMAIL": "console", "FONTE_DADOS": "exemplo",
        "EMAIL_DESTINO_TESTE": "caixa.teste@exemplo.com", "CONTROLE_DB_URL": controle_url,
        "MAX_ENVIOS_POR_EXECUCAO": "50", "PAUSA_ENTRE_ENVIOS": "0",
    }.items():
        monkeypatch.setenv(k, v)
    cfg = config_mod.carregar()
    controle = Controle(controle_url)
    from regua.controle import metadata
    metadata.drop_all(controle.engine)
    return cfg, controle


def _status(controle):
    with controle.engine.connect() as conn:
        return dict(conn.execute(text("SELECT nome || '|' || marco_tipo, status FROM notificacao")).all())


def test_fluxo_completo_sem_duplicar(ambiente):
    cfg, controle = ambiente
    prov = Captura()

    r1 = executar(cfg, controle, prov, HOJE, DADOS)
    # 1ª execução: todos começam pela carta de 15 dias (as 3 cartas saem em sequência); sem e-mail fica registrado
    assert (r1["enviados"], r1["sem_email"], r1["erros"]) == (4, 1, 0)
    assert all(e.para_email == "caixa.teste@exemplo.com" for e in prov.enviados)   # modo teste redireciona
    assert all(e.assunto.startswith("[TESTE]") for e in prov.enviados)
    assert any("seria enviado para: teste.aviso@exemplo.com" in e.html for e in prov.enviados)
    assert {e.tags[0] for e in prov.enviados} == {"aviso"}
    assert prov.enviados[0].assunto == "[TESTE] Regularização de Pendência"

    r2 = executar(cfg, controle, prov, HOJE, DADOS)          # mesmo dia: ninguém recebe de novo
    assert r2["enviados"] == 0 and len(prov.enviados) == 4

    # 15 dias depois: quem recebeu a de 15 recebe a de 30 (sem depender do ERP: o texto só avisa)
    r3 = executar(cfg, controle, prov, HOJE + timedelta(days=15), DADOS)
    novos = {(e.para_nome, e.tags[0]) for e in prov.enviados[4:]}
    assert novos == {
        ("Beneficiária Teste Aviso", "suspensao"),
        ("Beneficiário Teste Suspensão", "suspensao"),
        ("Beneficiária Teste Cancelamento", "suspensao"),
        ("Beneficiário Teste Aguardando ERP", "suspensao"),
        ("Beneficiário Teste Em Dia", "aviso"),                # 6 -> 21 dias: entra na régua
    }
    assert r3["erros"] == 0
    assunto = {e.tags[0]: e.assunto for e in prov.enviados}
    assert assunto["suspensao"] == "[TESTE] Regularização de Pendência e Aviso de Suspensão do Plano de Saúde"
    html_susp = next(e.html for e in prov.enviados if e.tags[0] == "suspensao")
    assert "será" in html_susp and "houve a" not in html_susp

    # 30 dias depois da carta de 30: a de 60 | "Em Dia" (51 dias) recebe a de 30
    executar(cfg, controle, prov, HOJE + timedelta(days=45), DADOS)
    tipos = {}
    for e in prov.enviados[9:]:
        tipos.setdefault(e.tags[0], set()).add(e.para_nome)
    assert tipos["cancelamento"] == {"Beneficiária Teste Aviso", "Beneficiário Teste Suspensão",
                                     "Beneficiária Teste Cancelamento", "Beneficiário Teste Aguardando ERP"}
    assert tipos["suspensao"] == {"Beneficiário Teste Em Dia"}

    # Depois das 3 cartas, não recebe mais nada no mesmo ciclo
    executar(cfg, controle, prov, HOJE + timedelta(days=90), DADOS)
    assert {(e.para_nome, e.tags[0]) for e in prov.enviados[14:]} == {("Beneficiário Teste Em Dia", "cancelamento")}

    st = _status(controle)
    assert st["Beneficiária Teste Aviso|aviso"] == "ENVIADO"
    assert st["Beneficiária Teste Aviso|suspensao"] == "ENVIADO"
    assert st["Beneficiária Teste Sem Email|aviso"] == "SEM_EMAIL"


def test_producao_nao_envia_sem_boleto(ambiente, monkeypatch):
    cfg, controle = ambiente
    cfg = replace(cfg, modo="producao", email_destino_teste=None)
    prov = Captura()
    r = executar(cfg, controle, prov, HOJE, DADOS)
    assert r["enviados"] == 0 and r["erros"] == 0 and r["ignorados"] == 4
    assert prov.enviados == []
    assert "|aviso" not in "".join(k for k, v in _status(controle).items() if v != "SEM_EMAIL")   # nada pendente/erro


def test_erro_e_tentado_de_novo_ate_o_limite(ambiente):
    cfg, controle = ambiente
    cfg = replace(cfg, max_tentativas=2)
    falha = Captura(falhar_para={"Beneficiária Teste Aviso"})

    assert executar(cfg, controle, falha, HOJE, DADOS)["erros"] == 1
    assert _status(controle)["Beneficiária Teste Aviso|aviso"] == "ERRO"
    assert executar(cfg, controle, falha, HOJE, DADOS)["erros"] == 1        # 2ª tentativa
    r = executar(cfg, controle, falha, HOJE, DADOS)                         # esgotou: não tenta mais
    assert r["erros"] == 0 and r["ignorados"] == 1

    ok = Captura()
    with controle.engine.begin() as conn:                            # alguém corrigiu e zerou as tentativas
        conn.exec_driver_sql("UPDATE notificacao SET tentativas = 0 WHERE status = 'ERRO'")
    assert executar(cfg, controle, ok, HOJE, DADOS)["enviados"] == 1
    assert _status(controle)["Beneficiária Teste Aviso|aviso"] == "ENVIADO"


def test_producao_exige_confirmacao(monkeypatch, controle_url):
    monkeypatch.setenv("MODO", "producao")
    monkeypatch.setenv("PROVEDOR_EMAIL", "brevo")
    monkeypatch.setenv("BREVO_API_KEY", "x")
    monkeypatch.setenv("REMETENTE_EMAIL", "a@b.com")
    monkeypatch.setenv("FONTE_DADOS", "banco")
    monkeypatch.setenv("CONTROLE_DB_URL", controle_url)
    monkeypatch.delenv("CONFIRMAR_PRODUCAO", raising=False)
    with pytest.raises(config_mod.ConfigError, match="CONFIRMAR_PRODUCAO"):
        config_mod.carregar()


def test_previa_envia_uma_carta_de_cada_tipo_sem_gravar_no_controle(ambiente):
    from regua.main import enviar_previa
    cfg, controle = ambiente
    controle.aplicar_schema()
    prov = Captura()
    assert enviar_previa(cfg, prov) == 0
    assert [e.tags[0] for e in prov.enviados] == ["aviso", "suspensao", "cancelamento"]
    assert all(e.para_email == "caixa.teste@exemplo.com" and e.assunto.startswith("[TESTE]") for e in prov.enviados)
    assert _status(controle) == {}


def test_previa_recusa_producao(ambiente):
    from regua.main import enviar_previa
    cfg, _ = ambiente
    prov = Captura()
    assert enviar_previa(replace(cfg, modo="producao"), prov) == 2
    assert prov.enviados == []
