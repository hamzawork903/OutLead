<div align="center">

# 🔴 OutLead

### Find local businesses. Write each one a real email. Never send twice.

**A self-hosted lead-generation and cold-outreach engine.**
Scrapes Google Maps, finds and *verifies* contact emails, scores every lead,
lets an LLM write a personalised 3-email sequence per business, and sends them
on a schedule — stopping the instant someone replies.

Runs entirely on your machine. No SaaS, no seat pricing, no per-lead fees.

![Python](https://img.shields.io/badge/Python-3.11+-3776AB?logo=python&logoColor=white)
![React](https://img.shields.io/badge/React-19-61DAFB?logo=react&logoColor=black)
![Playwright](https://img.shields.io/badge/Playwright-2EAD33?logo=playwright&logoColor=white)
![SQLite](https://img.shields.io/badge/SQLite-003B57?logo=sqlite&logoColor=white)
![License](https://img.shields.io/badge/License-MIT-ff3131)

</div>

---

## 🧭 Why this exists

Most "lead gen" tools sell you a CSV and stop. The hard part isn't finding
businesses — it's everything after: which addresses are real, which leads are
worth contacting, what to actually *say* to each one, and how to follow up
without emailing the same person twice or pestering someone who already
replied.

OutLead is the whole loop, and every stage refuses to do something dumb:

|  | Most scrapers | OutLead |
|---|---|---|
| 📮 Email quality | regex off the page | syntax → MX → placeholder filter → **live mailbox check** |
| ✍️ Copy | one template, mail-merged | **the LLM writes 3 emails per lead** from their own website |
| 🔁 Follow-ups | you track them in a spreadsheet | scheduled state machine, **auto-stops on reply** |
| 🛑 Accidents | one click from a mistake | **two-key send gate**, dry-run previews mutate nothing |

---

## ⚡ The pipeline

```
    🗺️  SCRAPE            🔍  ENRICH            ✅  VERIFY
   Google Maps    →    visit the website   →   syntax · MX · placeholder
   name, phone,        find email, socials,     · live mailbox check
   rating, hours       phones, page text        (dead inboxes never queue)
        │                                              │
        └──────────────────────┬───────────────────────┘
                               ▼
    📊  QUALIFY           ✍️  WRITE             📬  SEND
   rule-based score  →   LLM writes a       →  paced sends, daily cap,
   0–100, no LLM,        3-email sequence      suppression list checked
   instant + free        from THEIR data       before every single send
                               │
                               ▼
                         📥  LISTEN
                   IMAP watches for replies
                   reply    → sequence STOPS, flagged for you
                   unsub    → address suppressed forever
                   auto-OOO → ignored, sequence continues
```

Every stage writes to one SQLite file (`leads.db`) and is **resumable** —
crash, close the laptop, come back tomorrow, nothing is lost or repeated.

---

## 🖥️ The interface

Two surfaces, both local:

**🗺️ Scraper** — a tactical map view. Watch businesses get pinned live as
they're found, with a HUD showing coordinates, zoom and target count. Pick
what to collect (emails, socials, hours, phones), set filters, load a batch
plan from JSON.

**📊 Outreach console** — a React dashboard:

| Page | What it's for |
|---|---|
| **Dashboard** | KPIs, live activity log, and a *Needs attention* feed for replies and stopped leads |
| **Campaigns** | One card per scrape → drill into every lead → open a drawer showing **the exact 3 emails written for that business**, its send history, and stop/suppress controls |
| **Email Sender** | Pick a campaign, a count, and an order — with a confirmation dialog before anything real goes out |
| **Sequences** | Visual timing editor: how many steps, how many days between them |

---

## 🚀 Quick start

```bash
git clone https://github.com/hamzawork903/OutLead.git
cd OutLead

python -m venv .venv && source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -r requirements.txt
playwright install chromium

cp .env.example .env        # then open it — every line is documented
python webui/app.py         # → http://127.0.0.1:5050
```

**First run needs no keys at all.** Scraping works immediately; each optional
key in `.env` unlocks the next stage.

<details>
<summary><b>🎨 Rebuilding the dashboard (only if you change the React code)</b></summary>

```bash
cd frontend
npm install
npm run build     # outputs to webui/dist, served by Flask at /outreach
npm run dev       # or: hot-reload dev server, proxies the API to Flask
```
</details>

<details>
<summary><b>⌨️ Command line</b></summary>

```bash
python main.py "dentists in Austin, TX" --limit 50   # scrape one search
python batch.py --niches "roofers,hvac" --cities "Dallas TX"
python enrich.py                                     # find + verify emails
python qualify.py                                    # score leads
python outreach.py                                   # DRY RUN — prints, sends nothing
python outreach.py --live                            # real send (see safety below)
python live.py "plumbers in Miami, FL" --sync-qualify --sync-send
```
</details>

---

## 🔒 Safety — read this before sending

This software emails real people. The guardrails exist because each one was
earned by a real mistake.

> ### 🔑 The two-key send gate
> A real email requires **both** of these, independently:
> 1. `SEQUENCE["dry_run"] = False` in `config.py` — a deliberate code edit
> 2. `--live` on the CLI, or the explicit "send for real" checkbox in the UI
>
> Either one alone sends nothing. There is no third path, and no button that
> bypasses it.

- **🧪 Dry run is a pure preview.** It renders and logs what it *would* send
  and changes **nothing** — no step advances, no sequence completes. (This was
  violated once and two preview passes silently consumed an entire campaign.)
- **🙅 Placeholder identity blocks live sending.** Until you set your real name
  and booking link in `.env`, live sends are refused outright — a fresh clone
  can never mail strangers signed "Your Name".
- **🚫 The suppression list is checked before every single send.** An
  unsubscribed address is never contacted again, by any code path.
- **🐌 Rate discipline by default** — daily send cap, human-like pacing between
  sends, scraper daily cap and cooldown. These protect your sending domain and
  your IP. Raise them slowly and deliberately.
- **🤖 The AI can't invent links.** It writes a `[BOOK_LINK]` placeholder;
  only your configured URL is ever substituted in. Every generated email is
  validated (length, structure, no URLs or addresses, no fabricated sign-off)
  and **discarded entirely** if anything is off.
- **🌐 A network blip never burns a lead.** Transient failures (DNS, timeouts)
  retry on the next pass; only a genuinely refused address stops a sequence.

---

## ⚖️ Legal — your responsibility

Cold outreach law varies enormously by country, and **you are the sender**.

- **B2B cold email is opt-out in some places and opt-in in others.** It's
  broadly workable in the US (CAN-SPAM) and the UK (PECR, corporate
  subscribers). It effectively requires prior consent in Germany, and consent
  regimes apply across much of the EU. Canada's CASL needs documented implied
  consent. **Check your target country before your first send.**
- **Scraping Google Maps is against Google's Terms of Service.** Use a burner
  Google account, keep the rate limits, and understand you may be blocked.
- Personal data you collect is subject to GDPR/UK-GDPR and similar laws —
  including the right to be forgotten.

The defaults here (opt-out in every footer, permanent suppression, honest
sender identity, conservative pacing) are designed to keep you on the right
side of this — but they are not legal advice, and the responsibility is yours.

---

## 🏗️ Architecture

```
main.py  batch.py  live.py            ← scrape entry points (Playwright)
enrich.py  qualify.py  outreach.py    ← pipeline stages
│
├── core/          browser session, retry/backoff, block detection, logging, events
├── scraper/       search, feed collection, per-listing extraction
├── enrichment/    site harvester, email finder, verifier, LLM email writer
├── qualifier/     rule-based scoring (no LLM, no network, instant)
├── sequencer/     sequence state machine, SMTP mailer, IMAP replies, templates
├── storage/       ALL SQL lives here — additive migrations only
├── webui/         Flask API + the scraper map page
└── frontend/      React + Vite dashboard → built into webui/dist
```

**Design rules** (enforced in `CLAUDE.md`): all SQL in `storage/`, every
tunable in `config.py`, ~200-line file cap, fail loud — never
`except: pass`, and every feature logs through `core/logbook.py`.

---

## 🤝 Contributing

Issues and PRs welcome. Before opening a PR:

1. Read `CLAUDE.md` — it's the coding standard this repo is held to.
2. Never commit `.env`, `leads.db`, `profile/`, `logs/`, or `exports/`.
3. If you touch the send path, say in the PR how you verified nothing can
   send without both keys of the gate.

---

## 📄 License

MIT — see [LICENSE](LICENSE). Use it, sell what you build with it, no warranty.
