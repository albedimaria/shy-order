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
- Tutto il backend è un solo file: [`main.py`](main.py) (~1000 righe). Frontend: `index.html`. Schema: `migrations/`.

---

## 1. Il pitch in 30 secondi (saperlo dire a voce)

> "È un voice agent che prenota tavoli o ordini d'asporto **chiamando davvero il ristorante al
> telefono**, end-to-end, senza umani da nessuna delle due parti. L'utente parla con un'AI nel
> browser (testo o voce, IT/EN/ES); quando ha raccolto l'ordine, il backend lancia un **secondo
> agente** che telefona al ristorante — in italiano, come un cliente — tramite l'integrazione
> **Twilio nativa di ElevenLabs**, e gli passa i dettagli come variabili dinamiche. Il punto
> architetturale: **due agenti, uno per persona** (assistente-sito + chiamante-ristorante), e il
> **backend possiede l'attesa** dell'esito invece di delegarla all'LLM. In produzione su Render,
> billing Stripe a consumo."

Punti che differenziano (dilli se c'è spazio):
- **Due agenti dedicati** → una persona ciascuno; il chiamante è forced-Italian e riceve l'ordine come variabili dinamiche.
- **Twilio nativo di ElevenLabs** (non un bridge media fatto a mano) → ElevenLabs fa il bridge dell'audio; il backend non tocca mai i byte audio.
- **Backend-owns-the-wait**: l'LLM vede una sola tool call con l'esito (un summary), non gestisce un loop.
- **Sicurezza non banale**: SSRF guard per-hop, doppio client Supabase, auth tool constant-time.
- **Latenza instrumentata su ciò che possiedo** (i round-trip backend), non sulla pipeline audio della piattaforma.

---

## 2. Il modello mentale: cos'è una "prenotazione" qui dentro

Il concetto che, capito quello, spiega il 70% del progetto:

> **Due agenti ElevenLabs, una persona ciascuno.** L'**assistente-sito** (`AGENT_ID`,
> [`main.py`](main.py)) parla con l'utente nel browser e raccoglie l'ordine. Il
> **chiamante-ristorante** (`CALLER_AGENT_ID`) telefona al ristorante, in italiano, e riceve i
> dettagli dell'ordine come **variabili dinamiche** all'avvio della conversazione. Sono due
> conversazioni ElevenLabs separate: il backend fa da orchestratore tra le due.

E il secondo pilastro:

> Quando l'assistente-sito ha tutto, chiama il tool `make_restaurant_call`; il backend lancia il
> chiamante via **outbound nativo Twilio di ElevenLabs**, poi **blocca ~55s** facendo polling sullo
> **stato della conversazione del chiamante** e restituisce un solo risultato (un `summary`). L'LLM
> non gestisce nessun loop. ([`make_restaurant_call_tool`, main.py](main.py))

Tutto il resto (auth, billing, scraping, analytics) ruota attorno a questi due fatti.

> ⚠️ **Differenza chiave col dance-voice-agent:** lì l'audio è una pipeline custom (Twilio→Deepgram→
> GPT→ElevenLabs in `asyncio`). **Qui no.** Audio, ASR, TTS, turn-taking **e il bridge telefonico
> Twilio** sono **della piattaforma ElevenLabs** (integrazione nativa). Il backend **non vede mai i
> byte audio**: lancia la chiamata via API e legge l'esito della conversazione. Cambia *tutto* —
> incluso cosa puoi instrumentare (§7).
>
> 🎤 *Lezione vera del progetto:* la prima versione provava a fare il bridge **a mano** con un TwiML
> `<Connect><Stream>` puntato sul WebSocket dell'SDK browser di ElevenLabs — protocolli diversi da
> Twilio Media Streams, **l'audio non passava, l'agente restava muto**. Scoperto con una telefonata
> di test reale; risolto migrando all'integrazione nativa. Ottimo aneddoto: "ho debuggato perché la
> chiamata si connetteva ma l'agente non parlava, e ho capito che era un mismatch di protocollo".

---

## 3. Il flusso end-to-end (la storia di una prenotazione)

