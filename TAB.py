import re
import time
import socket
import threading
import random
import system.lib.minescript as minescript
from system.lib.java import eval_pyjinn_script as eps

# ==========================================
# 1. 本地 Socket 服務器 (雙向 UTF-8 通訊)
# ==========================================
server_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
server_socket.bind(("127.0.0.1", 0))
server_socket.listen(1)
port = server_socket.getsockname()[1]

client_conn = None
latest_tab_lines = []
lines_lock = threading.Lock()
tab_received_event = threading.Event()
tab_request_lock = threading.Lock()

# ==========================================
# 2. PyJinn 腳本 (Java 端：Socket 雙向 UTF-8 監聽與發送)
# ==========================================
pyjinn_script = r"""
Socket = JavaClass("java.net.Socket")
PrintWriter = JavaClass("java.io.PrintWriter")
OutputStreamWriter = JavaClass("java.io.OutputStreamWriter")
BufferedReader = JavaClass("java.io.BufferedReader")
InputStreamReader = JavaClass("java.io.InputStreamReader")
StandardCharsets = JavaClass("java.nio.charset.StandardCharsets")
mc = JavaClass("net.minecraft.client.Minecraft").getInstance()

writer = None
reader = None

try:
    bridge_socket = Socket("127.0.0.1", """ + str(port) + r""")
    # 建立雙向 UTF-8 串流
    writer = PrintWriter(OutputStreamWriter(bridge_socket.getOutputStream(), StandardCharsets.UTF_8), True)
    reader = BufferedReader(InputStreamReader(bridge_socket.getInputStream(), StandardCharsets.UTF_8))
except Exception as e:
    pass

def send_tablist():
    if not writer: return
    try:
        conn = mc.getConnection()
        if conn:
            players = conn.getOnlinePlayers()
            if players:
                writer.println("===TAB_START===")
                iterator = players.iterator()
                while iterator.hasNext():
                    entry = iterator.next()
                    disp = entry.getTabListDisplayName()
                    if disp:
                        raw_str = ""
                        try: raw_str = disp.getString()
                        except: raw_str = str(disp)
                        
                        if raw_str:
                            safe_str = raw_str.replace("\r", "").replace("\n", " ")
                            writer.println(safe_str)
                            
                writer.println("===TAB_END===")
                writer.flush()
    except Exception as e:
        pass

# 背景執行續：Java 端持續等待 Python 發送 REQ_TAB 請求
def listen_requests():
    while reader:
        try:
            line = reader.readLine()
            if not line: break
            if line.strip() == "REQ_TAB":
                send_tablist()
        except Exception as e:
            break

Thread = JavaClass("java.lang.Thread")
Runnable = JavaClass("java.lang.Runnable")
# 在 Java 端開啟非同步執行續監聽 Socket 請求
Thread(listen_requests).start()
"""

# ==========================================
# 3. 背景 Socket 處理 (Python 端：強制 UTF-8)
# ==========================================
def socket_listener():
    global client_conn, latest_tab_lines
    try:
        conn, _ = server_socket.accept()
        client_conn = conn
        reader = conn.makefile(encoding="utf-8", mode="r")
        
        buffer = []
        is_recording = False
        
        while True:
            line = reader.readline()
            if not line: break
            line_str = line.strip()
            
            if line_str == "===TAB_START===":
                buffer = []
                is_recording = True
            elif line_str == "===TAB_END===":
                is_recording = False
                with lines_lock:
                    latest_tab_lines = list(buffer)
                tab_received_event.set()  # 標記接收完成
            elif is_recording:
                buffer.append(line_str)
    except Exception:
        pass

threading.Thread(target=socket_listener, daemon=True).start()
eps(pyjinn_script)

