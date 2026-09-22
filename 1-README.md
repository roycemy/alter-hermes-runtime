# Hermes runtime

Hermes is Royce Myers' standalone backup builder. It runs in Docker on a Windows development machine or a generic VPS, takes steering from Royce through Telegram, hands work to Instinct first by email, and uses its own local fallback only if Instinct explicitly cannot help or the fallback timer expires.

Hermes is not OpenClaw and does not watch Royce's inbox, calendar, or deadlines. Instinct owns those jobs. Hermes only reads replies in the Hermes mailbox from `xt1udv@mail.instinct.com` that belong to a tracked Hermes request.

## Safety model

Hermes always identifies itself as "Hermes, Royce Myers' backup agent." It never impersonates Royce. Code-level checks block graded-schoolwork completion/submission, spending money or credits, third-party messages as Royce, and account/security/settings changes. Blocked work is surfaced to Royce and may be sent to Instinct only as a request for safe support. Model instructions cannot turn these checks off.

The container starts with `HERMES_MODE=readonly`. Review `directives.yaml`, then explicitly change it to `active`. Each directive has one autonomy level:

- `build-freely`: Hermes may write inside that directive's local workspace after Instinct declines or times out.
- `draft-only`: Hermes may make local drafts, never publish or send them.
- `ask-first`: Telegram approval is required before safe local drafting. Hard boundaries still apply after approval.

## What it does

1. Royce sends `/task <directive-id> <outcome>` to the private Telegram bot.
2. Hermes checks hard boundaries and validates that the directive is enabled.
3. Hermes emails Instinct at `xt1udv@mail.instinct.com`, clearly identifying itself and attaching a correlation ID.
4. Hermes records the request and Instinct's reply in SQLite at `/data/hermes.db`.
5. If Instinct says it cannot help, or the fallback timer expires, Hermes asks the configured LLM to create code or written drafts inside `/workspace/<directive local_path>` according to the autonomy level.
6. Royce gets the result or decision request in Telegram.

It does not silently deploy, push, submit, purchase, or send to third parties.

## Configure

Copy `.env.example` to `.env` and fill in:

- `OPENAI_API_KEY` and optionally `HERMES_MODEL`
- a dedicated Hermes mailbox's SMTP/IMAP username and app password
- a Telegram bot token from BotFather
- Royce's numeric Telegram user ID in `TELEGRAM_ALLOWED_USER_ID`

Do not use Royce's personal inbox as the Hermes mailbox. Keep `.env` off GitHub. Update `directives.yaml` with the real repos/ideas Hermes owns; leave unknown items disabled. Clone or mount working repositories under `./workspace` using each directive's `local_path`.

## Windows development machine

Requirements: Docker Desktop with Linux containers and Git.

```powershell
git clone https://github.com/roycemy/alter-hermes-runtime.git
cd alter-hermes-runtime
Copy-Item .env.example .env
notepad .env
notepad directives.yaml
docker compose build
docker compose up -d
docker compose logs -f hermes
```

Start in `readonly`. In Telegram, send `/start` and `/status`. After reviewing the directives and volume paths, set `HERMES_MODE=active` in `.env` and restart with `docker compose up -d`.

## VPS deployment

On any current Ubuntu/Debian VPS, install Docker Engine and the Compose plugin, then:

```bash
git clone https://github.com/roycemy/alter-hermes-runtime.git
cd alter-hermes-runtime
cp .env.example .env
chmod 600 .env
$EDITOR .env
$EDITOR directives.yaml
mkdir -p workspace
docker compose build
docker compose up -d
docker compose logs -f hermes
```

Keep the host firewall closed except for SSH. Telegram uses outbound polling and email uses outbound IMAP/SMTP, so Hermes needs no public HTTP port. Back up the named `hermes-data` volume if the audit log matters. Pull updates with `git pull && docker compose build && docker compose up -d`.

## Exact test-the-loop step

After the bot says Hermes is online, Royce sends this command to the Telegram bot:

```text
/testloop
```

Hermes emails `xt1udv@mail.instinct.com` with subject `[Hermes] Loop test` and body text saying who it is. Reply to that email from Instinct. Hermes' monitor records the reply. For a correlated end-to-end test, then send:

```text
/task idea-inbox Draft a one-paragraph test idea brief and return it to me
```

That message gets a `[Hermes:<task-id>]` subject, so the reply is matched and returned to Telegram.

## Operations

- `/status` shows recent task IDs and states.
- Runtime events and full tracked replies are in `/data/hermes.db`.
- `docker compose logs -f hermes` shows service logs, not secrets.
- To stop: `docker compose down` (the named data volume remains).
- To reset only after making a backup: `docker compose down -v`.

## Current limitations

Hermes' local fallback writes files; it does not yet push Git commits, deploy sites, browse accounts, or call school systems. Add capabilities only as explicit tools behind the same directive and boundary checks. Instinct remains the source for live email, calendar, and deadline state.