### Canale A — sessione utente (browser)
1. L'utente fa login/registrazione → Supabase Auth + cliente Stripe creato ([`/auth/register`, main.py](main.py)).
2. Salva una carta via Stripe Elements → `SetupIntent` ([`/payment/setup`, main.py](main.py)).
3. Preme "Start" → [`/session/start` (main.py)](main.py): verifica la carta, apre una riga in `sessions`,
   chiede un **signed URL** effimero all'API ElevenLabs e lo restituisce al browser.
4. Il **browser apre un WebSocket diretto a ElevenLabs** con quel signed URL. Da qui audio/testo
   scorrono **browser ⇄ ElevenLabs** — il backend non è nel mezzo. Il frontend chiama subito
   [`/session/link`](main.py) con il `conversation_id` per l'analytics.

### Canale B — chiamata al ristorante (dentro la sessione)
5. L'assistente-sito cerca il numero: [`lookup_restaurant`](main.py) → tabella `restaurants`.
6. Con tutti i dati, chiama [`make_restaurant_call`](main.py) passando i dettagli prenotazione/ordine.
7. Il backend chiama `POST /v1/convai/twilio/outbound-call` puntando al **chiamante-ristorante**
   (`CALLER_AGENT_ID`), con l'ordine nelle `dynamic_variables`. ElevenLabs piazza la chiamata sul
   **numero Twilio importato** e **fa lui il bridge dell'audio**. In test, `TWILIO_OVERRIDE_TO`
   dirotta il `to_number` sul tuo numero (fai tu da ristorante).
8. Il backend **blocca ~55s** facendo polling sullo **stato della conversazione del chiamante**; a
   `done` restituisce `summary` + `call_successful`. Se ancora in corso → `in_progress` e
   l'assistente-sito richiama una volta `check_call_status(conversation_id)`. Riferisce il summary all'utente.
9. L'utente preme "End" → [`/session/end`](main.py): durata calcolata server-side,
   addebito Stripe `minuti × €0.15 + €0.20` (idempotente).

---

## 4. ⭐ L'architettura portante (LA sezione da padroneggiare)

```
   BROWSER                    FastAPI (main.py)                  ElevenLabs                Restaurant
   ───────                    ─────────────────                  ──────────                ──────────
   login / card / Start ────► /session/start
                                fetch signed_url ──────────────► API
                           ◄─── { signed_url }
   WebSocket ════════════════════════════════════════════════► ASSISTENTE-SITO (AGENT_ID)
   (audio/testo, backend NON nel mezzo)                            │  raccoglie l'ordine
                                                                   │  chiama un tool
   ASSISTENTE ──► POST /tools/make_restaurant_call (dettagli) ─────┘
                                POST /v1/convai/twilio/outbound-call ──► piazza la chiamata ──► dial
                                  agent_id = CHIAMANTE                    (ElevenLabs fa il
                                  dynamic_variables = ordine               bridge dell'audio)
                           ◄──── { conversation_id, callSid }          CHIAMANTE ⇄ ristorante
                                blocca ~55s, polling stato conv.         (italiano, prenota)
                           ◄──── { call_status, call_successful, summary }
   ASSISTENTE ◄── summary ── riferisce l'esito all'utente
```

### Componenti (un solo servizio, niente microservizi; **niente codice Twilio**)

| Gruppo route | Cosa fa | Riferimento |
|---|---|---|
| Auth (`/auth/*`) | Supabase Auth email/password + Google OAuth, crea cliente Stripe | [main.py](main.py) |
| Billing (`/payment/*`, `/session/*`) | carta, SetupIntent, addebito a consumo (idempotente) | [main.py](main.py) |
| Voice session (`/session/*`) | signed URL effimero dell'assistente-sito, link conversazione↔sessione | [main.py](main.py) |
| Tools webhook (`/tools`, `/tools/{name}`) | **unica superficie che l'assistente chiama**; auth a secret; un tool lancia la chiamata | [main.py](main.py) |
| Scrape (`/scrape`) | estrazione info ristorante via LLM (SSRF-guarded) | [main.py](main.py) |

Le route `/twilio/*` **non esistono più**: la telefonia è tutta dentro l'integrazione nativa ElevenLabs.

### 🎤 Se ti chiedono "perché due agenti invece di uno?"

