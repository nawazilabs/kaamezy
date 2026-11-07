# Chaukas — Regulatory Compliance Map

> **Owner:** one person now (formerly Member 2 for controls, Member 3 for UI surfaces and deck slide 9)
> **Purpose:** every regulatory requirement we claim → the exact control in Chaukas → where it lives (file / table) → how we prove it (demo scene / test). If a row isn't **Built**, the deck must not claim it as built.
>
> ⚠️ **Verify before slides:** section numbers and requirement wording below are our reading of the regulations. Before anything goes on a slide, check it against the official text (RBI website, MeitY gazette). Say "aligned with", not "certified compliant" — accountability stays with the regulated entity (Paytm / the lender).

**Status legend:** ✅ Built (P0) · 🟡 P1 (if time) · 🔵 P2 / roadmap (deck only, say "planned")

**Regulations in scope**
| Code | Regulation | Why it applies to us |
|------|-----------|----------------------|
| **R** | RBI **FREE-AI** framework (Aug 2025) — Framework for Responsible and Ethical Enablement of AI; 7 sutras, 26 recommendations | AI used by a regulated payments/lending business, touching customer money |
| **L** | RBI **Digital Lending Directions, 2025** (consolidating the 2022 digital lending guidelines) | We make loan offers inside the conversation, and run a personal-loan flow (section 2c) |
| **P** | RBI directive on **storage of payment system data** (2018) | Settlement / payout data is payment data |
| **D** | **DPDP Act 2023** (Digital Personal Data Protection Act) + DPDP Rules | We process merchants' personal data (name, phone, VPA, PAN, bank account) |

---

## 1. RBI FREE-AI — responsible AI

