# WireGuard EC2 Bot

This project is an experimental setup for creating a low-cost, ad-hoc VPN using **WireGuard** and **Pi-hole** on an AWS EC2 instance. It leverages AWS Lambda and a Telegram bot for convenient management, allowing you to start/stop the instance, recreate VPN peers, and fetch configuration files with minimal effort and cost.

## Project Overview

The goal of this project is to build a lightweight, cost-effective VPN solution that can be spun up on-demand using AWS services. The setup includes:

- An **AWS EC2 instance** running WireGuard (a modern VPN protocol) and Pi-hole (for ad-blocking).
- A **Telegram bot** powered by AWS Lambda and API Gateway, providing a simple interface to manage the EC2 instance and VPN peers.
- A **Python script** (`check_wg.py`) that monitors WireGuard activity and automatically shuts down the instance if peers are inactive for 1 hour, minimizing costs.

This is an experimental project focused on keeping AWS costs as low as possible while maintaining functionality. By using on-demand EC2 instances and Lambda's free tier, the setup can cost just a few dollars per month (or less if the instance is stopped when not in use).

## Features

- **Telegram Bot Commands**:
  - Start/Stop EC2 instance ("Start EC2", "Stop EC2").
  - Check instance status ("Check Status").
  - Fetch WireGuard peer configuration files ("Get Peer Files").
  - Recreate peers by restarting `docker-compose` ("Recreate Peers").
- **Automatic Shutdown**:
  - The `check_wg.py` script runs on the EC2 instance and shuts it down if no WireGuard peers have been active (no handshake) for 1 hour, ensuring you only pay for the time the VPN is actually used.
- **Cost Optimization**:
  - Uses EC2 with auto-assigned public IPs (no Elastic IP to avoid extra charges).
  - Leverages Lambda's free tier and minimal API Gateway usage.
  - Automatically stops the EC2 instance when idle.

## Requirements

To replicate this setup, you'll need:

- An **AWS account** with access to EC2, Lambda, and API Gateway.
- A **Telegram bot** token (create one via BotFather).
- An EC2 instance running Ubuntu with Docker installed, and containers for WireGuard and Pi-hole (configured via `docker-compose`).
- An SSH key pair for accessing the EC2 instance.
- (Optional) An S3 bucket to store the SSH key (or configure it as an environment variable in Lambda).

## Installation

### 1. Set Up EC2 Instance

1. Launch an EC2 instance (e.g., `t3.micro`, Ubuntu 20.04 or later).

2. Enable "Auto-assign Public IP" in the instance settings to avoid Elastic IP costs.

3. Install Docker and Docker Compose on the instance:

   ```bash
   sudo apt update
   sudo apt install docker.io docker-compose -y
   sudo systemctl enable docker
   sudo systemctl start docker
   sudo usermod -aG docker ubuntu
   ```

4. Set up WireGuard and Pi-hole using `docker-compose` in `/home/ubuntu/wireguard`. Example `docker-compose.yml`:

   ```yaml
   version: '3'
   services:
     wireguard:
       image: linuxserver/wireguard
       container_name: wireguard
       cap_add:
         - NET_ADMIN
         - SYS_MODULE
       environment:
         - PUID=1000
         - PGID=1000
         - TZ=Europe/London
       volumes:
         - ./wireguard:/config
       ports:
         - 51820:51820/udp
       sysctls:
         - net.ipv4.conf.all.src_valid_mark=1
       restart: unless-stopped
   
     pihole:
       image: pihole/pihole:latest
       container_name: pihole
       environment:
         - TZ=Europe/London
         - WEBPASSWORD=yourpassword
       volumes:
         - ./pihole:/etc/pihole
         - ./dnsmasq.d:/etc/dnsmasq.d
       ports:
         - 53:53/tcp
         - 53:53/udp
         - 80:80/tcp
       restart: unless-stopped
   ```

5. Start the containers:

   ```bash
   cd /home/ubuntu/wireguard
   docker-compose up -d
   ```

### 2. Set Up Telegram Bot

