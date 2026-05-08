"""
ai_agent.py — Núcleo de inteligência do agente de follow-up.

Usa a API da Anthropic (Claude Haiku) para:
  1. Gerar mensagens de follow-up proativo baseadas no contexto do deal
  2. Gerar respostas a mensagens recebidas dos clientes
  3. Decidir quando escalar para o vendedor humano (Pedro)

O agente conhece o histórico da conversa no WhatsApp e as atividades
do Moskit, garantindo respostas sempre contextualizadas.
"""

import logging
import json
from dataclasses import dataclass
from typing import Optional

import anthropic

from config import settings

logger = logging.getLogger(__name__)

client = anthropic.Anthropic(api_key=settings.ANTHROPIC_API_KEY)

# Modelo: Haiku é rápido, barato e mais que suficiente para follow-up de vendas
MODEL = "claude-haiku-4-5-20251001"
MAX_TOKENS = 600  # Mensagens de WhatsApp devem ser curtas e diretas


# ── Data classes ───────────────────────────────────────────────────────────────

@dataclass
class AgentResponse:
    message: str            # Texto a enviar para o cliente
    should_escalate: bool   # True = avisar o Pedro para tomar o controle
    escalate_reason: str    # Por que escalou (vazio se should_escalate=False)
    confidence: float       # 0.0–1.0 (confiança da IA na resposta)


# ── System prompt base ─────────────────────────────────────────────────────────

SYSTEM_PROMPT = f"""Você é o assistente de vendas do {settings.SELLER_NAME}, vendedor da {settings.COMPANY_NAME}.

Seu trabalho é fazer follow-up com clientes via WhatsApp de forma profissional, \
calorosa e eficiente — como se fosse o próprio {settings.SELLER_NAME} digitando.

DIRETRIZES DE COMUNICAÇÃO:
- Tom: profissional mas amigável, nunca robótico
- Tamanho: mensagens curtas (2-4 frases no máximo). WhatsApp não é e-mail.
- Nunca mencione que é uma IA ou assistente automático
- Use o nome do cliente quando souber
- Seja direto ao ponto — o objetivo é avançar o negócio
- Se o cliente fizer uma pergunta técnica ou comercial que você não sabe responder \
com certeza, sinalize que precisa escalar para {settings.SELLER_NAME} ver pessoalmente
- Nunca invente informações sobre produto, preço ou prazo — escale se não souber

QUANDO ESCALAR PARA {settings.SELLER_NAME.upper()}:
- Cliente está claramente insatisfeito ou com reclamação grave
- Pedido de desconto ou negociação de preço
- Pergunta técnica muito específica fora do contexto do deal
- Cliente diz que vai cancelar ou já cancelou
- Situação de crise ou urgência que exige decisão humana
- Qualquer coisa que você sinta que é sensível demais para a IA tratar

FORMATO DE RESPOSTA:
Responda APENAS com JSON válido, sem markdown, sem explicações fora do JSON:
{{
  "message": "texto que será enviado ao cliente",
  "should_escalate": false,
  "escalate_reason": "",
  "confidence": 0.9
}}
"""


# ── Helpers ────────────────────────────────────────────────────────────────────

def _format_history(history: list[dict]) -> str:
    """Formata o histórico de conversa para incluir no prompt."""
    if not history:
        return "Nenhuma conversa anterior registrada."
    lines = []
    for msg in history:
        role = "Cliente" if msg["direction"] == "inbound" else settings.SELLER_NAME
        lines.append(f"[{role}]: {msg['content']}")
    return "\n".join(lines)


def _format_moskit_history(activities: list[dict]) -> str:
    """Formata as atividades recentes do Moskit para contexto."""
    if not activities:
        return "Nenhuma atividade anterior registrada no CRM."
    lines = []
    for act in activities[:settings.CONTEXT_HISTORY_LIMIT]:
        title = act.get("title") or act.get("text") or act.get("content", "")
        date = act.get("dueDate") or act.get("createdAt") or act.get("date", "")
        act_type = act.get("type", "")
        status = act.get("status", "")
        if title:
            lines.append(f"- [{date[:10] if date else 'sem data'}] ({act_type}/{status}): {title[:200]}")
    return "\n".join(lines) if lines else "Nenhuma atividade com detalhes encontrada."


def _parse_llm_response(raw: str) -> AgentResponse:
    """Parseia a resposta JSON do Claude. Em caso de falha, escalada de segurança."""
    try:
        # Remove possível markdown residual
        clean = raw.strip().strip("```json").strip("```").strip()
        data = json.loads(clean)
        return AgentResponse(
            message=data.get("message", ""),
            should_escalate=bool(data.get("should_escalate", False)),
            escalate_reason=data.get("escalate_reason", ""),
            confidence=float(data.get("confidence", 0.5)),
        )
    except Exception as e:
        logger.warning("Falha ao parsear resposta da IA: %s | Raw: %s", e, raw[:200])
        # Em caso de falha no parse, escala por segurança
        return AgentResponse(
            message="",
            should_escalate=True,
            escalate_reason=f"Erro interno ao processar resposta: {e}",
            confidence=0.0,
        )


