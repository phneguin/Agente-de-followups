"""
moskit_client.py — Wrapper para a API V2 do Moskit CRM.

Documentação oficial: https://moskit.stoplight.io/docs/api-v2/

Principais operações:
  - Buscar atividades pendentes do usuário
  - Buscar detalhes de deal e contato
  - Buscar histórico de atividades de um deal
  - Marcar atividade como concluída
  - Criar nota/anotação no deal
  - Agendar próxima atividade
  - Buscar contato pelo número de telefone
"""

import logging
import re
from typing import Optional
import httpx

from config import settings

logger = logging.getLogger(__name__)

BASE_URL = "https://app.ms.prod.moskit.services/v2"

HEADERS = {
    "Apikey": settings.MOSKIT_API_KEY,
    "Content-Type": "application/json",
    "Accept": "application/json",
}


def _clean_phone(phone: str) -> str:
    """Remove tudo que não é dígito de um número de telefone."""
    return re.sub(r"\D", "", phone)


def _get(path: str, params: Optional[dict] = None) -> Optional[dict | list]:
    """GET genérico com tratamento de erro."""
    try:
        r = httpx.get(f"{BASE_URL}{path}", headers=HEADERS, params=params, timeout=15)
        r.raise_for_status()
        return r.json()
    except httpx.HTTPStatusError as e:
        logger.error("Moskit GET %s → HTTP %s: %s", path, e.response.status_code, e.response.text)
        return None
    except Exception as e:
        logger.error("Moskit GET %s → %s", path, e)
        return None


def _post(path: str, payload: dict) -> Optional[dict]:
    """POST genérico com tratamento de erro."""
    try:
        r = httpx.post(f"{BASE_URL}{path}", headers=HEADERS, json=payload, timeout=15)
        r.raise_for_status()
        return r.json()
    except httpx.HTTPStatusError as e:
        logger.error("Moskit POST %s → HTTP %s: %s", path, e.response.status_code, e.response.text)
        return None
    except Exception as e:
        logger.error("Moskit POST %s → %s", path, e)
        return None


def _put(path: str, payload: dict) -> Optional[dict]:
    """PUT genérico com tratamento de erro."""
    try:
        r = httpx.put(f"{BASE_URL}{path}", headers=HEADERS, json=payload, timeout=15)
        r.raise_for_status()
        return r.json()
    except httpx.HTTPStatusError as e:
        logger.error("Moskit PUT %s → HTTP %s: %s", path, e.response.status_code, e.response.text)
        return None
    except Exception as e:
        logger.error("Moskit PUT %s → %s", path, e)
        return None


# ── Atividades ─────────────────────────────────────────────────────────────────

def get_pending_activities(user_id: str) -> list[dict]:
    """
    Retorna todas as atividades pendentes (status=open) do usuário para hoje.
    A API do Moskit retorna atividades paginadas; buscamos até 100 por chamada.
    """
    data = _get("/activities", params={
        "responsibleId": user_id,
        "status": "open",
        "page": 1,
        "pageSize": 100,
    })

    if data is None:
        return []

    # A API pode retornar { "data": [...] } ou diretamente uma lista
    if isinstance(data, dict):
        return data.get("data", data.get("activities", []))
    return data if isinstance(data, list) else []


def get_activity(activity_id: str) -> Optional[dict]:
    """Busca uma atividade pelo ID."""
    return _get(f"/activities/{activity_id}")


def complete_activity(activity_id: str) -> bool:
    """Marca uma atividade como concluída no Moskit."""
    result = _put(f"/activities/{activity_id}", {"status": "done"})
    if result is None:
        # Tenta endpoint alternativo
        result = _post(f"/activities/{activity_id}/done", {})
    return result is not None


def create_activity(
    deal_id: str,
    contact_id: str,
    title: str,
    due_date: str,
    activity_type: str = "task",
    responsible_id: Optional[str] = None,
) -> Optional[dict]:
    """
    Cria uma nova atividade de follow-up no Moskit.

    due_date: string ISO 8601 (ex: "2026-05-11T09:00:00")
    """
    payload = {
        "title": title,
        "type": activity_type,
        "dueDate": due_date,
        "dealId": deal_id,
        "contactId": contact_id,
        "status": "open",
    }
    if responsible_id:
        payload["responsibleId"] = responsible_id

    return _post("/activities", payload)


# ── Deals (Negócios) ───────────────────────────────────────────────────────────

