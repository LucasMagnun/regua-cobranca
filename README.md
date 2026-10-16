# Régua de cobrança por e-mail

Job diário que avisa por e-mail os titulares inadimplentes conforme o tempo de atraso:

| Atraso (dias) | E-mail |
|---|---|
| ≥ 15 | Aviso de pagamento em aberto |
| ≥ 30 | Aviso de suspensão |
| ≥ 60 | Aviso de cancelamento |

O atraso é contado **por beneficiário**, a partir do **vencimento mais antigo em aberto**.
As **3 cartas saem em sequência** (15 → 30 → 60), uma por vez, sem pular nenhuma, inclusive para
quem já tem mais de 60 dias de atraso na primeira execução (decisão da PO). Entre uma carta e a
seguinte há um intervalo mínimo: por padrão, 15 dias e depois 30 (`INTERVALO_ENTRE_CARTAS_DIAS`).

## Fluxo

```
segurado (contrato)  ->  pessoa (e-mail, pelo CPF)  ->  cobranca (financeiro)  ->  arquivo_retorno_detalhe (financeiro)
          \_______________________ calcula atraso por beneficiário _______________________/
                                   |
        banco de CONTROLE (quem já recebeu o quê)  ->  envia (console | SMTP/SES | Brevo | SES API)  ->  registra
```

## Rodando local SEM Docker (sem precisar de admin)

O banco de controle vira um arquivo SQLite (`controle_regua.db`) na pasta do projeto, que o DBeaver abre.

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
Copy-Item .env.example .env        # e ajuste os valores
python rodar.py
```
Rode sempre de dentro da pasta do projeto. Para limpar os testes: `python rodar.py --limpar-teste`.

Para ver as 3 cartas na sua caixa (dados fictícios, não lê os bancos nem grava no controle):
`python rodar.py --previa` (exige `MODO=teste` e `EMAIL_DESTINO_TESTE`).

## Rodando numa VM (Google Compute Engine) com cron, sem Docker

```bash
sudo timedatectl set-timezone America/Sao_Paulo   # o cron passa a usar o horário de Brasília
cd ~/regua-cobranca && python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
cp .env.example .env && nano .env                 # CONTROLE_DB_URL com caminho absoluto (4 barras)
bash scripts/rodar_cron.sh                         # teste manual; log em logs/regua-AAAA-MM-DD.log
crontab -e                                         # 0 9 * * * /bin/bash /home/ubuntu/regua-cobranca/scripts/rodar_cron.sh
```
Na VM o disco é permanente, então o banco de controle pode ser o SQLite
(`CONTROLE_DB_URL=sqlite:////home/ubuntu/regua-cobranca/controle_regua.db`). Faça backup dele (snapshot do disco):
é ele que impede que alguém receba a mesma carta duas vezes.

## Rodando local com Docker (opcional)

```powershell
copy .env.example .env          # e ajuste os valores
docker compose up -d controle-db
docker compose run --rm regua
```

### Etapa 1: simulação, sem enviar nada
No `.env`: `MODO=teste`, `FONTE_DADOS=exemplo`, `PROVEDOR_EMAIL=console`.
O log mostra quem receberia cada aviso.

### Etapa 2: envio real para a sua caixa, com dados fictícios
1. Crie uma conta no Brevo e verifique um remetente (*Senders, Domains & Dedicated IPs*).
2. Gere uma API key (*SMTP & API > API Keys*).
3. No `.env`:
   ```
   PROVEDOR_EMAIL=brevo
   BREVO_API_KEY=xkeysib-...
   REMETENTE_EMAIL=o.remetente.verificado@...
   EMAIL_DESTINO_TESTE=seu.email@...
   ```
4. `python rodar.py --limpar-teste` e depois `python rodar.py`

Em `MODO=teste` **todo e-mail vai para `EMAIL_DESTINO_TESTE`**, com `[TESTE]` no assunto e uma faixa
dizendo para quem iria em produção. São no máximo 5 por execução, salvo se `MAX_ENVIOS_POR_EXECUCAO` for definido.

### Etapa 2 (alternativa): Amazon SES via SMTP
Com o usuário e a senha SMTP do SES (não são chaves de acesso da AWS):
```
PROVEDOR_EMAIL=smtp
SMTP_HOST=email-smtp.sa-east-1.amazonaws.com
SMTP_PORTA=587
SMTP_USUARIO=...
SMTP_SENHA=...
REMETENTE_EMAIL=<endereço ou domínio VERIFICADO no SES>
EMAIL_DESTINO_TESTE=seu.email@...
```
Se a conta do SES estiver em *sandbox*, o `EMAIL_DESTINO_TESTE` também precisa estar verificado no SES.

