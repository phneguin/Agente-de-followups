"""
main.py — Servidor FastAPI do agente de follow-up (mini-CRM próprio).

Endpoints:
  GET  /                    → serve a interface web
  GET  /api/stats           → estatísticas do dashboard
  GET  /api/clients         → lista todos os clientes
  POST /api/clients         → cria cliente
  GET  /api/clients/{id}    → detalhe do cliente
  PUT  /api/clients/{id}    → atualiza cliente
  DELETE /api/clients/{id}  → remove cliente
  GET  /api/clients/{id}/messages   → histórico de mensagens
  GET  /api/clients/{id}/followups  → histórico de follow-ups
  POST /api/clients/{id}/followups  → agenda follow-up manual
  GET  /api/reports         → log de atividades (para Moskit)
  POST /webhook             → webhook Z-API (mensagens recebidas)
  POST /run-now             → disparo manual de follow-ups
"""

import logging
import os
from contextlib import asynccontextmanager
from datetime import datetime

from fastapi import FastAPI, Request, HTTPException, Header
from fastapi.responses import JSONResponse, FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from typing import Optional

import database as db
import zapi_client as zapi
import ai_agent as agent
import email_notifier as email
from scheduler import create_scheduler, run_followups
from config import settings

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
)
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("🚀 Iniciando agente de follow-up (mini-CRM)...")
    db.init_db()
    scheduler = create_scheduler()
    scheduler.start()
    logger.info("⏰ Scheduler iniciado — varredura a cada %d minutos.",
                settings.SCHEDULER_INTERVAL_MINUTES)
    yield
    scheduler.shutdown(wait=False)
    logger.info("🛑 Agente encerrado.")


app = FastAPI(
    title="Agente de Follow-up",
    version="2.0.0",
    lifespan=lifespan,
)

# Serve arquivos estáticos
static_dir = os.path.join(os.path.dirname(__file__), "static")
if os.path.exists(static_dir):
    app.mount("/static", StaticFiles(directory=static_dir), name="static")


# ── Modelos Pydantic ──────────────────────────────────────────────────────────

class ClientCreate(BaseModel):
    name: str
    phone: str
    stage: str = "em_contato"
    value: float = 0
    notes: str = ""

class ClientUpdate(BaseModel):
    name: Optional[str] = None
    phone: Optional[str] = None
    stage: Optional[str] = None
    value: Optional[float] = None
    notes: Optional[str] = None

class FollowupCreate(BaseModel):
    scheduled_at: str
    title: str = "Follow-up"
    ai_notes: str = ""


# ── Interface web ─────────────────────────────────────────────────────────────

@app.get("/")
async def index():
    html_path = os.path.join(static_dir, "index.html")
    if os.path.exists(html_path):
        return FileResponse(html_path)
    return JSONResponse({"status": "ok", "version": "2.0.0"})


# ── Stats ─────────────────────────────────────────────────────────────────────

@app.get("/api/stats")
async def get_stats():
    return db.get_stats()


# ── Clientes ──────────────────────────────────────────────────────────────────

@app.get("/api/clients")
async def list_clients():
    return db.get_all_clients()


@app.post("/api/clients", status_code=201)
async def create_client(body: ClientCreate):
    try:
        crm_client = db.create_client(
            name=body.name, phone=body.phone,
            stage=body.stage, value=body.value, notes=body.notes,
        )
        # Cria primeiro follow-up para amanhã às 9h
        import pytz
        from datetime import timedelta
        brt = pytz.timezone("America/Sao_Paulo")
        tomorrow = (datetime.now(brt) + timedelta(days=1)).replace(
            hour=9, minute=0, second=0, microsecond=0
        )
        db.create_followup(
            client_id=crm_client["id"],
            scheduled_at=tomorrow.strftime("%Y-%m-%d %H:%M:%S"),
            title=f"Primeiro contato — {body.name}",
            ai_notes="Follow-up inicial criado ao cadastrar o cliente.",
        )
        return crm_client
    except Exception as e:
        if "UNIQUE constraint" in str(e):
            raise HTTPException(400, "Já existe um cliente com esse número.")
        raise HTTPException(500, str(e))


@app.get("/api/clients/{client_id}")
async def get_client(client_id: int):
    crm_client = db.get_client(client_id)
    if not crm_client:
        raise HTTPException(404, "Cliente não encontrado.")
    crm_client["messages"]     = db.get_client_messages(client_id, limit=50)
    crm_client["followups"]    = db.get_client_followups(client_id)
    crm_client["activity_log"] = db.get_activity_log(client_id, limit=30)
    return crm_client


@app.put("/api/clients/{client_id}")
async def update_client(client_id: int, body: ClientUpdate):
    updates = {k: v for k, v in body.dict().items() if v is not None}
    crm_client = db.update_client(client_id, **updates)
    if not crm_client:
        raise HTTPException(404, "Cliente não encontrado.")
    return crm_client


@app.delete("/api/clients/{client_id}")
async def delete_client(client_id: int):
    if not db.delete_client(client_id):
        raise HTTPException(404, "Cliente não encontrado.")
    return {"status": "deleted"}


# ── Mensagens ─────────────────────────────────────────────────────────────────

@app.get("/api/clients/{client_id}/messages")
async def get_messages(client_id: int):
    crm_client = db.get_client(client_id)
    if not crm_client:
        raise HTTPException(404, "Cliente não encontrado.")
    return db.get_client_messages(client_id)


