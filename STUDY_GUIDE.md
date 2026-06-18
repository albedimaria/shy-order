# Study Guide — shy-order

> Companion di studio per padroneggiare il progetto in vista del colloquio tecnico
> (Yellowtech, AI Voice Agent Engineer). Non è documentazione per utenti: è materiale d'esame.
> Ogni sezione spiega **cosa** fa il sistema, **perché** è fatto così, e **come** risponderesti a voce.
>
> Lettura consigliata: una passata completa, poi rileggi le sezioni 4 (architettura portante) e
> 7 (latenza che possiedi) finché non le sai disegnare alla lavagna.
>
> Riferimento incrociato: il gemello **dance-voice-agent** è *inbound* con pipeline audio custom;
> shy-order è *outbound* su **piattaforma ElevenLabs**. Saper spiegare la differenza è un punto a favore.

---

## 0. Come usare questa guida

- I termini in `monospace` sono nomi reali nel codice — sappi puntarli a `file:riga`.
- I box **🎤 Se ti chiedono…** sono domande probabili con la risposta pronta in prima persona.
- I box **⚠️ Onestà** sono i punti deboli: meglio ammetterli tu con lucidità che farteli scovare.
- Tutto il backend è un solo file: [`main.py`](main.py) (~1090 righe). Frontend: `index.html`. Schema: `migrations/`.

---

## 1. Il pitch in 30 secondi (saperlo dire a voce)

> "È un voice agent che prenota tavoli o ordini d'asporto **chiamando davvero il ristorante al
> telefono**, end-to-end, senza umani da nessuna delle due parti. L'utente parla con un'AI nel
> browser (testo o voce, IT/EN/ES); quando l'agente ha tutti i dati, fa una **telefonata reale**
> al ristorante via Twilio — in italiano, come farebbe un cliente — tratta la prenotazione e
> riferisce l'esito. Il punto architetturale: **un solo agente ElevenLabs gestisce entrambe le
> conversazioni** (browser e telefono), e il **backend possiede l'attesa** della chiamata invece
> di delegarla all'LLM. È in produzione su Render, con billing Stripe a consumo."

Punti che differenziano (dilli se c'è spazio):
- **Stesso agente, due canali WebSocket** → niente sincronizzazione tra due agenti separati.
- **Backend-owns-the-wait**: l'LLM vede una sola tool call con l'esito, non gestisce un loop di polling.
- **Sicurezza non banale**: firma Twilio, SSRF guard per-hop, doppio client Supabase, auth tool constant-time.
- **Instrumentazione della latenza che possiedo** (i round-trip backend), non di quella della piattaforma.

---

## 2. Il modello mentale: cos'è una "prenotazione" qui dentro

Il concetto che, capito quello, spiega il 70% del progetto:

> **Un solo `AGENT_ID` ElevenLabs** ([`main.py:36`](main.py)) **risponde su due canali WebSocket
> indipendenti**: quello del browser e quello della telefonata al ristorante. Non sono due agenti,
> è lo stesso. La sua *persona* cambia in base alla fase, ma è il medesimo agente.

E il secondo pilastro:

> Quando l'agente decide di chiamare, il **backend blocca la richiesta HTTP fino a 60s** facendo
> polling sullo stato della chiamata, e restituisce all'LLM **un solo risultato con l'esito finale**.
> L'LLM non gestisce nessun loop. ([`make_restaurant_call_tool`, main.py:380](main.py))

Tutto il resto (auth, billing, scraping, analytics) ruota attorno a questi due fatti.

> ⚠️ **Differenza chiave col dance-voice-agent:** lì l'audio è una pipeline custom (Twilio→Deepgram→
> GPT→ElevenLabs in `asyncio`). **Qui no.** L'audio, l'ASR, il TTS e il turn-taking sono **della
> piattaforma ElevenLabs**. Il backend di shy-order **non vede mai i byte audio**: vede solo i
> webhook dei tool e le callback di Twilio. Questo cambia *tutto* — incluso cosa puoi instrumentare (§7).

---

## 3. Il flusso end-to-end (la storia di una prenotazione)

