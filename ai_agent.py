"""
ai_agent.py — Núcleo de inteligência do agente de follow-up.

Usa Claude Haiku para:
  1. Gerar mensagens de follow-up proativo baseadas no contexto do cliente
  2. Gerar respostas a mensagens recebidas
  3. Decidir quando escalar para o Pedro
  4. Sugerir a data do próximo follow-up com base no contexto
  5. Gerar relatório formatado para colar no Moskit
"""

import logging
import json
from dataclasses import dataclass, field
from typing import Optional
from datetime import datetime

import anthropic

from config import settings

logger = logging.getLogger(__name__)

client = anthropic.Anthropic(api_key=settings.ANTHROPIC_API_KEY)

MODEL = "claude-haiku-4-5-20251001"
MAX_TOKENS = 700


# ── Data classes ───────────────────────────────────────────────────────────────

@dataclass
class AgentResponse:
    message: str
    should_escalate: bool
    escalate_reason: str
    confidence: float
    next_followup_days: int = 3       # dias até o próximo follow-up
    next_followup_reason: str = ""    # por que sugere esse prazo


# ── System prompt ──────────────────────────────────────────────────────────────

SYSTEM_PROMPT = f"""Você é o assistente de vendas do {settings.SELLER_NAME}, vendedor da {settings.COMPANY_NAME}.

Seu trabalho é fazer follow-up com clientes via WhatsApp de forma profissional, \
calorosa e eficiente — como se fosse o próprio {settings.SELLER_NAME} digitando.

DIRETRIZES:
- Tom: profissional mas amigável, nunca robótico
- Tamanho: mensagens curtas (2-4 frases). WhatsApp não é e-mail.
- Nunca mencione que é IA ou assistente automático
- Use o nome do cliente quando souber
- Seja direto — o objetivo é avançar o negócio
- Nunca invente preços, prazos ou especificações técnicas — escale se não souber

QUANDO ESCALAR PARA {settings.SELLER_NAME.upper()}:
- Cliente claramente insatisfeito ou com reclamação grave
- Pedido de desconto ou negociação de preço
- Pergunta técnica específica fora do contexto
- Cliente diz que vai cancelar
- Situação de crise ou urgência

PRAZO DO PRÓXIMO FOLLOW-UP:
- Use o bom senso com base na conversa
- Se cliente pediu para retornar em X dias, use isso
- Se cliente demonstrou interesse alto: 1-2 dias
- Se cliente está avaliando: 3-5 dias
- Se cliente está distante/sem resposta: 5-7 dias
- Se escalado: 0 (Pedro vai decidir)

FORMATO DE RESPOSTA — APENAS JSON válido, sem markdown:
{{
  "message": "texto que será enviado ao cliente",
  "should_escalate": false,
  "escalate_reason": "",
  "confidence": 0.9,
  "next_followup_days": 3,
  "next_followup_reason": "Cliente está avaliando proposta, prazo razoável"
}}
"""


# ── Helpers ────────────────────────────────────────────────────────────────────

def _format_history(history: list[dict]) -> str:
    if not history:
        return "Nenhuma conversa anterior registrada."
    lines = []
    for msg in history:
        role = "Cliente" if msg.get("direction") == "inbound" else settings.SELLER_NAME
        lines.append(f"[{role}]: {msg.get('content', '')}")
    return "\n".join(lines)


def _format_client_context(client_notes: str, stage: str, value: float) -> str:
    stage_label = "Em Contato" if stage == "em_contato" else "Em Negociação"
    lines = [f"Etapa: {stage_label}"]
    if value:
        lines.append(f"Valor estimado: R$ {value:,.2f}")
    if client_notes:
        lines.append(f"Observações: {client_notes[:400]}")
    return "\n".join(lines)


def _parse_response(raw: str) -> AgentResponse:
    try:
        clean = raw.strip().strip("```json").strip("```").strip()
        data = json.loads(clean)
        return AgentResponse(
            message=data.get("message", ""),
            should_escalate=bool(data.get("should_escalate", False)),
            escalate_reason=data.get("escalate_reason", ""),
            confidence=float(data.get("confidence", 0.5)),
            next_followup_days=int(data.get("next_followup_days", 3)),
            next_followup_reason=data.get("next_followup_reason", ""),
        )
    except Exception as e:
        logger.warning("Falha ao parsear resposta da IA: %s | Raw: %s", e, raw[:200])
        return AgentResponse(
            message="",
            should_escalate=True,
            escalate_reason=f"Erro interno: {e}",
            confidence=0.0,
        )


