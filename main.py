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
from datetime import datetime, timezone
from urllib.parse import quote, urljoin, urlparse

import requests as _requests
import stripe as _stripe
import uvicorn
from openai import OpenAI
from twilio.request_validator import RequestValidator as TwilioRequestValidator
from twilio.rest import Client as TwilioClient
from twilio.twiml.voice_response import Connect, Stream, VoiceResponse
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

_openai_api_key = os.getenv("OPENAI_API_KEY", "")
_openai_client = OpenAI(api_key=_openai_api_key) if _openai_api_key else None
_OPENAI_MODEL = "gpt-4.1-mini"

_twilio_account_sid  = os.getenv("TWILIO_ACCOUNT_SID", "")
_twilio_auth_token   = os.getenv("TWILIO_AUTH_TOKEN", "")
_twilio_phone_number = os.getenv("TWILIO_PHONE_NUMBER", "")
_twilio_client = (
    TwilioClient(_twilio_account_sid, _twilio_auth_token)
    if _twilio_account_sid and _twilio_auth_token
    else None
)

# Public base URL Twilio reaches us at (used to build webhook URLs and to
# reconstruct the URL for Twilio signature validation). Deployment moved from
# Railway to Render; PUBLIC_BASE_URL is the current name, RAILWAY_PUBLIC_URL is
# still honored for back-compat with any existing env config.
_PUBLIC_BASE_URL      = os.getenv("PUBLIC_BASE_URL") or os.getenv("RAILWAY_PUBLIC_URL", "https://shy-order.onrender.com")
# Where Google OAuth returns the user. The voice frontend is served both by this
# app (at /) and at the Vercel URL below; the redirect lands on the Vercel copy,
# which talks to the same backend. Override with FRONTEND_URL if needed.
_FRONTEND_URL         = os.getenv("FRONTEND_URL", "https://shy-order.vercel.app")
_TOOLS_WEBHOOK_SECRET = os.getenv("TOOLS_WEBHOOK_SECRET", "")
# Override all outbound calls to a fixed number (e.g. for Twilio trial testing).
# Set to "" or unset in production to call the real restaurant number.
_TWILIO_OVERRIDE_TO   = os.getenv("TWILIO_OVERRIDE_TO", "")

# In-memory fallback for call statuses (single-worker only; Supabase is preferred)
_call_statuses_mem: dict[str, str] = {}

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

class TwilioCallRequest(BaseModel):
    to: str
    restaurant_name: str

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _is_e164(phone: str) -> bool:
    return bool(_E164_RE.match(phone))


def _compute_charge_cents(duration_seconds: int) -> int:
    """Billing: €0.20 base + €0.15 per started minute (rounded up). Minimum 1 minute."""
    minutes = math.ceil(max(1, duration_seconds) / 60)
    return minutes * 15 + 20


def _validate_twilio_sig(request: Request, params: dict) -> None:
    """Reject requests not signed by Twilio. No-op if auth token not configured."""
    if not _twilio_auth_token:
        return
    path = request.url.path
    query = str(request.url.query)
    url = f"{_PUBLIC_BASE_URL}{path}"
    if query:
        url += f"?{query}"
    validator = TwilioRequestValidator(_twilio_auth_token)
    signature = request.headers.get("X-Twilio-Signature", "")
    if not validator.validate(url, params, signature):
        raise HTTPException(status_code=403, detail="Invalid Twilio signature")


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
# Call status store — Supabase-backed, in-memory fallback for local dev
# ---------------------------------------------------------------------------

def _get_call_status(call_sid: str) -> str | None:
    if supabase_admin:
        try:
            result = supabase_admin.table("call_statuses").select("status").eq("call_sid", call_sid).execute()
            if result.data:
                return result.data[0]["status"]
            return None
        except Exception:
            _log(logging.WARNING, "call_status_read_failed", exc=True, call_sid=call_sid)
    return _call_statuses_mem.get(call_sid)


def _set_call_status(call_sid: str, status: str) -> None:
    if supabase_admin:
        try:
            supabase_admin.table("call_statuses").upsert({
                "call_sid": call_sid,
                "status": status,
                "updated_at": datetime.now(timezone.utc).isoformat(),
            }).execute()
            return
        except Exception:
            _log(logging.WARNING, "call_status_write_failed", exc=True, call_sid=call_sid, status=status)
    _call_statuses_mem[call_sid] = status

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
        result = supabase_admin.table("restaurants").select("*").ilike("name", name_query).execute()
        if result.data:
            r = result.data[0]
            return {
                "found": True,
                "name": r["name"],
                "phone_number": r["phone_number"],
                "address": r.get("address", ""),
            }
    except Exception:
        _log(logging.WARNING, "restaurant_lookup_failed", exc=True, name=name_query)
    return {"found": False}


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
    return {
        "call_status": d.get("status"),
        "call_successful": analysis.get("call_successful"),
        "summary": analysis.get("transcript_summary") or "",
    }


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
    if not AGENT_PHONE_NUMBER_ID:
        return {"success": False, "error": "AGENT_PHONE_NUMBER_ID not configured"}

    # Test mode: redirect the call to a fixed number (you stand in for the restaurant).
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
    while time.time() < deadline:
        time.sleep(4)
        outcome = _conversation_outcome(caller_conversation_id)
        if outcome.get("call_status") == "done":
            return {"success": True, "call_sid": call_sid,
                    "conversation_id": caller_conversation_id, **outcome}

    # Still talking/processing — hand the agent the id so it can poll once more.
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
    return {"success": True, "conversation_id": conversation_id, **outcome}


