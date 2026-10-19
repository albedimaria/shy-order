import argparse
import hmac
import ipaddress
import json
import logging
import math
import os
import re
import socket
import sys
import time
from datetime import datetime, timedelta, timezone
from urllib.parse import urljoin, urlparse

import requests as _requests
import stripe as _stripe
import uvicorn
from openai import OpenAI
from bs4 import BeautifulSoup
from dotenv import load_dotenv
from elevenlabs import ElevenLabs
from elevenlabs.conversational_ai.conversation import ClientTools, Conversation
from elevenlabs.conversational_ai.default_audio_interface import DefaultAudioInterface
from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse, Response
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel
from supabase import create_client as _create_supabase_client

load_dotenv()

AGENT_ID = "agent_9901kjyr4vwpeyyr2rc3e37qkncs"  # website assistant: talks to the user

# Restaurant-caller agent: a separate ElevenLabs agent that places the actual
# phone call to the restaurant (customer persona, forced Italian). The website
# assistant collects the order, then the backend triggers this agent via
# ElevenLabs' native Twilio outbound call — which bridges the audio correctly,
# unlike the old hand-rolled <Connect><Stream> that never spoke the Twilio
# Media Streams protocol. Booking details are passed as dynamic variables.
CALLER_AGENT_ID       = os.getenv("CALLER_AGENT_ID", "agent_9401kvdgv4fxftxr951vgbh19dvy")
AGENT_PHONE_NUMBER_ID = os.getenv("AGENT_PHONE_NUMBER_ID", "phnum_5501kvdgbrayfty8ptzwm7c5j72r")

_E164_RE = re.compile(r"^\+\d{7,15}$")

# ---------------------------------------------------------------------------
# Structured logging
# ---------------------------------------------------------------------------
# One JSON line per event so Render's log drain stays grep-/jq-friendly. The
# point is to stop swallowing failures silently: every caught-and-ignored
# exception below now emits a WARNING with enough context (tool, call_sid,
# session_id) to debug a production voice call after the fact.

class _JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "level": record.levelname,
            "event": record.getMessage(),
        }
        payload.update(getattr(record, "extra_fields", {}))
        if record.exc_info:
            payload["error"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str)


def _setup_logging() -> logging.Logger:
    logger = logging.getLogger("shyorder")
    if not logger.handlers:
        handler = logging.StreamHandler(sys.stdout)
        handler.setFormatter(_JsonFormatter())
        logger.addHandler(handler)
        logger.setLevel(os.getenv("LOG_LEVEL", "INFO").upper())
        logger.propagate = False
    return logger


log = _setup_logging()


def _log(level: int, event: str, exc: bool = False, **fields) -> None:
    """Emit one structured log line; `fields` become top-level JSON keys."""
    log.log(level, event, exc_info=exc, extra={"extra_fields": fields})

# ---------------------------------------------------------------------------
# External clients
# ---------------------------------------------------------------------------

_supabase_url = os.getenv("SUPABASE_URL", "")
_supabase_service_key = os.getenv("SUPABASE_SERVICE_ROLE_KEY", "")
_supabase_anon_key = os.getenv("SUPABASE_ANON_KEY", "")
# Service-role client: bypasses RLS, used for all DB/admin operations.
# NEVER call sign_in_with_password on this client — a SIGNED_IN event would
# downgrade its PostgREST Authorization header from service_role to the user's
# JWT (and that mutation is shared across all concurrent requests), silently
# breaking RLS-protected inserts. User sign-in goes through supabase_auth below.
supabase_admin = (
    _create_supabase_client(_supabase_url, _supabase_service_key)
    if _supabase_url and _supabase_service_key
    else None
)
# Dedicated anon client for user sign-in only. Its auth/postgrest state is never
# used for table operations, so concurrent sign-ins clobbering it is harmless.
supabase_auth = (
    _create_supabase_client(_supabase_url, _supabase_anon_key)
    if _supabase_url and _supabase_anon_key
    else None
)

_stripe.api_key = os.getenv("STRIPE_SECRET_KEY", "")
# Demo: pagamenti in pausa finché il prodotto è una demo. Spenti di default; per riaccenderli
# impostare PAYMENTS_ENABLED=true su Render. Spenti = niente carta richiesta, niente addebito,
# la durata della sessione si registra comunque.
PAYMENTS_ENABLED = os.getenv("PAYMENTS_ENABLED", "false").strip().lower() in ("1", "true", "yes")


def _shown_payment_method(has_card: bool) -> bool:
    """Quello che il frontend deve credere: con i pagamenti spenti, la carta "c'è" sempre.
    Usato da TUTTE le risposte che il frontend legge per decidere la schermata della carta
    (login, register, google-complete, payment/status)."""
    return has_card or not PAYMENTS_ENABLED

_openai_api_key = os.getenv("OPENAI_API_KEY", "")
_openai_client = OpenAI(api_key=_openai_api_key) if _openai_api_key else None
_OPENAI_MODEL = "gpt-4.1-mini"

# Public base URL of this app. Twilio is handled entirely by ElevenLabs' native
# integration now, so this is only used for CORS / the OAuth redirect origin.
_PUBLIC_BASE_URL      = os.getenv("PUBLIC_BASE_URL") or os.getenv("RAILWAY_PUBLIC_URL", "https://shy-order.onrender.com")
# Where Google OAuth returns the user. The voice frontend is served both by this
# app (at /) and at the Vercel URL below; the redirect lands on the Vercel copy,
# which talks to the same backend. Override with FRONTEND_URL if needed.
_FRONTEND_URL         = os.getenv("FRONTEND_URL", "https://shy-order.vercel.app")
_TOOLS_WEBHOOK_SECRET = os.getenv("TOOLS_WEBHOOK_SECRET", "")
# Test mode: redirect the outbound restaurant call to a fixed number (you stand
# in for the restaurant). Unset in production to call the real restaurant number.
_TWILIO_OVERRIDE_TO   = os.getenv("TWILIO_OVERRIDE_TO", "")

_http_bearer = HTTPBearer()

# ---------------------------------------------------------------------------
# Auth dependency
# ---------------------------------------------------------------------------

async def _get_user(creds: HTTPAuthorizationCredentials = Depends(_http_bearer)):
    if not supabase_admin:
        raise HTTPException(status_code=503, detail="Supabase not configured")
    try:
        resp = supabase_admin.auth.get_user(creds.credentials)
        return resp.user
    except Exception:
        raise HTTPException(status_code=401, detail="Invalid or expired token")

# ---------------------------------------------------------------------------
# Pydantic models
# ---------------------------------------------------------------------------

class RegisterRequest(BaseModel):
    email: str
    password: str

class LoginRequest(BaseModel):
    email: str
    password: str

class ScrapeRequest(BaseModel):
    url: str

class EndSessionRequest(BaseModel):
    session_id: str

class SessionLinkRequest(BaseModel):
    session_id: str
    conversation_id: str

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _is_e164(phone: str) -> bool:
    return bool(_E164_RE.match(phone))


def _compute_charge_cents(duration_seconds: int) -> int:
    """Billing: €0.20 base + €0.15 per started minute (rounded up). Minimum 1 minute."""
    minutes = math.ceil(max(1, duration_seconds) / 60)
    return minutes * 15 + 20


