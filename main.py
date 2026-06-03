import argparse
import hmac
import math
import os
import re
import sys
import time
from datetime import datetime, timezone
from urllib.parse import quote

import requests as _requests
import stripe as _stripe
import uvicorn
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

AGENT_ID = "agent_9901kjyr4vwpeyyr2rc3e37qkncs"

_E164_RE = re.compile(r"^\+\d{7,15}$")

# ---------------------------------------------------------------------------
# External clients
# ---------------------------------------------------------------------------

_supabase_url = os.getenv("SUPABASE_URL", "")
_supabase_service_key = os.getenv("SUPABASE_SERVICE_ROLE_KEY", "")
supabase_admin = (
    _create_supabase_client(_supabase_url, _supabase_service_key)
    if _supabase_url and _supabase_service_key
    else None
)

_stripe.api_key = os.getenv("STRIPE_SECRET_KEY", "")

_twilio_account_sid  = os.getenv("TWILIO_ACCOUNT_SID", "")
_twilio_auth_token   = os.getenv("TWILIO_AUTH_TOKEN", "")
_twilio_phone_number = os.getenv("TWILIO_PHONE_NUMBER", "")
_twilio_client = (
    TwilioClient(_twilio_account_sid, _twilio_auth_token)
    if _twilio_account_sid and _twilio_auth_token
    else None
)

_RAILWAY_BASE_URL     = os.getenv("RAILWAY_PUBLIC_URL", "https://shy-order.onrender.com")
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


def _validate_twilio_sig(request: Request, params: dict) -> None:
    """Reject requests not signed by Twilio. No-op if auth token not configured."""
    if not _twilio_auth_token:
        return
    path = request.url.path
    query = str(request.url.query)
    url = f"{_RAILWAY_BASE_URL}{path}"
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
            pass
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
            pass
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
    name_query = parameters.get("name", "").strip()
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
        pass
    return {"found": False}


def save_restaurant_to_local_db_tool(parameters: dict) -> dict:
    try:
        return get_or_create_restaurant(
            name=parameters.get("name", ""),
            phone_number=parameters.get("phone_number", ""),
            address=parameters.get("address", ""),
        )
    except RuntimeError as e:
        return {"error": str(e)}


def make_restaurant_call_tool(parameters: dict) -> dict:
    # __conversation_id__ is injected by the /tools webhook handler
    conversation_id = parameters.pop("__conversation_id__", "")
    phone_number    = parameters.get("phone_number", "").strip()
    restaurant_name = parameters.get("restaurant_name", "").strip()

    if not phone_number:
        return {"success": False, "error": "phone_number is required"}
    if not _is_e164(phone_number):
        return {"success": False, "error": "phone_number must be E.164 format (e.g. +390612345678)"}
    if not _twilio_client:
        return {"success": False, "error": "Twilio not configured"}
    if not _twilio_phone_number:
        return {"success": False, "error": "TWILIO_PHONE_NUMBER not configured"}

    # Trial mode: redirect all outbound calls to a fixed test number
    dial_to = _TWILIO_OVERRIDE_TO if _TWILIO_OVERRIDE_TO else phone_number

    webhook_url = f"{_RAILWAY_BASE_URL}/twilio/incoming?restaurant_name={quote(restaurant_name)}"

    try:
        call = _twilio_client.calls.create(
            to=dial_to,
            from_=_twilio_phone_number,
            url=webhook_url,
            status_callback=f"{_RAILWAY_BASE_URL}/twilio/status",
            status_callback_method="POST",
        )
    except Exception as e:
        return {"success": False, "error": str(e)}

    call_sid = call.sid
    _set_call_status(call_sid, call.status)

    # Track restaurant analytics
    if supabase_admin and restaurant_name:
        try:
            supabase_admin.rpc(
                "increment_restaurant_call_count", {"p_name": restaurant_name}
            ).execute()
        except Exception:
            pass
        # Link restaurant to the current session (if conversation_id was supplied)
        if conversation_id:
            try:
                supabase_admin.table("sessions").update(
                    {"restaurant_name": restaurant_name}
                ).eq("elevenlabs_conversation_id", conversation_id).execute()
            except Exception:
                pass

    terminal = {"completed", "no-answer", "busy", "failed", "canceled"}
    deadline = time.time() + 60
    while time.time() < deadline:
        time.sleep(3)
        status = _get_call_status(call_sid) or ""
        if status in terminal:
            return {"success": True, "call_sid": call_sid, "status": status}

    return {"success": True, "call_sid": call_sid, "status": _get_call_status(call_sid) or "timeout"}


def check_call_status_tool(parameters: dict) -> dict:
    call_sid = parameters.get("call_sid", "").strip()
    if not call_sid:
        return {"success": False, "error": "call_sid is required"}
    status = _get_call_status(call_sid)
    if status is None:
        return {"success": False, "error": "call_sid not found"}
    return {"success": True, "call_sid": call_sid, "status": status}


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

def _scrape_name(soup: BeautifulSoup) -> str | None:
    og = soup.find("meta", property="og:title")
    if og and og.get("content"):
        return og["content"].strip()
    if soup.title and soup.title.string:
        return soup.title.string.strip()
    h1 = soup.find("h1")
    if h1:
        return h1.get_text(strip=True)
    return None


