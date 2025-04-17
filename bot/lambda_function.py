import json
import telegram
import requests
import paramiko
import boto3
import os
import base64
from io import BytesIO
from telegram import Update, ReplyKeyboardMarkup, KeyboardButton
from telegram.ext import Application, CommandHandler, MessageHandler, ContextTypes
from telegram.ext.filters import Text
import asyncio
import traceback

# Version: v0.8
# Changes:
# - Removed S3 dependency for SSH key storage
# - SSH key is now loaded from Lambda environment variable (SSH_KEY in Base64)

# Constants
# Replace the following with your own values
TELEGRAM_TOKEN = "YOUR_TOKEN_HERE"  # Your Telegram bot token
ALLOWED_CHAT_ID = "YOUR_CHAT_ID_HERE"  # Your Telegram chat ID
SSH_USER = "Ubuntu"  # SSH user for EC2 instance (adjust if needed)
PEERS_DIR = "/home/ubuntu/wireguard/wireguard"  # Directory for WireGuard peers (adjust if needed)
DOCKER_COMPOSE_DIR = "/home/ubuntu/wireguard"  # Directory for docker-compose.yml (adjust if needed)
LOG_FILE = "/tmp/bot_log.txt"  # Log file path in Lambda
EC2_REGION = "eu-west-2"  # AWS region for EC2
EC2_TAG_KEY = "Name"  # EC2 tag key to identify the instance
EC2_TAG_VALUE = "your-ec2-tag-value"  # Example EC2 tag value (replace with your instance's tag)

# Initialize boto3 clients for EC2
ec2_client = boto3.client("ec2", region_name=EC2_REGION)
ec2_resource = boto3.resource("ec2", region_name=EC2_REGION)

# Initialize the Telegram bot
application = Application.builder().token(TELEGRAM_TOKEN).build()

# Logging function to /tmp
def log(message):
    try:
        with open(LOG_FILE, "a") as f:
            f.write(f"{message}\n")
    except Exception as e:
        print(f"error logging: {str(e)}")

# Access check for Telegram chat
def check_access(update: Update) -> bool:
    chat_id = update.effective_chat.id if update.message else None
    if chat_id is None:
        log("failed to determine chat_id")
        return False
    log(f"checking chat_id: {chat_id}")
    if chat_id != ALLOWED_CHAT_ID:
        return False
    return True

# Create a persistent keyboard menu with buttons
MAIN_KEYBOARD = ReplyKeyboardMarkup(
    [
        [KeyboardButton("Start EC2"), KeyboardButton("Stop EC2")],
        [KeyboardButton("Check Status")],
        [KeyboardButton("Get Peer Files"), KeyboardButton("Recreate Peers")]
    ],
    resize_keyboard=True,
    one_time_keyboard=False
)

# Command: /start
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    log("called /start command")
    if not check_access(update):
        await update.message.reply_text("Access denied!", reply_markup=MAIN_KEYBOARD)
        return
    await update.message.reply_text(
        "Hello! I am an EC2 bot.\nChoose an action from the menu below:",
        reply_markup=MAIN_KEYBOARD
    )

