# 🚀 VC Attack Bot

Telegram bot for stress testing via GitHub Actions.

## Commands

| Command | Description |
|---------|-------------|
| `/start` | Check access & menu |
| `/attack <ip> <port> <time>` | Start attack |
| `/status` | Check attack status |
| `/stop` | Stop attack |
| `/help` | Help menu |
| `/myid` | Get user ID |

### Admin Commands
| Command | Description |
|---------|-------------|
| `/addtoken <token>` | Add GitHub token |
| `/tokens` | List tokens |
| `/removetoken <num>` | Remove token |
| `/approve <id> <days>` | Approve user |
| `/remove <id>` | Remove user |
| `/users` | List users |
| `/pending` | Pending requests |
| `/broadcast <msg>` | Broadcast |
| `/maintenance <on/off>` | Toggle maintenance |

## Deploy on Railway

1. Add `BOT_TOKEN` env variable
2. Add `ADMIN_IDS` env variable (comma-separated)
3. Deploy!
