# Shy Order — UI redesign

## Goal
The person who would rather not phone the restaurant opens Shy Order and understands within seconds that they can talk, and that an agent then makes the call for them. The redesign makes that idea read as one confident product instead of a minimal dark page. It changes only `index.html` and `style.css`; the flow stays the same.

## Decisions (from the grilling, 19/10/2026)
- **App only, no landing page.** There is still one page with screens (auth / main). No hero with scroll sections. Reason: Alberto chose it. The page is a tool, not a marketing site.
- **Logged-out screen:** name, tagline "essere timidi ha il suo fascino", one plain line on what it does, "Continua con Google". The orb is visible but switched off, so it is clear from the first look that you talk to it. There is no email form: the current UI is Google only, and adding email means new logic before the demo.
- **The orb is the main action.** The restaurant link field becomes secondary: a quiet helper. The data it finds shows in a readable card (name, phone, address, hours) to keep in view while talking. `/scrape` behaves as before; its data is still not sent to the agent.
- **Orb states stay the four we have now** (idle, connecting, active listening/speaking, ending), designed properly. There is no "calling the restaurant" state: the voice fillers already cover the wait.
- **Unchanged:** Google login via Supabase, `/session/start` + `Conversation.startSession` with `last_orders`, `/session/link` and `/session/end`, the single-end guard, the visible AI disclosure in IT/EN/ES and the language selector. The payment screen stays in the code (flag-driven) but never appears while `PAYMENTS_ENABLED` is off.
- **Libraries come from a CDN** (jsdelivr, pinned versions) and fonts come from Google Fonts. Reason: the backend serves only `/` and `/style.css`, so local vendor files would need new routes in `main.py`.
- **Timing:** build on branch `ui/rebrand`. Merge to main before the demo (20/10/2026, 12:00) only if Alberto's voice test on the branch passes and he says go. Otherwise the merge comes after the demo.
- **Aesthetic direction:** 2-3 directions designed for this product (shyness, voice, restaurants). Alberto picks one. No ready-made palettes.

## Chosen direction: B · Sala a lume basso (Alberto, 19/10/2026)
Reference artboard: `refs/directions.html`, section B. Alberto said the palette and the mockup effect are exactly right: the build must match it, not reinvent it.
- **Palette:** background `#13201a` with a radial glow `#24382c` from the bottom; card `#1c2d24` with a border `rgba(243,234,216,.14)`; text `#f3ead8`; secondary text `#c3bba8` (8.8:1 on the background, 7.6:1 on the card); candle amber `#f0b45e`, light core `#ffd9a0`.
- **Type:** Bodoni Moda (titles, logo in italic 500, greeting) + Figtree (text and UI). Google Fonts.
- **Orb = candle.** In idle and logged out it is a dim glow with a thin ring. In connecting it lights up slowly. In listening it breathes slowly (about 3 s per cycle). In speaking it gets brighter and breathes faster, but never under 1.2 s per cycle. In ending it fades. It stays a `<button>` with a visible focus ring.
- **Primary button:** filled cream `#f3ead8` on green, fully rounded. Language selector: pill with "IT ▾" that opens a menu (Popover API) showing only the current language when closed. Account: circular outlined button on the right.
- **Logged-out layout:** centered; header with the logo on the left and the language selector on the right; the candle above the title; title on two lines ("Essere timidi / *ha il suo fascino.*") with the second line in italic.

## Out of scope
- Backend changes (`main.py`), new routes, a landing page, an email login form.
- A new "calling the restaurant" state, or sending the scraped data to the agent.
- Agent prompts and config, the dashboard (separate repo), payments.

## Files and interfaces
- `index.html`: markup plus the inline module script. JS logic is kept; only markup, classes and copy change.
- `style.css`: full rewrite.
- The JS depends on these IDs, which must survive: `profile-area`, `profile-btn`, `profile-dropdown`, `dropdown-name`, `logout-btn`, `logo`, `greeting`, `tagline`, `screen-auth`, `google-btn-text`, `auth-error`, `screen-payment`, `payment-title`, `payment-sub`, `card-element`, `save-card-btn`, `payment-error`, `screen-main`, `restaurant-url`, `scrape-btn`, `scrape-result`, `no-data-hint`, `call-orb`, `call-label`, `ai-disclosure`, `call-error`. It also uses the classes `.lang-btn`, `.screen`, `.hidden` and the orb state classes `connecting / active / listening / speaking`.

## Pass bar
- [ ] `python -m pytest -q` exits 0 (backend untouched, regression guard).
- [ ] An ID check script exits 0: every ID listed above exists exactly once in `index.html`.
- [ ] `node <avoid-ai-design>/scripts/detect.mjs index.html style.css` reports no high-severity tells.
- [ ] Contrast: every text/background pair is ≥ 4.5:1 (≥ 3:1 for text ≥ 24px) and the orb focus ring is ≥ 3:1. Checked with dembrandt `check_contrast` or a script, with results pasted.
- [ ] The minimum font size is 16px for body and input text (no text under 14px anywhere), checked by computed styles in the browser.
- [ ] Screenshots at 1440×900 and 375×812 of: logged out, logged in idle with a scrape result card, orb connecting, listening and speaking (classes forced via JS). Each matches the chosen direction artboard, with no horizontal scroll and nothing overlapping.
- [ ] `prefers-reduced-motion: reduce` gives no transform animation (checked by emulation), and a timeout fallback reveals all content if the animation library fails to load (checked by blocking the CDN).
- [ ] Every point of the `site-review-recurring-fixes` checklist that applies to an app has been gone through, with a note per point: balanced header, language dropdown that shows only the current language, sensible title line breaks, font with personality, slow expo.out animations (1.2-1.4 s), brand scrollbar, copy that does not sound like AI.
- [ ] End to end, done by Alberto on the branch build: Google login, orb, speak, agent answers, end the call. `/session/end` returns 200 and the AI disclosure is visible in IT/EN/ES.
- [ ] `/code-review` on the diff with no open correctness findings.

## How to test the branch before merge
The page hard-codes `BASE = https://shy-order.onrender.com`, and Google OAuth returns to `FRONTEND_URL` (default `https://shy-order.vercel.app`). **Chosen path: the backend runs locally.** On localhost the page uses the same origin (`IS_LOCAL` in `index.html`), so it does not touch Render or CORS:
```bash
FRONTEND_URL=http://localhost:8000 PUBLIC_BASE_URL=http://localhost:8000 ALLOWED_ORIGINS=http://localhost:8000 .venv/Scripts/python -m uvicorn main:app --host 127.0.0.1 --port 8000
```
Open http://localhost:8000. The `.env` file supplies keys, `PAYMENTS_ENABLED` is off, and `TWILIO_OVERRIDE_TO` sends the restaurant call to the test number.
- Verified: `/auth/google` sends Supabase `redirect_to=http://localhost:8000`, and Render's CORS rejects localhost (preflight 400). That is why the local backend is used and the Render backend is not.
- Not verified: whether `http://localhost:8000` is in Supabase's redirect allowlist. If Google login returns to `shy-order.vercel.app` instead of localhost, add `http://localhost:8000/**` under Supabase → Authentication → URL Configuration → Redirect URLs.