def get_deal(deal_id: str) -> Optional[dict]:
    """Busca detalhes de um negócio pelo ID."""
    return _get(f"/deals/{deal_id}")


def get_deal_activities(deal_id: str, limit: int = 10) -> list[dict]:
    """Retorna as últimas atividades/notas de um negócio (contexto histórico)."""
    data = _get(f"/deals/{deal_id}/activities", params={"pageSize": limit})
    if data is None:
        return []
    if isinstance(data, dict):
        return data.get("data", data.get("activities", []))
    return data if isinstance(data, list) else []


def add_deal_note(deal_id: str, note: str) -> Optional[dict]:
    """Adiciona uma anotação/nota ao negócio."""
    # Alguns endpoints usam /notes, outros /annotations — tentamos os dois
    result = _post(f"/deals/{deal_id}/notes", {"content": note})
    if result is None:
        result = _post(f"/deals/{deal_id}/annotations", {"text": note})
    return result


# ── Contatos ───────────────────────────────────────────────────────────────────

def get_contact(contact_id: str) -> Optional[dict]:
    """Busca detalhes de um contato pelo ID."""
    return _get(f"/contacts/{contact_id}")


def find_contact_by_phone(phone: str) -> Optional[dict]:
    """
    Busca um contato pelo número de telefone.
    Tenta variações de formato (com/sem DDD, com/sem 9).
    """
    clean = _clean_phone(phone)

    # Tenta busca direta por telefone
    data = _get("/contacts", params={"phone": clean, "pageSize": 5})
    contacts = []
    if isinstance(data, dict):
        contacts = data.get("data", data.get("contacts", []))
    elif isinstance(data, list):
        contacts = data

    if contacts:
        return contacts[0]

    # Tenta sem o código do país (55)
    if clean.startswith("55") and len(clean) > 11:
        return find_contact_by_phone(clean[2:])

    return None


def get_active_deal_for_contact(contact_id: str) -> Optional[dict]:
    """
    Retorna o negócio ativo mais recente de um contato.
    Prioriza deals em aberto (status != won/lost).
    """
    data = _get(f"/contacts/{contact_id}/deals", params={"pageSize": 10})
    deals = []
    if isinstance(data, dict):
        deals = data.get("data", data.get("deals", []))
    elif isinstance(data, list):
        deals = data

    # Filtra deals abertos
    open_deals = [d for d in deals if d.get("status") not in ("won", "lost", "deleted")]
    if open_deals:
        return open_deals[0]

    # Se não houver aberto, retorna o mais recente
    return deals[0] if deals else None


# ── Utilitários ────────────────────────────────────────────────────────────────

def extract_phone_from_contact(contact: dict) -> Optional[str]:
    """
    Extrai o número de WhatsApp/celular de um contato Moskit.
    Prioriza campos marcados como WhatsApp ou celular.
    """
    phones = contact.get("phones", [])
    if not phones:
        # Tenta campo direto
        raw = contact.get("phone") or contact.get("mobile") or contact.get("whatsapp")
        return _clean_phone(raw) if raw else None

    # Prioridade: whatsapp > mobile > work > first available
    priority = {"whatsapp": 0, "mobile": 1, "celular": 1, "work": 2}
    phones_sorted = sorted(
        phones,
        key=lambda p: priority.get(str(p.get("type", "")).lower(), 9),
    )
    phone_value = phones_sorted[0].get("phone") or phones_sorted[0].get("number", "")
    return _clean_phone(phone_value) if phone_value else None


def summarize_deal_for_context(deal: dict) -> str:
    """Gera um resumo textual do deal para incluir no prompt da IA."""
    name = deal.get("name", "Negócio sem nome")
    stage = deal.get("stage", {})
    stage_name = stage.get("name", "") if isinstance(stage, dict) else str(stage)
    value = deal.get("value", 0) or deal.get("price", 0)
    status = deal.get("status", "open")
    tags = ", ".join(deal.get("tags", []) or [])

    lines = [
        f"Negócio: {name}",
        f"Etapa: {stage_name}",
        f"Valor: R$ {value:,.2f}" if value else "Valor: não informado",
        f"Status: {status}",
    ]
    if tags:
        lines.append(f"Tags: {tags}")

    notes = deal.get("notes", "") or deal.get("description", "")
    if notes:
        lines.append(f"Observações do deal: {notes[:300]}")

    return "\n".join(lines)
