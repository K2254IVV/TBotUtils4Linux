import asyncio
import logging
import paramiko
import html
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes, filters

# Configuration
BOT_TOKEN = "YOUR_BOT_TOKEN_HERE"
ALLOWED_USER_ID = 1234567890  # Your User ID

# Enable logging
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

class SSHTunnelBot:
    def __init__(self):
        self.ssh_clients = {}  # user_id -> SSHClient
        self.ssh_sessions = {}  # user_id -> SSH session info
        self.active_commands = {}  # user_id -> (channel, task, message)
        self.input_queues = {}  # user_id -> asyncio.Queue
        self.current_dirs = {}  # user_id -> current directory
        
    async def connect_ssh(self, user_id, host_port, username, password):
        """Establish SSH connection"""
        try:
            # Parse host:port
            if ":" in host_port:
                host, port = host_port.split(":", 1)
                port = int(port)
            else:
                host = host_port
                port = 22
            
            # Create SSH client
            client = paramiko.SSHClient()
            client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
            
            # Connect with timeout
            client.connect(
                hostname=host,
                port=port,
                username=username,
                password=password,
                timeout=10,
                banner_timeout=10,
                auth_timeout=10
            )
            
            # Get initial directory
            stdin, stdout, stderr = client.exec_command("pwd")
            initial_dir = stdout.read().decode().strip()
            
            # Store connection
            self.ssh_clients[user_id] = client
            self.ssh_sessions[user_id] = {
                'host': host,
                'port': port,
                'username': username,
                'connected_at': asyncio.get_event_loop().time()
            }
            self.current_dirs[user_id] = initial_dir
            
            # Create input queue for this user
            self.input_queues[user_id] = asyncio.Queue()
            
            return f"✅ Connected to {username}@{host}:{port}\n📁 Current directory: {initial_dir}"
            
        except paramiko.AuthenticationException:
            return "❌ Authentication failed. Check username/password."
        except paramiko.SSHException as e:
            return f"❌ SSH Error: {str(e)}"
        except Exception as e:
            return f"❌ Connection failed: {str(e)}"
    
    async def disconnect_ssh(self, user_id):
        """Disconnect SSH session"""
        if user_id not in self.ssh_clients:
            return "❌ Not connected to any SSH server."
        
        try:
            # Stop any active command
            await self.stop_command(user_id)
            
            client = self.ssh_clients[user_id]
            session_info = self.ssh_sessions[user_id]
            host = session_info['host']
            username = session_info['username']
            
            client.close()
            
            # Clean up
            if user_id in self.ssh_clients:
                del self.ssh_clients[user_id]
            if user_id in self.ssh_sessions:
                del self.ssh_sessions[user_id]
            if user_id in self.input_queues:
                del self.input_queues[user_id]
            if user_id in self.current_dirs:
                del self.current_dirs[user_id]
            
            return f"✅ Disconnected from {username}@{host}"
            
        except Exception as e:
            return f"❌ Error disconnecting: {str(e)}"
    
    async def execute_command_realtime(self, user_id, command, message_callback):
        """Execute command with real-time output streaming"""
        if user_id not in self.ssh_clients:
            return None, "❌ Not connected to SSH. Use /connect first."
        
        try:
            client = self.ssh_clients[user_id]
            
            # Check if this is a cd command
            is_cd_command = command.strip().startswith("cd ")
            
            # For cd commands, handle specially
            if is_cd_command:
                # Extract path from cd command
                cd_path = command[3:].strip()
                
                # Execute cd and get new directory
                # We use && pwd to get the new directory after cd
                cd_command = f"cd {cd_path} 2>/dev/null && pwd || echo 'Error: Directory not found'"
                
                stdin, stdout, stderr = client.exec_command(cd_command)
                output = stdout.read().decode().strip()
                error = stderr.read().decode().strip()
                
                if output and "Error:" not in output:
                    # Update current directory
                    new_dir = output
                    old_dir = self.current_dirs.get(user_id, "~")
                    self.current_dirs[user_id] = new_dir
                    return command, f"📁 Directory changed:\n{old_dir} → {new_dir}"
                else:
                    error_msg = error if error else "Directory not found or permission denied"
                    return command, f"❌ Failed to change directory: {error_msg}"
            
            # For regular commands, prepend with cd to current directory
            else:
                # Get current directory
                current_dir = self.current_dirs.get(user_id, "~")
                
                # Create SSH channel
                transport = client.get_transport()
                channel = transport.open_session()
                
                # Request PTY for better command handling
                channel.get_pty(term='xterm', width=80, height=24)
                
                # Execute command with cd to current directory
                actual_command = f"cd '{current_dir}' && {command}"
                channel.exec_command(actual_command)
                
                # Store active command
                self.active_commands[user_id] = {
                    'channel': channel,
                    'command': command,
                    'start_time': asyncio.get_event_loop().time(),
                    'actual_command': actual_command
                }
                
                output_buffer = ""
                last_update_time = asyncio.get_event_loop().time()
                update_interval = 0.3  # Update every 0.3 seconds
                
                # Function to send input if available
                async def check_and_send_input():
                    if user_id in self.input_queues:
                        try:
                            # Check if there's input in the queue (non-blocking)
                            input_data = await asyncio.wait_for(
                                self.input_queues[user_id].get(), 
                                timeout=0.01
                            )
                            if input_data and channel.send_ready():
                                channel.send(input_data + "\n")
                                await message_callback(command, f"[Input sent: {input_data}]\n" + output_buffer)
                                return True
                        except (asyncio.TimeoutError, asyncio.QueueEmpty):
                            pass
                    return False
                
                while True:
                    # Check for input to send
                    await check_and_send_input()
                    
                    # Check if channel has data
                    if channel.recv_ready():
                        while channel.recv_ready():
                            try:
                                data = channel.recv(1024).decode('utf-8', errors='ignore')
                                if data:
                                    output_buffer += data
                            except:
                                break
                    
                    # Check if channel has stderr data
                    if channel.recv_stderr_ready():
                        while channel.recv_stderr_ready():
                            try:
                                data = channel.recv_stderr(1024).decode('utf-8', errors='ignore')
                                if data:
                                    output_buffer += f"[stderr] {data}"
                            except:
                                break
                    
                    # Check if command has finished
                    if channel.exit_status_ready():
                        # Get any remaining data
                        while channel.recv_ready():
                            try:
                                data = channel.recv(1024).decode('utf-8', errors='ignore')
                                if data:
                                    output_buffer += data
                            except:
                                break
                        break
                    
                    # Update message at intervals if there's new output
                    current_time = asyncio.get_event_loop().time()
                    if output_buffer and (current_time - last_update_time > update_interval):
                        await message_callback(command, output_buffer)
                        last_update_time = current_time
                    
                    # Small delay
                    await asyncio.sleep(0.05)
                
                # Clean up active command
                if user_id in self.active_commands:
                    del self.active_commands[user_id]
                
                # Get exit status
                exit_status = channel.recv_exit_status()
                
                # Add exit status to output if non-zero
                if exit_status != 0:
                    output_buffer += f"\n\nExit status: {exit_status}"
                
                return command, output_buffer
            
        except paramiko.SSHException as e:
            # Clean up on error
            if user_id in self.active_commands:
                del self.active_commands[user_id]
            return command, f"❌ SSH Error: {str(e)}"
        except Exception as e:
            if user_id in self.active_commands:
                del self.active_commands[user_id]
            return command, f"❌ Execution failed: {str(e)}"
    
    async def stop_command(self, user_id):
        """Stop currently running command (Ctrl+C)"""
        if user_id not in self.active_commands:
            return False, "❌ No active command to stop."
        
        try:
            cmd_info = self.active_commands[user_id]
            channel = cmd_info['channel']
            command = cmd_info['command']
            
            # Send Ctrl+C (SIGINT) - ASCII code 3
            channel.send('\x03')
            
            # Wait a bit for command to terminate
            await asyncio.sleep(0.5)
            
            # Clean up
            del self.active_commands[user_id]
            
            return True, f"⏹️ Command '{command}' stopped (Ctrl+C sent)"
            
        except Exception as e:
            return False, f"❌ Error stopping command: {str(e)}"
    
    async def send_input(self, user_id, input_data):
        """Send input to currently running command"""
        if user_id not in self.active_commands:
            return False, "❌ No active command to send input to."
        
        if user_id not in self.input_queues:
            return False, "❌ Input queue not available."
        
        try:
            # Put input in queue
            await self.input_queues[user_id].put(input_data)
            return True, f"📥 Input queued: '{input_data}'"
        except Exception as e:
            return False, f"❌ Error queuing input: {str(e)}"
    
    async def get_current_dir(self, user_id):
        """Get current directory for user"""
        if user_id in self.current_dirs:
            return self.current_dirs[user_id]
        return "~"