| ID | Requirement | Chaukas control | Where | Proof | Status |
|----|-------------|-----------------|-------|-------|:------:|
| R1 | Customers must know they're dealing with AI | First message of every session: "Main Chaukas hoon, Paytm ka AI assistant" + "AI" badge in chat | `chaukas/compliance/disclosure.py`, `chaukas/gateway/session.py`, UI | Scene 1 | ✅ |
| R2 | Human oversight; customer can reach a human | Handoff with decision brief; intent `human_request` ("insaan se baat karao") always → handoff; "Talk to a human" button | `chaukas/agents/handoff.py`, `handoffs` table, UI | Scene 3 | ✅ |
| R3 | Explainability ("understandable by design") | Every decision carries `reason_code` + human-readable `reason`; lender returns `reason_codes`; brief lists verified / done / refused + why | `audit_log.reason_code`, `chaukas/agents/guardrail.py`, `chaukas/integrations/lending.py` | Scenes 2, 3, 5 | ✅ |
| R4 | Accountability + audit trail | Append-only `audit_log` (DB trigger), `actor` column (agent / merchant / human / system), `ticket_events` | `data/schema.sql`, `chaukas/observability/audit.py` | Scene 5 (UPDATE → ABORT) | ✅ |
| R5 | Safety: limits, fail-safes, kill switch | Deterministic guardrail (10 checks), value + daily limits, `executions_require_allow` trigger, rollback tokens, **`disabled_actions` kill switch** in policy | `chaukas/policy/engine.py`, `config/policy.yaml` | Scenes 2, 4; `tests/test_guardrail.py` | ✅ |
| R6 | Fairness, no discrimination | Simulated lender uses **only cashflow features** — never name, gender, location, language, religion. Test: same cashflow with different name / `preferred_lang` → identical decision | `chaukas/integrations/lending.py`, `tests/test_fairness.py` | Eval; `tests/test_fairness.py` (code reads only cashflow_features; changing name / language / device changes nothing, for all 20 merchants, in the lender and in the guardrail's offer gating) | ✅ |
| R7 | Testing before deployment, red-teaming | Golden set + red-team suite as a merge gate; "0 unsafe actions" hard gate | `eval/`, `data/golden_set.json`, `data/redteam_set.json` | Scene 5 scorecard | ✅ |
| R8 | Monitoring + incident logging | Per-step trace spans (latency, cost, model); every BLOCK on an attack is an audit row | `chaukas/observability/trace.py` | Scene 5 | ✅ (alerting 🔵) |
| R9 | Model governance / inventory | Model per agent in `config/models.yaml`, prompts versioned (`*.v1.txt`), model name recorded in every span | `config/models.yaml`, `prompts/` | Trace panel | ✅ |

## 2. RBI Digital Lending — the loan offer

| ID | Requirement | Chaukas control | Where | Proof | Status |
|----|-------------|-----------------|-------|-------|:------:|
| L1 | **Key Fact Statement (KFS)** with APR, fees, tenure shown **before** the borrower agrees | Offer card shows KFS (mock values); `offers.kfs_shown_at` must be set before `accepted` — **DB CHECK enforces it** | `offers.kfs_json`, `offers.kfs_shown_at`, UI KFS card | Scene 1 | ✅ |
| L2 | Name the regulated lender | `offers.lender_name` shown on the card ("Offered by <Demo NBFC> via Paytm") | `offers.lender_name`, `config/lender_sim.yaml` | Scene 1 | ✅ |
| L3 | Explicit borrower consent; no auto-acceptance | `create_offer` always NEEDS_CONFIRMATION (`CONSENT_REQUIRED`); acceptance only on CONFIRM / button | `chaukas/agents/guardrail.py` check 10 | Scene 1 | ✅ |
| L4 | Credit decision belongs to the regulated lender | Simulated lender decides eligibility + amount; Chaukas only decides **whether and when** to offer | `chaukas/integrations/lending.py`, design B4.1 | Deck slide 11 | ✅ |
| L5 | Grievance redressal contact | Grievance contact in KFS and in the disclosure message | `compliance/disclosure.py`, `kfs_json.grievance_contact` | Scene 1 | ✅ |
| L6 | Borrower can exit the loan in a cooling-off / look-up period | Lender-side, after disbursal — **out of scope** (we only make the offer). ⚠ Our "offer cooling-off" (don't re-offer for 7 days after a decline) is a **different thing** — see decision D14 | — | — | 🔵 |
| L7 | Collect only needed data, with consent | Lender gets cashflow features only; purpose-specific consent `loan_offer` | `consents` table | — | ✅ |

## 2b. Shop insurance (additive, v3.3): same pattern as the loan offer

Chaukas recommends the cover that fits and records the merchant's interest. It never issues a policy, never charges a premium and moves no money. All insurer names, covers and premiums are ILLUSTRATIVE mock values (`config/insurance_sim.yaml`).

| # | Requirement | How we meet it | Where | Test | Status |
|---|-------------|----------------|-------|------|:---:|
| I1 | The merchant knows who offers it and that figures are illustrative | The offer names the insurer, says "illustrative", and shows the grievance contact (kept unmasked as a public contact) | `config/insurance_sim.yaml`, `agents/reply.py` templates | `tests/test_insurance.py` | ✅ (partner and figures are mock) |
| I2 | Explainable recommendation | Deterministic rules over business category, sales, device and PAST support tickets, with reason codes (`CATEGORY_ELECTRONICS`, `DEVICE_TICKET_HISTORY`, ...). The guardrail calls the same function, so offer and permission cannot disagree. No LLM | `integrations/insurance.py`, engine check 7 | rules for all 20 merchants, fairness test | ✅ |
| I3 | Consent per purpose, withdrawal as easy as giving it | Consent purpose `insurance_offer`; the offer needs the merchant's own yes (check 10); a withdrawn consent BLOCKs with `CONSENT_WITHDRAWN`, final | `compliance/consent.py`, `policy/engine.py` | `tests/test_insurance.py`, red-team RT-N1, RT-N5 | ✅ |
| I4 | No mis-selling | Support first (open issue, pending KYC, open handoff all block), one product per conversation, 7-day cooling-off after a no, no second lead for the same product, only the recommended product can be offered | engine check 7, `insurance_leads` | red-team RT-N2..N9, golden I01-I05 | ✅ |
| I5 | Insurance distribution is regulated (IRDAI): a licensed insurer or broker must issue and advise | Prototype = recommendation + interest record only; the licensed partner follows up. **Verify the exact distribution rules before any claim on slides** | roadmap | none | 🔵 |

Safe to say: "Chaukas suggests the cover that fits the shop and never buys it for the merchant: it asks first, records interest, and the licensed insurer takes it from there." Never say "IRDAI-compliant" or "certified".

## 2c. Consumer flows: personal loan, health insurance, assistant (additive)

Code: `chaukas/consumer/` · rules: `config/consumer.yaml` · facts: `config/consumer_kb.yaml` · tables: `data/consumer_schema.sql` · tests: `tests/test_consumer.py`, `tests/test_consumer_assistant.py`. Insurer, lender, DigiLocker and disbursal are mocks; every number is illustrative.

**Personal loan** (same Digital Lending ideas as section 2, now with a server-side clock and two consents)

| ID | Requirement | Chaukas control | Where | Proof | Status |
|----|-------------|-----------------|-------|-------|:------:|
| CL1 | KFS shown **before** the borrower agrees | The screen calls `kfs-shown` when the Key Facts are on display; the **server** stamps `kfs_shown_at`. Guardrail check 2 BLOCKs `KFS_NOT_SHOWN`; `consumer_loans.kfs_shown_at` is NOT NULL | `consumer/api.py`, `guardrail.check_loan_accept` | `test_no_loan_before_the_key_facts_were_shown` | ✅ |
| CL2 | Every cost, and the lender's name, shown up front; no bundling | KFS lists lender, APR, EMI, interest, processing fee + GST, amount received, total repaid, cooling-off, grievance contact, and `insurance_required: false` | `engine.build_kfs` | `test_take_a_loan_end_to_end` | ✅ |
| CL3 | Explicit consent, per purpose, no auto-acceptance | Two separate consents (`loan_offer`, `credit_check`) both required (check 3); rows in append-only `consumer_consents` | guardrail check 3, `store.consent` | `test_no_loan_without_both_consents_or_identity` | ✅ |
| CL4 | The credit decision belongs to the lender | Simulated lender decides eligibility and amount; Chaukas only decides whether acceptance may proceed | `engine.lender_decide` | `test_lender_caps_the_amount_and_declines_for_plain_reasons` | ✅ (mock lender) |
| CL5 | Fairness | The lender sees only age, income, EMIs, amount, tenure. `LoanApplication` has no name, gender, location or language field | `consumer/models.py` | `test_lender_never_sees_name_gender_location_or_language` | ✅ |
| CL6 | Amount integrity | The accept request carries no amount; the server compares the stored offer with a fresh lender run (`AMOUNT_MISMATCH`) | guardrail check 5 | `test_the_amount_comes_from_the_lender_never_from_the_request` | ✅ |
| CL7 | Big or risky loans need more than a "yes" | One-time code above the auto limit (checked on the server); self-employed above the review limit needs a human; duplicates and more than 2 active loans BLOCK | guardrail checks 6-8 | `test_a_big_loan_needs_the_one_time_code`, `test_at_most_two_active_loans` | ✅ |
| CL8 | Borrower can exit in a cooling-off period | Shown in the KFS and stored (`cooling_off_end`); the exit / prepayment action itself is **not built** | `consumer_loans.cooling_off_end` | — | 🔵 shown, not executed |

**Health insurance** (IRDAI-aligned practice; in real life a licensed insurer must issue and advise: verify the rules before any claim on slides)

| ID | Requirement | Chaukas control | Where | Proof | Status |
|----|-------------|-----------------|-------|-------|:------:|
| CI1 | Honest, explainable recommendation | Deterministic rule engine; the recommendation carries a plain reason; the person picks | `engine.quotes`, `engine.recommend` | `test_recommendation_is_deterministic_and_explained` | ✅ |
| CI2 | Consent to share health details, per purpose | `share_health_data` consent required (check 2) before any purchase | guardrail check 2 | `test_insurance_blocks_bad_input` | ✅ |
| CI3 | Identity without collecting the Aadhaar number | DigiLocker mock: the server keeps a verified flag and the last 4 digits; the card photo reader ignores any Aadhaar number | `store.set_kyc`, `web/docparse.js` | `test_no_policy_without_verified_identity`, `docparse.test.js` | ✅ (mock KYC) |
| CI4 | No mis-selling | The assistant cannot buy; purchase only through the guardrail; the loan flow never requires insurance and a policy never changes loan approval, amount or rate | `consumer/guardrail.py` | `test_a_reply_that_adds_facts_promises_or_leaks_is_replaced_by_the_knowledge_base` | ✅ |
| CI5 | Underwriting review for risky declarations | Heart condition above the cover limit, cancer history, or a member above the age limit needs a human; the approval is bound to the exact quote and insurer (no bait-and-switch) | guardrail check 7, `consumer_handoffs` | `test_a_human_approval_is_bound_to_the_exact_insurer` | ✅ (mock human) |
| CI6 | Proposer eligibility is a final BLOCK | Age outside the range BLOCKs; no OTP or human can override | guardrail check 3 | `test_proposer_age_is_a_final_block` | ✅ |
| CI7 | Nominee and holder details captured, masked at rest | Nominee, holder and PAN stored masked only | `store`, `api.insurance_purchase` | `test_no_raw_pii_is_stored_anywhere` | ✅ |

**Assistant (LLM) and privacy**

| ID | Requirement | Chaukas control | Where | Proof | Status |
|----|-------------|-----------------|-------|-------|:------:|
| CA1 | The person knows it is AI, and which kind of answer they got | AI line from `config/disclosure.yaml` at chat start; the header says "Live AI model" or "Backup helper (the AI model is unavailable)" | `assistant.disclosure`, `web/flows.js` | `test_the_session_starts_with_the_ai_disclosure` | ✅ |
| CA2 | Answers are grounded | A reply may use only facts in `config/consumer_kb.yaml`: no new number, no rupee amount, no "approved / issued / guaranteed", no ask for OTP / PIN / Aadhaar / PAN, no personal data, right language; otherwise the knowledge-base text is sent | `assistant.reply_problem` | `test_a_reply_that_adds_facts_promises_or_leaks_is_replaced_by_the_knowledge_base` | ✅ |
| CA3 | The model cannot act | Topic maps to one on-screen action in code; an "action" in the model's JSON is ignored | `config/consumer_kb.yaml`, `assistant.answer` | `test_the_model_picks_the_topic_but_code_picks_the_action` | ✅ |
| CA8 | The assistant and the app speak the person's language, without sending their data to a translator | Fixed wording and knowledge-base sentences are translated once at build time (Sarvam Translate) and committed; the browser swaps only exact-match fixed strings, so names, PAN, amounts and chat text are never sent anywhere. A translation is kept only if it keeps every digit and rupee sign, and an assistant sentence may add no number. The reply is still a knowledge-base sentence, never free model wording. **Not native-speaker reviewed: say "machine-translated"** | `scripts/build_i18n.py`, `web/i18n.js`, `config/consumer_kb_i18n.yaml`, `assistant.answer` | `tests/test_i18n.py` | ✅ (review of wording 🔵) |
| CA7 | Chat can fill the loan form, not decide the loan | The model copies out only figures the person stated; code requires each to appear in their message, range-checks it, drops (never clamps) failures, leaves unstated fields empty, and words the reply from the knowledge base. No quote, credit check or execution happens until the person taps "Check my offer"; the amount still comes only from the lender simulator. Audit names fields, never figures | `consumer/prefill.py`, `prompts/consumer_extract.v1.txt`, `web/flows.js` | `tests/test_consumer_prefill.py` | ✅ |
| CA4 | Prompt injection | Screened in code **before** any model call (`config/injection.yaml`), audited as `BLOCK SUSPECTED_INJECTION`; user text is XML-escaped in the prompt | `assistant.answer` | `test_prompt_injection_is_stopped_before_any_model_call` | ✅ |
| CA5 | No raw PII to the model or in storage | `mask_text` before the prompt; only masked turns stored; the audit stores topic and source, never the words | `assistant.answer`, `store.add_turn` | `test_raw_pii_never_reaches_the_prompt_or_storage` | ✅ |
| CA6 | A human is reachable | The `human_request` topic answers and writes `request_human` to the audit log; the consumer agent desk itself is not built | `assistant.answer` | `test_a_human_request_is_logged_for_the_desk` | 🟡 logged, no live desk |
| CP1 | Documents read without uploading them | The photo is read in the browser tab by an open-source OCR engine; a parser extracts name, date of birth, gender, PAN; the person confirms before use. (The OCR engine and model are downloaded as code; the photo is not uploaded) | `web/flows.js`, `web/docparse.js` | `node web/docparse.test.js` | ✅ |
| CP2 | The person can see what the AI did | "Privacy & AI activity" lists every decision from the append-only `consumer_audit` | `GET /api/consumer/activity` | `test_activity_lists_the_decisions_and_never_pii` | ✅ |
| CP3 | Append-only trail, no unlogged execution | Triggers on `consumer_audit`, `consumer_consents`; `consumer_exec_require_allow` | `data/consumer_schema.sql` | `test_audit_log_and_consents_are_append_only`, `test_the_database_refuses_an_execution_without_an_allow_decision` | ✅ |
| CP4 | Only the caller's own data | The user id comes from the Supabase token the server checks on every request; every query is scoped to it | `consumer/auth.py` | `test_other_users_data_is_not_reachable` | ✅ |
| CP5 | Withdraw consent, see and erase my data (DPDP s.6, s.11-12) | Consents are recorded and append-only, but there is **no withdraw button, data summary or erasure** for consumers yet | — | — | 🔵 planned |

Safe to say: "Premiums and loan figures come from a rule engine and a lender, never from the AI. The AI can explain but cannot buy: every purchase passes a deterministic guardrail, and your Aadhaar number never reaches us or the AI." Never say "IRDAI-compliant", "RBI-compliant" or "certified".

## 3. RBI payment data storage

| ID | Requirement | Chaukas control | Where | Status |
|----|-------------|-----------------|-------|:------:|
| P1 | Payment system data stored only in India | DB local (prototype) / India region (prod). LLM routing rule: **raw-ish context only to the India-hosted core model; the fallback model receives masked/tokenised data only.** Verify each provider's hosting region before claiming | `config/models.yaml`, `chaukas/llm/client.py` | ✅ rule · 🔵 prod hosting |

## 4. DPDP Act 2023 — personal data

| ID | Requirement (section — verify) | Chaukas control | Where | Proof | Status |
|----|-------------------------------|-----------------|-------|-------|:------:|
| D1 | **Notice** before processing: what data, why, rights (s.5) | Disclosure message includes a short notice + link; `notice_version` (hash of the exact wording) recorded. Full notice in Hinglish, Hindi, English. Final languages are Hindi, English, Marathi, Hinglish, Gujarati: Marathi and Gujarati carry the AI-disclosure line only (the notice is English), **planned** until a native speaker reviews them. Tamil, Telugu, Malayalam, Bengali and Punjabi were removed; those codes fall back to the full Hindi notice | `compliance/disclosure.py`, `config/disclosure.yaml`, `consents.notice_version` | Scene 1 | ✅ (hi / hi-Latn / en) · 🔵 mr / gu notice translation |
| D2 | **Consent**: free, specific, informed, clear affirmative action (s.6) | Per-purpose consent artefacts (`ai_assistance`, `loan_offer`), append-only history (UPDATE and DELETE both refused by DB triggers); `loan_offer` consent is only valid after the KFS was shown | `consents` table, `compliance/consent.py`, `GET/POST /api/merchants/{id}/consents` | `tests/test_compliance.py` | ✅ `ai_assistance` is granted at session start (gateway), `loan_offer` on accept with the KFS shown (orchestrator) |
| D10 | Proactive outreach: Chaukas may *inform* first, but never *move money* first (s.6 consent before action) | A session started by the system (`sessions.trigger_source = 'proactive'`, read fresh from the DB) can never auto-execute `retrigger_payout` or `create_offer`: check 10 returns NEEDS_CONFIRMATION `CONSENT_REQUIRED` even under the limit. One yes covers one action (same action + identical params). List in `config/policy.yaml` `proactive_consent_required` | `policy/engine.py` check 10, `config/policy.yaml`, `data/schema.sql` | `tests/test_guardrail.py` (proactive), red-team RT-P1..P5, golden P01..P08 | ✅ guardrail + red-team · ⏳ golden cases run once Member 1's event watcher lands · notification opt-in / quiet hours are roadmap |
| D3 | Withdrawal as easy as giving consent (s.6) | "Consent wapas lo" → `withdrawn` row; the guardrail then BLOCKs `create_offer` with `CONSENT_WITHDRAWN` (final, no confirmation can lift it) | `compliance/consent.py`, `policy/engine.py` check 7, `POST /api/merchants/{id}/consents` | red-team RT40, `tests/test_guardrail.py` | ✅ guardrail side · UI toggle (M3) pending |
| D4 | Purpose limitation / data minimisation | Each agent gets only the fields it needs (access matrix, design B7.3); guardrail sees **no identity**; LLM never sees raw cashflow or raw PII | `chaukas/privacy/`, agents | Trace panel | ✅ |
| D5 | Reasonable security safeguards (s.8) | Mask at ingestion, tokenise to model, scrub logs, encrypt at rest (prod), only Action agent detokenises one field | `chaukas/privacy/pii.py`, `scrub.py` | Scene 5, PII-leak test | ✅ (encryption 🔵) |
| D6 | Erase when the purpose is served / retention (s.8) | Retention window for `turns`; audit keeps only masked data | `compliance/retention.py`, `config/policy.yaml retention_days` | `tests/test_retention.py`; run the sweep with `python -m chaukas.compliance.retention` (a scheduler is roadmap) | ✅ (scheduling 🔵) |
| D7 | Rights: access summary, correction, erasure (s.11–12) | Merchant data summary (masked) + erasure request: personal fields blanked, both consents withdrawn, only the legally required records kept, receipt returned | `GET /api/merchants/{id}/data-summary`, `POST /api/merchants/{id}/erasure`, `compliance/retention.py` | `tests/test_retention.py` | ✅ access + erasure · 🔵 correction |
| D8 | Grievance redressal (s.13) | `human_request` → handoff; grievance contact in notice | `agents/handoff.py`, disclosure | Scene 3 | ✅ |
| D9 | Breach notification (s.8) | Prevention: PII-leak test as a gate; notification process = roadmap | `tests/test_privacy.py`, `tests/test_pii_leak.py` (whole system: prompts, wire, API, 11 tables, traces, logs) | Eval | 🔵 (the gate ✅, the notification process is roadmap) |
| D10 | Accuracy of data used for decisions (s.8) | Money state always read fresh, never cached; amount must match ledger | `chaukas/agents/context.py`, guardrail check 5, `tests/test_cache_safety.py` | Scene 4 | ✅ |

---

## 5. Pitch lines (safe to say)

- "Designed to align with **RBI's FREE-AI framework**, **RBI Digital Lending Directions** and the **DPDP Act 2023**."
- "Every money decision is explainable (reason code), auditable (append-only log) and reversible (rollback token)."
- "The most safety-critical agent — the guardrail — sees **no personal data at all**."
- "Loan offers show the Key Fact Statement and the lender's name before consent — the database itself rejects an acceptance without it."
- ❌ Don't say: "RBI-compliant", "certified", "approved". ❌ Don't cite section numbers on slides until verified.

## 6. Who builds what (historical: one person owns everything now)

| Item | Owner |
|------|-------|
| `compliance/disclosure.py` (text per language), `consent.py`, `retention.py` | Member 2 |
| `human_request` intent + handoff | Member 1 |
| KFS fields in `create_offer` + `lender_sim.yaml` (lender name, mock APR/fee/tenure) | Member 1 |
| UI: AI badge, "Talk to a human" button, KFS card on the offer, consent toggle | Member 3 |
| Fairness test, PII-leak test, policy kill switch | Member 2 |
| Deck slide 9 (Safety & compliance) from §5 of this file | Member 3 |