### Etapa 3: dados reais, e-mails ainda só para você
`FONTE_DADOS=banco`, com as credenciais dos bancos no `.env` e a chave `.pem` na pasta `secrets/`
(`SSH_KEY_PATH=/secrets/lucas_castro.pem`). Continue em `MODO=teste`.

### Produção
`MODO=producao` + `CONFIRMAR_PRODUCAO=sim` + `PROVEDOR_EMAIL=smtp` (ou `ses`, se houver chave de API). Sem essa confirmação o job se recusa a rodar.

## Banco de controle

É um banco **separado do ERP**, onde o job tem permissão de escrita: SQLite na sua máquina, PostgreSQL em produção.
As tabelas são criadas automaticamente na primeira execução (definidas em `src/regua/controle.py`).

- **`execucao`**: uma linha por execução do job (status e totais).
- **`notificacao`**: uma linha por beneficiário + ciclo + marco. É o histórico e o comprovante de envio.
  - `status`: `PENDENTE` → `ENVIADO` (com `message_id` do provedor) | `ERRO` (tentado de novo até `MAX_TENTATIVAS`) | `SEM_EMAIL`
  - a chave única `(modo, cpf, vencimento_base, marco_dias)` impede envio duplicado
  - `modo` separa testes (`TESTE`) de envios reais (`PRODUCAO`)

Para consultar no DBeaver: *Nova conexão > SQLite* e escolha o arquivo `controle_regua.db` (com Docker: PostgreSQL em `localhost:55432`, banco/usuário/senha `regua`).

```sql
SELECT nome, marco_tipo, dias_atraso, status, email_enviado_para, enviado_em, erro
FROM notificacao ORDER BY atualizado_em DESC;
```

## Testes

```powershell
pip install -r requirements-dev.txt
$env:PYTHONPATH="src"; pytest
```
Os testes de fluxo rodam sempre no SQLite. Para rodar também no PostgreSQL, defina `CONTROLE_DB_URL_TESTE`
apontando para um banco descartável (ele **apaga** as tabelas).

## Código de saída
`0` tudo certo | `1` algum e-mail com erro | `2` falha geral ou configuração inválida.
O Cloud Run Jobs marca a execução como falha quando o código é diferente de 0.

## Regras que vêm dos textos aprovados
- Os textos estão em `src/regua/templates/`, fiéis aos documentos `notificacao_15/30/60_dias_versao_final_1.docx`.
- Os e-mails de 30 e 60 dias **avisam** que o plano *será* suspenso/cancelado (ajuste aprovado pela PO; os
  documentos originais diziam "houve"). Por isso não dependem da situação no ERP. Se um dia voltarem a afirmar
  um fato, dá para exigir a situação com `SITUACAO_EXIGIDA=suspensao:SUSPENSO,cancelamento:CANCELADO`.
- Os e-mails **não levam boleto**: orientam o beneficiário a emitir o boleto atualizado no Portal do Beneficiário
  (Cobrança > Dívidas Atualizadas), com botão para `PORTAL_URL` (texto em `templates/_portal.html`).

## Pendências antes de produção
- [x] **Boleto**: substituído pelo link do Portal do Beneficiário (decisão da PO).
- [ ] **E-mail de atendimento** (`EMAIL_ATENDIMENTO`): o documento traz "&lt;incluir e-mail de atendimento ao beneficiário da Maida&gt;".
- [ ] **Assunto do e-mail de 30 dias**: ajustado para "Aviso de Suspensão" (o documento dizia "Reativação"). Validar com a PO. O de 60 dias segue o documento.
- [ ] **Validar com a PO os novos textos** de 30 e 60 dias (`templates/suspensao.html` e `cancelamento.html`).
- [ ] **Valores da situação do plano** no ERP: confirmar se são exatamente `SUSPENSO` e `CANCELADO`.
- [x] Origem do e-mail: banco pessoa, tabela `pessoa`, cruzando `cpf_cnpj`. Falta confirmar o nome da coluna (`COLUNA_EMAIL`) e o nome do banco (`PESSOA_DB_NAME`).
- [ ] Regra da 1ª execução (`DIAS_ATRASO_MAXIMO`).
- [ ] Contrato de produção, SES (remetente verificado, fora do sandbox) e banco de controle definitivo.
- [ ] GCP: Cloud Run Job + Cloud Scheduler + Secret Manager + saída de rede liberada no jump.
