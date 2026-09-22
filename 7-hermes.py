from __future__ import annotations
import asyncio, email, imaplib, json, logging, os, shlex, smtplib, sqlite3, ssl, subprocess, time, uuid
from email.message import EmailMessage
from pathlib import Path
from typing import Any
import yaml
from openai import AsyncOpenAI
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes, MessageHandler, filters
from safety import inspect

logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"), format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("hermes")
DATA = Path(os.getenv("HERMES_DATA_DIR", "/data")); DATA.mkdir(parents=True, exist_ok=True)
WORKSPACE = Path(os.getenv("HERMES_WORKSPACE", "/workspace")).resolve(); WORKSPACE.mkdir(parents=True, exist_ok=True)
DB_PATH = DATA / "hermes.db"
INSTINCT = os.getenv("HERMES_INSTINCT_ADDRESS", "xt1udv@mail.instinct.com")
OWNER_ID = int(os.getenv("TELEGRAM_ALLOWED_USER_ID", "0") or 0)
MODE = os.getenv("HERMES_MODE", "readonly").lower()
FALLBACK_SECONDS = int(os.getenv("HERMES_FALLBACK_MINUTES", "30")) * 60

class Store:
    def __init__(self):
        self.db = sqlite3.connect(DB_PATH, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.executescript("""
        CREATE TABLE IF NOT EXISTS tasks(id TEXT PRIMARY KEY, directive TEXT, request TEXT, status TEXT, created REAL, updated REAL, instinct_message_id TEXT, result TEXT);
        CREATE TABLE IF NOT EXISTS events(id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL, task_id TEXT, kind TEXT, detail TEXT);
        CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY, value TEXT);
        """); self.db.commit()
    def event(self, kind, detail, task_id=None):
        self.db.execute("INSERT INTO events(ts,task_id,kind,detail) VALUES(?,?,?,?)", (time.time(),task_id,kind,detail[:10000])); self.db.commit()
    def task(self, directive, request):
        tid=uuid.uuid4().hex[:12]; now=time.time(); self.db.execute("INSERT INTO tasks VALUES(?,?,?,?,?,?,?,?)",(tid,directive,request,"queued",now,now,None,None)); self.db.commit(); self.event("task_created",request,tid); return tid
    def update(self, tid, status, result=None):
        self.db.execute("UPDATE tasks SET status=?, updated=?, result=COALESCE(?,result) WHERE id=?",(status,time.time(),result,tid)); self.db.commit(); self.event("status",status,tid)
    def get(self, tid): return self.db.execute("SELECT * FROM tasks WHERE id=?",(tid,)).fetchone()
    def pending(self): return self.db.execute("SELECT * FROM tasks WHERE status IN ('sent_to_instinct','fallback_ready') ORDER BY created").fetchall()
    def recent(self): return self.db.execute("SELECT * FROM tasks ORDER BY created DESC LIMIT 8").fetchall()
store=Store()

def load_directives():
    with open(os.getenv("HERMES_DIRECTIVES", "/app/directives.yaml"), encoding="utf-8") as f: return yaml.safe_load(f)

def directive(item_id):
    for x in load_directives().get("items",[]):
        if x.get("id")==item_id and x.get("enabled",False): return x
    return None

def owned_path(rel: str) -> Path:
    p=(WORKSPACE/rel).resolve()
    if p != WORKSPACE and WORKSPACE not in p.parents: raise ValueError("path escapes workspace")
    return p

class MailBridge:
    def send(self, subject, body, task_id=None):
        sender=os.environ["HERMES_EMAIL_ADDRESS"]
        msg=EmailMessage(); msg["From"]=sender; msg["To"]=INSTINCT; msg["Subject"]=subject
        msg.set_content("Hermes here - Royce Myers' backup agent. I am not Royce and cannot authorize actions in his name.\n\n"+body)
        with smtplib.SMTP_SSL(os.environ["SMTP_HOST"],int(os.getenv("SMTP_PORT","465")),context=ssl.create_default_context()) as s:
            s.login(os.environ["SMTP_USERNAME"],os.environ["SMTP_PASSWORD"]); s.send_message(msg)
        store.event("email_sent",f"to={INSTINCT}; subject={subject}",task_id)
    def replies(self):
        out=[]
        with imaplib.IMAP4_SSL(os.environ["IMAP_HOST"],int(os.getenv("IMAP_PORT","993"))) as im:
            im.login(os.environ["IMAP_USERNAME"],os.environ["IMAP_PASSWORD"]); im.select("INBOX")
            _,data=im.search(None,'UNSEEN',f'FROM "{INSTINCT}"')
            for num in data[0].split():
                _,raw=im.fetch(num,"(RFC822)"); msg=email.message_from_bytes(raw[0][1])
                text=""
                if msg.is_multipart():
                    for part in msg.walk():
                        if part.get_content_type()=="text/plain" and not part.get_filename(): text=part.get_payload(decode=True).decode(errors="replace"); break
                else: text=msg.get_payload(decode=True).decode(errors="replace")
                out.append((msg.get("Subject",""),text,msg.get("Message-ID",""))); im.store(num,"+FLAGS","\\Seen")
        return out
mail=MailBridge()

def allowed_user(update): return bool(update.effective_user and OWNER_ID and update.effective_user.id==OWNER_ID)
async def deny(update): await update.effective_message.reply_text("Hermes is private.")
async def ping_owner(app, text):
    if OWNER_ID: await app.bot.send_message(chat_id=OWNER_ID,text=text[:4000])

async def ask_instinct(tid):
    row=store.get(tid)
    body=f"Outcome requested: {row['request']}\nDirective: {row['directive']}\n\nPlease take this work if it fits your tools. Reply with what you did, what Royce must decide, or explicitly say you cannot do it. Correlation ID: {tid}."
    await asyncio.to_thread(mail.send,f"[Hermes:{tid}] Work request",body,tid); store.update(tid,"sent_to_instinct")

SYSTEM="""You are Hermes, Royce Myers' backup builder. You are always Hermes, never Royce. Be terse. Never complete or submit graded coursework; never spend money or credits; never message third parties as Royce; never change account settings. Work only inside the given workspace. Return JSON with keys summary and files, where files is a list of {path,content}. Produce useful local drafts or code; do not claim deployment, sending, or execution you did not perform."""
async def local_work(tid):
    row=store.get(tid); item=directive(row["directive"])
    if not item: store.update(tid,"blocked","Directive missing or disabled"); return "Blocked: directive missing or disabled."
    decision=inspect(row["request"])
    if not decision.allowed: store.update(tid,"escalated",decision.reason); return decision.reason
    if MODE != "active": store.update(tid,"blocked","Runtime is read-only. Set HERMES_MODE=active after reviewing directives."); return "Read-only mode: no files changed."
    autonomy=item.get("autonomy","ask-first")
    if autonomy=="ask-first": store.update(tid,"needs_approval","Directive requires Royce approval."); return "This directive is ask-first. Reply /approve "+tid
    client=AsyncOpenAI(api_key=os.environ["OPENAI_API_KEY"])
    prompt=json.dumps({"directive":item,"request":row["request"],"autonomy":autonomy})
    res=await client.chat.completions.create(model=os.getenv("HERMES_MODEL","gpt-4.1-mini"),response_format={"type":"json_object"},messages=[{"role":"system","content":SYSTEM},{"role":"user","content":prompt}])
    data=json.loads(res.choices[0].message.content); written=[]
    base=item.get("local_path",item["id"])
    for f in data.get("files",[]):
        p=owned_path(str(Path(base)/f["path"])); p.parent.mkdir(parents=True,exist_ok=True); p.write_text(f["content"],encoding="utf-8"); written.append(str(p.relative_to(WORKSPACE)))
    result=data.get("summary","Draft complete")+("\nFiles: "+", ".join(written) if written else "")
    store.update(tid,"drafted" if autonomy=="draft-only" else "completed",result); return result

async def cmd_start(update:Update, context:ContextTypes.DEFAULT_TYPE):
    if not allowed_user(update): return await deny(update)
    await update.message.reply_text(f"Hermes online. Mode: {MODE}. Commands: /task <directive-id> <work>, /status, /testloop, /approve <id>.")
async def cmd_status(update,context):
    if not allowed_user(update): return await deny(update)
    rows=store.recent(); text=f"Mode: {MODE}\n"+"\n".join(f"{r['id']} {r['status']} - {r['request'][:80]}" for r in rows)
    await update.message.reply_text(text or "No tasks yet.")
async def cmd_testloop(update,context):
    if not allowed_user(update): return await deny(update)
    try: await asyncio.to_thread(mail.send,"[Hermes] Loop test","Hermes is online. Please reply so I can confirm the Instinct bridge is live."); await update.message.reply_text("Test email sent to Instinct as Hermes.")
    except Exception as e: log.exception("test loop"); await update.message.reply_text("Test failed. Check Hermes mailbox settings.")
async def cmd_task(update,context):
    if not allowed_user(update): return await deny(update)
    if len(context.args)<2: return await update.message.reply_text("Use: /task <directive-id> <what to do>")
    did=context.args[0]; req=" ".join(context.args[1:]); item=directive(did)
    if not item: return await update.message.reply_text("That directive is missing or disabled.")
    d=inspect(req)
    if not d.allowed:
        store.event("boundary_block",d.reason); await update.message.reply_text(d.reason+" I can ask Instinct to coach or prepare safe support instead."); return
    tid=store.task(did,req)
    try: await ask_instinct(tid); await update.message.reply_text(f"Sent to Instinct as Hermes. Tracking {tid}; local fallback in {FALLBACK_SECONDS//60} minutes if needed.")
    except Exception: log.exception("bridge"); store.update(tid,"fallback_ready","Instinct bridge failed"); await update.message.reply_text(f"Instinct bridge failed. Tracking {tid} for local fallback.")
async def cmd_approve(update,context):
    if not allowed_user(update): return await deny(update)
    if not context.args: return await update.message.reply_text("Use: /approve <task-id>")
    tid=context.args[0]; row=store.get(tid)
    if not row: return await update.message.reply_text("Unknown task.")
    item=directive(row["directive"])
    if not item: return await update.message.reply_text("Directive unavailable.")
    # Approval only allows safe local drafting/building; hard boundaries still run.
    original=item.get("autonomy"); item["autonomy"]="draft-only"
    decision=inspect(row["request"])
    if not decision.allowed: return await update.message.reply_text(decision.reason)
    store.update(tid,"fallback_ready"); await update.message.reply_text(await local_work(tid))
async def plain(update,context):
    if not allowed_user(update): return await deny(update)
    await update.message.reply_text("Use /task <directive-id> <work>. Hermes asks Instinct first and tracks the handoff.")

async def monitor(app):
    while True:
        try:
            for subject,body,msgid in await asyncio.to_thread(mail.replies):
                import re
                m=re.search(r"\[Hermes:([a-f0-9]{12})\]",subject,re.I)
                if not m: store.event("unmatched_reply",subject); continue
                tid=m.group(1).lower(); row=store.get(tid)
                if not row: continue
                store.event("instinct_reply",body,tid)
                low=body.lower()
                if any(x in low for x in ("cannot do","can't do","unable to","cannot help")):
                    store.update(tid,"fallback_ready",body); result=await local_work(tid); await ping_owner(app,f"Instinct could not take {tid}. Hermes fallback: {result}")
                else:
                    store.update(tid,"completed_by_instinct",body); await ping_owner(app,f"Instinct replied on {tid}:\n{body[:3200]}")
            now=time.time()
            for row in store.pending():
                if row["status"]=="sent_to_instinct" and now-row["updated"]>=FALLBACK_SECONDS:
                    store.update(row["id"],"fallback_ready","Instinct fallback timer expired"); result=await local_work(row["id"]); await ping_owner(app,f"No Instinct reply before fallback for {row['id']}. Hermes: {result}")
        except Exception: log.exception("monitor loop")
        await asyncio.sleep(30)

async def post_init(app): app.create_task(monitor(app))
def main():
    required=["TELEGRAM_BOT_TOKEN","TELEGRAM_ALLOWED_USER_ID","HERMES_EMAIL_ADDRESS","SMTP_HOST","SMTP_USERNAME","SMTP_PASSWORD","IMAP_HOST","IMAP_USERNAME","IMAP_PASSWORD"]
    missing=[x for x in required if not os.getenv(x)]
    if missing: raise SystemExit("Missing environment variables: "+", ".join(missing))
    app=Application.builder().token(os.environ["TELEGRAM_BOT_TOKEN"]).post_init(post_init).build()
    app.add_handler(CommandHandler("start",cmd_start)); app.add_handler(CommandHandler("status",cmd_status)); app.add_handler(CommandHandler("testloop",cmd_testloop)); app.add_handler(CommandHandler("task",cmd_task)); app.add_handler(CommandHandler("approve",cmd_approve)); app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND,plain)); app.run_polling(drop_pending_updates=True)
if __name__=="__main__": main()
