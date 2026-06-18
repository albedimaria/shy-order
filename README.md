# Shy Order

A voice agent that books restaurant tables or takeaway orders **by actually calling the restaurant on the phone**, end to end, with no human in the loop on either side of the call.

The user talks to an AI in the browser (text or voice, IT/EN/ES). Once the agent has every detail it needs, it puts in a real outbound phone call to the restaurant — speaking Italian, like a normal customer would — negotiates the booking, and reports back to the user what was agreed.

Live demo: **https://shy-order.onrender.com** (Stripe is in test mode — the payment UI is fully wired up, but no real card is ever charged. See [Billing](#billing) below.)

> **Note on the phone leg:** the Twilio account is a **trial**, which can only place calls to *verified* numbers. The browser conversation, booking flow, and the outbound-call logic are all fully live; the actual phone call is therefore demoable to a verified number (the `TWILIO_OVERRIDE_TO` env var redirects every call to one fixed verified number for exactly this reason). Going to production is just a paid Twilio number away — no code change.

---

## How it works — in three lines

Two ElevenLabs agents, one job each. A **website assistant** talks to the user in the browser and collects the order. When it's ready, it calls the `make_restaurant_call` tool, and the backend fires a **restaurant-caller agent** at the restaurant's number via ElevenLabs' native Twilio outbound integration — passing the booking details as dynamic variables. That second agent places the call in Italian, handles the back-and-forth, and the backend hands its outcome (a transcript summary) back to the website assistant to relay to the user. ElevenLabs bridges the phone media; the backend never touches audio.

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

## Pipeline

### 1. User session

```
 Browser               FastAPI (main.py)          ElevenLabs
 ───────               ─────────────────          ──────────
 POST /auth/login ──▶  Supabase Auth
 POST /auth/register ▶ Supabase Auth + create Stripe customer

 POST /payment/setup ▶ Stripe SetupIntent (card saved to customer)

 POST /session/start ▶ verify card on file
                       open `sessions` row in Postgres
                       fetch short-lived signed URL ──▶ ElevenLabs API
                    ◀─ { signed_url, session_id }

 WebSocket ────────────────────────────────────────────▶ Agent
 (audio/text streams directly browser ⇄ ElevenLabs;
  FastAPI never touches the audio)

   [agent collects restaurant, date/time, party size, name]
   [agent calls backend tools — see pipeline 2 below]
   [agent reports outcome to user]

 POST /session/end ──▶ compute duration from server timestamps
                       PaymentIntent: minutes × €0.15 + €0.20
```

### 2. Restaurant call (triggered from inside the session)

```
 Website agent         FastAPI (main.py)              ElevenLabs            Restaurant
 ─────────────         ─────────────────              ──────────            ──────────
 POST /tools/lookup_restaurant
                       SELECT restaurants WHERE lower(name)=…
                    ◀─ { found, phone_number, address }

 POST /tools/make_restaurant_call (with booking details)
                       POST /v1/convai/twilio/outbound-call ──▶ places call ──────▶ dials
                         agent_id = RESTAURANT-CALLER agent              (ElevenLabs bridges
                         dynamic_variables = booking details              the media itself)
                    ◀─ { conversation_id, callSid }                    caller agent ⇄ restaurant
                       poll conversation status (≤55s)                  (Italian, places the order)
                    ◀─ { call_status, call_successful, summary }
                       (if still going → call_status:"in_progress";
                        the website agent polls once via check_call_status)

 website agent relays the summary to the user
```