TOOL_REGISTRY["lookup_restaurant"] = lookup_restaurant_tool
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
        "has_payment_method": False,
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
        "has_payment_method": has_payment_method,
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
        return JSONResponse({"has_payment_method": False})

    stripe_customer_id = existing.data[0].get("stripe_customer_id")
    try:
        pms = _stripe.PaymentMethod.list(customer=stripe_customer_id, type="card")
        return JSONResponse({"has_payment_method": len(pms.data) > 0})
    except Exception:
        _log(logging.WARNING, "google_pm_lookup_failed", exc=True, user_id=str(user.id))
        return JSONResponse({"has_payment_method": False})

# ---------------------------------------------------------------------------
# Payment endpoints
# ---------------------------------------------------------------------------

@app.get("/payment/status")
def payment_status(user=Depends(_get_user)) -> JSONResponse:
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
            resp = _requests.get(
                "https://api.elevenlabs.io/v1/convai/conversation/token",
                params={"agent_id": AGENT_ID},
                headers={"xi-api-key": api_key},
                timeout=10,
            )
            resp.raise_for_status()
            data = resp.json()
            signed_url = f"wss://api.elevenlabs.io/v1/convai/conversation?token={data['token']}"
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"ElevenLabs error: {e}")

    return JSONResponse({"signed_url": signed_url, "session_id": session_id})


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

# ---------------------------------------------------------------------------
# Twilio endpoints
# ---------------------------------------------------------------------------

@app.post("/twilio/call")
def twilio_call(req: TwilioCallRequest, user=Depends(_get_user)) -> JSONResponse:
    if not _twilio_client:
        raise HTTPException(status_code=503, detail="Twilio not configured")
    if not _twilio_phone_number:
        raise HTTPException(status_code=503, detail="TWILIO_PHONE_NUMBER not configured")
    if not _is_e164(req.to):
        raise HTTPException(status_code=422, detail="'to' must be E.164 format (e.g. +390612345678)")

    webhook_url = f"{_PUBLIC_BASE_URL}/twilio/incoming?restaurant_name={quote(req.restaurant_name)}"
    dial_to = _TWILIO_OVERRIDE_TO if _TWILIO_OVERRIDE_TO else req.to

    try:
        call = _twilio_client.calls.create(
            to=dial_to,
            from_=_twilio_phone_number,
            url=webhook_url,
            status_callback=f"{_PUBLIC_BASE_URL}/twilio/status",
            status_callback_method="POST",
        )
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Twilio error: {e}")

    _set_call_status(call.sid, call.status)
    return JSONResponse({"call_sid": call.sid})


@app.post("/twilio/status")
async def twilio_status(request: Request) -> Response:
    form = await request.form()
    _validate_twilio_sig(request, dict(form))
    call_sid    = form.get("CallSid", "")
    call_status = form.get("CallStatus", "")
    if call_sid:
        _set_call_status(call_sid, call_status)
    return Response(status_code=200)


@app.get("/twilio/call-status/{call_sid}")
def twilio_call_status(call_sid: str, user=Depends(_get_user)) -> JSONResponse:
    status = _get_call_status(call_sid)
    if status is None:
        raise HTTPException(status_code=404, detail="call_sid not found")
    return JSONResponse({"call_sid": call_sid, "status": status})


@app.post("/twilio/incoming")
async def twilio_incoming(request: Request) -> Response:
    form = await request.form()
    _validate_twilio_sig(request, dict(form))

    restaurant_name = request.query_params.get("restaurant_name", "")

    # Fetch a short-lived signed token to avoid exposing the API key in TwiML
    api_key = os.getenv("ELEVENLABS_API_KEY", "")
    ws_url = None
    with _track("elevenlabs_token:twilio") as t:
        try:
            resp = _requests.get(
                "https://api.elevenlabs.io/v1/convai/conversation/token",
                params={"agent_id": AGENT_ID},
                headers={"xi-api-key": api_key},
                timeout=5,
            )
            if resp.ok:
                token = resp.json().get("token")
                if token:
                    ws_url = f"wss://api.elevenlabs.io/v1/convai/conversation?token={token}"
            else:
                t.outcome = f"http_{resp.status_code}"
                _log(logging.ERROR, "elevenlabs_token_fetch_bad_status", status=resp.status_code)
        except Exception:
            t.outcome = "error"
            _log(logging.ERROR, "elevenlabs_token_fetch_failed", exc=True)

    if not ws_url:
        # The call is already ringing; without a token we can only apologise and
        # hang up. This path used to be silent — now it's logged loudly above.
        _log(logging.ERROR, "twilio_incoming_no_ws_url", restaurant_name=restaurant_name)
        error_twiml = VoiceResponse()
        error_twiml.say("Si è verificato un errore tecnico. Riprova più tardi.", language="it-IT")
        error_twiml.hangup()
        return Response(content=str(error_twiml), media_type="text/xml")

    response = VoiceResponse()
    connect = Connect()
    stream = Stream(url=ws_url)
    connect.append(stream)
    response.append(connect)

    return Response(content=str(response), media_type="text/xml")

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
