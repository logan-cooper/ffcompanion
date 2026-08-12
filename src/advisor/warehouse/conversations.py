"""Chat history, so a page refresh does not lose the thread.

The HTTP layer is deliberately stateless: every request reloads its conversation
from here rather than holding it in process memory. That keeps a browser refresh,
a second tab, and a server restart all behaving the same, and it means the only
place conversation state lives is the one place the rest of the app already
trusts.

Like `team_intent`, this is user-owned data — re-ingesting stats or re-linking a
league must never touch it.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any

from advisor.db import query

# Long threads cost context and local inference is slow. The model sees only the
# tail; the database keeps everything, so the UI can still show the whole thing.
HISTORY_TURNS = 12


def _now() -> datetime:
    return datetime.now(timezone.utc)


def create(league_id: str, roster_id: int | None = None) -> str:
    conversation_id = uuid.uuid4().hex
    now = _now()
    query(
        """
        INSERT INTO conversations
            (conversation_id, league_id, roster_id, title, created_at, updated_at)
        VALUES (?, ?, ?, NULL, ?, ?)
        """,
        [conversation_id, league_id, roster_id, now, now],
    )
    return conversation_id


def exists(conversation_id: str) -> bool:
    return bool(
        query(
            "SELECT 1 FROM conversations WHERE conversation_id = ?", [conversation_id]
        )
    )


def append(
    conversation_id: str,
    role: str,
    content: str,
    tools_used: list[str] | None = None,
) -> None:
    rows = query(
        "SELECT COALESCE(MAX(seq), 0) AS last FROM messages WHERE conversation_id = ?",
        [conversation_id],
    )
    seq = (rows[0]["last"] if rows else 0) + 1
    now = _now()

    query(
        """
        INSERT INTO messages
            (conversation_id, seq, role, content, tools_used, created_at)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        [conversation_id, seq, role, content, ",".join(tools_used or []) or None, now],
    )
    query(
        "UPDATE conversations SET updated_at = ? WHERE conversation_id = ?",
        [now, conversation_id],
    )
    # First user message becomes the title — enough to tell threads apart in a
    # list without asking anyone to name them.
    if role == "user":
        query(
            """
            UPDATE conversations SET title = ?
            WHERE conversation_id = ? AND title IS NULL
            """,
            [content[:80], conversation_id],
        )


def history(conversation_id: str) -> list[dict[str, Any]]:
    """Everything in the thread, oldest first — for display."""
    return query(
        """
        SELECT role, content, tools_used, created_at
        FROM messages WHERE conversation_id = ?
        ORDER BY seq
        """,
        [conversation_id],
    )


def for_model(conversation_id: str, turns: int = HISTORY_TURNS) -> list[dict[str, str]]:
    """The tail of the thread, shaped for the agent loop.

    Truncated because a small model's context is the scarcest resource here, and
    an unbounded history would silently push the system prompt out of the window
    — the exact failure that made a 4096-token default so expensive to diagnose.
    """
    rows = query(
        """
        SELECT role, content FROM messages
        WHERE conversation_id = ? ORDER BY seq DESC LIMIT ?
        """,
        [conversation_id, turns],
    )
    return [{"role": r["role"], "content": r["content"]} for r in reversed(rows)]


def recent(league_id: str | None = None, limit: int = 20) -> list[dict[str, Any]]:
    if league_id:
        return query(
            """
            SELECT conversation_id, title, updated_at FROM conversations
            WHERE league_id = ? ORDER BY updated_at DESC LIMIT ?
            """,
            [league_id, limit],
        )
    return query(
        """
        SELECT conversation_id, title, updated_at FROM conversations
        ORDER BY updated_at DESC LIMIT ?
        """,
        [limit],
    )


def delete(conversation_id: str) -> None:
    query("DELETE FROM messages WHERE conversation_id = ?", [conversation_id])
    query("DELETE FROM conversations WHERE conversation_id = ?", [conversation_id])
