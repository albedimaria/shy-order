# Shy Order

A voice agent that books restaurant tables or takeaway orders **by actually calling the restaurant on the phone**, end to end, with no human in the loop on either side of the call.

The user talks to an AI in the browser (text or voice, IT/EN/ES). Once the agent has every detail it needs, it puts in a real outbound phone call to the restaurant — speaking Italian, like a normal customer would — negotiates the booking, and reports back to the user what was agreed.

Live demo: **https://shy-order.onrender.com** (Stripe is in test mode — the payment UI is fully wired up, but no real card is ever charged. See [Billing](#billing) below.)

---

## How it works — in three lines

One ElevenLabs agent handles both sides of the interaction. The browser session and the outbound phone call to the restaurant are two independent WebSocket connections to the **same `agent_id`**. When Twilio connects the call and asks "what should I say?", the backend fetches a fresh signed URL and streams the phone audio to the same agent — exactly like the browser does. The agent's prompt is what tells it to behave as a warm user-facing assistant in phase 1, and as a confident Italian-speaking caller in phase 2.

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
 ElevenLabs Agent      FastAPI (main.py)          Twilio         Restaurant
 ────────────────      ─────────────────          ──────         ──────────

 POST /tools/lookup_restaurant
                       SELECT restaurants WHERE lower(name) = …
                    ◀─ { found, phone_number, address }

 POST /tools/make_restaurant_call
                       Twilio.calls.create(to, from) ──▶ dials phone number
                       polling loop, ≤60s ◀── POST /twilio/status (signed)
                                            ◀── call connected

                       POST /twilio/incoming ◀── "what TwiML should I return?"
                       returns <Connect><Stream>
                       pointing at the SAME agent_id, fresh signed URL
                                                      WebSocket ────▶ Agent
                                                      (same agent, now on
                                                       the phone, in Italian)
                                                                      │
                                                      call ends ──────┘
                       polling loop returns final status
                    ◀─ { status: "completed" | "no-answer" | "busy" | … }

 agent reports booking outcome to the user
```

The key invariant: **the backend owns the wait, not the agent.** `make_restaurant_call` blocks for up to 60 seconds polling the call status before it returns — the LLM sees a single tool call with a final answer, not a loop it has to manage itself. See [Why a phone call blocks an HTTP request](#why-a-phone-call-blocks-an-http-request-for-up-to-60-seconds) for the full justification.

---

## Key design decisions

| Decision | Why |
|---|---|
| Same agent, two WebSocket channels | Avoids running two separate agents in sync; the prompt's two-phase structure handles persona switching. See [How it works](#how-it-works--in-three-lines). |
| Backend blocks ≤60s instead of agent polling | Keeps multi-turn bookkeeping in Python, not the LLM. See [below](#why-a-phone-call-blocks-an-http-request-for-up-to-60-seconds). |
| LLM-based restaurant extraction (not regex) | Restaurant websites have wildly different layouts; a forced tool call generalises to any of them. See [Restaurant data](#restaurant-data-structured-extraction-not-regex). |
| Two separate Supabase clients | `sign_in_with_password` on a service-role client silently downgrades its auth header for every concurrent request — a real bug class, not hypothetical. See [Security notes](#security-notes-worth-knowing-before-reading-the-code). |

---

## Conversation behavior

### Turn detection and barge-in

ElevenLabs uses its `turn_v2` model for end-of-turn detection — there is no fixed silence threshold; it models natural speech patterns to decide when a speaker has finished. The agent is configured with:

- **`patient` eagerness** — waits for a genuine pause before responding, so it won't cut off the restaurant staff mid-sentence.
- **`speculative_turn: true`** — the agent starts generating its reply while the current speaker is still talking, reducing perceived latency without pre-empting them.
- **`background_voice_detection: false`** — on the restaurant-call leg, kitchen noise and background chatter don't falsely trigger a new user turn; only the main voice (restaurant staff) is tracked.
- **7-second `turn_timeout`** — if audio goes silent for 7 seconds, the agent takes its turn. This is a safety net for dropped audio, not the primary detection mechanism.

### Error handling

| Call status | What happens |
|---|---|
| `completed` | `make_restaurant_call` returns immediately; agent reports what was agreed. |
| `no-answer`, `busy`, `failed`, `canceled` | Same — agent tells the user the restaurant didn't answer and asks whether to retry. |
| `timeout` (call still live after 60s) | Agent says "it's taking a little longer than usual", then calls `check_call_status` once with the returned `call_sid` to get the final outcome before reporting back. |
| Tool HTTP error (5xx from backend) | ElevenLabs surfaces it as a tool error; the `##Guardrails` section of the system prompt catches it and keeps the agent from going off-script. |

