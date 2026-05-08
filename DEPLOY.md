# Guia de Deploy — Agente de Follow-up

## O que esse sistema faz

```
Moskit CRM ──► Scheduler (cada 60min) ──► Claude IA ──► Z-API ──► Cliente WhatsApp
                                                │
                                         Registro no Moskit
                                         (nota + atividade concluída)

Cliente responde ──► Z-API Webhook ──► Claude IA ──► Z-API ──► Resposta ao cliente
                                             │
                                      Registro no Moskit
```

---

## Pré-requisitos

- [x] Conta Z-API ativa com instância conectada ao WhatsApp Business
- [x] Chave API do Moskit (Configurações > Integrações > API)
- [x] Chave API da Anthropic (console.anthropic.com)
- [x] Conta no Railway (railway.app) — plano gratuito funciona

---

## Passo 1 — Preparar o repositório

```bash
# Na pasta do projeto
git init
git add .
git commit -m "feat: agente de follow-up inicial"
```

Suba para um repositório no GitHub (pode ser privado).

---

## Passo 2 — Deploy no Railway

1. Acesse https://railway.app e faça login com o GitHub
2. Clique em **New Project → Deploy from GitHub repo**
3. Selecione o repositório do agente
4. Railway detecta o `Procfile` automaticamente

---

## Passo 3 — Configurar variáveis de ambiente

No Railway, vá em **Settings → Variables** e adicione todas as variáveis do `.env.example`:

| Variável | Onde encontrar |
|---|---|
| `MOSKIT_API_KEY` | Moskit > Config > Integrações > API |
| `MOSKIT_USER_ID` | GET https://api.moskit.com.br/v2/users/me |
| `ZAPI_INSTANCE_ID` | Painel Z-API |
| `ZAPI_TOKEN` | Painel Z-API |
| `ZAPI_CLIENT_TOKEN` | Crie um token forte qualquer (ex: use um UUID) |
| `ANTHROPIC_API_KEY` | console.anthropic.com/settings/keys |
| `PEDRO_ALERT_PHONE` | Seu número (5511999999999) |

---

## Passo 4 — Configurar Webhook na Z-API

1. No painel Z-API, vá na sua instância
2. Em **Webhooks**, adicione:
   - **URL**: `https://SEU-APP.railway.app/webhook`
   - **Events**: marque **Received Messages**
   - **Client-Token**: o mesmo valor de `ZAPI_CLIENT_TOKEN`

---

## Passo 5 — Verificar se está funcionando

```bash
# Health check
curl https://SEU-APP.railway.app/

# Disparar rodada manual de follow-ups
curl -X POST https://SEU-APP.railway.app/run-now \
  -H "x-admin-token: SEU_ZAPI_CLIENT_TOKEN"
```

Verifique os logs no Railway para acompanhar a execução.

---

## Personalizar o agente

### Ajustar o tom das mensagens
Edite o `SYSTEM_PROMPT` em `ai_agent.py`. Você pode adicionar exemplos de mensagens
no seu estilo, expressões que usa, etc. Quanto mais contexto, melhor a IA se adapta.

### Mudar a frequência de varredura
Altere `SCHEDULER_INTERVAL_MINUTES` no `.env` (ou Railway Variables).
- `30` = varre a cada 30 min
- `60` = a cada hora (padrão)

### Mudar quando cria próximo follow-up
Altere `NEXT_FOLLOWUP_DAYS` e `AUTO_SCHEDULE_NEXT`.

---

## Encontrar seu MOSKIT_USER_ID

```bash
curl https://api.moskit.com.br/v2/users/me \
  -H "apikey: SUA_CHAVE_API"
```

O campo `id` da resposta é o seu User ID.

---

## Custo estimado

| Serviço | Custo |
|---|---|
| Railway | Gratuito (500h/mês) ou ~$5/mês pro |
| Z-API | ~R$ 70/mês |
| Anthropic (Claude Haiku) | ~$0,001/mensagem → ~R$ 1/dia para 40 msgs |
| **Total** | ~R$ 75/mês |
