"""
Banco de CONTROLE: registra execuções e cada notificação (enviada, com erro ou sem e-mail).

Funciona em dois bancos, escolhidos pelo CONTROLE_DB_URL:
  - SQLite (arquivo local, sem instalar nada):  sqlite:///controle_regua.db
  - PostgreSQL (produção):                      postgresql+psycopg2://usuario:senha@host:5432/banco
As tabelas são criadas automaticamente na primeira execução.

Fluxo de cada e-mail (garante que ninguém recebe o mesmo aviso duas vezes):
  1. registrar_pendente()  -> grava PENDENTE; se já estiver ENVIADO, devolve None e o envio é pulado
  2. envia pelo provedor
  3. marcar_enviado() com o message_id   |   marcar_erro() com a mensagem de erro
Na próxima execução, os ERRO são tentados de novo (até MAX_TENTATIVAS).
"""

from __future__ import annotations

import logging
from datetime import date

from sqlalchemy import (
    JSON, BigInteger, CheckConstraint, Column, Date, DateTime, ForeignKey, Index, Integer, MetaData,
    Numeric, SmallInteger, String, Table, Text, UniqueConstraint, create_engine, delete, func, select, update,
)
from sqlalchemy.dialects import postgresql, sqlite

log = logging.getLogger("regua.controle")

# BIGINT no PostgreSQL; INTEGER no SQLite (é o tipo que o SQLite autoincrementa)
_ID = BigInteger().with_variant(Integer, "sqlite")
_JSON = JSON().with_variant(postgresql.JSONB, "postgresql")
_AGORA = func.now()

metadata = MetaData()

execucao = Table(
    "execucao", metadata,
    Column("id", _ID, primary_key=True, autoincrement=True),
    Column("modo", String(10), nullable=False),                 # TESTE | PRODUCAO
    Column("provedor", String(20), nullable=False),             # console | smtp | brevo | ses
    Column("fonte_dados", String(20), nullable=False),          # banco | exemplo
    Column("iniciado_em", DateTime(timezone=True), nullable=False, server_default=_AGORA),
    Column("finalizado_em", DateTime(timezone=True)),
    Column("status", String(20), nullable=False, server_default="EM_ANDAMENTO"),
    Column("total_candidatos", Integer),
    Column("total_enviados", Integer),
    Column("total_erros", Integer),
    Column("total_sem_email", Integer),
    Column("total_ignorados", Integer),
    Column("detalhe", Text),
    CheckConstraint("status IN ('EM_ANDAMENTO', 'OK', 'COM_ERROS', 'FALHOU')", name="ck_execucao_status"),
)

# Uma linha por (beneficiário, ciclo de inadimplência, marco): histórico/comprovante e trava contra duplicidade.
notificacao = Table(
    "notificacao", metadata,
    Column("id", _ID, primary_key=True, autoincrement=True),
    Column("execucao_id", _ID, ForeignKey("execucao.id")),
    Column("modo", String(10), nullable=False),                 # TESTE | PRODUCAO
    Column("cpf", String(11), nullable=False),
    Column("nome", Text),
    Column("marco_dias", SmallInteger, nullable=False),         # 15 | 30 | 60
    Column("marco_tipo", String(20), nullable=False),           # aviso | suspensao | cancelamento
    Column("vencimento_base", Date, nullable=False),            # vencimento mais antigo em aberto (o "ciclo")
    Column("dias_atraso", Integer, nullable=False),
    Column("qtd_cobrancas", Integer),
    Column("valor_total", Numeric(14, 2)),
    Column("cobrancas", _JSON),                                 # boletos considerados
    Column("email_beneficiario", Text),                         # e-mail cadastrado no ERP
    Column("email_enviado_para", Text),                         # para onde foi de fato (em teste: caixa de teste)
    Column("provedor", String(20)),
    Column("status", String(20), nullable=False),               # PENDENTE | ENVIADO | ERRO | SEM_EMAIL
    Column("message_id", Text),                                 # id devolvido pelo SES/Brevo
    Column("erro", Text),
    Column("tentativas", SmallInteger, nullable=False, server_default="0"),
    Column("criado_em", DateTime(timezone=True), nullable=False, server_default=_AGORA),
    Column("atualizado_em", DateTime(timezone=True), nullable=False, server_default=_AGORA),
    Column("enviado_em", DateTime(timezone=True)),
    UniqueConstraint("modo", "cpf", "vencimento_base", "marco_dias", name="uq_notificacao"),
    CheckConstraint("status IN ('PENDENTE', 'ENVIADO', 'ERRO', 'SEM_EMAIL')", name="ck_notificacao_status"),
)
Index("ix_notificacao_modo_status", notificacao.c.modo, notificacao.c.status)
Index("ix_notificacao_cpf", notificacao.c.cpf)

_CHAVE = ["modo", "cpf", "vencimento_base", "marco_dias"]


