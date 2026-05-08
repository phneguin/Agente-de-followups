"""
config.py — Variáveis de ambiente do agente de follow-up.
Todas as credenciais ficam no .env (nunca no código).
"""

from pydantic_settings import BaseSettings
from functools import lru_cache


class Settings(BaseSettings):
    # ── Moskit CRM ────────────────────────────────────────────────────────────
    MOSKIT_API_KEY: str                  # Chave API do Moskit (Settings > Integrações)
    MOSKIT_USER_ID: str                  # Seu ID de usuário no Moskit

    # ── Z-API (WhatsApp) ──────────────────────────────────────────────────────
    ZAPI_INSTANCE_ID: str                # ID da instância Z-API
    ZAPI_TOKEN: str                      # Token da instância Z-API
    ZAPI_CLIENT_TOKEN: str               # Client-Token do header de segurança do webhook

    # ── Anthropic (Claude) ────────────────────────────────────────────────────
    ANTHROPIC_API_KEY: str               # Chave API da Anthropic

    # ── Configurações do agente ───────────────────────────────────────────────
    # Número de WhatsApp para alertas quando IA não sabe responder
    # Formato internacional sem + (ex: 5511999999999)
    PEDRO_ALERT_PHONE: str

    # Nome do vendedor — usado nos prompts da IA
    SELLER_NAME: str = "Pedro"

    # Nome da empresa — usado nos prompts
    COMPANY_NAME: str = "Metalsol Serviços de Energia Solar"

    # Horário de início/fim da janela de envio (24h, horário de Brasília)
    BUSINESS_HOUR_START: int = 8
    BUSINESS_HOUR_END: int = 18

    # Intervalo do scheduler em minutos (padrão: varre o Moskit a cada 60 min)
    SCHEDULER_INTERVAL_MINUTES: int = 60

    # Quantidade máxima de follow-ups por rodada (evita spam acidental)
    MAX_FOLLOWUPS_PER_RUN: int = 30

    # Quantas atividades/notas anteriores enviar como contexto para a IA
    CONTEXT_HISTORY_LIMIT: int = 8

    # Se True, a IA cria uma nova atividade de follow-up após concluir a atual
    AUTO_SCHEDULE_NEXT: bool = True

    # Dias para agendar o próximo follow-up automático
    NEXT_FOLLOWUP_DAYS: int = 3

    # ── Servidor ──────────────────────────────────────────────────────────────
    PORT: int = 8000

    class Config:
        env_file = ".env"
        env_file_encoding = "utf-8"


@lru_cache()
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
