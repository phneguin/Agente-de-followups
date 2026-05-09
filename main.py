"""
main.py — Servidor FastAPI do agente de follow-up.

Endpoints:
  GET  /         → health check
  POST /webhook  → recebe mensagens do WhatsApp (Z-API) e responde automaticamente
  POST /run-now  → dispara uma rodada de follow-ups manualmente (protegido por token)

Ao iniciar:
  - Cria o banco de dados SQLite
  - Inicia o scheduler de varredura do Moskit
"""

import logging
import os
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request, Header, HTTPException
from fastapi.responses import JSONResponse

import database as db
import moskit_client as moskit
import zapi_client as zapi
import ai_agent as agent
from scheduler import create_scheduler, run_followups
from config import settings

# ── Logging ────────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
)
logger = logging.getLogger(__name__)


# ── Lifespan (startup / shutdown) ─────────────────────────────────────────────
@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("🚀 Iniciando agente de follow-up...")
    db.init_db()
    scheduler = create_scheduler()
    scheduler.start()
    logger.info(
        "⏰ Scheduler iniciado — varredura a cada %d minutos.",
        settings.SCHEDULER_INTERVAL_MINUTES,
    )
    yield
    scheduler.shutdown(wait=False)
    logger.info("🛑 Agente encerrado.")


app = FastAPI(
    title="Agente de Follow-up",
    description="Automação de follow-up Moskit ↔ WhatsApp com IA",
    version="1.0.0",
    lifespan=lifespan,
)


# ── Health check ───────────────────────────────────────────────────────────────
@app.get("/")
async def health():
    return {"status": "ok", "agent": "follow-up", "version": "1.0.0"}


# ── Webhook Z-API ──────────────────────────────────────────────────────────────
@app.post("/webhook")
async def webhook(request: Request):
    """
    Recebe mensagens de entrada do WhatsApp via Z-API.

    A Z-API envia um POST para este endpoint sempre que uma mensagem
    for recebida na instância configurada.

    Segurança: a Z-API inclui o Client-Token no header; validamos aqui.
    """
    # Valida token de segurança do webhook (Z-API envia no header)
    client_token = request.headers.get("client-token") or request.headers.get("Client-Token")
    if client_token and settings.ZAPI_CLIENT_TOKEN and client_token != settings.ZAPI_CLIENT_TOKEN:
        logger.warning("Webhook recebido com token inválido.")
        raise HTTPException(status_code=401, detail="Unauthorized")

    payload = await request.json()
    logger.debug("Webhook recebido: %s", str(payload)[:200])

    # Parseia e filtra o payload
    msg = zapi.parse_webhook_payload(payload)
    if not msg:
        return JSONResponse({"status": "ignored"})

    phone = msg["phone"]
    text = msg["text"]
    contact_name = msg["name"]

    logger.info("📥 Mensagem recebida de %s (%s): %s", contact_name, phone, text[:80])

    # ── Busca contexto no Moskit ───────────────────────────────────────────────
    contact = moskit.find_contact_by_phone(phone)
    deal = None
    deal_id = ""
    deal_name = "Negócio não identificado"
    deal_summary = "Contato não encontrado no Moskit."

    if contact:
        contact_id = str(contact.get("id", ""))
        contact_name = contact.get("name") or contact.get("fullName") or contact_name
        deal = moskit.get_active_deal_for_contact(contact_id)
        if deal:
            responsible = deal.get("responsible") or deal.get("user") or {}
            responsible_id = str(
                responsible.get("id", "") if isinstance(responsible, dict)
                else responsible
            )
            if responsible_id and responsible_id != str(settings.MOSKIT_USER_ID):
                return JSONResponse({"status": "ignored_other_user"})

            deal_id = str(deal.get("id", ""))
            deal_name = deal.get("name", "Negócio")
            deal_summary = moskit.summarize_deal_for_context(deal)

    # ── Salva mensagem recebida no banco ───────────────────────────────────────
    db.save_message(
        phone=phone,
        direction="inbound",
        content=text,
        deal_id=deal_id or None,
    )

    # ── Registra mensagem no Moskit ────────────────────────────────────────────
    if deal_id:
        note_in = agent.build_moskit_note("inbound", text)
        moskit.add_deal_note(deal_id, note_in)

    # ── Gera resposta via IA ───────────────────────────────────────────────────
    wa_history = db.get_conversation_history(phone, limit=settings.CONTEXT_HISTORY_LIMIT)
    # Remove a mensagem recém-salva do histórico (a IA a recebe separadamente)
    if wa_history and wa_history[-1]["direction"] == "inbound":
        wa_history = wa_history[:-1]

    crm_history = moskit.get_deal_activities(deal_id, limit=settings.CONTEXT_HISTORY_LIMIT) if deal_id else []

    response = agent.generate_reply(
        incoming_message=text,
        contact_name=contact_name,
        deal_summary=deal_summary,
        whatsapp_history=wa_history,
        moskit_history=crm_history,
    )

    # ── Trata escalada ─────────────────────────────────────────────────────────
    if response.should_escalate or not response.message:
        reason = response.escalate_reason or "IA optou por escalar"
        logger.info("Escalando conversa de %s: %s", phone, reason)

        alert = agent.build_escalation_alert(
            contact_name=contact_name,
            phone=phone,
            reason=reason,
            last_message=text,
            deal_name=deal_name,
        )
        zapi.send_text(settings.PEDRO_ALERT_PHONE, alert, simulate_typing=False)

        if deal_id:
            moskit.add_deal_note(
                deal_id,
                agent.build_moskit_note("inbound", f"[ESCALADO] {reason}", was_escalated=True),
            )

        return JSONResponse({"status": "escalated", "reason": reason})

    # ── Envia resposta ao cliente ──────────────────────────────────────────────
    sent = zapi.send_text(phone, response.message)

    if sent:
        db.save_message(
            phone=phone,
            direction="outbound",
            content=response.message,
            deal_id=deal_id or None,
        )

        if deal_id:
            note_out = agent.build_moskit_note("outbound", response.message)
            moskit.add_deal_note(deal_id, note_out)

        logger.info("✅ Resposta enviada para %s (%s).", contact_name, phone)
        return JSONResponse({"status": "replied"})
    else:
        logger.error("Falha ao enviar resposta para %s.", phone)
        return JSONResponse({"status": "send_failed"}, status_code=500)


# ── Trigger manual ─────────────────────────────────────────────────────────────
@app.post("/run-now")
async def run_now(x_admin_token: str = Header(default="")):
    """
    Dispara uma rodada de follow-ups manualmente.
    Protegido pelo mesmo ZAPI_CLIENT_TOKEN para simplicidade.
    """
    if x_admin_token != settings.ZAPI_CLIENT_TOKEN:
        raise HTTPException(status_code=401, detail="Unauthorized")

    logger.info("Rodada manual de follow-ups disparada via /run-now")
    run_followups()
    return JSONResponse({"status": "done"})


# ── Entrypoint ─────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=settings.PORT, reload=False)
