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


# ------------------------------------------------------------- league selection

@pytest.fixture
def two_leagues(tmp_path, monkeypatch):
    """A hermetic two-league database, so these run on a fresh clone."""
    from advisor import db
    from advisor.config import get_settings
    from advisor.db import query
    from advisor.warehouse.schema import create_schema

    db.close_conn()
    db.get_conn(tmp_path / "web.duckdb")
    create_schema()
    monkeypatch.setattr(get_settings(), "sleeper_username", "tester", raising=False)

    for league_id, name in (("A", "Alpha Dynasty"), ("B", "Bravo Dynasty")):
        query(
            """
            INSERT INTO leagues (league_id, season, name, status, total_rosters,
                                 sleeper_type, format, format_source, superflex,
                                 has_taxi, is_continuation, roster_positions,
                                 scoring_settings, settings, fetched_at)
            VALUES (?, 2025, ?, 'complete', 12, 2, 'dynasty', 'settings.type',
                    false, false, false, '[]', '{}', '{}', now())
            """,
            [league_id, name],
        )
        query(
            "INSERT INTO league_users (league_id, user_id, display_name, team_name) "
            "VALUES (?, ?, 'tester', NULL)",
            [league_id, f"U-{league_id}"],
        )
        query(
            "INSERT INTO league_rosters (league_id, roster_id, owner_id, players, "
            "starters, taxi, reserve, wins, losses, ties, fpts) "
            "VALUES (?, 1, ?, '[]', '[]', '[]', '[]', 0, 0, 0, 0.0)",
            [league_id, f"U-{league_id}"],
        )
    yield query
    db.close_conn()


def _frames(response):
    """Parse an SSE body into (event, data) pairs."""
    out = []
    for frame in response.text.split("\n\n"):
        lines = frame.splitlines()
        event = next((l[7:] for l in lines if l.startswith("event: ")), None)
        data = next((l[6:] for l in lines if l.startswith("data: ")), None)
        if event and data:
            out.append((event, json.loads(data)))
    return out


@pytest.fixture
def dead_model(monkeypatch):
    """No Ollama. Everything about league selection is testable without one."""
    from advisor.agent.backend import BackendError

    def refuse():
        raise BackendError("Cannot reach Ollama. Start it with: ollama serve")

    monkeypatch.setattr("advisor.web.server._backend", refuse)


def test_leagues_lists_what_the_ui_needs_to_switch(client, two_leagues):
    """Runs the real SQL against the real schema — the only thing that catches
    an invented column, which has bitten twice."""
    body = client.get("/leagues").json()

    assert len(body["leagues"]) == 2
    for row in body["leagues"]:
        assert {"league_id", "name", "season", "format", "roster_id"} <= set(row)


def test_the_default_league_is_the_first_one_offered(client, two_leagues):
    """One picker, one order. If these ever disagree, the dropdown shows one
    league while answers come from another."""
    from advisor.cli import _pick_league

    offered = client.get("/leagues").json()["leagues"]
    assert _pick_league(None)[0] == offered[0]["league_id"]


def test_an_unknown_league_is_refused_before_the_stream_opens(client, two_leagues):
    """A bogus id used to pass the picker and raise inside the generator, so the
    stream died after a 200 — which a browser renders as an answer that stops
    mid-sentence."""
    response = client.post("/chat", json={"message": "hi", "league_id": "nope"})
    assert response.status_code == 404
    assert "not linked" in response.json()["detail"]


def test_a_thread_keeps_its_league_when_the_default_moves(
    client, two_leagues, dead_model
):
    """The drift bug. conversations.league_id was written and never read back,
    so turn two re-ran the picker — and the picker's answer moves."""
    from advisor.warehouse import conversations

    conversation_id = conversations.create("B", 1)

    # Flip the default to A by giving it an intent, which outranks everything.
    two_leagues(
        "INSERT INTO team_intent (league_id, roster_id, intent, updated_at) "
        "VALUES ('A', 1, 'contend', now())"
    )
    from advisor.cli import _pick_league

    assert _pick_league(None)[0] == "A", "the default really did move"

    response = client.post(
        "/chat", json={"message": "hi", "conversation_id": conversation_id}
    )
    start = next(data for event, data in _frames(response) if event == "start")
    assert start["league_id"] == "B", "the thread must stay in the league it began in"