### Canale A — sessione utente (browser)
1. L'utente fa login/registrazione → Supabase Auth + cliente Stripe creato ([`/auth/register`, main.py:610](main.py)).
2. Salva una carta via Stripe Elements → `SetupIntent` ([`/payment/setup`, main.py:752](main.py)).
3. Preme "Start" → [`/session/start` (main.py:774)](main.py): verifica la carta, apre una riga in `sessions`,
   chiede un **signed URL** effimero all'API ElevenLabs e lo restituisce al browser.
4. Il **browser apre un WebSocket diretto a ElevenLabs** con quel signed URL. Da qui audio/testo
   scorrono **browser ⇄ ElevenLabs** — il backend non è nel mezzo. Il frontend chiama subito
   [`/session/link`](main.py) con il `conversation_id` per l'analytics.

### Canale B — chiamata al ristorante (dentro la sessione)
5. L'agente cerca il numero: [`lookup_restaurant`](main.py) → tabella `restaurants`.
6. Con tutti i dati, l'agente chiama [`make_restaurant_call`](main.py): il backend crea una chiamata
   Twilio e **blocca ≤60s** facendo polling ([main.py:433-441](main.py)).
7. Twilio, quando la chiamata si connette, fa `POST` a [`/twilio/incoming` (main.py:950)](main.py):
   il backend prende **un nuovo signed URL per lo stesso `AGENT_ID`** e risponde con TwiML
   `<Connect><Stream>` → l'audio della telefonata viene streammato **allo stesso agente**.
8. La chiamata finisce → le status-callback di Twilio aggiornano `call_statuses` ([`/twilio/status`](main.py)).
   Il loop di polling vede lo stato terminale e lo restituisce all'agente, che riferisce l'esito.
9. L'utente preme "End" → [`/session/end` (main.py:813)](main.py): durata calcolata server-side,
   addebito Stripe `minuti × €0.15 + €0.20`.

---

## 4. ⭐ L'architettura portante (LA sezione da padroneggiare)

```
   BROWSER                       FastAPI (main.py)                 ElevenLabs            Twilio
   ───────                       ─────────────────                 ──────────            ──────
   login / card / Start ───────► /auth, /payment, /session/start
                                   crea sessione, fetch token ───► API ElevenLabs
                              ◄─── { signed_url }
   WebSocket ════════════════════════════════════════════════════► AGENT_ID
   (audio/testo, il backend NON è nel mezzo)                          │
                                                                      │ (l'agente chiama un tool)
   AGENT ──► POST /tools/make_restaurant_call ──► crea chiamata ─────────────────────► dial
                                   blocca ≤60s, polling ◄── POST /twilio/status ◄──────  status
                                   POST /twilio/incoming ◄── "che TwiML?" ◄────────────  connessa
                                   fetch NUOVO token ───► API ElevenLabs
                                   restituisce <Connect><Stream> ──────────────────────► stream
                                                                  AGENT_ID ◄═════════════ audio telefono
                                                                  (LO STESSO agente, ora "al telefono")
                                   loop ritorna lo stato ──► AGENT riferisce all'utente
```

### Componenti (un solo servizio, niente microservizi)

| Gruppo route | Cosa fa | Riferimento |
|---|---|---|
| Auth (`/auth/*`) | Supabase Auth email/password + Google OAuth, crea cliente Stripe | [main.py:610-736](main.py) |
| Billing (`/payment/*`, `/session/*`) | carta, SetupIntent, addebito a consumo | [main.py:752-887](main.py) |
| Voice session (`/session/*`) | signed URL effimero, link conversazione↔sessione | [main.py:774-901](main.py) |
| Tools webhook (`/tools`, `/tools/{name}`) | **unica superficie che l'agente chiama**; auth a secret | [main.py:1048-1072](main.py) |
| Twilio (`/twilio/*`) | crea la chiamata, serve il TwiML-bridge, riceve le status-callback firmate | [main.py:904-997](main.py) |
| Scrape (`/scrape`) | estrazione info ristorante via LLM | [main.py:1013](main.py) |

### 🎤 Se ti chiedono "come fa lo stesso agente a stare su browser e telefono insieme?"

> "Sono **due connessioni WebSocket indipendenti allo stesso `agent_id`**. Il browser ne apre una
> col signed URL che gli do in `/session/start`. Quando parte la telefonata, Twilio chiama il mio
> webhook `/twilio/incoming`, io chiedo a ElevenLabs **un altro** signed URL per lo stesso agente e
> rispondo con un TwiML `<Connect><Stream>` che punta lì. ElevenLabs non distingue 'browser' da
> 'telefono': vede due sessioni. È il **system prompt** che fa cambiare persona all'agente — caldo e
> rassicurante con l'utente, sicuro e sbrigativo in italiano col ristorante."

