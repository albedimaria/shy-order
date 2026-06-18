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
    assert fake.query.captured["ilike"] == ("name", "PepeVerde")


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
