-- Banco de CONTROLE da régua de cobrança (separado dos bancos do ERP).
-- Idempotente: pode rodar várias vezes. O job aplica este arquivo ao iniciar.

-- Uma linha por execução do job (diária).
CREATE TABLE IF NOT EXISTS execucao (
    id               BIGSERIAL PRIMARY KEY,
    modo             VARCHAR(10)  NOT NULL,                  -- TESTE | PRODUCAO
    provedor         VARCHAR(20)  NOT NULL,                  -- console | brevo | ses
    fonte_dados      VARCHAR(20)  NOT NULL,                  -- banco | exemplo
    iniciado_em      TIMESTAMPTZ  NOT NULL DEFAULT now(),
    finalizado_em    TIMESTAMPTZ,
    status           VARCHAR(20)  NOT NULL DEFAULT 'EM_ANDAMENTO',
    total_candidatos INT,
    total_enviados   INT,
    total_erros      INT,
    total_sem_email  INT,
    total_ignorados  INT,
    detalhe          TEXT,
    CONSTRAINT ck_execucao_status CHECK (status IN ('EM_ANDAMENTO', 'OK', 'COM_ERROS', 'FALHOU'))
);

-- Uma linha por (beneficiário, ciclo de inadimplência, marco).
-- É o histórico/comprovante de notificação e a trava contra envio duplicado.
CREATE TABLE IF NOT EXISTS notificacao (
    id                 BIGSERIAL PRIMARY KEY,
    execucao_id        BIGINT       REFERENCES execucao (id),
    modo               VARCHAR(10)  NOT NULL,                -- TESTE | PRODUCAO
    cpf                CHAR(11)     NOT NULL,
    nome               TEXT,
    marco_dias         SMALLINT     NOT NULL,                -- 15 | 30 | 60
    marco_tipo         VARCHAR(20)  NOT NULL,                -- aviso | suspensao | cancelamento
    vencimento_base    DATE         NOT NULL,                -- vencimento mais antigo em aberto (identifica o ciclo)
    dias_atraso        INT          NOT NULL,
    qtd_cobrancas      INT,
    valor_total        NUMERIC(14, 2),
    cobrancas          JSONB,                                -- boletos citados no e-mail
    email_beneficiario TEXT,                                 -- e-mail cadastrado no ERP
    email_enviado_para TEXT,                                 -- para onde foi de fato (em teste: caixa de teste)
    provedor           VARCHAR(20),
    status             VARCHAR(20)  NOT NULL,                -- PENDENTE | ENVIADO | ERRO | SEM_EMAIL
    message_id         TEXT,                                 -- id devolvido pelo Brevo/SES
    erro               TEXT,
    tentativas         SMALLINT     NOT NULL DEFAULT 0,
    criado_em          TIMESTAMPTZ  NOT NULL DEFAULT now(),
    atualizado_em      TIMESTAMPTZ  NOT NULL DEFAULT now(),
    enviado_em         TIMESTAMPTZ,
    CONSTRAINT uq_notificacao UNIQUE (modo, cpf, vencimento_base, marco_dias),
    CONSTRAINT ck_notificacao_status CHECK (status IN ('PENDENTE', 'ENVIADO', 'ERRO', 'SEM_EMAIL'))
);

CREATE INDEX IF NOT EXISTS ix_notificacao_modo_status ON notificacao (modo, status);
CREATE INDEX IF NOT EXISTS ix_notificacao_cpf        ON notificacao (cpf);
