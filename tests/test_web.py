"""Local web UI.

These tests cover the HTTP contract and the SSE framing without calling a model,
so they run in a second and work while Ollama is not running. What they cannot
cover is a real streamed turn — that needs the model and the database, and is
verified by hand against `make serve`.
"""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from advisor.web.server import _sse, app


@pytest.fixture
def client():
    return TestClient(app)


# ------------------------------------------------------------------ SSE framing

def test_sse_frames_are_wellformed():
    """A frame the browser cannot parse loses the whole turn, and the framing is
    hand-rolled on both ends, so it is worth pinning."""
    frame = _sse("token", {"text": "hello"})

    assert frame.startswith("event: token\n")
    assert frame.endswith("\n\n"), "frames are separated by a blank line"
    payload = frame.split("data: ", 1)[1].strip()
    assert json.loads(payload) == {"text": "hello"}


def test_sse_survives_content_that_looks_like_framing():
    """Model output containing newlines must not be read as a frame boundary —
    JSON encoding is what prevents that, so assert it rather than assume it."""
    frame = _sse("token", {"text": "line one\n\nline two"})

    assert frame.count("\n\n") == 1, "the only blank line is the terminator"
    body = json.loads(frame.split("data: ", 1)[1].strip())
    assert body["text"] == "line one\n\nline two"


# --------------------------------------------------------------------- routing

def test_the_page_is_served(client):
    response = client.get("/")
    assert response.status_code == 200
    assert "<title>ffcompanion</title>" in response.text


def test_the_page_needs_no_build_step_and_no_network(client):
    """One file, no framework, no CDN. A leaguemate should be able to run this
    offline after the model is pulled."""
    page = client.get("/").text
    assert "<script" in page and "src=" not in page.split("<script")[1][:200]
    for remote in ("http://", "https://", "cdn."):
        assert remote not in page, f"page reaches out to {remote}"


def test_health_reports_model_and_zero_cost(client):
    body = client.get("/health").json()

    assert body["model"]
    assert body["context_tokens"] >= 8192
    # Stated in the payload because it is the point of the whole architecture.
    assert "$0.00" in body["cost"]


def test_health_explains_a_missing_backend_rather_than_500ing(client, monkeypatch):
    """Ollama not running is THE first-run failure. It has to arrive as an
    explanation with a fix in it, not a stack trace."""
    from advisor.agent.backend import BackendError

    def refuse():
        raise BackendError("Cannot reach Ollama at http://localhost:11434.\n  Start it with:  ollama serve")

    monkeypatch.setattr("advisor.web.server._backend", refuse)
    body = client.get("/health").json()

    assert body["ok"] is False
    assert "ollama serve" in body["error"]


def test_unknown_conversation_is_a_404_not_a_new_thread(client):
    """Silently starting a fresh thread would look like the history vanished."""
    response = client.get("/conversations/does-not-exist")
    assert response.status_code == 404


def test_chat_rejects_an_unknown_conversation(client):
    response = client.post(
        "/chat", json={"message": "hi", "conversation_id": "nope"}
    )
    assert response.status_code == 404


def test_chat_requires_a_message(client):
    assert client.post("/chat", json={}).status_code == 422


# ------------------------------------------------------- queries against the DB

def test_data_status_query_actually_runs(client):
    """Hits the real schema. Both bugs found by hand here were invented
    identifiers — a `rosters` table and a `rows` column, neither of which
    exists — and only executing the SQL catches that."""
    response = client.get("/data-status")

    assert response.status_code == 200, response.text
    body = response.json()
    assert "seasons" in body and "leagues" in body
    for season in body["seasons"]:
        assert {"season", "fetched_at", "row_count"} <= set(season)


def test_listing_conversations_runs(client):
    response = client.get("/conversations")
    assert response.status_code == 200
    assert "conversations" in response.json()


def test_a_conversation_round_trips_through_the_database(client):
    """The HTTP layer is stateless, so this is what makes a refresh keep the
    thread."""
    from advisor.warehouse import conversations

    league = client.get("/data-status").json()["leagues"]
    if not league:
        pytest.skip("no league linked")

    cid = conversations.create(league[0]["league_id"], 1)
    conversations.append(cid, "user", "How is Puka Nacua?")
    conversations.append(cid, "assistant", "1793 yards.", tools_used=["resolve_player"])
    try:
        body = client.get(f"/conversations/{cid}").json()
        assert [m["role"] for m in body["messages"]] == ["user", "assistant"]
        assert body["messages"][1]["tools_used"] == "resolve_player"
    finally:
        conversations.delete(cid)

    assert client.get(f"/conversations/{cid}").status_code == 404


# ------------------------------------------------------------------ no cloud

def test_nothing_in_the_app_binds_beyond_localhost():
    """Serving other people from one machine is what the pivot was away from —
    that machine's GPU would be doing everyone's inference."""
    from pathlib import Path

    makefile = Path("Makefile").read_text()
    serve_line = next(l for l in makefile.splitlines() if "uvicorn" in l)
    assert "127.0.0.1" in serve_line
    assert "0.0.0.0" not in serve_line
