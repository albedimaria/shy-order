import argparse
import json
import os
import re
import sys
from pathlib import Path

import requests as _requests
import uvicorn
from bs4 import BeautifulSoup
from dotenv import load_dotenv
from elevenlabs import ElevenLabs
from elevenlabs.conversational_ai.conversation import ClientTools, Conversation
from elevenlabs.conversational_ai.default_audio_interface import DefaultAudioInterface
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel

load_dotenv()

AGENT_ID = "agent_9901kjyr4vwpeyyr2rc3e37qkncs"

TOOL_REGISTRY: dict[str, callable] = {}

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["GET", "POST"],
    allow_headers=["*"],
)


# ---------------------------------------------------------------------------
# Restaurant DB helpers
# ---------------------------------------------------------------------------

def get_or_create_restaurant(
    name: str,
    phone_number: str,
    address: str,
    db_path: str | os.PathLike[str] = "restaurants.json",
) -> dict:
    """
    Save restaurant info to a local JSON file acting as a simple database.
    If the restaurant already exists (matched by name, case-insensitive),
    return the existing record instead of saving a new one.
    """
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
    """
    ElevenLabs client tool callback.

    Expected parameters (as configured in the Shy Order agent tools):
      - name: string
      - phone_number: string
      - address: string

    When Shy Order completes a reservation, it should call this tool
    so the restaurant is stored in the local restaurants.json database.
    """
    name = parameters.get("name", "")
    phone_number = parameters.get("phone_number", "")
    address = parameters.get("address", "")

    return get_or_create_restaurant(
        name=name,
        phone_number=phone_number,
        address=address,
    )


TOOL_REGISTRY["lookup_restaurant"] = lookup_restaurant_tool
TOOL_REGISTRY["save_restaurant_to_local_db"] = save_restaurant_to_local_db_tool

# ---------------------------------------------------------------------------
# ElevenLabs ClientTools (used in --local mode)
# ---------------------------------------------------------------------------

client_tools = ClientTools()
client_tools.register("lookup_restaurant", lookup_restaurant_tool)
client_tools.register("save_restaurant_to_local_db", save_restaurant_to_local_db_tool)


# ---------------------------------------------------------------------------
# FastAPI endpoints
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


class ScrapeRequest(BaseModel):
    url: str


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


@app.post("/tools")
async def tools_webhook(payload: dict) -> JSONResponse:
    """
    Receives ElevenLabs tool call webhooks.
    Expected payload shape: {"tool_name": "...", "parameters": {...}}
    """
    tool_name = payload.get("tool_name")
    parameters = payload.get("parameters", {})

    if not tool_name:
        raise HTTPException(status_code=400, detail="Missing 'tool_name' in request body")

    handler = TOOL_REGISTRY.get(tool_name)
    if handler is None:
        raise HTTPException(status_code=404, detail=f"Unknown tool: '{tool_name}'")

    result = handler(parameters)
    return JSONResponse(result)


# ---------------------------------------------------------------------------
# Entry points
# ---------------------------------------------------------------------------

def run_local() -> None:
    api_key = os.getenv("ELEVENLABS_API_KEY")
    if not api_key:
        print(
            "Error: ELEVENLABS_API_KEY is not set.\n"
            "Create a .env file in this directory with a line like:\n"
            "ELEVENLABS_API_KEY=your_api_key_here",
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
    parser.add_argument("--local", action="store_true", help="Run local voice session instead of web server")
    args = parser.parse_args()

    if args.local:
        run_local()
    else:
        uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=True)
