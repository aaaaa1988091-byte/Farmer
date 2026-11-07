import re
import time
import random
import socket
import queue
import json
import threading
from system.lib.java import eval_pyjinn_script as eps
from system.lib.minescript import *

# ==========================================
# 1. 本地 Socket 服務器
# ==========================================
server_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
server_socket.bind(("127.0.0.1", 0))
server_socket.listen(5)
port = server_socket.getsockname()[1]

listeners = []
listeners_lock = threading.Lock()
click_lock = threading.Lock()
request_lock = threading.Lock()
client_conn = None
client_ready = threading.Event()


def _pace_gui_action_locked():
    """Wait a randomized human reaction interval before every GUI action."""
    time.sleep(random.uniform(0.2, 0.35))


def wait_for_gui_action():
    """Wait a randomized human reaction interval before a GUI-opening command."""
    with click_lock:
        _pace_gui_action_locked()

# ==========================================
# 2. PyJinn 背包/容器與螢幕座標計算腳本
# ==========================================
pyjinn_script = r"""
Socket = JavaClass("java.net.Socket")
PrintWriter = JavaClass("java.io.PrintWriter")
OutputStreamWriter = JavaClass("java.io.OutputStreamWriter")
StandardCharsets = JavaClass("java.nio.charset.StandardCharsets")
BufferedReader = JavaClass("java.io.BufferedReader")
InputStreamReader = JavaClass("java.io.InputStreamReader")
mc = JavaClass("net.minecraft.client.Minecraft").getInstance()

writer = None
reader = None
try:
    bridge_socket = Socket("127.0.0.1", """ + str(port) + r""")
    writer = PrintWriter(OutputStreamWriter(bridge_socket.getOutputStream(), StandardCharsets.UTF_8), True)
    reader = BufferedReader(InputStreamReader(bridge_socket.getInputStream(), StandardCharsets.UTF_8))
except Exception as e:
    print("[PyJinn Error] Socket connection failed:", e)

inventory_requested = False

def listen_inventory_requests():
    global inventory_requested
    while reader:
        try:
            line = reader.readLine()
            if not line:
                break
            if line.strip() == "REQ_INV":
                inventory_requested = True
        except Exception:
            break

Thread = JavaClass("java.lang.Thread")
Thread(listen_inventory_requests).start()

def get_current_screen():
    try:
        if mc.screen: return mc.screen
    except: pass
    
    try:
        if mc.currentScreen: return mc.currentScreen
    except: pass

    try:
        f = mc.getClass().getDeclaredField("screen")
        f.setAccessible(True)
        return f.get(mc)
    except: pass

    try:
        f = mc.getClass().getDeclaredField("currentScreen")
        f.setAccessible(True)
        return f.get(mc)
    except: pass

    return None

def get_slot_screen_pos(screen, slot_obj):
    try:
        scale = 1.0
        try: scale = mc.getWindow().getGuiScale()
        except:
            try: scale = mc.window.getGuiScale()
            except: pass

        gui_x = 0
        gui_y = 0

        if screen:
            for field_name in ["leftPos", "x", "xOrigin", "titleX"]:
                try:
                    f = screen.getClass().getField(field_name)
                    f.setAccessible(True)
                    gui_x = f.get(screen)
                    break
                except:
                    try:
                        f = screen.getClass().getDeclaredField(field_name)
                        f.setAccessible(True)
                        gui_x = f.get(screen)
                        break
                    except: pass

            for field_name in ["topPos", "y", "yOrigin", "titleY"]:
                try:
                    f = screen.getClass().getField(field_name)
                    f.setAccessible(True)
                    gui_y = f.get(screen)
                    break
                except:
                    try:
                        f = screen.getClass().getDeclaredField(field_name)
                        f.setAccessible(True)
                        gui_y = f.get(screen)
                        break
                    except: pass

        slot_x = 0
        slot_y = 0
        try: slot_x = slot_obj.x
        except: pass

        try: slot_y = slot_obj.y
        except: pass

        screen_x = int((gui_x + slot_x + 8) * scale)
        screen_y = int((gui_y + slot_y + 8) * scale)
        return screen_x, screen_y
    except Exception as e:
        return 0, 0

def get_full_inventory_json():
    all_slots = []

    try:
        player = mc.player
        screen = get_current_screen()

        if player:
            menu = None
            try: menu = player.containerMenu
            except:
                try: menu = player.currentScreenHandler
                except: pass

            if menu:
                slots = menu.slots

                for i in range(slots.size()):
                    slot_obj = slots.get(i)
                    item_stack = None
                    try: item_stack = slot_obj.getItem()
                    except:
                        try: item_stack = slot_obj.getStack()
                        except: pass

                    name = ""
                    count = 0
                    item_id = ""

                    if item_stack and not item_stack.isEmpty():
                        try: name = item_stack.getHoverName().getString()
                        except:
                            try: name = item_stack.getName().getString()
                            except: name = str(item_stack)

                        try: count = item_stack.getCount()
                        except: count = 1

                        try: item_id = item_stack.getItem().toString()
                        except: pass

                    screen_x, screen_y = get_slot_screen_pos(screen, slot_obj)

                    safe_name = name.replace('"', '\\"').replace('\n', '')
                    safe_id = item_id.replace('"', '\\"')

                    slot_json = (
                        '{"slot":' + str(i) +
                        ',"name":"' + safe_name + '"' +
                        ',"count":' + str(count) +
                        ',"id":"' + safe_id + '"' +
                        ',"x":' + str(screen_x) +
                        ',"y":' + str(screen_y) + '}'
                    )
                    all_slots.append(slot_json)

    except Exception as e:
        print("[PyJinn Error] Inventory check error:", e)

    return "[" + ",".join(all_slots) + "]"

def check_inventory(_):
    global inventory_requested
    if writer and inventory_requested:
        try:
            inventory_requested = False
            json_str = get_full_inventory_json()
            writer.println("INV_DATA:" + json_str)
            writer.flush()
        except Exception as e:
            pass

add_event_listener("render", check_inventory)
"""

