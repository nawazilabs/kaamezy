# ui/ (older merchant UI, build output only)

This folder holds the **built output** of the first Lovable merchant UI (`.output/`), its Lovable notes
(`AGENTS.md`, `LOVABLE_PROMPTS.md`) and a change list written for the backend (`BACKEND_CHANGES_FOR_MEMBER_1.md`).
The source (`src/`) is not in this repo, so the build cannot be regenerated from here.

**The live frontend is `web/`** (static files: sign-in, Paytm-style home, health insurance, personal loan, assistant,
merchant chat and agent desk). See `web/README.md`.

`ui/node_modules/` and `ui/.wrangler/` are not tracked (see `.gitignore`). `chaukas/main.py` serves `ui/index.html` only
if one exists; there is none, so nothing here is served.
