"""
database.py — Banco PostgreSQL persistente do mini-CRM de follow-up.

Usa DATABASE_URL injetado automaticamente pelo Railway ao adicionar PostgreSQL.
Dados persistem entre deploys e reinicializações — nunca somem.
"""

import os
import logging
from contextlib import contextmanager
from typing import Optional

import psycopg2
import psycopg2.extras

logger = logging.getLogger(__name__)

DATABASE_URL = os.environ.get("DATABASE_URL", "")


@contextmanager
def get_conn():
    conn = psycopg2.connect(DATABASE_URL)
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def _to_dict(cursor, row):
    if row is None:
        return None
    cols = [d[0] for d in cursor.description]
    return dict(zip(cols, row))


def _to_dicts(cursor, rows):
    cols = [d[0] for d in cursor.description]
    return [dict(zip(cols, r)) for r in rows]


def init_db() -> None:
    """Cria todas as tabelas se não existirem."""
    with get_conn() as conn:
        cur = conn.cursor()
        cur.execute("""
        CREATE TABLE IF NOT EXISTS clients (
            id          SERIAL PRIMARY KEY,
            name        TEXT NOT NULL,
            phone       TEXT NOT NULL UNIQUE,
            stage       TEXT NOT NULL DEFAULT 'em_contato'
                            CHECK(stage IN ('em_contato','em_negociacao')),
            value       REAL DEFAULT 0,
            notes       TEXT DEFAULT '',
            created_at  TIMESTAMP DEFAULT NOW(),
            updated_at  TIMESTAMP DEFAULT NOW()
        );

        CREATE TABLE IF NOT EXISTS follow_ups (
            id              SERIAL PRIMARY KEY,
            client_id       INTEGER NOT NULL REFERENCES clients(id) ON DELETE CASCADE,
            scheduled_at    TIMESTAMP NOT NULL,
            title           TEXT DEFAULT 'Follow-up',
            status          TEXT DEFAULT 'pending'
                                CHECK(status IN ('pending','done','cancelled')),
            ai_notes        TEXT DEFAULT '',
            created_at      TIMESTAMP DEFAULT NOW()
        );

        CREATE TABLE IF NOT EXISTS messages (
            id          SERIAL PRIMARY KEY,
            phone       TEXT NOT NULL,
            direction   TEXT NOT NULL CHECK(direction IN ('outbound','inbound')),
            content     TEXT NOT NULL,
            client_id   INTEGER REFERENCES clients(id) ON DELETE SET NULL,
            sent_at     TIMESTAMP DEFAULT NOW()
        );

        CREATE TABLE IF NOT EXISTS activity_log (
            id              SERIAL PRIMARY KEY,
            client_id       INTEGER REFERENCES clients(id) ON DELETE SET NULL,
            action_type     TEXT NOT NULL,
            description     TEXT NOT NULL,
            ai_message      TEXT DEFAULT '',
            client_response TEXT DEFAULT '',
            moskit_note     TEXT DEFAULT '',
            created_at      TIMESTAMP DEFAULT NOW()
        );
        """)
    logger.info("Banco PostgreSQL inicializado.")


# ── Clients ───────────────────────────────────────────────────────────────────

def create_client(name: str, phone: str, stage: str = "em_contato",
                  value: float = 0, notes: str = "") -> dict:
    import re
    phone = re.sub(r"\D", "", phone)
    with get_conn() as conn:
        cur = conn.cursor()
        cur.execute(
            "INSERT INTO clients (name, phone, stage, value, notes) VALUES (%s,%s,%s,%s,%s) RETURNING id",
            (name, phone, stage, value, notes)
        )
        new_id = cur.fetchone()[0]
    return get_client(new_id)


def get_client(client_id: int) -> Optional[dict]:
    with get_conn() as conn:
        cur = conn.cursor()
        cur.execute("SELECT * FROM clients WHERE id = %s", (client_id,))
        return _to_dict(cur, cur.fetchone())


def get_client_by_phone(phone: str) -> Optional[dict]:
    import re
    clean = re.sub(r"\D", "", phone)
    with get_conn() as conn:
        cur = conn.cursor()
        cur.execute("SELECT * FROM clients WHERE phone = %s", (clean,))
        row = cur.fetchone()
        if row:
            return _to_dict(cur, row)
        if clean.startswith("55") and len(clean) > 11:
            cur.execute("SELECT * FROM clients WHERE phone = %s", (clean[2:],))
            row = cur.fetchone()
            return _to_dict(cur, row)
        return None


def get_all_clients() -> list:
    with get_conn() as conn:
        cur = conn.cursor()
        cur.execute("""
            SELECT c.*,
                (SELECT COUNT(*) FROM follow_ups f
                 WHERE f.client_id = c.id AND f.status = 'pending') AS pending_followups,
                (SELECT MIN(scheduled_at) FROM follow_ups f
                 WHERE f.client_id = c.id AND f.status = 'pending') AS next_followup_at,
                (SELECT MAX(sent_at) FROM messages m
                 WHERE m.client_id = c.id) AS last_message_at
            FROM clients c
            ORDER BY c.updated_at DESC
        """)
        return _to_dicts(cur, cur.fetchall())


def update_client(client_id: int, **kwargs) -> Optional[dict]:
    import re
    allowed = {"name", "phone", "stage", "value", "notes"}
    updates = {k: v for k, v in kwargs.items() if k in allowed}
    if not updates:
        return get_client(client_id)
    if "phone" in updates:
        updates["phone"] = re.sub(r"\D", "", updates["phone"])
    fields = ", ".join(f"{k} = %s" for k in updates)
    fields += ", updated_at = NOW()"
    values = list(updates.values()) + [client_id]
    with get_conn() as conn:
        cur = conn.cursor()
        cur.execute(f"UPDATE clients SET {fields} WHERE id = %s", values)
    return get_client(client_id)


