#!/usr/bin/env python3

# импортируем модули
# subprocess: для `wg show`, `rm`, `shutdown`
# re: для парсинга времени handshake
# time: для timestamp
# os: для проверки файлов
import subprocess
import re
import time
import os

# константы
# PEERS_DIR: папка с пирами
# LOG_FILE: лог ошибок
# HANDSHAKE_THRESHOLD: порог для handshake (60 минут = 3600 секунд)
PEERS_DIR = "/home/ubuntu/wireguard/wireguard"
LOG_FILE = "/tmp/wg_check.log"
HANDSHAKE_THRESHOLD = 3600

# логирование
def log(message):
    try:
        with open(LOG_FILE, "a") as f:
            f.write(f"{time.ctime()}: {message}\n")
    except Exception as e:
        pass  # Молча игнорируем ошибки логирования

# timestamp
timestamp = int(time.time())
log(f"Запуск скрипта, timestamp={timestamp}")

# проверяем время последнего запуска
last_run_file = "/tmp/wg_last_run"
if os.path.exists(last_run_file):
    with open(last_run_file, "r") as f:
        last_run = int(f.read().strip())
    if timestamp - last_run < 60:
        log(f"Слишком частый запуск, пропускаем (last_run={last_run})")
        exit(0)
with open(last_run_file, "w") as f:
    f.write(str(timestamp))

# запускаем `wg show`
try:
    wg_output = subprocess.run(
        ["docker", "exec", "wireguard", "wg", "show"],
        capture_output=True,
        text=True
    ).stdout
    log(f"wg show: {wg_output[:100]}...")
except Exception as e:
    log(f"Ошибка wg show: {e}")
    exit(1)

# парсим `wg show`
current_peer = None
all_peers_inactive = True  # флаг: все пиры неактивны

for line in wg_output.splitlines():
    if line.startswith("peer:"):
        current_peer = line.split("peer: ")[1].strip()
        log(f"Начало пира: {current_peer}")
    elif "latest handshake:" in line and current_peer:
        match = re.search(r"latest handshake:\s*(.+)", line)
        if match:
            handshake_time = match[1].strip()
            if handshake_time == "0 seconds ago":
                seconds = 0
            else:
                # парсим время handshake (например, "1 hour, 2 minutes, 34 seconds ago")
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
            
            log(f"Пир {current_peer}: последний handshake {seconds} секунд назад")
            if seconds < HANDSHAKE_THRESHOLD:
                all_peers_inactive = False
                log(f"Пир {current_peer} активен (handshake младше {HANDSHAKE_THRESHOLD} секунд)")

# если handshake не найден для пира, считаем его неактивным
if all_peers_inactive:
    log("Все пиры неактивны (handshake старше 60 минут или отсутствует)")
    try:
        # удаляем папку с пирами
        rm_result = subprocess.run(
            ["rm", "-rf", PEERS_DIR],
            capture_output=True,
            text=True
        )
        log(f"rm result: code={rm_result.returncode}, stderr={rm_result.stderr}")
        
        # выключаем инстанс
        shutdown_result = subprocess.run(
            ["sudo", "/sbin/shutdown", "now"],
            capture_output=True,
            text=True
        )
        log(f"shutdown result: code={shutdown_result.returncode}, stderr={shutdown_result.stderr}")
    except Exception as e:
        log(f"Ошибка выключения: {e}")
else:
    log("Есть активные пиры, инстанс остаётся включённым")