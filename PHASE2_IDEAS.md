# Phase 2 design notes — ideas salvaged from "Outreach Engine"

A pre-built outreach scaffold (`Outreach Engine/` folder) was reviewed on 2026-07-08
and removed from the project — its core email/call/SMS sending only works through
paid third-party APIs (Instantly.ai, Talkstone, Telnyx, OpenAI) with no free/SMTP
fallback anywhere in the code, and it requires a heavy Docker + Postgres + Redis +
Temporal + Next.js stack. That directly conflicts with Phase 2's constraints: free,
email-only, lightweight (matching this project's plain-Python-script style).

No code was reused. No secrets were present in the folder (all API key fields were
empty strings; the only credential-like value was a hardcoded `outreach_dev_2024`
Postgres dev password in `docker-compose.yml`, standard for local-only container
auth). The design ideas below are worth keeping for Stage 2 (the sequence manager).

## Ideas worth adopting (adapted to be free + lightweight)

1. **Permanent suppression list** (from `dnc_sync.py` + `consent.py`) — a
   "never contact again" table checked before every send, with an audit trail.
   Build as one SQLite table: `email, reason, opted_out_at`. This is the most
   important idea here — non-negotiable for any real outreach tool.

2. **Per-lead sequence state** (from the `CampaignLead` model) — one row per
   lead per sequence tracking `current_step`, `last_contacted_at`,
   `next_action_at`, `status`. This is the shape our outreach state table
   should have in `leads.db`.

3. **Configurable step-delay schedule** — e.g. `[0, 48h, 24h, 72h, 96h]`
   between follow-up emails. Add as a `SEQUENCE` block in `config.py`,
   matching the existing `QUALIFY` / `RATE` / `ENRICH` pattern.

4. **Pre-send checklist, in order** (from `email-orchestrator/SKILL.md` Step 2):
   is it due yet? → valid email syntax? → on the suppression list? → already
   replied/stopped? Build this exact gate before every send.

5. **Retry taxonomy (transient vs. permanent errors)** — already built for the
   scraper in `core/reliability.py` (the `retry()` helper, `TRANSIENT` vs
   `BlockDetected`). Reuse the same pattern for email-sending failures instead
   of writing new retry logic.

6. **Rate/pacing limits** — their spec caps at 50 sends/cycle with a minimum
   delay between calls and respects the provider's daily limit. This is
   already built for the scraper as `RATE` config + `rate_gate()` (Milestone
   4) — reuse directly, sized to whatever free ESP/Gmail limit we pick
   (Gmail ~500/day, Brevo free tier ~300/day).

7. **Stop-on-reply — simplified, no AI cost.** Their version pays OpenAI to
   classify reply sentiment (positive/negative/objection/etc.) and routes
   accordingly. Free version: the moment a REAL human reply lands (not an
   auto-reply/out-of-office), pause the sequence and flag it for manual
   review — no AI needed. Add:
   - a keyword check for "unsubscribe / stop / remove me" → auto-add to the
     suppression list
   - an out-of-office/auto-reply detector (common headers/phrases) so a
     vacation auto-responder doesn't wrongly kill a sequence
   - optional: a cheap LLM sentiment tag later, as a nice-to-have, never a
     requirement

8. **Per-message log table** (from the `emails` model: status, sent_at,
   opened_at, replied_at) — worth mirroring at a smaller scale so every send
   and its outcome is recorded.

## Explicitly NOT adopting

- Docker + Postgres + Redis + Temporal + multi-tenant SaaS architecture — stay
  lightweight: SQLite + plain Python scripts, matching `main.py` / `enrich.py`
  / `qualify.py` / `batch.py`.
- Instantly.ai / Talkstone / Telnyx / OpenAI paid integrations — use free SMTP
  (Gmail app-password, or a free-tier ESP like Brevo/Resend) instead.
- Their B2B "firmographic/seniority" lead scorer (`lead-enricher` skill) — built
  for enterprise SaaS sales using company-size/job-title data we don't have.
  Doesn't fit local-business leads from Google Maps; our own `qualify.py` is
  the right tool for this domain.
- TCPA calling-hours enforcement (`calling_hours.py`) — relevant to phone/SMS
  compliance, not email. Could inspire an optional "only send during business
  hours" politeness feature later, but not a requirement.

## Status: built

Stage 2 is built (`outreach.py` + `sequencer/` package) using the adapted
ideas above: free SMTP sending, the suppression list, the step-delay sequence
engine, and stop-on-reply — all free, no Docker, no paid APIs. See the
"Phase 2" section of README.md for details, usage, and test results.