class Controle:
    def __init__(self, url: str):
        opcoes = {"pool_pre_ping": True}
        if not url.startswith("sqlite"):
            opcoes.update(pool_size=2, max_overflow=0)
        self.engine = create_engine(url, **opcoes)
        self._insert = sqlite.insert if self.engine.dialect.name == "sqlite" else postgresql.insert

    # ------------------------------------------------------------------ schema
    def aplicar_schema(self) -> None:
        metadata.create_all(self.engine)   # só cria o que não existe
        log.info("Banco de controle pronto (%s)", self.engine.dialect.name)

    # ---------------------------------------------------------------- execução
    def iniciar_execucao(self, modo: str, provedor: str, fonte: str) -> int:
        with self.engine.begin() as conn:
            return conn.execute(
                execucao.insert().values(modo=modo, provedor=provedor, fonte_dados=fonte).returning(execucao.c.id)
            ).scalar_one()

    def finalizar_execucao(self, execucao_id: int, status: str, totais: dict, detalhe: str | None = None) -> None:
        with self.engine.begin() as conn:
            conn.execute(
                update(execucao).where(execucao.c.id == execucao_id).values(
                    finalizado_em=_AGORA, status=status, detalhe=detalhe,
                    total_candidatos=totais.get("candidatos", 0), total_enviados=totais.get("enviados", 0),
                    total_erros=totais.get("erros", 0), total_sem_email=totais.get("sem_email", 0),
                    total_ignorados=totais.get("ignorados", 0),
                )
            )

    # ------------------------------------------------------------ notificações
    def maior_marco_enviado(self, modo: str) -> dict[tuple[str, date], int]:
        """{(cpf, vencimento_base): maior marco já ENVIADO}."""
        n = notificacao.c
        with self.engine.connect() as conn:
            linhas = conn.execute(
                select(n.cpf, n.vencimento_base, func.max(n.marco_dias))
                .where(n.modo == modo, n.status == "ENVIADO")
                .group_by(n.cpf, n.vencimento_base)
            ).all()
        return {(cpf, venc): int(marco) for cpf, venc, marco in linhas}

    @staticmethod
    def _valores(execucao_id: int, modo: str, c: dict, provedor: str, enviado_para: str | None) -> dict:
        return {
            "execucao_id": execucao_id, "modo": modo, "cpf": c["cpf"], "nome": c["nome"],
            "marco_dias": int(c["marco_dias"]), "marco_tipo": c["marco_tipo"],
            "vencimento_base": c["vencimento_base"], "dias_atraso": int(c["dias_atraso"]),
            "qtd_cobrancas": int(c["qtd_cobrancas"]), "valor_total": c["valor_total"],
            "cobrancas": c["itens"],
            "email_beneficiario": c["email"] if isinstance(c["email"], str) else None,
            "email_enviado_para": enviado_para, "provedor": provedor,
        }

    def registrar_pendente(
        self, execucao_id: int, modo: str, candidato: dict, provedor: str, enviado_para: str, max_tentativas: int
    ) -> int | None:
        """
        Grava/reabre a notificação como PENDENTE e devolve o id.
        Devolve None se ela já foi ENVIADA ou esgotou as tentativas (não enviar).
        """
        valores = self._valores(execucao_id, modo, candidato, provedor, enviado_para)
        stmt = self._insert(notificacao).values(**valores, status="PENDENTE", tentativas=1)
        novo = stmt.excluded
        atuais = notificacao.c
        stmt = stmt.on_conflict_do_update(
            index_elements=_CHAVE,
            set_={
                "execucao_id": novo.execucao_id, "dias_atraso": novo.dias_atraso,
                "qtd_cobrancas": novo.qtd_cobrancas, "valor_total": novo.valor_total,
                "cobrancas": novo.cobrancas, "email_beneficiario": novo.email_beneficiario,
                "email_enviado_para": novo.email_enviado_para, "provedor": novo.provedor,
                "status": "PENDENTE", "erro": None,
                "tentativas": atuais.tentativas + 1, "atualizado_em": _AGORA,
            },
            where=atuais.status.in_(("ERRO", "PENDENTE", "SEM_EMAIL")) & (atuais.tentativas < max_tentativas),
        ).returning(atuais.id)
        with self.engine.begin() as conn:
            return conn.execute(stmt).scalar()

    def registrar_sem_email(self, execucao_id: int, modo: str, candidato: dict, provedor: str) -> None:
        """Beneficiário atingiu o marco mas não tem e-mail válido: fica registrado para tratamento."""
        valores = self._valores(execucao_id, modo, candidato, provedor, None)
        stmt = self._insert(notificacao).values(**valores, status="SEM_EMAIL", erro="Sem e-mail válido no cadastro")
        stmt = stmt.on_conflict_do_update(
            index_elements=_CHAVE,
            set_={
                "execucao_id": stmt.excluded.execucao_id, "dias_atraso": stmt.excluded.dias_atraso,
                "email_beneficiario": stmt.excluded.email_beneficiario, "atualizado_em": _AGORA,
            },
            where=notificacao.c.status == "SEM_EMAIL",
        )
        with self.engine.begin() as conn:
            conn.execute(stmt)

    def marcar_enviado(self, notificacao_id: int, message_id: str | None) -> None:
        with self.engine.begin() as conn:
            conn.execute(
                update(notificacao).where(notificacao.c.id == notificacao_id)
                .values(status="ENVIADO", message_id=message_id, enviado_em=_AGORA, atualizado_em=_AGORA)
            )

    def marcar_erro(self, notificacao_id: int, erro: str) -> None:
        with self.engine.begin() as conn:
            conn.execute(
                update(notificacao).where(notificacao.c.id == notificacao_id)
                .values(status="ERRO", erro=erro[:2000], atualizado_em=_AGORA)
            )

    def limpar_testes(self) -> int:
        """Apaga os registros de MODO=teste (para repetir os testes do zero)."""
        with self.engine.begin() as conn:
            n = conn.execute(delete(notificacao).where(notificacao.c.modo == "TESTE")).rowcount
            conn.execute(delete(execucao).where(execucao.c.modo == "TESTE"))
        return n
