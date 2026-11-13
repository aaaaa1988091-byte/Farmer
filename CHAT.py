
import re
import time
import socket
import queue
import threading

from system.lib.java import eval_pyjinn_script as eps
from system.lib.minescript import echo, echo_json


# ============================================================
# 1. 本地 Socket 服務器
# ============================================================

class ChatBridgeServer:
    """負責建立本地 TCP Server，並管理訊息訂閱者(listeners)。"""

    COLOR_CODE_PATTERN = re.compile(r'§[0-9a-fk-or]', re.IGNORECASE)

    def __init__(self):
        self._socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._socket.bind(("127.0.0.1", 0))
        self._socket.listen(1)
        self.port = self._socket.getsockname()[1]

        self._listeners: list[queue.Queue] = []
        self._listeners_lock = threading.Lock()

    # --- 訂閱者管理 -------------------------------------------------

    def subscribe(self) -> queue.Queue:
        """建立一個新的訊息訂閱佇列。"""
        q = queue.Queue()
        with self._listeners_lock:
            self._listeners.append(q)
        return q

    def unsubscribe(self, q: queue.Queue) -> None:
        """移除訂閱佇列。"""
        with self._listeners_lock:
            if q in self._listeners:
                self._listeners.remove(q)

    def _broadcast(self, text: str) -> None:
        with self._listeners_lock:
            for q in self._listeners:
                q.put(text)

    # --- 背景執行緒 ---------------------------------------------------

    def start(self) -> None:
        threading.Thread(target=self._listen_loop, daemon=True).start()

    def _listen_loop(self) -> None:
        try:
            conn, _ = self._socket.accept()
            reader = conn.makefile(encoding="utf-8", mode="r")
            while True:
                line = reader.readline()
                if not line:
                    break
                line = line.strip()
                if not line:
                    continue
                clean_text = self.COLOR_CODE_PATTERN.sub("", line)
                self._broadcast(clean_text)
        except Exception:
            pass


bridge_server = ChatBridgeServer()


# ============================================================
# 2. PyJinn 腳本（注入遊戲端，負責監聽聊天並回傳）
# ============================================================

def build_pyjinn_script(port: int) -> str:
    """組出要注入遊戲的 PyJinn 腳本原始碼。"""
    return r"""
Socket = JavaClass("java.net.Socket")
PrintWriter = JavaClass("java.io.PrintWriter")
OutputStreamWriter = JavaClass("java.io.OutputStreamWriter")
StandardCharsets = JavaClass("java.nio.charset.StandardCharsets")
mc = JavaClass("net.minecraft.client.Minecraft").getInstance()

writer = None
try:
    bridge_socket = Socket("127.0.0.1", """ + str(port) + r""")
    writer = PrintWriter(OutputStreamWriter(bridge_socket.getOutputStream(), StandardCharsets.UTF_8), True)
except Exception as e:
    pass

chat_field = None
msg_field = None

try:
    gui = mc.gui
    if gui and gui.hud:
        hud_class = gui.hud.getClass()
        chat_field = hud_class.getDeclaredField("chat")
        chat_field.setAccessible(True)
        chat_comp = chat_field.get(gui.hud)
        if chat_comp:
            chat_class = chat_comp.getClass()
            msg_field = chat_class.getDeclaredField("allMessages")
            msg_field.setAccessible(True)
except Exception as e:
    pass

# 記錄上一條最新訊息的 Java 物件，用物件位址比對是否有新訊息
last_msg_obj = None
frame_count = 0
dead = False
listener_id = None

def check_chat(_):
    global last_msg_obj, frame_count, dead
    # 連線不存在（或已確認斷線）就什麼都不做，不再每幀做反射
    if dead or not writer:
        return
    frame_count += 1
    if frame_count % 120 == 0:
        # 心跳：送空行（Python 端會忽略）。對端行程已結束時 checkError() 會變 True，
        # 舊的渲染監聽就能自己退役，不會隨 Farm.py 每次重啟越積越多。
        try:
            writer.println("")
            writer.flush()
            if writer.checkError():
                dead = True
        except Exception as e0:
            dead = True
        if dead:
            try:
                bridge_socket.close()
            except Exception as e1:
                pass
            try:
                remove_event_listener(listener_id)
            except Exception as e2:
                pass
            return
    try:
        if chat_field and msg_field and mc.gui and mc.gui.hud:
            chat_comp = chat_field.get(mc.gui.hud)
            if chat_comp:
                msg_list = msg_field.get(chat_comp)
                if msg_list and msg_list.size() > 0:
                    first_msg = msg_list.get(0)

                    if first_msg != last_msg_obj:
                        last_msg_obj = first_msg

                        comp = None
                        try: comp = first_msg.content()
                        except: comp = first_msg

                        raw_str = ""
                        try: raw_str = comp.getString()
                        except: raw_str = str(comp)

                        if raw_str and writer:
                            safe_str = raw_str.replace("\r", "").replace("\n", " ")
                            writer.println(safe_str)
                            writer.flush()
    except Exception as e:
        pass

listener_id = add_event_listener("render", check_chat)
"""