def _check_tools_auth(request: Request) -> None:
    """Validate shared secret on ElevenLabs tool webhook calls."""
    if not _TOOLS_WEBHOOK_SECRET:
        return
    provided = request.headers.get("x-tools-secret", "")
    if not hmac.compare_digest(provided, _TOOLS_WEBHOOK_SECRET):
        raise HTTPException(status_code=401, detail="Unauthorized")


def _assert_safe_scrape_url(url: str) -> None:
    """Block SSRF: only allow http(s) to public hosts.

    Rejects non-http schemes and any hostname that resolves to a private,
    loopback, link-local, or otherwise reserved IP (e.g. cloud metadata at
    169.254.169.254). Raises HTTPException(422) on a disallowed URL.
    """
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        raise HTTPException(status_code=422, detail="URL must use http or https")
    host = parsed.hostname
    if not host:
        raise HTTPException(status_code=422, detail="URL has no host")
    try:
        infos = socket.getaddrinfo(host, parsed.port or 80, proto=socket.IPPROTO_TCP)
    except OSError:
        raise HTTPException(status_code=422, detail="Could not resolve host")
    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        if (
            ip.is_private
            or ip.is_loopback
            or ip.is_link_local
            or ip.is_reserved
            or ip.is_multicast
            or ip.is_unspecified
        ):
            raise HTTPException(status_code=422, detail="URL resolves to a disallowed address")

# ---------------------------------------------------------------------------
# Latency / outcome telemetry — the round-trips we own (see migration 004)
# ---------------------------------------------------------------------------
# ElevenLabs owns the audio/ASR/TTS/turn-taking, so we can't time those. What
# we CAN time: each tool webhook round-trip, the scrape (fetch + OpenAI), the
# ElevenLabs token fetch, and the Twilio call outcome. _track logs one JSON line
# and persists one tool_metrics row per operation, best-effort.

def _persist_metric(tool: str, duration_ms: float, outcome: str, ctx: dict) -> None:
    """Best-effort insert into tool_metrics; never breaks the request it measures."""
    if not supabase_admin:
        return
    try:
        supabase_admin.table("tool_metrics").insert({
            "tool": tool,
            "duration_ms": duration_ms,
            "outcome": outcome,
            "call_sid": ctx.get("call_sid"),
            "conversation_id": ctx.get("conversation_id") or None,
        }).execute()
    except Exception:
        _log(logging.WARNING, "tool_metric_persist_failed", exc=True, tool=tool)


class _track:
    """Context manager: time a block, log it as JSON, persist it to tool_metrics.

    Set `.outcome` or update `.ctx` inside the block to enrich the row (e.g. the
    Twilio terminal status, the resulting call_sid). An exception marks the
    outcome 'error' and is re-raised.
    """
    def __init__(self, tool: str, **ctx):
        self.tool = tool
        self.ctx = ctx
        self.outcome = "ok"

    def __enter__(self) -> "_track":
        self._start = time.perf_counter()
        return self

    def __exit__(self, exc_type, exc, tb) -> bool:
        duration_ms = round((time.perf_counter() - self._start) * 1000, 1)
        if exc_type is not None:
            self.outcome = "error"
        _log(logging.INFO, "tool_metric", tool=self.tool,
             duration_ms=duration_ms, outcome=self.outcome, **self.ctx)
        _persist_metric(self.tool, duration_ms, self.outcome, self.ctx)
        return False

# ---------------------------------------------------------------------------
# Restaurant DB helpers — Supabase-backed (replaces restaurants.json)
# ---------------------------------------------------------------------------

TOOL_REGISTRY: dict[str, callable] = {}


def get_or_create_restaurant(name: str, phone_number: str, address: str) -> dict:
    if not supabase_admin:
        raise RuntimeError("Supabase not configured")
    name = name.strip()
    try:
        result = supabase_admin.table("restaurants").select("*").ilike("name", name).execute()
        if result.data:
            return result.data[0]
        result = supabase_admin.table("restaurants").insert({
            "name": name,
            "phone_number": phone_number.strip(),
            "address": address.strip(),
        }).execute()
        return result.data[0]
    except Exception as e:
        raise RuntimeError(f"Restaurant DB error: {e}") from e

# ---------------------------------------------------------------------------
# Tool functions
# ---------------------------------------------------------------------------

def lookup_restaurant_tool(parameters: dict) -> dict:
    name_query = (parameters.get("restaurant_name") or parameters.get("name") or "").strip()
    if not supabase_admin or not name_query:
        return {"found": False}
    try:
        # Il nome arriva dalla trascrizione: "Pepe Verde", "pepeverde", "Pepe-Verde" sono lo stesso
        # ristorante. Si confronta senza spazi, punteggiatura e maiuscole (la tabella e' piccola).
        def _norm(x: str) -> str:
            return re.sub(r"[^0-9a-zà-ù]", "", (x or "").lower())
        wanted = _norm(name_query)
        rows = supabase_admin.table("restaurants").select("*").execute().data or []
        match = next((row for row in rows if _norm(row.get("name", "")) == wanted), None)
        if match:
            r = match
            return {
                "found": True,
                "name": r["name"],
                "phone_number": r["phone_number"],
                "address": r.get("address", ""),
            }
    except Exception:
        _log(logging.WARNING, "restaurant_lookup_failed", exc=True, name=name_query)
    return {"found": False}


def _normalize_it_phone(raw: str) -> str:
    """'+39 02 8940 6277', '02 89406277', '0039...' -> '+390289406277'. Stringa vuota se non e' un numero italiano plausibile."""
    digits = re.sub(r"[^0-9+]", "", raw or "")
    if digits.startswith("0039"):
        digits = "+39" + digits[4:]
    elif digits.startswith("39") and not digits.startswith("+"):
        digits = "+" + digits
    elif not digits.startswith("+"):
        digits = "+39" + digits
    if not digits.startswith("+39") or not _is_e164(digits) or not (11 <= len(digits) <= 14):
        return ""
    return digits


def find_restaurant_tool(parameters: dict) -> dict:
    """Cerca sul web il telefono di un ristorante che non e' in rubrica (OpenAI Responses + web_search).

    Il numero trovato NON e' verificato: il prompt impone di ripeterlo all'utente prima di chiamare.
    Accetta solo numeri italiani in E.164; tutto il resto torna come found: false.
    """
    name = (parameters.get("restaurant_name") or parameters.get("name") or "").strip()
    city = (parameters.get("city") or "").strip()
    if not name:
        return {"found": False, "error": "restaurant_name is required"}
    if not _openai_client:
        return {"found": False, "error": "web search not configured"}
    where = f" a {city}" if city else " in Italia"
    prompt = (
        f"Trova il ristorante '{name}'{where}. Rispondi SOLO con un oggetto JSON, senza testo intorno: "
        '{"name": nome ufficiale, "phone_number": telefono del locale, "address": indirizzo completo, '
        '"source_url": pagina da cui hai preso il numero}. '
        "Se non trovi il locale o non sei sicuro del numero, metti null in phone_number."
    )
    try:
        with _track("find_restaurant:web_search"):
            resp = _openai_client.responses.create(
                model=_OPENAI_MODEL, tools=[{"type": "web_search"}], input=prompt, timeout=15,
            )
        text = resp.output_text or ""
        data = json.loads(text[text.find("{"): text.rfind("}") + 1])
    except Exception:
        _log(logging.WARNING, "find_restaurant_failed", exc=True, name=name, city=city)
        return {"found": False}
    phone = _normalize_it_phone(data.get("phone_number") or "")
    if not phone:
        return {"found": False}
    return {
        "found": True,
        "name": data.get("name") or name,
        "phone_number": phone,
        "address": data.get("address") or "",
        "source_url": data.get("source_url") or "",
        "verified": False,
    }


