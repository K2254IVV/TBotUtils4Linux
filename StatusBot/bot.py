import subprocess
import logging
from functools import wraps
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes

# Configuration
BOT_TOKEN = "YOUR_BOT_TOKEN_HERE"
ALLOWED_USER_ID = 123456789 # YOUR REAL USER ID HERE

# Enable logging
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

def run_command(cmd):
    """Execute shell command and return output"""
    try:
        result = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=10)
        return result.stdout.strip() if result.returncode == 0 else f"Error: {result.stderr}"
    except subprocess.TimeoutExpired:
        return "Error: Command timed out"
    except Exception as e:
        return f"Command failed: {str(e)}"

def restricted(func):
    """Decorator to restrict access to specific user"""
    @wraps(func)
    async def wrapped(update: Update, context: ContextTypes.DEFAULT_TYPE, *args, **kwargs):
        user_id = update.effective_user.id
        if user_id != ALLOWED_USER_ID:
            await update.message.reply_text(
                "⛔ Access denied.\n"
                "To install StatusBot, clone the repository https://github.com/K2254IVV/TBotUtils4Linux"
            )
            return
        return await func(update, context, *args, **kwargs)
    return wrapped

async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /start command"""
    user_id = update.effective_user.id
    if user_id != ALLOWED_USER_ID:
        await update.message.reply_text(
            "⛔ Access denied.\n"
            "To install StatusBot, clone the repository https://github.com/K2254IVV/TBotUtils4Linux"
        )
        return
    
    welcome_text = """🔧 *pyStatusBot Active*
    
Available commands:
/status - System status information
/uptimeinfo - System uptime information
    """
    await update.message.reply_text(welcome_text, parse_mode='Markdown')

@restricted
async def status_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /status command"""
    try:
        # Collect system information
        hostname = run_command("cat /etc/hostname")
        username = run_command("whoami")
        product_name = run_command("cat /sys/devices/virtual/dmi/id/product_name")
        disk_info = run_command("df -h")
        ram_info = run_command("free -h")
        ip_info = run_command("ip -4 -o addr show | awk '{split($4, a, \"/\"); print \"📝 \" $2 \": \" a[1]}'")
        os_info = run_command(". /etc/os-release && echo \"📝 $NAME $VERSION\"")
        temp_info = run_command("sensors | grep -E 'Adapter|temp1|Composite' | grep -v 'high\\|low\\|crit'")
        
        # Format message with Markdown
        message = f"""🏴‍☠️ {hostname}:{username} {product_name} 🏴‍☠️ Status:

✅ Free Disk Space ✅:
```fdsinfo
{disk_info}
```

🤠 RAM Info 🤠:
```rinfo
{ram_info}
```

🌐 IPv4 addresses:
```IPlist
{ip_info}
```

🔥 Temperature Information:
```tempinfo
{temp_info}
```

Summary:
```summary
{os_info}
{product_name}

{disk_info}

{ram_info}

{ip_info}

{temp_info}
```"""
        
        await update.message.reply_text(message, parse_mode='Markdown')
        
    except Exception as e:
        logger.error(f"Error in status command: {e}")
        await update.message.reply_text("❌ Error fetching system status.")

@restricted
async def uptimeinfo_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /uptimeinfo command"""
    try:
        # Get system uptime in HH:MM:SS format
        uptime_seconds = run_command("cat /proc/uptime | awk '{print int($1)}'")
        
        if "Error:" not in uptime_seconds:
            seconds = int(uptime_seconds)
            hours = seconds // 3600
            minutes = (seconds % 3600) // 60
            secs = seconds % 60
            uptime_output = f"{hours:02d}:{minutes:02d}:{secs:02d}"
        else:
            uptime_output = "Error getting uptime"
        
        # Format the message exactly as requested
        message = f"""```uptimeinfo
🕒: {uptime_output}
```"""
        
        await update.message.reply_text(message, parse_mode='Markdown')
        
    except Exception as e:
        logger.error(f"Error in uptimeinfo command: {e}")
        await update.message.reply_text("❌ Error fetching uptime information.")

def main():
    """Start the bot"""
    # Create application
    application = Application.builder().token(BOT_TOKEN).build()
    
    # Add command handlers
    application.add_handler(CommandHandler("start", start_command))
    application.add_handler(CommandHandler("status", status_command))
    application.add_handler(CommandHandler("uptimeinfo", uptimeinfo_command))
    
    # Start bot
    print("🤖 pyStatusBot is running...")
    print(f"👤 Allowed User ID: {ALLOWED_USER_ID}")
    application.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == '__main__':
    main()