# ============================================================
# 3. Pattern -> Regex 共用工具
# ============================================================

WILDCARD_TOKENS = (r'\{\}', r'\*')  # pattern 中允許的萬用字元寫法


def _compile_pattern(pattern: str, capture: bool) -> re.Pattern:
    """
    將使用者寫的 pattern（含 * 或 {} 萬用字元）轉成 regex。

    capture=False -> chatlist 用，萬用字元轉成非捕獲組（只判斷有沒有符合）
    capture=True  -> chatget  用，萬用字元轉成捕獲組（可以取出對應內容）
    """
    escaped = re.escape(pattern)
    group = r'([\s\S]+?)' if capture else r'[\s\S]+?'

    regex_str = escaped.replace(r'\{\}', group).replace(r'\*', group)

    # pattern 開頭/結尾是 * 時視為模糊比對，不整條錨定
    anchored = pattern.startswith("*") or pattern.endswith("*")
    if not anchored:
        regex_str = f"^{regex_str}$"

    return re.compile(regex_str, re.IGNORECASE)


def _wait_for_match(pattern: str, timeout: float, capture: bool):
    """
    共用的等待邏輯：訂閱聊天訊息，在 timeout 內尋找符合 pattern 的訊息。

    回傳 re.Match 物件，逾時或沒找到則回傳 None。
    """
    regex = _compile_pattern(pattern, capture=capture)
    my_queue = bridge_server.subscribe()

    start_time = time.time()
    try:
        while True:
            remaining = timeout - (time.time() - start_time)
            if remaining <= 0:
                return None
            try:
                msg = my_queue.get(timeout=min(remaining, 0.1))
            except queue.Empty:
                continue

            match = regex.search(msg)
            if match:
                return match
    finally:
        bridge_server.unsubscribe(my_queue)


# ============================================================
# 4. Public API
# ============================================================

def chatlist(pattern: str, timeout: float = 3.0) -> int:
    """
    判斷 timeout 秒內，是否出現符合 pattern 的聊天訊息。
    pattern 中可用 * 或 {} 代表任意內容。

    回傳：符合 -> 1，逾時未符合 -> 0
    """
    match = _wait_for_match(pattern, timeout, capture=False)
    return 1 if match else 0


def chatget(pattern: str, timeout: float = 3.0):
    """
    擷取 timeout 秒內，符合 pattern 的聊天訊息中，
    * 或 {} 對應到的實際文字內容。

    例如：
        chatget("i have * coins!", timeout=3)
        # 訊息 "i have 3B coins!" -> 回傳 "3B"

    回傳規則：
        - 剛好 1 個萬用字元 -> 回傳字串
        - 多個萬用字元       -> 回傳字串 list（依出現順序）
        - 沒有萬用字元       -> 回傳整條訊息字串
        - 逾時未符合         -> 回傳 None
    """
    match = _wait_for_match(pattern, timeout, capture=True)
    if not match:
        return None

    groups = match.groups()
    if len(groups) == 0:
        return match.group(0)
    if len(groups) == 1:
        return groups[0]
    return list(groups)


# ============================================================
# 5. 啟動橋接
# ============================================================

def start_bridge() -> None:
    bridge_server.start()
    eps(build_pyjinn_script(bridge_server.port))


start_bridge()


# ============================================================
# 測試範例（依需要取消註解）
# ============================================================

# if chatlist("Autopet equipped your [Lvl 200] [207✦] Rose Dragon! VIEW RULE", timeout=3) == 1:
#     echo_json({"text": "我想測試個東西", "color": "red", "bold": True})
# else:
#     echo("很顯然第一次失敗了")

# if chatlist("Autopet equipped your * Rose Dragon! VIEW RULE", timeout=3) == 1:
#     echo("恐龍")
# else:
#     echo("太可惜了 第二次失敗")

# coins = chatget("i have * coins!", timeout=3)
# if coins is not None:
#     echo(f"抓到的數量是: {coins}")
# else:
#     echo("沒抓到符合的訊息")