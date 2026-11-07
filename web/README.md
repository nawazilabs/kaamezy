# Chaukas web frontend

Static files, no build step: `index.html` (shell, auth, merchant chat), `flows.js` + `flows.css` (health insurance and loan flows). It calls the FastAPI backend over HTTPS.

Flow: sign in / sign up (Supabase) -> Paytm-style wallet home (only the "Chaukas Teammate" tile is live)
-> Chaukas landing (link a business or try a sample merchant) -> assistant chat, plus "Behind the scenes"
(trace, agent desk, audit log, red-team, scorecard). On desktop it renders inside a phone frame.

- Auth: Supabase JS from a CDN, using the project's publishable (client-side) key. The login is front-end only; the FastAPI backend does not check it.
- Backend URL: defaults to the Render deployment; override with `?api=https://...` or the "API" link on the landing page (kept in localStorage).
- On `localhost` it talks to `http://localhost:8000`.
- Supabase dashboard: add the deployed URL under Auth -> URL Configuration -> Redirect URLs (needed for confirm and reset links).
- Vercel: Root Directory `web`, Framework Preset `Other`, no build command, no output directory.
- Local: `uvicorn chaukas.main:app` and `python -m http.server 8080 --directory web`.

- Health insurance and loan flows call the backend (`/api/consumer`, CONTRACTS C5b): the server's rule engine prices, the simulated lender decides, a deterministic guardrail gates every policy and loan, and every decision goes to an append-only audit log. The browser sends its Supabase token; the server checks it. If the backend is down the flows show an error (there is no offline fallback). The assistant is still a scripted help bot until the backend LLM agent is connected.
- Document autofill (insurance step 1) runs on the device: the photo is read by Tesseract.js in the browser tab (engine and English model load from jsDelivr, the photo is never uploaded), `docparse.js` extracts name, date of birth, gender and PAN, and an Aadhaar number is detected only to be ignored. Test the parser with `node web/docparse.test.js`.
- Loan: the lender sees only age, income and EMIs; the Key Facts Statement is shown (and timed by the server) before two separate consents; a policy holder can pick "Medical bridge (claim pending)" as the purpose, which never changes approval, amount or rate.
- Deploying: the Render backend must be redeployed with the consumer code and these env vars: `SUPABASE_URL`, `SUPABASE_PUBLISHABLE_KEY`, `CHAUKAS_STAFF_KEY`. Without `SUPABASE_URL` the consumer API runs in `off` mode (one shared demo user), which is only for local development.
