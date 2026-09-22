# Hermes - quiet background reader

Hermes is a small, cheap, read-only agent that remembers what Royce is working
on and notices when something stalls. Once a night it reads four small memory
files plus Instinct's task summaries, makes one cheap-model pass, and emails a
stall report to Instinct. Instinct relays it to Royce in the morning.

That's the whole product. Hermes does not build, code, receive mail, or run a
chat app. Royce never talks to Hermes; he talks to Instinct.

```
Royce -> Instinct (all day, ideas and approvals)
Instinct -> inbox/*.md (a 3-line summary after each real task)
Hermes  -> reads memory + inbox at 1am -> one cheap model pass -> stall report
Hermes  -> email -> Instinct -> folded into Royce's morning
```

## The memory files (source of truth)

| file | what it holds |
|---|---|
| `me.md` | who Royce is, how he decides, what never to do without asking |
| `projects.md` | the 5 tracked projects: status, last moved date, next step |
| `decisions.md` | what Royce chose and why, one line each |
| `proposals.md` | next steps waiting on Royce's approval |

Instinct is the only writer of these files (it has the live context). Hermes
only reads them. Royce can read and correct them anytime - they are plain
markdown in this repo.

## The nightly run

At 01:00 America/New_York (configurable in `directives.yaml`):

1. Read the 4 memory files and every `inbox/*.md` summary.
2. Compute the stall signal: days since each project's `Last moved:` date.
   A project is stalled past `stall_days` (default 3).
3. One model pass (`HERMES_MODEL`, default `gpt-4o-mini`) writes the report:
   what moved, what's stalled and why it matters, the obvious next step, and
   the 1-3 suggested focus items.
4. Email the report to Instinct. Done.

Nothing in the report is a work order. Royce replies to Instinct ("go 1") and
Instinct does the work.

## Cost ceiling

- `HERMES_MAX_TOKENS_PER_RUN` caps every single run (default 1500).
- `HERMES_MONTHLY_BUDGET_USD` caps the month (default $1.00). Spend is tracked
  in SQLite (`/data/hermes.db`, the `runs` table) with per-run token counts and
  cost. Hit the cap and Hermes stops making model calls, sends a notice with a
  deterministic no-model report instead, and resumes next month.
- At defaults a full month of nightly runs costs well under $0.10.

## Deploy on the VPS

```bash
git clone https://github.com/roycemy/alter-hermes-runtime.git
cd alter-hermes-runtime
git checkout hermes-reader
cp .env.example .env   # then fill it in
docker compose up -d --build
```

Required in `.env`:

- `OPENAI_API_KEY` - the model brain. Cheap tier is correct; this is one small
  call per night.
- `HERMES_EMAIL_ADDRESS`, `SMTP_HOST`, `SMTP_USERNAME`, `SMTP_PASSWORD` - a
  dedicated Hermes mailbox used ONLY to send the nightly report (Gmail with an
  app password works). There is deliberately no IMAP config: Hermes cannot
  receive mail, so nothing can reach in and steer it.

One-shot runs instead of the built-in loop: `python hermes.py --once`
(cron-friendly). Test without sending: `python hermes.py --dry-run`.

## How Instinct feeds Hermes

After any real task, Instinct commits a file like
`inbox/2026-09-22-proof-usage-check.md`:

```
# Proof usage check
Did: pulled Vercel logs, 12 sessions this week, $0.31 voice spend.
Next: nothing until credit hits 50%.
```

Hermes reads these at night. The files are the paper trail, not state - prune
them anytime.

## Boundaries

`safety.py` is code, not a prompt: graded coursework, spending money,
impersonating Royce, and account changes are blocked before any content
reaches the model, and model output cannot override it (`pytest test_safety.py`).
At startup, `validate_architecture()` refuses to boot if any env var points at
Gmail, calendar, contacts, Canvas, school accounts, Slack, Notion, WhatsApp,
Telegram, or IMAP. The container runs as a non-root user, read-only
filesystem, no published ports, all capabilities dropped.

## What this used to be

This repo previously held an always-on Telegram-controlled builder agent. That
design was shelved: Instinct does all the building, so Hermes became this
reader. The Telegram bot, builder loop, IMAP reply polling, and workspace
writes were removed.
