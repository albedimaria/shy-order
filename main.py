import argparse
import json
import math
import os
import re
import sys
from pathlib import Path

import requests as _requests
import stripe as _stripe
import uvicorn
from bs4 import BeautifulSoup
from dotenv import load_dotenv
from elevenlabs import ElevenLabs
from elevenlabs.conversational_ai.conversation import ClientTools, Conversation
from elevenlabs.conversational_ai.default_audio_interface import DefaultAudioInterface
from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel
from supabase import create_client as _create_supabase_client

load_dotenv()

AGENT_ID = "agent_9901kjyr4vwpeyyr2rc3e37qkncs"

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
    duration_seconds: int

# ---------------------------------------------------------------------------
# Restaurant DB helpers
# ---------------------------------------------------------------------------

TOOL_REGISTRY: dict[str, callable] = {}


def get_or_create_restaurant(
    name: str,
    phone_number: str,
    address: str,
    db_path: str | os.PathLike[str] = "restaurants.json",
) -> dict:
    db_file = Path(db_path)
    if db_file.exists():
        try:
            with db_file.open("r", encoding="utf-8") as f:
                data = json.load(f)
        except (json.JSONDecodeError, OSError):
            data = []
    else:
        data = []
    if not isinstance(data, list):
        data = []

    name_lower = name.strip().lower()
    for restaurant in data:
        if isinstance(restaurant, dict) and restaurant.get("name", "").strip().lower() == name_lower:
            return restaurant

    new_restaurant = {
        "name": name.strip(),
        "phone_number": phone_number.strip(),
        "address": address.strip(),
    }
    data.append(new_restaurant)
    try:
        with db_file.open("w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
    except OSError as e:
        print(f"Error writing to restaurant database file: {e}", file=sys.stderr)
        sys.exit(1)
    return new_restaurant


# ---------------------------------------------------------------------------
# Tool functions
# ---------------------------------------------------------------------------

def lookup_restaurant_tool(parameters: dict) -> dict:
    name_query = parameters.get("name", "").strip().lower()
    db_file = Path("restaurants.json")
    if not db_file.exists():
        return {"found": False}
    try:
        with db_file.open("r", encoding="utf-8") as f:
            data = json.load(f)
    except (json.JSONDecodeError, OSError):
        return {"found": False}
    if not isinstance(data, list):
        return {"found": False}
    for restaurant in data:
        if isinstance(restaurant, dict) and name_query in restaurant.get("name", "").strip().lower():
            return {
                "found": True,
                "name": restaurant["name"],
                "phone_number": restaurant["phone_number"],
                "address": restaurant["address"],
            }
    return {"found": False}


def save_restaurant_to_local_db_tool(parameters: dict) -> dict:
    return get_or_create_restaurant(
        name=parameters.get("name", ""),
        phone_number=parameters.get("phone_number", ""),
        address=parameters.get("address", ""),
    )


TOOL_REGISTRY["lookup_restaurant"] = lookup_restaurant_tool
TOOL_REGISTRY["save_restaurant_to_local_db"] = save_restaurant_to_local_db_tool

client_tools = ClientTools()
client_tools.register("lookup_restaurant", lookup_restaurant_tool)
client_tools.register("save_restaurant_to_local_db", save_restaurant_to_local_db_tool)

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

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
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

    # Create Stripe customer
    try:
        customer = _stripe.Customer.create(email=req.email)
        stripe_customer_id = customer.id
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Stripe error: {e}")

    # Save to public.users
    supabase_admin.table("users").insert({
        "id": str(user.id),
        "email": req.email,
        "stripe_customer_id": stripe_customer_id,
    }).execute()

    # Sign in to get access token
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
    # Check payment method
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
    base_url = str(request.base_url).rstrip("/")
    # Use Supabase's authorize endpoint directly (implicit flow — no code verifier needed)
    oauth_url = (
        f"{_supabase_url}/auth/v1/authorize"
        f"?provider=google"
        f"&redirect_to={base_url}/auth/callback"
    )
    return RedirectResponse(url=oauth_url)


@app.get("/auth/callback")
def auth_callback() -> HTMLResponse:
    """
    Supabase redirects here with #access_token=... in the URL fragment.
    Serve a minimal page that reads the fragment, syncs the user with Stripe,
    then forwards to the main app with ?token=...&has_payment=...
    """
    html = """<!DOCTYPE html>
<html><head><meta charset="UTF-8"/><title>Accesso…</title></head>
<body style="background:#0e0e0e;color:#666;font-family:Georgia,serif;display:flex;align-items:center;justify-content:center;height:100vh;margin:0">
<p>Accesso in corso…</p>
<script>
const hash = new URLSearchParams(window.location.hash.slice(1));
const token = hash.get('access_token');
const FRONTEND = 'https://shy-order.vercel.app';
const BACKEND  = 'https://shy-order-production.up.railway.app';
if (!token) {
  window.location.href = FRONTEND + '/?error=no_token';
} else {
  fetch(BACKEND + '/auth/google-complete', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', 'Authorization': 'Bearer ' + token }
  })
  .then(r => r.json())
  .then(d => {
    window.location.href = FRONTEND + '/?token=' + encodeURIComponent(token)
      + '&has_payment=' + (d.has_payment_method ? '1' : '0');
  })
  .catch(() => {
    window.location.href = FRONTEND + '/?token=' + encodeURIComponent(token) + '&has_payment=0';
  });
}
</script>
</body></html>"""
    return HTMLResponse(content=html)


@app.post("/auth/google-complete")
def auth_google_complete(user=Depends(_get_user)) -> JSONResponse:
    """Ensure a Stripe customer exists for this OAuth user, return payment status."""
    try:
        existing = supabase_admin.table("users").select("stripe_customer_id").eq("id", str(user.id)).execute()
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

    if not existing.data:
        # First login — create Stripe customer and insert user record
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

    # Returning user — check for saved card
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
    # Verify payment method exists
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

    # Create session record
    try:
        session_resp = supabase_admin.table("sessions").insert({"user_id": str(user.id)}).execute()
        session_id = session_resp.data[0]["id"]
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Could not create session: {e}")

    # Get ElevenLabs signed URL
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
    # Cost: €0.10/min ElevenLabs + €0.05/min Twilio + €0.20 flat fee
    minutes = math.ceil(req.duration_seconds / 60)
    amount_cents = minutes * 15 + 20  # 15 cents/min + 20 cents flat

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

    # Charge
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

    # Update session record
    from datetime import datetime, timezone
    supabase_admin.table("sessions").update({
        "ended_at": datetime.now(timezone.utc).isoformat(),
        "duration_seconds": req.duration_seconds,
        "amount_charged": amount_cents,
        "stripe_payment_intent_id": pi_id,
    }).eq("id", req.session_id).eq("user_id", str(user.id)).execute()

    return JSONResponse({"amount_charged": amount_cents, "duration_seconds": req.duration_seconds})

# ---------------------------------------------------------------------------
# Scrape endpoint
# ---------------------------------------------------------------------------

@app.post("/scrape")
def scrape(req: ScrapeRequest) -> JSONResponse:
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

@app.post("/tools")
async def tools_webhook(payload: dict) -> JSONResponse:
    tool_name = payload.get("tool_name")
    parameters = payload.get("parameters", {})
    if not tool_name:
        raise HTTPException(status_code=400, detail="Missing 'tool_name' in request body")
    handler = TOOL_REGISTRY.get(tool_name)
    if handler is None:
        raise HTTPException(status_code=404, detail=f"Unknown tool: '{tool_name}'")
    return JSONResponse(handler(parameters))

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
