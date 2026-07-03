#!/usr/bin/env python3
"""
🔥 GITHUB TOKEN REMOVER COMMAND FOR YOUR BOT
Add this file to your bot to get /removetoken command
"""

import re
import sqlite3
import json
import os
from datetime import datetime
from pyrogram import Client, filters
from pyrogram.types import Message, InlineKeyboardButton, InlineKeyboardMarkup

# ============ ADMIN ID - CHANGE THIS ============
ADMIN_ID = 123456789  # Apna Telegram ID daalo

# ============ DATABASE SETUP ============
DB_PATH = "bot_database.db"  # Tere bot ka database path

def get_db_connection():
    """Get database connection"""
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def init_token_table():
    """Initialize tokens table if not exists"""
    conn = get_db_connection()
    cursor = conn.cursor()
    
    # Check if tokens table exists
    cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='tokens'")
    if not cursor.fetchone():
        # Create tokens table
        cursor.execute('''
            CREATE TABLE tokens (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                token TEXT UNIQUE,
                added_by INTEGER,
                added_date TEXT,
                expiry_date TEXT
            )
        ''')
        conn.commit()
    
    conn.close()

# ============ TOKEN FUNCTIONS ============
def get_all_tokens():
    """Get all tokens from database"""
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT token, added_date FROM tokens ORDER BY id DESC")
    tokens = cursor.fetchall()
    conn.close()
    return tokens

def remove_token_from_db(token):
    """Remove token from database"""
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("DELETE FROM tokens WHERE token = ?", (token,))
    affected = cursor.rowcount
    conn.commit()
    conn.close()
    return affected > 0

def remove_all_tokens_db():
    """Remove all tokens from database"""
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("DELETE FROM tokens")
    count = cursor.rowcount
    conn.commit()
    conn.close()
    return count

def is_github_token(text):
    """Check if text is a GitHub token"""
    patterns = [
        r'ghp_[A-Za-z0-9]{36}',
        r'gho_[A-Za-z0-9]{36}',
        r'ghu_[A-Za-z0-9]{36}',
        r'ghs_[A-Za-z0-9]{36}',
        r'ghr_[A-Za-z0-9]{36}',
    ]
    for pattern in patterns:
        if re.match(pattern, text):
            return True
    return False

