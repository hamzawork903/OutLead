# Coding Standards — read before writing any code

This project is a free, self-hosted lead-gen + outreach system, all Python + a local Flask web UI:

**Pipeline:** Playwright Google Maps scraper (`main.py` / `batch.py` / `live.py`) → email/social enrichment (`enrichment/`) → rule-based qualifier (`qualifier/`, no LLM) → email sequences with stop-on-reply (`sequencer/`, own SMTP/IMAP — no third-party sender) → SQLite (`leads.db` via `storage/store.py`, the single source of truth) → web UI (`webui/`: Scrape, Outreach, Terminal pages over SSE).

Every rule below is mandatory.

## 1. Email safety — non-negotiable, overrides everything

- **Real sending requires the two-key gate:** `config.SEQUENCE["dry_run"] = False` in code **and** an explicit per-run opt-in (`--live` / the UI's "send for real"). No new feature may add a third path around it.
- **Dry-run is a pure preview.** It renders and logs what it *would* send but never advances, completes, or stops a sequence — previews must never mutate campaign state. (This was violated once and it silently consumed the whole campaign.)
- The suppression list is checked before every send, no exceptions. An unsubscribed address is never contacted again.
- Test sends go **only** to `config.TEST_SEND_ADDRESS` — never to a scraped lead.
- Rate discipline (daily send cap, between-send pacing, scraper daily cap/cooldown) exists to protect the sending address and IP. Never raise or bypass these silently — ask first.
- SMTP/IMAP credentials live only in `.env` (gitignored). Never in code, commits, or logs.

## 2. Scraped data is hostile input

- **Never interpolate RAW scraped text into outgoing emails.** The only copy that reaches a recipient is LLM-written and then validated by `enrichment/llm_writer.py` (structure, length, no URLs/handles, cleaned greeting name — anything off is discarded, falling back to a bare "Hi,"). LLM output is itself untrusted input: it must pass that validator, and the HTML renderer escapes it. Our identity/links come only from `config.OUTREACH_IDENTITY`.
- Emails found on websites can be template placeholders (`youremail@website.com` passed MX checks and burned a real send). Validate through `enrichment/verify.py` — extend its placeholder rules when a new junk pattern appears.
- Validate scraped/external input at the boundary; trust internal data after that — don't re-check everywhere.

## 3. Verify like the user, not like the builder

- "It compiles" and "the endpoint returns 200" are not done. Click through the real flow the way the user will: start a run, **navigate away and back**, resize to a narrow window, feed it messy data.
- The web UI pages are fixed-height app layouts (`h-screen` + `overflow:hidden`) — any layout change must be checked at narrow widths, where the grid stacks and content can become unreachable.
- A safety gate or validation that blocks an action must tell the user **why** in the UI, immediately — a silent refusal buried in a log reads as "nothing happened" and wastes hours.
- Long-running work streams over SSE; pages must reconnect to an in-progress run on load (`/api/status` + buffered `/stream` replay). Don't break that contract.

## 4. Reuse before you write

- **Before writing any new function, search the codebase for an existing one that does the job** (Grep for the verb/noun: `retry`, `slug`, `normalize`, `send`, …). If one exists, call it. If it almost fits, extend it with a parameter — do not write a near-copy. (`live.py` was built almost entirely from existing functions; that's the model.)
- Shared helpers live in `core/`. If two modules need the same logic, move it to `core/`, never paste it twice.
- All SQL lives in `storage/store.py` — no other module writes queries. Schema changes are **additive migrations** in `_MIGRATIONS` (never destructive; existing `leads.db` data must survive every upgrade).
- **Rule of three:** duplicating a small snippet once is acceptable; the third occurrence must be extracted into a shared function.
- Before creating a new top-level module, check for name collisions with packages (`outreach.py` vs `outreach/` made imports ambiguous once — the package had to be renamed to `sequencer/`).

## 5. Keep files small

- **Soft cap: ~200 lines per file.** If an edit would push a file past that, split it by responsibility first (e.g. one file = one stage of the pipeline, one concern).
- Never split just to hit a number — split along a real seam (queries vs. writes, routes vs. rendering, template text vs. sending logic).
- Existing oversized files (`storage/store.py`, `main.py`, `webui/app.py`) should shrink over time: when touching them, prefer extracting the part you're working on into its own module over adding more lines.

## 6. Keep functions small and single-purpose

- One function = one job, describable in one sentence without "and".
- Target ≤ 40 lines and ≤ 4 parameters. Past that, extract helpers or pass a small dataclass (see `core/models.py`).
- No flag parameters that make a function do two different things — write two functions or branch at the call site. (Exception that proves the rule: `dry_run` is the project's one sanctioned mode flag, because preview-vs-real must share one code path to stay honest.)

## 7. Simplicity rules (lean code)

- **YAGNI:** build only what the current task needs. No "might need it later" options, hooks, or config knobs.
- No clever one-liners. Boring, obvious code beats compact code — optimize for the reader, not the writer.
- Delete dead code, commented-out blocks, and unused imports on sight. Git history is the archive.
- Prefer plain functions and dataclasses over classes with one method. A class must hold state that outlives one call to justify existing.
- Flat over nested: use early returns / guard clauses instead of deep `if` pyramids.
- Every tunable (timeout, delay, cap, selector) lives in `config.py` — no other module hardcodes these values.

## 8. Naming

- Names say what a thing is or does: `fetch_place_details`, `leads_missing_email` — never `data2`, `tmp`, `helper`, `process()`.
- Follow PEP 8: `snake_case` functions/variables, `PascalCase` classes, `UPPER_CASE` constants. Match the style already in the file.
- User-facing labels use readable names, never raw internal keys (`marketing_agency` leaked into a dropdown once — map keys to labels at the display layer).

## 9. Errors: fail loud

- Never use bare `except:` or `except Exception: pass`. Catch the specific exception, log it via the logbook, and either recover meaningfully or re-raise.
- Blocking network calls need a **hard external deadline** — `imaplib`/SSL ignored their own `timeout=` params here; the reliable pattern is a daemon thread + `join(timeout=N)` (see `sequencer/inbox.py`).

## 10. Logging (project rule)

- Every feature logs to console **and** file through `core/logbook.py`. New modules get logging wired in from the first commit, not "later".
- Log the decision points (what was skipped and why, counts, dry-run vs. real), not every line executed.
- UI-facing progress goes through `core/events.py` (`emit()`, gated by `LEADSCAN_EMIT_JSON`) so CLI output stays clean and the web UI stays live.

## 11. Comments and docs

- Comments explain **why**, never **what** — the code says what. A comment restating the next line must be deleted.
- One-line docstring on public functions stating what goes in and what comes out. No essay docstrings.

## 12. Git workflow

- **Never commit or push to `main`.** Always create a branch and open a PR; never merge it yourself.
- Small, single-purpose commits with messages that say why, not just what.
- Never commit: `.env`, `leads.db`, `profile/` (live session cookies), `logs/`, `exports/`.
- **Scan the diff for secrets and personal data BEFORE every commit — no exceptions.**
  Run it over the staged changes, not from memory, and look for: API keys and
  tokens, private keys (`BEGIN ... PRIVATE KEY`), passwords, service-account
  JSON, OAuth client secrets, real email addresses, phone numbers, home
  addresses, spreadsheet/document IDs, and absolute paths containing a
  username. A leaked key in git history survives deletion of the file — the
  only real fix is rotating the credential, so the scan happens before the
  commit, never after.
- Business strategy is data, not code. Pricing, target lists, objection
  handling and go-to-market plans get gitignored with a `.example` version
  committed in their place — the repo is public, and that material is worth
  more to a competitor than the code is.

## 13. Definition of done — check before finishing any task

1. No duplicated logic (searched for existing helpers first).
2. No file grew past ~200 lines without being split.
3. New behavior is logged via `core/logbook.py`.
4. No dead code, unused imports, or leftover debug prints.
5. The change was actually run/verified **through the user's real flow** (see rule 3), not just written.
6. Nothing real can send unless both keys of the send gate agree (see rule 1).