# ==========================================
# 3. 背景 Socket 廣播與佇列管理
# ==========================================
def socket_listener():
    global client_conn
    while True:
        conn = None
        try:
            conn, _ = server_socket.accept()
            client_conn = conn
            client_ready.set()
            reader = conn.makefile(encoding="utf-8", mode="r")
            while True:
                line = reader.readline()
                if not line: break
                line_str = line.strip()
                if line_str.startswith("INV_DATA:"):
                    json_payload = line_str[9:]
                    with listeners_lock:
                        for q in listeners:
                            q.put(json_payload)
        except Exception:
            break
        finally:
            client_conn = None
            client_ready.clear()
            if conn:
                try: conn.close()
                except: pass

threading.Thread(target=socket_listener, daemon=True).start()
eps(pyjinn_script)

# ==========================================
# 4. 核心 API 實作 (Find_Item, Get_Slot, click, close_gui)
# ==========================================
def close_gui() -> bool:
    """關閉當前開啟的容器或 GUI 介面"""
    close_script = """
mc = JavaClass("net.minecraft.client.Minecraft").getInstance()
if mc.player:
    mc.player.closeContainer()
"""
    try:
        with click_lock:
            eps(close_script)
        return True
    except Exception as e:
        print(f"[Close GUI Error] 關閉介面失敗: {e}")
        return False

def get_all_slots(timeout: float = 2.0) -> list:
    my_queue = queue.Queue()
    with listeners_lock:
        listeners.append(my_queue)

    start_time = time.time()
    slots_data = []

    try:
        remaining = timeout - (time.time() - start_time)
        if remaining > 0 and client_ready.wait(timeout=remaining):
            with request_lock:
                conn = client_conn
                if conn:
                    conn.sendall(b"REQ_INV\n")

        while time.time() - start_time < timeout:
            remaining = timeout - (time.time() - start_time)
            if remaining <= 0:
                break
            try:
                raw_data = my_queue.get(timeout=min(remaining, 0.1))
                slots_data = json.loads(raw_data)
                break
            except (queue.Empty, json.JSONDecodeError):
                continue
    finally:
        with listeners_lock:
            if my_queue in listeners:
                listeners.remove(my_queue)

    return slots_data