def save_restaurant_to_local_db_tool(parameters: dict) -> dict:
    try:
        return get_or_create_restaurant(
            name=(parameters.get("restaurant_name") or parameters.get("name") or ""),
            phone_number=parameters.get("phone_number", ""),
            address=parameters.get("address", ""),
        )
    except RuntimeError as e:
        return {"error": str(e)}


_BOOKING_VAR_KEYS = (
    "booking_type", "customer_name", "party_size", "date",
    "time_primary", "time_fallback", "order_items", "pickup_time", "special_requests",
)


def _caller_dynamic_vars(parameters: dict, restaurant_name: str) -> dict:
    """Build the dynamic variables the restaurant-caller agent reads from its prompt.
    All keys are always present (empty string if not supplied) so the conversation
    never fails on a missing variable; empty fields are simply not spoken."""
    dyn = {"restaurant_name": restaurant_name}
    for k in _BOOKING_VAR_KEYS:
        v = parameters.get(k, "")
        dyn[k] = str(v) if v is not None else ""
    return dyn


def _conversation_outcome(conversation_id: str) -> dict:
    """Fetch the caller conversation's final analysis (status, success, summary)."""
    api_key = os.getenv("ELEVENLABS_API_KEY", "")
    try:
        r = _requests.get(
            f"https://api.elevenlabs.io/v1/convai/conversations/{conversation_id}",
            headers={"xi-api-key": api_key}, timeout=10,
        )
        d = r.json()
    except Exception:
        return {}
    analysis = d.get("analysis") or {}
    collected = {k: (v or {}).get("value") for k, v in (analysis.get("data_collection_results") or {}).items()}
    return {
        "call_status": d.get("status"),
        "call_successful": analysis.get("call_successful"),
        "summary": analysis.get("transcript_summary") or "",
        # Campi strutturati estratti dalla piattaforma (data collection dell'agente chiamante).
        "details": {k: collected.get(k) for k in ("esito", "orario_confermato", "persone", "nota")},
    }


def _persist_call_outcome(conversation_id: str, *, call_sid: str = "",
                          website_conversation_id: str = "", restaurant_name: str = "") -> None:
    """Best-effort upsert of the caller conversation's outcome into call_outcomes —
    the observability source for the call leg. Linkage fields (call_sid,
    website_conversation_id, restaurant_name) are only written when provided, so a
    later refresh (check_call_status) updates the analysis without clobbering them.
    Never breaks the request it's attached to."""
    if not supabase_admin or not conversation_id:
        return
    try:
        api_key = os.getenv("ELEVENLABS_API_KEY", "")
        r = _requests.get(
            f"https://api.elevenlabs.io/v1/convai/conversations/{conversation_id}",
            headers={"xi-api-key": api_key}, timeout=10,
        )
        d = r.json()
    except Exception:
        _log(logging.WARNING, "call_outcome_fetch_failed", exc=True, conversation_id=conversation_id)
        return
    md = d.get("metadata") or {}
    an = d.get("analysis") or {}
    row = {
        "conversation_id": conversation_id,
        "status": d.get("status"),
        "call_successful": an.get("call_successful"),          # 'success' | 'failure' | 'unknown'
        "transcript_summary": an.get("transcript_summary") or "",
        "summary_title": an.get("call_summary_title") or "",
        "duration_seconds": md.get("call_duration_secs"),
        "termination_reason": md.get("termination_reason"),
        "evaluation_results": an.get("evaluation_criteria_results") or {},
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }
    if call_sid:
        row["call_sid"] = call_sid
    if website_conversation_id:
        row["website_conversation_id"] = website_conversation_id
    if restaurant_name:
        row["restaurant_name"] = restaurant_name
    try:
        supabase_admin.table("call_outcomes").upsert(row, on_conflict="conversation_id").execute()
    except Exception:
        _log(logging.WARNING, "call_outcome_persist_failed", exc=True, conversation_id=conversation_id)


# ---------------------------------------------------------------------------
# Persistent memory layer — order_history (migration 007)
# ---------------------------------------------------------------------------
# ElevenLabs is stateless per conversation; this is the piece that survives
# across sessions so the website assistant can propose "il solito" to a
# returning user. MVP: items + restaurant + date, no raw transcripts.

_EXTRACT_CONFIRMED_ORDER_TOOL = {
    "type": "function",
    "function": {
        "name": "extract_confirmed_order",
        "description": "Record the FINAL details agreed with the restaurant, as negotiated on the call.",
        "parameters": {
            "type": "object",
            "properties": {
                "items": {
                    "type": ["array", "null"],
                    "items": {"type": "string"},
                    "description": "Confirmed order items with quantities (takeaway). null if unchanged/not mentioned.",
                },
                "party_size": {"type": ["string", "null"]},
                "time": {"type": ["string", "null"],
                         "description": "The FINAL agreed time (e.g. the fallback if the preferred slot was unavailable)."},
                "date": {"type": ["string", "null"]},
                "pickup_time": {"type": ["string", "null"]},
            },
            "required": ["items", "party_size", "time", "date", "pickup_time"],
            "additionalProperties": False,
        },
    },
}


def _extract_confirmed_order(summary: str, base: dict) -> dict:
    """Hybrid source of truth: the tool parameters are the base (what the user
    asked for); one forced tool call over the transcript summary overrides the
    fields that were renegotiated on the call (e.g. 20:00 -> 21:30). Falls back
    to the base untouched if the LLM call fails or returns nothing."""
    payload = dict(base)
    if not _openai_client or not summary:
        return payload
    try:
        resp = _openai_client.chat.completions.create(
            model=_OPENAI_MODEL,
            tools=[_EXTRACT_CONFIRMED_ORDER_TOOL],
            tool_choice={"type": "function", "function": {"name": "extract_confirmed_order"}},
            messages=[{
                "role": "user",
                "content": (
                    "This is the summary of a phone call our assistant made to a restaurant "
                    "to place the booking/order below. Extract the FINAL agreed details. "
                    "Return null for any field the summary does not change.\n\n"
                    f"Requested: {json.dumps(base, ensure_ascii=False)}\n\nCall summary: {summary}"
                ),
            }],
        )
        tool_calls = resp.choices[0].message.tool_calls
        if tool_calls:
            confirmed = json.loads(tool_calls[0].function.arguments)
            for k, v in confirmed.items():
                if v:  # null/empty = keep the requested value
                    payload[k] = v
    except Exception:
        _log(logging.WARNING, "confirmed_order_extraction_failed", exc=True)
    return payload


