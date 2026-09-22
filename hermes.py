"""Hermes: a quiet background reader for Royce Myers.

Nightly, Hermes reads four small memory files (me.md, projects.md,
decisions.md, proposals.md) plus task summaries Instinct drops in inbox/,
makes one cheap-model pass, and emails a stall report to Instinct. It does
not build, code, or message anyone else. Instinct relays the report to
Royce; nothing comes back to Hermes.
"""
from __future__ import annotations
import argparse, logging, os, re, smtplib, sqlite3, ssl, sys, time
from datetime import datetime, timedelta
from email.message import EmailMessage
from pathlib import Path
from zoneinfo import ZoneInfo
import yaml
from safety import inspect

logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"), format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("hermes")

DATA = Path(os.getenv("HERMES_DATA_DIR", "/data")); DATA.mkdir(parents=True, exist_ok=True)
DB_PATH = DATA / "hermes.db"
APP = Path(os.getenv("HERMES_APP_DIR", "/app"))
INSTINCT = os.getenv("HERMES_INSTINCT_ADDRESS", "xt1udv@mail.instinct.com")
MODEL = os.getenv("HERMES_MODEL", "gpt-4o-mini")
MAX_TOKENS = int(os.getenv("HERMES_MAX_TOKENS_PER_RUN", "1500"))
MONTHLY_BUDGET_USD = float(os.getenv("HERMES_MONTHLY_BUDGET_USD", "1.00"))

# USD per 1M tokens (input, output). Override with HERMES_PRICE_INPUT_PER_1M / HERMES_PRICE_OUTPUT_PER_1M.
PRICES = {
    "gpt-4o-mini": (0.15, 0.60),
    "gpt-4.1-mini": (0.40, 1.60),
    "gpt-4.1-nano": (0.10, 0.40),
    "gpt-5-mini": (0.25, 2.00),
}
FORBIDDEN_INTEGRATION_MARKERS = ("GMAIL", "GOOGLE_CALENDAR", "CONTACTS", "CANVAS", "VHL", "SCHOOL_ACCOUNT", "SLACK", "NOTION", "WHATSAPP", "TELEGRAM", "IMAP")

def validate_architecture():
    configured = [k for k, v in os.environ.items() if v and any(m in k.upper() for m in FORBIDDEN_INTEGRATION_MARKERS)]
    if configured:
        raise SystemExit("Unsupported integration configured: " + ", ".join(sorted(configured))
            + ". Hermes may connect only to OpenAI and its one-way SMTP report mailbox. It has no inbox, no chat apps, and no account access.")