# Global bot instance
ssh_bot = SSHTunnelBot()

def restricted(func):
    """Decorator to restrict access to specific user"""
    from functools import wraps
    
    @wraps(func)
    async def wrapped(update: Update, context: ContextTypes.DEFAULT_TYPE, *args, **kwargs):
        user_id = update.effective_user.id
        if user_id != ALLOWED_USER_ID:
            await update.message.reply_text(
                "⛔ Access denied.\n"
                "TheTunnel Bot - Secure SSH Bridge"
            )
            return
        return await func(update, context, *args, **kwargs)
    return wrapped

@restricted
async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /start command"""
    welcome_text = """🔐 TheTunnel Bot - SSH Bridge

Available commands:
/connect <IP:PORT> <Username> <Password> - Connect to SSH server
/execute <command> - Execute command on connected server
/pwd - Show current directory
/stop - Stop current command (Ctrl+C)
/input <data> - Send input to running command
/disconnect - Disconnect from SSH server
/status - Show connection status

Examples:
/connect 192.168.1.100:22 root mypassword
/execute cd /var/www
/execute ls -la
/pwd
"""
    await update.message.reply_text(welcome_text)

@restricted
async def connect_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /connect command"""
    if not context.args or len(context.args) < 3:
        await update.message.reply_text(
            "❌ Usage: /connect <IP:PORT> <Username> <Password>\n"
            "Example: /connect 192.168.1.100:22 root mypassword"
        )
        return
    
    host_port = context.args[0]
    username = context.args[1]
    password = context.args[2]
    
    user_id = update.effective_user.id
    
    # Show connecting message
    connecting_msg = await update.message.reply_text(f"🔗 Connecting to {username}@{host_port}...")
    
    # Connect
    result = await ssh_bot.connect_ssh(user_id, host_port, username, password)
    
    # Update message
    await connecting_msg.edit_text(result)

