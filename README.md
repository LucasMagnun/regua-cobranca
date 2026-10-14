# Régua de cobrança por e-mail

Job diário que avisa por e-mail os titulares inadimplentes conforme o tempo de atraso:

| Atraso (dias) | E-mail |
|---|---|
| ≥ 15 | Aviso de pagamento em aberto |
| ≥ 30 | Aviso de suspensão |
| ≥ 60 | Aviso de cancelamento |

O atraso é contado **por beneficiário**, a partir do **vencimento mais antigo em aberto**.
Cada pessoa recebe só o maior marco atingido, uma única vez por ciclo de inadimplência.

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
- Os e-mails de 30 e 60 dias **afirmam** que houve suspensão/cancelamento. Por isso só saem quando a situação
  do plano no ERP confirma (`SITUACAO_EXIGIDA=suspensao:SUSPENSO,cancelamento:CANCELADO`). Até lá, a pessoa
  recebe o maior marco permitido, e o seguinte sai quando o ERP atualizar.
- Os três textos citam um **boleto com vencimento**. Em produção, sem boleto o e-mail não sai: fica para a
  próxima execução e não conta tentativa. Em teste, aparece `<dd/mm/aaaa> [boleto ainda não integrado]`.

## Pendências antes de produção
- [ ] **Boleto**: de onde vem o boleto que "segue" no e-mail (link, linha digitável ou PDF anexo) e seu vencimento.
- [ ] **E-mail de atendimento** (`EMAIL_ATENDIMENTO`): o documento traz "&lt;incluir e-mail de atendimento ao beneficiário da Maida&gt;".
- [ ] **Assunto do e-mail de 60 dias**: o documento não tem. Hoje está "Cancelamento do Plano por Inadimplência" (`config.py`, `ASSUNTOS`).
- [ ] **Valores da situação do plano** no ERP: confirmar se são exatamente `SUSPENSO` e `CANCELADO`.
- [x] Origem do e-mail: banco pessoa, tabela `pessoa`, cruzando `cpf_cnpj`. Falta confirmar o nome da coluna (`COLUNA_EMAIL`) e o nome do banco (`PESSOA_DB_NAME`).
- [ ] Regra da 1ª execução (`DIAS_ATRASO_MAXIMO`).
- [ ] Contrato de produção, SES (remetente verificado, fora do sandbox) e banco de controle definitivo.
- [ ] GCP: Cloud Run Job + Cloud Scheduler + Secret Manager + saída de rede liberada no jump.