### 🎤 Se ti chiedono "perché il backend blocca 60s invece di far pollare l'agente?"

> "Perché così il **loop di polling lo possiede il backend, non l'LLM**. L'alternativa — ritornare
> subito un `call_sid` e far chiamare all'agente un tool `check_call_status` in loop — è un pattern
> valido, ma scarica sul modello un bookkeeping multi-turno fragile da scrivere nel prompt. Bloccando
> server-side, l'agente fa **una sola tool call** e riceve l'esito reale. Il prezzo: il timeout del
> tool lato ElevenLabs (75s) dev'essere più lungo della finestra di blocking (60s), altrimenti la
> piattaforma tronca la chiamata prima che il backend risponda. `check_call_status` resta come
> fallback per l'unico caso limite in cui 60s non bastano."

---

## 5. Le decisioni tecniche, una per una (il "perché")

### 5.1 Stesso agente, due canali (§4)
Vedi sopra. La chiave: `/twilio/incoming` ([main.py:950](main.py)) replica esattamente il pattern del
browser — fetch token + WebSocket — solo che il "client" è Twilio invece del browser.

### 5.2 Blocking call vs polling dell'agente (+ il tetto di scalabilità)
[`make_restaurant_call_tool`, main.py:380](main.py); il loop è a [main.py:433-441](main.py).
> ⚠️ **Onestà sulla scalabilità:** le route `/tools/*` sono handler **sync** (`def`), quindi FastAPI
> le esegue in un **threadpool** (default ~40 worker). Una chiamata che blocca 60s tiene occupato
> **un thread** per quel tempo. Va benissimo per la demo e per volumi realistici, ma **~40 chiamate
> concorrenti** esaurirebbero il pool (e su una singola istanza Render free il tetto pratico è più
> basso). La risposta "a scala" è il pattern async che ho deliberatamente evitato per semplicità.
> A colloquio: *"per questo prodotto la semplicità di una tool call bloccante è il trade giusto;
> oltre quella soglia passerei ad async con push dell'esito via webhook."*

### 5.3 Doppio client Supabase (il bug RLS-downgrade)
[main.py:86-102](main.py). Un client **service-role** per tutte le operazioni DB (bypassa RLS), e un
client **anon dedicato solo** a `sign_in_with_password`.
> 🎤 *"Perché due client?"* → "Perché chiamare `sign_in_with_password` sul client service-role
> emette un evento `SIGNED_IN` che **declassa l'header Authorization** del client da service_role al
> JWT dell'utente — e quella mutazione è condivisa tra tutte le richieste concorrenti che usano
> quel client. Risultato: insert protetti da RLS che falliscono in modo silenzioso e non
> deterministico. Tenendo i due client separati, il sign-in non tocca mai lo stato usato per il DB.
> È una classe di bug reale del client Python di Supabase, non un'ipotesi."

### 5.4 SSRF guard rivalidato a ogni hop
[`_assert_safe_scrape_url`, main.py:215](main.py) + [`_safe_get`, main.py:1000](main.py).
`/scrape` prende un URL arbitrario dall'utente → rischio SSRF. La guardia risolve l'host e rifiuta
IP **private/loopback/link-local/reserved/multicast** (incluso il metadata cloud `169.254.169.254`),
**prima di ogni fetch e a ogni redirect** (i redirect sono seguiti a mano, non da `requests`).
> 🎤 *"Perché rivalidare a ogni redirect?"* → "Perché un host pubblico può fare `302` verso
> `http://169.254.169.254/`. Se validi solo l'URL iniziale e poi lasci che `requests` segua i
> redirect, l'attaccante aggira la guardia. Per questo seguo i redirect manualmente
> (`allow_redirects=False`) e rivalido l'IP a ogni hop."

### 5.5 Firma Twilio sui webhook
[`_validate_twilio_sig`, main.py:191](main.py), usato in `/twilio/status` e `/twilio/incoming`.
Senza, chiunque potrebbe POSTare uno stato chiamata falso ("completed") o dirottare il TwiML.