def _resolve_user_id(website_conversation_id: str) -> str | None:
    """Website conversation -> session -> user. The linkage /session/link wrote."""
    if not supabase_admin or not website_conversation_id:
        return None
    try:
        res = supabase_admin.table("sessions").select("user_id").eq(
            "elevenlabs_conversation_id", website_conversation_id).limit(1).execute()
        if res.data:
            return res.data[0]["user_id"]
    except Exception:
        _log(logging.WARNING, "order_history_user_lookup_failed", exc=True,
             conversation_id=website_conversation_id)
    return None


def _record_order(caller_conversation_id: str, website_conversation_id: str = "") -> None:
    """Best-effort: persist a successful call into order_history.

    Fetches the caller conversation once — it carries everything we need:
    the ORIGINAL booking details (conversation_initiation_client_data.dynamic_variables,
    so this also works from check_call_status, which has no tool parameters) and
    the transcript summary (what was actually agreed). Upsert on
    caller_conversation_id makes the double trigger (make_restaurant_call +
    check_call_status) idempotent. Never breaks the request it's attached to."""
    if not supabase_admin or not caller_conversation_id:
        return
    try:
        api_key = os.getenv("ELEVENLABS_API_KEY", "")
        r = _requests.get(
            f"https://api.elevenlabs.io/v1/convai/conversations/{caller_conversation_id}",
            headers={"xi-api-key": api_key}, timeout=10,
        )
        d = r.json()
    except Exception:
        _log(logging.WARNING, "order_record_fetch_failed", exc=True,
             conversation_id=caller_conversation_id)
        return
    analysis = d.get("analysis") or {}
    if d.get("status") != "done" or analysis.get("call_successful") != "success":
        return  # only successful calls become memory

    dyn = ((d.get("conversation_initiation_client_data") or {}).get("dynamic_variables") or {})
    restaurant_name = (dyn.get("restaurant_name") or "").strip()
    booking_type = (dyn.get("booking_type") or "prenotazione").strip() or "prenotazione"
    if not restaurant_name:
        return

    # Recover the website conversation (and thus the user) when not passed in.
    if not website_conversation_id:
        try:
            row = supabase_admin.table("call_outcomes").select("website_conversation_id").eq(
                "conversation_id", caller_conversation_id).limit(1).execute()
            if row.data:
                website_conversation_id = row.data[0].get("website_conversation_id") or ""
        except Exception:
            _log(logging.WARNING, "order_record_outcome_lookup_failed", exc=True,
                 conversation_id=caller_conversation_id)
    user_id = _resolve_user_id(website_conversation_id)
    if not user_id:
        _log(logging.INFO, "order_record_skipped_no_user",
             conversation_id=caller_conversation_id)
        return

    if booking_type == "asporto":
        raw = (dyn.get("order_items") or "").strip()
        base = {
            "items": [s.strip() for s in raw.split(",") if s.strip()],
            "pickup_time": (dyn.get("pickup_time") or "").strip(),
        }
    else:
        base = {
            "party_size": (dyn.get("party_size") or "").strip(),
            "time": (dyn.get("time_primary") or "").strip(),
            "date": (dyn.get("date") or "").strip(),
        }
    payload = _extract_confirmed_order(analysis.get("transcript_summary") or "", base)
    payload = {k: v for k, v in payload.items() if v}  # drop empties

    try:
        supabase_admin.table("order_history").upsert({
            "user_id": user_id,
            "caller_conversation_id": caller_conversation_id,
            "restaurant_name": restaurant_name,
            "booking_type": booking_type,
            "items": payload,
        }, on_conflict="caller_conversation_id").execute()
        _log(logging.INFO, "order_recorded", conversation_id=caller_conversation_id,
             restaurant_name=restaurant_name, booking_type=booking_type)
    except Exception:
        _log(logging.WARNING, "order_record_persist_failed", exc=True,
             conversation_id=caller_conversation_id)


def _relative_date_it(created_at: str) -> str:
    """'oggi' / 'ieri' / 'N giorni fa' / 'N settimane fa' — computed here so the
    LLM never has to do date math (it would hallucinate)."""
    try:
        then = datetime.fromisoformat(created_at.replace("Z", "+00:00"))
    except Exception:
        return ""
    days = max(0, (datetime.now(timezone.utc) - then).days)
    if days == 0:
        return "oggi"
    if days == 1:
        return "ieri"
    if days < 14:
        return f"{days} giorni fa"
    return f"{days // 7} settimane fa"


def _get_last_orders(user_id: str, n: int = 3, max_age_days: int = 90) -> str:
    """The user's recent orders as compact speakable Italian lines (one per order),
    for the website assistant's last_orders dynamic variable. Empty string when
    there's no history — the prompt is told to stay silent in that case.
    Decay: an 8-month-old order isn't 'il solito' — filter at read time."""
    if not supabase_admin or not user_id:
        return ""
    cutoff = (datetime.now(timezone.utc) - timedelta(days=max_age_days)).isoformat()
    try:
        res = (supabase_admin.table("order_history").select("*")
               .eq("user_id", user_id).gte("created_at", cutoff)
               .order("created_at", desc=True).limit(n).execute())
        rows = res.data or []
    except Exception:
        _log(logging.WARNING, "last_orders_read_failed", exc=True, user_id=user_id)
        return ""
    lines = []
    for row in rows:
        p = row.get("items") or {}
        when = _relative_date_it(row.get("created_at") or "")
        if row.get("booking_type") == "asporto":
            what = ", ".join(p.get("items") or []) or "asporto"
            line = f"{row['restaurant_name']} — asporto: {what}"
        else:
            bits = []
            if p.get("party_size"):
                bits.append(f"tavolo per {p['party_size']}")
            if p.get("time"):
                bits.append(f"alle {p['time']}")
            line = f"{row['restaurant_name']} — {' '.join(bits) or 'prenotazione'}"
        lines.append(f"{line} ({when})" if when else line)
    return "\n".join(lines)


# Idempotenza della chiamata in uscita: se l'agente del sito richiama il tool (per esempio perche'
# un'interruzione ha abbandonato la prima esecuzione) non parte una seconda telefonata.
# Prova live del 19/10: 5 chiamate in un minuto. In memoria: Render gira con un solo processo.
_ACTIVE_CALLS: dict[str, tuple[str, str, float]] = {}
_ACTIVE_CALL_TTL_S = 180