# ============ BOT COMMAND HANDLERS ============
def register_commands(bot):
    """Register all token removal commands"""
    
    # Initialize token table
    init_token_table()
    
    # ============ /removetoken ============
    @bot.on_message(filters.command("removetoken") & filters.private)
    async def removetoken_command(client, message: Message):
        """Remove GitHub token - Interactive or direct"""
        
        # Check admin
        if message.from_user.id != ADMIN_ID:
            await message.reply("❌ **Admin only command!**")
            return
        
        try:
            args = message.text.split()
            
            # If token provided directly
            if len(args) >= 2:
                token = args[1]
                
                if not is_github_token(token):
                    await message.reply("❌ **Invalid GitHub token format!**\nToken should start with: `ghp_`, `gho_`, `ghu_`, `ghs_`, `ghr_`")
                    return
                
                if remove_token_from_db(token):
                    await message.reply(f"✅ **Token removed successfully!**\n`{token[:10]}...{token[-4:]}`")
                else:
                    await message.reply("❌ **Token not found in database!**")
                return
            
            # Interactive mode - show all tokens
            tokens = get_all_tokens()
            
            if not tokens:
                await message.reply("📭 **No GitHub tokens found in database!**\n\nUse `/addtoken <token>` to add one.")
                return
            
            # Create buttons for each token
            buttons = []
            for i, token_row in enumerate(tokens[:15]):  # Show first 15
                token = token_row[0]
                added_date = token_row[1] or "Unknown"
                token_short = token[:10] + "..." + token[-4:]
                
                buttons.append([
                    InlineKeyboardButton(
                        f"❌ {token_short}",
                        callback_data=f"removetoken_{token}"
                    )
                ])
            
            # Add bulk options
            buttons.append([
                InlineKeyboardButton("🗑️ Remove ALL Tokens", callback_data="removealltokens")
            ])
            buttons.append([
                InlineKeyboardButton("🔄 Cancel", callback_data="cancel_remove")
            ])
            
            await message.reply(
                "🗑️ **Select token to remove:**\n\n"
                f"📊 Total tokens: {len(tokens)}\n"
                "💡 Click a token to remove it",
                reply_markup=InlineKeyboardMarkup(buttons)
            )
            
        except Exception as e:
            await message.reply(f"❌ **Error:** `{str(e)}`")
    
    # ============ /cleartokens ============
    @bot.on_message(filters.command("cleartokens") & filters.private)
    async def cleartokens_command(client, message: Message):
        """Clear all tokens - with confirmation"""
        
        if message.from_user.id != ADMIN_ID:
            await message.reply("❌ **Admin only command!**")
            return
        
        tokens = get_all_tokens()
        if not tokens:
            await message.reply("📭 **No tokens to clear!**")
            return
        
        await message.reply(
            f"⚠️ **WARNING: Delete ALL {len(tokens)} tokens?**\n\n"
            "This action cannot be undone!\n\n"
            "Type `/confirm_clear` to confirm.",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🚨 CONFIRM DELETE", callback_data="confirm_clear")],
                [InlineKeyboardButton("❌ Cancel", callback_data="cancel_clear")]
            ])
        )
    
    # ============ /listtokens ============
    @bot.on_message(filters.command("listtokens") & filters.private)
    async def listtokens_command(client, message: Message):
        """List all tokens"""
        
        if message.from_user.id != ADMIN_ID:
            await message.reply("❌ **Admin only command!**")
            return
        
        tokens = get_all_tokens()
        
        if not tokens:
            await message.reply("📭 **No tokens found!**")
            return
        
        response = "📋 **GitHub Tokens List:**\n\n"
        for i, token_row in enumerate(tokens, 1):
            token = token_row[0]
            added_date = token_row[1] or "Unknown"
            response += f"{i}. `{token[:10]}...{token[-4:]}`\n"
            response += f"   📅 Added: {added_date}\n\n"
        
        # Split if too long
        if len(response) > 4000:
            parts = [response[i:i+4000] for i in range(0, len(response), 4000)]
            for part in parts:
                await message.reply(part)
        else:
            await message.reply(response)
    
    # ============ /removetokenhelp ============
    @bot.on_message(filters.command("removetokenhelp") & filters.private)
    async def removetokenhelp_command(client, message: Message):
        """Show help for token removal"""
        
        if message.from_user.id != ADMIN_ID:
            await message.reply("❌ **Admin only command!**")
            return
        
        help_text = """
📚 **GitHub Token Removal Commands:**

**/removetoken** - Interactive token removal
**/removetoken <token>** - Remove specific token directly
**/listtokens** - List all stored tokens
**/cleartokens** - Clear ALL tokens (with confirmation)

**Examples:**
`/removetoken ghp_abcdef1234567890`
`/removetoken` (then select from buttons)

**Token Formats:** 
- `ghp_` - Personal Access Token
- `gho_` - OAuth Token  
- `ghu_` - User Token
- `ghs_` - Server Token
- `ghr_` - Refresh Token
"""
        await message.reply(help_text)
    
    # ============ CALLBACK HANDLERS ============
    @bot.on_callback_query()
    async def token_callback(client, callback_query):
        data = callback_query.data
        
        # Remove specific token
        if data.startswith("removetoken_"):
            token = data.replace("removetoken_", "")
            
            if remove_token_from_db(token):
                await callback_query.answer("✅ Token removed!", show_alert=True)
                await callback_query.message.edit_text(
                    f"✅ **Token removed successfully!**\n`{token[:10]}...{token[-4:]}`"
                )
            else:
                await callback_query.answer("❌ Token not found!", show_alert=True)
                await callback_query.message.edit_text("❌ **Token not found in database!**")
        
        # Remove all tokens
        elif data == "removealltokens":
            count = get_all_tokens()
            await callback_query.message.edit_text(
                f"⚠️ **Are you sure you want to delete ALL {len(count)} tokens?**\n\n"
                "Type `/confirm_clear` to confirm."
            )
            await callback_query.answer()
        
        # Confirm clear
        elif data == "confirm_clear":
            count = remove_all_tokens_db()
            await callback_query.message.edit_text(f"🗑️ **Deleted {count} tokens successfully!**")
            await callback_query.answer(f"✅ Removed {count} tokens!", show_alert=True)
        
        # Cancel
        elif data in ["cancel_remove", "cancel_clear"]:
            await callback_query.message.edit_text("❌ **Operation cancelled.**")
            await callback_query.answer()
    
    print("✅ Token removal commands loaded!")
    print("📝 Available commands:")
    print("   /removetoken - Remove GitHub tokens")
    print("   /listtokens - List all tokens")
    print("   /cleartokens - Clear all tokens")
    print("   /removetokenhelp - Help for removal")

# ============ SETUP FUNCTION ============
def setup_token_remover(bot, admin_id=None):
    """Setup token remover in your bot"""
    global ADMIN_ID
    
    if admin_id:
        ADMIN_ID = admin_id
    
    # Initialize database
    init_token_table()
    
    # Register all commands
    register_commands(bot)
    
    print(f"✅ Token remover setup complete!")
    print(f"👑 Admin ID: {ADMIN_ID}")
    return True

# ============ USAGE ============
if __name__ == "__main__":
    print("""
    ╔═══════════════════════════════════════════╗
    ║    🔥 GITHUB TOKEN REMOVER                ║
    ║    Add /removetoken command to your bot   ║
    ╚═══════════════════════════════════════════╝
    
    HOW TO USE:
    1. Set ADMIN_ID at the top of this file
    2. Import in your main.py:
       from remove_token_command import setup_token_remover
    
    3. Call setup in your bot:
       setup_token_remover(app)
    
    4. Commands available:
       /removetoken - Interactive removal
       /listtokens - List all tokens
       /cleartokens - Clear all tokens
    """)