@restricted
async def execute_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /execute command with real-time updates"""
    if not context.args:
        await update.message.reply_text(
            "❌ Usage: /execute <command>\n"
            "Example: /execute ls -la"
        )
        return
    
    command = " ".join(context.args)
    user_id = update.effective_user.id
    
    # Check if already executing a command
    if user_id in ssh_bot.active_commands:
        await update.message.reply_text(
            "⚠️ Another command is already running.\n"
            "Use /stop to stop it first."
        )
        return
    
    # Store message reference for updates
    class MessageUpdater:
        def __init__(self, message):
            self.message = message
            self.last_output = ""
        
        async def update(self, cmd, output):
            # Clean the output
            output_clean = html.escape(output)
            cmd_clean = html.escape(cmd)
            
            # Format response
            response = f'output "{cmd_clean}":\n<pre>{output_clean}</pre>'
            
            # Limit to 4096 characters for Telegram
            if len(response) > 4096:
                # Keep only last 4000 characters
                output_clean = output_clean[-4000:]
                response = f'output "{cmd_clean}":\n<pre>{output_clean}</pre>'
            
            # Update message
            try:
                await self.message.edit_text(response, parse_mode='HTML')
            except Exception as e:
                logger.error(f"Error updating message: {e}")
    
    # Create initial message
    executing_msg = await update.message.reply_text(f"⚡ Executing '{command[:50]}'...")
    
    # Create updater
    updater = MessageUpdater(executing_msg)
    
    # Execute command with real-time updates
    cmd_executed, output = await ssh_bot.execute_command_realtime(
        user_id, 
        command, 
        updater.update
    )
    
    # Final update
    if output.startswith("❌"):
        await executing_msg.edit_text(output)
    else:
        output_clean = html.escape(output)
        cmd_clean = html.escape(cmd_executed)
        response = f'output "{cmd_clean}":\n<pre>{output_clean}</pre>'
        
        # Handle long output
        if len(response) > 4096:
            # Split into chunks
            chunks = []
            chunk_size = 4000
            
            for i in range(0, len(output_clean), chunk_size):
                chunk = output_clean[i:i+chunk_size]
                if i == 0:
                    chunk_response = f'output "{cmd_clean}":\n<pre>{chunk}</pre>'
                else:
                    chunk_response = f'<pre>{chunk}</pre>'
                chunks.append(chunk_response)
            
            # Update first chunk
            await executing_msg.edit_text(chunks[0], parse_mode='HTML')
            
            # Send remaining chunks
            for chunk in chunks[1:]:
                await update.message.reply_text(chunk, parse_mode='HTML')
        else:
            await executing_msg.edit_text(response, parse_mode='HTML')

@restricted
async def pwd_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show current directory"""
    user_id = update.effective_user.id
    
    if user_id not in ssh_bot.ssh_clients:
        await update.message.reply_text("❌ Not connected to SSH")
        return
    
    current_dir = ssh_bot.current_dirs.get(user_id, "~")
    
    await update.message.reply_text(f"📁 Current directory:\n`{current_dir}`", parse_mode='Markdown')

