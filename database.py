"""
database.py — Banco SQLite local para histórico de conversas e log de envios.

O banco persiste entre reinicializações e serve como memória de curto prazo
do agente: evita reenvios duplicados e fornece contexto da conversa para a IA.
"""

import sqlite3
import os
from datetime import datetime
from typing import Optional

DB_PATH = os.environ.get("DB_PATH", "followup_agent.db")


def get_connection() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    """Cria as tabelas se ainda não existirem."""
    conn = get_connection()
    cursor = conn.cursor()

    cursor.executescript("""
        -- Histórico de mensagens trocadas com cada contato
        CREATE TABLE IF NOT EXISTS messages (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            phone           TEXT NOT NULL,
            direction       TEXT NOT NULL CHECK(direction IN ('outbound', 'inbound')),
            content         TEXT NOT NULL,
            sent_at         DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
            moskit_deal_id  TEXT,
            moskit_note_id  TEXT,
            activity_id     TEXT
        );

        -- Controle de atividades do Moskit já processadas (evita duplicatas)
        CREATE TABLE IF NOT EXISTS processed_activities (
            activity_id     TEXT PRIMARY KEY,
            processed_at    DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
            phone           TEXT,
            outcome         TEXT    -- 'sent', 'skipped_no_phone', 'error'
        );

        -- Índices para buscas frequentes
        CREATE INDEX IF NOT EXISTS idx_messages_phone
            ON messages(phone, sent_at DESC);

        CREATE INDEX IF NOT EXISTS idx_messages_deal
            ON messages(moskit_deal_id, sent_at DESC);
    """)

    conn.commit()
    conn.close()


# ── Mensagens ─────────────────────────────────────────────────────────────────

def save_message(
    phone: str,
    direction: str,
    content: str,
    deal_id: Optional[str] = None,
    activity_id: Optional[str] = None,
) -> int:
    """Salva uma mensagem enviada ou recebida. Retorna o ID inserido."""
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute(
        """INSERT INTO messages (phone, direction, content, moskit_deal_id, activity_id)
           VALUES (?, ?, ?, ?, ?)""",
        (phone, direction, content, deal_id, activity_id),
    )
    row_id = cursor.lastrowid
    conn.commit()
    conn.close()
    return row_id


def get_conversation_history(phone: str, limit: int = 10) -> list[dict]:
    """Retorna as últimas `limit` mensagens de um número (mais antigas primeiro)."""
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute(
        """SELECT direction, content, sent_at
           FROM messages
           WHERE phone = ?
           ORDER BY sent_at DESC
           LIMIT ?""",
        (phone, limit),
    )
    rows = [dict(r) for r in cursor.fetchall()]
    conn.close()
    # Inverte para ordem cronológica
    return list(reversed(rows))


# ── Atividades processadas ────────────────────────────────────────────────────

def mark_activity_processed(
    activity_id: str,
    phone: Optional[str] = None,
    outcome: str = "sent",
) -> None:
    conn = get_connection()
    conn.execute(
        """INSERT OR REPLACE INTO processed_activities (activity_id, phone, outcome)
           VALUES (?, ?, ?)""",
        (activity_id, phone, outcome),
    )
    conn.commit()
    conn.close()


def is_activity_processed(activity_id: str) -> bool:
    conn = get_connection()
    row = conn.execute(
        "SELECT 1 FROM processed_activities WHERE activity_id = ?",
        (activity_id,),
    ).fetchone()
    conn.close()
    return row is not None