def make_restaurant_call_tool(parameters: dict) -> dict:
    # __conversation_id__ is the WEBSITE conversation (for analytics linking)
    website_conversation_id = parameters.pop("__conversation_id__", "")
    phone_number    = parameters.get("phone_number", "").strip()
    restaurant_name = parameters.get("restaurant_name", "").strip()

    if not phone_number:
        return {"success": False, "error": "phone_number is required"}
    if not _is_e164(phone_number):
        return {"success": False, "error": "phone_number must be E.164 format (e.g. +390612345678)"}
    api_key = os.getenv("ELEVENLABS_API_KEY", "")
    if not api_key:
        return {"success": False, "error": "ELEVENLABS_API_KEY not configured"}

    call_key = website_conversation_id or phone_number
    active = _ACTIVE_CALLS.get(call_key)
    if active and time.time() - active[2] < _ACTIVE_CALL_TTL_S:
        outcome = _conversation_outcome(active[0])
        if outcome.get("call_status") != "done":
            _log(logging.INFO, "duplicate_call_suppressed", conversation_id=active[0])
            return {"success": True, "call_sid": active[1], "conversation_id": active[0],
                    "call_status": "in_progress",
                    "note": "La chiamata al ristorante e' gia' in corso: non rilanciarla. Usa check_call_status."}
        return {"success": True, "call_sid": active[1], "conversation_id": active[0], **outcome}

    if not AGENT_PHONE_NUMBER_ID:
        return {"success": False, "error": "AGENT_PHONE_NUMBER_ID not configured"}

    # Test mode: redirect the call to a fixed number (you stand in for the restaurant).
    # Guardia: senza override non si chiama un ristorante vero, a meno di ALLOW_REAL_CALLS=true.
    # (find_restaurant trova numeri VERI: in demo devono squillare solo al numero di prova.)
    if not _TWILIO_OVERRIDE_TO and os.getenv("ALLOW_REAL_CALLS", "").strip().lower() != "true":
        _log(logging.WARNING, "real_call_blocked", restaurant_name=restaurant_name)
        return {"success": False, "error": "demo: chiamate reali disattivate (manca TWILIO_OVERRIDE_TO)"}
    to_number = _TWILIO_OVERRIDE_TO if _TWILIO_OVERRIDE_TO else phone_number

    payload = {
        "agent_id": CALLER_AGENT_ID,
        "agent_phone_number_id": AGENT_PHONE_NUMBER_ID,
        "to_number": to_number,
        "conversation_initiation_client_data": {
            "dynamic_variables": _caller_dynamic_vars(parameters, restaurant_name),
        },
    }
    try:
        resp = _requests.post(
            "https://api.elevenlabs.io/v1/convai/twilio/outbound-call",
            json=payload, headers={"xi-api-key": api_key, "Content-Type": "application/json"},
            timeout=15,
        )
        data = resp.json()
    except Exception as e:
        return {"success": False, "error": f"outbound-call error: {e}"}
    if not resp.ok or not data.get("success"):
        return {"success": False, "error": data.get("message") or f"outbound-call HTTP {resp.status_code}"}

    caller_conversation_id = data.get("conversation_id", "")
    call_sid = data.get("callSid", "")
    _ACTIVE_CALLS[call_key] = (caller_conversation_id, call_sid, time.time())

    # Analytics: bump the restaurant's call count and link it to the website session.
    if supabase_admin and restaurant_name:
        try:
            supabase_admin.rpc("increment_restaurant_call_count", {"p_name": restaurant_name}).execute()
        except Exception:
            _log(logging.WARNING, "restaurant_call_count_increment_failed", exc=True, restaurant_name=restaurant_name)
        if website_conversation_id:
            try:
                supabase_admin.table("sessions").update(
                    {"restaurant_name": restaurant_name}
                ).eq("elevenlabs_conversation_id", website_conversation_id).execute()
            except Exception:
                _log(logging.WARNING, "session_restaurant_link_failed", exc=True,
                     conversation_id=website_conversation_id, restaurant_name=restaurant_name)

    # Block (under the 75s tool timeout) until the caller conversation finishes.
    # ElevenLabs owns the call now; we poll its conversation status, not Twilio's.
    deadline = time.time() + 55
    _persist = lambda: _persist_call_outcome(
        caller_conversation_id, call_sid=call_sid,
        website_conversation_id=website_conversation_id, restaurant_name=restaurant_name)
    while time.time() < deadline:
        time.sleep(4)
        outcome = _conversation_outcome(caller_conversation_id)
        if outcome.get("call_status") == "done":
            _persist()  # observability: persist the final outcome (best-effort)
            if outcome.get("call_successful") == "success":
                _record_order(caller_conversation_id, website_conversation_id)  # memory layer
            return {"success": True, "call_sid": call_sid,
                    "conversation_id": caller_conversation_id, **outcome}

    # Still talking/processing — persist what we have; check_call_status will refresh.
    _persist()
    return {"success": True, "call_sid": call_sid,
            "conversation_id": caller_conversation_id, "call_status": "in_progress"}


def check_call_status_tool(parameters: dict) -> dict:
    # New flow: the call lives as an ElevenLabs caller conversation.
    conversation_id = (parameters.get("conversation_id") or parameters.get("call_sid") or "").strip()
    if not conversation_id:
        return {"success": False, "error": "conversation_id is required"}
    outcome = _conversation_outcome(conversation_id)
    if not outcome:
        return {"success": False, "error": "conversation not found"}
    _persist_call_outcome(conversation_id)  # refresh the outcome row (analysis now complete)
    if outcome.get("call_successful") == "success":
        # Long calls return in_progress from make_restaurant_call; the order is
        # recorded here instead (params recovered from the conversation itself).
        _record_order(conversation_id)
    return {"success": True, "conversation_id": conversation_id, **outcome}


TOOL_REGISTRY["lookup_restaurant"] = lookup_restaurant_tool
TOOL_REGISTRY["find_restaurant"] = find_restaurant_tool
TOOL_REGISTRY["save_restaurant_to_local_db"] = save_restaurant_to_local_db_tool
TOOL_REGISTRY["make_restaurant_call"] = make_restaurant_call_tool
TOOL_REGISTRY["check_call_status"] = check_call_status_tool

client_tools = ClientTools()
client_tools.register("lookup_restaurant", lookup_restaurant_tool)
client_tools.register("save_restaurant_to_local_db", save_restaurant_to_local_db_tool)
client_tools.register("make_restaurant_call", make_restaurant_call_tool)
client_tools.register("check_call_status", check_call_status_tool)

# ---------------------------------------------------------------------------
# Scraping helpers
# ---------------------------------------------------------------------------

_EXTRACT_RESTAURANT_INFO_TOOL = {
    "type": "function",
    "function": {
        "name": "extract_restaurant_info",
        "description": "Record the restaurant's name, phone number, address, and opening hours found on the page.",
        "parameters": {
            "type": "object",
            "properties": {
                "name": {"type": ["string", "null"]},
                "phone_number": {
                    "type": ["string", "null"],
                    "description": "E.164 format if the country can be determined, otherwise exactly as written on the page.",
                },
                "address": {"type": ["string", "null"]},
                "hours": {"type": ["string", "null"]},
            },
            "required": ["name", "phone_number", "address", "hours"],
            "additionalProperties": False,
        },
    },
}


def _tel_hrefs(soup: BeautifulSoup) -> list[str]:
    """Collect tel: link targets, which carry the E.164 number get_text() would otherwise lose."""
    return [
        a["href"].replace("tel:", "").strip()
        for a in soup.find_all("a", href=re.compile(r"^tel:"))
        if a.get("href", "").replace("tel:", "").strip()
    ]


def _visible_text(soup: BeautifulSoup, max_chars: int = 8000) -> str:
    """Strip non-content tags and collapse whitespace, keeping the page within a token budget."""
    for tag in soup(["script", "style", "noscript", "svg", "img"]):
        tag.decompose()
    text = soup.get_text("\n", strip=True)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text[:max_chars]


