"""
zapi_client.py — Wrapper para a Z-API (WhatsApp Business).

Documentação Z-API: https://developer.z-api.io/

Operações:
  - Enviar mensagem de texto
  - Enviar status "digitando..." (experiência mais natural)
  - Parsear payload de webhook (mensagens recebidas)
"""

import logging
import time
from typing import Optional
import httpx

from config import settings

logger = logging.getLogger(__name__)

BASE_URL = (
    f"https://api.z-api.io/instances/{settings.ZAPI_INSTANCE_ID}"
    f"/token/{settings.ZAPI_TOKEN}"
)

HEADERS = {
    "Client-Token": settings.ZAPI_CLIENT_TOKEN,
    "Content-Type": "application/json",
}


def _format_phone(phone: str) -> str:
    """
    Garante que o número esteja no formato E.164 sem '+'.
    Z-API espera: 5511999999999 (país + DDD + número)
    """
    digits = "".join(c for c in phone if c.isdigit())
    # Se não começar com 55 (Brasil), adiciona
    if not digits.startswith("55"):
        digits = "55" + digits
    return digits


def send_typing(phone: str, duration_seconds: int = 2) -> None:
    """
    Envia status 'digitando...' por alguns segundos antes da mensagem.
    Torna a interação mais natural e humana.
    """
    try:
        formatted = _format_phone(phone)
        httpx.post(
            f"{BASE_URL}/typing",
            headers=HEADERS,
            json={"phone": formatted, "seconds": duration_seconds},
            timeout=10,
        )
        time.sleep(duration_seconds)
    except Exception as e:
        logger.debug("send_typing ignorado: %s", e)


def send_text(phone: str, message: str, simulate_typing: bool = True) -> bool:
    """
    Envia uma mensagem de texto via Z-API.

    Args:
        phone: Número do destinatário (qualquer formato, será normalizado)
        message: Texto da mensagem
        simulate_typing: Se True, envia status 'digitando' antes (mais natural)

    Returns:
        True se enviou com sucesso, False se houve erro.
    """
    formatted = _format_phone(phone)

    if simulate_typing:
        # Calcula duração proporcional ao tamanho da mensagem (máx 4s)
        typing_secs = min(len(message) // 40 + 1, 4)
        send_typing(formatted, typing_secs)

    try:
        r = httpx.post(
            f"{BASE_URL}/send-text",
            headers=HEADERS,
            json={"phone": formatted, "message": message},
            timeout=20,
        )
        r.raise_for_status()
        logger.info("Z-API → mensagem enviada para %s (%d chars)", formatted, len(message))
        return True
    except httpx.HTTPStatusError as e:
        logger.error(
            "Z-API send-text HTTP %s para %s: %s",
            e.response.status_code,
            formatted,
            e.response.text,
        )
        return False
    except Exception as e:
        logger.error("Z-API send-text erro para %s: %s", formatted, e)
        return False


# ── Webhook parsing ────────────────────────────────────────────────────────────

def parse_webhook_payload(payload: dict) -> Optional[dict]:
    """
    Parseia o payload do webhook Z-API e retorna um dict normalizado.

    Retorna None se não for uma mensagem de texto recebida de pessoa física
    (ignora grupos, status, notificações de sistema, etc.)

    Formato retornado:
    {
        "phone": "5511999999999",
        "name":  "Nome do contato",
        "text":  "Texto da mensagem",
        "message_id": "...",
        "is_from_me": False,
    }
    """
    # Filtra apenas mensagens recebidas de texto
    msg_type = payload.get("type", "")
    if msg_type not in ("ReceivedCallback", "received"):
        return None

    # Ignora grupos
    if payload.get("isGroupMsg") or payload.get("chatId", "").endswith("@g.us"):
        return None

    # Ignora mensagens enviadas pelo próprio número
    from_me = payload.get("fromMe", False)
    if from_me:
        return None

    # Extrai o texto — pode vir em campos diferentes conforme versão da Z-API
    text = (
        payload.get("text", {}).get("message")
        or payload.get("body")
        or payload.get("message", {}).get("conversation")
        or ""
    )

    if not text or not text.strip():
        return None

    # Extrai o telefone — remove @c.us se presente
    phone_raw = payload.get("phone") or payload.get("chatId", "")
    phone = phone_raw.replace("@c.us", "").replace("@s.whatsapp.net", "")
    phone = "".join(c for c in phone if c.isdigit())

    if not phone:
        return None

    name = (
        payload.get("senderName")
        or payload.get("pushName")
        or payload.get("name")
        or phone
    )

    return {
        "phone": phone,
        "name": name,
        "text": text.strip(),
        "message_id": payload.get("messageId") or payload.get("id", ""),
        "is_from_me": False,
    }
