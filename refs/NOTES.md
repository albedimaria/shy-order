# Reference — Shy Order redesign (tokens extracted with dembrandt, 19/10/2026)

Inspiration, not a copy. Two worlds: **voice / AI that does not look like tech** (Pi, Sesame, Granola) and **restaurants with character** (Resy, Dishoom). The Infatuation could not be resolved by dembrandt and was dropped.

| Site | Surface / text | Accent | Type | Motion | What to take |
|---|---|---|---|---|---|
| pi.ai | #faf3ea / #000 | forest green #1a4631 | Grenette 22px body (warm serif) + DM Sans UI | 150-500 ms, standard ease | A voice AI that speaks in large, calm, warm type. Nothing techy. |
| sesame.com | #fff / #111 | none, greys only | ABC Diatype 15px + William (serif); footer #261518 | 1.2 s fade, cubic-bezier(.16,1,.3,1) | Extreme restraint: the voice is the product, the page steps aside. The slow fade is the timing we want. |
| granola.ai | #fff / #292929 | pea green #b2c248 | Quadrant display 110-160px + Melange body 24px | 150-700 ms | Huge, expressive display type next to a quiet product. Personality from typography alone. |
| resy.com | #fff / #2a2a2a | tomato #ff462d | Beatrice bold 48px + GT America 18px | 150-500 ms | Restaurant booking: one saturated red, a boxed logo like a stamp, a dense useful layout. |
| dishoom.com | #f0ece0 / #353839 | none | Cheltenham 32-42px everywhere (old signage serif) | 150 ms-1 s | A restaurant brand with a voice: period type used for UI too. Warm, never cute. |

## Patterns
- Voice products with taste do **not** use a glowing gradient orb on black: they use paper, big type and one colour.
- Body text sits at 18-24px across the voice sites (Pi 22, Granola 24). Our 16px floor is comfortable.
- The restaurant world gives the colour (Resy's red, the stamp logo) and the period type (Dishoom).
- Motion: everyone uses 150 ms for hover; the slow 1.2 s expo-out fade appears only on Sesame's entrance. That matches the checklist: entrance at 1.2-1.4 s, hover at 200 ms.

## Defaults to avoid (named before designing)
- The current page: near-black + italic serif + bordeaux orb with glow.
- A gradient orb with blur on dark (Siri/ChatGPT voice), purple/blue.
- Cream + terracotta, near-black + acid green, all-caps mono chrome.
- Inter / DM Sans / a neutral grotesque as the only typeface.