### 5.6 Auth dei tool webhook (constant-time + l'onestà del fail-open)
[`_check_tools_auth`, main.py:206](main.py). Header `x-tools-secret` confrontato con
`hmac.compare_digest` (constant-time → no timing attack). Lato ElevenLabs il secret è un **workspace
secret** inviato come header su tutti e 4 i tool.
> ⚠️ **Onestà:** se `TOOLS_WEBHOOK_SECRET` non è impostato, il check **fallisce in apertura**
> (ritorna senza verificare) — comodo per il dev locale, ma significa che la protezione è reale solo
> se la env var è davvero impostata sul deploy. **In produzione lo è** (verificato: senza header → 401).

### 5.7 Estrazione LLM strutturata, non regex (+ l'insight dei `tel:`)
[`_extract_restaurant_info`, main.py:509](main.py). I siti dei ristoranti hanno layout imprevedibili;
la vecchia versione era una catena di regex/BeautifulSoup che si rompeva su qualsiasi layout nuovo.
Ora: pagina → testo visibile → **forced tool call** OpenAI (`gpt-4.1-mini`) che può restituire solo i
4 campi `name/phone_number/address/hours`.
> ⚠️ **Bug reale risolto (bell'aneddoto):** `get_text()` butta via gli attributi HTML, inclusi gli
> `href`. Un numero in formato E.164 che vive **solo** in un link `tel:+39...` (mentre il testo
> visibile mostra "06 1234 5678") andava perso — e `make_restaurant_call` **pretende** E.164. Fix:
> [`_tel_hrefs`, main.py:491](main.py) raccoglie gli `href` dei `tel:` a parte e li passa all'LLM come
> hint da preferire. È un debug reale della catena scraping→LLM→telefonata.

### 5.8 Billing server-side + idempotenza
[`/session/end`, main.py:813](main.py); formula in [`_compute_charge_cents`, main.py:185](main.py).
La durata è calcolata da `started_at`/`ended_at` **server-side**, mai dal client.
> 🎤 *"Come eviti il doppio addebito?"* → "Due livelli: l'endpoint **bail-a subito se la sessione ha
> già `ended_at`**, e passo a Stripe una **idempotency key** derivata dal `session_id`. Così un retry,
> una seconda tab o un retry di rete riusano lo stesso PaymentIntent invece di addebitare due volte.
> E se l'addebito riesce ma l'update del DB fallisce, **loggo forte** (`session_end_update_failed_after_charge`)
> così l'addebito orfano è recuperabile."

### 5.9 Link analytics via conversation_id (system dynamic variable)
[`tools_by_path`, main.py:1061](main.py) inietta `__conversation_id__`; `make_restaurant_call_tool` lo usa
per scrivere `sessions.restaurant_name`.
> ⚠️ **Bug trovato e risolto in questa fase:** i tool dell'agente chiamano la route **path-based**
> `/tools/{name}`, che **non iniettava** il `conversation_id` (lo faceva solo la route body-based
> `/tools`). Risultato: `sessions.restaurant_name` restava sempre null in produzione. Fix su due lati:
> il backend ora inietta il conversation_id anche nella route path-based, **e** il tool
> `make_restaurant_call` dell'agente è stato configurato per inviarlo via la system dynamic variable
> `system__conversation_id`.

---

## 6. Inventario: endpoint e tool

### Tool dell'agente (le uniche cose che l'agente chiama)
| Tool | Timeout | I/O | Note |
|---|---|---|---|
| `lookup_restaurant` | 20s | Supabase | match case-insensitive (`ilike`) prima di chiedere il numero; legge `restaurant_name` |
| `save_restaurant_to_local_db` | 20s | Supabase | get-or-create dopo una prenotazione riuscita |
| `make_restaurant_call` | **75s** | Twilio | crea la chiamata e **blocca ≤60s**; ritorna lo stato finale |
| `check_call_status` | 25s | Supabase | fallback se `make_restaurant_call` è andato in `timeout` |

> ⚠️ **Tre bug di config dell'agente già corretti** (raccontabili): `check_call_status` era citato nel
> prompt ma **non registrato** sull'agente; il suo webhook puntava a `GET /tools` (metodo/percorso
> sbagliati); `make_restaurant_call` aveva timeout 20s contro i 60s di blocking del backend → la
> piattaforma troncava la chiamata. Tutti e tre risolti sull'agente live (timeout → 75s, tool
> corretto e registrato, prompt riscritto).