@restricted
async def stop_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /stop command (Ctrl+C)"""
    user_id = update.effective_user.id
    
    # Stop the command
    success, message = await ssh_bot.stop_command(user_id)
    
    await update.message.reply_text(message)

@restricted
async def input_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /input command"""
    if not context.args:
        await update.message.reply_text(
            "❌ Usage: /input <data>\n"
            "Example: /input yes\n"
            "Example: /input password123\n"
            "Example: /input q (to quit interactive programs)"
        )
        return
    
    input_data = " ".join(context.args)
    user_id = update.effective_user.id
    
    # Send input to command
    success, message = await ssh_bot.send_input(user_id, input_data)
    
    await update.message.reply_text(message)

@restricted
async def disconnect_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /disconnect command"""
    user_id = update.effective_user.id
    
    # Show disconnecting message
    disconnecting_msg = await update.message.reply_text("🔌 Disconnecting...")
    
    # Disconnect
    result = await ssh_bot.disconnect_ssh(user_id)
    
    # Update message
    await disconnecting_msg.edit_text(result)

@restricted
async def status_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show SSH connection status"""
    user_id = update.effective_user.id
    
    status_lines = []
    
    # Connection status
    if user_id in ssh_bot.ssh_clients:
        session = ssh_bot.ssh_sessions[user_id]
        uptime = asyncio.get_event_loop().time() - session['connected_at']
        
        status_lines.append("🔗 SSH Connection Active")
        status_lines.append(f"Host: {session['host']}:{session['port']}")
        status_lines.append(f"User: {session['username']}")
        status_lines.append(f"Uptime: {int(uptime)} seconds")
        
        # Current directory
        current_dir = ssh_bot.current_dirs.get(user_id, "Unknown")
        status_lines.append(f"Directory: {current_dir}")
    else:
        status_lines.append("❌ Not connected to any SSH server")
    
    # Active command status
    if user_id in ssh_bot.active_commands:
        cmd_info = ssh_bot.active_commands[user_id]
        cmd_uptime = asyncio.get_event_loop().time() - cmd_info['start_time']
        
        status_lines.append("")
        status_lines.append("⚡ Active Command:")
        status_lines.append(f"Command: {cmd_info['command']}")
        status_lines.append(f"Running: {int(cmd_uptime)} seconds")
        status_lines.append("Use /stop to terminate, /input to send data")
    else:
        status_lines.append("")
        status_lines.append("⏹️ No active commands")
    
    await update.message.reply_text("\n".join(status_lines))