def Find_Item(item_name: str, timeout: float = 2.0):
    slots = get_all_slots(timeout=timeout)
    if not slots:
        return 0

    escaped = re.escape(item_name)
    regex_pattern = escaped.replace(r'\{\}', r'.*?').replace(r'\*', r'.*?')
    regex = re.compile(f"^{regex_pattern}$", re.IGNORECASE)

    for slot in slots:
        if slot["count"] > 0:
            if regex.search(slot["name"]) or regex.search(slot["id"]):
                return {
                    "slot": slot["slot"],
                    "x": slot["x"],
                    "y": slot["y"],
                    "name": slot["name"],
                    "count": slot["count"],
                    "TF": 1
                }
    return 0

def Get_Slot(slot_index: int, timeout: float = 2.0):
    slots = get_all_slots(timeout=timeout)
    for slot in slots:
        if slot["slot"] == slot_index:
            return {
                "slot": slot["slot"],
                "x": slot["x"],
                "y": slot["y"],
                "name": slot["name"],
                "count": slot["count"],
                "id": slot["id"]
            }
    return 0

def click(target, button: int = 0, shift: bool = False) -> bool:
    """
    點擊指定的物品或槽位
    :param target: 物品名稱/ID (str) 或 槽位索引 (int)
    :param button: 0 代表左鍵，1 代表右鍵
    :param shift: True 時使用 GUI 的 QUICK_MOVE（Shift-click），不操作玩家蹲下
    """
    slot_index = None

    if isinstance(target, int):
        slot_index = target
    elif isinstance(target, str):
        item_data = Find_Item(target)
        if item_data and isinstance(item_data, dict):
            slot_index = item_data["slot"]
        else:
            print(f"[Click Error] 未找到物品: {target}")
            return False

    if slot_index is None:
        return False

    click_type_name = "QUICK_MOVE" if shift else "PICKUP"

    exec_script = f"""
mc = JavaClass("net.minecraft.client.Minecraft").getInstance()
player = mc.player
clicked = False
click_error = "no compatible slot-click method"
if player:
    menu = None
    try: menu = player.containerMenu
    except:
        try: menu = player.currentScreenHandler
        except: pass

    if menu:
        container_id = 0
        try: container_id = menu.containerId
        except:
            try: container_id = menu.syncId
            except: pass

        gm = mc.gameMode
        if gm:
            # 只選三個整數、ClickType enum、以及當前 Player 型別的槽位點擊方法。
            for method in gm.getClass().getDeclaredMethods():
                if method.getParameterCount() == 5:
                    try:
                        param_types = method.getParameterTypes()

                        if (param_types[0].getName() not in ["int", "java.lang.Integer"]
                            or param_types[1].getName() not in ["int", "java.lang.Integer"]
                            or param_types[2].getName() not in ["int", "java.lang.Integer"]
                            or not param_types[3].isEnum()
                            or not param_types[4].isInstance(player)):
                            continue

                        click_type_enum_cls = param_types[3]
                        enum_constants = click_type_enum_cls.getEnumConstants()
                        click_type_obj = None
                        if enum_constants:
                            for const_item in enum_constants:
                                if str(const_item) == "{click_type_name}":
                                    click_type_obj = const_item
                                    break
                        if click_type_obj is None:
                            continue

                        obj_cls = player.getClass().getSuperclass()
                        while obj_cls.getSuperclass():
                            obj_cls = obj_cls.getSuperclass()

                        array_cls = JavaClass("java.lang.reflect.Array")
                        args_array = array_cls.newInstance(obj_cls, 5)
                        array_cls.set(args_array, 0, JavaClass("java.lang.Integer")(int(container_id)))
                        array_cls.set(args_array, 1, JavaClass("java.lang.Integer")(int({slot_index})))
                        array_cls.set(args_array, 2, JavaClass("java.lang.Integer")(int({button})))
                        array_cls.set(args_array, 3, click_type_obj)
                        array_cls.set(args_array, 4, player)

                        method.setAccessible(True)
                        method.invoke(gm, args_array)
                        clicked = True
                        break
                    except Exception as inner_ex:
                        click_error = str(inner_ex)
                        break
if not clicked:
    raise Exception("slot click failed: " + click_error)
"""
    try:
        with click_lock:
            eps(exec_script)
            return True
    except Exception as e:
        print(f"[Click Error] 執行點擊失敗: {e}")
        return False