### Endpoint principali
`/auth/*`, `/payment/*`, `/session/{start,end,link}`, `/twilio/{call,status,incoming,call-status}`,
`/scrape`, `/tools[/{name}]`, più `/health` e `/config` (publishable key Stripe). Statici: `/`, `/style.css`.

---

## 7. ⭐ La latenza che POSSIEDI (l'asse critico, fatto bene)

Questa è la sezione dove dimostri di aver capito il dominio **e** i limiti della tua posizione.

> 🎤 *"Qual è la tua latenza end-to-end?"* → "Domanda giusta, ma qui va spacchettata: **l'audio,
> l'ASR, il TTS e il turn-taking sono di ElevenLabs**, non miei. Non costruisco la pipeline audio come
> nel mio progetto inbound — la piattaforma la possiede. Quindi non instrumento ms che non controllo:
> instrumento la latenza che **possiedo davvero**, cioè i round-trip del backend."

Cosa misuro (tutto via [`_track`, main.py:268](main.py), che logga JSON **e** persiste in `tool_metrics`):

```
Latenza posseduta dal backend:
   ├─ tool:lookup_restaurant        round-trip Supabase                ~ 50-150ms
   ├─ tool:make_restaurant_call     l'INTERA chiamata (≤60s, blocking) → outcome = stato Twilio
   ├─ scrape:fetch                  fetch della pagina (SSRF-guarded)  ~ 100-800ms
   ├─ scrape:extract                forced tool call gpt-4.1-mini      ~ 300-900ms (stima)
   └─ elevenlabs_token              fetch signed URL (session_start /  ~ 100-300ms (stima)
                                    twilio_incoming)

Latenza NON posseduta (di ElevenLabs, fuori dal mio controllo):
   └─ ASR endpointing, time-to-first-token LLM, time-to-first-chunk TTS, turn-taking
```

**Come la espongo:** ogni operazione → una riga in `tool_metrics` (migration 004) con
`tool, duration_ms, outcome, call_sid, conversation_id`. Best-effort: un insert di metrica fallito
**non rompe mai** la richiesta che sta misurando ([`_persist_metric`, main.py:252](main.py)). La
dashboard Next.js legge quella tabella per i grafici.

> 🎤 *"E il turn-taking / barge-in?"* → "Quelli li configuro sull'agente ElevenLabs, non li scrivo io:
> modello `turn_v2`, eagerness `patient` (non taglio la parola allo staff del ristorante),
> `speculative_turn` per ridurre la latenza percepita, `background_voice_detection: false` sulla
> gamba telefonica così il rumore di cucina non triggera falsi turni. Sapere quali leve esistono e
> perché le ho settate così è il punto — non fingere di averle implementate."

---

## 8. Osservabilità e stato

- **Logging strutturato JSON** ([`_log`, main.py:75](main.py)): una riga per evento, grep/jq-friendly
  per il log drain di Render. Ogni `except` prima silenzioso ora **logga con contesto**
  (tool, call_sid, session_id) — incluso il fetch token fallito in `/twilio/incoming`, che prima era muto.
- **Middleware di timing** ([main.py:568](main.py)): una riga `http_request` con `duration_ms` per ogni richiesta.
- **Stato chiamata**: tabella `call_statuses` (Supabase) con **fallback in memoria** per il dev locale
  ([`_get_call_status`/`_set_call_status`, main.py:297-326](main.py)).
  > ⚠️ Il fallback `_call_statuses_mem` è **single-worker**: con più worker non è condiviso. In prod
  > vince Supabase, quindi è ininfluente; ma sappilo dire.
- **Stato sessione/billing**: tutto in Postgres (`sessions`), niente in memoria di critico.

---

## 9. ⚠️ Punti deboli da saper difendere (onestà = forza)

- **Tetto di scalabilità del blocking** (§5.2): handler sync → ~40 thread → ~40 chiamate concorrenti.
  → *Soluzione: pattern async (ritorna `call_sid`, push esito via webhook) oltre quella soglia.*
- **Tools-auth fail-open** se la env var manca (§5.6). → *Soluzione: fallire chiuso in prod e tenere il
  fail-open solo dietro un flag esplicito di dev; oggi è mitigato dal fatto che il secret è impostato.*