The bot code and dependencies are included in the `bot` directory, ready for deployment to AWS Lambda.

#### Download and Configure the Bot

1. Clone this repository or download the `bot` directory:

   ```bash
   git clone https://github.com/ikuranoff/wireguard-ec2-bot.git
   cd wireguard-ec2-bot/bot
   ```

2. Open `lambda_function.py` in a text editor and replace the following placeholders with your own values:

   - `YOUR_TOKEN_HERE`: Your Telegram bot token (get it from BotFather).
   - `YOUR_CHAT_ID_HERE`: Your Telegram chat ID (find it by messaging your bot and checking the logs).
   - `YOUR_S3_BUCKET`: Your S3 bucket name where the SSH key is stored.
   - `YOUR_S3_KEY_PATH`: Path to your SSH key in S3 (e.g., "my-key.pem").
   - `YOUR_EC2_TAG_VALUE`: The tag value of your EC2 instance (e.g., "my-vpn-instance").
   - Adjust `PEERS_DIR`, `DOCKER_COMPOSE_DIR`, `EC2_REGION`, `EC2_TAG_KEY`, and `EC2_TAG_VALUE` to match your setup.

#### Package the Bot for Lambda

The `bot` directory already contains all necessary dependencies (`python-telegram-bot==20.7`, `paramiko`, `boto3`). To deploy it to Lambda:

1. Navigate to the `bot` directory:

   ```bash
   cd ~/wireguard-ec2-bot/bot
   ```

2. Create a ZIP archive:

   ```bash
   zip -r ../lambda_package.zip .
   ```

   This will create `lambda_package.zip` in the parent directory (`~/wireguard-ec2-bot`).

#### Deploy to AWS Lambda

1. In AWS Lambda, create a new function (Python 3.11 runtime).
2. In the "Code" section, select "Upload from" → ".zip file" and upload `lambda_package.zip`.
3. Configure IAM roles for Lambda to access EC2 (`ec2:StartInstances`, `ec2:StopInstances`, `ec2:DescribeInstances`) and S3 (if using S3 for SSH keys).
4. Deploy the function.

#### Set Up API Gateway

1. Create a REST API in API Gateway.

2. Configure a POST endpoint to trigger the Lambda function.

3. Set the webhook for your Telegram bot:

   ```bash
   curl -X POST https://api.telegram.org/bot<YOUR_TOKEN>/setWebhook -d url=<API_GATEWAY_URL>
   ```

### 3. Set Up Peer Activity Monitoring

1. Copy `check_wg.py` to `/home/ubuntu/wireguard` on your EC2 instance.

2. Make it executable:

   ```bash
   chmod +x /home/ubuntu/wireguard/check_wg.py
   ```

3. Set up a cron job to run the script every 5 minutes:

   ```bash
   crontab -e
   ```

   Add the following line:

   ```bash
   */5 * * * * /usr/bin/python3 /home/ubuntu/wireguard/check_wg.py
   ```

### Usage

1. Start your Telegram bot and use the following commands:
   - `Start EC2`: Launches the EC2 instance with an auto-assigned public IP.
   - `Stop EC2`: Stops the instance and removes peers.
   - `Check Status`: Shows instance status, uptime, and peer activity.
   - `Get Peer Files`: Fetches WireGuard peer configuration files.
   - `Recreate Peers`: Removes existing peers and restarts `docker-compose` to generate new ones.
2. The `check_wg.py` script will automatically stop the instance if no peers are active (no handshake) for 1 hour.

### Cost Optimization

This project is designed to minimize AWS costs:

- **EC2**: Uses `t3.micro` (or `t4g.micro` for ARM to save \~$2/month) and auto-assigned public IPs (no Elastic IP charges).
- **Lambda**: Operates within the free tier (1M requests/month).
- **API Gateway**: Minimal usage (\~$0.0035/month for 1000 requests).
- **Automatic Shutdown**: Stops the instance after 1 hour of inactivity, ensuring you only pay for active usage.

### License

This project is licensed under the MIT License. See the LICENSE file for details.