def _call_claude(user_content: str) -> AgentResponse:
    try:
        resp = client.messages.create(
            model=MODEL,
            max_tokens=MAX_TOKENS,
            system=SYSTEM_PROMPT,
            messages=[{"role": "user", "content": user_content}],
        )
        return _parse_response(resp.content[0].text)
    except Exception as e:
        logger.error("Erro na API Claude: %s", e)
        return AgentResponse(
            message="",
            should_escalate=True,
            escalate_reason=f"Erro na API: {e}",
            confidence=0.0,
        )


# ── Funções principais ─────────────────────────────────────────────────────────

def generate_followup_message(
    contact_name: str,
    client_notes: str,
    stage: str,
    value: float,
    whatsapp_history: list[dict],
) -> AgentResponse:
    """Gera mensagem de follow-up proativo."""
    context = _format_client_context(client_notes, stage, value)
    history_text = _format_history(whatsapp_history)

    prompt = f"""TAREFA: Escreva um follow-up para este cliente.

CLIENTE: {contact_name}
{context}

HISTÓRICO DE MENSAGENS WHATSAPP (mais antigas primeiro):
{history_text}

Com base nesse contexto, escreva a mensagem mais adequada para avançar o negócio."""

    return _call_claude(prompt)


def generate_reply(
    incoming_message: str,
    contact_name: str,
    client_notes: str,
    stage: str,
    value: float,
    whatsapp_history: list[dict],
) -> AgentResponse:
    """Gera resposta a mensagem recebida do cliente."""
    context = _format_client_context(client_notes, stage, value)
    history_text = _format_history(whatsapp_history)

    prompt = f"""TAREFA: Responda a mensagem do cliente abaixo.

MENSAGEM DO CLIENTE: "{incoming_message}"

CLIENTE: {contact_name}
{context}

HISTÓRICO DE MENSAGENS WHATSAPP (sem a mensagem atual):
{history_text}

Responda de forma contextualizada e natural."""

    return _call_claude(prompt)


def generate_moskit_note(
    contact_name: str,
    stage: str,
    ai_message: str,
    client_response: str,
    action_type: str,
    next_followup_days: int,
    next_followup_reason: str,
) -> str:
    """
    Gera a nota formatada para colar no Moskit.
    Pedro copia essa nota e registra manualmente no CRM.
    """
    now = datetime.now().strftime("%d/%m/%Y %H:%M")
    stage_label = "Em Contato" if stage == "em_contato" else "Em Negociação"
    action_label = {
        "followup_sent": "✅ Follow-up proativo enviado",
        "reply_sent": "💬 Resposta automática enviada",
        "escalated": "⚠️ Escalado para vendedor",
    }.get(action_type, "🤖 Ação do agente")

    lines = [
        f"📋 *Registro automático — {now}*",
        f"Etapa: {stage_label}",
        f"Ação: {action_label}",
        "",
    ]

    if ai_message:
        lines += [f"📤 *Mensagem enviada:*", f'"{ai_message}"', ""]

    if client_response:
        lines += [f"📥 *Resposta do cliente:*", f'"{client_response}"', ""]

    if next_followup_days > 0:
        lines += [
            f"⏭️ *Próximo contato:* em {next_followup_days} dia(s)",
            f"Motivo: {next_followup_reason}",
        ]

    return "\n".join(lines)


def build_escalation_alert(
    contact_name: str,
    phone: str,
    reason: str,
    last_message: str,
    stage: str,
) -> str:
    stage_label = "Em Contato" if stage == "em_contato" else "Em Negociação"
    return (
        f"🔔 *Atenção necessária — Agente de Follow-up*\n\n"
        f"*Cliente:* {contact_name} ({phone})\n"
        f"*Etapa:* {stage_label}\n"
        f"*Última mensagem:* \"{last_message[:200]}\"\n\n"
        f"*Motivo do alerta:* {reason}\n\n"
        f"O agente pausou a automação para esse contato. "
        f"Responda diretamente pelo WhatsApp."
    )