def test_switching_leagues_mid_thread_is_refused(client, two_leagues):
    """Answering about A under a UI showing B is worse than saying no."""
    from advisor.warehouse import conversations

    conversation_id = conversations.create("A", 1)
    response = client.post(
        "/chat",
        json={"message": "hi", "conversation_id": conversation_id, "league_id": "B"},
    )
    assert response.status_code == 409
    assert "start a new one" in response.json()["detail"]


def test_the_first_event_names_the_league_even_when_the_model_is_down(
    client, two_leagues, dead_model
):
    """A dead Ollama should still say which league you asked about, and hand
    back a conversation_id to retry into rather than orphaning the turn."""
    frames = _frames(client.post("/chat", json={"message": "hi"}))

    assert frames[0][0] == "start"
    assert frames[0][1]["league_id"] == "A"
    assert frames[0][1]["conversation_id"]
    assert frames[1][0] == "error"
    assert "ollama serve" in frames[1][1]["message"]


def test_the_page_offers_a_league_switcher(client):
    page = client.get("/").text
    assert 'id="league"' in page
    assert "/leagues" in page


# ------------------------------------------------------------------- panels
#
# The shapes are covered in tests/test_panels.py. These are about the HTTP edge:
# that the routes exist, resolve a league the same way `/chat` does, and fail as
# statuses rather than tracebacks.

def test_the_panels_answer_for_the_default_league(client, two_leagues):
    """No league_id means the same league the chat would answer about. Two
    resolutions would put one league in the sidebar and another in the answer."""
    for path in ("/panel/roster", "/panel/standings", "/panel/waivers"):
        body = client.get(path).json()
        assert body["league_id"] == "A", path


def test_a_panel_for_an_unlinked_league_is_a_404(client, two_leagues):
    for path in ("/panel/roster", "/panel/standings", "/panel/waivers"):
        assert client.get(f"{path}?league_id=nope").status_code == 404, path


def test_the_roster_panel_can_be_asked_about_another_team(client, two_leagues):
    """The right-hand column is exactly this: your roster on the left, anyone
    else's on the right."""
    two_leagues(
        "INSERT INTO league_rosters (league_id, roster_id, owner_id, players, "
        "starters, taxi, reserve, wins, losses, ties, fpts) "
        "VALUES ('A', 2, 'U-them', '[]', '[]', '[]', '[]', 0, 0, 0, 0.0)"
    )

    body = client.get("/panel/roster?league_id=A&roster_id=2").json()

    assert body["roster_id"] == 2
    assert body["is_you"] is False, "roster 1 is the tester's, not roster 2"
    assert {"starters", "bench", "taxi", "reserve", "team"} <= set(body)


def test_viewing_another_team_does_not_answer_as_that_team(client, two_leagues):
    """The context carries who is *asking*. Rebuilt around the team being
    looked at, it marks an opponent's roster as yours and weights their players
    by their intent — the documented hazard of rebuilding a LeagueContext from
    an id, arriving through a new door."""
    two_leagues(
        "INSERT INTO league_rosters (league_id, roster_id, owner_id, players, "
        "starters, taxi, reserve, wins, losses, ties, fpts) "
        "VALUES ('A', 2, 'U-them', '[]', '[]', '[]', '[]', 0, 0, 0, 0.0)"
    )
    two_leagues(
        "INSERT INTO team_intent (league_id, roster_id, intent, updated_at) "
        "VALUES ('A', 1, 'contend', now()), ('A', 2, 'rebuild', now())"
    )

    body = client.get("/panel/roster?league_id=A&roster_id=2").json()

    assert body["roster_id"] == 2, "roster 2 is what is being displayed"
    assert body["your_roster_id"] == 1
    assert body["team_intent"] == "contend", "their intent is not yours"


def test_the_standings_double_as_the_team_list(client, two_leagues):
    """The Teams tab picks from this rather than a second endpoint — seeing who
    is 11-3 and wanting to know what they have is one thought, not two."""
    body = client.get("/panel/standings?league_id=A").json()

    assert body["standings"]
    for row in body["standings"]:
        assert {"roster_id", "team", "record", "points_for", "is_you"} <= set(row)


