"""Unit + endpoint tests for the shy-order backend.

These run fully offline. main.py is imported once (which loads .env and builds
the external client objects, but makes no network calls at import time). Tests
that would otherwise touch Supabase/Stripe/Twilio monkeypatch the relevant
module global to force the no-I/O branch.

Run:  .venv/Scripts/python.exe -m pytest -q
"""
import socket

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

import main


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("phone, ok", [
    ("+390612345678", True),
    ("+12025550100", True),
    ("0612345678", False),     # no +
    ("+39 06 1234", False),    # spaces
    ("+", False),
    ("", False),
    ("+12", False),            # too short (<7 digits)
    ("+1234567890123456", False),  # too long (>15 digits)
])
def test_is_e164(phone, ok):
    assert main._is_e164(phone) is ok


@pytest.mark.parametrize("seconds, cents", [
    (1, 35),       # 1 min: 15 + 20
    (59, 35),      # still 1 min
    (60, 35),      # exactly 1 min
    (61, 50),      # 2 min: 30 + 20
    (120, 50),     # 2 min
    (600, 170),    # 10 min: 150 + 20
    (0, 35),       # clamped to 1 min minimum
])
def test_compute_charge_cents(seconds, cents):
    assert main._compute_charge_cents(seconds) == cents


def test_tel_hrefs_extracts_and_strips_prefix():
    from bs4 import BeautifulSoup
    html = """
      <a href="tel:+390612345678">Call</a>
      <a href="tel: 06 123 ">spaced</a>
      <a href="mailto:x@y.it">email</a>
      <a href="/menu">menu</a>
    """
    soup = BeautifulSoup(html, "html.parser")
    hrefs = main._tel_hrefs(soup)
    assert "+390612345678" in hrefs
    assert "06 123" in hrefs           # internal spaces kept, ends stripped
    assert all("mailto" not in h for h in hrefs)
    assert len(hrefs) == 2


def test_visible_text_strips_scripts_and_truncates():
    from bs4 import BeautifulSoup
    html = "<html><body><script>var x=1;</script><style>.a{}</style><p>Ciao</p><p>mondo</p></body></html>"
    soup = BeautifulSoup(html, "html.parser")
    text = main._visible_text(soup)
    assert "var x" not in text
    assert ".a{}" not in text
    assert "Ciao" in text and "mondo" in text

    soup2 = BeautifulSoup("<p>" + ("a" * 5000) + "</p>", "html.parser")
    assert len(main._visible_text(soup2, max_chars=100)) == 100


# ---------------------------------------------------------------------------
# SSRF guard
# ---------------------------------------------------------------------------