class Store:
    def __init__(self):
        self.db = sqlite3.connect(DB_PATH, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.executescript("""
        CREATE TABLE IF NOT EXISTS runs(id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL, month TEXT,
            model TEXT, prompt_tokens INTEGER, completion_tokens INTEGER, cost_usd REAL, status TEXT, detail TEXT);
        CREATE TABLE IF NOT EXISTS events(id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL, kind TEXT, detail TEXT);
        """); self.db.commit()
    def event(self, kind, detail=""):
        self.db.execute("INSERT INTO events(ts,kind,detail) VALUES(?,?,?)", (time.time(), kind, detail[:10000])); self.db.commit()
    def run(self, model, pt, ct, cost, status, detail=""):
        self.db.execute("INSERT INTO runs(ts,month,model,prompt_tokens,completion_tokens,cost_usd,status,detail) VALUES(?,?,?,?,?,?,?,?)",
            (time.time(), datetime.now().strftime("%Y-%m"), model, pt, ct, cost, status, detail[:10000])); self.db.commit()
    def month_spend(self):
        row = self.db.execute("SELECT COALESCE(SUM(cost_usd),0) AS s FROM runs WHERE month=?", (datetime.now().strftime("%Y-%m"),)).fetchone()
        return float(row["s"])
store = Store()

def load_config():
    with open(os.getenv("HERMES_DIRECTIVES", str(APP / "directives.yaml")), encoding="utf-8") as f:
        return yaml.safe_load(f)

def read_memory_files(cfg):
    texts = {}
    for name in cfg.get("memory_files", ["me.md", "projects.md", "decisions.md", "proposals.md"]):
        p = APP / name
        texts[name] = p.read_text(encoding="utf-8") if p.exists() else "(missing)"
    return texts

def read_inbox(cfg):
    inbox = APP / cfg.get("inbox_dir", "inbox")
    summaries, blocked = [], []
    if inbox.is_dir():
        for p in sorted(inbox.glob("*.md")):
            if p.name.lower() == "readme.md": continue
            text = p.read_text(encoding="utf-8")
            decision = inspect(text)
            if decision.allowed:
                summaries.append((p.name, text))
            else:
                blocked.append((p.name, decision.reason))
                store.event("inbox_blocked", f"{p.name}: {decision.reason}")
    return summaries, blocked

def parse_projects(projects_md, stall_days):
    """Deterministic stall signal from projects.md sections with 'Last moved:' dates."""
    projects, current = [], None
    for line in projects_md.splitlines():
        m = re.match(r"^##\s+(.+?)\s*$", line)
        if m:
            current = {"name": m.group(1), "last_moved": None, "next": ""}
            projects.append(current); continue
        if current is None: continue
        d = re.search(r"last moved:\s*(\d{4}-\d{2}-\d{2})", line, re.I)
        if d: current["last_moved"] = d.group(1)
        n = re.search(r"next:\s*(.+)", line, re.I)
        if n and not current["next"]: current["next"] = n.group(1).strip()
    today = datetime.now().date()
    for p in projects:
        if p["last_moved"]:
            p["days"] = (today - datetime.strptime(p["last_moved"], "%Y-%m-%d").date()).days
        else:
            p["days"] = None
        p["stalled"] = p["days"] is not None and p["days"] >= stall_days
    return projects

def token_price(model):
    if model in PRICES: return PRICES[model]
    i = float(os.getenv("HERMES_PRICE_INPUT_PER_1M", "0")); o = float(os.getenv("HERMES_PRICE_OUTPUT_PER_1M", "0"))
    return (i, o)

def model_pass(cfg, memory, summaries, projects):
    from openai import OpenAI
    client = OpenAI(api_key=os.environ["OPENAI_API_KEY"])
    inbox_text = "\n\n".join(f"--- {name} ---\n{text}" for name, text in summaries) or "(no new Instinct summaries)"
    stall_lines = "\n".join(
        f"- {p['name']}: {p['days']} days since movement. Next on file: {p['next'] or 'none recorded'}"
        for p in projects) or "(no projects parsed)"
    system = ("You are Hermes, the quiet background reader for Royce Myers. You are not Royce and cannot act for him. "
        "Read the memory files and Instinct's task summaries, then write a short morning stall report. "
        "For each tracked project: whether it moved, how many days since it moved, why it matters, and the single obvious next step. "
        "Flag the 1-3 most stalled projects as suggested focus. Never propose spending money, messaging anyone as Royce, or doing graded schoolwork. "
        "Be terse; plain text; under 250 words.")
    user = (f"STALL SIGNAL (computed, trust these numbers):\n{stall_lines}\n\n"
        f"me.md:\n{memory.get('me.md','')}\n\nprojects.md:\n{memory.get('projects.md','')}\n\n"
        f"decisions.md:\n{memory.get('decisions.md','')}\n\nproposals.md:\n{memory.get('proposals.md','')}\n\n"
        f"INSTINCT TASK SUMMARIES:\n{inbox_text}")
    res = client.chat.completions.create(model=MODEL, max_tokens=MAX_TOKENS,
        messages=[{"role": "system", "content": system}, {"role": "user", "content": user}])
    usage = res.usage
    pin, pout = token_price(MODEL)
    cost = (usage.prompt_tokens * pin + usage.completion_tokens * pout) / 1_000_000
    return res.choices[0].message.content, usage.prompt_tokens, usage.completion_tokens, cost

def deterministic_report(projects, blocked):
    lines = ["STALL SIGNAL (no model pass):"]
    for p in projects:
        flag = "STALLED" if p["stalled"] else "ok"
        days = "unknown" if p["days"] is None else str(p["days"])
        lines.append(f"- {p['name']}: {days} days since movement [{flag}]. Next on file: {p['next'] or 'none recorded'}")
    for name, reason in blocked:
        lines.append(f"- inbox/{name} was blocked by safety rules: {reason}")
    return "\n".join(lines)

class MailBridge:
    def send(self, subject, body):
        msg = EmailMessage()
        msg["From"] = os.environ["HERMES_EMAIL_ADDRESS"]; msg["To"] = INSTINCT; msg["Subject"] = subject
        msg.set_content("Hermes here - Royce Myers' background reader. I am not Royce and cannot authorize actions in his name.\n\n" + body)
        with smtplib.SMTP_SSL(os.environ["SMTP_HOST"], int(os.getenv("SMTP_PORT", "465")), context=ssl.create_default_context()) as s:
            s.login(os.environ["SMTP_USERNAME"], os.environ["SMTP_PASSWORD"]); s.send_message(msg)
        store.event("email_sent", f"to={INSTINCT}; subject={subject}")
mail = MailBridge()

def run_once(dry_run=False):
    cfg = load_config()
    stall_days = int(cfg.get("stall_days", 3))
    memory = read_memory_files(cfg)
    summaries, blocked = read_inbox(cfg)
    projects = parse_projects(memory.get("projects.md", ""), stall_days)
    spend = store.month_spend()
    log.info("month spend so far: $%.4f (cap $%.2f)", spend, MONTHLY_BUDGET_USD)

    if spend >= MONTHLY_BUDGET_USD:
        notice = (f"Hermes monthly budget cap hit: ${spend:.4f} of ${MONTHLY_BUDGET_USD:.2f} spent. "
                  "No model pass made. Raise HERMES_MONTHLY_BUDGET_USD or wait for next month.\n\n" + deterministic_report(projects, blocked))
        store.run(MODEL, 0, 0, 0.0, "budget_cap")
        if dry_run: print(notice); return
        mail.send("[Hermes] Budget cap reached - report without model pass", notice); return

    if os.getenv("OPENAI_API_KEY"):
        try:
            report, pt, ct, cost = model_pass(cfg, memory, summaries, projects)
        except Exception as e:
            log.exception("model pass failed")
            report, pt, ct, cost = deterministic_report(projects, blocked) + f"\n\n(model pass failed: {e})", 0, 0, 0.0
            store.run(MODEL, pt, ct, cost, "model_error", str(e))
        else:
            store.run(MODEL, pt, ct, cost, "ok")
    else:
        report, pt, ct, cost = deterministic_report(projects, blocked), 0, 0, 0.0
        store.run(MODEL, 0, 0, 0.0, "no_api_key", "deterministic report only")

    if blocked:
        report += "\n\nBlocked by safety rules: " + "; ".join(f"inbox/{n} ({r})" for n, r in blocked)
    report += f"\n\n-- run cost: ${cost:.4f} ({pt}+{ct} tokens, {MODEL}); month total: ${store.month_spend():.4f} of ${MONTHLY_BUDGET_USD:.2f}"

    if dry_run:
        print(report); return
    mail.send(f"[Hermes] Nightly stall report - {datetime.now():%Y-%m-%d}", report)
    log.info("report emailed to %s", INSTINCT)

def next_run_dt(cfg):
    tz = ZoneInfo(cfg.get("timezone", "America/New_York"))
    hh, mm = (int(x) for x in str(cfg.get("run_time", "01:00")).split(":")[:2])
    now = datetime.now(tz)
    nxt = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
    if nxt <= now: nxt += timedelta(days=1)
    return nxt

def main():
    ap = argparse.ArgumentParser(description="Hermes quiet background reader")
    ap.add_argument("--once", action="store_true", help="run one pass and exit (cron-friendly)")
    ap.add_argument("--dry-run", action="store_true", help="run one pass and print the report instead of emailing it")
    args = ap.parse_args()
    validate_architecture()
    if args.once or args.dry_run:
        run_once(dry_run=args.dry_run); return
    for var in ("HERMES_EMAIL_ADDRESS", "SMTP_HOST", "SMTP_USERNAME", "SMTP_PASSWORD", "OPENAI_API_KEY"):
        if not os.getenv(var): raise SystemExit(f"Missing environment variable: {var}")
    cfg = load_config()
    log.info("Hermes reader online. Nightly run at %s %s.", cfg.get("run_time", "01:00"), cfg.get("timezone", "America/New_York"))
    while True:
        nxt = next_run_dt(cfg)
        log.info("next run: %s", nxt.isoformat())
        while (delay := (nxt - datetime.now(nxt.tzinfo)).total_seconds()) > 0:
            time.sleep(min(delay, 300))
        try: run_once()
        except Exception: log.exception("nightly run failed")

if __name__ == "__main__":
    main()
