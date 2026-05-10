"""
database.py — Banco SQLite do mini-CRM de follow-up.

Tabelas:
  - clients        : clientes cadastrados manualmente
  - follow_ups     : agenda de follow-ups
  - messages       : histórico de mensagens WhatsApp
  - activity_log   : log de todas as ações (para relatórios e Moskit)
"""

import sqlite3
import os
import logging
from contextlib import contextmanager
from typing import Optional

logger = logging.getLogger(__name__)

DB_PATH = os.environ.get("DB_PATH", "followup_agent.db")


@contextmanager
def get_conn():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_db() -> None:
    """Cria todas as tabelas se não existirem."""
    with get_conn() as conn:
        conn.executescript("""
        -- Clientes cadastrados manualmente pelo Pedro
        CREATE TABLE IF NOT EXISTS clients (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            name        TEXT NOT NULL,
            phone       TEXT NOT NULL UNIQUE,
            stage       TEXT NOT NULL DEFAULT 'em_contato'
                            CHECK(stage IN ('em_contato','em_negociacao')),
            value       REAL DEFAULT 0,
            notes       TEXT DEFAULT '',
            created_at  TEXT DEFAULT (datetime('now','localtime')),
            updated_at  TEXT DEFAULT (datetime('now','localtime'))
        );

        -- Agenda de follow-ups por cliente
        CREATE TABLE IF NOT EXISTS follow_ups (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            client_id       INTEGER NOT NULL,
            scheduled_at    TEXT NOT NULL,
            title           TEXT DEFAULT 'Follow-up',
            status          TEXT DEFAULT 'pending'
                                CHECK(status IN ('pending','done','cancelled')),
            ai_notes        TEXT DEFAULT '',
            created_at      TEXT DEFAULT (datetime('now','localtime')),
            FOREIGN KEY (client_id) REFERENCES clients(id) ON DELETE CASCADE
        );

        -- Histórico de mensagens trocadas via WhatsApp
        CREATE TABLE IF NOT EXISTS messages (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            phone       TEXT NOT NULL,
            direction   TEXT NOT NULL CHECK(direction IN ('outbound','inbound')),
            content     TEXT NOT NULL,
            client_id   INTEGER,
            sent_at     TEXT DEFAULT (datetime('now','localtime')),
            FOREIGN KEY (client_id) REFERENCES clients(id) ON DELETE SET NULL
        );

        -- Log de ações do agente (para relatórios e notas no Moskit)
        CREATE TABLE IF NOT EXISTS activity_log (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            client_id       INTEGER,
            action_type     TEXT NOT NULL,
            description     TEXT NOT NULL,
            ai_message      TEXT DEFAULT '',
            client_response TEXT DEFAULT '',
            moskit_note     TEXT DEFAULT '',
            created_at      TEXT DEFAULT (datetime('now','localtime')),
            FOREIGN KEY (client_id) REFERENCES clients(id) ON DELETE SET NULL
        );

        -- Índices
        CREATE INDEX IF NOT EXISTS idx_messages_phone
            ON messages(phone, sent_at DESC);
        CREATE INDEX IF NOT EXISTS idx_followups_client
            ON follow_ups(client_id, scheduled_at);
        CREATE INDEX IF NOT EXISTS idx_log_client
            ON activity_log(client_id, created_at DESC);
        """)
    logger.info("Banco de dados inicializado em %s", DB_PATH)


# ── Clients ───────────────────────────────────────────────────────────────────

def create_client(name: str, phone: str, stage: str = "em_contato",
                  value: float = 0, notes: str = "") -> dict:
    import re
    phone = re.sub(r"\D", "", phone)
    with get_conn() as conn:
        cursor = conn.execute(
            "INSERT INTO clients (name, phone, stage, value, notes) VALUES (?,?,?,?,?)",
            (name, phone, stage, value, notes)
        )
        return get_client(cursor.lastrowid)


def get_client(client_id: int) -> Optional[dict]:
    with get_conn() as conn:
        row = conn.execute("SELECT * FROM clients WHERE id = ?", (client_id,)).fetchone()
        return dict(row) if row else None


def get_client_by_phone(phone: str) -> Optional[dict]:
    import re
    clean = re.sub(r"\D", "", phone)
    with get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM clients WHERE phone = ?", (clean,)
        ).fetchone()
        if row:
            return dict(row)
        # Tenta sem código do país (55)
        if clean.startswith("55") and len(clean) > 11:
            row = conn.execute(
                "SELECT * FROM clients WHERE phone = ?", (clean[2:],)
            ).fetchone()
            return dict(row) if row else None
        return None


def get_all_clients() -> list[dict]:
    with get_conn() as conn:
        rows = conn.execute("""
            SELECT
                c.*,
                (SELECT COUNT(*) FROM follow_ups f
                 WHERE f.client_id = c.id AND f.status = 'pending') AS pending_followups,
                (SELECT MIN(scheduled_at) FROM follow_ups f
                 WHERE f.client_id = c.id AND f.status = 'pending') AS next_followup_at,
                (SELECT MAX(sent_at) FROM messages m
                 WHERE m.client_id = c.id) AS last_message_at
            FROM clients c
            ORDER BY c.updated_at DESC
        """).fetchall()
        return [dict(r) for r in rows]


def update_client(client_id: int, **kwargs) -> Optional[dict]:
    import re
    allowed = {"name", "phone", "stage", "value", "notes"}
    updates = {k: v for k, v in kwargs.items() if k in allowed}
    if not updates:
        return get_client(client_id)
    if "phone" in updates:
        updates["phone"] = re.sub(r"\D", "", updates["phone"])
    fields = ", ".join(f"{k} = ?" for k in updates)
    fields += ", updated_at = datetime('now','localtime')"
    values = list(updates.values()) + [client_id]
    with get_conn() as conn:
        conn.execute(f"UPDATE clients SET {fields} WHERE id = ?", values)
    return get_client(client_id)


