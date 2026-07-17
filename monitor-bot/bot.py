#!/usr/bin/env python3
"""
🖥 Server Monitor Bot
Per-user private server monitoring via Telegram ping checks.
"""

import asyncio
import sqlite3
import subprocess
import logging
import time
from datetime import datetime
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes

logging.basicConfig(format="%(asctime)s %(levelname)s %(message)s", level=logging.INFO)
log = logging.getLogger(__name__)

# ─── CONFIG ───────────────────────────────────────────────────────────────────
BOT_TOKEN = "8151486704:AAGnpS__jhHraHawjtmEJuFmuEi2OekrUdQ"
ADMIN_ID  = 1132489406   # lord's Telegram chat ID
DB_PATH   = "/opt/monitor-bot/monitoring.db"
LOOP_TICK = 10           # loop runs every 10 seconds

# ─── DATABASE ─────────────────────────────────────────────────────────────────
def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    with get_db() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS users (
                chat_id    INTEGER PRIMARY KEY,
                username   TEXT,
                interval   INTEGER DEFAULT 60,
                alert_mode TEXT    DEFAULT 'once',
                repeat_min INTEGER DEFAULT 30
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS servers (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_id    INTEGER NOT NULL,
                ip         TEXT    NOT NULL,
                name       TEXT    NOT NULL,
                active     INTEGER DEFAULT 1,
                status     TEXT    DEFAULT 'unknown',
                last_check REAL    DEFAULT 0,
                last_alert REAL    DEFAULT 0,
                down_since REAL    DEFAULT 0,
                UNIQUE(chat_id, ip)
            )
        """)

# ─── HELPERS ──────────────────────────────────────────────────────────────────
def fmt_duration(seconds: float) -> str:
    if seconds < 60:
        return f"{int(seconds)}s"
    elif seconds < 3600:
        return f"{int(seconds / 60)}m"
    else:
        h = int(seconds / 3600)
        m = int((seconds % 3600) / 60)
        return f"{h}h {m}m"

def now_str() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")

def ensure_user(chat_id: int, username: str):
    with get_db() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO users (chat_id, username) VALUES (?,?)",
            (chat_id, username or str(chat_id))
        )

# ─── PING ─────────────────────────────────────────────────────────────────────
def do_ping(ip: str) -> bool:
    try:
        r = subprocess.run(
            ["ping", "-c", "1", "-W", "2", ip],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=5
        )
        return r.returncode == 0
    except Exception:
        return False

# ─── MONITORING LOOP ──────────────────────────────────────────────────────────
async def check_server(app, chat_id, s, alert_mode, repeat_min):
    loop = asyncio.get_event_loop()
    now = time.time()

    if now - s["last_check"] < s["interval"]:
        return

    # Ping 3 times, need 2 failures to confirm down
    results = []
    for _ in range(3):
        result = await loop.run_in_executor(None, do_ping, s["ip"])
        results.append(result)
        await asyncio.sleep(1)

    is_up      = results.count(True) >= 2   # majority wins
    new_status = "up" if is_up else "down"
    old_status = s["status"]
    updates    = {"last_check": now}

    if new_status != old_status:
        updates["status"] = new_status
        if new_status == "down":
            updates["down_since"] = now
            updates["last_alert"] = now
            msg = (
                f"🔴 *Server Down!*\n\n"
                f"🖥 *{s['name']}* (`{s['ip']}`)\n"
                f"🕐 {now_str()}"
            )
        else:
            duration = fmt_duration(now - s["down_since"]) if s["down_since"] else "unknown"
            updates["last_alert"] = now
            msg = (
                f"✅ *Server Recovered!*\n\n"
                f"🖥 *{s['name']}* (`{s['ip']}`)\n"
                f"⏱ Was down for: *{duration}*\n"
                f"🕐 {now_str()}"
            )
        try:
            await app.bot.send_message(chat_id, msg, parse_mode="Markdown")
        except Exception as e:
            log.error(f"Alert failed for {chat_id}: {e}")

    elif new_status == "down" and alert_mode == "repeat":
        if now - s["last_alert"] >= repeat_min * 60:
            updates["last_alert"] = now
            duration = fmt_duration(now - s["down_since"]) if s["down_since"] else "unknown"
            msg = (
                f"🔴 *Still Down — Reminder*\n\n"
                f"🖥 *{s['name']}* (`{s['ip']}`)\n"
                f"⏱ Down for: *{duration}*\n"
                f"🕐 {now_str()}"
            )
            try:
                await app.bot.send_message(chat_id, msg, parse_mode="Markdown")
            except Exception as e:
                log.error(f"Reminder failed for {chat_id}: {e}")

    set_clause = ", ".join(f"{k}=?" for k in updates)
    vals = list(updates.values()) + [s["id"]]
    with get_db() as conn:
        conn.execute(f"UPDATE servers SET {set_clause} WHERE id=?", vals)


async def check_user(app, user):
    chat_id    = user["chat_id"]
    alert_mode = user["alert_mode"]
    repeat_min = user["repeat_min"]

    with get_db() as conn:
        servers = conn.execute(
            "SELECT s.*, u.interval FROM servers s "
            "JOIN users u ON s.chat_id = u.chat_id "
            "WHERE s.chat_id=? AND s.active=1", (chat_id,)
        ).fetchall()

    await asyncio.gather(*[
        check_server(app, chat_id, s, alert_mode, repeat_min)
        for s in servers
    ])


async def monitor_loop(app: Application):
    while True:
        try:
            with get_db() as conn:
                users = conn.execute("SELECT * FROM users").fetchall()

            # All users checked simultaneously
            await asyncio.gather(*[check_user(app, u) for u in users])

        except Exception as e:
            log.error(f"Monitor loop error: {e}")

        await asyncio.sleep(LOOP_TICK)
# ─── USER COMMANDS ────────────────────────────────────────────────────────────
HELP_TEXT = (
    "🖥 *Server Monitor Bot*\n\n"
    "*Server Management:*\n"
    "`/addserver <ip> <name>` — add a server\n"
    "`/removeserver <ip>` — remove a server\n"
    "`/listservers` — list your servers\n"
    "`/status` — live check all servers now\n"
    "`/pause <ip>` — pause monitoring\n"
    "`/resume <ip>` — resume monitoring\n\n"
    "*Settings:*\n"
    "`/setinterval <seconds>` — check interval (min 10s)\n"
    "`/setalert once` — alert once when down\n"
    "`/setalert repeat <minutes>` — repeat alert every X min\n\n"
    "`/help` — show this menu"
)

async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    ensure_user(user.id, user.username or user.first_name)
    await update.message.reply_text(
        f"👋 Welcome *{user.first_name}*!\n\n"
        "I'll monitor your servers privately and alert only you.\n\n"
        + HELP_TEXT,
        parse_mode="Markdown"
    )

async def cmd_help(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(HELP_TEXT, parse_mode="Markdown")

async def cmd_addserver(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    ensure_user(user.id, user.username or user.first_name)

    if len(ctx.args) < 2:
        await update.message.reply_text(
            "Usage: `/addserver <ip> <name>`\n"
            "Example: `/addserver 10.66.66.1 Client VPS`",
            parse_mode="Markdown"
        )
        return

    ip   = ctx.args[0]
    name = " ".join(ctx.args[1:])

    with get_db() as conn:
        try:
            conn.execute(
                "INSERT INTO servers (chat_id, ip, name) VALUES (?,?,?)",
                (user.id, ip, name)
            )
            await update.message.reply_text(
                f"✅ Added *{name}* (`{ip}`) to your monitoring list.\n"
                f"I'll start checking it on your next interval.",
                parse_mode="Markdown"
            )
        except sqlite3.IntegrityError:
            await update.message.reply_text(
                f"⚠️ `{ip}` is already in your list.", parse_mode="Markdown"
            )

async def cmd_removeserver(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not ctx.args:
        await update.message.reply_text("Usage: `/removeserver <ip>`", parse_mode="Markdown")
        return

    ip = ctx.args[0]
    with get_db() as conn:
        result = conn.execute(
            "DELETE FROM servers WHERE chat_id=? AND ip=?", (update.effective_user.id, ip)
        )
    if result.rowcount:
        await update.message.reply_text(f"🗑 Removed `{ip}` from your list.", parse_mode="Markdown")
    else:
        await update.message.reply_text(f"❌ `{ip}` not found in your list.", parse_mode="Markdown")

async def cmd_listservers(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_user.id
    with get_db() as conn:
        servers = conn.execute(
            "SELECT * FROM servers WHERE chat_id=? ORDER BY name", (chat_id,)
        ).fetchall()
        user = conn.execute("SELECT * FROM users WHERE chat_id=?", (chat_id,)).fetchone()

    if not servers:
        await update.message.reply_text(
            "📭 No servers yet. Use `/addserver <ip> <name>` to add one.",
            parse_mode="Markdown"
        )
        return

    interval   = user["interval"] if user else 60
    alert_mode = user["alert_mode"] if user else "once"
    repeat_min = user["repeat_min"] if user else 30

    alert_info = f"once" if alert_mode == "once" else f"every {repeat_min}m"
    lines = [
        f"📋 *Your Servers* (check every {interval}s, alert: {alert_info})\n"
    ]

    for s in servers:
        icon   = {"up": "🟢", "down": "🔴", "unknown": "⚪"}.get(s["status"], "⚪")
        paused = " ⏸ *paused*" if not s["active"] else ""
        if s["last_check"]:
            checked = fmt_duration(time.time() - s["last_check"]) + " ago"
        else:
            checked = "not yet"
        lines.append(f"{icon}{paused} *{s['name']}* — `{s['ip']}`\n   Last checked: {checked}")

    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")

async def cmd_status(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_user.id
    with get_db() as conn:
        servers = conn.execute(
            "SELECT * FROM servers WHERE chat_id=? AND active=1 ORDER BY name", (chat_id,)
        ).fetchall()

    if not servers:
        await update.message.reply_text("📭 No active servers to check.", parse_mode="Markdown")
        return

    msg = await update.message.reply_text("⏳ Pinging all servers...", parse_mode="Markdown")
    loop  = asyncio.get_event_loop()
    lines = [f"🔍 *Live Status — {now_str()}*\n"]

    for s in servers:
        is_up = await loop.run_in_executor(None, do_ping, s["ip"])
        icon  = "🟢 Up" if is_up else "🔴 Down"
        extra = ""
        if not is_up and s["down_since"]:
            extra = f" *(down {fmt_duration(time.time() - s['down_since'])})*"
        lines.append(f"{icon}{extra} — *{s['name']}* (`{s['ip']}`)")

    await msg.edit_text("\n".join(lines), parse_mode="Markdown")

async def cmd_setinterval(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not ctx.args or not ctx.args[0].isdigit():
        await update.message.reply_text(
            "Usage: `/setinterval <seconds>`\nExample: `/setinterval 60`",
            parse_mode="Markdown"
        )
        return

    secs = int(ctx.args[0])
    if secs < 10:
        await update.message.reply_text("⚠️ Minimum interval is *10 seconds*.", parse_mode="Markdown")
        return

    chat_id = update.effective_user.id
    ensure_user(chat_id, update.effective_user.username)
    with get_db() as conn:
        conn.execute("UPDATE users SET interval=? WHERE chat_id=?", (secs, chat_id))
    await update.message.reply_text(
        f"✅ Check interval set to *{secs} seconds*.", parse_mode="Markdown"
    )

async def cmd_setalert(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_user.id
    ensure_user(chat_id, update.effective_user.username)

    if not ctx.args:
        await update.message.reply_text(
            "Usage:\n"
            "`/setalert once` — alert once when down, once when recovered\n"
            "`/setalert repeat <minutes>` — repeat alert every X min while down",
            parse_mode="Markdown"
        )
        return

    mode = ctx.args[0].lower()

    if mode == "once":
        with get_db() as conn:
            conn.execute("UPDATE users SET alert_mode='once' WHERE chat_id=?", (chat_id,))
        await update.message.reply_text(
            "✅ Alert mode: *once*\nYou'll get one alert when a server goes down and one when it recovers.",
            parse_mode="Markdown"
        )
    elif mode == "repeat":
        if len(ctx.args) < 2 or not ctx.args[1].isdigit():
            await update.message.reply_text(
                "Usage: `/setalert repeat <minutes>`\nExample: `/setalert repeat 15`",
                parse_mode="Markdown"
            )
            return
        mins = int(ctx.args[1])
        with get_db() as conn:
            conn.execute(
                "UPDATE users SET alert_mode='repeat', repeat_min=? WHERE chat_id=?",
                (mins, chat_id)
            )
        await update.message.reply_text(
            f"✅ Alert mode: *repeat every {mins} minutes* while server is down.",
            parse_mode="Markdown"
        )
    else:
        await update.message.reply_text("Options: `once` or `repeat <minutes>`", parse_mode="Markdown")

async def cmd_pause(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not ctx.args:
        await update.message.reply_text("Usage: `/pause <ip>`", parse_mode="Markdown")
        return

    ip = ctx.args[0]
    with get_db() as conn:
        result = conn.execute(
            "UPDATE servers SET active=0 WHERE chat_id=? AND ip=?",
            (update.effective_user.id, ip)
        )
    if result.rowcount:
        await update.message.reply_text(f"⏸ Paused monitoring for `{ip}`.", parse_mode="Markdown")
    else:
        await update.message.reply_text(f"❌ `{ip}` not found.", parse_mode="Markdown")

async def cmd_resume(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not ctx.args:
        await update.message.reply_text("Usage: `/resume <ip>`", parse_mode="Markdown")
        return

    ip = ctx.args[0]
    with get_db() as conn:
        result = conn.execute(
            "UPDATE servers SET active=1, status='unknown', last_check=0 WHERE chat_id=? AND ip=?",
            (update.effective_user.id, ip)
        )
    if result.rowcount:
        await update.message.reply_text(f"▶️ Resumed monitoring for `{ip}`.", parse_mode="Markdown")
    else:
        await update.message.reply_text(f"❌ `{ip}` not found.", parse_mode="Markdown")

# ─── ADMIN COMMANDS ───────────────────────────────────────────────────────────
def is_admin(update: Update) -> bool:
    return update.effective_user.id == ADMIN_ID

async def cmd_admin(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update):
        await update.message.reply_text("⛔ Admin only.")
        return

    with get_db() as conn:
        users          = conn.execute("SELECT * FROM users").fetchall()
        total_servers  = conn.execute("SELECT COUNT(*) as c FROM servers").fetchone()["c"]
        active_servers = conn.execute("SELECT COUNT(*) as c FROM servers WHERE active=1").fetchone()["c"]
        down_servers   = conn.execute("SELECT COUNT(*) as c FROM servers WHERE status='down'").fetchone()["c"]

    lines = [
        "👑 *Admin Overview*\n",
        f"👥 Users: *{len(users)}*",
        f"🖥 Servers: *{total_servers}* total, *{active_servers}* active",
        f"🔴 Currently down: *{down_servers}*\n",
        "*Users:*"
    ]
    for u in users:
        alert = u["alert_mode"] if u["alert_mode"] == "once" else f"repeat/{u['repeat_min']}m"
        lines.append(
            f"• @{u['username']} (`{u['chat_id']}`)\n"
            f"  interval: {u['interval']}s | alert: {alert}"
        )
    lines.append("\nUse `/adminservers <chat_id>` to see a user's servers.")

    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")

async def cmd_adminservers(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update):
        await update.message.reply_text("⛔ Admin only.")
        return

    if not ctx.args:
        await update.message.reply_text("Usage: `/adminservers <chat_id>`", parse_mode="Markdown")
        return

    target_id = int(ctx.args[0])
    with get_db() as conn:
        user    = conn.execute("SELECT * FROM users WHERE chat_id=?", (target_id,)).fetchone()
        servers = conn.execute("SELECT * FROM servers WHERE chat_id=? ORDER BY name", (target_id,)).fetchall()

    if not user:
        await update.message.reply_text("❌ User not found.", parse_mode="Markdown")
        return

    lines = [f"🖥 *Servers for @{user['username']}:*\n"]
    if not servers:
        lines.append("No servers added yet.")
    else:
        for s in servers:
            icon   = {"up": "🟢", "down": "🔴", "unknown": "⚪"}.get(s["status"], "⚪")
            paused = " ⏸" if not s["active"] else ""
            if s["down_since"] and s["status"] == "down":
                extra = f" *(down {fmt_duration(time.time() - s['down_since'])})*"
            else:
                extra = ""
            lines.append(f"{icon}{paused} *{s['name']}* — `{s['ip']}`{extra}")

    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")

async def cmd_adminbroadcast(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update):
        await update.message.reply_text("⛔ Admin only.")
        return

    if not ctx.args:
        await update.message.reply_text("Usage: `/adminbroadcast <message>`", parse_mode="Markdown")
        return

    text = " ".join(ctx.args)
    with get_db() as conn:
        users = conn.execute("SELECT chat_id FROM users").fetchall()

    sent = 0
    for u in users:
        try:
            await ctx.bot.send_message(u["chat_id"], f"📢 *Broadcast:*\n{text}", parse_mode="Markdown")
            sent += 1
        except Exception as e:
            log.error(f"Broadcast failed for {u['chat_id']}: {e}")

    await update.message.reply_text(f"✅ Sent to {sent}/{len(users)} users.", parse_mode="Markdown")

# ─── MAIN ─────────────────────────────────────────────────────────────────────
async def post_init(app: Application):
    asyncio.create_task(monitor_loop(app))
    log.info("Monitor loop started.")

def main():
    init_db()
    log.info("Database initialized.")

    app = (
        Application.builder()
        .token(BOT_TOKEN)
        .post_init(post_init)
        .build()
    )

    # User commands
    app.add_handler(CommandHandler("start",        cmd_start))
    app.add_handler(CommandHandler("help",         cmd_help))
    app.add_handler(CommandHandler("addserver",    cmd_addserver))
    app.add_handler(CommandHandler("removeserver", cmd_removeserver))
    app.add_handler(CommandHandler("listservers",  cmd_listservers))
    app.add_handler(CommandHandler("status",       cmd_status))
    app.add_handler(CommandHandler("setinterval",  cmd_setinterval))
    app.add_handler(CommandHandler("setalert",     cmd_setalert))
    app.add_handler(CommandHandler("pause",        cmd_pause))
    app.add_handler(CommandHandler("resume",       cmd_resume))

    # Admin commands
    app.add_handler(CommandHandler("admin",           cmd_admin))
    app.add_handler(CommandHandler("adminservers",    cmd_adminservers))
    app.add_handler(CommandHandler("adminbroadcast",  cmd_adminbroadcast))

    log.info("Bot is running...")
    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()