@restricted
async def ls_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /ls command - list directory contents"""
    user_id = update.effective_user.id
    
    # Check if connected
    if user_id not in ssh_bot.ssh_clients:
        await update.message.reply_text("❌ Not connected to SSH. Use /connect first.")
        return
    
    # Get current directory
    current_dir = ssh_bot.current_dirs.get(user_id, "~")
    
    # Create message
    executing_msg = await update.message.reply_text(f"📂 Listing directory: {current_dir}")
    
    # Execute ls command
    class MessageUpdater:
        def __init__(self, message):
            self.message = message
        
        async def update(self, cmd, output):
            output_clean = html.escape(output)
            response = f'output "{cmd}":\n<pre>{output_clean}</pre>'
            await self.message.edit_text(response, parse_mode='HTML')
    
    updater = MessageUpdater(executing_msg)
    
    cmd_executed, output = await ssh_bot.execute_command_realtime(
        user_id, 
        "ls -la", 
        updater.update
    )
    
    # Final update
    if output.startswith("❌"):
        await executing_msg.edit_text(output)
    else:
        output_clean = html.escape(output)
        response = f'output "{cmd_executed}":\n<pre>{output_clean}</pre>'
        await executing_msg.edit_text(response, parse_mode='HTML')

def main():
    """Start the bot"""
    # Install paramiko if not installed
    try:
        import paramiko
    except ImportError:
        print("Installing paramiko...")
        import subprocess
        subprocess.run(["pip", "install", "paramiko"])
        import paramiko
    
    # Create application
    application = Application.builder().token(BOT_TOKEN).build()
    
    # Add command handlers
    application.add_handler(CommandHandler("start", start_command))
    application.add_handler(CommandHandler("connect", connect_command))
    application.add_handler(CommandHandler("execute", execute_command))
    application.add_handler(CommandHandler("pwd", pwd_command))
    application.add_handler(CommandHandler("ls", ls_command))
    application.add_handler(CommandHandler("stop", stop_command))
    application.add_handler(CommandHandler("input", input_command))
    application.add_handler(CommandHandler("disconnect", disconnect_command))
    application.add_handler(CommandHandler("status", status_command))
    
    # Start bot
    print("🔐 TheTunnel Bot - SSH Bridge")
    print(f"👤 Allowed User ID: {ALLOWED_USER_ID}")
    print("📝 Commands:")
    print("  /start - Show help")
    print("  /connect <ip:port> <user> <pass> - Connect to SSH")
    print("  /execute <command> - Run command")
    print("  /pwd - Show current directory")
    print("  /ls - List directory contents")
    print("  /stop - Stop current command (Ctrl+C)")
    print("  /input <data> - Send input to command")
    print("  /disconnect - Disconnect")
    print("  /status - Show status")
    print("⚡ Features: Persistent directory, real-time output, input sending")
    print("⚠️  Warning: This bot provides SSH access via Telegram. Use with caution!")
    
    application.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == '__main__':
    main()