def test_an_empty_wire_explains_itself_rather_than_500ing(client, two_leagues):
    """The two-league fixture links no free agents, which is also the state of
    any league linked before `make warehouse` ran."""
    body = client.get("/panel/waivers?league_id=A").json()

    assert "error" in body
    assert body["positions"], "still say what could be filtered on"


def test_the_page_has_a_roster_column_and_a_league_column(client):
    page = client.get("/").text
    assert 'id="left"' in page and 'id="right"' in page
    for path in ("/panel/roster", "/panel/standings", "/panel/waivers"):
        assert path in page


# ------------------------------------------------------- keeping rosters current

def test_a_league_load_pulls_the_current_rosters(client, two_leagues, fake_sleeper):
    """Rosters move on trades and waiver claims, so they come from Sleeper on
    every load rather than from whenever `make refresh` last ran."""
    body = client.post("/league/refresh", json={"league_id": "A"}).json()

    assert body["ok"] is True
    assert body["changed"] is True, "the fixture league has never been synced"
    assert body["synced_at"]

    again = client.post(
        "/league/refresh", json={"league_id": "A", "force": True}
    ).json()
    assert again["changed"] is False, "nothing moved between the two calls"


def test_a_refresh_reports_a_failure_rather_than_failing(client, two_leagues, fake_sleeper, monkeypatch):
    """This runs on every page load. A 500 here would mean a network blip takes
    down an app whose whole premise is that it works locally."""
    from advisor.sources.sleeper import SleeperError

    def down(league_id):
        raise SleeperError("no network")

    monkeypatch.setattr(fake_sleeper, "get_league", down)
    response = client.post("/league/refresh", json={"league_id": "A"})

    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is False
    assert "saved rosters" in body["reason"]


def test_refreshing_an_unlinked_league_is_a_404(client, two_leagues, fake_sleeper):
    assert client.post(
        "/league/refresh", json={"league_id": "nope"}
    ).status_code == 404


def test_the_panels_say_how_old_the_rosters_are(client, two_leagues, fake_sleeper):
    """Two different ages: stats publish weekly from nflverse, rosters come from
    Sleeper on load. One date for both would hide a stale roster."""
    before = client.get("/panel/roster?league_id=A").json()
    assert before["rosters_as_of"] is None

    client.post("/league/refresh", json={"league_id": "A"})

    after = client.get("/panel/roster?league_id=A").json()
    assert after["rosters_as_of"]


def test_the_page_refreshes_the_league_before_it_renders_it(client):
    page = client.get("/").text
    assert "/league/refresh" in page
    # Awaited, not fired and forgotten: panels rendered before it lands would
    # show the stale rosters this exists to replace.
    assert "await syncLeague()" in page


# ------------------------------------------------- finding leagues by username
#
# The setup path: type a Sleeper username, see what you play in, pick one. These
# stub Sleeper rather than call it, so they run offline and in a second — but
# they run the real ingest and the real SQL behind it, because the promise being
# tested is "the league you picked is the one you get advice about", and only
# executing that end to end shows it.

SLEEPER_USER = {"user_id": "U-me", "display_name": "cooper257"}

DYNASTY_POSITIONS = ["QB", "RB", "WR", "TE", "SUPER_FLEX", "BN"]

FOUND = [
    {
        "league_id": "A",
        "name": "Alpha Dynasty",
        "total_rosters": 12,
        "roster_positions": DYNASTY_POSITIONS,
        "scoring_settings": {"rec": 1.0},
        "settings": {"type": 2, "taxi_slots": 4},
    },
    {
        "league_id": "C",
        "name": "Charlie Redraft",
        "total_rosters": 10,
        "roster_positions": ["QB", "RB", "WR", "TE", "FLEX", "BN"],
        "scoring_settings": {"rec": 0.5},
        "settings": {"type": 0},
    },
]