def _extract_restaurant_info(page_text: str, tel_hints: list[str] | None = None) -> dict:
    """Use an LLM to pull structured restaurant info out of arbitrary page text.

    Heuristics (regex on raw HTML) broke on any layout that didn't match the
    handful of patterns we'd anticipated. A forced tool call gives the same
    strict shape with far better real-world coverage, at the cost of one
    small LLM call per scrape.

    tel_hints carries tel: link targets separately: get_text() drops href
    attributes, so a page whose visible text shows "06 1234 5678" but whose
    tel: link is "tel:+390612345678" would otherwise lose the only E.164
    copy of the number — and make_restaurant_call requires E.164.
    """
    if not _openai_client:
        raise HTTPException(status_code=503, detail="OPENAI_API_KEY not configured")
    hints = f"\n\ntel: links found on the page: {', '.join(tel_hints)}" if tel_hints else ""
    try:
        resp = _openai_client.chat.completions.create(
            model=_OPENAI_MODEL,
            tools=[_EXTRACT_RESTAURANT_INFO_TOOL],
            tool_choice={"type": "function", "function": {"name": "extract_restaurant_info"}},
            messages=[{
                "role": "user",
                "content": (
                    "Extract this restaurant's contact details from the page content below. "
                    "Prefer a tel: link's number (already E.164) over a phone number as written in "
                    f"the visible text. Use null for any field you can't find.\n\n{page_text}{hints}"
                ),
            }],
        )
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Extraction error: {e}")

    tool_calls = resp.choices[0].message.tool_calls
    if not tool_calls:
        raise HTTPException(status_code=502, detail="Extraction failed: no structured output returned")
    return json.loads(tool_calls[0].function.arguments)

# ---------------------------------------------------------------------------
# FastAPI app
# ---------------------------------------------------------------------------

app = FastAPI()

_allowed_origins = [
    o.strip() for o in os.getenv(
        "ALLOWED_ORIGINS",
        f"{_FRONTEND_URL},{_PUBLIC_BASE_URL}",
    ).split(",") if o.strip()
]

app.add_middleware(
    CORSMiddleware,
    allow_origins=_allowed_origins,
    allow_methods=["GET", "POST"],
    allow_headers=["*"],
)


@app.middleware("http")
async def _log_requests(request: Request, call_next):
    """One structured access-log line per request, with server-side latency.

    This is the latency we own end to end (request in → response out). Note that
    /tools/make_restaurant_call will legitimately show ~60s here: that's the
    backend blocking on the phone call, by design (see make_restaurant_call_tool).
    """
    start = time.perf_counter()
    response = await call_next(request)
    duration_ms = round((time.perf_counter() - start) * 1000, 1)
    _log(
        logging.INFO, "http_request",
        method=request.method, path=request.url.path,
        status=response.status_code, duration_ms=duration_ms,
    )
    return response

# ---------------------------------------------------------------------------
# Static files
# ---------------------------------------------------------------------------

@app.get("/")
def index() -> FileResponse:
    return FileResponse("index.html")

@app.get("/style.css")
def css() -> FileResponse:
    return FileResponse("style.css", media_type="text/css")

@app.get("/health")
def health() -> JSONResponse:
    return JSONResponse({"status": "ok"})

@app.get("/config")
def config() -> JSONResponse:
    return JSONResponse({"stripe_publishable_key": os.getenv("STRIPE_PUBLISHABLE_KEY", "")})

# ---------------------------------------------------------------------------
# Auth endpoints
# ---------------------------------------------------------------------------

@app.post("/auth/register")
def auth_register(req: RegisterRequest) -> JSONResponse:
    if not supabase_admin:
        raise HTTPException(status_code=503, detail="Supabase not configured")
    try:
        auth_resp = supabase_admin.auth.admin.create_user({
            "email": req.email,
            "password": req.password,
            "email_confirm": True,
        })
        user = auth_resp.user
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))

    try:
        customer = _stripe.Customer.create(email=req.email)
        stripe_customer_id = customer.id
    except Exception as e:
        try:
            supabase_admin.auth.admin.delete_user(str(user.id))
        except Exception:
            pass
        raise HTTPException(status_code=502, detail=f"Stripe error: {e}")

    try:
        supabase_admin.table("users").insert({
            "id": str(user.id),
            "email": req.email,
            "stripe_customer_id": stripe_customer_id,
        }).execute()
    except Exception as e:
        try:
            supabase_admin.auth.admin.delete_user(str(user.id))
            _stripe.Customer.delete(stripe_customer_id)
        except Exception:
            pass
        raise HTTPException(status_code=500, detail=f"Could not save user: {e}")

    if not supabase_auth:
        raise HTTPException(status_code=503, detail="Supabase auth not configured")
    try:
        sign_in = supabase_auth.auth.sign_in_with_password({"email": req.email, "password": req.password})
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

    return JSONResponse({
        "access_token": sign_in.session.access_token,
        "refresh_token": sign_in.session.refresh_token,
        "expires_in": sign_in.session.expires_in,
        "user": {"id": str(user.id), "email": req.email},
        "has_payment_method": _shown_payment_method(False),
    })


@app.post("/auth/login")
def auth_login(req: LoginRequest) -> JSONResponse:
    if not supabase_admin or not supabase_auth:
        raise HTTPException(status_code=503, detail="Supabase not configured")
    try:
        resp = supabase_auth.auth.sign_in_with_password({"email": req.email, "password": req.password})
    except Exception as e:
        raise HTTPException(status_code=401, detail=str(e))

    user = resp.user
    has_payment_method = False
    try:
        record = supabase_admin.table("users").select("stripe_customer_id").eq("id", str(user.id)).single().execute()
        stripe_customer_id = record.data.get("stripe_customer_id")
        if stripe_customer_id:
            pms = _stripe.PaymentMethod.list(customer=stripe_customer_id, type="card")
            has_payment_method = len(pms.data) > 0
    except Exception:
        _log(logging.WARNING, "login_payment_lookup_failed", exc=True, user_id=str(user.id))

    return JSONResponse({
        "access_token": resp.session.access_token,
        "refresh_token": resp.session.refresh_token,
        "expires_in": resp.session.expires_in,
        "user": {"id": str(user.id), "email": user.email},
        "has_payment_method": _shown_payment_method(has_payment_method),
    })

# ---------------------------------------------------------------------------
# Google OAuth endpoints
# ---------------------------------------------------------------------------

@app.get("/auth/google")
def auth_google(request: Request) -> RedirectResponse:
    if not supabase_admin:
        raise HTTPException(status_code=503, detail="Supabase not configured")
    oauth_url = (
        f"{_supabase_url}/auth/v1/authorize"
        f"?provider=google"
        f"&redirect_to={_FRONTEND_URL}"
    )
    return RedirectResponse(url=oauth_url)


@app.post("/auth/google-complete")
def auth_google_complete(user=Depends(_get_user)) -> JSONResponse:
    try:
        existing = supabase_admin.table("users").select("stripe_customer_id").eq("id", str(user.id)).execute()
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

    if not existing.data:
        try:
            customer = _stripe.Customer.create(email=user.email)
        except Exception as e:
            raise HTTPException(status_code=502, detail=f"Stripe error: {e}")
        supabase_admin.table("users").insert({
            "id": str(user.id),
            "email": user.email,
            "stripe_customer_id": customer.id,
        }).execute()
        return JSONResponse({"has_payment_method": _shown_payment_method(False)})

    stripe_customer_id = existing.data[0].get("stripe_customer_id")
    try:
        pms = _stripe.PaymentMethod.list(customer=stripe_customer_id, type="card")
        return JSONResponse({"has_payment_method": _shown_payment_method(len(pms.data) > 0)})
    except Exception:
        _log(logging.WARNING, "google_pm_lookup_failed", exc=True, user_id=str(user.id))
        return JSONResponse({"has_payment_method": _shown_payment_method(False)})