# ── Follow-ups ────────────────────────────────────────────────────────────────

@app.get("/api/clients/{client_id}/followups")
async def get_followups(client_id: int):
    crm_client = db.get_client(client_id)
    if not crm_client:
        raise HTTPException(404, "Cliente não encontrado.")
    return db.get_client_followups(client_id)


@app.post("/api/clients/{client_id}/followups", status_code=201)
async def schedule_followup(client_id: int, body: FollowupCreate):
    crm_client = db.get_client(client_id)
    if not crm_client:
        raise HTTPException(404, "Cliente não encontrado.")
    return db.create_followup(
        client_id=client_id,
        scheduled_at=body.scheduled_at,
        title=body.title,
        ai_notes=body.ai_notes,
    )


# ── Relatórios ────────────────────────────────────────────────────────────────

@app.get("/api/reports")
async def get_reports(client_id: Optional[int] = None, limit: int = 100):
    return db.get_activity_log(client_id=client_id, limit=limit)


# ── Webhook Z-API ─────────────────────────────────────────────────────────────

@app.post("/webhook")
async def webhook(request: Request):
    client_token = (request.headers.get("client-token")
                    or request.headers.get("Client-Token"))
    if (client_token and settings.ZAPI_CLIENT_TOKEN
            and client_token != settings.ZAPI_CLIENT_TOKEN):
        raise HTTPException(401, "Unauthorized")

    payload = await request.json()
    msg = zapi.parse_webhook_payload(payload)
    if not msg:
        return JSONResponse({"status": "ignored"})

    phone       = msg["phone"]
    text        = msg["text"]
    sender_name = msg["name"]

    logger.info("📥 Mensagem de %s (%s): %s", sender_name, phone, text[:80])

    crm_client = db.get_client_by_phone(phone)
    if not crm_client:
        logger.info("Número %s não cadastrado no CRM — ignorando.", phone)
        return JSONResponse({"status": "unknown_contact"})

    client_id    = crm_client["id"]
    contact_name = crm_client["name"]
    stage        = crm_client["stage"]
    notes        = crm_client.get("notes") or ""
    value        = crm_client.get("value") or 0

    db.save_message(phone=phone, direction="inbound",
                    content=text, client_id=client_id)

    wa_history = db.get_conversation_history(phone, limit=settings.CONTEXT_HISTORY_LIMIT)
    if wa_history and wa_history[-1]["direction"] == "inbound":
        wa_history = wa_history[:-1]

    response = agent.generate_reply(
        incoming_message=text,
        contact_name=contact_name,
        client_notes=notes,
        stage=stage,
        value=value,
        whatsapp_history=wa_history,
    )

    moskit_note = agent.generate_moskit_note(
        contact_name=contact_name, stage=stage,
        ai_message=response.message if not response.should_escalate else "",
        client_response=text,
        action_type="escalated" if response.should_escalate else "reply_sent",
        next_followup_days=response.next_followup_days,
        next_followup_reason=response.next_followup_reason,
    )

    if response.should_escalate or not response.message:
        reason = response.escalate_reason or "IA optou por escalar"
        alert  = agent.build_escalation_alert(
            contact_name=contact_name, phone=phone,
            reason=reason, last_message=text, stage=stage,
        )
        zapi.send_text(settings.PEDRO_ALERT_PHONE, alert, simulate_typing=False)
        db.log_activity(client_id=client_id, action_type="escalated",
                        description=reason, client_response=text,
                        moskit_note=moskit_note)
        email.notify_escalation(client_name=contact_name, phone=phone,
                                stage=stage, reason=reason, last_message=text)
        return JSONResponse({"status": "escalated", "reason": reason})

    sent = zapi.send_text(phone, response.message)
    if not sent:
        return JSONResponse({"status": "send_failed"}, status_code=500)

    db.save_message(phone=phone, direction="outbound",
                    content=response.message, client_id=client_id)
    db.log_activity(client_id=client_id, action_type="reply_sent",
                    description=f"Resposta para {contact_name}",
                    ai_message=response.message, client_response=text,
                    moskit_note=moskit_note)

    if response.next_followup_days > 0:
        import pytz
        from datetime import timedelta
        brt    = pytz.timezone("America/Sao_Paulo")
        next_dt = (datetime.now(brt) + timedelta(days=response.next_followup_days))
        next_dt = next_dt.replace(hour=9, minute=0, second=0, microsecond=0)
        db.create_followup(
            client_id=client_id,
            scheduled_at=next_dt.strftime("%Y-%m-%d %H:%M:%S"),
            title=f"Follow-up — {contact_name}",
            ai_notes=response.next_followup_reason,
        )

    email.notify_reply_received(
        client_name=contact_name, phone=phone, stage=stage,
        client_message=text, ai_response=response.message,
        moskit_note=moskit_note,
    )

    logger.info("✅ Resposta enviada para %s.", contact_name)
    return JSONResponse({"status": "replied"})


# ── Trigger manual ────────────────────────────────────────────────────────────

@app.post("/run-now")
async def run_now(x_admin_token: str = Header(default="")):
    if x_admin_token != settings.ZAPI_CLIENT_TOKEN:
        raise HTTPException(401, "Unauthorized")
    run_followups()
    return JSONResponse({"status": "done"})


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=settings.PORT, reload=False)
