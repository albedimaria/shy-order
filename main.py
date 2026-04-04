import argparse
import json
import os
import sys
from pathlib import Path

import uvicorn
from dotenv import load_dotenv
from elevenlabs import ElevenLabs
from elevenlabs.conversational_ai.conversation import ClientTools, Conversation
from elevenlabs.conversational_ai.default_audio_interface import DefaultAudioInterface
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse

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


@app.get("/signed-url")
def signed_url() -> JSONResponse:
    api_key = os.getenv("ELEVENLABS_API_KEY")
    if not api_key:
        raise HTTPException(status_code=500, detail="ELEVENLABS_API_KEY not configured")

    import requests as _requests
    try:
        resp = _requests.get(
            "https://api.elevenlabs.io/v1/convai/conversation/token",
            params={"agent_id": AGENT_ID},
            headers={"xi-api-key": api_key},
            timeout=10,
        )
        resp.raise_for_status()
        data = resp.json()
        print(f"[signed-url] keys: {list(data.keys())}", flush=True)
        print(f"[signed-url] full response: {data}", flush=True)
        return JSONResponse({"signed_url": f"wss://api.elevenlabs.io/v1/convai/conversation?token={data['token']}"})
    except Exception as e:
        raise HTTPException(status_code=502, detail=str(e))


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