# Handler for button messages
async def handle_buttons(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    log("received message from button")
    if not check_access(update):
        await update.message.reply_text("Access denied!", reply_markup=MAIN_KEYBOARD)
        return
    
    message_text = update.message.text
    log(f"button pressed: {message_text}")
    
    if message_text == "Start EC2":
        await start_ec2(update, context)
    elif message_text == "Stop EC2":
        await stop_ec2(update, context)
    elif message_text == "Check Status":
        await get_instance_info(update, context)
    elif message_text == "Get Peer Files":
        await get_files(update, context)
    elif message_text == "Recreate Peers":
        await recreate_peers(update, context)
    else:
        log(f"unknown button: {message_text}")
        await update.message.reply_text("Unknown action!", reply_markup=MAIN_KEYBOARD)

# Command: Start EC2 instance using boto3 (without EIP, using auto-assigned public IP)
async def start_ec2(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    log("called start_ec2")
    try:
        # Find the instance by tag
        response = ec2_client.describe_instances(
            Filters=[
                {
                    "Name": f"tag:{EC2_TAG_KEY}",
                    "Values": [EC2_TAG_VALUE]
                },
                {
                    "Name": "instance-state-name",
                    "Values": ["stopped", "stopping"]
                }
            ]
        )
        instances = response["Reservations"]
        if not instances:
            await update.message.reply_text("Instance not found or already running!", reply_markup=MAIN_KEYBOARD)
            return
        
        instance_id = instances[0]["Instances"][0]["InstanceId"]
        log(f"starting instance: {instance_id}")
        ec2_client.start_instances(InstanceIds=[instance_id])
        
        # Wait for the instance to start
        instance = ec2_resource.Instance(instance_id)
        instance.wait_until_running()
        instance.reload()
        log(f"instance {instance_id} started, state: {instance.state['Name']}")
        
        # Get the new public IP (auto-assigned by EC2)
        public_ip = instance.public_ip_address
        if not public_ip:
            await update.message.reply_text("Instance started, but no public IP assigned! Check Auto-assign Public IP settings.", reply_markup=MAIN_KEYBOARD)
            return
        
        log(f"instance public IP: {public_ip}")
        await update.message.reply_text(f"Instance {instance_id} started!\nIP: {public_ip}", reply_markup=MAIN_KEYBOARD)
    except Exception as e:
        log(f"error in start_ec2: {str(e)}")
        await update.message.reply_text(f"Error: {str(e)}", reply_markup=MAIN_KEYBOARD)

# Command: Stop EC2 instance using boto3 (without EIP, with peer deletion and delay)
async def stop_ec2(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    log("called stop_ec2")
    try:
        # Find the instance by tag
        response = ec2_client.describe_instances(
            Filters=[
                {
                    "Name": f"tag:{EC2_TAG_KEY}",
                    "Values": [EC2_TAG_VALUE]
                },
                {
                    "Name": "instance-state-name",
                    "Values": ["running"]
                }
            ]
        )
        instances = response["Reservations"]
        if not instances:
            await update.message.reply_text("Instance not found or already stopped!", reply_markup=MAIN_KEYBOARD)
            return
        
        instance_id = instances[0]["Instances"][0]["InstanceId"]
        instance = ec2_resource.Instance(instance_id)
        ec2_ip = instance.public_ip_address
        
        if ec2_ip:
            # Delete the peers folder before shutdown
            log(f"SSH clear_peers before shutdown, IP: {ec2_ip}")
            # Load SSH key from environment variable (expected to be Base64-encoded)
            ssh_key_b64 = os.getenv("SSH_KEY")
            if not ssh_key_b64:
                log("SSH_KEY environment variable not set")
                await update.message.reply_text("Error: SSH_KEY environment variable not set!", reply_markup=MAIN_KEYBOARD)
                return
            ssh_key = base64.b64decode(ssh_key_b64).decode("utf-8")
            key_file = "/tmp/wireguard-key.pem"
            with open(key_file, "w") as f:
                f.write(ssh_key)
            os.chmod(key_file, 0o400)
            ssh = paramiko.SSHClient()
            ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
            ssh.connect(ec2_ip, username=SSH_USER, key_filename=key_file)
            ssh.exec_command(f"rm -rf {PEERS_DIR}")
            log(f"folder {PEERS_DIR} deleted before shutdown")
            ssh.close()
            os.remove(key_file)
            
            # Add a delay to ensure SSH completes
            await asyncio.sleep(2)

        # Stop the instance
        log(f"stopping instance: {instance_id}")
        ec2_client.stop_instances(InstanceIds=[instance_id])
        await update.message.reply_text(f"Instance {instance_id} is stopping! Peers deleted.", reply_markup=MAIN_KEYBOARD)
    except Exception as e:
        log(f"error in stop_ec2: {str(e)}")
        await update.message.reply_text(f"Error: {str(e)}", reply_markup=MAIN_KEYBOARD)

# Command: Fetch peer configuration files
async def get_files(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    log("called get_files")
    if not check_access(update):
        await update.message.reply_text("Access denied!")
        return
    try:
        # Find the instance by tag
        response = ec2_client.describe_instances(
            Filters=[
                {
                    "Name": f"tag:{EC2_TAG_KEY}",
                    "Values": [EC2_TAG_VALUE]
                },
                {
                    "Name": "instance-state-name",
                    "Values": ["running"]
                }
            ]
        )
        instances = response["Reservations"]
        if not instances:
            await update.message.reply_text("Instance not found or not running!", reply_markup=MAIN_KEYBOARD)
            return
        
        instance = instances[0]["Instances"][0]
        ec2_ip = instance.get("PublicIpAddress", None)
        if not ec2_ip:
            await update.message.reply_text("Instance has no public IP!", reply_markup=MAIN_KEYBOARD)
            return
        
        log(f"SSH get_files, IP: {ec2_ip}")
        # Load SSH key from environment variable (expected to be Base64-encoded)
        ssh_key_b64 = os.getenv("SSH_KEY")
        if not ssh_key_b64:
            log("SSH_KEY environment variable not set")
            await update.message.reply_text("Error: SSH_KEY environment variable not set!", reply_markup=MAIN_KEYBOARD)
            return
        ssh_key = base64.b64decode(ssh_key_b64).decode("utf-8")
        key_file = "/tmp/wireguard-key.pem"
        with open(key_file, "w") as f:
            f.write(ssh_key)
        os.chmod(key_file, 0o400)
        ssh = paramiko.SSHClient()
        ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        ssh.connect(ec2_ip, username=SSH_USER, key_filename=key_file)
        sftp = ssh.open_sftp()
        
        # Check if there are files in PEERS_DIR
        try:
            files_in_dir = sftp.listdir(PEERS_DIR)
            if not files_in_dir:
                await update.message.reply_text("The peer profiles folder is empty!", reply_markup=MAIN_KEYBOARD)
                sftp.close()
                ssh.close()
                os.remove(key_file)
                return
        except FileNotFoundError:
            await update.message.reply_text("The peer profiles folder is empty!", reply_markup=MAIN_KEYBOARD)
            sftp.close()
            ssh.close()
            os.remove(key_file)
            return

        # If the folder is not empty, download the files
        for i in range(1, 8):
            peer_dir = f"{PEERS_DIR}/peer{i}"
            files = [f"peer{i}.png", f"peer{i}.conf"]
            for file_name in files:
                remote_path = f"{peer_dir}/{file_name}"
                log(f"trying to fetch file: {remote_path}")
                try:
                    with sftp.file(remote_path, "rb") as remote_file:
                        file_data = remote_file.read()
                    file_stream = BytesIO(file_data)
                    await update.message.reply_document(
                        document=file_stream,
                        filename=file_name,
                        caption=f"File {file_name} for peer{i}",
                        reply_markup=MAIN_KEYBOARD
                    )
                except FileNotFoundError:
                    log(f"file not found: {remote_path}")
                    continue  # Skip missing files
                except Exception as e:
                    log(f"error fetching file {file_name}: {str(e)}")
                    continue
        sftp.close()
        ssh.close()
        os.remove(key_file)
        await update.message.reply_text("All files sent!", reply_markup=MAIN_KEYBOARD)
    except Exception as e:
        log(f"error in get_files: {str(e)}")
        await update.message.reply_text(f"SSH Error: {str(e)}", reply_markup=MAIN_KEYBOARD)

# Command: Get instance information with peer status
async def get_instance_info(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    log("called get_instance_info")
    if not check_access(update):
        await update.message.reply_text("Access denied!")
        return
    try:
        # Find the instance by tag
        response = ec2_client.describe_instances(
            Filters=[
                {
                    "Name": f"tag:{EC2_TAG_KEY}",
                    "Values": [EC2_TAG_VALUE]
                }
            ]
        )
        instances = response["Reservations"]
        if not instances:
            await update.message.reply_text("Instance not found!", reply_markup=MAIN_KEYBOARD)
            return

        # Get the first matching instance
        instance = instances[0]["Instances"][0]
        instance_id = instance["InstanceId"]
        state = instance["State"]["Name"]
        external_ip = instance.get("PublicIpAddress", "IP not assigned")
        log(f"instance public IP: {external_ip}")

        # Get uptime via SSH if the instance is running
        uptime = "Could not retrieve uptime (instance not running)"
        peers_info = "Peers: absent"
        if state == "running" and external_ip != "IP not assigned":
            # Load SSH key from environment variable (expected to be Base64-encoded)
            ssh_key_b64 = os.getenv("SSH_KEY")
            if not ssh_key_b64:
                log("SSH_KEY environment variable not set")
                await update.message.reply_text("Error: SSH_KEY environment variable not set!", reply_markup=MAIN_KEYBOARD)
                return
            ssh_key = base64.b64decode(ssh_key_b64).decode("utf-8")
            key_file = "/tmp/wireguard-key.pem"
            with open(key_file, "w") as f:
                f.write(ssh_key)
            os.chmod(key_file, 0o400)
            ssh = paramiko.SSHClient()
            ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
            ssh.connect(external_ip, username=SSH_USER, key_filename=key_file)
            
            # Check for peers
            sftp = ssh.open_sftp()
            try:
                files_in_dir = sftp.listdir(PEERS_DIR)
                if files_in_dir:
                    peers_info = "Peers: present"
            except FileNotFoundError:
                peers_info = "Peers: absent"
            sftp.close()

            # Get uptime
            try:
                stdin, stdout, stderr = ssh.exec_command("uptime")
                uptime = stdout.read().decode().strip()
                log(f"uptime: {uptime}")
            except Exception as e:
                log(f"error getting uptime: {str(e)}")
                uptime = f"Uptime retrieval error: {str(e)}"
            
            ssh.close()
            os.remove(key_file)

        context.user_data["external_ip"] = external_ip  # Save IP for subsequent commands

        await update.message.reply_text(
            f"Instance Information:\n"
            f"Instance ID: {instance_id}\n"
            f"State: {state}\n"
            f"Public IP: {external_ip}\n"
            f"{peers_info}\n"
            f"Uptime: {uptime}",
            reply_markup=MAIN_KEYBOARD
        )
    except Exception as e:
        log(f"error in get_instance_info: {str(e)}")
        await update.message.reply_text(f"Error: {str(e)}", reply_markup=MAIN_KEYBOARD)

# Command: Delete and recreate peers
async def recreate_peers(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    log("called recreate_peers")
    if not check_access(update):
        await update.message.reply_text("Access denied!")
        return
    try:
        # Find the instance by tag
        response = ec2_client.describe_instances(
            Filters=[
                {
                    "Name": f"tag:{EC2_TAG_KEY}",
                    "Values": [EC2_TAG_VALUE]
                },
                {
                    "Name": "instance-state-name",
                    "Values": ["running"]
                }
            ]
        )
        instances = response["Reservations"]
        if not instances:
            await update.message.reply_text("Instance not found or not running!", reply_markup=MAIN_KEYBOARD)
            return
        
        instance = instances[0]["Instances"][0]
        ec2_ip = instance.get("PublicIpAddress", None)
        if not ec2_ip:
            await update.message.reply_text("Instance has no public IP!", reply_markup=MAIN_KEYBOARD)
            return

        log(f"SSH recreate_peers, IP: {ec2_ip}")
        # Load SSH key from environment variable (expected to be Base64-encoded)
        ssh_key_b64 = os.getenv("SSH_KEY")
        if not ssh_key_b64:
            log("SSH_KEY environment variable not set")
            await update.message.reply_text("Error: SSH_KEY environment variable not set!", reply_markup=MAIN_KEYBOARD)
            return
        ssh_key = base64.b64decode(ssh_key_b64).decode("utf-8")
        key_file = "/tmp/wireguard-key.pem"
        with open(key_file, "w") as f:
            f.write(ssh_key)
        os.chmod(key_file, 0o400)
        ssh = paramiko.SSHClient()
        ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        ssh.connect(ec2_ip, username=SSH_USER, key_filename=key_file)
        
        # Check for existing peers
        sftp = ssh.open_sftp()
        peers_exist = False
        try:
            files_in_dir = sftp.listdir(PEERS_DIR)
            if files_in_dir:
                peers_exist = True
                log(f"peers found in {PEERS_DIR}, will delete")
                ssh.exec_command(f"rm -rf {PEERS_DIR}")
                log(f"folder {PEERS_DIR} deleted")
            else:
                log(f"no peers in {PEERS_DIR}, skipping deletion")
        except FileNotFoundError:
            log(f"directory {PEERS_DIR} does not exist, skipping deletion")
        sftp.close()

        # Restart docker-compose
        command = f"cd {DOCKER_COMPOSE_DIR} && docker-compose down && docker-compose up -d"
        stdin, stdout, stderr = ssh.exec_command(command)
        exit_status = stdout.channel.recv_exit_status()
        if exit_status != 0:
            error_output = stderr.read().decode().strip()
            log(f"error restarting docker-compose: {error_output}")
            await update.message.reply_text(f"Error restarting docker-compose: {error_output}", reply_markup=MAIN_KEYBOARD)
        else:
            log(f"docker-compose restarted in {DOCKER_COMPOSE_DIR}")
            if peers_exist:
                await update.message.reply_text("Peers recreated! Old profiles deleted, docker-compose restarted.", reply_markup=MAIN_KEYBOARD)
            else:
                await update.message.reply_text("Peers created! docker-compose restarted.", reply_markup=MAIN_KEYBOARD)

        ssh.close()
        os.remove(key_file)
    except Exception as e:
        log(f"error in recreate_peers: {str(e)}")
        await update.message.reply_text(f"SSH Error: {str(e)}", reply_markup=MAIN_KEYBOARD)

# Add command handlers
application.add_handler(CommandHandler("start", start))
application.add_handler(MessageHandler(Text(), handle_buttons))

# Main Lambda handler
def lambda_handler(event, context):
    try:
        log("received request from Telegram")
        log(f"raw request data: {json.dumps(event)}")
        update_data = json.loads(event["body"])
        log(f"update data: {json.dumps(update_data)}")
        update = Update.de_json(update_data, application.bot)
        if update is None:
            log("failed to create Update object")
            return {"statusCode": 200, "body": "OK"}
        log("Update object created")
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            loop.run_until_complete(application.initialize())
            loop.run_until_complete(application.process_update(update))
            log("request processed")
        finally:
            loop.run_until_complete(application.shutdown())
            loop.close()
        return {"statusCode": 200, "body": "OK"}
    except Exception as e:
        error_msg = f"error in Lambda: {str(e)}\n{traceback.format_exc()}"
        log(error_msg)
        raise Exception(error_msg)