@pytest.fixture
def fake_sleeper(monkeypatch):
    """Sleeper's API, stubbed at the module the server and ingest both import."""
    from advisor.sources import sleeper
    from advisor.warehouse import refresh

    # The share window hands a just-computed result to the next caller, which is
    # right in a browser and wrong across tests.
    refresh._recent.clear()

    monkeypatch.setattr(sleeper, "get_user", lambda username: SLEEPER_USER)
    monkeypatch.setattr(sleeper, "get_user_leagues", lambda user_id, season: FOUND)
    monkeypatch.setattr(
        sleeper,
        "get_league",
        lambda league_id: next(
            (lg for lg in FOUND if lg["league_id"] == league_id), None
        ),
    )
    monkeypatch.setattr(
        sleeper,
        "get_all_players",
        lambda refresh=False: {
            "p1": {"full_name": "Rostered Guy", "position": "RB", "team": "BUF",
                   "active": True, "gsis_id": "00-1"},
            "p2": {"full_name": "Free Agent", "position": "WR", "team": "SEA",
                   "active": True, "gsis_id": "00-2"},
        },
    )
    monkeypatch.setattr(
        sleeper,
        "get_league_users",
        lambda league_id: [{"user_id": "U-me", "display_name": "cooper257",
                            "metadata": {"team_name": "My Team"}}],
    )
    monkeypatch.setattr(
        sleeper,
        "get_rosters",
        lambda league_id: [
            {"roster_id": 4, "owner_id": "U-me", "players": ["p1"],
             "starters": ["p1"], "settings": {"wins": 1, "losses": 0}}
        ],
    )
    monkeypatch.setattr(sleeper, "get_traded_picks", lambda league_id: [])
    return sleeper


def test_a_username_lists_the_leagues_it_plays_in(client, two_leagues, fake_sleeper):
    body = client.get("/sleeper/leagues?username=cooper257&season=2025").json()

    assert body["username"] == "cooper257"
    assert [row["league_id"] for row in body["leagues"]] == ["A", "C"]
    for row in body["leagues"]:
        assert {"league_id", "name", "format", "total_rosters", "linked"} <= set(row)


def test_the_format_is_shown_before_committing_to_a_league(
    client, two_leagues, fake_sleeper
):
    """Format is what changes the advice, so it has to be visible while
    choosing — not discovered after the ingest."""
    rows = client.get("/sleeper/leagues?username=cooper257").json()["leagues"]
    by_id = {row["league_id"]: row for row in rows}

    assert by_id["A"]["format"] == "dynasty"
    assert by_id["A"]["superflex"] is True
    assert by_id["C"]["format"] == "redraft"


def test_looking_up_a_username_writes_nothing(client, two_leagues, fake_sleeper):
    """A typo should cost one request, not a half-ingested league."""
    before = client.get("/leagues").json()["leagues"]
    client.get("/sleeper/leagues?username=cooper257")
    after = client.get("/leagues").json()["leagues"]

    assert [r["league_id"] for r in before] == [r["league_id"] for r in after]


def test_an_already_linked_league_is_marked_as_such(client, two_leagues, fake_sleeper):
    """Without this the only way to tell is to link it again and wait."""
    rows = client.get("/sleeper/leagues?username=cooper257").json()["leagues"]
    linked = {row["league_id"]: row["linked"] for row in rows}

    assert linked == {"A": True, "C": False}, "A is in the two-league fixture"


def test_an_unknown_username_says_so_rather_than_returning_nothing(
    client, two_leagues, fake_sleeper, monkeypatch
):
    """Sleeper answers `null` for an unknown user instead of a 404, so an empty
    list is the shape a typo arrives in — and reads as "you have no leagues"."""
    monkeypatch.setattr(fake_sleeper, "get_user", lambda username: None)

    response = client.get("/sleeper/leagues?username=nope")

    assert response.status_code == 404
    assert "no user named" in response.json()["detail"]


def test_sleeper_being_unreachable_is_not_a_bad_username(
    client, two_leagues, fake_sleeper, monkeypatch
):
    """Reported as a typo, this sends someone retyping a name that was right."""
    from advisor.sources.sleeper import SleeperError

    def down(username):
        raise SleeperError("GET .../user/x failed after 4 attempts")

    monkeypatch.setattr(fake_sleeper, "get_user", down)
    response = client.get("/sleeper/leagues?username=cooper257")

    assert response.status_code == 502
    assert "reach Sleeper" in response.json()["detail"]


def test_a_blank_username_is_refused_before_the_request(client, two_leagues):
    assert client.get("/sleeper/leagues?username=%20%20").status_code == 422