# ==========================================
# 4. Python 業務邏輯解析
# ==========================================
def get_tablist_info(timeout: float = 1.0, retries: int = 2) -> dict:
    data = {
        "area": None,
        "server": None,
        "speed": None,
        "farming_fortune": None,
        "Nether_Wart_fortune": None,
        "bank": None,
        "copper": None,
        "gems": None,
        "pet_name": None,
        "pet_level": None,
        "pest_cooldown": None,
        "Plots": None,
        "bonus": None,
        "spray": None,
        "health": None,
        "mana": None,
        "defense": None,
    }

    def _clean_text(raw_text):
        if not raw_text: return ""
        return re.sub(r'§[0-9a-fk-orA-FK-OR]', '', raw_text).strip()

    def _extract_num(text):
        clean = _clean_text(text)
        match = re.search(r'([\d,]+(?:\.\d+)?)\s*([kmbKMB])?', clean)
        if not match: return 0
        num_str, unit = match.groups()
        try:
            val = float(num_str.replace(",", ""))
            if unit:
                u = unit.lower()
                if u == 'k': val *= 1_000
                elif u == 'm': val *= 1_000_000
                elif u == 'b': val *= 1_000_000_000
            return int(val)
        except ValueError:
            return 0

    if not client_conn:
        return data

    lines = None
    # Multiple farm workers can ask for TAB data at once. Serialize the shared
    # event/socket exchange, and retry timeouts instead of letting one reader
    # consume another reader's response or treating a missed packet as state.
    with tab_request_lock:
        per_attempt_timeout = max(0.05, timeout / (retries + 1))
        for attempt in range(retries + 1):
            tab_received_event.clear()
            try:
                client_conn.sendall("REQ_TAB\n".encode("utf-8"))
            except Exception:
                return data

            if tab_received_event.wait(timeout=per_attempt_timeout):
                with lines_lock:
                    lines = list(latest_tab_lines)
                break
            if attempt < retries:
                time.sleep(random.uniform(0.08, 0.16))

    if lines is None:
        return data

    # 3. 解析 UTF-8 字串
    for raw_line in lines:
        text = _clean_text(raw_line)
        
        if not text: continue
        try:
            if "Area:" in text:
                data["area"] = text.split("Area:")[1].strip()
            if "Spray:" in text:
                data["spray"] = text.split("Spray:")[1].strip()
            if "Plots:" in text:
                data["Plots"] = text.split("Plots:")[1].strip()
            if "Server:" in text:
                data["server"] = text.split("Server:")[1].strip()
            if "Speed:" in text:
                data["speed"] = _extract_num(text.split("Speed:")[1])
            if "Farming Fortune:" in text:
                data["farming_fortune"] = _extract_num(text.split("Farming Fortune:")[1])
            if "Nether Wart Fortune:" in text:
                data["Nether_Wart_fortune"] = _extract_num(text.split("Nether Wart Fortune:")[1])
            if "Cooldown:" in text:
                data["pest_cooldown"] = text.split("Cooldown:")[1].strip()
            if "Bonus:" in text:
                data["bonus"] = text.split("Bonus:")[1].strip()
            if "Bank:" in text:
                data["bank"] = _extract_num(text.split("Bank:")[1])
            if "Copper:" in text:
                data["copper"] = _extract_num(text.split("Copper:")[1])
            if "Gems:" in text:
                data["gems"] = _extract_num(text.split("Gems:")[1])
            if "[Lvl " in text:
                ls = text.find("[Lvl ")
                le = text.find("]", ls)
                if le != -1:
                    data["pet_level"] = int(text[ls + 5:le])
                    if "[" in text[le + 1:].strip():
                        ls2 = text[le + 1:].strip().find("[")
                        le2 = text[le + 1:].strip().find("]", ls2)
                        data["pet_name"] = text[le + 1:].strip()[le2 + 1:].strip()
                    else:data["pet_name"] = text[le + 1:].strip()

            if "Health:" in text:
                data["health"] = _extract_num(text.split("Health:")[1])
            if "Mana:" in text:
                data["mana"] = _extract_num(text.split("Mana:")[1])
            if "Defense:" in text:
                data["defense"] = _extract_num(text.split("Defense:")[1])
        except Exception:
            continue
        #minescript.echo(text)
    return data


def get_tablist_info_confirmed(fields, timeout: float = 0.6, confirmations: int = 2, attempts: int = 3) -> dict:
    """Re-read requested TAB fields; return a value only after repeated matches."""
    if isinstance(fields, str):
        fields = (fields,)
    fields = tuple(fields)
    latest = get_tablist_info(timeout=timeout)
    previous = {field: None for field in fields}
    counts = {field: 0 for field in fields}
    confirmed = {}

    for attempt in range(attempts):
        if attempt:
            time.sleep(random.uniform(0.12, 0.22))
            latest = get_tablist_info(timeout=timeout)
        for field in fields:
            value = latest.get(field)
            if value is None:
                previous[field] = None
                counts[field] = 0
            elif value == previous[field]:
                counts[field] += 1
                if counts[field] >= confirmations:
                    confirmed[field] = value
            else:
                previous[field] = value
                counts[field] = 1
        if len(confirmed) == len(fields):
            break

    for field in fields:
        latest[field] = confirmed.get(field)
    return latest

#get_tablist_info().get("area") or ""