> "Una persona per agente. L'assistente-sito è caldo, multilingue, raccoglie l'ordine; il chiamante
> è un cliente forced-Italian che piazza l'ordine al ristorante. Sono **due conversazioni ElevenLabs
> separate** — non condividono memoria — quindi passo l'ordine al chiamante come **variabili
> dinamiche** all'avvio. Tenerli separati evita un singolo prompt che si biforca su due personae, e
> il chiamante è riusabile e testabile da solo. È la stessa idea dei 'subagent' dei Workflows, fatta
> a mano con il backend come orchestratore."

### 🎤 Se ti chiedono "perché il backend aspetta l'esito invece di far pollare l'agente?"

> "Così il **loop lo possiede il backend, non l'LLM**. `make_restaurant_call` blocca ~55s sullo stato
> della conversazione del chiamante e ritorna **una sola tool call** con il summary. L'alternativa —
> ritornare subito e far pollare l'LLM in loop — scarica sul prompt un bookkeeping multi-turno
> fragile. La finestra di 55s sta sotto il timeout del tool (75s); se la chiamata dura di più (o
> ElevenLabs sta ancora elaborando l'analisi) ritorno `in_progress` e l'agente richiama una volta
> `check_call_status`."

### 🎤 Se ti chiedono "perché non hai usato ElevenLabs Workflows?"

> "Workflows orchestra sotto-agenti **dentro una singola conversazione**: un grafo che la chiamata
> attraversa, con transizioni condizionali (deterministiche o valutate dall'LLM), handoff tra
> sotto-agenti e trasferimento a operatori umani. Qui i miei due agenti **non sono in una
> conversazione sola** — sono due conversazioni separate (browser e telefonata) legate da una tool
> call del backend. Non c'è handoff in-call, quindi un grafo non si applica: il beneficio degli
> 'agenti specializzati' l'ho già a livello di architettura. Lo adotterei subito se il chiamante
> dovesse **trasferire a un umano** a metà chiamata, o se l'assistente-sito avesse **fasi
> deterministiche** da forzare (autentica → raccogli → conferma) dentro la sua conversazione."

---

## 5. Le decisioni tecniche, una per una (il "perché")

### 5.1 Due agenti, una persona ciascuno (§4)
Assistente-sito (`AGENT_ID`) raccoglie; chiamante-ristorante (`CALLER_AGENT_ID`) telefona, forced-Italian,
ordine via variabili dinamiche. Il backend orchestra. **Perché non un agente solo che cambia fase?**
Perché un prompt che si biforca su due personae è fragile, e il chiamante separato è testabile da solo.

### 5.2 Twilio nativo di ElevenLabs, non un bridge a mano (+ il tetto di scalabilità)
`make_restaurant_call_tool` chiama `POST /v1/convai/twilio/outbound-call`; ElevenLabs fa il bridge.
> ⚠️ **La lezione:** la v1 puntava un TwiML `<Connect><Stream>` sul WebSocket dell'SDK browser —
> protocollo incompatibile con Twilio Media Streams, audio mai passato, agente muto. Migrato al nativo.
> ⚠️ **Onestà sulla scalabilità:** le route `/tools/*` sono handler **sync** (`def`) → FastAPI le
> esegue in un **threadpool** (~40 worker). Una chiamata che blocca ~55s tiene un thread; **~40
> concorrenti** esaurirebbero il pool (su Render free il tetto è più basso). A scala: async puro, con
> push dell'esito via post-call webhook di ElevenLabs. Per questo prodotto, la tool call bloccante è
> il trade giusto.

### 5.3 Doppio client Supabase (il bug RLS-downgrade)
[main.py](main.py). Un client **service-role** per tutte le operazioni DB (bypassa RLS), e un
client **anon dedicato solo** a `sign_in_with_password`.
> 🎤 *"Perché due client?"* → "Perché chiamare `sign_in_with_password` sul client service-role
> emette un evento `SIGNED_IN` che **declassa l'header Authorization** del client da service_role al
> JWT dell'utente — e quella mutazione è condivisa tra tutte le richieste concorrenti che usano
> quel client. Risultato: insert protetti da RLS che falliscono in modo silenzioso e non
> deterministico. Tenendo i due client separati, il sign-in non tocca mai lo stato usato per il DB.
> È una classe di bug reale del client Python di Supabase, non un'ipotesi."

### 5.4 SSRF guard rivalidato a ogni hop
[`_assert_safe_scrape_url`, main.py](main.py) + [`_safe_get`, main.py](main.py).
`/scrape` prende un URL arbitrario dall'utente → rischio SSRF. La guardia risolve l'host e rifiuta
IP **private/loopback/link-local/reserved/multicast** (incluso il metadata cloud `169.254.169.254`),
**prima di ogni fetch e a ogni redirect** (i redirect sono seguiti a mano, non da `requests`).
> 🎤 *"Perché rivalidare a ogni redirect?"* → "Perché un host pubblico può fare `302` verso
> `http://169.254.169.254/`. Se validi solo l'URL iniziale e poi lasci che `requests` segua i
> redirect, l'attaccante aggira la guardia. Per questo seguo i redirect manualmente
> (`allow_redirects=False`) e rivalido l'IP a ogni hop."

### 5.5 Telefonia = responsabilità di ElevenLabs (niente webhook Twilio nostri)
Con l'integrazione nativa, ElevenLabs piazza la chiamata sul numero importato e gestisce i suoi
webhook Twilio. Il backend **non ha più endpoint `/twilio/*`** né validazione di firma Twilio: meno
superficie d'attacco e meno codice. (La v1 li aveva, ma erano parte del bridge che non funzionava.)

### 5.6 Auth dei tool webhook (constant-time + l'onestà del fail-open)
[`_check_tools_auth`, main.py](main.py). Header `x-tools-secret` confrontato con
`hmac.compare_digest` (constant-time → no timing attack). Lato ElevenLabs il secret è un **workspace
secret** inviato come header su tutti e 4 i tool.
> ⚠️ **Onestà:** se `TOOLS_WEBHOOK_SECRET` non è impostato, il check **fallisce in apertura**
> (ritorna senza verificare) — comodo per il dev locale, ma significa che la protezione è reale solo
> se la env var è davvero impostata sul deploy. **In produzione lo è** (verificato: senza header → 401).

### 5.7 Estrazione LLM strutturata, non regex (+ l'insight dei `tel:`)
[`_extract_restaurant_info`, main.py](main.py). I siti dei ristoranti hanno layout imprevedibili;
la vecchia versione era una catena di regex/BeautifulSoup che si rompeva su qualsiasi layout nuovo.
Ora: pagina → testo visibile → **forced tool call** OpenAI (`gpt-4.1-mini`) che può restituire solo i
4 campi `name/phone_number/address/hours`.
> ⚠️ **Bug reale risolto (bell'aneddoto):** `get_text()` butta via gli attributi HTML, inclusi gli
> `href`. Un numero in formato E.164 che vive **solo** in un link `tel:+39...` (mentre il testo
> visibile mostra "06 1234 5678") andava perso — e `make_restaurant_call` **pretende** E.164. Fix:
> [`_tel_hrefs`, main.py](main.py) raccoglie gli `href` dei `tel:` a parte e li passa all'LLM come
> hint da preferire. È un debug reale della catena scraping→LLM→telefonata.

### 5.8 Billing server-side + idempotenza
[`/session/end`, main.py](main.py); formula in [`_compute_charge_cents`, main.py](main.py).
La durata è calcolata da `started_at`/`ended_at` **server-side**, mai dal client.
> 🎤 *"Come eviti il doppio addebito?"* → "Due livelli: l'endpoint **bail-a subito se la sessione ha
> già `ended_at`**, e passo a Stripe una **idempotency key** derivata dal `session_id`. Così un retry,
> una seconda tab o un retry di rete riusano lo stesso PaymentIntent invece di addebitare due volte.
> E se l'addebito riesce ma l'update del DB fallisce, **loggo forte** (`session_end_update_failed_after_charge`)
> così l'addebito orfano è recuperabile."

### 5.9 Link analytics via conversation_id (system dynamic variable)
[`tools_by_path`, main.py](main.py) inietta `__conversation_id__`; `make_restaurant_call_tool` lo usa
per scrivere `sessions.restaurant_name`.
> ⚠️ **Bug trovato e risolto in questa fase:** i tool dell'agente chiamano la route **path-based**
> `/tools/{name}`, che **non iniettava** il `conversation_id` (lo faceva solo la route body-based
> `/tools`). Risultato: `sessions.restaurant_name` restava sempre null in produzione. Fix su due lati:
> il backend ora inietta il conversation_id anche nella route path-based, **e** il tool
> `make_restaurant_call` dell'agente è stato configurato per inviarlo via la system dynamic variable
> `system__conversation_id`.

---

## 6. Inventario: endpoint e tool

### Tool dell'assistente-sito (le uniche cose che l'assistente chiama)
| Tool | Timeout | I/O | Note |
|---|---|---|---|
| `lookup_restaurant` | 20s | Supabase | match case-insensitive (`ilike`) prima di chiedere il numero; legge `restaurant_name` |
| `save_restaurant_to_local_db` | 20s | Supabase | get-or-create dopo una prenotazione riuscita |
| `make_restaurant_call` | **75s** | ElevenLabs API | lancia l'outbound nativo verso il chiamante; **blocca ~55s**; ritorna `summary`+`call_successful` o `in_progress` |
| `check_call_status` | 25s | ElevenLabs API | fallback: legge l'esito della conversazione del chiamante via `conversation_id` |

### Endpoint principali
`/auth/*`, `/payment/*`, `/session/{start,end,link}`, `/scrape`, `/tools[/{name}]`, più `/health` e
`/config` (publishable key Stripe). Statici: `/`, `/style.css`. **Nessun endpoint `/twilio/*`** — la
telefonia è dentro ElevenLabs.

---

## 7. ⭐ La latenza che POSSIEDI (l'asse critico, fatto bene)

Questa è la sezione dove dimostri di aver capito il dominio **e** i limiti della tua posizione.

> 🎤 *"Qual è la tua latenza end-to-end?"* → "Domanda giusta, ma qui va spacchettata: **l'audio,
> l'ASR, il TTS e il turn-taking sono di ElevenLabs**, non miei. Non costruisco la pipeline audio come
> nel mio progetto inbound — la piattaforma la possiede. Quindi non instrumento ms che non controllo:
> instrumento la latenza che **possiedo davvero**, cioè i round-trip del backend."

Cosa misuro (tutto via [`_track`, main.py](main.py), che logga JSON **e** persiste in `tool_metrics`):

```
Latenza posseduta dal backend (via _track → tool_metrics):
   ├─ tool:lookup_restaurant        round-trip Supabase                  ~ 50-150ms
   ├─ tool:make_restaurant_call     l'INTERA chiamata (blocca ~55s)      → outcome = call_successful
   ├─ scrape:fetch                  fetch della pagina (SSRF-guarded)    ~ 100-800ms
   ├─ scrape:extract                forced tool call gpt-4.1-mini        ~ 300-900ms (stima)
   └─ elevenlabs_token:session_start fetch signed URL del browser        ~ 100-300ms (stima)

Latenza NON posseduta (di ElevenLabs, fuori dal mio controllo):
   └─ ASR endpointing, TTFT dell'LLM, TTF-chunk del TTS, turn-taking, bridge audio Twilio
```

**Come la espongo:** ogni operazione → una riga in `tool_metrics` (migration 004) con
`tool, duration_ms, outcome, call_sid, conversation_id`. Best-effort: un insert fallito **non rompe
mai** la richiesta che misura ([`_persist_metric`](main.py)). La dashboard Next.js legge quella tabella.

> 🎤 *"E il turn-taking / barge-in?"* → "Quelli li configuro sull'agente **chiamante**, non li scrivo
> io: modello `turn_v2`, eagerness `patient` (non taglio la parola allo staff), `background_voice_detection:
> false` così il rumore di cucina non triggera falsi turni. Sapere quali leve esistono e perché le ho
> settate così è il punto — non fingere di averle implementate."

---

## 8. Osservabilità e stato

- **Logging strutturato JSON** ([`_log`](main.py)): una riga per evento, grep/jq-friendly per il log
  drain di Render. Ogni `except` prima silenzioso ora **logga con contesto** (tool, conversation_id, session_id).
- **Middleware di timing**: una riga `http_request` con `duration_ms` per ogni richiesta.
- **Stato chiamata**: vive nella **conversazione ElevenLabs del chiamante** (status + analysis), che il
  backend interroga via API — niente più tabella `call_statuses` né store in memoria.
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
- **Copertura test parziale**: la suite ([`test_main.py`](test_main.py), 33 test offline) copre la logica
  pura ad alto rischio (E.164, billing, SSRF guard, dynamic-vars del chiamante, auth 401/200), **non** la
  pipeline live. → *Soluzione: test d'integrazione mockando l'API outbound di ElevenLabs.*

🎤 *"Cosa miglioreresti?"* → scegline 2-3 e proponi la soluzione. Mostra maturità.

---

## 10. Glossario lampo (termini che potrebbero usare)

- **Native Twilio outbound**: l'API ElevenLabs (`/v1/convai/twilio/outbound-call`) che piazza la chiamata sul numero importato e fa lei il bridge dell'audio.
- **Signed URL**: URL WebSocket effimero (con token) per connettersi a un agente ElevenLabs senza esporre l'API key.
- **Dynamic variables**: coppie chiave-valore passate all'avvio della conversazione (`conversation_initiation_client_data`), referenziate nel prompt come `{{var}}` — qui portano l'ordine al chiamante.
- **E.164**: formato standard internazionale dei numeri di telefono (`+39…`).
- **SSRF**: Server-Side Request Forgery — far fare al server richieste verso host interni.
- **HMAC**: firma con chiave segreta per autenticare/verificare integrità (qui: auth del tool webhook, constant-time).
- **RLS**: Row-Level Security di Postgres/Supabase.
- **Idempotency key**: chiave che fa sì che una richiesta ripetuta produca un solo effetto (Stripe).
- **Forced tool call / function calling**: l'LLM costretto a rispondere chiamando una funzione con schema fisso.
- **Threadpool (FastAPI)**: dove girano gli handler sync `def`; default ~40 worker.
- **Turn-taking / barge-in / endpointing**: gestione dei turni di parola — qui **di ElevenLabs**, non mia.
- **Dynamic variable (`system__conversation_id`)**: variabile di sistema ElevenLabs iniettabile nei tool.

---

## 11. Note operative / deploy

- **Cold start (Render free):** il container si spegne dopo ~15 min senza traffico HTTP; la prima
  richiesta lo risveglia (~30-60s). Se l'assistente-sito chiama un tool su un'istanza fredda, il tool
  può andare in timeout. Fix: pinger esterno su `/health`. (Stesso problema del dance-voice-agent.)
- **Secret assistente ↔ backend:** `TOOLS_WEBHOOK_SECRET` deve combaciare tra l'env Render e il
  workspace secret ElevenLabs (header `x-tools-secret`). Se non combaciano → tutte le tool call danno 401.
- **`CALLER_AGENT_ID` / `AGENT_PHONE_NUMBER_ID`:** il chiamante e il numero Twilio importato in
  ElevenLabs. Se cambiano, vanno aggiornati (default in `main.py`, override via env).
- **`TWILIO_OVERRIDE_TO`:** in test **dirotta il `to_number`** della chiamata sul tuo numero (fai tu
  da ristorante). Da togliere quando si chiamano ristoranti veri.

---

## 12. Checklist "so disegnarlo alla lavagna"

Prima del colloquio, verifica di saper fare senza guardare:
- [ ] I **due agenti** (assistente-sito + chiamante) e perché separati, non un agente solo (§2, §4).
- [ ] La sequenza browser: `/session/start` → signed URL → WebSocket → `/session/link` (§3).
- [ ] La sequenza chiamata: `make_restaurant_call` → outbound nativo verso il chiamante con i dettagli come dynamic vars → polling esito (§3-4).
- [ ] Perché il bridge a mano (`<Connect><Stream>`) non funzionava — mismatch di protocollo con Twilio Media Streams (§2, §5.2).
- [ ] Perché il backend possiede l'attesa + il tetto di scalabilità del threadpool (§5.2).
- [ ] Il doppio client Supabase e il bug RLS-downgrade che previene (§5.3).
- [ ] La SSRF guard rivalidata a ogni redirect (§5.4).
- [ ] **Cosa possiedi vs cosa è di ElevenLabs** sulla latenza, e cosa instrumenti in `tool_metrics` (§7).
- [ ] 3 punti deboli con la rispettiva soluzione (§9).
