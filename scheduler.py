"""
scheduler.py — Varredura periódica do Moskit para follow-ups proativos.

A cada N minutos (configurável em SCHEDULER_INTERVAL_MINUTES):
  1. Busca atividades pendentes do usuário no Moskit
  2. Para cada atividade com telefone disponível:
     a. Verifica se já foi processada
     b. Busca contexto: deal + contato + histórico
     c. Gera mensagem via IA
     d. Envia via Z-API
     e. Marca atividade como concluída no Moskit
     f. Registra nota no deal
     g. Opcionalmente cria próxima atividade de follow-up
"""

import logging
from datetime import datetime, timedelta

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.interval import IntervalTrigger
import pytz

import moskit_client as moskit
import zapi_client as zapi
import ai_agent as agent
import database as db
from config import settings

logger = logging.getLogger(__name__)

BRASILIA_TZ = pytz.timezone("America/Sao_Paulo")


def _is_business_hours() -> bool:
    """Verifica se está dentro da janela de horário comercial."""
    now = datetime.now(BRASILIA_TZ)
    return settings.BUSINESS_HOUR_START <= now.hour < settings.BUSINESS_HOUR_END


def _next_followup_date() -> str:
    """Calcula a data ISO para o próximo follow-up automático."""
    next_dt = datetime.now(BRASILIA_TZ) + timedelta(days=settings.NEXT_FOLLOWUP_DAYS)
    # Agenda para 9h no dia calculado
    next_dt = next_dt.replace(hour=9, minute=0, second=0, microsecond=0)
    return next_dt.isoformat()


def run_followups() -> None:
    """
    Função principal do scheduler: processa follow-ups pendentes.
    É chamada automaticamente pelo APScheduler no intervalo configurado.
    """
    if not _is_business_hours():
        logger.debug("Fora do horário comercial — scheduler pausado.")
        return

    logger.info("=== Iniciando rodada de follow-ups ===")

    activities = moskit.get_pending_activities(settings.MOSKIT_USER_ID)
    logger.info("Atividades pendentes encontradas: %d", len(activities))

    processed_count = 0
    skipped_count = 0

    for activity in activities:
        if processed_count >= settings.MAX_FOLLOWUPS_PER_RUN:
            logger.info("Limite de %d follow-ups por rodada atingido.", settings.MAX_FOLLOWUPS_PER_RUN)
            break

        activity_id = str(activity.get("id", ""))
        if not activity_id:
            continue

        # Evita reprocessar atividades já tratadas
        if db.is_activity_processed(activity_id):
            skipped_count += 1
            continue

        activity_title = activity.get("title") or activity.get("name") or "Follow-up"
        deal_id = str(activity.get("dealId") or activity.get("deal_id") or "")
        contact_id = str(activity.get("contactId") or activity.get("contact_id") or "")

        logger.info("Processando atividade %s: '%s'", activity_id, activity_title)

        # ── Busca contexto ─────────────────────────────────────────────────────
        deal = moskit.get_deal(deal_id) if deal_id else None
        contact = moskit.get_contact(contact_id) if contact_id else None

        if not contact:
            logger.warning("Atividade %s sem contato — pulando.", activity_id)
            db.mark_activity_processed(activity_id, outcome="skipped_no_contact")
            skipped_count += 1
            continue

        phone = moskit.extract_phone_from_contact(contact)
        if not phone:
            logger.warning(
                "Contato '%s' sem número de telefone — pulando atividade %s.",
                contact.get("name", "?"),
                activity_id,
            )
            db.mark_activity_processed(activity_id, phone=None, outcome="skipped_no_phone")
            skipped_count += 1
            continue

        contact_name = contact.get("name") or contact.get("fullName") or phone
        deal_summary = moskit.summarize_deal_for_context(deal) if deal else "Negócio não encontrado no CRM."
        deal_name = (deal.get("name") or "Negócio") if deal else "Negócio"

        # Histórico para contexto da IA
        wa_history = db.get_conversation_history(phone, limit=settings.CONTEXT_HISTORY_LIMIT)
        crm_history = moskit.get_deal_activities(deal_id, limit=settings.CONTEXT_HISTORY_LIMIT) if deal_id else []

        # ── Gera mensagem via IA ───────────────────────────────────────────────
        response = agent.generate_followup_message(
            activity_title=activity_title,
            deal_summary=deal_summary,
            contact_name=contact_name,
            whatsapp_history=wa_history,
            moskit_history=crm_history,
        )

        # ── Tratamento de escalada ─────────────────────────────────────────────
        if response.should_escalate or not response.message:
            reason = response.escalate_reason or "IA optou por não responder automaticamente"
            logger.info("Escalando atividade %s: %s", activity_id, reason)

            alert = agent.build_escalation_alert(
                contact_name=contact_name,
                phone=phone,
                reason=reason,
                last_message=activity_title,
                deal_name=deal_name,
            )
            zapi.send_text(settings.PEDRO_ALERT_PHONE, alert, simulate_typing=False)

            if deal_id:
                moskit.add_deal_note(
                    deal_id,
                    agent.build_moskit_note("outbound", f"[NÃO ENVIADO] {reason}", was_escalated=True),
                )

            db.mark_activity_processed(activity_id, phone=phone, outcome="escalated")
            skipped_count += 1
            continue

        # ── Envia mensagem ao cliente ──────────────────────────────────────────
        sent = zapi.send_text(phone, response.message)

        if not sent:
            logger.error("Falha ao enviar mensagem para %s (atividade %s).", phone, activity_id)
            db.mark_activity_processed(activity_id, phone=phone, outcome="error")
            continue

        # ── Registra no banco local ────────────────────────────────────────────
        db.save_message(
            phone=phone,
            direction="outbound",
            content=response.message,
            deal_id=deal_id,
            activity_id=activity_id,
        )

        # ── Atualiza Moskit ────────────────────────────────────────────────────
        # 1. Registra nota no deal
        if deal_id:
            note_text = agent.build_moskit_note("outbound", response.message)
            moskit.add_deal_note(deal_id, note_text)

        # 2. Conclui a atividade
        moskit.complete_activity(activity_id)

        # 3. Cria próximo follow-up (opcional)
        if settings.AUTO_SCHEDULE_NEXT and deal_id and contact_id:
            next_date = _next_followup_date()
            moskit.create_activity(
                deal_id=deal_id,
                contact_id=contact_id,
                title=f"Follow-up automático — {contact_name}",
                due_date=next_date,
                responsible_id=settings.MOSKIT_USER_ID,
            )
            logger.info("Próximo follow-up agendado para %s.", next_date[:10])

        db.mark_activity_processed(activity_id, phone=phone, outcome="sent")
        processed_count += 1
        logger.info(
            "✅ Follow-up enviado para %s (%s) — atividade %s concluída.",
            contact_name,
            phone,
            activity_id,
        )

    logger.info(
        "=== Rodada concluída: %d enviados, %d pulados ===",
        processed_count,
        skipped_count,
    )


# ── Setup do scheduler ─────────────────────────────────────────────────────────

def create_scheduler() -> BackgroundScheduler:
    """Cria e configura o APScheduler."""
    scheduler = BackgroundScheduler(timezone=BRASILIA_TZ)
    scheduler.add_job(
        run_followups,
        trigger=IntervalTrigger(minutes=settings.SCHEDULER_INTERVAL_MINUTES),
        id="followup_job",
        name="Follow-up Moskit → WhatsApp",
        replace_existing=True,
        max_instances=1,  # Evita sobreposição de execuções
    )
    return scheduler