# ---------------------------------------------------------------------------
# Payment endpoints
# ---------------------------------------------------------------------------

@app.get("/payment/status")
def payment_status(user=Depends(_get_user)) -> JSONResponse:
    if not PAYMENTS_ENABLED:
        return JSONResponse({"has_payment_method": True, "payments_enabled": False})
    try:
        record = supabase_admin.table("users").select("stripe_customer_id").eq("id", str(user.id)).single().execute()
        stripe_customer_id = record.data.get("stripe_customer_id")
        if not stripe_customer_id:
            return JSONResponse({"has_payment_method": False})
        pms = _stripe.PaymentMethod.list(customer=stripe_customer_id, type="card")
        return JSONResponse({"has_payment_method": len(pms.data) > 0})
    except Exception as e:
        raise HTTPException(status_code=502, detail=str(e))


@app.post("/payment/setup")
def payment_setup(user=Depends(_get_user)) -> JSONResponse:
    try:
        record = supabase_admin.table("users").select("stripe_customer_id").eq("id", str(user.id)).single().execute()
        stripe_customer_id = record.data.get("stripe_customer_id")
        if not stripe_customer_id:
            raise HTTPException(status_code=404, detail="Stripe customer not found")
        intent = _stripe.SetupIntent.create(
            customer=stripe_customer_id,
            payment_method_types=["card"],
            usage="off_session",
        )
        return JSONResponse({"client_secret": intent.client_secret})
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=502, detail=str(e))

# ---------------------------------------------------------------------------
# Session endpoints
# ---------------------------------------------------------------------------

@app.post("/session/start")
def session_start(user=Depends(_get_user)) -> JSONResponse:
    if PAYMENTS_ENABLED:
        try:
            record = supabase_admin.table("users").select("stripe_customer_id").eq("id", str(user.id)).single().execute()
            stripe_customer_id = record.data.get("stripe_customer_id")
            pms = _stripe.PaymentMethod.list(customer=stripe_customer_id, type="card")
            if not pms.data:
                raise HTTPException(status_code=402, detail="No payment method on file")
        except HTTPException:
            raise
        except Exception as e:
            raise HTTPException(status_code=502, detail=str(e))

    try:
        session_resp = supabase_admin.table("sessions").insert({"user_id": str(user.id)}).execute()
        session_id = session_resp.data[0]["id"]
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Could not create session: {e}")

    api_key = os.getenv("ELEVENLABS_API_KEY")
    if not api_key:
        raise HTTPException(status_code=500, detail="ELEVENLABS_API_KEY not configured")
    try:
        with _track("elevenlabs_token:session_start"):
            # Websocket = signed URL. Il token di /conversation/token oggi e' per WebRTC:
            # messo in un URL wss la sessione si chiude con 1011 (verificato il 19/10/2026).
            resp = _requests.get(
                "https://api.elevenlabs.io/v1/convai/conversation/get-signed-url",
                params={"agent_id": AGENT_ID},
                headers={"xi-api-key": api_key},
                timeout=10,
            )
            resp.raise_for_status()
            signed_url = resp.json()["signed_url"]
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"ElevenLabs error: {e}")

    # Memory layer: the user's recent orders (<=3, last 90 days) as a speakable
    # string. The frontend passes it to the assistant as the last_orders dynamic
    # variable so it can propose "il solito". Empty string for new users.
    last_orders = _get_last_orders(str(user.id))

    return JSONResponse({"signed_url": signed_url, "session_id": session_id,
                         "last_orders": last_orders})


@app.post("/session/end")
def session_end(req: EndSessionRequest, user=Depends(_get_user)) -> JSONResponse:
    # Compute duration from server-side started_at to prevent client manipulation
    try:
        session_rec = (
            supabase_admin.table("sessions")
            .select("started_at, ended_at, duration_seconds, amount_charged")
            .eq("id", req.session_id)
            .eq("user_id", str(user.id))
            .single()
            .execute()
        )
    except Exception:
        raise HTTPException(status_code=404, detail="Session not found")

    # Idempotency guard: if this session was already closed, return what it was
    # charged instead of charging again. The frontend already guards against a
    # double-fire, but a retry / second tab / network retry must not double-bill.
    if session_rec.data.get("ended_at"):
        return JSONResponse({
            "amount_charged": session_rec.data.get("amount_charged") or 0,
            "duration_seconds": session_rec.data.get("duration_seconds") or 0,
            "already_ended": True,
        })

    started_at = datetime.fromisoformat(session_rec.data["started_at"].replace("Z", "+00:00"))
    now = datetime.now(timezone.utc)
    duration_seconds = max(1, int((now - started_at).total_seconds()))
    amount_cents = _compute_charge_cents(duration_seconds)

    if not PAYMENTS_ENABLED:
        # Demo: si registra la durata, nessun addebito.
        try:
            supabase_admin.table("sessions").update({
                "ended_at": now.isoformat(),
                "duration_seconds": duration_seconds,
                "amount_charged": 0,
            }).eq("id", req.session_id).eq("user_id", str(user.id)).execute()
        except Exception:
            _log(logging.WARNING, "session_end_update_failed_demo", exc=True, session_id=req.session_id)
        return JSONResponse({"amount_charged": 0, "duration_seconds": duration_seconds, "payments_enabled": False})

    try:
        record = supabase_admin.table("users").select("stripe_customer_id").eq("id", str(user.id)).single().execute()
        stripe_customer_id = record.data.get("stripe_customer_id")
        pms = _stripe.PaymentMethod.list(customer=stripe_customer_id, type="card")
        if not pms.data:
            raise HTTPException(status_code=402, detail="No payment method on file")
        pm_id = pms.data[0].id
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=502, detail=str(e))

    try:
        payment_intent = _stripe.PaymentIntent.create(
            amount=amount_cents,
            currency="eur",
            customer=stripe_customer_id,
            payment_method=pm_id,
            confirm=True,
            off_session=True,
            # Stripe dedupes by this key, so a retried/concurrent /session/end for
            # the same session reuses the original PaymentIntent instead of charging twice.
            idempotency_key=f"session-end-{req.session_id}",
        )
        pi_id = payment_intent.id
    except _stripe.error.CardError as e:
        raise HTTPException(status_code=402, detail=str(e.user_message))
    except Exception as e:
        raise HTTPException(status_code=502, detail=str(e))

    try:
        supabase_admin.table("sessions").update({
            "ended_at": now.isoformat(),
            "duration_seconds": duration_seconds,
            "amount_charged": amount_cents,
            "stripe_payment_intent_id": pi_id,
        }).eq("id", req.session_id).eq("user_id", str(user.id)).execute()
    except Exception:
        # The charge already went through — if we can't record it, log loudly so
        # the orphaned charge is recoverable (the idempotency key prevents a re-charge).
        _log(logging.ERROR, "session_end_update_failed_after_charge", exc=True,
             session_id=req.session_id, payment_intent_id=pi_id, amount_charged=amount_cents)

    return JSONResponse({"amount_charged": amount_cents, "duration_seconds": duration_seconds})


