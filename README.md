# Shy Order

A voice agent that books restaurant tables or takeaway orders **by actually calling the restaurant on the phone**, end to end, with no human in the loop on either side of the call.

The user talks to an AI in the browser (text or voice, IT/EN/ES). Once the agent has every detail it needs, it puts in a real outbound phone call to the restaurant — speaking Italian, like a normal customer would — negotiates the booking, and reports back to the user what was agreed.

Live demo: **https://shy-order.onrender.com** (Stripe is in test mode — the payment UI is fully wired up, but no real card is ever charged. See [Billing](#billing) below.)

This README is written for someone seeing the project for the first time and is intentionally architecture-first: the goal is to explain *why* each piece exists and how the pieces hand off to each other, not just to list features.

---

## The problem this solves

Most "AI receptionist" demos stop at "the AI can talk." The actual hard part of a restaurant-booking agent is that **the booking only exists once a human at the restaurant has agreed to it on the phone** — most small restaurants have no booking API, no website form, nothing. The only integration surface is a phone line answered by a person.

So the agent has to be two different things depending on who it's talking to:

| | Talking to the user | Talking to the restaurant |
|---|---|---|
| Goal | Collect enough info to make a real reservation | Get a yes/no and the practical details |
| Tone | Warm, reassuring, patient | Confident, brisk, professional |
| Language | Whatever the user speaks (IT/EN/ES) | Always Italian |
| Tools needed | Look up known restaurants, save new ones | Place the call, read back its outcome |

That asymmetry drives most of the architecture below.

---

## High-level flow

```
 Browser (index.html)                FastAPI (main.py)              ElevenLabs Agent           Twilio
 ──────────────────                  ─────────────────              ─────────────────          ──────
 1. Log in / register      ──POST──▶ /auth/login, /auth/register
                                      (Supabase Auth + Stripe customer)

 2. Add a card              ──POST──▶ /payment/setup
    (Stripe Elements)                (Stripe SetupIntent)

 3. "Start" button          ──POST──▶ /session/start
                                      • requires a saved card
                                      • opens a `sessions` row
                                      • fetches a short-lived ElevenLabs
                                        conversation token
                            ◀────────  { signed_url, session_id }

 4. Browser opens a direct WebSocket to ElevenLabs using signed_url
    (audio/text streams browser ⇄ ElevenLabs from here on)
                                                                  │
                                      Agent decides it needs a    │
                                      restaurant's number   ──────┤
                                      POST /tools  ◀───────────── lookup_restaurant
                            (Supabase `restaurants` table)
                                      POST /tools  ◀───────────── make_restaurant_call
                                      • starts a real Twilio call ───────────────────▶ dials restaurant
                                      • blocks (≤60s) polling the                      │
                                        call's status                                 │
                                                            ◀── /twilio/status ────────┤ (status webhook)
                                      Twilio asks "what do I    ◀───────────────────── incoming call leg
                                      say on this call?"
                                      POST /twilio/incoming
                                      • returns TwiML <Connect><Stream>
                                        pointing at the SAME ElevenLabs agent
                                                                                        │
                                      ── the agent is now on a live phone call ────────┘
                                         negotiating with restaurant staff, in Italian

                                      make_restaurant_call returns the final
                                      status to the agent  ─────────▶ agent reports
                                                                       outcome to user

 5. "End" button             ──POST──▶ /session/end
                                      • duration computed server-side
                                      • Stripe charge: minutes × €0.15 + €0.20
```

The detail worth underlining: **the same ElevenLabs agent handles both legs of the conversation.** The browser session and the outbound phone call are two independent WebSocket connections to the same `agent_id` — `/twilio/incoming` (the TwiML webhook Twilio hits when the call connects) fetches a fresh signed URL and streams the call audio to it, exactly like the browser does in step 4. The agent's own prompt is what tells it to behave completely differently depending on which "channel" it's on.

---

## Why a phone call blocks an HTTP request for up to 60 seconds