@pytest.fixture
def empty_db(tmp_path):
    """A warehouse with a schema and nothing in it — the fresh-install state."""
    from advisor import db
    from advisor.warehouse.schema import create_schema

    db.close_conn()
    db.get_conn(tmp_path / "link.duckdb")
    create_schema()
    yield
    db.close_conn()


def test_linking_a_chosen_league_makes_it_the_one_being_advised_on(
    client, empty_db, fake_sleeper
):
    """The whole feature, end to end: nothing linked, a username and a choice,
    and afterwards that league is selectable with the right roster bound."""
    assert client.get("/leagues").json()["leagues"] == []

    body = client.post(
        "/leagues/link",
        json={"username": "cooper257", "season": 2025, "league_id": "A"},
    ).json()

    assert body["league"]["league_id"] == "A"
    assert body["league"]["format"] == "dynasty"

    listed = client.get("/leagues").json()
    assert [row["league_id"] for row in listed["leagues"]] == ["A"]
    # Only the chosen one. Linking everything found would put a league the user
    # did not ask about ahead of the one they did.
    assert listed["leagues"][0]["name"] == "Alpha Dynasty"


def test_linking_records_whose_roster_to_answer_about(client, empty_db, fake_sleeper):
    """THE fresh-install failure, in its web form. Without the username on file
    the league links fine and "how does my roster look?" is still answered with
    "give me your roster_id" — a question the user cannot answer."""
    client.post(
        "/leagues/link",
        json={"username": "cooper257", "season": 2025, "league_id": "A"},
    )

    listed = client.get("/leagues").json()
    assert listed["username"] == "cooper257"
    assert listed["leagues"][0]["roster_id"] == 4, "the roster U-me owns"


def test_a_linked_league_can_be_chatted_about_immediately(
    client, empty_db, fake_sleeper, dead_model
):
    """No restart, no re-read of .env. The turn fails at the model, which is the
    only thing missing here — the league resolved."""
    client.post(
        "/leagues/link",
        json={"username": "cooper257", "season": 2025, "league_id": "A"},
    )

    frames = _frames(client.post("/chat", json={"message": "hi"}))

    assert frames[0][1]["league_id"] == "A"
    assert frames[0][1]["roster_id"] == 4


def test_linking_a_league_the_user_is_not_in_is_a_404(client, empty_db, fake_sleeper):
    response = client.post(
        "/leagues/link",
        json={"username": "cooper257", "season": 2025, "league_id": "not-mine"},
    )
    assert response.status_code == 404


def test_the_server_builds_its_schema_on_a_fresh_clone(tmp_path):
    """`make serve` before anything else has to work, because the page is now
    where setup happens. Without this the first screen a newcomer sees is one
    where every request 500s on a table nobody has created."""
    from advisor import db

    db.close_conn()
    db.get_conn(tmp_path / "brand-new.duckdb")
    try:
        # The lifespan runs on __enter__, which is the whole point here.
        with TestClient(app) as fresh:
            assert fresh.get("/leagues").json()["leagues"] == []
            assert fresh.get("/data-status").status_code == 200
    finally:
        db.close_conn()


def test_the_page_asks_for_a_username_and_offers_what_it_finds(client):
    page = client.get("/").text
    assert 'id="username"' in page
    assert "/sleeper/leagues" in page
    assert "/leagues/link" in page


def test_every_element_the_script_reaches_for_exists(client):
    """There is no build step and no framework here on purpose, so nothing
    checks this. A mistyped id is not an error in a browser — getElementById
    returns null and the button it belonged to is simply dead."""
    import re

    page = client.get("/").text
    wanted = set(re.findall(r'getElementById\("([^"]+)"\)', page))
    present = set(re.findall(r'id="([^"]+)"', page))

    assert wanted, "the page should be scripted"
    assert wanted <= present, f"script reaches for missing ids: {wanted - present}"


def test_the_seasons_offered_include_next_year(client):
    """A dynasty league rolls over on Sleeper months before its season starts,
    so from February to September the league you play in is next year's."""
    from advisor.context import current_nfl_season

    body = client.get("/leagues").json()

    assert body["season"] == current_nfl_season()
    assert max(body["seasons"]) == current_nfl_season() + 1
