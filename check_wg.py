#!/usr/bin/env python3

# Import modules
# subprocess: for running `wg show`, `rm`, `shutdown`
# re: for parsing handshake time
# time: for timestamp
# os: for file checks
import subprocess
import re
import time
import os

# Constants
# PEERS_DIR: directory for peer files
# LOG_FILE: log file path
# HANDSHAKE_THRESHOLD: handshake threshold (60 minutes = 3600 seconds)
PEERS_DIR = "/home/ubuntu/wireguard/wireguard"
LOG_FILE = "/tmp/wg_check.log"
HANDSHAKE_THRESHOLD = 3600

# Logging function
def log(message):
    try:
        with open(LOG_FILE, "a") as f:
            f.write(f"{time.ctime()}: {message}\n")
    except Exception as e:
        pass  # Silently ignore logging errors

# Timestamp
timestamp = int(time.time())
log(f"Script started, timestamp={timestamp}")

# Check the time of the last run
last_run_file = "/tmp/wg_last_run"
if os.path.exists(last_run_file):
    with open(last_run_file, "r") as f:
        last_run = int(f.read().strip())
    if timestamp - last_run < 60:
        log(f"Too frequent run, skipping (last_run={last_run})")
        exit(0)
with open(last_run_file, "w") as f:
    f.write(str(timestamp))

# Run `wg show`
try:
    wg_output = subprocess.run(
        ["docker", "exec", "wireguard", "wg", "show"],
        capture_output=True,
        text=True
    ).stdout
    log(f"wg show: {wg_output[:100]}...")
except Exception as e:
    log(f"Error running wg show: {e}")
    exit(1)

# Parse `wg show` output
current_peer = None
all_peers_inactive = True  # Flag: all peers are inactive

for line in wg_output.splitlines():
    if line.startswith("peer:"):
        current_peer = line.split("peer: ")[1].strip()
        log(f"Starting peer: {current_peer}")
    elif "latest handshake:" in line and current_peer:
        match = re.search(r"latest handshake:\s*(.+)", line)
        if match:
            handshake_time = match[1].strip()
            if handshake_time == "0 seconds ago":
                seconds = 0
            else:
                # Parse handshake time (e.g., "1 hour, 2 minutes, 34 seconds ago")
                seconds = 0
                if "hour" in handshake_time:
                    hours = int(re.search(r"(\d+)\s*hour", handshake_time).group(1))
                    seconds += hours * 3600
                if "minute" in handshake_time:
                    minutes = int(re.search(r"(\d+)\s*minute", handshake_time).group(1))
                    seconds += minutes * 60
                if "second" in handshake_time:
                    seconds_match = re.search(r"(\d+)\s*second", handshake_time)
                    if seconds_match:
                        seconds += int(seconds_match.group(1))
            
            log(f"Peer {current_peer}: latest handshake {seconds} seconds ago")
            if seconds < HANDSHAKE_THRESHOLD:
                all_peers_inactive = False
                log(f"Peer {current_peer} is active (handshake younger than {HANDSHAKE_THRESHOLD} seconds)")

# If no handshake is found for a peer, consider it inactive
if all_peers_inactive:
    log("All peers are inactive (handshake older than 60 minutes or absent)")
    try:
        # Delete the peers folder
        rm_result = subprocess.run(
            ["rm", "-rf", PEERS_DIR],
            capture_output=True,
            text=True
        )
        log(f"rm result: code={rm_result.returncode}, stderr={rm_result.stderr}")
        
        # Shut down the instance
        shutdown_result = subprocess.run(
            ["sudo", "/sbin/shutdown", "now"],
            capture_output=True,
            text=True
        )
        log(f"shutdown result: code={shutdown_result.returncode}, stderr={shutdown_result.stderr}")
    except Exception as e:
        log(f"Shutdown error: {e}")
else:
    log("Active peers found, instance remains running")