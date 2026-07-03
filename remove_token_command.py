#!/usr/bin/env python3
"""
🔥 GITHUB TOKEN REMOVER COMMAND FOR RYAMAHA0963-BOT
Add /removetoken command to your bot
"""

import re
import sqlite3
from datetime import datetime
from pyrogram import Client, filters
from pyrogram.types import Message, InlineKeyboardButton, InlineKeyboardMarkup

# ============ ADMIN ID - CHANGE KARO ============
ADMIN_ID = 8819216195  # APNA TELEGRAM ID DAALO

# ============ DATABASE ============
DB_PATH = "bot_database.db"

def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def init_tokens_table():
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS tokens (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            token TEXT UNIQUE,
            added_by INTEGER,
            added_date TEXT
        )
    ''')
    conn.commit()
    conn.close()

def get_all_tokens():
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("SELECT token, added_date FROM tokens ORDER BY id DESC")
    tokens = cursor.fetchall()
    conn.close()
    return tokens

def remove_token(token):
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("DELETE FROM tokens WHERE token = ?", (token,))
    affected = cursor.rowcount
    conn.commit()
    conn.close()
    return affected > 0

def remove_all_tokens():
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("DELETE FROM tokens")
    count = cursor.rowcount
    conn.commit()
    conn.close()
    return count

def is_github_token(text):
    patterns = [r'ghp_[A-Za-z0-9]{36}', r'gho_[A-Za-z0-9]{36}', 
                r'ghu_[A-Za-z0-9]{36}', r'ghs_[A-Za-z0-9]{36}', r'ghr_[A-Za-z0-9]{36}']
    for pattern in patterns:
        if re.match(pattern, text):
            return True
    return False

# ============ COMMANDS ============
def register_commands(bot):
    init_tokens_table()
    
    # /removetoken
    @bot.on_message(filters.command("removetoken") & filters.private)
    async def removetoken_cmd(client, message):
        if message.from_user.id != ADMIN_ID:
            await message.reply("❌ Admin only!")
            return
        
        args = message.text.split()
        
        # Direct token remove
        if len(args) >= 2:
            token = args[1]
            if not is_github_token(token):
                await message.reply("❌ Invalid GitHub token!")
                return
            if remove_token(token):
                await message.reply(f"✅ Token removed: `{token[:10]}...`")
            else:
                await message.reply("❌ Token not found!")
            return
        
        # Interactive mode
        tokens = get_all_tokens()
        if not tokens:
            await message.reply("📭 No tokens found!")
            return
        
        buttons = []
        for token_row in tokens[:15]:
            token = token_row[0]
            buttons.append([InlineKeyboardButton(
                f"❌ {token[:10]}...{token[-4:]}",
                callback_data=f"rmtoken_{token}"
            )])
        
        buttons.append([InlineKeyboardButton("🗑️ Remove ALL", callback_data="rmall")])
        buttons.append([InlineKeyboardButton("❌ Cancel", callback_data="rmcancel")])
        
        await message.reply(
            f"🗑️ **Select token to remove:**\nTotal: {len(tokens)}",
            reply_markup=InlineKeyboardMarkup(buttons)
        )
    
    # /listtokens
    @bot.on_message(filters.command("listtokens") & filters.private)
    async def listtokens_cmd(client, message):
        if message.from_user.id != ADMIN_ID:
            await message.reply("❌ Admin only!")
            return
        
        tokens = get_all_tokens()
        if not tokens:
            await message.reply("📭 No tokens found!")
            return
        
        text = "📋 **GitHub Tokens:**\n\n"
        for i, token_row in enumerate(tokens, 1):
            token = token_row[0]
            text += f"{i}. `{token[:10]}...{token[-4:]}`\n"
        
        await message.reply(text)
    
    # /cleartokens
    @bot.on_message(filters.command("cleartokens") & filters.private)
    async def cleartokens_cmd(client, message):
        if message.from_user.id != ADMIN_ID:
            await message.reply("❌ Admin only!")
            return
        
        await message.reply(
            "⚠️ **Delete ALL tokens?**\nType `/confirm_clear` to confirm."
        )
    
    # /confirm_clear
    @bot.on_message(filters.command("confirm_clear") & filters.private)
    async def confirm_clear_cmd(client, message):
        if message.from_user.id != ADMIN_ID:
            return
        
        count = remove_all_tokens()
        await message.reply(f"🗑️ Deleted {count} tokens!")
    
    # Callbacks
    @bot.on_callback_query()
    async def callback_handler(client, cb):
        data = cb.data
        
        if data.startswith("rmtoken_"):
            token = data.replace("rmtoken_", "")
            if remove_token(token):
                await cb.answer("✅ Removed!", show_alert=True)
                await cb.message.edit_text(f"✅ Removed: `{token[:10]}...`")
            else:
                await cb.answer("❌ Not found!")
        
        elif data == "rmall":
            await cb.message.edit_text("⚠️ Type `/confirm_clear` to delete ALL tokens")
            await cb.answer()
        
        elif data == "rmcancel":
            await cb.message.edit_text("❌ Cancelled")
            await cb.answer()
    
    print("✅ Token removal commands loaded!")

# ============ SETUP ============
def setup_token_remover(bot, admin_id=None):
    global ADMIN_ID
    if admin_id:
        ADMIN_ID = admin_id
    register_commands(bot)
    print(f"✅ Setup complete! Admin: {ADMIN_ID}")
    return True

if __name__ == "__main__":
    print("""
    🔥 GITHUB TOKEN REMOVER
    Add to your bot: setup_token_remover(bot)
    """)