This is the one piece of the design that looks wrong at first glance, so it's worth justifying explicitly.

`make_restaurant_call` (in [`main.py`](main.py)) does this:

```python
call = _twilio_client.calls.create(...)
deadline = time.time() + 60
while time.time() < deadline:
    time.sleep(3)
    status = _get_call_status(call_sid)
    if status in {"completed", "no-answer", "busy", "failed", "canceled"}:
        return {"success": True, "call_sid": call_sid, "status": status}
return {"success": True, "call_sid": call_sid, "status": "timeout"}
```

The alternative would be: return immediately with just a `call_sid`, and have the agent poll a separate `check_call_status` tool in a loop until the call ends. That's a perfectly valid pattern (it's actually what an earlier version of this prompt assumed) — but it means the LLM has to manage a polling loop itself, which is exactly the kind of multi-turn bookkeeping that's fragile to get right in a system prompt.

Blocking server-side instead means the *backend* — not the LLM — owns the polling loop, and the agent gets a single tool call that returns the actual outcome. The trade-off is that the webhook tool's timeout on the ElevenLabs side has to be configured *longer* than the backend's blocking window, or the platform will cut the tool call off before the backend answers. (`check_call_status` still exists as a tool, scoped to the one edge case where 60 seconds wasn't enough — see the system prompt.)

---

## Components

### `main.py` — the only backend service

One FastAPI app, no internal microservices. Routes group into:

- **Auth** (`/auth/*`) — Supabase Auth for email/password and Google OAuth, plus a Stripe customer created per user on signup.
- **Billing** (`/payment/*`, `/session/start`, `/session/end`) — pay-per-minute via Stripe, see below.
- **Voice session** (`/session/*`) — issues short-lived ElevenLabs tokens, links a conversation back to a session row for analytics.
- **Tools webhook** (`/tools`, `/tools/{tool_name}`) — the only HTTP surface the ElevenLabs agent itself calls. Protected by a shared secret (`TOOLS_WEBHOOK_SECRET`) checked with constant-time comparison.
- **Twilio** (`/twilio/*`) — places the outbound call, serves the TwiML that bridges the call audio to the agent, and receives Twilio's signed status callbacks.
- **Scrape** (`/scrape`) — restaurant info extraction (see below).

There's no job queue and no background worker: the "background" work (waiting for a phone call to finish) happens inside the HTTP request, which is only viable because the wait has a hard ceiling (60s) and the alternative (a queue + websocket push back to the agent) would be a lot of infrastructure for a tool call.

### Restaurant data: structured extraction, not regex

Most independent restaurants don't have a booking API — sometimes not even a phone number anywhere obvious on their website. The original version of `/scrape` used a stack of regexes and `BeautifulSoup` heuristics per field (look for a `tel:` link, then three different phone-number regex patterns, then a class name containing "orari"...). It worked on the two sites it was tested against and broke on anything with a different layout.

It's now a single structured-extraction call: the page is fetched (through an SSRF-guarded fetcher, see below), stripped down to visible text, and handed to an LLM with a forced tool call (`extract_restaurant_info`) that can only return the four fields we need — `name`, `phone_number`, `address`, `hours`, each nullable. This generalizes to layouts no one anticipated, at the cost of one small LLM call per scrape.

### Security notes worth knowing before reading the code

- **Twilio webhooks** (`/twilio/status`, `/twilio/incoming`) verify the `X-Twilio-Signature` header against the exact callback URL — without this, anyone could POST a fake "call completed" status.
- **SSRF guard on `/scrape`**: the target URL's resolved IP is checked against private/loopback/link-local/reserved ranges (including cloud metadata endpoints like `169.254.169.254`) before every fetch, including on every redirect hop — an attacker can't use this endpoint to probe internal infrastructure.
- **Two separate Supabase clients**: a service-role client used for all data access (bypasses RLS by design), and a dedicated anon client used *only* for `sign_in_with_password`. They're kept apart because calling `sign_in_with_password` on the service-role client would silently downgrade its auth header for every concurrent request sharing it — a real bug class with Supabase's Python client, not a hypothetical.
- **Billing amounts are computed server-side** from `started_at`/`ended_at` timestamps stored in Postgres, never trusted from the client.

### Billing

Stripe is configured in **test mode** (`pk_test_…` / `sk_test_…`, confirmed live on the deployed instance via `GET /config`). The full flow — card setup via Stripe Elements, a SetupIntent, a per-minute `PaymentIntent` on session end (`minutes × €0.15 + €0.20`) — runs exactly as it would in production. No real money ever moves; this is the standard way to demo a paid product safely.

---

## Where ElevenLabs' Workflows feature could go next

The system prompt currently does something Workflows is built to make explicit: it runs as **one prompt with two implicit phases** (collect from user → call the restaurant), and the model itself decides when it's done with phase 1 ("as soon as you have confirmed ALL of the following... immediately call `make_restaurant_call`"). That's a judgment call buried in prose, not a deterministic transition.

[ElevenLabs Workflows](https://elevenlabs.io/docs/eleven-agents/customization/agent-workflows) (introduced 2026) model a conversation as an explicit graph: **Subagent nodes** (own prompt, tools, voice, even LLM), connected by **edges** with either an LLM-evaluated condition, a deterministic expression, or an unconditional transition. Mapped onto this project, that's:

```
[Start] → [Subagent: Collect details]  --(all required fields present, expression-evaluated)-->  [Subagent: Call restaurant]  → [End]
              tools: lookup_restaurant        tools: make_restaurant_call, check_call_status
              language: IT/EN/ES              language: forced Italian
              tone: warm, reassuring           tone: confident, brisk
```

The phase transition becomes a checkable condition instead of an instruction the model has to interpret correctly every time, and each subagent's prompt only has to describe one persona instead of two. This wasn't built into the live agent for this round — Workflows graphs are built in the dashboard's visual editor (the API doesn't expose node/edge creation yet), and changing the structure of the one agent wired into the live demo right before showing it felt like the wrong risk to take. The prompt and tool-config bugs found while reviewing it (below) were fixed instead, since those were unambiguous correctness fixes.

### Bugs found and fixed while reviewing the prompt

- `check_call_status` was referenced in the system prompt ("wait silently, then call `check_call_status`...") but the tool was **never actually registered on the agent** — that whole branch of the prompt was dead instruction the model couldn't follow.
- The registered `check_call_status` tool's own webhook config pointed at `GET /tools` (wrong method, wrong path) — it would have failed even if it had been wired up.
- `make_restaurant_call`'s tool timeout was 20 seconds, while the backend can legitimately block for up to 60 seconds waiting for the call to finish — the platform would have aborted the tool call before the backend ever answered.

All three are fixed directly on the live agent (timeout raised to 75s, `check_call_status`'s webhook config corrected and registered, prompt rewritten to match what the backend actually returns — see `##Tools` in the agent's system prompt).

---

## Running locally

```bash
python -m venv .venv
.venv/Scripts/activate   # or source .venv/bin/activate on macOS/Linux
pip install -r requirements.txt
cp .env.example .env     # fill in keys — Stripe test keys, not live
uvicorn main:app --reload
```

Or talk to the agent directly from the terminal, no browser/Twilio involved:

```bash
python main.py --local
```

### Required environment variables

See [`.env.example`](.env.example). Notably:

- `OPENAI_API_KEY` — powers the restaurant-info extraction in `/scrape`.
- `TWILIO_OVERRIDE_TO` — redirects every outbound call to a fixed test number; unset only when actually calling real restaurants.
- `TOOLS_WEBHOOK_SECRET` — shared secret the ElevenLabs agent sends back on every `/tools` call.

## Stack

Python · FastAPI · ElevenLabs Conversational AI · Twilio · Supabase (Postgres + Auth) · Stripe · OpenAI (restaurant-info extraction) · deployed on Render.

A companion analytics dashboard (Next.js, separate repo) reads the same Supabase project.