def _scrape_phone(soup: BeautifulSoup) -> str | None:
    tel_link = soup.find("a", href=re.compile(r"^tel:"))
    if tel_link:
        return tel_link["href"].replace("tel:", "").strip()
    text = soup.get_text(" ")
    for pattern in [
        r"\+39[\s\-\.]?\d{2,4}[\s\-\.]?\d{4,8}",
        r"\b0\d{1,3}[\s\-\.]?\d{6,8}\b",
        r"\b3\d{2}[\s\-\.]?\d{6,7}\b",
    ]:
        m = re.search(pattern, text)
        if m:
            return re.sub(r"\s+", " ", m.group()).strip()
    return None


def _scrape_address(soup: BeautifulSoup) -> str | None:
    el = soup.find(attrs={"itemprop": "streetAddress"})
    if el:
        return el.get_text(strip=True)
    addr = soup.find("address")
    if addr:
        return addr.get_text(" ", strip=True)
    for term in ["address", "indirizzo", "location"]:
        el = soup.find(class_=re.compile(term, re.I))
        if el:
            return el.get_text(" ", strip=True)
    return None


def _scrape_hours(soup: BeautifulSoup) -> str | None:
    els = soup.find_all(attrs={"itemprop": "openingHours"})
    if els:
        return ", ".join(el.get("content") or el.get_text(strip=True) for el in els)
    for term in ["hours", "orari", "opening"]:
        el = soup.find(class_=re.compile(term, re.I)) or soup.find(id=re.compile(term, re.I))
        if el:
            return el.get_text(" ", strip=True)[:300]
    return None

# ---------------------------------------------------------------------------
# FastAPI app
# ---------------------------------------------------------------------------

app = FastAPI()

_allowed_origins = [
    o.strip() for o in os.getenv(
        "ALLOWED_ORIGINS",
        f"{_FRONTEND_URL},{_RAILWAY_BASE_URL}",
    ).split(",") if o.strip()
]

app.add_middleware(
    CORSMiddleware,
    allow_origins=_allowed_origins,
    allow_methods=["GET", "POST"],
    allow_headers=["*"],
)

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

    try:
        sign_in = supabase_admin.auth.sign_in_with_password({"email": req.email, "password": req.password})
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

    return JSONResponse({
        "access_token": sign_in.session.access_token,
        "user": {"id": str(user.id), "email": req.email},
        "has_payment_method": False,
    })


@app.post("/auth/login")
def auth_login(req: LoginRequest) -> JSONResponse:
    if not supabase_admin:
        raise HTTPException(status_code=503, detail="Supabase not configured")
    try:
        resp = supabase_admin.auth.sign_in_with_password({"email": req.email, "password": req.password})
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
        pass

    return JSONResponse({
        "access_token": resp.session.access_token,
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
            .select("started_at")
            .eq("id", req.session_id)
            .eq("user_id", str(user.id))
            .single()
            .execute()
        )
    except Exception:
        raise HTTPException(status_code=404, detail="Session not found")

    started_at = datetime.fromisoformat(session_rec.data["started_at"].replace("Z", "+00:00"))
    now = datetime.now(timezone.utc)
    duration_seconds = max(1, int((now - started_at).total_seconds()))
    minutes = math.ceil(duration_seconds / 60)
    amount_cents = minutes * 15 + 20

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
        )
        pi_id = payment_intent.id
    except _stripe.error.CardError as e:
        raise HTTPException(status_code=402, detail=str(e.user_message))
    except Exception as e:
        raise HTTPException(status_code=502, detail=str(e))

    supabase_admin.table("sessions").update({
        "ended_at": now.isoformat(),
        "duration_seconds": duration_seconds,
        "amount_charged": amount_cents,
        "stripe_payment_intent_id": pi_id,
    }).eq("id", req.session_id).eq("user_id", str(user.id)).execute()

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

    webhook_url = f"{_RAILWAY_BASE_URL}/twilio/incoming?restaurant_name={quote(req.restaurant_name)}"
    dial_to = _TWILIO_OVERRIDE_TO if _TWILIO_OVERRIDE_TO else req.to

    try:
        call = _twilio_client.calls.create(
            to=dial_to,
            from_=_twilio_phone_number,
            url=webhook_url,
            status_callback=f"{_RAILWAY_BASE_URL}/twilio/status",
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
    except Exception:
        pass

    if not ws_url:
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

@app.post("/scrape")
def scrape(req: ScrapeRequest, user=Depends(_get_user)) -> JSONResponse:
    try:
        resp = _requests.get(
            req.url,
            headers={"User-Agent": "Mozilla/5.0 (compatible; ShyOrder/1.0)"},
            timeout=10,
        )
        resp.raise_for_status()
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Failed to fetch URL: {e}")

    soup = BeautifulSoup(resp.text, "html.parser")
    return JSONResponse({
        "name":         _scrape_name(soup),
        "phone_number": _scrape_phone(soup),
        "address":      _scrape_address(soup),
        "hours":        _scrape_hours(soup),
    })

# ---------------------------------------------------------------------------
# Tools webhook
# ---------------------------------------------------------------------------

def _run_tool(tool_name: str, parameters: dict) -> JSONResponse:
    handler = TOOL_REGISTRY.get(tool_name)
    if handler is None:
        raise HTTPException(status_code=404, detail=f"Unknown tool: '{tool_name}'")
    return JSONResponse(handler(parameters))


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
    parameters = (payload or {}).get("parameters", payload or {})
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
