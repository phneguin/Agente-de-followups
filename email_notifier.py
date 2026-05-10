"""
email_notifier.py — Notificações por e-mail para o Pedro.

Usa Gmail SMTP (ou qualquer SMTP). Requer SMTP_USER e SMTP_PASS no .env.

Para usar com Gmail:
  1. Ative 2 fatores na sua conta Google
  2. Acesse: myaccount.google.com → Segurança → Senhas de app
  3. Gere uma senha de app e use como SMTP_PASS
"""

import smtplib
import logging
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from datetime import datetime

from config import settings

logger = logging.getLogger(__name__)

# Cor principal das notificações
PURPLE = "#7C3AED"
GREEN  = "#10B981"
RED    = "#EF4444"
AMBER  = "#F59E0B"


def _send(subject: str, html: str) -> bool:
    if not settings.SMTP_USER or not settings.SMTP_PASS:
        logger.warning("E-mail não configurado — notificação pulada.")
        return False
    try:
        msg = MIMEMultipart("alternative")
        msg["Subject"] = subject
        msg["From"]    = f"Agente Follow-up <{settings.SMTP_USER}>"
        msg["To"]      = settings.NOTIFICATION_EMAIL
        msg.attach(MIMEText(html, "html", "utf-8"))

        with smtplib.SMTP(settings.SMTP_HOST, settings.SMTP_PORT) as srv:
            srv.starttls()
            srv.login(settings.SMTP_USER, settings.SMTP_PASS)
            srv.send_message(msg)

        logger.info("📧 E-mail enviado: %s", subject)
        return True
    except Exception as e:
        logger.error("Falha ao enviar e-mail: %s", e)
        return False


def _base_template(color: str, icon: str, title: str, body_html: str) -> str:
    now = datetime.now().strftime("%d/%m/%Y às %H:%M")
    return f"""
    <div style="font-family: Arial, sans-serif; max-width: 620px; margin: 0 auto; background: #F8FAFC; padding: 16px;">
      <div style="background:{color}; color:white; padding:20px 24px; border-radius:10px 10px 0 0;">
        <h2 style="margin:0; font-size:18px;">{icon} {title}</h2>
        <p style="margin:4px 0 0; font-size:12px; opacity:.85;">{now}</p>
      </div>
      <div style="background:#fff; padding:24px; border-radius:0 0 10px 10px; border:1px solid #E2E8F0; border-top:none;">
        {body_html}
        <hr style="border:none; border-top:1px solid #E2E8F0; margin:20px 0 12px;">
        <p style="color:#94A3B8; font-size:11px; margin:0;">
          Agente de Follow-up — {settings.COMPANY_NAME}
        </p>
      </div>
    </div>
    """


def _row(label: str, value: str) -> str:
    return f"""
    <tr>
      <td style="padding:6px 12px 6px 0; font-weight:600; color:#475569;
                 font-size:13px; white-space:nowrap; vertical-align:top;">{label}</td>
      <td style="padding:6px 0; font-size:13px; color:#1E293B;">{value}</td>
    </tr>"""


def _box(color: str, label: str, text: str) -> str:
    return f"""
    <div style="margin:14px 0; padding:14px 16px; background:#F8FAFC;
                border-left:4px solid {color}; border-radius:4px;">
      <strong style="font-size:12px; color:{color}; text-transform:uppercase;
                     letter-spacing:.5px;">{label}</strong>
      <p style="margin:6px 0 0; font-size:13px; color:#334155;
                white-space:pre-wrap; line-height:1.5;">{text}</p>
    </div>"""


# ── Notificações ───────────────────────────────────────────────────────────────

def notify_followup_sent(
    client_name: str,
    phone: str,
    stage: str,
    message_sent: str,
    next_followup_days: int,
    next_followup_reason: str,
    moskit_note: str,
) -> bool:
    stage_label = "Em Contato" if stage == "em_contato" else "Em Negociação"
    next_label  = f"em {next_followup_days} dia(s)" if next_followup_days else "a definir"

    body = f"""
    <table style="width:100%; border-collapse:collapse;">
      {_row("Cliente:", client_name)}
      {_row("WhatsApp:", phone)}
      {_row("Etapa:", stage_label)}
      {_row("Próx. contato:", next_label)}
    </table>
    {_box(PURPLE, "💬 Mensagem enviada", message_sent)}
    {_box(GREEN,  "⏭️ Motivo do prazo",  next_followup_reason or "—")}
    {_box(AMBER,  "📋 Nota para o Moskit — copie e cole", moskit_note)}
    """
    html = _base_template(PURPLE, "✅", f"Follow-up enviado — {client_name}", body)
    return _send(f"✅ Follow-up enviado — {client_name}", html)


def notify_reply_received(
    client_name: str,
    phone: str,
    stage: str,
    client_message: str,
    ai_response: str,
    moskit_note: str,
) -> bool:
    stage_label = "Em Contato" if stage == "em_contato" else "Em Negociação"

    body = f"""
    <table style="width:100%; border-collapse:collapse;">
      {_row("Cliente:", client_name)}
      {_row("WhatsApp:", phone)}
      {_row("Etapa:", stage_label)}
    </table>
    {_box("#2563EB", "📱 Mensagem do cliente", client_message)}
    {_box(PURPLE,   "🤖 Resposta enviada pela IA", ai_response)}
    {_box(AMBER,    "📋 Nota para o Moskit — copie e cole", moskit_note)}
    """
    html = _base_template(GREEN, "💬", f"Nova resposta — {client_name}", body)
    return _send(f"💬 Resposta recebida — {client_name}", html)


def notify_escalation(
    client_name: str,
    phone: str,
    stage: str,
    reason: str,
    last_message: str,
) -> bool:
    stage_label = "Em Contato" if stage == "em_contato" else "Em Negociação"

    body = f"""
    <table style="width:100%; border-collapse:collapse;">
      {_row("Cliente:", client_name)}
      {_row("WhatsApp:", phone)}
      {_row("Etapa:", stage_label)}
      {_row("Motivo:", f'<span style="color:{RED}; font-weight:600;">{reason}</span>')}
    </table>
    {_box(RED, "💬 Última mensagem do cliente", last_message)}
    <p style="font-size:13px; color:#475569; margin-top:16px;">
      ⚡ A automação foi pausada para este contato. Responda diretamente pelo WhatsApp.
    </p>
    """
    html = _base_template(RED, "🚨", f"Atenção necessária — {client_name}", body)
    return _send(f"🚨 ATENÇÃO — Escalado: {client_name}", html)
