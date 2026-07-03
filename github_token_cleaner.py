#!/usr/bin/env python3
"""
🔥 GITHUB TOKEN CLEANER - AUTO REMOVE EXPIRED TOKENS
📌 GitHub Deployment Ready - Push to repo and deploy
"""

import os
import re
import sys
import json
import sqlite3
import requests
from datetime import datetime, timedelta
from pathlib import Path

# ============ CONFIG ============
TOKEN_PATTERNS = [
    r'ghp_[A-Za-z0-9]{36}',
    r'gho_[A-Za-z0-9]{36}', 
    r'ghu_[A-Za-z0-9]{36}',
    r'ghs_[A-Za-z0-9]{36}',
    r'ghr_[A-Za-z0-9]{36}',
]

DB_PATH = "tokens.db"
LOG_FILE = "token_cleanup.log"

# ============ LOGGING ============
def log_message(msg, level="INFO"):
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with open(LOG_FILE, "a") as f:
        f.write(f"[{timestamp}] [{level}] {msg}\n")
    print(f"[{timestamp}] {msg}")

# ============ TOKEN VALIDATOR ============
class TokenValidator:
    @staticmethod
    def is_github_token(text):
        for pattern in TOKEN_PATTERNS:
            if re.match(pattern, text):
                return True
        return False
    
    @staticmethod
    def check_token_validity(token):
        """Check if token is valid via GitHub API"""
        try:
            headers = {
                'Authorization': f'token {token}',
                'Accept': 'application/vnd.github.v3+json'
            }
            response = requests.get(
                'https://api.github.com/user',
                headers=headers,
                timeout=10
            )
            
            if response.status_code == 200:
                return True, "Valid"
            elif response.status_code == 401:
                return False, "Invalid/Expired"
            else:
                return False, f"Error: {response.status_code}"
        except Exception as e:
            return False, f"Connection error: {str(e)}"

# ============ DATABASE HANDLER ============
class TokenDatabase:
    def __init__(self, db_path=DB_PATH):
        self.db_path = db_path
        self.init_db()
    
    def init_db(self):
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        
        # Create tables
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS github_tokens (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                token TEXT UNIQUE,
                added_by INTEGER,
                added_date TEXT,
                expiry_date TEXT,
                status TEXT DEFAULT 'active',
                last_checked TEXT,
                validity TEXT
            )
        ''')
        
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS token_cleanup_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                token TEXT,
                action TEXT,
                timestamp TEXT,
                reason TEXT
            )
        ''')
        
        conn.commit()
        conn.close()
    
    def get_all_tokens(self):
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        cursor.execute("SELECT token, added_date, expiry_date, status FROM github_tokens")
        tokens = cursor.fetchall()
        conn.close()
        return tokens
    
    def remove_token(self, token, reason="Manual removal"):
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        
        # Log removal
        cursor.execute(
            "INSERT INTO token_cleanup_log (token, action, timestamp, reason) VALUES (?, ?, ?, ?)",
            (token, 'removed', datetime.now().isoformat(), reason)
        )
        
        # Remove token
        cursor.execute("DELETE FROM github_tokens WHERE token = ?", (token,))
        affected = cursor.rowcount
        
        conn.commit()
        conn.close()
        return affected > 0
    
    def update_token_status(self, token, status, validity):
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        cursor.execute(
            "UPDATE github_tokens SET status = ?, last_checked = ?, validity = ? WHERE token = ?",
            (status, datetime.now().isoformat(), validity, token)
        )
        conn.commit()
        conn.close()
    
    def remove_all_tokens(self):
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        
        # Get count
        cursor.execute("SELECT COUNT(*) FROM github_tokens")
        count = cursor.fetchone()[0]
        
        # Delete all
        cursor.execute("DELETE FROM github_tokens")
        
        # Log
        cursor.execute(
            "INSERT INTO token_cleanup_log (token, action, timestamp, reason) VALUES (?, ?, ?, ?)",
            ('ALL', 'bulk_removed', datetime.now().isoformat(), 'Bulk cleanup')
        )
        
        conn.commit()
        conn.close()
        return count

