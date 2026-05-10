"""
scheduler.py — Varredura periódica do banco local para follow-ups.

A cada N minutos (SCHEDULER_INTERVAL_MINUTES):
  1. Busca follow-ups pendentes cujo horário chegou
  2. Para cada um:
     a. Gera mensagem via IA
     b. Envia via Z-API
     c. Agenda o próximo follow-up (prazo sugerido pela IA)
     d. Loga a ação
     e. Envia e-mail de notificação para o Pedro
"""

import logging
from datetime import datetime, timedelta

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.interval import IntervalTrigger
import pytz

import database as db
import zapi_client as zapi
import ai_agent as agent
import email_notifier as email
from config import settings

logger = logging.getLogger(__name__)

BRASILIA_TZ = pytz.timezone("America/Sao_Paulo")


def _is_business_hours() -> bool:
    now = datetime.now(BRASILIA_TZ)
    return settings.BUSINESS_HOUR_START <= now.hour < settings.BUSINESS_HOUR_END


def _next_followup_datetime(days: int) -> str:
    """Retorna string ISO para o próximo follow-up, sempre às 9h."""
    next_dt = datetime.now(BRASILIA_TZ) + timedelta(days=max(days, 1))
    next_dt = next_dt.replace(hour=9, minute=0, second=0, microsecond=0)
    return next_dt.strftime("%Y-%m-%d %H:%M:%S")


def run_followups() -> None:
    """Processa todos os follow-ups pendentes cujo horário chegou."""
    if not _is_business_hours():
        logger.debug("Fora do horário comercial — scheduler pausado.")
        return

    logger.info("=== Iniciando rodada de follow-ups ===")

    pending = db.get_pending_followups()
    logger.info("Follow-ups pendentes: %d", len(pending))

    processed = 0
    for fu in pending:
        if processed >= settings.MAX_FOLLOWUPS_PER_RUN:
            logger.info("Limite de %d atingido.", settings.MAX_FOLLOWUPS_PER_RUN)
            break

        client_id = fu["client_id"]
        followup_id = fu["id"]
        phone = fu["phone"]
        contact_name = fu["client_name"]
        stage = fu["stage"]
        notes = fu["client_notes"] or ""
        value = fu.get("value") or 0

        logger.info("Processando follow-up %d para %s (%s)", followup_id, contact_name, phone)

        # Histórico de conversa
        wa_history = db.get_conversation_history(phone, limit=settings.CONTEXT_HISTORY_LIMIT)

        # Gera mensagem via IA
        response = agent.generate_followup_message(
            contact_name=contact_name,
            client_notes=notes,
            stage=stage,
            value=value,
            whatsapp_history=wa_history,
        )

        # Marca follow-up como concluído independente do resultado
        db.complete_followup(followup_id)

        # Tratamento de escalada
        if response.should_escalate or not response.message:
            reason = response.escalate_reason or "IA optou por não enviar automaticamente"
            logger.info("Escalando follow-up %d: %s", followup_id, reason)

            alert = agent.build_escalation_alert(
                contact_name=contact_name,
                phone=phone,
                reason=reason,
                last_message=fu.get("title", "Follow-up"),
                stage=stage,
            )
            zapi.send_text(settings.PEDRO_ALERT_PHONE, alert, simulate_typing=False)

            moskit_note = agent.generate_moskit_note(
                contact_name=contact_name, stage=stage,
                ai_message="", client_response="",
                action_type="escalated",
                next_followup_days=0, next_followup_reason=reason,
            )
            db.log_activity(
                client_id=client_id, action_type="escalated",
                description=reason, moskit_note=moskit_note,
            )
            email.notify_escalation(
                client_name=contact_name, phone=phone, stage=stage,
                reason=reason, last_message=fu.get("title", ""),
            )
            continue

        # Envia mensagem ao cliente
        sent = zapi.send_text(phone, response.message)
        if not sent:
            logger.error("Falha ao enviar para %s (follow-up %d).", phone, followup_id)
            continue

        # Salva mensagem no banco
        db.save_message(phone=phone, direction="outbound",
                        content=response.message, client_id=client_id)

        # Gera nota para o Moskit
        moskit_note = agent.generate_moskit_note(
            contact_name=contact_name, stage=stage,
            ai_message=response.message, client_response="",
            action_type="followup_sent",
            next_followup_days=response.next_followup_days,
            next_followup_reason=response.next_followup_reason,
        )

        # Loga a ação
        db.log_activity(
            client_id=client_id, action_type="followup_sent",
            description=f"Follow-up enviado para {contact_name}",
            ai_message=response.message, moskit_note=moskit_note,
        )

        # Agenda próximo follow-up
        next_date = _next_followup_datetime(response.next_followup_days)
        db.create_followup(
            client_id=client_id,
            scheduled_at=next_date,
            title=f"Follow-up — {contact_name}",
            ai_notes=response.next_followup_reason,
        )
        logger.info("Próximo follow-up agendado para %s (%s).",
                    next_date[:10], response.next_followup_reason)

        # Notifica Pedro por e-mail
        email.notify_followup_sent(
            client_name=contact_name, phone=phone, stage=stage,
            message_sent=response.message,
            next_followup_days=response.next_followup_days,
            next_followup_reason=response.next_followup_reason,
            moskit_note=moskit_note,
        )

        processed += 1
        logger.info("✅ Follow-up enviado para %s (%s).", contact_name, phone)

    logger.info("=== Rodada concluída: %d enviados ===", processed)


def create_scheduler() -> BackgroundScheduler:
    scheduler = BackgroundScheduler(timezone=BRASILIA_TZ)
    scheduler.add_job(
        run_followups,
        trigger=IntervalTrigger(minutes=settings.SCHEDULER_INTERVAL_MINUTES),
        id="followup_job",
        name="Follow-up automático",
        replace_existing=True,
        max_instances=1,
    )
    return scheduler