def delete_client(client_id: int) -> bool:
    with get_conn() as conn:
        cur = conn.cursor()
        cur.execute("DELETE FROM clients WHERE id = %s", (client_id,))
        return cur.rowcount > 0


# ── Follow-ups ────────────────────────────────────────────────────────────────

def create_followup(client_id: int, scheduled_at: str,
                    title: str = "Follow-up", ai_notes: str = "") -> dict:
    with get_conn() as conn:
        cur = conn.cursor()
        cur.execute(
            "INSERT INTO follow_ups (client_id, scheduled_at, title, ai_notes) VALUES (%s,%s,%s,%s) RETURNING id",
            (client_id, scheduled_at, title, ai_notes)
        )
        new_id = cur.fetchone()[0]
        cur.execute("SELECT * FROM follow_ups WHERE id = %s", (new_id,))
        return _to_dict(cur, cur.fetchone())


def get_pending_followups() -> list:
    with get_conn() as conn:
        cur = conn.cursor()
        cur.execute("""
            SELECT f.*, c.name AS client_name, c.phone, c.stage,
                   c.notes AS client_notes, c.value
            FROM follow_ups f
            JOIN clients c ON f.client_id = c.id
            WHERE f.status = 'pending'
              AND f.scheduled_at <= NOW()
            ORDER BY f.scheduled_at ASC
        """)
        return _to_dicts(cur, cur.fetchall())


def complete_followup(followup_id: int) -> None:
    with get_conn() as conn:
        cur = conn.cursor()
        cur.execute("UPDATE follow_ups SET status = 'done' WHERE id = %s", (followup_id,))


def get_client_followups(client_id: int) -> list:
    with get_conn() as conn:
        cur = conn.cursor()
        cur.execute(
            "SELECT * FROM follow_ups WHERE client_id = %s ORDER BY scheduled_at DESC",
            (client_id,)
        )
        return _to_dicts(cur, cur.fetchall())


# ── Messages ──────────────────────────────────────────────────────────────────

def save_message(phone: str, direction: str, content: str,
                 client_id: Optional[int] = None) -> int:
    import re
    phone = re.sub(r"\D", "", phone)
    with get_conn() as conn:
        cur = conn.cursor()
        cur.execute(
            "INSERT INTO messages (phone, direction, content, client_id) VALUES (%s,%s,%s,%s) RETURNING id",
            (phone, direction, content, client_id)
        )
        return cur.fetchone()[0]


def get_conversation_history(phone: str, limit: int = 10) -> list:
    import re
    phone = re.sub(r"\D", "", phone)
    with get_conn() as conn:
        cur = conn.cursor()
        cur.execute(
            "SELECT direction, content, sent_at FROM messages WHERE phone = %s ORDER BY sent_at DESC LIMIT %s",
            (phone, limit)
        )
        return list(reversed(_to_dicts(cur, cur.fetchall())))


def get_client_messages(client_id: int, limit: int = 50) -> list:
    with get_conn() as conn:
        cur = conn.cursor()
        cur.execute(
            "SELECT * FROM messages WHERE client_id = %s ORDER BY sent_at DESC LIMIT %s",
            (client_id, limit)
        )
        return list(reversed(_to_dicts(cur, cur.fetchall())))


# ── Activity log ──────────────────────────────────────────────────────────────

def log_activity(client_id: int, action_type: str, description: str,
                 ai_message: str = "", client_response: str = "",
                 moskit_note: str = "") -> None:
    with get_conn() as conn:
        cur = conn.cursor()
        cur.execute("""
            INSERT INTO activity_log
                (client_id, action_type, description, ai_message, client_response, moskit_note)
            VALUES (%s,%s,%s,%s,%s,%s)
        """, (client_id, action_type, description, ai_message, client_response, moskit_note))


def get_activity_log(client_id: Optional[int] = None, limit: int = 100) -> list:
    with get_conn() as conn:
        cur = conn.cursor()
        if client_id:
            cur.execute("""
                SELECT a.*, c.name AS client_name, c.phone
                FROM activity_log a
                LEFT JOIN clients c ON a.client_id = c.id
                WHERE a.client_id = %s ORDER BY a.created_at DESC LIMIT %s
            """, (client_id, limit))
        else:
            cur.execute("""
                SELECT a.*, c.name AS client_name, c.phone
                FROM activity_log a
                LEFT JOIN clients c ON a.client_id = c.id
                ORDER BY a.created_at DESC LIMIT %s
            """, (limit,))
        return _to_dicts(cur, cur.fetchall())


def get_stats() -> dict:
    with get_conn() as conn:
        cur = conn.cursor()
        cur.execute("SELECT COUNT(*) FROM clients"); total = cur.fetchone()[0]
        cur.execute("SELECT COUNT(*) FROM clients WHERE stage = 'em_contato'"); em_c = cur.fetchone()[0]
        cur.execute("SELECT COUNT(*) FROM clients WHERE stage = 'em_negociacao'"); em_n = cur.fetchone()[0]
        cur.execute("SELECT COUNT(*) FROM follow_ups WHERE status = 'pending'"); pend = cur.fetchone()[0]
        cur.execute("""
            SELECT COUNT(*) FROM messages
            WHERE direction = 'outbound' AND sent_at::date = CURRENT_DATE
        """); sent = cur.fetchone()[0]
        return {
            "total_clients": total,
            "em_contato": em_c,
            "em_negociacao": em_n,
            "pending_followups": pend,
            "sent_today": sent,
        }