# ============ CLEANER ENGINE ============
class TokenCleaner:
    def __init__(self):
        self.db = TokenDatabase()
        self.validator = TokenValidator()
    
    def scan_and_clean(self):
        """Scan all tokens and remove expired ones"""
        log_message("🚀 Starting token cleanup scan...")
        
        tokens = self.db.get_all_tokens()
        if not tokens:
            log_message("📭 No tokens found in database")
            return {"total": 0, "removed": 0, "valid": 0, "invalid": 0}
        
        log_message(f"📊 Found {len(tokens)} tokens to check")
        
        removed_count = 0
        valid_count = 0
        invalid_count = 0
        
        for token_data in tokens:
            token = token_data[0]
            expiry_date = token_data[2]
            
            # Check if expired by date
            if expiry_date and datetime.fromisoformat(expiry_date) < datetime.now():
                if self.db.remove_token(token, "Expired by date"):
                    removed_count += 1
                    log_message(f"🗑️ Removed expired token: {token[:10]}...")
                    continue
            
            # Check via API
            is_valid, message = self.validator.check_token_validity(token)
            
            if is_valid:
                valid_count += 1
                self.db.update_token_status(token, 'active', 'valid')
                log_message(f"✅ Valid token: {token[:10]}...")
            else:
                invalid_count += 1
                if self.db.remove_token(token, f"Invalid: {message}"):
                    removed_count += 1
                    log_message(f"🗑️ Removed invalid token: {token[:10]}... ({message})")
        
        result = {
            "total": len(tokens),
            "removed": removed_count,
            "valid": valid_count,
            "invalid": invalid_count
        }
        
        log_message(f"✅ Cleanup complete! Removed: {removed_count}, Valid: {valid_count}, Invalid: {invalid_count}")
        return result
    
    def remove_specific_token(self, token):
        """Remove specific token by value"""
        if not self.validator.is_github_token(token):
            return False, "Invalid token format"
        
        if self.db.remove_token(token, "Manual removal"):
            return True, "Token removed successfully"
        return False, "Token not found"
    
    def list_all_tokens(self):
        """Get all tokens with status"""
        tokens = self.db.get_all_tokens()
        result = []
        for token_data in tokens:
            result.append({
                "token": token_data[0],
                "added_date": token_data[1],
                "expiry_date": token_data[2],
                "status": token_data[3]
            })
        return result

# ============ GITHUB ACTION HANDLER ============
def github_action_mode():
    """Run in GitHub Action mode - silent cleanup"""
    log_message("🤖 Running in GitHub Action mode...")
    
    cleaner = TokenCleaner()
    result = cleaner.scan_and_clean()
    
    # Create summary
    summary = f"""
## 🔥 GitHub Token Cleanup Report

- **Total Tokens Checked:** {result['total']}
- **✅ Valid Tokens:** {result['valid']}
- **🗑️ Removed Tokens:** {result['removed']}
- **❌ Invalid Tokens:** {result['invalid']}

**Cleanup completed at:** {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}

_Automated cleanup by GitHub Token Cleaner_
"""
    
    # Write to GitHub Actions summary
    with open(os.environ.get('GITHUB_STEP_SUMMARY', 'cleanup_report.md'), 'w') as f:
        f.write(summary)
    
    log_message("📝 Cleanup report generated")
    return result

# ============ MAIN ============
def main():
    print("""
    ╔═══════════════════════════════════════════╗
    ║    🔥 GITHUB TOKEN CLEANER                ║
    ║    Auto-remove expired/invalid tokens     ║
    ╚═══════════════════════════════════════════╝
    """)
    
    # Check if running in GitHub Action
    if os.environ.get('GITHUB_ACTIONS') == 'true':
        github_action_mode()
        return
    
    # Interactive mode
    print("Choose an option:")
    print("1. Scan and clean all tokens")
    print("2. Remove specific token")
    print("3. List all tokens")
    print("4. Remove ALL tokens (WARNING!)")
    print("5. Exit")
    
    choice = input("\nEnter choice (1-5): ")
    
    cleaner = TokenCleaner()
    
    if choice == "1":
        result = cleaner.scan_and_clean()
        print(f"\n✅ Cleanup complete!")
        print(f"   Total: {result['total']}")
        print(f"   Removed: {result['removed']}")
        print(f"   Valid: {result['valid']}")
        print(f"   Invalid: {result['invalid']}")
    
    elif choice == "2":
        token = input("Enter token to remove: ")
        success, message = cleaner.remove_specific_token(token)
        print(f"\n{'✅' if success else '❌'} {message}")
    
    elif choice == "3":
        tokens = cleaner.list_all_tokens()
        if not tokens:
            print("\n📭 No tokens found")
        else:
            print(f"\n📋 Found {len(tokens)} tokens:")
            for i, token_info in enumerate(tokens, 1):
                status = "✅" if token_info['status'] == 'active' else "❌"
                print(f"{i}. {status} {token_info['token'][:10]}...")
                print(f"   Added: {token_info['added_date']}")
                print(f"   Expiry: {token_info['expiry_date']}")
    
    elif choice == "4":
        confirm = input("⚠️ WARNING: Delete ALL tokens? Type 'DELETE ALL' to confirm: ")
        if confirm == "DELETE ALL":
            count = cleaner.db.remove_all_tokens()
            print(f"🗑️ Deleted {count} tokens")
        else:
            print("❌ Operation cancelled")
    
    else:
        print("👋 Exiting...")

# ============ AUTO-RUN ON DEPLOY ============
if __name__ == "__main__":
    # Auto-run cleanup on deploy
    if len(sys.argv) > 1 and sys.argv[1] == "--auto":
        log_message("🚀 Auto-cleanup mode activated")
        cleaner = TokenCleaner()
        result = cleaner.scan_and_clean()
        print(f"✅ Auto-cleanup complete! Removed {result['removed']} tokens")
    else:
        main()
