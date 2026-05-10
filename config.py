"""
config.py — Variáveis de ambiente do agente de follow-up.
Todas as credenciais ficam no .env (nunca no código).
"""

from pydantic_settings import BaseSettings
from functools import lru_cache


class Settings(BaseSettings):
    # ── Z-API (WhatsApp) ──────────────────────────────────────────────────────
    ZAPI_INSTANCE_ID: str
    ZAPI_TOKEN: str
    ZAPI_CLIENT_TOKEN: str

    # ── Anthropic (Claude) ────────────────────────────────────────────────────
    ANTHROPIC_API_KEY: str

    # ── Alertas WhatsApp ──────────────────────────────────────────────────────
    # Número do Pedro para alertas de escalada (formato: 5531988706010)
    PEDRO_ALERT_PHONE: str

    # ── Identidade ────────────────────────────────────────────────────────────
    SELLER_NAME: str = "Pedro"
    COMPANY_NAME: str = "Metalsol Serviços de Energia Solar"

    # ── E-mail (Gmail SMTP) ────────────────────────────────────────────────────
    # Para ativar notificações por e-mail:
    # 1. Ative a verificação em 2 etapas no Google
    # 2. Acesse myaccount.google.com → Segurança → Senhas de app
    # 3. Gere uma senha de 16 caracteres e use como SMTP_PASS
    SMTP_HOST: str = "smtp.gmail.com"
    SMTP_PORT: int = 587
    SMTP_USER: str = ""           # ex: phpereiradasilva@gmail.com
    SMTP_PASS: str = ""           # Senha de App do Google (16 chars)
    NOTIFICATION_EMAIL: str = ""  # Endereço que receberá as notificações

    # ── Horário comercial (Brasília) ───────────────────────────────────────────
    BUSINESS_HOUR_START: int = 8
    BUSINESS_HOUR_END: int = 18

    # ── Scheduler ─────────────────────────────────────────────────────────────
    SCHEDULER_INTERVAL_MINUTES: int = 30
    MAX_FOLLOWUPS_PER_RUN: int = 30
    CONTEXT_HISTORY_LIMIT: int = 8

    # ── Servidor ──────────────────────────────────────────────────────────────
    PORT: int = 8000

    # ── Legado Moskit (mantido para não quebrar .env existente) ──────────────
    MOSKIT_API_KEY: str = ""
    MOSKIT_USER_ID: str = ""
    AUTO_SCHEDULE_NEXT: bool = True
    NEXT_FOLLOWUP_DAYS: int = 3

    class Config:
        env_file = ".env"
        env_file_encoding = "utf-8"


@lru_cache()
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