def _fake_getaddrinfo(ip):
    def _inner(host, port, *a, **k):
        return [(socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", (ip, port or 80))]
    return _inner


def test_ssrf_blocks_non_http_scheme():
    with pytest.raises(HTTPException) as e:
        main._assert_safe_scrape_url("ftp://example.com/x")
    assert e.value.status_code == 422


def test_ssrf_blocks_private_ip(monkeypatch):
    monkeypatch.setattr(main.socket, "getaddrinfo", _fake_getaddrinfo("10.0.0.5"))
    with pytest.raises(HTTPException) as e:
        main._assert_safe_scrape_url("http://internal.example.com/")
    assert e.value.status_code == 422


def test_ssrf_blocks_cloud_metadata(monkeypatch):
    monkeypatch.setattr(main.socket, "getaddrinfo", _fake_getaddrinfo("169.254.169.254"))
    with pytest.raises(HTTPException):
        main._assert_safe_scrape_url("http://metadata/")


def test_ssrf_allows_public_ip(monkeypatch):
    monkeypatch.setattr(main.socket, "getaddrinfo", _fake_getaddrinfo("93.184.216.34"))
    main._assert_safe_scrape_url("https://example.com/")  # must not raise


# ---------------------------------------------------------------------------
# Tool param extraction (the regression we fixed: agent sends restaurant_name)
# ---------------------------------------------------------------------------

class _FakeQuery:
    def __init__(self, rows):
        self._rows = rows
        self.captured = {}
    def select(self, *a, **k):
        return self
    def ilike(self, col, val):
        self.captured["ilike"] = (col, val)
        return self
    def insert(self, payload):
        self.captured["insert"] = payload
        self._rows = [payload]
        return self
    def execute(self):
        return type("R", (), {"data": self._rows})()


class _FakeSupabase:
    def __init__(self, rows=None):
        self.query = _FakeQuery(rows or [])
    def table(self, name):
        return self.query


def test_lookup_reads_restaurant_name(monkeypatch):
    fake = _FakeSupabase(rows=[{"name": "PepeVerde", "phone_number": "+390373123456", "address": "Via X"}])
    monkeypatch.setattr(main, "supabase_admin", fake)
    out = main.lookup_restaurant_tool({"restaurant_name": "PepeVerde"})
    assert out["found"] is True
    assert out["phone_number"] == "+390373123456"


def test_lookup_falls_back_to_legacy_name(monkeypatch):
    fake = _FakeSupabase(rows=[{"name": "X", "phone_number": "+391", "address": ""}])
    monkeypatch.setattr(main, "supabase_admin", fake)
    out = main.lookup_restaurant_tool({"name": "X"})
    assert out["found"] is True


def test_lookup_empty_name_returns_not_found(monkeypatch):
    monkeypatch.setattr(main, "supabase_admin", _FakeSupabase())
    assert main.lookup_restaurant_tool({})["found"] is False


def test_save_passes_restaurant_name(monkeypatch):
    fake = _FakeSupabase(rows=[])
    monkeypatch.setattr(main, "supabase_admin", fake)
    main.save_restaurant_to_local_db_tool(
        {"restaurant_name": "Trattoria", "phone_number": "+390101", "address": "Via Y"}
    )
    assert fake.query.captured["ilike"] == ("name", "Trattoria")


def test_make_call_requires_phone():
    out = main.make_restaurant_call_tool({"restaurant_name": "X"})
    assert out["success"] is False
    assert "phone_number" in out["error"]


def test_make_call_rejects_non_e164():
    out = main.make_restaurant_call_tool({"phone_number": "0612345678", "restaurant_name": "X"})
    assert out["success"] is False
    assert "E.164" in out["error"]


def test_check_call_status_requires_sid():
    out = main.check_call_status_tool({})
    assert out["success"] is False


# ---------------------------------------------------------------------------
# Endpoints (TestClient, no network)
# ---------------------------------------------------------------------------

client = TestClient(main.app)


def test_health():
    r = client.get("/health")
    assert r.status_code == 200
    assert r.json() == {"status": "ok"}


def test_tools_auth_rejects_without_secret(monkeypatch):
    monkeypatch.setattr(main, "_TOOLS_WEBHOOK_SECRET", "s3cret")
    r = client.post("/tools/lookup_restaurant", json={"restaurant_name": "X"})
    assert r.status_code == 401


def test_tools_auth_rejects_wrong_secret(monkeypatch):
    monkeypatch.setattr(main, "_TOOLS_WEBHOOK_SECRET", "s3cret")
    r = client.post("/tools/lookup_restaurant",
                    json={"restaurant_name": "X"}, headers={"x-tools-secret": "nope"})
    assert r.status_code == 401


def test_tools_auth_accepts_correct_secret(monkeypatch):
    monkeypatch.setattr(main, "_TOOLS_WEBHOOK_SECRET", "s3cret")
    monkeypatch.setattr(main, "supabase_admin", None)  # force no-DB → {found: False}
    r = client.post("/tools/lookup_restaurant",
                    json={"restaurant_name": "X"}, headers={"x-tools-secret": "s3cret"})
    assert r.status_code == 200
    assert r.json() == {"found": False}


def test_caller_dynamic_vars_always_complete():
    """The restaurant-caller agent's prompt references these dynamic variables;
    all must always be present (empty string if unset) or the conversation fails."""
    dyn = main._caller_dynamic_vars({"booking_type": "prenotazione", "party_size": 2}, "PepeVerde")
    assert dyn["restaurant_name"] == "PepeVerde"
    assert dyn["booking_type"] == "prenotazione"
    assert dyn["party_size"] == "2"          # coerced to string
    assert dyn["order_items"] == ""          # unset → empty, never missing
    assert set(main._BOOKING_VAR_KEYS).issubset(dyn)


# ---------------------------------------------------------------------------
# Persistent memory layer (order_history)
# ---------------------------------------------------------------------------

from datetime import datetime, timedelta, timezone


@pytest.mark.parametrize("days, expected", [
    (0, "oggi"), (1, "ieri"), (5, "5 giorni fa"), (13, "13 giorni fa"), (21, "3 settimane fa"),
])
def test_relative_date_it(days, expected):
    iso = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    assert main._relative_date_it(iso) == expected


class _FakeOrderQuery:
    """Records the filters _get_last_orders applies, returns preset rows."""
    def __init__(self, rows):
        self._rows = rows
        self.filters = {}
    def select(self, *a, **k):
        return self
    def eq(self, col, val):
        self.filters["eq"] = (col, val)
        return self
    def gte(self, col, val):
        self.filters["gte"] = (col, val)
        return self
    def order(self, *a, **k):
        return self
    def limit(self, n):
        self.filters["limit"] = n
        return self
    def execute(self):
        return type("R", (), {"data": self._rows})()


class _FakeOrderSupabase:
    def __init__(self, rows):
        self.query = _FakeOrderQuery(rows)
    def table(self, name):
        assert name == "order_history"
        return self.query


def test_last_orders_filters_by_user_and_90_days(monkeypatch):
    """Per-user confinement + decay: the query must filter user_id AND a 90-day cutoff."""
    fake = _FakeOrderSupabase(rows=[])
    monkeypatch.setattr(main, "supabase_admin", fake)
    out = main._get_last_orders("user-123")
    assert out == ""  # no history -> empty string, prompt stays silent
    assert fake.query.filters["eq"] == ("user_id", "user-123")
    col, cutoff = fake.query.filters["gte"]
    assert col == "created_at"
    cutoff_dt = datetime.fromisoformat(cutoff)
    age = datetime.now(timezone.utc) - cutoff_dt
    assert 89 <= age.days <= 90
    assert fake.query.filters["limit"] == 3


def test_last_orders_formats_speakable_lines(monkeypatch):
    now = datetime.now(timezone.utc).isoformat()
    rows = [
        {"restaurant_name": "PepeVerde", "booking_type": "asporto",
         "items": {"items": ["2x sushi misto", "1x ramen"]}, "created_at": now},
        {"restaurant_name": "Trattoria da Roma", "booking_type": "prenotazione",
         "items": {"party_size": "2", "time": "21:30"}, "created_at": now},
    ]
    monkeypatch.setattr(main, "supabase_admin", _FakeOrderSupabase(rows))
    out = main._get_last_orders("user-123")
    lines = out.split("\n")
    assert lines[0] == "PepeVerde — asporto: 2x sushi misto, 1x ramen (oggi)"
    assert lines[1] == "Trattoria da Roma — tavolo per 2 alle 21:30 (oggi)"


def test_extract_confirmed_order_falls_back_without_client(monkeypatch):
    """Vague/failed extraction must never lose the order: base params survive."""
    monkeypatch.setattr(main, "_openai_client", None)
    base = {"party_size": "2", "time": "20:00"}
    assert main._extract_confirmed_order("whatever summary", base) == base


def test_extract_confirmed_order_merges_negotiated_fields(monkeypatch):
    """LLM nulls keep the base; non-null values override (the negotiated time)."""
    class _Fn:
        arguments = '{"items": null, "party_size": null, "time": "21:30", "date": null, "pickup_time": null}'
    class _TC:
        function = _Fn()
    class _Msg:
        tool_calls = [_TC()]
    class _Choice:
        message = _Msg()
    class _Resp:
        choices = [_Choice()]
    class _Completions:
        def create(self, **kw):
            return _Resp()
    class _Chat:
        completions = _Completions()
    class _FakeOpenAI:
        chat = _Chat()
    monkeypatch.setattr(main, "_openai_client", _FakeOpenAI())
    base = {"party_size": "2", "time": "20:00", "date": "venerdì"}
    out = main._extract_confirmed_order("negotiated to 21:30", base)
    assert out["time"] == "21:30"          # overridden
    assert out["party_size"] == "2"        # null -> base kept
    assert out["date"] == "venerdì"


def test_delete_memory_requires_auth():
    r = client.delete("/memory")
    assert r.status_code in (401, 403)  # HTTPBearer rejects missing credentials


# --- pagamenti in pausa (demo) ---------------------------------------------

def _as_user(monkeypatch):
    class _U:
        id = "00000000-0000-0000-0000-000000000001"
    main.app.dependency_overrides[main._get_user] = lambda: _U()


def test_payment_status_skips_card_when_payments_disabled(monkeypatch):
    monkeypatch.setattr(main, "PAYMENTS_ENABLED", False)
    _as_user(monkeypatch)
    try:
        r = client.get("/payment/status")
        assert r.status_code == 200
        assert r.json() == {"has_payment_method": True, "payments_enabled": False}
    finally:
        main.app.dependency_overrides.clear()



def test_shown_payment_method_is_true_when_payments_disabled(monkeypatch):
    # login, register e google-complete passano tutti da qui: e' da li' che il frontend
    # decide la schermata della carta (bug del primo deploy: avevo coperto solo /payment/status).
    monkeypatch.setattr(main, "PAYMENTS_ENABLED", False)
    assert main._shown_payment_method(False) is True
    monkeypatch.setattr(main, "PAYMENTS_ENABLED", True)
    assert main._shown_payment_method(False) is False
    assert main._shown_payment_method(True) is True


def test_lookup_ignores_spaces_and_case(monkeypatch):
    # In chiamata vera l'utente ha detto "Pepe Verde" e la ricerca esatta non trovava "PepeVerde".
    fake = _FakeSupabase([{"name": "PepeVerde", "phone_number": "+390373123456", "address": "Crema"}])
    monkeypatch.setattr(main, "supabase_admin", fake)
    out = main.lookup_restaurant_tool({"restaurant_name": "Pepe Verde"})
    assert out["found"] is True and out["name"] == "PepeVerde"


@pytest.mark.parametrize("raw,expected", [
    ("+39 02 8940 6277", "+390289406277"),
    ("02 89406277", "+390289406277"),
    ("0039 0373 123456", "+390373123456"),
    ("+33 1 23 45 67 89", ""),
    ("non disponibile", ""),
])
def test_normalize_it_phone(raw, expected):
    assert main._normalize_it_phone(raw) == expected


def test_find_restaurant_parses_web_answer(monkeypatch):
    class _Resp:
        output_text = 'Ecco: {"name": "Al Pont de Ferr", "phone_number": "+39 02 8940 6277", "address": "Ripa di Porta Ticinese 55, Milano", "source_url": "https://example.com"}'
    class _Client:
        class responses:
            @staticmethod
            def create(**kw):
                assert kw["tools"] == [{"type": "web_search"}]
                return _Resp()
    monkeypatch.setattr(main, "_openai_client", _Client())
    out = main.find_restaurant_tool({"restaurant_name": "Al Pont de Ferr", "city": "Milano"})
    assert out["found"] is True and out["phone_number"] == "+390289406277" and out["verified"] is False


def test_find_restaurant_rejects_missing_or_foreign_number(monkeypatch):
    class _Resp:
        output_text = '{"name": "X", "phone_number": null, "address": ""}'
    class _Client:
        class responses:
            @staticmethod
            def create(**kw):
                return _Resp()
    monkeypatch.setattr(main, "_openai_client", _Client())
    assert main.find_restaurant_tool({"restaurant_name": "X"})["found"] is False


def test_make_call_blocks_real_numbers_without_override(monkeypatch):
    monkeypatch.setattr(main, "_TWILIO_OVERRIDE_TO", "")
    monkeypatch.delenv("ALLOW_REAL_CALLS", raising=False)
    monkeypatch.setenv("ELEVENLABS_API_KEY", "x")
    out = main.make_restaurant_call_tool({"phone_number": "+390289406277", "restaurant_name": "X"})
    assert out["success"] is False and "chiamate reali disattivate" in out["error"]