@app.get("/session/call-outcome")
def session_call_outcome(conversation_id: str, user=Depends(_get_user)) -> JSONResponse:
    """Esito della telefonata al ristorante per la conversazione del sito, per la scheda nella UI.

    state: none (nessuna chiamata) | in_progress | done. Solo la sessione dell'utente stesso.
    """
    if not supabase_admin or not conversation_id:
        raise HTTPException(status_code=400, detail="conversation_id required")
    owned = (supabase_admin.table("sessions").select("id")
             .eq("elevenlabs_conversation_id", conversation_id).eq("user_id", str(user.id))
             .execute().data)
    if not owned:
        raise HTTPException(status_code=404, detail="Session not found")

    caller_id, restaurant = "", ""
    active = _ACTIVE_CALLS.get(conversation_id)
    if active:
        caller_id = active[0]
    rows = (supabase_admin.table("call_outcomes").select("conversation_id, restaurant_name")
            .eq("website_conversation_id", conversation_id).order("updated_at", desc=True).limit(1)
            .execute().data)
    if rows:
        caller_id = caller_id or rows[0]["conversation_id"]
        restaurant = rows[0].get("restaurant_name") or ""
    if not caller_id:
        return JSONResponse({"state": "none"})

    outcome = _conversation_outcome(caller_id)
    if outcome.get("call_status") != "done":
        return JSONResponse({"state": "in_progress", "restaurant_name": restaurant})
    _persist_call_outcome(caller_id)
    details = outcome.get("details") or {}
    return JSONResponse({
        "state": "done",
        "success": outcome.get("call_successful") == "success",
        "restaurant_name": restaurant,
        "esito": details.get("esito"),
        "orario_confermato": details.get("orario_confermato"),
        "persone": details.get("persone"),
        "nota": details.get("nota"),
        "summary": outcome.get("summary") or "",
    })


@app.post("/session/link")
def session_link(req: SessionLinkRequest, user=Depends(_get_user)) -> JSONResponse:
    """Link an ElevenLabs conversation_id to a session so restaurant analytics can be tracked."""
    try:
        supabase_admin.table("sessions").update(
            {"elevenlabs_conversation_id": req.conversation_id}
        ).eq("id", req.session_id).eq("user_id", str(user.id)).execute()
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
    return JSONResponse({"ok": True})


@app.delete("/memory")
def delete_memory(user=Depends(_get_user)) -> JSONResponse:
    """GDPR right-to-erasure for the memory layer: the logged-in user deletes
    their own order history. Self-service — no admin in the loop."""
    if not supabase_admin:
        raise HTTPException(status_code=503, detail="Supabase not configured")
    try:
        res = supabase_admin.table("order_history").delete().eq(
            "user_id", str(user.id)).execute()
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
    deleted = len(res.data or [])
    _log(logging.INFO, "order_history_deleted", user_id=str(user.id), deleted=deleted)
    return JSONResponse({"deleted": deleted})

# ---------------------------------------------------------------------------
# Scrape endpoint
# ---------------------------------------------------------------------------

def _safe_get(url: str, max_redirects: int = 5) -> "_requests.Response":
    """GET following redirects manually, re-validating every hop against SSRF."""
    headers = {"User-Agent": "Mozilla/5.0 (compatible; ShyOrder/1.0)"}
    for _ in range(max_redirects + 1):
        _assert_safe_scrape_url(url)
        resp = _requests.get(url, headers=headers, timeout=10, allow_redirects=False)
        if resp.is_redirect and resp.headers.get("Location"):
            url = urljoin(url, resp.headers["Location"])
            continue
        return resp
    raise HTTPException(status_code=502, detail="Too many redirects")


@app.post("/scrape")
def scrape(req: ScrapeRequest, user=Depends(_get_user)) -> JSONResponse:
    try:
        with _track("scrape:fetch"):
            resp = _safe_get(req.url)
            resp.raise_for_status()
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Failed to fetch URL: {e}")

    soup = BeautifulSoup(resp.text, "html.parser")
    tel_hints = _tel_hrefs(soup)
    with _track("scrape:extract"):
        info = _extract_restaurant_info(_visible_text(soup), tel_hints)
    return JSONResponse(info)

# ---------------------------------------------------------------------------
# Tools webhook
# ---------------------------------------------------------------------------

def _run_tool(tool_name: str, parameters: dict) -> JSONResponse:
    handler = TOOL_REGISTRY.get(tool_name)
    if handler is None:
        raise HTTPException(status_code=404, detail=f"Unknown tool: '{tool_name}'")
    with _track(f"tool:{tool_name}", conversation_id=parameters.get("__conversation_id__", "")) as t:
        result = handler(parameters)
        if isinstance(result, dict):
            # Surface the call's terminal status (or an error) as the metric outcome.
            t.outcome = result.get("status") or ("error" if result.get("error") else "ok")
            if result.get("call_sid"):
                t.ctx["call_sid"] = result["call_sid"]
        return JSONResponse(result)


@app.post("/tools")
def tools_webhook(request: Request, payload: dict) -> JSONResponse:
    _check_tools_auth(request)
    tool_name  = payload.get("tool_name")
    parameters = dict(payload.get("parameters", {}))
    if not tool_name:
        raise HTTPException(status_code=400, detail="Missing 'tool_name' in request body")
    # Inject ElevenLabs conversation_id so tools can link analytics back to the session
    parameters["__conversation_id__"] = payload.get("conversation_id", "")
    return _run_tool(tool_name, parameters)


@app.post("/tools/{tool_name}")
def tools_by_path(tool_name: str, request: Request, payload: dict | None = None) -> JSONResponse:
    _check_tools_auth(request)
    payload = payload or {}
    # ElevenLabs path-based tools POST the params at the top level (no "parameters"
    # wrapper), so fall back to the whole body. Inject __conversation_id__ the same
    # way the body-based /tools handler does — without this, make_restaurant_call
    # never receives it and sessions.restaurant_name is never linked.
    parameters = dict(payload.get("parameters", payload))
    parameters["__conversation_id__"] = (
        payload.get("conversation_id") or parameters.get("conversation_id") or ""
    )
    return _run_tool(tool_name, parameters)

# ---------------------------------------------------------------------------
# Entry points
# ---------------------------------------------------------------------------

def run_local() -> None:
    api_key = os.getenv("ELEVENLABS_API_KEY")
    if not api_key:
        print(
            "Error: ELEVENLABS_API_KEY is not set.\n"
            "Create a .env file with: ELEVENLABS_API_KEY=your_key",
            file=sys.stderr,
        )
        sys.exit(1)

    client = ElevenLabs(api_key=api_key)
    conversation = Conversation(
        client=client,
        agent_id=AGENT_ID,
        requires_auth=True,
        audio_interface=DefaultAudioInterface(),
        client_tools=client_tools,
    )
    input("Press Enter to start the conversation...")
    conversation.start_session()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--local", action="store_true")
    args = parser.parse_args()
    if args.local:
        run_local()
    else:
        uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=True)