---

## Database schema

Four tables, created by [`migrations/`](migrations/):

| Table | Key columns | RLS |
|---|---|---|
| `users` | `id` (UUID, FK → `auth.users`), `email`, `stripe_customer_id` | SELECT own row |
| `sessions` | `id`, `user_id`, `started_at`, `ended_at`, `duration_seconds`, `amount_charged` (€ cents), `stripe_payment_intent_id`, `restaurant_name`, `elevenlabs_conversation_id` | SELECT own sessions |
| `restaurants` | `id` (SERIAL), `name` (UNIQUE, case-insensitive index), `phone_number`, `address`, `call_count` | service-role only |
| `call_statuses` | `call_sid` (PK), `status`, `updated_at` | service-role only |
| `tool_metrics` | `id`, `tool`, `duration_ms`, `outcome`, `call_sid`, `conversation_id`, `created_at` | service-role only |

All backend access goes through a service-role Supabase client (bypasses RLS). A separate anon client is used only for `sign_in_with_password` — see [Security notes](#security-notes-worth-knowing-before-reading-the-code).

---

## ElevenLabs integration

### Tools webhook

The agent calls the backend via four registered webhook tools, all `POST` to `/tools/{tool_name}`:

| Tool | Timeout | What it does |
|---|---|---|
| `lookup_restaurant` | 20s | Case-insensitive lookup in the `restaurants` table before asking the user for a phone number |
| `save_restaurant_to_local_db` | 20s | Upsert a new restaurant after a successful booking |
| `make_restaurant_call` | **75s** | Place a Twilio call and block until it completes (≤60s); returns final status |
| `check_call_status` | 25s | Fallback: look up the call by `call_sid` if `make_restaurant_call` timed out |

The 75s timeout on `make_restaurant_call` is intentional — it has to be longer than the backend's 60s polling window or ElevenLabs will cut the tool call before the backend answers.

Authentication: every inbound `/tools` request is verified with `hmac.compare_digest` against `TOOLS_WEBHOOK_SECRET` (constant-time comparison, set via env var). On the ElevenLabs side the secret is stored as a workspace secret and sent as the `x-tools-secret` header on each tool. **Caveat (honest):** if `TOOLS_WEBHOOK_SECRET` is unset the check fails *open* (`main.py:_check_tools_auth`) — convenient for local dev, but it means the protection is only real when the env var is actually set on the deploy. It is set in production.

### Signed URL flow

`POST /session/start` calls the ElevenLabs API to get a short-lived signed URL, returns it to the browser, and the browser opens a WebSocket directly to ElevenLabs. The backend never touches the audio stream. The same pattern is replicated in `POST /twilio/incoming` to bridge the phone call: Twilio hits the endpoint, FastAPI fetches a new signed URL, and returns a TwiML `<Connect><Stream>` pointing at it.

---

## Why a phone call blocks an HTTP request for up to 60 seconds

This is the one piece of the design that looks wrong at first glance.

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

The alternative would be: return immediately with just a `call_sid`, and have the agent poll a separate `check_call_status` tool in a loop until the call ends. That's a valid pattern — but it means the LLM has to manage a polling loop itself, which is exactly the kind of multi-turn bookkeeping that's fragile to get right in a system prompt.

Blocking server-side means the *backend* — not the LLM — owns the polling loop. The agent gets a single tool call that returns the actual outcome. The trade-off is that the webhook tool's timeout on the ElevenLabs side must be configured longer than the backend's blocking window (`check_call_status` still exists for the one edge case where 60 seconds wasn't enough).

**Where this stops scaling (honest):** the `/tools/*` routes are sync handlers, so FastAPI runs each in a threadpool (default ~40 workers). A blocking restaurant call therefore holds one thread for up to 60s — fine for a demo and realistic call volumes, but ~40 concurrent in-flight calls would exhaust the pool, and on a single small Render instance the practical ceiling is lower. The "correct at scale" answer is the async pattern this design deliberately traded away (return the `call_sid` immediately, push the outcome back via the status webhook). For this product's scale, the simplicity of one blocking tool call is the right trade; past it, you'd switch.

---

## Components

### `main.py` — the only backend service

One FastAPI app, no internal microservices. Routes group into:

- **Auth** (`/auth/*`) — Supabase Auth for email/password and Google OAuth, plus a Stripe customer created per user on signup.
- **Billing** (`/payment/*`, `/session/start`, `/session/end`) — pay-per-minute via Stripe, see below.
- **Voice session** (`/session/*`) — issues short-lived ElevenLabs tokens, links a conversation back to a session row for analytics.
- **Tools webhook** (`/tools`, `/tools/{tool_name}`) — the only HTTP surface the ElevenLabs agent itself calls. Protected by a shared secret checked with constant-time comparison.
- **Twilio** (`/twilio/*`) — places the outbound call, serves the TwiML that bridges the call audio to the agent, and receives Twilio's signed status callbacks.
- **Scrape** (`/scrape`) — restaurant info extraction (see below).

There's no job queue and no background worker: the "background" work (waiting for a phone call to finish) happens inside the HTTP request, which is only viable because the wait has a hard ceiling (60s).

### Observability and latency

Every request gets one structured JSON log line (`method`, `path`, `status`, `duration_ms`); every previously-silent `except` now logs with context (`tool`, `call_sid`, `session_id`) so a failed production call is debuggable after the fact. On top of that, a small `_track` context manager records the latency we actually own — ElevenLabs owns the audio/ASR/TTS pipeline, so the measurable surface is the **backend round-trips**: each tool webhook (`tool:make_restaurant_call` etc., tagged with the Twilio terminal status as `outcome`), the scrape (`scrape:fetch` + `scrape:extract`), and the ElevenLabs token fetch. Each is logged and persisted to `tool_metrics` (one row per op, best-effort — a failed metric insert never breaks the request) for the dashboard to chart.

### Restaurant data: structured extraction, not regex

Most independent restaurants don't have a booking API — sometimes not even a phone number anywhere obvious on their website. The original version of `/scrape` used a stack of regexes and BeautifulSoup heuristics per field. It worked on the two sites it was tested against and broke on anything with a different layout.

It's now a single structured-extraction call: the page is fetched (through an SSRF-guarded fetcher), stripped down to visible text — with `tel:` link targets extracted separately, since `get_text()` drops `href` attributes and would lose the E.164-formatted number — and handed to an LLM with a forced tool call (`extract_restaurant_info`) that can only return the four fields we need: `name`, `phone_number`, `address`, `hours`, each nullable.

---

## Security notes worth knowing before reading the code

- **Twilio webhooks** (`/twilio/status`, `/twilio/incoming`) verify the `X-Twilio-Signature` header against the exact callback URL — without this, anyone could POST a fake "call completed" status.
- **SSRF guard on `/scrape`**: the target URL's resolved IP is checked against private/loopback/link-local/reserved ranges (including cloud metadata endpoints like `169.254.169.254`) before every fetch, including on every redirect hop.
- **Two separate Supabase clients**: a service-role client for all data access, and a dedicated anon client used *only* for `sign_in_with_password`. They're kept apart because calling `sign_in_with_password` on the service-role client silently downgrades its auth header for every concurrent request sharing it — a real bug class with Supabase's Python client, not a hypothetical.
- **Billing amounts are computed server-side** from `started_at`/`ended_at` timestamps stored in Postgres, never trusted from the client.

---

## Billing

Stripe is configured in **test mode** (`pk_test_…` / `sk_test_…`, confirmed live on the deployed instance via `GET /config`). The full flow — card setup via Stripe Elements, a SetupIntent, a per-minute `PaymentIntent` on session end (`minutes × €0.15 + €0.20`) — runs exactly as it would in production. No real money ever moves; this is the standard way to demo a paid product safely.

`/session/end` is **idempotent**: it bails early if the session already has an `ended_at`, and passes a Stripe idempotency key derived from the `session_id`, so a retry, a second tab, or a network retry can't double-charge. The amount is always computed server-side from stored timestamps, never trusted from the client.

---

## Where ElevenLabs' Workflows feature could go next

The system prompt currently does something Workflows is built to make explicit: it runs as **one prompt with two implicit phases** (collect from user → call the restaurant), and the model itself decides when it's done with phase 1. That's a judgment call buried in prose, not a deterministic transition.

[ElevenLabs Workflows](https://elevenlabs.io/docs/eleven-agents/customization/agent-workflows) (introduced 2026) model a conversation as an explicit graph: **Subagent nodes** (own prompt, tools, voice, even LLM), connected by **edges** with either an LLM-evaluated condition, a deterministic expression, or an unconditional transition. Mapped onto this project:

```
[Start] → [Subagent: Collect details]  --(all required fields present)-->  [Subagent: Call restaurant]  → [End]
              tools: lookup_restaurant                                          tools: make_restaurant_call
              language: IT/EN/ES                                               language: forced Italian
              tone: warm, reassuring                                            tone: confident, brisk
```

The phase transition becomes a checkable condition instead of an instruction the model has to interpret correctly every time, and each subagent's prompt only has to describe one persona instead of two. This wasn't built into the live agent for this round — Workflows graphs are built in the dashboard's visual editor (the API doesn't expose node/edge creation yet).

### Bugs found and fixed while reviewing the prompt

- `check_call_status` was referenced in the system prompt but the tool was **never actually registered on the agent** — that whole branch of the prompt was dead instruction the model couldn't follow.
- The registered `check_call_status` tool's own webhook config pointed at `GET /tools` (wrong method, wrong path) — it would have failed even if it had been wired up.
- `make_restaurant_call`'s tool timeout was 20 seconds, while the backend can legitimately block for up to 60 seconds — the platform would have aborted the tool call before the backend ever answered.

All three are fixed directly on the live agent (timeout raised to 75s, `check_call_status` corrected and registered, prompt rewritten to match what the backend actually returns).

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

### Tests

```bash
pip install -r requirements-dev.txt
python -m pytest -q
```

The suite is fully offline (no Supabase/Stripe/Twilio calls — those globals are monkeypatched to the no-I/O branch). It covers the high-risk pure logic: E.164 validation, the billing formula, the SSRF guard (private IP / cloud-metadata / non-http, with `getaddrinfo` mocked), `tel:`/visible-text extraction, the tool param-name handling, and the `/tools` auth 401/200 paths.

### Required environment variables

See [`.env.example`](.env.example). Notably:

- `OPENAI_API_KEY` — powers restaurant-info extraction in `/scrape`.
- `TWILIO_OVERRIDE_TO` — redirects every outbound call to a fixed test number; unset only when actually calling real restaurants.
- `TOOLS_WEBHOOK_SECRET` — shared secret the ElevenLabs agent sends on every `/tools` call; verified server-side with constant-time comparison.

---

## Stack

Python · FastAPI · ElevenLabs Conversational AI · Twilio · Supabase (Postgres + Auth) · Stripe · OpenAI (restaurant-info extraction) · deployed on Render.

A companion analytics dashboard (Next.js, separate repo) reads the same Supabase project.