The key invariant: **the backend owns the wait, not the LLM.** `make_restaurant_call` returns a single result (the caller conversation's outcome summary), so the website agent never manages a polling loop. The audio bridging is ElevenLabs' native Twilio integration — not a hand-rolled media stream (see [Bugs found and fixed](#bugs-found-and-fixed)).

---

## Key design decisions

| Decision | Why |
|---|---|
| Two dedicated agents (assistant + caller) | One persona per agent beats one agent branching between "talk to user" and "call restaurant". The caller is forced-Italian, receives the order as dynamic variables, and is reusable/independently testable. |
| ElevenLabs native Twilio (not a custom media bridge) | Twilio Media Streams and ElevenLabs' conversation WebSocket speak different protocols; bridging them by hand is what the old code got wrong. The native integration handles the media. See [Bugs found and fixed](#bugs-found-and-fixed). |
| Backend owns the wait | `make_restaurant_call` returns one result with the call outcome; the website agent never runs a polling loop. |
| LLM-based restaurant extraction (not regex) | Restaurant websites have wildly different layouts; a forced tool call generalises to any of them. See [Restaurant data](#restaurant-data-structured-extraction-not-regex). |
| Two separate Supabase clients | `sign_in_with_password` on a service-role client silently downgrades its auth header for every concurrent request — a real bug class, not hypothetical. See [Security notes](#security-notes-worth-knowing-before-reading-the-code). |

---

## Conversation behavior

### Turn detection and barge-in

ElevenLabs uses its `turn_v2` model for end-of-turn detection — no fixed silence threshold; it models natural speech patterns to decide when a speaker has finished. The **restaurant-caller** agent (the one on the phone) is configured with:

- **`patient` eagerness** — waits for a genuine pause before responding, so it won't cut off the restaurant staff mid-sentence.
- **`background_voice_detection: false`** — kitchen noise and background chatter on the line don't falsely trigger a new turn; only the main voice (the staff member) is tracked.

These are ElevenLabs platform settings on the agent, not something the backend implements — see [Observability and latency](#observability-and-latency) for what the backend *does* own.

### Error handling

| Call status | What happens |
|---|---|
| `call_status: done` + `summary` | The caller conversation finished; `make_restaurant_call` returns its `transcript_summary` and `call_successful`. The website agent relays it to the user. |
| `call_status: in_progress` | The call is still going (or ElevenLabs is still producing the analysis) past the ~55s window. The website agent tells the user it's taking a moment and calls `check_call_status` once with the returned `conversation_id`. |
| Outbound-call API error / bad number | `make_restaurant_call` returns `{success: false, error}`; the website agent's `##Guardrails` keep it from going off-script. E.164 is validated before any call is placed. |

---

## Database schema

Four tables, created by [`migrations/`](migrations/):

| Table | Key columns | RLS |
|---|---|---|
| `users` | `id` (UUID, FK → `auth.users`), `email`, `stripe_customer_id` | SELECT own row |
| `sessions` | `id`, `user_id`, `started_at`, `ended_at`, `duration_seconds`, `amount_charged` (€ cents), `stripe_payment_intent_id`, `restaurant_name`, `elevenlabs_conversation_id` | SELECT own sessions |
| `restaurants` | `id` (SERIAL), `name` (UNIQUE, case-insensitive index), `phone_number`, `address`, `call_count` | service-role only |
| `tool_metrics` | `id`, `tool`, `duration_ms`, `outcome`, `call_sid`, `conversation_id`, `created_at` | service-role only |

(`call_statuses` from migration 002 is now unused — it backed the old hand-rolled Twilio polling, which the native integration replaced. The table is left in place but no longer written.)

All backend access goes through a service-role Supabase client (bypasses RLS). A separate anon client is used only for `sign_in_with_password` — see [Security notes](#security-notes-worth-knowing-before-reading-the-code).

---

## ElevenLabs integration

### Two agents

- **Website assistant** (`AGENT_ID`) — talks to the user in the browser, collects the order, calls `make_restaurant_call`, relays the outcome.
- **Restaurant-caller** (`CALLER_AGENT_ID`) — a separate agent with a customer-calling persona, forced Italian, no tools. It receives the booking details as **dynamic variables** at conversation start and places the call. Created/maintained independently of the assistant.

### Tools webhook

The website assistant calls the backend via webhook tools, all `POST` to `/tools/{tool_name}`:

| Tool | Timeout | What it does |
|---|---|---|
| `lookup_restaurant` | 20s | Case-insensitive lookup in the `restaurants` table before asking the user for a phone number |
| `save_restaurant_to_local_db` | 20s | Upsert a new restaurant after a successful booking |
| `make_restaurant_call` | **75s** | Triggers the native Twilio outbound call to the caller agent; blocks ≤55s then returns the conversation's outcome summary (or `in_progress`) |
| `check_call_status` | 25s | Fallback: fetch the caller conversation's final summary by `conversation_id` |

Authentication: every inbound `/tools` request is verified with `hmac.compare_digest` against `TOOLS_WEBHOOK_SECRET` (constant-time, set via env var). On the ElevenLabs side the secret is a workspace secret sent as the `x-tools-secret` header on each tool. **Caveat (honest):** if `TOOLS_WEBHOOK_SECRET` is unset the check fails *open* (`main.py:_check_tools_auth`) — convenient for local dev, but the protection is only real when the env var is set on the deploy. It is set in production.

### Two ways the agent reaches ElevenLabs audio

- **Browser (website assistant):** `POST /session/start` fetches a short-lived signed URL; the browser opens a WebSocket **directly** to ElevenLabs. The backend never touches the audio.
- **Phone (restaurant-caller):** `make_restaurant_call` calls `POST /v1/convai/twilio/outbound-call`. ElevenLabs places the call through the **imported Twilio number** and bridges the media itself — there is no TwiML or media stream in this codebase. The Twilio account credentials live in ElevenLabs (Phone Numbers import), not in the backend.

---

## Why `make_restaurant_call` blocks the request

`make_restaurant_call` ([`main.py`](main.py)) places the outbound call, then **polls the caller conversation's status for up to ~55s** before returning. If it reaches `done`, it returns the conversation's `transcript_summary` + `call_successful`; otherwise it returns `call_status: "in_progress"` and the website agent polls once more via `check_call_status`.

The alternative — return immediately and let the LLM poll in a loop until the call ends — pushes multi-turn bookkeeping into the system prompt, which is fragile. Blocking server-side means the *backend* owns the wait and the agent gets a single tool call with the outcome. The 55s window sits under the 75s ElevenLabs tool timeout; the `in_progress` + `check_call_status` path covers longer calls (and ElevenLabs' post-call processing delay, during which the summary isn't ready yet).

**Where this stops scaling (honest):** `/tools/*` are sync handlers, so FastAPI runs each in a threadpool (default ~40 workers). A blocking call holds one thread for up to ~55s — fine for a demo and realistic volumes, but ~40 concurrent in-flight calls would exhaust the pool, and on a single small Render instance the practical ceiling is lower. The "correct at scale" answer is fully async (return immediately, deliver the outcome via an ElevenLabs post-call webhook). For this product's scale, the blocking tool call is the right trade; past it, you'd switch.

---

## Components

### `main.py` — the only backend service

One FastAPI app, no internal microservices. Routes group into:

- **Auth** (`/auth/*`) — Supabase Auth for email/password and Google OAuth, plus a Stripe customer created per user on signup.
- **Billing** (`/payment/*`, `/session/start`, `/session/end`) — pay-per-minute via Stripe, see below.
- **Voice session** (`/session/*`) — issues the website assistant's short-lived signed URL, links a conversation back to a session row for analytics.
- **Tools webhook** (`/tools`, `/tools/{tool_name}`) — the only HTTP surface the website assistant calls; one of those tools triggers the outbound restaurant call. Protected by a shared secret checked with constant-time comparison.
- **Scrape** (`/scrape`) — restaurant info extraction (see below).

There is **no Twilio code in the backend** anymore: ElevenLabs' native integration owns the telephony. No job queue or background worker either — the wait for the call to finish happens inside the `make_restaurant_call` request, viable because it has a hard ceiling (~55s).

### Observability and latency

Every request gets one structured JSON log line (`method`, `path`, `status`, `duration_ms`); every previously-silent `except` now logs with context (`tool`, `conversation_id`, `session_id`) so a failed production call is debuggable after the fact. A small `_track` context manager records the latency we actually own — ElevenLabs owns the audio/ASR/TTS/turn-taking, so the measurable surface is the **backend round-trips**: each tool webhook (`tool:make_restaurant_call` tagged with the call outcome), the scrape (`scrape:fetch` + `scrape:extract`), and the signed-URL fetch. Each is logged and persisted to `tool_metrics` (one row per op, best-effort — a failed metric insert never breaks the request) for the dashboard to chart.

### Restaurant data: structured extraction, not regex

Most independent restaurants don't have a booking API — sometimes not even a phone number anywhere obvious on their website. The original version of `/scrape` used a stack of regexes and BeautifulSoup heuristics per field. It worked on the two sites it was tested against and broke on anything with a different layout.

It's now a single structured-extraction call: the page is fetched (through an SSRF-guarded fetcher), stripped down to visible text — with `tel:` link targets extracted separately, since `get_text()` drops `href` attributes and would lose the E.164-formatted number — and handed to an LLM with a forced tool call (`extract_restaurant_info`) that can only return the four fields we need: `name`, `phone_number`, `address`, `hours`, each nullable.

---

## Security notes worth knowing before reading the code

- **`/tools` webhook auth**: every call from the website agent carries `x-tools-secret`, checked with `hmac.compare_digest` (constant-time). Telephony auth is no longer our concern — ElevenLabs owns the Twilio leg, so there are no inbound Twilio webhooks to forge.
- **SSRF guard on `/scrape`**: the target URL's resolved IP is checked against private/loopback/link-local/reserved ranges (including cloud metadata endpoints like `169.254.169.254`) before every fetch, including on every redirect hop.
- **Two separate Supabase clients**: a service-role client for all data access, and a dedicated anon client used *only* for `sign_in_with_password`. They're kept apart because calling `sign_in_with_password` on the service-role client silently downgrades its auth header for every concurrent request sharing it — a real bug class with Supabase's Python client, not a hypothetical.
- **Billing amounts are computed server-side** from `started_at`/`ended_at` timestamps stored in Postgres, never trusted from the client.

---

## Billing

Stripe is configured in **test mode** (`pk_test_…` / `sk_test_…`, confirmed live on the deployed instance via `GET /config`). The full flow — card setup via Stripe Elements, a SetupIntent, a per-minute `PaymentIntent` on session end (`minutes × €0.15 + €0.20`) — runs exactly as it would in production. No real money ever moves; this is the standard way to demo a paid product safely.

`/session/end` is **idempotent**: it bails early if the session already has an `ended_at`, and passes a Stripe idempotency key derived from the `session_id`, so a retry, a second tab, or a network retry can't double-charge. The amount is always computed server-side from stored timestamps, never trusted from the client.

---

## Bugs found and fixed

The big one — found by actually placing an end-to-end test call:

- **The phone leg never bridged audio.** The old `/twilio/incoming` returned a TwiML `<Connect><Stream>` pointing at ElevenLabs' *browser-SDK* conversation WebSocket. But Twilio Media Streams and that WebSocket speak **different protocols**, so the agent received no intelligible audio — the call connected, sat silent, and dropped. (And even if audio had bridged, the phone conversation is separate from the browser one and was never handed the booking details.) Fixed by migrating to ElevenLabs' **native Twilio outbound** integration with a dedicated restaurant-caller agent (see [How it works](#how-it-works--in-three-lines)); the hand-rolled bridge was deleted.
- **`python-multipart` was missing** from `requirements.txt`, so the old Twilio webhooks (`await request.form()`) returned HTTP 500 in production — the call would ring but nothing worked. (Moot now that those webhooks are gone, but it's why the old flow failed silently on Render.)
- Earlier agent-config fixes: `check_call_status` was referenced in the prompt but never registered; its webhook pointed at the wrong method/path; `make_restaurant_call`'s tool timeout was shorter than the backend's wait.

### On ElevenLabs Workflows (and why they're *not* used here)

[ElevenLabs Workflows](https://elevenlabs.io/docs/eleven-agents/customization/agent-workflows) orchestrate sub-agents **within a single conversation**: a graph the call traverses, with conditional transitions (deterministic or LLM-evaluated), sub-agent handoffs, and transfer to human operators.

This project's two agents don't live in one conversation — they're **two separate conversations** (the browser session and the outbound phone call) bridged by a backend tool call. There's no in-conversation handoff or escalation, so a workflow graph doesn't map onto the split. The "specialised agents instead of one mega-agent" benefit that Workflows is built for is already achieved here at the *architecture* level (two agents + backend orchestration) rather than as an in-conversation graph.

Where Workflows *would* earn their place: if the restaurant-caller needed to **transfer to a human** mid-call, or if the website assistant grew **deterministic phases** to gate (authenticate → collect → confirm) inside its own conversation. Neither is a current need.

---

## Running locally

```bash
python -m venv .venv
.venv/Scripts/activate   # or source .venv/bin/activate on macOS/Linux
pip install -r requirements.txt
cp .env.example .env     # fill in keys — Stripe test keys, not live
uvicorn main:app --reload
```

Or talk to the website assistant directly from the terminal, no browser involved:

```bash
python main.py --local
```

### Tests

```bash
pip install -r requirements-dev.txt
python -m pytest -q
```

The suite is fully offline (no Supabase/Stripe/ElevenLabs calls — those globals are monkeypatched to the no-I/O branch). It covers the high-risk pure logic: E.164 validation, the billing formula, the SSRF guard (private IP / cloud-metadata / non-http, with `getaddrinfo` mocked), `tel:`/visible-text extraction, the caller dynamic-variables builder, and the `/tools` auth 401/200 paths.

### Required environment variables

See [`.env.example`](.env.example). Notably:

- `OPENAI_API_KEY` — powers restaurant-info extraction in `/scrape`.
- `CALLER_AGENT_ID` / `AGENT_PHONE_NUMBER_ID` — the restaurant-caller agent and the Twilio number imported into ElevenLabs (sensible defaults in `main.py`).
- `TWILIO_OVERRIDE_TO` — redirects the outbound restaurant call to a fixed test number (you stand in for the restaurant); unset to call real restaurants.
- `TOOLS_WEBHOOK_SECRET` — shared secret the website agent sends on every `/tools` call; verified server-side with constant-time comparison.

---

## Stack

Python · FastAPI · ElevenLabs Conversational AI (two agents + native Twilio outbound) · Supabase (Postgres + Auth) · Stripe · OpenAI (restaurant-info extraction) · deployed on Render.

A companion analytics dashboard (Next.js, separate repo) reads the same Supabase project.
