# 📖 Full Setup Guide — Server Monitor Bot

This guide walks through the complete setup, from an empty machine to a working
Telegram bot that monitors your servers and alerts you the moment one goes down.

---

## Prerequisites

- A monitoring server (VPS) running Ubuntu 22.04+, reachable via SSH
- A local machine to run Ansible from (Windows users: via WSL2)
- A Telegram account

---

## Step 1 — Create Your Telegram Bot

1. Open Telegram and search for **@BotFather**
2. Send:
   ```
   /newbot
   ```
3. Give it a name (anything), then a username ending in `bot` (e.g. `myservermonitor_bot`)
4. BotFather replies with a **bot token** that looks like:
   ```
   123456789:ABCdefGhIjKlmNoPQRstuVWXyz
   ```
   Save this — you'll need it in Step 4.

---

## Step 2 — Get Your Telegram Chat ID

1. Search for **@userinfobot** in Telegram and open it
2. Send `/start`
3. It replies with your info, including:
   ```
   Id: 987654321
   ```
   Save this number — this is your **chat ID**.

4. **Important:** search for *your own bot* by its username and hit **Start**.
   Telegram bots cannot message a user who hasn't started a conversation with them first —
   this trips up almost everyone the first time.

---

## Step 3 — Install Ansible (Control Machine)

If you're on **Windows**, install WSL2 first:

```powershell
wsl --install
```

Restart your PC, then open the Ubuntu terminal it installs and continue below.

On Linux/WSL:

```bash
sudo apt update && sudo apt upgrade -y
sudo apt install python3 python3-pip -y
pip3 install ansible
```

Verify:

```bash
ansible --version
```

---

## Step 4 — Set Up SSH Access to Your Monitoring Server

Generate an SSH key (skip if you already have one):

```bash
ssh-keygen -t ed25519
```

Copy it to your monitoring server:

```bash
ssh-copy-id your_user@YOUR_SERVER_IP
```

Test passwordless login:

```bash
ssh your_user@YOUR_SERVER_IP
```

You should get in with **no password prompt**.

---

## Step 5 — Clone and Configure the Repo

```bash
git clone https://github.com/YOUR_USERNAME/server-monitor-bot.git
cd server-monitor-bot
```

Set up your inventory:

```bash
nano inventory/hosts.ini
```

```ini
[monitoring_server]
monitor01 ansible_host=YOUR_SERVER_IP ansible_user=your_user
```

Set up your config from the template:

```bash
cp inventory/group_vars/all.yml.example inventory/group_vars/all.yml
nano inventory/group_vars/all.yml
```

```yaml
telegram_bot_token: "YOUR_BOT_TOKEN"
telegram_admin_chat_id: "YOUR_CHAT_ID"
```

Make sure this real file is git-ignored so your token never gets committed:

```bash
cat .gitignore
```

It should include:
```
*.secret
inventory/group_vars/all.yml
monitor-bot/monitoring.db
__pycache__/
*.pyc
.venv/
```

---

## Step 6 — Deploy With Ansible

Run the playbook — this installs Python, dependencies, copies the bot code, and
sets it up as a systemd service that starts on boot:

```bash
ansible-playbook playbooks/deploy_bot.yml
```

Verify it's running on the server:

```bash
ssh your_user@YOUR_SERVER_IP "sudo systemctl status monitor-bot --no-pager"
```

You should see:
```
Active: active (running)
```

---

## Step 7 — Test the Bot

In Telegram, message your bot:

```
/start
/addserver 10.0.0.5 "My VPS"
/setinterval 30
/status
```

The bot should reply confirming the server was added, and `/status` should show
its current up/down state.

---

## Step 8 — Understanding the Alert Logic (and a Bug We Fixed)

**Original design:** ping once per interval, alert immediately on failure.

**Problem encountered during testing:** a single dropped ICMP packet (normal
network noise) would cause the bot to fire a "server down" alert, then seconds
later a "server back up" alert — a false alarm, not a real outage. This is
commonly called **flapping**.

**Fix:** instead of one ping per check, the bot now sends **3 pings, one second
apart**, and only declares the server down if **2 of the 3 fail**:

```python
results = []
for _ in range(3):
    result = await loop.run_in_executor(None, do_ping, s["ip"])
    results.append(result)
    await asyncio.sleep(1)

is_up = results.count(True) >= 2   # majority wins
```

This means a single bad packet can never trigger a false alarm — the server has
to be genuinely unreachable across multiple probes before Telegram gets pinged.

**Timing note:** if your interval is set to 30 seconds, the actual check cycle
is closer to ~33 seconds total (30s wait + ~3s for the three pings). This is
expected and doesn't affect alert accuracy.

---

## Step 9 — Concurrency (Why Alerts Don't Get Delayed)

With multiple users each monitoring their own servers, checking them one at a
time in sequence means later users wait behind earlier ones. The bot instead
checks every user's servers **at the same time** using `asyncio.gather`:

```python
await asyncio.gather(*[check_user(app, u) for u in users])
```

So whether there are 2 users or 20, every server is probed concurrently on
every cycle — no one's alerts are delayed by someone else's servers.

---

## Step 10 — Per-User Isolation

Every server and setting is stored in SQLite keyed to the Telegram **chat ID**
of the user who added it:

```
Ziad adds 10.66.66.1  →  only Ziad is alerted about 10.66.66.1
Ahmed adds 192.168.1.5 →  only Ahmed is alerted about 192.168.1.5
```

Users never see each other's servers or alerts. The admin (set via
`telegram_admin_chat_id`) can use `/admin` to see a summary of all users and
how many servers each is monitoring, without exposing details to anyone else.

---

## Step 11 — Redeploying After Code Changes

Whenever you edit `monitor-bot/bot.py` locally, push the update with a single
command — Ansible handles copying the file and restarting the service:

```bash
ansible-playbook playbooks/deploy_bot.yml
```

---

## Troubleshooting

**Bot doesn't respond on Telegram**
Check the service is actually running:
```bash
ssh your_user@YOUR_SERVER_IP "sudo systemctl status monitor-bot --no-pager"
```

**Only the admin receives alerts, other users don't**
Confirm every user has sent `/start` to the bot at least once — Telegram bots
cannot message users who haven't initiated contact first. You can verify chat
IDs directly via:
```
https://api.telegram.org/botYOUR_BOT_TOKEN/getUpdates
```
(No `<` or `>` characters around the token — a common copy-paste mistake.)

**Ansible can't find a role / playbook fails immediately**
Make sure you're running `ansible-playbook` from inside the repo root, and that
`ansible.cfg` is present so Ansible picks up the correct inventory and roles
path automatically.

**Permission denied connecting via SSH**
Re-run `ssh-copy-id` and confirm `ansible_user` in `hosts.ini` matches the
account you copied the key to.

---

## Summary

| Step | What Happens |
|---|---|
| 1–2 | Create Telegram bot + get your chat ID |
| 3–4 | Install Ansible + set up SSH access |
| 5 | Configure inventory and secrets |
| 6 | Deploy via Ansible (systemd service installed) |
| 7 | Test bot commands in Telegram |
| 8–10 | Understand the reliability features under the hood |
| 11 | One-command redeploy for future changes |

You now have a private, multi-user, self-hosted server monitoring system
running entirely on infrastructure you control.
