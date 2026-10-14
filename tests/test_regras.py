from datetime import date, timedelta

from regua import exemplo, regras
from regua.config import _marcos, _situacao_exigida

MARCOS = _marcos("15:aviso,30:suspensao,60:cancelamento")
EXIGIDA = _situacao_exigida("suspensao:SUSPENSO,cancelamento:CANCELADO")
HOJE = date(2026, 10, 8)


def _candidatos(dados=None, **kw):
    kw.setdefault("situacao_exigida", EXIGIDA)
    return regras.calcular_candidatos(dados or exemplo.gerar(HOJE), MARCOS, HOJE, **kw).set_index("nome")


def test_marco_e_o_maior_atingido_e_confirmado_pelo_erp():
    c = _candidatos()
    assert c.loc["Beneficiária Teste Aviso", "marco_tipo"] == "aviso"                # 18 dias, ATIVO
    assert c.loc["Beneficiário Teste Suspensão", "marco_tipo"] == "suspensao"        # 35 dias, SUSPENSO
    assert c.loc["Beneficiária Teste Cancelamento", "marco_tipo"] == "cancelamento"  # 72 dias, CANCELADO
    assert c.loc["Beneficiária Teste Cancelamento", "dias_atraso"] == 72
    assert c.loc["Beneficiária Teste Cancelamento", "qtd_cobrancas"] == 2
    assert c.loc["Beneficiária Teste Cancelamento", "valor_total"] == 597.80


def test_sem_situacao_no_erp_fica_no_maior_marco_permitido():
    # 40 dias mas ATIVO: o e-mail de suspensão diria "houve a SUSPENSÃO", então recebe o aviso
    assert _candidatos().loc["Beneficiário Teste Aguardando ERP", "marco_tipo"] == "aviso"
    # sem a regra, receberia a suspensão
    assert _candidatos(situacao_exigida={}).loc["Beneficiário Teste Aguardando ERP", "marco_tipo"] == "suspensao"


def test_suspenso_com_mais_de_60_dias_recebe_suspensao_ate_o_erp_cancelar():
    dados = exemplo.gerar(HOJE)
    dados.segurados.loc[dados.segurados["nome"] == "Beneficiária Teste Cancelamento", "situacao_plano"] = "SUSPENSO"
    assert _candidatos(dados).loc["Beneficiária Teste Cancelamento", "marco_tipo"] == "suspensao"


def test_quem_nao_atingiu_o_primeiro_marco_fica_de_fora():
    assert "Beneficiário Teste Em Dia" not in _candidatos().index   # 6 dias


def test_situacoes_ignoradas():
    assert "Beneficiária Teste Cancelamento" not in _candidatos(situacoes_ignorar=("CANCELADO",)).index


def test_dias_atraso_maximo():
    c = _candidatos(dias_atraso_maximo=60)
    assert "Beneficiária Teste Cancelamento" not in c.index
    assert "Beneficiária Teste Aviso" in c.index


def test_boleto_que_ainda_nao_venceu_nao_conta():
    dados = exemplo.gerar(HOJE)
    dados.vencimentos.loc[:, "data_vencimento"] = dados.vencimentos["data_vencimento"] + timedelta(days=400)
    assert regras.calcular_candidatos(dados, MARCOS, HOJE).empty


def test_filtrar_ja_notificados_por_marco_e_ciclo():
    c = regras.calcular_candidatos(exemplo.gerar(HOJE), MARCOS, HOJE, situacao_exigida=EXIGIDA)
    susp = c[c["marco_tipo"] == "suspensao"].iloc[0]
    aviso = c[c["marco_tipo"] == "aviso"].iloc[0]
    enviados = {
        (susp["cpf"], susp["vencimento_base"]): 30,                         # já recebeu a suspensão
        (aviso["cpf"], aviso["vencimento_base"] - timedelta(days=90)): 60,  # ciclo ANTERIOR: não bloqueia
    }
    restantes = set(regras.filtrar_ja_notificados(c, enviados)["cpf"])
    assert susp["cpf"] not in restantes
    assert aviso["cpf"] in restantes


def test_email_valido():
    assert regras.email_valido("a.b@empresa.com.br")
    assert not regras.email_valido(None)
    assert not regras.email_valido("sem-arroba")
    assert not regras.email_valido("")


def test_buscar_emails_pessoa(monkeypatch):
    """E-mail vem da tabela pessoa: CPF com/sem máscara, e-mail vazio ignorado, duplicado -> primeiro."""
    import pandas as pd
    from regua import consultas, db

    monkeypatch.setattr(db, "tipo_coluna", lambda *a: "character varying")
    retorno = pd.DataFrame({
        "cpf": ["000.000.001-91", "00000000191", "00000000272", "00000000353"],
        "email": [" Fulano@Exemplo.com ", "outro@exemplo.com", "", None],
    })
    chamadas = {}

    def falso(engine, sql, param, valores, extras=None):
        chamadas["sql"], chamadas["valores"] = sql, valores
        return retorno.copy()

    monkeypatch.setattr(db, "consultar_em_lotes", falso)
    df = consultas.buscar_emails(None, ["00000000191", "00000000272", "00000000353"])

    assert "FROM pessoa" in chamadas["sql"] and "cpf_cnpj IN :cpfs" in chamadas["sql"]
    assert "000.000.001-91" in chamadas["valores"]          # procura também o CPF com máscara
    assert df.to_dict("records") == [{"cpf": "00000000191", "email": "fulano@exemplo.com"}]