# ── Funções principais ─────────────────────────────────────────────────────────

def generate_followup_message(
    activity_title: str,
    deal_summary: str,
    contact_name: str,
    whatsapp_history: list[dict],
    moskit_history: list[dict],
) -> AgentResponse:
    """
    Gera uma mensagem de follow-up proativo para uma atividade pendente.

    Args:
        activity_title: Título da atividade no Moskit (ex: "Retornar sobre proposta")
        deal_summary: Resumo textual do negócio (gerado por moskit_client.summarize_deal_for_context)
        contact_name: Nome do contato
        whatsapp_history: Histórico de mensagens WhatsApp do banco local
        moskit_history: Atividades/notas anteriores do deal no Moskit
    """
    wa_history_text = _format_history(whatsapp_history)
    crm_history_text = _format_moskit_history(moskit_history)

    user_content = f"""TAREFA: Escreva um follow-up para a atividade abaixo.

ATIVIDADE NO CRM: {activity_title}

INFORMAÇÕES DO NEGÓCIO:
{deal_summary}

NOME DO CLIENTE: {contact_name}

HISTÓRICO DE MENSAGENS WHATSAPP (mais antigas primeiro):
{wa_history_text}

HISTÓRICO DE ATIVIDADES NO MOSKIT (CRM):
{crm_history_text}

Com base nesse contexto, escreva a mensagem de follow-up mais adequada para \
avançar esse negócio. Seja natural, direto e relevante ao momento do deal."""

    try:
        resp = client.messages.create(
            model=MODEL,
            max_tokens=MAX_TOKENS,
            system=SYSTEM_PROMPT,
            messages=[{"role": "user", "content": user_content}],
        )
        raw = resp.content[0].text
        return _parse_llm_response(raw)
    except Exception as e:
        logger.error("Erro ao chamar Claude API (followup): %s", e)
        return AgentResponse(
            message="",
            should_escalate=True,
            escalate_reason=f"Erro na API Claude: {e}",
            confidence=0.0,
        )


def generate_reply(
    incoming_message: str,
    contact_name: str,
    deal_summary: str,
    whatsapp_history: list[dict],
    moskit_history: list[dict],
) -> AgentResponse:
    """
    Gera uma resposta a uma mensagem recebida do cliente.

    Args:
        incoming_message: Texto que o cliente acabou de enviar
        contact_name: Nome do contato no Moskit
        deal_summary: Resumo do negócio ativo
        whatsapp_history: Histórico de mensagens (sem a mensagem atual)
        moskit_history: Atividades anteriores do deal no CRM
    """
    wa_history_text = _format_history(whatsapp_history)
    crm_history_text = _format_moskit_history(moskit_history)

    user_content = f"""TAREFA: Responda a mensagem do cliente abaixo.

MENSAGEM DO CLIENTE: "{incoming_message}"

NOME DO CLIENTE: {contact_name}

INFORMAÇÕES DO NEGÓCIO:
{deal_summary}

HISTÓRICO DE MENSAGENS WHATSAPP (mais antigas primeiro, sem a mensagem atual):
{wa_history_text}

HISTÓRICO DE ATIVIDADES NO MOSKIT (CRM):
{crm_history_text}

Responda de forma contextualizada. Se precisar escalar para {settings.SELLER_NAME}, \
informe o motivo claramente no campo escalate_reason."""

    try:
        resp = client.messages.create(
            model=MODEL,
            max_tokens=MAX_TOKENS,
            system=SYSTEM_PROMPT,
            messages=[{"role": "user", "content": user_content}],
        )
        raw = resp.content[0].text
        return _parse_llm_response(raw)
    except Exception as e:
        logger.error("Erro ao chamar Claude API (reply): %s", e)
        return AgentResponse(
            message="",
            should_escalate=True,
            escalate_reason=f"Erro na API Claude: {e}",
            confidence=0.0,
        )


def build_escalation_alert(
    contact_name: str,
    phone: str,
    reason: str,
    last_message: str,
    deal_name: str,
) -> str:
    """
    Monta a mensagem de alerta para o Pedro quando a IA precisa escalar.
    Enviada para o número pessoal do Pedro via WhatsApp.
    """
    return (
        f"🔔 *Atenção necessária — Agente de Follow-up*\n\n"
        f"*Cliente:* {contact_name} ({phone})\n"
        f"*Negócio:* {deal_name}\n"
        f"*Última mensagem:* \"{last_message[:200]}\"\n\n"
        f"*Motivo do alerta:* {reason}\n\n"
        f"O agente pausou a automação para esse contato. "
        f"Responda diretamente pelo WhatsApp."
    )


def build_moskit_note(
    direction: str,
    message: str,
    was_escalated: bool = False,
) -> str:
    """
    Monta o texto da nota que será registrada no Moskit após cada interação.
    """
    icon = "📤" if direction == "outbound" else "📥"
    label = "Mensagem enviada" if direction == "outbound" else "Mensagem recebida"
    suffix = " ⚠️ [Escalado para vendedor]" if was_escalated else " ✅ [Tratado pelo agente]"
    return f"{icon} *{label} via WhatsApp automático:*\n\n\"{message}\"{suffix}"