def delete_client(client_id: int) -> bool:
    with get_conn() as conn:
        cur = conn.execute("DELETE FROM clients WHERE id = ?", (client_id,))
        return cur.rowcount > 0


# ── Follow-ups ────────────────────────────────────────────────────────────────

def create_followup(client_id: int, scheduled_at: str,
                    title: str = "Follow-up", ai_notes: str = "") -> dict:
    with get_conn() as conn:
        cursor = conn.execute(
            "INSERT INTO follow_ups (client_id, scheduled_at, title, ai_notes) VALUES (?,?,?,?)",
            (client_id, scheduled_at, title, ai_notes)
        )
        row = conn.execute(
            "SELECT * FROM follow_ups WHERE id = ?", (cursor.lastrowid,)
        ).fetchone()
        return dict(row)


def get_pending_followups() -> list[dict]:
    """Retorna follow-ups pendentes cujo horário já chegou."""
    with get_conn() as conn:
        rows = conn.execute("""
            SELECT f.*, c.name AS client_name, c.phone, c.stage,
                   c.notes AS client_notes, c.value
            FROM follow_ups f
            JOIN clients c ON f.client_id = c.id
            WHERE f.status = 'pending'
              AND f.scheduled_at <= datetime('now','localtime')
            ORDER BY f.scheduled_at ASC
        """).fetchall()
        return [dict(r) for r in rows]


def complete_followup(followup_id: int) -> None:
    with get_conn() as conn:
        conn.execute(
            "UPDATE follow_ups SET status = 'done' WHERE id = ?", (followup_id,)
        )


def get_client_followups(client_id: int) -> list[dict]:
    with get_conn() as conn:
        rows = conn.execute("""
            SELECT * FROM follow_ups WHERE client_id = ?
            ORDER BY scheduled_at DESC
        """, (client_id,)).fetchall()
        return [dict(r) for r in rows]


# ── Messages ──────────────────────────────────────────────────────────────────

def save_message(phone: str, direction: str, content: str,
                 client_id: Optional[int] = None) -> int:
    import re
    phone = re.sub(r"\D", "", phone)
    with get_conn() as conn:
        cursor = conn.execute(
            "INSERT INTO messages (phone, direction, content, client_id) VALUES (?,?,?,?)",
            (phone, direction, content, client_id)
        )
        return cursor.lastrowid


def get_conversation_history(phone: str, limit: int = 10) -> list[dict]:
    import re
    phone = re.sub(r"\D", "", phone)
    with get_conn() as conn:
        rows = conn.execute("""
            SELECT direction, content, sent_at FROM messages
            WHERE phone = ?
            ORDER BY sent_at DESC LIMIT ?
        """, (phone, limit)).fetchall()
        return list(reversed([dict(r) for r in rows]))


def get_client_messages(client_id: int, limit: int = 50) -> list[dict]:
    with get_conn() as conn:
        rows = conn.execute("""
            SELECT * FROM messages WHERE client_id = ?
            ORDER BY sent_at DESC LIMIT ?
        """, (client_id, limit)).fetchall()
        return list(reversed([dict(r) for r in rows]))


# ── Activity log ──────────────────────────────────────────────────────────────

def log_activity(client_id: int, action_type: str, description: str,
                 ai_message: str = "", client_response: str = "",
                 moskit_note: str = "") -> None:
    with get_conn() as conn:
        conn.execute("""
            INSERT INTO activity_log
                (client_id, action_type, description, ai_message, client_response, moskit_note)
            VALUES (?,?,?,?,?,?)
        """, (client_id, action_type, description, ai_message, client_response, moskit_note))


def get_activity_log(client_id: Optional[int] = None, limit: int = 100) -> list[dict]:
    with get_conn() as conn:
        if client_id:
            rows = conn.execute("""
                SELECT a.*, c.name AS client_name, c.phone
                FROM activity_log a
                LEFT JOIN clients c ON a.client_id = c.id
                WHERE a.client_id = ?
                ORDER BY a.created_at DESC LIMIT ?
            """, (client_id, limit)).fetchall()
        else:
            rows = conn.execute("""
                SELECT a.*, c.name AS client_name, c.phone
                FROM activity_log a
                LEFT JOIN clients c ON a.client_id = c.id
                ORDER BY a.created_at DESC LIMIT ?
            """, (limit,)).fetchall()
        return [dict(r) for r in rows]


def get_stats() -> dict:
    """Retorna estatísticas para o dashboard."""
    with get_conn() as conn:
        total = conn.execute("SELECT COUNT(*) FROM clients").fetchone()[0]
        em_contato = conn.execute(
            "SELECT COUNT(*) FROM clients WHERE stage = 'em_contato'"
        ).fetchone()[0]
        em_neg = conn.execute(
            "SELECT COUNT(*) FROM clients WHERE stage = 'em_negociacao'"
        ).fetchone()[0]
        pending = conn.execute(
            "SELECT COUNT(*) FROM follow_ups WHERE status = 'pending'"
        ).fetchone()[0]
        today_sent = conn.execute("""
            SELECT COUNT(*) FROM messages
            WHERE direction = 'outbound'
              AND DATE(sent_at) = DATE('now','localtime')
        """).fetchone()[0]
        return {
            "total_clients": total,
            "em_contato": em_contato,
            "em_negociacao": em_neg,
            "pending_followups": pending,
            "sent_today": today_sent,
        }