- **Cold start su Render free** (§11). → *Soluzione: pinger esterno su `/health` ogni ~10 min.*
- **Doppio deploy del frontend** (onrender + vercel): l'OAuth Google rimbalza l'utente sulla copia
  Vercel dello stesso frontend. **Funziona** (stessa app, stesso backend), ma è un'incongruenza di
  origine. → *Soluzione: unificare su un solo dominio, dopo aver allineato la allowlist Redirect URL su Supabase.*
- **Niente retry/circuit-breaker** sui tool che toccano servizi esterni. → *Soluzione: retry con
  backoff sui tool idempotenti.*
- **Copertura test parziale**: la suite ([`test_main.py`](test_main.py), 32 test offline) copre la logica
  pura ad alto rischio (E.164, billing, SSRF guard, estrazione param, auth 401/200), **non** la
  pipeline live. → *Soluzione: test d'integrazione su un flusso chiamata con Twilio mockato.*

🎤 *"Cosa miglioreresti?"* → scegline 2-3 e proponi la soluzione. Mostra maturità.

---

## 10. Glossario lampo (termini che potrebbero usare)

- **TwiML**: il dialetto XML con cui istruisci Twilio (`<Connect><Stream>`).
- **Signed URL**: URL WebSocket effimero (con token) per connettersi a un agente ElevenLabs senza esporre l'API key.
- **E.164**: formato standard internazionale dei numeri di telefono (`+39…`).
- **SSRF**: Server-Side Request Forgery — far fare al server richieste verso host interni.
- **HMAC**: firma con chiave segreta per autenticare/verificare integrità (firma Twilio, auth tool).
- **RLS**: Row-Level Security di Postgres/Supabase.
- **Idempotency key**: chiave che fa sì che una richiesta ripetuta produca un solo effetto (Stripe).
- **Forced tool call / function calling**: l'LLM costretto a rispondere chiamando una funzione con schema fisso.
- **Threadpool (FastAPI)**: dove girano gli handler sync `def`; default ~40 worker.
- **Turn-taking / barge-in / endpointing**: gestione dei turni di parola — qui **di ElevenLabs**, non mia.
- **Dynamic variable (`system__conversation_id`)**: variabile di sistema ElevenLabs iniettabile nei tool.

---

## 11. Note operative / deploy

- **Cold start (Render free):** il container si spegne dopo ~15 min senza traffico HTTP; la prima
  richiesta deve risvegliarlo (~30-60s) e il webhook Twilio ha un timeout (~15s) → può cadere.
  Fix: pinger esterno su `/health`. (Stesso problema del dance-voice-agent.)
- **Secret agente ↔ backend:** `TOOLS_WEBHOOK_SECRET` deve combaciare tra l'env Render e il workspace
  secret ElevenLabs (header `x-tools-secret`). Se non combaciano → tutte le tool call danno 401.
- **`TWILIO_OVERRIDE_TO`** ([main.py:131](main.py)): in modalità trial **dirotta ogni chiamata** a un numero
  fisso di test. Da rimuovere solo quando si chiamano ristoranti veri.
- **`PUBLIC_BASE_URL`**: usato per costruire i webhook Twilio e per **ricostruire l'URL su cui validare
  la firma** — deve combaciare con l'URL pubblico reale, o la firma Twilio fallisce.

---

## 12. Checklist "so disegnarlo alla lavagna"

Prima del colloquio, verifica di saper fare senza guardare:
- [ ] Il diagramma dei due canali WebSocket sullo **stesso `AGENT_ID`** + il ruolo di `/twilio/incoming` (§4).
- [ ] La sequenza browser: `/session/start` → signed URL → WebSocket → `/session/link` (§3).
- [ ] La sequenza chiamata: `make_restaurant_call` (blocking) → `/twilio/incoming` → TwiML → status webhook (§3-4).
- [ ] Perché il backend blocca 60s invece di far pollare l'agente + il tetto di scalabilità (§5.2).
- [ ] Il doppio client Supabase e il bug RLS-downgrade che previene (§5.3).
- [ ] La SSRF guard rivalidata a ogni redirect (§5.4).
- [ ] **Cosa possiedi vs cosa è di ElevenLabs** sulla latenza, e cosa instrumenti in `tool_metrics` (§7).
- [ ] 3 punti deboli con la rispettiva soluzione (§9).
