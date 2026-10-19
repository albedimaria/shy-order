# Shy Order — project rules

- **Render deploys on every push to `main`.** Never push or merge to `main` without Alberto's explicit "vai". UI work goes on `ui/rebrand`.
- The frontend is `index.html` (markup plus an inline module script) and `style.css`. The backend serves only `/` and `/style.css`: put libraries on a CDN with a pinned version, and do not add local assets without a route in `main.py`.
- Keep the JS flow intact: auth, `/session/start` → `Conversation.startSession` with `last_orders`, `/session/link`, `/session/end`, and the single-end guard. The list of IDs the JS needs is in `SPEC.md`.
- Payments are paused (`PAYMENTS_ENABLED` off): do not bring the card screen back into view.

## Checks (the pass bar is in SPEC.md)
```bash
python -m pytest -q
node C:/Users/Alberto/.claude/skills/avoid-ai-design/scripts/detect.mjs index.html style.css
python -m http.server 4322 --bind 127.0.0.1   # static preview: http://127.0.0.1:4322/
```
- Screenshots at 1440×900 and 375×812. Contrast is checked with dembrandt `check_contrast`.
- Before the first review: go through `~/.claude/projects/C--Users-Alberto/memory/site-review-recurring-fixes.md`.
