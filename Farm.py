from system.lib.minescript import *
from system.lib.java import eval_pyjinn_script as _eval_pyjinn_script
from pest_main import PestCleaner
from pest_utils import look_at_block, look_at_the_block, _release_all, get_main_hand_item, check_front_crops, check_sides, player_tracker, get_current_plot, get_scoreboard_lines
from TAB import get_tablist_info_confirmed
from BAG import close_gui as _bag_close_gui, Find_Item, get_all_slots, click as _bag_click
from CHAT import chatget, bridge_server as _chat_bridge
import time, threading, random, re, socket, json, subprocess, sys, hmac
import builtins
import collections
import gc
import os
import queue

# ─────────────────────────────────────────────
# 測試模式（單人存檔）與功能開關：可由 Discord 面板即時切換，存在 Farm_watchdog_state.json
# ─────────────────────────────────────────────
FEATURE_LABELS = {
    "pest": "除蟲",
    "equipment": "換裝(/loadout)",
    "sell": "賣出/合成(/bz)",
    "spray": "噴霧",
    "bonus": "Bonus 續費(/call)",
}
# 測試模式下會被自動關閉的 Hypixel 專屬功能
HYPIXEL_ONLY_FEATURES = ("equipment", "sell", "spray", "bonus")
# 單人存檔不存在的指令：測試模式下直接攔截，不送出（避免 Unknown command 洗版）
HYPIXEL_ONLY_COMMANDS = {
    "/bz", "/viewrecipe", "/recipe", "/boostercookie", "/loadout", "/call",
    "/warp", "/setspawn", "/plottp", "/tptoplot", "/l", "/skyblock", "/pets",
}
_test_mode_flag = False
_features = {}
_manual_pest_plot = None
_real_execute = execute


def _feature_on(name):
    return bool(_features.get(name, True))


def execute(command, *args, **kwargs):
    """包住 Minescript execute：測試模式下攔截 Hypixel 專屬指令。"""
    try:
        head = str(command).strip().split()[0].lower()
    except IndexError:
        head = ""
    if _test_mode_flag and head in HYPIXEL_ONLY_COMMANDS:
        echo(f"§7[測試模式] 已略過 Hypixel 專屬指令 {command!r}")
        return None
    return _real_execute(command, *args, **kwargs)


def click(target, button: int = 0, shift: bool = False) -> bool:
    """Apply Farm.py's human-paced wait even when BAG is already cached by Minescript."""
    if anti_fail_hold.is_set():
        return False
    time.sleep(random.uniform(0.2, 0.35))
    if anti_fail_hold.is_set():
        return False
    return _bag_click(target, button=button, shift=shift)


def close_gui(resume_attack: bool = True) -> bool:
    """Close a GUI, refresh held keys, then optionally resume hoe farming."""
    time.sleep(random.uniform(0.2, 0.35))
    closed = _bag_close_gui()
    flush()
    # Let Minecraft process the screen transition before sending key-up; doing
    # both in the same tick can leave attack logically held across the GUI.
    time.sleep(random.uniform(0.1, 0.2))

    # A GUI can leave Minecraft's held-key state stale. Release the old state
    # after closing, then send a fresh attack press only for active hoe farming.
    with _attack_input_lock:
        _farm_attack_monitor_active.clear()
        _release_all()
        flush()

    if (
        closed
        and resume_attack
        and not _stop_event.is_set()
        and 開關
        and 農業中
        and not 害蟲處理中
        and not anti_fail_hold.is_set()
    ):
        time.sleep(random.uniform(0.1, 0.2))
        hand = get_main_hand_item(timeout=1.0)
        if isinstance(hand, dict) and hand.get("slot") == HOE and 農田朝向正確():
            with _attack_input_lock:
                if 開關 and 農業中 and not 害蟲處理中 and not anti_fail_hold.is_set():
                    左鍵(True)
                    flush()
                    _farm_attack_monitor_active.set()
            echo("✅ GUI 關閉後已重新送出鋤頭攻擊")
        else:
            echo("§eGUI 已關閉，但主手尚未確認為 HOE，暫不恢復攻擊")
    return closed

# 同一個 Minescript Python runtime 內重跑此檔時，共用同一組生命週期狀態。
_LIFECYCLE_KEY = "_farmbot_farm_lifecycle"
_previous_lifecycle = getattr(builtins, _LIFECYCLE_KEY, None)
_startup_f8 = bool(_previous_lifecycle.get("restart_f8", False)) if _previous_lifecycle else False
_startup_direction = bool(_previous_lifecycle.get("direction", True)) if _previous_lifecycle else True
_startup_run_started_at = _previous_lifecycle.get("run_started_at") if _previous_lifecycle else None
_startup_run_deadline_at = _previous_lifecycle.get("run_deadline_at") if _previous_lifecycle else None
_startup_auto_restart_at = _previous_lifecycle.get("auto_restart_at") if _previous_lifecycle else None
if _previous_lifecycle is not None:
    _previous_lifecycle["stop"].set()
    _previous_lifecycle["restart_f8"] = False

_lifecycle = {
    "stop": threading.Event(),
    "threads": [],
    "loadout": _previous_lifecycle.get("loadout") if _previous_lifecycle else None,
    "direction": _startup_direction,
    "restart_f8": False,
    "run_started_at": _startup_run_started_at if _startup_f8 else None,
    "run_deadline_at": _startup_run_deadline_at if _startup_f8 else None,
    "auto_restart_at": _startup_auto_restart_at if _startup_f8 else None,
}
setattr(builtins, _LIFECYCLE_KEY, _lifecycle)
_stop_event = _lifecycle["stop"]

_tracker_thread = getattr(player_tracker, "_thread", None)
if not (getattr(player_tracker, "_running", False) and _tracker_thread and _tracker_thread.is_alive()):
    player_tracker.start()
w, a, s, d = player_press_forward, player_press_left, player_press_backward, player_press_right

_MOUSE_CAPTURE_RESTORE_SCRIPT = r"""
mc = JavaClass("net.minecraft.client.Minecraft").getInstance()

def get_current_screen():
    try:
        if mc.screen:
            return mc.screen
    except:
        pass
    try:
        if mc.currentScreen:
            return mc.currentScreen
    except:
        pass
    for field_name in ["screen", "currentScreen"]:
        try:
            field = mc.getClass().getDeclaredField(field_name)
            field.setAccessible(True)
            screen = field.get(mc)
            if screen:
                return screen
        except:
            pass
    return None

if mc.player and get_current_screen() is None:
    Minescript = JavaClass("net.minescript.common.Minescript")
    mappings = Minescript.mappingsLoader.get()
    handler = mc.mouseHandler
    clazz = handler.getClass()
    runtime_name = mappings.getRuntimeFieldName(clazz, "mouseGrabbed")
    field = clazz.getDeclaredField(runtime_name)
    field.setAccessible(True)
    field.setBoolean(handler, True)
"""
_mouse_capture_restore_warned = False

def _restore_mouse_capture_before_press() -> None:
    global _mouse_capture_restore_warned
    try:
        _eval_pyjinn_script(_MOUSE_CAPTURE_RESTORE_SCRIPT)
    except Exception as exc:
        if not _mouse_capture_restore_warned:
            echo(f"滑鼠捕捉恢復失敗: {exc}")
            _mouse_capture_restore_warned = True

def 左鍵(pressed: bool):
    if pressed:
        _restore_mouse_capture_before_press()
    return player_press_attack(pressed)

def 右鍵(pressed: bool):
    if pressed:
        _restore_mouse_capture_before_press()
    return player_press_use(pressed)

蹲下 = player_press_sneak

AOTV       = 0
吸塵器      = 1
HOE        = 2
視角糾正    = 4
噴霧        = 5


# ===== ITEM_BRIDGE_BEGIN =====
# ─────────────────────────────────────────────
# 目標 yaw / pitch：直接讀「視角糾正」欄位物品（Squeaky Mousemat）lore 的 Selected Yaw / Pitch。
# 這個值就是 100% 正確的目標，取代 Discord 設定值。
# 做法同 CHAT.py：Python 開本機 Socket Server，遊戲內注入 Pyjinn，
# 用 java.net.Socket + PrintWriter(UTF-8) 把「這一格」的內容推回來（只讀這一格，不讀其他物品）。
# ─────────────────────────────────────────────
class _ItemBridge:
    LORE_SEP = "|~|"
    _NUM = r"-?\d+(?:\.\d+)?"

    def __init__(self):
        self._socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._socket.bind(("127.0.0.1", 0))
        self._socket.listen(1)
        self.port = self._socket.getsockname()[1]
        self._cond = threading.Condition()
        self._conn = None
        self._closed = False
        self._view = None      # (yaw, pitch) 或 None（物品不在 / 讀不到）
        self._version = 0      # 每收到一份完整快照 +1
        self.errors = collections.deque(maxlen=20)  # 只保留最近 20 筆，避免無限增長

    def start(self):
        threading.Thread(target=self._listen_loop, daemon=True).start()

    def close(self):
        self._closed = True
        for sock in (self._conn, self._socket):
            try:
                if sock is not None:
                    sock.close()
            except Exception:
                pass

    def _listen_loop(self):
        while not self._closed:
            try:
                conn, _ = self._socket.accept()
            except Exception:
                return
            self._conn = conn
            try:
                reader = conn.makefile(encoding="utf-8", errors="replace", mode="r")
                batch = None
                while not self._closed:
                    line = reader.readline()
                    if not line:
                        break
                    line = line.rstrip("\r\n")
                    if line == "BEGIN":
                        batch = []
                    elif line == "END":
                        if batch is not None:
                            self._publish(batch)
                        batch = None
                    elif line.startswith("ERR"):
                        self.errors.append(line)
                    elif batch is not None and line.startswith("I\t"):
                        parts = line.split("\t", 6)
                        if len(parts) >= 7:
                            batch.append(parts[6].split(self.LORE_SEP) if parts[6] else [])
            except Exception:
                pass
            finally:
                try:
                    conn.close()
                except Exception:
                    pass

    def _parse_view(self, lore_lines):
        blob = re.sub(r"§.", "", "\n".join(lore_lines))
        y = re.search(r"Selected\s+Yaw\s*:\s*(" + self._NUM + ")", blob)
        p = re.search(r"Selected\s+Pitch\s*:\s*(" + self._NUM + ")", blob)
        if y and p:
            return float(y.group(1)), float(p.group(1))
        return None

    def _publish(self, batch):
        view = self._parse_view(batch[0]) if batch else None
        with self._cond:
            self._view = view
            self._version += 1
            self._cond.notify_all()

    def latest(self, timeout=0.0):
        """回傳 (view, version)；timeout 內尚未收到任何快照則 version=0。"""
        deadline = time.time() + timeout
        with self._cond:
            while self._version == 0:
                remaining = deadline - time.time()
                if remaining <= 0:
                    return None, 0
                self._cond.wait(remaining)
            return self._view, self._version


def _build_item_pyjinn_script(port: int, slot: int) -> str:
    return r"""
Socket = JavaClass("java.net.Socket")
PrintWriter = JavaClass("java.io.PrintWriter")
OutputStreamWriter = JavaClass("java.io.OutputStreamWriter")
StandardCharsets = JavaClass("java.nio.charset.StandardCharsets")
DataComponents = JavaClass("net.minecraft.core.component.DataComponents")
Registry = JavaClass("net.minecraft.core.registries.BuiltInRegistries")
mc = JavaClass("net.minecraft.client.Minecraft").getInstance()
System = JavaClass("java.lang.System")
my_gen = str(""" + str(port) + r""")
System.setProperty("farmbot.item_bridge_gen", my_gen)

writer = None
try:
    bridge_socket = Socket("127.0.0.1", """ + str(port) + r""")
    writer = PrintWriter(OutputStreamWriter(bridge_socket.getOutputStream(), StandardCharsets.UTF_8), True)
except Exception as e:
    pass

frame_count = 0
last_snapshot = ""
dead = False
err_sent = False

def clean(x):
    try:
        return str(x).replace("\r", " ").replace("\n", " ").replace("\t", " ").replace("|~|", " ")
    except Exception as e:
        return "?"

def build_lines():
    lines = []
    inv = mc.player.getInventory()
    for i in [__SLOT__]:
        st = inv.getItem(i)
        if st.isEmpty():
            continue

        item_id = "?"
        try:
            item_id = clean(Registry.ITEM.getKey(st.getItem()).toString())
        except Exception as e:
            try:
                item_id = clean(st.getItem().toString())
            except Exception as e2:
                pass

        name = "?"
        try:
            name = clean(st.getHoverName().getString())
        except Exception as e:
            pass

        lore_parts = []
        try:
            lore = st.get(DataComponents.LORE)
            if lore:
                ll = lore.lines()
                for j in range(ll.size()):
                    lore_parts.append(clean(ll.get(j).getString()))
        except Exception as e:
            lore_parts.append("LOREERR " + clean(e))

        custom = ""
        try:
            cd = st.get(DataComponents.CUSTOM_DATA)
            if cd:
                custom = clean(cd.copyTag().toString())
        except Exception as e:
            custom = "CUSTOMERR " + clean(e)

        lines.append("I\t" + str(i) + "\t" + str(st.getCount()) + "\t" + item_id + "\t" + name + "\t" + custom + "\t" + "|~|".join(lore_parts))
    return lines

def check_items(_):
    global frame_count, last_snapshot, dead, err_sent
    frame_count = frame_count + 1
    if dead or not writer:
        return
    if frame_count % 20 != 0:
        return
    try:
        if str(System.getProperty("farmbot.item_bridge_gen")) != my_gen:
            # 已有較新的 Farm.py 注入了新的橋：本監聽退役，釋放連線並嘗試移除自己
            dead = True
            try:
                writer.close()
            except Exception as e0:
                pass
            try:
                remove_event_listener(listener_id)
            except Exception as e1:
                pass
            return
        if not mc.player:
            return
        lines = build_lines()
        text = "\n".join(lines)
        if text != last_snapshot:
            last_snapshot = text
            writer.println("BEGIN")
            for l in lines:
                writer.println(l)
            writer.println("END")
            writer.flush()
            if writer.checkError():
                dead = True
    except Exception as e:
        if not err_sent:
            err_sent = True
            try:
                writer.println("ERR " + clean(e))
                writer.flush()
            except Exception as e2:
                pass

listener_id = None
listener_id = add_event_listener("render", check_items)
""".replace("__SLOT__", str(int(slot)))


if _previous_lifecycle is not None:
    _old_item_bridge = _previous_lifecycle.get("item_bridge")
    if _old_item_bridge is not None:
        _old_item_bridge.close()
_item_bridge = _ItemBridge()
_lifecycle["item_bridge"] = _item_bridge
_item_bridge.start()
try:
    _eval_pyjinn_script(_build_item_pyjinn_script(_item_bridge.port, 視角糾正))
except Exception as _exc:
    echo(f"§e[視角] 糾正物品讀取橋接啟動失敗：{type(_exc).__name__}: {_exc}")

_item_view_live = False   # 目前的目標值是否來自糾正物品（False＝沿用上次存檔值）


def _sync_target_from_item(timeout=0.0):
    """用糾正物品的 Selected Yaw / Pitch 更新目標值；讀不到回傳 False（沿用現值）。"""
    global _target_yaw, _target_pitch, _item_view_live
    view, version = _item_bridge.latest(timeout)
    if version == 0 or view is None:
        return False
    yaw, pitch = view
    _item_view_live = True
    if yaw != _target_yaw or pitch != _target_pitch:
        _target_yaw, _target_pitch = yaw, pitch
        _write_watchdog_state(target_yaw=yaw, target_pitch=pitch)
        echo(f"§a[視角] 目標值取自糾正物品：yaw={yaw:g}、pitch={pitch:g}")
    return True
# ===== ITEM_BRIDGE_END =====

BOOSTERCOOKIE_SELL_ITEMS = (
    "Bookworm's Favorite Book",
    "Tool Exp Capsule",
    "Overclocker 3000",
    "Flowering Bouquet",
    "Not Just a Pest Vinyl",
    "Cricket Choir Vinyl",
    "Imagine Dragonflies Vinyl",
    "Earthworm Ensemble Vinyl",
    "Firefly in the Hole Vinyl",
    "Pretty Fly Vinyl",
    "Cicada Symphony Vinyl",
    "DynaMITES Vinyl",
    "Buzzin' Beats Vinyl",
    "Wings of Harmony Vinyl",
    "Pray For Me Vinyl",
    "Rodent Revolution Vinyl",
    "Slow and Groovy Vinyl",
)
BOOSTERCOOKIE_SELL_THRESHOLD = 0
BOOSTERCOOKIE_PLAYER_SLOT_START = 54
NETHER_WART_COMPACT_THRESHOLD = 64
CROP_COMPACT_INGREDIENT_COUNT = 160  # 每個壓縮配方都要 160 個前一階原料；不足就不開配方介面
# 每種 Garden 作物：recipes 由最高階排到最低階（與原本 Mutant → Enchanted 的順序相同），
# 每項為 (/viewrecipe 內部 ID, 該配方的原料物品名稱)。原料名稱需與背包物品名稱完全一致。
CROP_COMPACT_CONFIG = (
    {"name": "Nether Wart", "recipes": (
        ("MUTANT_NETHER_STALK", "Enchanted Nether Wart"),
        ("ENCHANTED_NETHER_STALK", "Nether Wart"))},
    {"name": "Wheat", "recipes": (
        ("ENCHANTED_HAY_BALE", "Enchanted Wheat"),
        ("ENCHANTED_WHEAT", "Wheat"))},
    {"name": "Seeds", "recipes": (
        ("BOX_OF_SEEDS", "Enchanted Seeds"),
        ("ENCHANTED_SEEDS", "Seeds"))},
    {"name": "Carrot", "recipes": (
        ("ENCHANTED_GOLDEN_CARROT", "Enchanted Carrot"),
        ("ENCHANTED_CARROT", "Carrot"))},
    {"name": "Potato", "recipes": (
        ("ENCHANTED_BAKED_POTATO", "Enchanted Potato"),
        ("ENCHANTED_POTATO", "Potato"))},
    {"name": "Pumpkin", "recipes": (
        ("POLISHED_PUMPKIN", "Enchanted Pumpkin"),
        ("ENCHANTED_PUMPKIN", "Pumpkin"))},
    {"name": "Melon", "recipes": (
        ("ENCHANTED_MELON_BLOCK", "Enchanted Melon Slice"),
        ("ENCHANTED_MELON", "Melon Slice"))},
    {"name": "Red Mushroom", "recipes": (
        ("ENCHANTED_HUGE_MUSHROOM_2", "Enchanted Red Mushroom"),
        ("ENCHANTED_RED_MUSHROOM", "Red Mushroom"))},
    {"name": "Brown Mushroom", "recipes": (
        ("ENCHANTED_HUGE_MUSHROOM_1", "Enchanted Brown Mushroom"),
        ("ENCHANTED_BROWN_MUSHROOM", "Brown Mushroom"))},
    {"name": "Cocoa Beans", "recipes": (
        ("ENCHANTED_COCOA", "Cocoa Beans"),)},
    {"name": "Cactus", "recipes": (
        ("ENCHANTED_CACTUS", "Enchanted Cactus Green"),
        ("ENCHANTED_CACTUS_GREEN", "Cactus"))},
    {"name": "Sugar Cane", "recipes": (
        ("ENCHANTED_SUGAR_CANE", "Enchanted Sugar"),
        ("ENCHANTED_SUGAR", "Sugar Cane"))},
)


# ===== Hypixel 伺服器排程重啟偵測 =====
# 離開 Garden 絕大多數是因為伺服器重啟：先有 "This server will restart soon" 預警（約 60 秒），
# 重啟時伺服器會顯示 "Evacuating to Hub..." 把玩家送去 Hub。直接聽聊天室，不必等 Area 讀數。
# 時間用牆上時間記錄，Farm.py 自重啟後也能接續。
REBOOT_WARN_WINDOW_SECONDS = 150.0       # 預警後多久內視為「即將重啟」
EVAC_WINDOW_SECONDS = 90.0               # 撤離訊息後多久內視為「已被撤離、尚未回到 Garden」
EVAC_GARDEN_LEAVE_CONFIRM_SECONDS = 1.0  # 撤離中：確認離開 Garden 只需 1 秒就 /warp garden（平常 3 秒）
_reboot_state = {"warned_at": 0.0, "evac_at": 0.0}


def _reboot_imminent():
    """已收到重啟預警、且尚未被撤離。除蟲這類長流程此時不要開始。"""
    warned = _reboot_state["warned_at"]
    return warned > 0 and time.time() - warned < REBOOT_WARN_WINDOW_SECONDS and _reboot_state["evac_at"] < warned


def _evacuating():
    """剛被伺服器撤離、尚未確認回到新的 Garden。流程應立刻放棄並讓出 action_lock。"""
    evac = _reboot_state["evac_at"]
    return evac > 0 and time.time() - evac < EVAC_WINDOW_SECONDS


def _note_chat_for_reboot(message):
    text = re.sub(r"§.", "", str(message or "")).lower()
    now = time.time()
    if "this server will restart soon" in text:
        if not _reboot_imminent():
            echo("§e[重啟預警] 伺服器即將排程重開，暫停開始新的除蟲流程")
        _reboot_state["warned_at"] = now
    elif "evacuating to hub" in text:
        _reboot_state["evac_at"] = now
        echo("§e[重啟] 伺服器正在撤離玩家到 Hub，放棄進行中的回位／校正，改走返回 Garden 流程")
    else:
        return
    try:
        _write_watchdog_state(reboot_warned_at=_reboot_state["warned_at"], reboot_evac_at=_reboot_state["evac_at"])
    except Exception:
        pass


def 重啟偵測():
    """常駐訂閱 CHAT 橋（UTF-8 安全，不碰 Minescript 原生文字讀取），
    看到伺服器重啟預警／撤離訊息就設定旗標。chatget 每次只等一小段時間，呼叫之間的訊息會漏，
    所以這裡直接訂閱橋的佇列持續讀。"""
    echo("伺服器重啟偵測 Thread 啟動")
    q = _chat_bridge.subscribe()
    try:
        while not _stop_event.is_set():
            try:
                message = q.get(timeout=0.5)
            except queue.Empty:
                continue
            _note_chat_for_reboot(message)
    finally:
        _chat_bridge.unsubscribe(q)


def _clear_reboot_state_if_back_in_garden():
    """撤離之後又確認回到 Garden：清掉旗標（撤離訊息後 8 秒內不清，避免舊伺服器 Area 還沒變時誤清）。"""
    evac = _reboot_state["evac_at"]
    if evac > 0 and time.time() - evac > 8.0:
        _reboot_state["warned_at"] = 0.0
        _reboot_state["evac_at"] = 0.0
        try:
            _write_watchdog_state(reboot_warned_at=0.0, reboot_evac_at=0.0)
        except Exception:
            pass


CROP_RECIPE_FAIL_COOLDOWN_SECONDS = 600.0
_crop_recipe_fail_until = {}  # recipe_id -> time.monotonic() 之前不再嘗試（開不出配方介面時設定）


def _save_crop_recipe_cooldowns():
    """把仍在冷卻中的配方存進狀態檔（以牆上時間記錄），農田自重啟後可以接續。"""
    now_mono = time.monotonic()
    now_wall = time.time()
    try:
        _write_watchdog_state(crop_recipe_cooldowns={
            recipe: now_wall + (until - now_mono)
            for recipe, until in _crop_recipe_fail_until.items()
            if until > now_mono
        })
    except Exception as exc:
        echo(f"§e[合成冷卻] 無法寫入狀態檔：{type(exc).__name__}: {exc}")


def _crop_recipe_cooling_down(recipe):
    return time.monotonic() < _crop_recipe_fail_until.get(recipe, 0.0)


def _count_inventory_item(slots, item_name):
    """玩家背包中名稱完全等於 item_name 的物品總數（不含 GUI 上半部槽位）。"""
    wanted = str(item_name).strip().casefold()
    return sum(
        slot.get("count", 0)
        for slot in slots
        if slot.get("count", 0) > 0
        and str(slot.get("name", "")).strip().casefold() == wanted
    )


def _craftable_crop_score(config, slots):
    """此作物所有「原料足夠」配方的原料總數；0 代表目前沒有任何可合成的配方。"""
    score = 0
    for recipe, ingredient in config["recipes"]:
        if _crop_recipe_cooling_down(recipe):
            continue
        count = _count_inventory_item(slots, ingredient)
        if count >= CROP_COMPACT_INGREDIENT_COUNT:
            score += count
    return score


BAZAAR_SELL_THRESHOLDS = {"Enchanted Book": 3}
PEST_COOLDOWN_CORRECTION = 165
PEST_COOLDOWN_TAB_REFRESH_WAIT = 4.2  # TAB 冷卻欄位約每 3–4 秒更新，切套裝後等新快照
PEST_COOLDOWN_POLL_SECONDS = 4.0
FARM_DURATION_HOURS = 2.0  # 開始農田後運行幾小時；到時執行 /l 並結束本腳本
AUTO_RESTART_MIN_SECONDS = 3 * 60
AUTO_RESTART_MAX_SECONDS = 5 * 60
GARDEN_LEAVE_CONFIRM_SECONDS = 3.0
AREA_NONE_CONFIRM_SECONDS = 5.0  # None 可能是讀取瞬斷；持續超過此時間才視為離開 SkyBlock
ANTI_FAIL_YAW_TOLERANCE = 50.0
ANTI_FAIL_TURN_CONFIRM_SECONDS = 0.25
ANTI_FAIL_PITCH_TOLERANCE = 12.0   # 挖田中允許的 pitch 偏差；超過就視為低頭/抬頭異常
CORRECT_PITCH_TOLERANCE = 2.0      # 使用視角糾正物品後的嚴格驗證
ANTI_FAIL_CORRECT_MAX_FAILS = 3    # 連續校正失敗幾次後直接鎖定，不再硬挖

try:
    _farm_log_directory = os.path.dirname(os.path.abspath(__file__))
except NameError:
    _farm_log_directory = os.getcwd()
FARM_RUNTIME_LOG = os.path.join(_farm_log_directory, "Farm_runtime.log")
FARM_ACTIVITY_LOG = os.path.join(_farm_log_directory, "Farm_activity.log")
FARM_WATCHDOG_STATE = os.path.join(_farm_log_directory, "Farm_watchdog_state.json")
FARM_WATCHDOG_STOP = os.path.join(_farm_log_directory, "Farm_watchdog.stop")
FARM_ADMIN_LOCK = os.path.join(_farm_log_directory, "Farm_admin_lock.json")


_SCRIPT_FOLDER_FALLBACK = "FarmBot"  # 只有在完全推算不出本檔位置時才使用


def _minescript_script_command(script_name):
    """組出 Minescript 啟動指令（如 \\FarmBot/Farm）。

    資料夾名稱由本檔實際位置相對於 Minescript 目錄推算，不寫死特定電腦的路徑。
    """
    folder = None
    try:
        here = os.path.dirname(os.path.abspath(__file__))
        try:
            import system.lib.minescript as _ms_lib
            # <minescript>/system/lib/minescript.py → 往上三層就是 Minescript 目錄
            base = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(_ms_lib.__file__))))
            rel = os.path.relpath(here, base)
            if not rel.startswith(".."):
                folder = "" if rel == "." else rel.replace(os.sep, "/")
        except Exception:
            pass
        if folder is None:
            folder = os.path.basename(here)
    except NameError:
        folder = _SCRIPT_FOLDER_FALLBACK
    return "\\" + (folder + "/" if folder else "") + script_name


def _own_script_name(default):
    try:
        return os.path.splitext(os.path.basename(__file__))[0]
    except NameError:
        return default


def _read_watchdog_state():
    try:
        with open(FARM_WATCHDOG_STATE, "r", encoding="utf-8") as state_file:
            state = json.load(state_file)
            return state if isinstance(state, dict) else {}
    except (OSError, ValueError):
        return {}


def _write_watchdog_state(**changes):
    state = _read_watchdog_state()
    state.update(changes)
    temporary = FARM_WATCHDOG_STATE + ".tmp"
    try:
        with open(temporary, "w", encoding="utf-8") as state_file:
            json.dump(state, state_file, ensure_ascii=False)
        os.replace(temporary, FARM_WATCHDOG_STATE)
    except OSError as exc:
        echo(f"§e無法更新跨服恢復狀態：{exc}")


_watchdog_state = _read_watchdog_state()
if _watchdog_state.get("restart_pending"):
    _startup_f8 = bool(_watchdog_state.get("resume_f8", True))
    _lifecycle["restart_f8"] = _startup_f8
    _startup_direction = bool(_watchdog_state.get("direction", _startup_direction))
    _lifecycle["direction"] = _startup_direction
    _startup_run_started_at = _watchdog_state.get("run_started_at") or _startup_run_started_at
    _startup_run_deadline_at = _watchdog_state.get("run_deadline_at") or _startup_run_deadline_at
    _startup_auto_restart_at = _watchdog_state.get("auto_restart_at") or _startup_auto_restart_at
    _watchdog_state["restart_pending"] = False
    _watchdog_state["resume_f8"] = False
    try:
        with open(FARM_WATCHDOG_STATE + ".tmp", "w", encoding="utf-8") as state_file:
            json.dump(_watchdog_state, state_file, ensure_ascii=False)
        os.replace(FARM_WATCHDOG_STATE + ".tmp", FARM_WATCHDOG_STATE)
    except OSError:
        pass
    _lifecycle["run_started_at"] = _startup_run_started_at if _startup_f8 else None
    _lifecycle["run_deadline_at"] = _startup_run_deadline_at if _startup_f8 else None
    _lifecycle["auto_restart_at"] = _startup_auto_restart_at if _startup_f8 else None

# 衣櫥套裝（FARM/PEST）也存進狀態檔：重啟（含 Watchdog / Discord 重啟）後若記憶體內沒有，
# 就沿用狀態檔最後一次成功點擊的套裝，避免「已是 FARM 又點 FARM」以及冷卻校正失準。
LOADOUT_STATE_MAX_AGE_SECONDS = 3600.0
if _lifecycle.get("loadout") is None:
    _saved_loadout = _watchdog_state.get("loadout")
    _saved_loadout_at = _watchdog_state.get("loadout_at")
    try:
        _loadout_fresh = (
            isinstance(_saved_loadout, bool)
            and _saved_loadout_at is not None
            and 0 <= time.time() - float(_saved_loadout_at) <= LOADOUT_STATE_MAX_AGE_SECONDS
        )
    except (TypeError, ValueError):
        _loadout_fresh = False
    if _loadout_fresh:
        _lifecycle["loadout"] = _saved_loadout

# 還原配方失敗冷卻（狀態檔存的是牆上時間，換算回 monotonic）
try:
    _saved_cooldowns = _watchdog_state.get("crop_recipe_cooldowns")
    if isinstance(_saved_cooldowns, dict):
        _now_wall, _now_mono = time.time(), time.monotonic()
        for _recipe_id, _until_wall in _saved_cooldowns.items():
            _remaining = float(_until_wall) - _now_wall
            if 0 < _remaining <= CROP_RECIPE_FAIL_COOLDOWN_SECONDS:
                _crop_recipe_fail_until[str(_recipe_id)] = _now_mono + _remaining
except (TypeError, ValueError):
    pass

# 還原伺服器重啟旗標（Farm.py 在預警／撤離期間自重啟時不要失憶）
try:
    _rw = float(_watchdog_state.get("reboot_warned_at") or 0.0)
    _re = float(_watchdog_state.get("reboot_evac_at") or 0.0)
    if time.time() - max(_rw, _re) < REBOOT_WARN_WINDOW_SECONDS:
        _reboot_state["warned_at"], _reboot_state["evac_at"] = _rw, _re
except (TypeError, ValueError):
    pass


# ─────────────────────────────────────────────
# 可由 Discord 調整的目標值（存在 Farm_watchdog_state.json）
#   target_yaw    : 視角校正目標 yaw
#   return_target : 回程設定 {plot, block, radius, z, x_min, x_max, y}
#                   block 有設定 → AOTV 前掃描周圍、瞄準最近的該種方塊；
#                   block 未設定 → 沿用舊的座標瞄準（x_min~x_max, y, z）。z 一律用來判定「是否已到原點」
# ─────────────────────────────────────────────
DEFAULT_TARGET_YAW = 180.0
YAW_CHOICES = (180.0, 90.0, 0.0, -90.0)
DEFAULT_RETURN_TARGET = {"plot": 13, "x_min": -95, "x_max": -80, "y": 66.0, "z": -144.5, "block": None, "radius": 16}


def _sanitize_block_name(value):
    """回程瞄準的方塊 id：'minecraft:xxx[...]' 會去掉方塊狀態；空字串 / none / off 代表停用。"""
    if value is None:
        return None
    text = str(value).strip().lower().split("[", 1)[0].strip()
    if text in ("", "none", "off", "null"):
        return None
    if not re.fullmatch(r"[a-z0-9_:./-]+", text):
        return None
    return text


def _sanitize_return_target(raw):
    target = dict(DEFAULT_RETURN_TARGET)
    if isinstance(raw, dict):
        try:
            if "plot" in raw: target["plot"] = int(raw["plot"])
            if "x_min" in raw: target["x_min"] = int(raw["x_min"])
            if "x_max" in raw: target["x_max"] = int(raw["x_max"])
            if "y" in raw: target["y"] = float(raw["y"])
            if "z" in raw: target["z"] = float(raw["z"])
            if "radius" in raw: target["radius"] = int(raw["radius"])
            if "block" in raw: target["block"] = _sanitize_block_name(raw["block"])
        except (TypeError, ValueError):
            return dict(DEFAULT_RETURN_TARGET)
    if not 1 <= target["plot"] <= 24:
        target["plot"] = DEFAULT_RETURN_TARGET["plot"]
    if target["x_min"] > target["x_max"]:
        target["x_min"], target["x_max"] = target["x_max"], target["x_min"]
    target["radius"] = min(max(target["radius"], 4), 32)
    return target


def _sanitize_target_yaw(raw):
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return DEFAULT_TARGET_YAW
    return value if -360.0 <= value <= 360.0 else DEFAULT_TARGET_YAW


_target_yaw = _sanitize_target_yaw(_watchdog_state.get("target_yaw", DEFAULT_TARGET_YAW))
_return_target = _sanitize_return_target(_watchdog_state.get("return_target"))


def _sanitize_target_pitch(raw):
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return None
    return value if -90.0 <= value <= 90.0 else None


# None = 尚未學到；第一次「真的用糾正物品」且 yaw 到位後會記錄並存檔
_target_pitch = _sanitize_target_pitch(_watchdog_state.get("target_pitch"))


def _sanitize_features(raw):
    features = {name: True for name in FEATURE_LABELS}
    if isinstance(raw, dict):
        for name in FEATURE_LABELS:
            if name in raw:
                features[name] = bool(raw[name])
    return features


_test_mode_flag = bool(_watchdog_state.get("test_mode", False))
_features = _sanitize_features(_watchdog_state.get("features"))


def _yaw_diff(yaw, target):
    """兩個 yaw 之間的最短角度差（0~180）。"""
    return abs((float(yaw) - float(target) + 180.0) % 360.0 - 180.0)


def _view_settings_text():
    rt = _return_target
    if rt.get("block"):
        aim_text = f"AOTV 瞄準最近的 {rt['block']}（掃描半徑 {rt['radius']}）、到位 Z={rt['z']:g}"
    else:
        aim_text = f"AOTV 目標座標 X={rt['x_min']}~{rt['x_max']}、Y={rt['y']:g}、Z={rt['z']:g}（未設定瞄準方塊）"
    pitch_text = f"{_target_pitch:g}" if _target_pitch is not None else "未知"
    src = "糾正物品" if _item_view_live else "上次記錄值（尚未讀到糾正物品）"
    return f"視角目標 yaw={_target_yaw:g}、pitch={pitch_text}（來源：{src}）；回程 Plot={rt['plot']}、{aim_text}"


def _read_dotenv():
    values = {}
    try:
        with open(os.path.join(_farm_log_directory, ".env"), "r", encoding="utf-8") as env_file:
            for raw_line in env_file:
                line = raw_line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, value = line.split("=", 1)
                values[key.strip()] = value.strip().strip('"').strip("'")
    except OSError:
        pass
    return values


_farmbot_env = _read_dotenv()
_FARMBOT_CONTROL_SECRET = _farmbot_env.get("FARMBOT_CONTROL_SECRET", "")
_FARMBOT_CONTROL_PORT = int(_farmbot_env.get("FARMBOT_CONTROL_PORT", "38751") or "38751")
_game_echo = echo
_game_echo_json = echo_json
_remote_request_queue = queue.Queue()
_discord_bot_process = None
_farm_heartbeat_at = time.monotonic()
_last_attack_send_error_log = 0.0


def echo(*messages):
    text = " ".join(str(message) for message in messages)
    try:
        line = f"{time.strftime('%Y-%m-%dT%H:%M:%S%z')}\t{text}\n"
        with open(FARM_ACTIVITY_LOG, "a", encoding="utf-8") as activity_file:
            activity_file.write(line)
            if activity_file.tell() > 1_000_000:
                activity_file.flush()
                with open(FARM_ACTIVITY_LOG, "rb") as source:
                    source.seek(-500_000, os.SEEK_END)
                    source.readline()
                    tail = source.read()
                with open(FARM_ACTIVITY_LOG, "wb") as target:
                    target.write(tail)
    except OSError:
        pass
    colors = {
        "0": "black", "1": "dark_blue", "2": "dark_green", "3": "dark_aqua",
        "4": "dark_red", "5": "dark_purple", "6": "gold", "7": "gray",
        "8": "dark_gray", "9": "blue", "a": "green", "b": "aqua",
        "c": "red", "d": "light_purple", "e": "yellow", "f": "white",
    }
    categories = (
        (("害蟲", "除蟲", "PEST"), "除蟲", "light_purple", "item/spider_eye"),
        (("衣櫥", "裝備", "套裝"), "裝備", "aqua", "item/diamond_chestplate"),
        (("農田", "挖田", "FARM"), "農田", "green", "item/diamond_hoe"),
        (("冷卻", "cooldown"), "冷卻", "yellow", "item/clock"),
        (("商店", "出售", "賣出"), "交易", "gold", "item/emerald"),
    )
    category = next(
        (entry for entry in categories if any(needle in text for needle in entry[0])),
        ((), "系統", "gray", None),
    )
    _, label, default_color, sprite = category
    components = [{"text": f"[{label}] ", "color": default_color}]
    if sprite:
        components.extend([
            {"text": "· ", "color": "dark_gray"},
            {"type": "object", "atlas": "minecraft:items", "sprite": sprite},
            {"text": " ", "color": default_color},
        ])
    style = {"color": default_color}
    for index, part in enumerate(re.split(r"§([0-9a-fk-or])", text, flags=re.IGNORECASE)):
        if index % 2:
            code = part.lower()
            if code in colors:
                style = {"color": colors[code]}
            elif code == "l":
                style["bold"] = True
            elif code == "o":
                style["italic"] = True
            elif code == "m":
                style["strikethrough"] = True
            elif code == "n":
                style["underlined"] = True
            elif code == "k":
                style["obfuscated"] = True
            elif code == "r":
                style = {"color": default_color}
        elif part:
            components.append({"text": part, **style})
    try:
        return _game_echo_json({"text": "", "extra": components})
    except Exception:
        return _game_echo(*messages)


def _set_direction(value):
    """Keep the farming lane direction across script and watchdog restarts."""
    global 方向
    value = bool(value)
    if 方向 == value:
        return
    方向 = value
    _lifecycle["direction"] = value
    _write_watchdog_state(direction=value)


def farm_attack_monitor():
    """Refresh the held attack input once every randomized 50–60 seconds."""
    global _last_attack_send_error_log
    echo("挖田攻擊監測 Thread 啟動")
    next_attack_refresh = None
    while not _stop_event.is_set():
        if (
            _farm_attack_monitor_active.is_set()
            and 開關
            and 農業中
            and not 害蟲處理中
            and not anti_fail_hold.is_set()
        ):
            now = time.monotonic()
            if next_attack_refresh is None:
                next_attack_refresh = now + random.uniform(50.0, 60.0)
            try:
                refresh = now >= next_attack_refresh
                if refresh:
                    with _attack_input_lock:
                        if (
                            _farm_attack_monitor_active.is_set()
                            and 開關
                            and 農業中
                            and not 害蟲處理中
                            and not anti_fail_hold.is_set()
                        ):
                            左鍵(False)
                            flush()
                        else:
                            next_attack_refresh = None
                            continue
                    if _stop_event.wait(random.uniform(0.1, 0.2)):
                        return
                    with _attack_input_lock:
                        if (
                            _farm_attack_monitor_active.is_set()
                            and 開關
                            and 農業中
                            and not 害蟲處理中
                            and not anti_fail_hold.is_set()
                        ):
                            if 農田朝向正確():
                                左鍵(True)
                                flush()
                            else:
                                _farm_attack_monitor_active.clear()
                    next_attack_refresh = time.monotonic() + random.uniform(50.0, 60.0)
                else:
                    _stop_event.wait(min(0.5, next_attack_refresh - now))
            except Exception as exc:
                now = time.monotonic()
                if now - _last_attack_send_error_log >= 5.0:
                    echo(f"§c[挖田監測] 攻擊狀態刷新失敗：{type(exc).__name__}: {exc}")
                    _last_attack_send_error_log = now
                if now >= next_attack_refresh:
                    next_attack_refresh = now + random.uniform(50.0, 60.0)
        else:
            next_attack_refresh = None
            _stop_event.wait(0.15)


開關: bool        = _startup_f8
方向: bool        = _startup_direction   # False=左 True=右；重啟沿用上次方向
農業中: bool      = True
害蟲處理中: bool  = False
待處理害蟲_plot = None
裝備優先完成 = threading.Event()
裝備套裝: bool | None = _lifecycle["loadout"]   # False=FARM，True=PEST；沿用同一 runtime 的最後一次衣櫥點擊
last_switch_time  = 0
裝備切換目標: bool | None = None   # 切換途中固定衣櫥目標，避免冷卻讀值變動造成反覆切換
_run_started_at: float | None = _startup_run_started_at if _startup_f8 else None
_run_deadline_at: float | None = _startup_run_deadline_at if _startup_f8 else None
if _run_started_at is not None and _run_deadline_at is None:
    _run_deadline_at = _run_started_at + FARM_DURATION_HOURS * 3600
_lifecycle["run_deadline_at"] = _run_deadline_at
_run_finished = threading.Event()
_restart_started = threading.Event()

action_lock = threading.Lock()
_attack_input_lock = threading.Lock()
anti_fail_hold = threading.Event()
anti_fail_reason = ""
_farm_attack_monitor_active = threading.Event()


def _start_run_timer_if_needed():
    global _run_started_at, _run_deadline_at
    if _run_started_at is None:
        _run_started_at = time.time()
    if _run_deadline_at is None:
        _run_deadline_at = _run_started_at + FARM_DURATION_HOURS * 3600
    _lifecycle["run_started_at"] = _run_started_at
    _lifecycle["run_deadline_at"] = _run_deadline_at
    _write_watchdog_state(
        run_started_at=_run_started_at,
        run_deadline_at=_run_deadline_at,
    )


def _format_timer_remaining(seconds):
    total_minutes = max(0, int(seconds // 60))
    hours, minutes = divmod(total_minutes, 60)
    return f"{hours} 小時 {minutes} 分鐘" if hours else f"{minutes} 分鐘"


def 等(inn=0.5):
    delay = random.uniform(random.uniform(0, inn), inn + random.uniform(0.1, 0.5))
    _stop_event.wait(delay)


def 開啟介面指令(command):
    if anti_fail_hold.is_set():
        echo(f"§e[Anti-Fail] 已攔截指令 {command!r}；未呼叫 execute()")
        return False
    time.sleep(random.uniform(0.1, 0.2))
    if anti_fail_hold.is_set():
        echo(f"§e[Anti-Fail] 等待期間進入暫停，已攔截指令 {command!r}；未呼叫 execute()")
        return False
    try:
        execute(command)
        return True
    except Exception as exc:
        echo(f"§c[指令錯誤] execute({command!r}) 拋出 {type(exc).__name__}: {exc}")
        return False


def 停止移動(標籤):
    echo(f"⏹ 停止移動：{標籤}")
    with _attack_input_lock:
        _farm_attack_monitor_active.clear()
        # 僅釋放農田移動流程控制的按鍵，避免清掉其他用途的按鍵狀態。
        w(False)
        a(False)
        d(False)
        左鍵(False)
        flush()


def 安全切換欄位(槽位):
    """切 hotbar 前先送達左鍵放開，避免非鋤頭欄位收到攻擊輸入。"""
    if anti_fail_hold.is_set():
        return False
    with _attack_input_lock:
        _farm_attack_monitor_active.clear()
        左鍵(False)
        flush()
        player_inventory_select_slot(槽位)
        flush()
    return True


def 校正農田視角(timeout=8.0):
    """使用視角糾正物品校正 yaw + pitch。

    1. 至少真的用一次物品（不再因為 yaw 剛好吻合就跳過，那會讓回程瞄準留下的低頭 pitch 殘留）。
    2. 成功條件 = yaw 到位 且 pitch 到位；pitch 目標第一次成功時學習並存檔。
    3. 每次點擊前確認主手真的是糾正物品，避免切欄位延遲時拿鋤頭點到泥土。
    """
    global _target_pitch
    _sync_target_from_item(0.5)
    deadline = time.time() + timeout
    target_yaw = _target_yaw
    used_item = False

    # 視角已經在目標範圍內（例如 /warp garden 或回紀錄點後視角已還原）就不必再用校正物品。
    # pitch 尚未學到時仍要用一次物品來學習；pitch 殘留的情況由下面的 pitch 容差一併排除。
    try:
        _ori = player_orientation()
        if (
            _ori
            and _target_pitch is not None
            and _yaw_diff(_ori[0], target_yaw) < 1.0
            and abs(float(_ori[1]) - _target_pitch) <= CORRECT_PITCH_TOLERANCE
        ):
            echo("§a[視角] 視角已在目標範圍內，略過校正物品")
            return True
    except (TypeError, ValueError, IndexError):
        pass

    while not _stop_event.is_set() and time.time() < deadline:
        if _evacuating() or not 在花園():
            echo("§e[視角] 目前不在 Garden（可能被傳送或伺服器重啟），中止視角校正")
            break
        if used_item:
            orientation = player_orientation()
            if orientation and _yaw_diff(orientation[0], target_yaw) < 1.0:
                pitch = float(orientation[1])
                if _target_pitch is None:
                    _target_pitch = pitch
                    _write_watchdog_state(target_pitch=_target_pitch)
                    echo(f"§a[視角] 已記錄糾正後 pitch={pitch:.1f}")
                if abs(pitch - _target_pitch) <= CORRECT_PITCH_TOLERANCE:
                    安全切換欄位(HOE)
                    左鍵(False)
                    return True

        if not 安全切換欄位(視角糾正):
            break
        等(0.15)
        hand = get_main_hand_item(timeout=0.6)
        if not (isinstance(hand, dict) and hand.get("slot") == 視角糾正):
            等(0.1)
            continue
        左鍵(True)
        等(0.08)
        左鍵(False)
        used_item = True
        等(0.3)

    安全切換欄位(HOE)
    左鍵(False)
    return False


def 在花園() -> bool:
    if _test_mode_flag:
        return True
    area = get_tablist_info_confirmed("area").get("area") or ""
    return area == "Garden"


def 確認所在Plot(attempts=3):
    previous = None
    for attempt in range(attempts):
        current = get_current_plot(timeout=1.5)
        if current is not None and current == previous:
            return current
        previous = current
        if attempt + 1 < attempts:
            time.sleep(random.uniform(0.15, 0.25))
    return None


def 農田朝向正確(orientation=None, pitch_tolerance=ANTI_FAIL_PITCH_TOLERANCE) -> bool:
    if orientation is None:
        orientation = player_orientation()
    if not orientation:
        return False
    if _yaw_diff(orientation[0], _target_yaw) > ANTI_FAIL_YAW_TOLERANCE:
        return False
    if _target_pitch is not None:
        try:
            if abs(float(orientation[1]) - _target_pitch) > pitch_tolerance:
                return False
        except (TypeError, ValueError, IndexError):
            return False
    return True


def 啟動防故障暫停(reason):
    global 開關, 農業中, anti_fail_reason
    if anti_fail_hold.is_set():
        return
    anti_fail_reason = reason
    anti_fail_hold.set()
    開關 = False
    _lifecycle["restart_f8"] = False
    農業中 = False
    _lifecycle["auto_restart_at"] = None
    _write_watchdog_state(f8=False, run_started_at=_run_started_at, run_deadline_at=_run_deadline_at, auto_restart_at=None)
    _release_all()
    echo(f"§c[Anti-Fail] 偵測到異常（{reason}），已停用農田操作；請檢查位置與視角後按 F8 嘗試恢復")


def anti_fail_monitor():
    """外力改變視角時用糾正物品復位。"""
    global 農業中
    wrong_yaw_since = None
    correct_fail_count = 0

    while not _stop_event.is_set():
        _sync_target_from_item()
        active = 開關 and not 害蟲處理中 and not anti_fail_hold.is_set() and 在花園()
        if not active:
            wrong_yaw_since = None
            _stop_event.wait(0.15)
            continue

        state = player_tracker.get()
        now = time.monotonic()

        yaw_aligned = 農田朝向正確((state["yaw"], state["pitch"]))
        if not yaw_aligned:
            if wrong_yaw_since is None:
                wrong_yaw_since = now
            elif now - wrong_yaw_since >= ANTI_FAIL_TURN_CONFIRM_SECONDS:
                wrong_yaw_since = None
                echo("§e[Anti-Fail] 視角偏離農田方向，使用視角糾正物品校正後繼續")
                with action_lock:
                    if _stop_event.is_set() or not 開關 or 害蟲處理中 or anti_fail_hold.is_set():
                        continue
                    was_farming = 農業中
                    if was_farming:
                        農業中 = False
                        停止移動("視角糾正開始")
                    corrected = 校正農田視角(timeout=8.0)
                    if corrected:
                        correct_fail_count = 0
                        if 開關 and not 害蟲處理中 and not anti_fail_hold.is_set():
                            農業中 = was_farming
                        echo("§a[Anti-Fail] 視角已校正，繼續目前流程")
                    else:
                        # 校正沒成功 → 不恢復農業，避免帶著錯誤視角繼續挖
                        correct_fail_count += 1
                        停止移動("視角校正失敗")
                        if correct_fail_count >= ANTI_FAIL_CORRECT_MAX_FAILS:
                            correct_fail_count = 0
                            啟動防故障暫停("視角連續校正失敗，已停止以免低頭挖土")
                        else:
                            echo(f"§e[Anti-Fail] 視角校正失敗（{correct_fail_count}/{ANTI_FAIL_CORRECT_MAX_FAILS}），保持暫停並重試")
                continue
        else:
            wrong_yaw_since = None

        _stop_event.wait(0.1)


def 恢復防故障暫停() -> bool:
    global 農業中, anti_fail_reason
    if not anti_fail_hold.is_set():
        return True

    area = get_tablist_info_confirmed("area").get("area")
    if area != "Garden":
        echo(f"§e[Anti-Fail] 尚未恢復：Area={area!r}，必須先回到 Garden；保持暫停")
        return False

    anti_fail_hold.clear()
    anti_fail_reason = ""
    農業中 = True
    echo("§a[Anti-Fail] 已確認位於 Garden，解除暫停；視角糾正由監控器接手")
    return True


def 更新自重啟排程(reset=False):
    _lifecycle["restart_f8"] = bool(開關)
    if not 開關:
        _lifecycle["auto_restart_at"] = None
        _write_watchdog_state(f8=False, run_started_at=_run_started_at, run_deadline_at=_run_deadline_at, auto_restart_at=None)
        return
    if reset or not _lifecycle.get("auto_restart_at"):
        interval = random.uniform(AUTO_RESTART_MIN_SECONDS, AUTO_RESTART_MAX_SECONDS)
        _lifecycle["auto_restart_at"] = time.time() + interval
        echo(f"定時自重啟已排程：{interval / 60:.1f} 分鐘後（3–5 分鐘範圍）")
    _write_watchdog_state(
        f8=True,
        run_started_at=_run_started_at,
        auto_restart_at=_lifecycle.get("auto_restart_at"),
        direction=bool(方向),
    )


def _restart_farm_job(reason, force_f8=None, schedule_next=False):
    global 開關, _run_started_at
    with action_lock:
        if _restart_started.is_set():
            return False
        _restart_started.set()
        resume_f8 = 開關 if force_f8 is None else bool(force_f8)
        if schedule_next:
            interval = random.uniform(AUTO_RESTART_MIN_SECONDS, AUTO_RESTART_MAX_SECONDS)
            _lifecycle["auto_restart_at"] = time.time() + interval
        elif resume_f8 and not _lifecycle.get("auto_restart_at"):
            interval = random.uniform(AUTO_RESTART_MIN_SECONDS, AUTO_RESTART_MAX_SECONDS)
            _lifecycle["auto_restart_at"] = time.time() + interval
        if resume_f8:
            _start_run_timer_if_needed()
        _lifecycle["restart_f8"] = resume_f8
        _lifecycle["direction"] = bool(方向)
        _lifecycle["run_started_at"] = _run_started_at
        _lifecycle["run_deadline_at"] = _run_deadline_at
        _write_watchdog_state(
            f8=resume_f8,
            restart_pending=True,
            resume_f8=resume_f8,
            run_started_at=_run_started_at,
            run_deadline_at=_run_deadline_at,
            auto_restart_at=_lifecycle.get("auto_restart_at"),
            direction=bool(方向),
        )
        開關 = False
        停止移動(reason)
        try:
            execute(_minescript_script_command(_own_script_name("Farm")))
        except Exception as exc:
            _restart_started.clear()
            _lifecycle["restart_f8"] = resume_f8
            開關 = resume_f8
            _write_watchdog_state(
                f8=resume_f8,
                restart_pending=False,
                resume_f8=False,
                run_started_at=_run_started_at,
                run_deadline_at=_run_deadline_at,
                auto_restart_at=_lifecycle.get("auto_restart_at"),
                direction=bool(方向),
            )
            echo(f"§c重啟 Farm.py 失敗：{type(exc).__name__}: {exc}")
            return False

        _stop_event.set()
        echo(f"已透過 Minescript 啟動新 Farm.py job（{reason}），F8={resume_f8}")
        return True


def _discord_control_server():
    if not _FARMBOT_CONTROL_SECRET:
        echo("§eDiscord 遠端控制未啟動：.env 缺少 FARMBOT_CONTROL_SECRET")
        return

    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    bind_deadline = time.monotonic() + 15.0
    while not _stop_event.is_set():
        try:
            server.bind(("127.0.0.1", _FARMBOT_CONTROL_PORT))
            server.listen(4)
            server.settimeout(0.5)
            break
        except OSError as exc:
            if time.monotonic() >= bind_deadline:
                echo(f"§cDiscord 遠端控制 socket 啟動失敗：{exc}")
                server.close()
                return
            _stop_event.wait(0.5)
    else:
        server.close()
        return

    while not _stop_event.is_set():
        try:
            conn, _ = server.accept()
        except socket.timeout:
            continue
        except OSError:
            break

        with conn:
            try:
                conn.settimeout(12.0)
                reader = conn.makefile("r", encoding="utf-8")
                request = json.loads(reader.readline(4096))
                reader.close()
                supplied_secret = str(request.get("secret", ""))
                if not hmac.compare_digest(supplied_secret, _FARMBOT_CONTROL_SECRET):
                    response = {"ok": False, "error": "local control authentication failed"}
                elif request.get("action") == "health":
                    heartbeat_age = time.monotonic() - _farm_heartbeat_at
                    response = {
                        "ok": heartbeat_age <= 5.0,
                        "heartbeat_age": heartbeat_age,
                        "f8": bool(開關),
                        "run_started_at": _run_started_at,
                        "run_deadline_at": _run_deadline_at,
                        "auto_restart_at": _lifecycle.get("auto_restart_at"),
                        "direction": bool(方向),
                    }
                else:
                    pending = {
                        "action": request.get("action"),
                        "minutes": request.get("minutes"),
                        "params": request,
                        "event": threading.Event(),
                        "result": {},
                    }
                    _remote_request_queue.put(pending)
                    if pending["event"].wait(10.0):
                        response = pending["result"]
                    else:
                        response = {"ok": False, "error": "Farm.py did not handle the request in time"}
                conn.sendall(json.dumps(response, ensure_ascii=False).encode("utf-8") + b"\n")
            except Exception as exc:
                try:
                    conn.sendall(json.dumps({"ok": False, "error": str(exc)}).encode("utf-8") + b"\n")
                except OSError:
                    pass

    server.close()


def _start_discord_bot():
    global _discord_bot_process
    # Discord bot 已內嵌在 Farm_watchdog.py；再啟動舊的 discord_control.py 會和它搶同一個 token，
    # 並用舊指令清單覆蓋 /farm_panel 的註冊。
    echo("Discord bot 由 Farm_watchdog 內嵌執行，略過舊的 discord_control.py")
    return
    bot_script = os.path.join(_farm_log_directory, "discord_control.py")
    if not os.path.isfile(bot_script) or not _farmbot_env.get("DISCORD_BOT_TOKEN"):
        echo("§eDiscord bot 未自動啟動：找不到 discord_control.py 或 .env 沒有 DISCORD_BOT_TOKEN")
        return
    try:
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        with open(os.path.join(_farm_log_directory, "discord_control.log"), "a", encoding="utf-8") as bot_log:
            _discord_bot_process = subprocess.Popen(
                [sys.executable, bot_script],
                cwd=_farm_log_directory,
                stdin=subprocess.DEVNULL,
                stdout=bot_log,
                stderr=subprocess.STDOUT,
                close_fds=True,
                creationflags=flags,
            )
        echo("Discord 遠端控制程序已啟動（單一實例鎖會避免重複連線）")
    except Exception as exc:
        echo(f"§cDiscord bot 啟動失敗：{type(exc).__name__}: {exc}")


def _start_survival_watchdog():
    try:
        try:
            os.remove(FARM_WATCHDOG_STOP)
        except FileNotFoundError:
            pass
        execute(_minescript_script_command("Farm_watchdog"))
        echo("跨服存活監控已啟動")
    except Exception as exc:
        echo(f"§c跨服存活監控啟動失敗：{type(exc).__name__}: {exc}")


def stop_farm_scripts_from_f9():
    """F9: stop this Farm.py lifecycle, the watchdog and the Discord bot (embedded in the watchdog)."""
    global 開關
    開關 = False
    _lifecycle["restart_f8"] = False
    _lifecycle["auto_restart_at"] = None
    _lifecycle["run_deadline_at"] = None
    _write_watchdog_state(
        f8=False,
        restart_pending=False,
        resume_f8=False,
        auto_restart_at=None,
        run_deadline_at=None,
    )
    try:
        with open(FARM_WATCHDOG_STOP, "w", encoding="utf-8") as stop_file:
            stop_file.write(str(time.time()))
    except OSError as exc:
        echo(f"§c無法通知 Farm watchdog 停止：{exc}")
    _stop_event.set()
    停止移動("F9 停止 Farm 與 watchdog")
    echo("§cF9：Farm.py、Farm watchdog 與 Discord bot 已要求全部停止")


def 回到Garden(timeout=120.0):
    """F8 開啟時依 Area 狀態返回 SkyBlock Garden；重連後可從磁碟階段續做。"""
    global _farm_heartbeat_at
    deadline = time.monotonic() + timeout
    state = _read_watchdog_state()
    recovery_stage = state.get("recovery_stage")
    none_since = None
    outside_since = None
    garden_since = None
    last_command_at = 0.0

    停止移動("返回 Garden 流程開始")
    while not _stop_event.is_set() and 開關 and time.monotonic() < deadline:
        _farm_heartbeat_at = time.monotonic()
        area = get_tablist_info_confirmed("area").get("area")
        now = time.monotonic()

        if area == "Garden":
            if garden_since is None:
                garden_since = now
            if now - garden_since >= GARDEN_LEAVE_CONFIRM_SECONDS:
                _clear_reboot_state_if_back_in_garden()
                _write_watchdog_state(recovery_stage=None)
                if anti_fail_hold.is_set():
                    恢復防故障暫停()
                echo("§a已連續確認 3 秒位於 Garden，恢復 Farm.py 工作流程")
                return True
            outside_since = None
            none_since = None
            _stop_event.wait(0.5)
            continue

        garden_since = None

        if area:
            none_since = None
            if outside_since is None:
                outside_since = now
            if now - outside_since >= (EVAC_GARDEN_LEAVE_CONFIRM_SECONDS if _evacuating() else GARDEN_LEAVE_CONFIRM_SECONDS) and (
                recovery_stage != "warp_pending" or now - last_command_at >= 15.0
            ):
                recovery_stage = "warp_pending"
                _write_watchdog_state(recovery_stage=recovery_stage)
                停止移動("準備傳送回 Garden")
                echo(f"目前 Area={area!r}，執行 /warp garden")
                execute("/warp garden")
                last_command_at = now
        else:
            outside_since = None
            if none_since is None:
                none_since = now
            elif now - none_since >= AREA_NONE_CONFIRM_SECONDS:
                if recovery_stage != "skyblock_pending":
                    recovery_stage = "skyblock_pending"
                    _write_watchdog_state(recovery_stage=recovery_stage)
                    echo("Area=None 已連續確認超過 5 秒，先返回主大廳")
                    execute("/l")
                    wait_until = time.monotonic() + random.uniform(3.0, 5.0)
                    while not _stop_event.is_set() and 開關 and time.monotonic() < wait_until:
                        _farm_heartbeat_at = time.monotonic()
                        _stop_event.wait(0.2)
                    if _stop_event.is_set() or not 開關:
                        return False
                if recovery_stage == "skyblock_pending" and now - last_command_at >= 5.0:
                    echo("執行 /skyblock，等待 SkyBlock Area 載入")
                    execute("/skyblock")
                    last_command_at = now

        _stop_event.wait(0.5)

    echo("§e返回 Garden 等待逾時；存活監控仍會維持 Farm.py 並在重連後重試")
    return False


def 回Garden監控():
    echo("Garden 返回監控 Thread 啟動")
    garden_since = None
    while not _stop_event.is_set():
        if not 開關 or _test_mode_flag:
            等(1.0)
            continue
        area = get_tablist_info_confirmed("area").get("area")
        if area == "Garden":
            if garden_since is None:
                garden_since = time.monotonic()
            if (
                time.monotonic() - garden_since >= GARDEN_LEAVE_CONFIRM_SECONDS
            ):
                _clear_reboot_state_if_back_in_garden()
                if _read_watchdog_state().get("recovery_stage"):
                    _write_watchdog_state(recovery_stage=None)
            等(1.0)
            continue
        garden_since = None
        with action_lock:
            if _stop_event.is_set() or not 開關:
                continue
            if get_tablist_info_confirmed("area").get("area") != "Garden":
                回到Garden(timeout=120.0)


def _panel_info():
    now = time.time()
    return {
        "f8": bool(開關),
        "farming": bool(農業中),
        "pest_active": bool(害蟲處理中),
        "anti_fail": anti_fail_reason if anti_fail_hold.is_set() else None,
        "remaining": _format_timer_remaining(_run_deadline_at - now) if _run_deadline_at is not None else None,
        "test_mode": bool(_test_mode_flag),
        "features": {name: _feature_on(name) for name in FEATURE_LABELS},
        "feature_labels": dict(FEATURE_LABELS),
        "pest_health": [_read_watchdog_state().get("pest_min_health"), _read_watchdog_state().get("pest_max_health")],
        "yaw": _target_yaw,
        "pitch": _target_pitch,
        "view_live": bool(_item_view_live),
        "return_target": dict(_return_target),
    }


def _process_discord_requests():
    global 開關, _run_deadline_at, _target_yaw, _return_target, _test_mode_flag, _manual_pest_plot
    while True:
        try:
            pending = _remote_request_queue.get_nowait()
        except queue.Empty:
            return False

        action = pending["action"]
        try:
            if action == "hypixel_ban":
                duration_seconds = max(1, int(pending.get("duration_seconds", 1)))
                deadline = time.time() + duration_seconds

                def _ban_countdown(until):
                    remaining = max(0, int(until - time.time()))
                    days, remaining = divmod(remaining, 86400)
                    hours, remaining = divmod(remaining, 3600)
                    minutes, seconds = divmod(remaining, 60)
                    return f"{days}d {hours}h {minutes}m {seconds}s"

                display_time = _ban_countdown(deadline)
                開關 = False
                _lifecycle["restart_f8"] = False
                _lifecycle["auto_restart_at"] = None
                _write_watchdog_state(
                    f8=False,
                    resume_f8=False,
                    restart_pending=False,
                    admin_lock=True,
                    auto_restart_at=None,
                )
                with open(FARM_ADMIN_LOCK, "w", encoding="utf-8") as lock_file:
                    json.dump(
                        {
                            "locked": True,
                            "created_at": time.time(),
                            "deadline": deadline,
                            "duration_seconds": duration_seconds,
                            "reason": "管理員鎖定",
                        },
                        lock_file,
                        ensure_ascii=False,
                    )
                停止移動("管理員鎖定")
                try:
                    countdown_literal = json.dumps(display_time, ensure_ascii=False)
                    _eval_pyjinn_script(f"""
mc = JavaClass("net.minecraft.client.Minecraft").getInstance()
server = mc.getCurrentServer()

if server is not None:
    address = str(server.ip).lower().strip()
    host = address.split(":")[0].rstrip(".")

    if host == "hypixel.net" or host.endswith(".hypixel.net"):
        Component = JavaClass("net.minecraft.network.chat.Component")
        Formatting = JavaClass("net.minecraft.ChatFormatting")

        line1 = Component.literal("You are temporarily banned for ").withColor(0xFF5555)
        line1 = line1.append(Component.literal({countdown_literal}).withColor(0xFFFFFF))
        line1 = line1.append(Component.literal(" from this server!").withColor(0xFF5555))

        line2 = Component.literal(
            "Reason: Your account's security appeal was processed and the account "
            "has entered a recovery phase and will be able to access the server "
            "again after 30 days. Use this time to change passwords, emails and "
            "security questions."
        ).withColor(0xFFFFFF)

        line3 = Component.literal("Find out more: ").withColor(0xAAAAAA)
        url = Component.literal("https://www.hypixel.net/appeal").withColor(0x55FFFF).withStyle(Formatting.UNDERLINE)
        line3 = line3.append(url)

        message = Component.literal("")
        message = message.append(line1)
        message = message.append(Component.literal("\\n"))
        message = message.append(line2)
        message = message.append(Component.literal("\\n"))
        message = message.append(line3)

        connection = mc.getConnection()
        if connection is not None:
            connection.getConnection().disconnect(message)
""")
                except Exception as exc:
                    echo(f"§c已鎖定，但自訂斷線畫面顯示失敗：{type(exc).__name__}: {exc}")
                echo(f"§c管理員鎖定：F8 已關閉；倒數：{display_time}；按 F10 才能解除")
                pending["result"] = {
                    "ok": True,
                    "message": f"已停止 FarmBot 並鎖定重新加入。倒數：{display_time}；時間歸零後仍需在 Minecraft 按 F10 才能解除。",
                }
            elif action == "admin_unlock":
                try:
                    os.remove(FARM_ADMIN_LOCK)
                except FileNotFoundError:
                    pass
                _lifecycle["restart_f8"] = False
                _lifecycle["auto_restart_at"] = None
                _write_watchdog_state(
                    f8=False,
                    resume_f8=False,
                    restart_pending=False,
                    admin_lock=False,
                    auto_restart_at=None,
                )
                echo("§a管理員已解除 FarmBot 緊急鎖定；F8 維持關閉")
                pending["result"] = {
                    "ok": True,
                    "message": "已解除緊急鎖定；F8 仍維持關閉，需手動確認後再啟動。",
                }
            elif action == "toggle":
                開關 = not 開關
                if 開關 and anti_fail_hold.is_set():
                    resumed = 恢復防故障暫停()
                else:
                    resumed = True
                if 開關 and _run_started_at is None:
                    _start_run_timer_if_needed()
                更新自重啟排程(reset=開關)
                if not 開關:
                    停止移動("Discord 遠端 F8 關閉")
                state = "開" if 開關 else "關"
                echo(f"Discord 遠端 F8 切換：{state}")
                pending["result"] = {
                    "ok": bool(resumed),
                    "message": f"遠端 F8 切換完成：{'開' if 開關 else '關'}" if resumed else "Anti-Fail 位置／視角檢查未通過，仍保持暫停",
                }
            elif action == "status":
                remaining_text = (
                    _format_timer_remaining(_run_deadline_at - time.time())
                    if _run_deadline_at is not None else "尚未開始計時"
                )
                pending["result"] = {
                    "ok": True,
                    "status": f"F8={'開' if 開關 else '關'} · 農業={'進行中' if 農業中 else '暫停'} · 除蟲={'進行中' if 害蟲處理中 else '否'} · 計時剩餘={remaining_text} · Anti-Fail={'鎖定：' + anti_fail_reason if anti_fail_hold.is_set() else '正常'}",
                    "message": f"F8={'開' if 開關 else '關'}；農業={'進行中' if 農業中 else '暫停'}；除蟲={'進行中' if 害蟲處理中 else '否'}；計時剩餘={remaining_text}；Anti-Fail={'鎖定：' + anti_fail_reason if anti_fail_hold.is_set() else '正常'}；{_view_settings_text()}",
                }
            elif action == "add_time":
                try:
                    add_minutes = int(pending.get("minutes", 0))
                except (TypeError, ValueError):
                    add_minutes = 0
                if not 1 <= add_minutes <= 1440:
                    pending["result"] = {"ok": False, "message": "加時分鐘數需介於 1 到 1440 分鐘。"}
                elif _run_started_at is None or _run_deadline_at is None:
                    pending["result"] = {"ok": False, "message": "目前尚未開始運行計時，請先啟動 Farm 再加時。"}
                elif _run_deadline_at <= time.time():
                    pending["result"] = {"ok": False, "message": "計時已到期，請重新啟動 Farm 後再加時。"}
                else:
                    _run_deadline_at += add_minutes * 60
                    _lifecycle["run_deadline_at"] = _run_deadline_at
                    _write_watchdog_state(
                        run_started_at=_run_started_at,
                        run_deadline_at=_run_deadline_at,
                    )
                    remaining_text = _format_timer_remaining(_run_deadline_at - time.time())
                    message = f"已加時 {add_minutes} 分鐘，距離登出並結束約 {remaining_text}。"
                    echo(f"Discord 遠端加時：+{add_minutes} 分鐘；剩餘 {remaining_text}")
                    pending["result"] = {"ok": True, "message": message}
            elif action == "get_view":
                pending["result"] = {"ok": True, "message": _view_settings_text()}
            elif action == "set_view":
                params = pending.get("params") or {}
                if params.get("reset"):
                    new_yaw = _target_yaw
                    new_target = dict(DEFAULT_RETURN_TARGET)
                else:
                    new_yaw = _target_yaw
                    new_target = dict(_return_target)
                    if params.get("yaw") is not None:
                        raise ValueError("視角 yaw / pitch 以「視角糾正」物品上的 Selected 值為準，無法從 Discord 設定")
                    for key, caster in (("plot", int), ("x_min", int), ("x_max", int), ("y", float), ("z", float), ("radius", int)):
                        if params.get(key) is not None:
                            new_target[key] = caster(params[key])
                    if params.get("block") is not None:
                        new_target["block"] = _sanitize_block_name(params["block"])   # "none" = 停用
                    if params.get("crosshair"):
                        targeted = player_get_targeted_block(20)
                        if targeted is None:
                            raise ValueError("準心 20 格內沒有對準任何方塊，請先看向要當回程目標的方塊")
                        new_target["block"] = _sanitize_block_name(targeted.type)
                        if not new_target["block"]:
                            raise ValueError(f"無法辨識準心方塊：{targeted.type}")
                    if not 4 <= new_target["radius"] <= 32:
                        raise ValueError("掃描半徑需介於 4 到 32")
                    if not 1 <= new_target["plot"] <= 24:
                        raise ValueError("Plot 需介於 1 到 24")
                    if not -64.0 <= new_target["y"] <= 320.0:
                        raise ValueError("Y 需介於 -64 到 320")
                    if abs(new_target["z"]) > 30000000 or abs(new_target["x_min"]) > 30000000 or abs(new_target["x_max"]) > 30000000:
                        raise ValueError("座標超出世界範圍")
                    new_target = _sanitize_return_target(new_target)
                _target_yaw = new_yaw
                _return_target = new_target
                _write_watchdog_state(target_yaw=_target_yaw, return_target=_return_target)
                echo(f"Discord 遠端設定視角/回程目標：{_view_settings_text()}")
                pending["result"] = {"ok": True, "message": "已儲存：" + _view_settings_text()}
            elif action == "panel":
                pending["result"] = {"ok": True, "info": _panel_info()}
            elif action == "set_feature":
                params = pending.get("params") or {}
                name = str(params.get("name", ""))
                if name not in FEATURE_LABELS:
                    raise ValueError(f"未知功能 {name!r}，可用：{', '.join(FEATURE_LABELS)}")
                value = params.get("value")
                new_value = (not _feature_on(name)) if value in (None, "toggle") else bool(value)
                _features[name] = new_value
                _write_watchdog_state(features=_features)
                echo(f"Discord 功能開關：{FEATURE_LABELS[name]} → {'開' if new_value else '關'}")
                pending["result"] = {"ok": True, "message": f"{FEATURE_LABELS[name]} 已{'開啟' if new_value else '關閉'}"}
            elif action == "set_test_mode":
                params = pending.get("params") or {}
                value = params.get("value")
                new_value = (not _test_mode_flag) if value in (None, "toggle") else bool(value)
                if new_value and not _test_mode_flag:
                    # 進入測試模式：備份目前功能開關，並關掉 Hypixel 專屬功能
                    _write_watchdog_state(features_backup=dict(_features))
                    for name in HYPIXEL_ONLY_FEATURES:
                        _features[name] = False
                elif not new_value and _test_mode_flag:
                    backup = _read_watchdog_state().get("features_backup")
                    restored = _sanitize_features(backup)
                    _features.clear()
                    _features.update(restored)
                _test_mode_flag = new_value
                _write_watchdog_state(test_mode=_test_mode_flag, features=_features)
                echo(f"Discord 測試模式：{'開' if _test_mode_flag else '關'}")
                pending["result"] = {
                    "ok": True,
                    "message": "測試模式已開啟：忽略 Garden 判定，攔截 Hypixel 專屬指令，並關閉換裝／賣出／噴霧／Bonus" if _test_mode_flag
                    else "測試模式已關閉，功能開關已還原",
                }
            elif action == "set_pest_health":
                params = pending.get("params") or {}
                if params.get("reset"):
                    _write_watchdog_state(pest_min_health=None, pest_max_health=None)
                    pending["result"] = {"ok": True, "message": "蟲血量範圍已還原預設"}
                else:
                    low = float(params["min"])
                    high = float(params["max"])
                    if not 0 <= low < high:
                        raise ValueError("需滿足 0 <= 下限 < 上限")
                    _write_watchdog_state(pest_min_health=low, pest_max_health=high)
                    pending["result"] = {"ok": True, "message": f"蟲血量範圍：{low:g} < HP < {high:g}"}
            elif action == "pest_now":
                params = pending.get("params") or {}
                plot_value = int(params.get("plot", 0))
                if not _test_mode_flag and not 1 <= plot_value <= 24:
                    raise ValueError("非測試模式需指定 Plot 1 到 24")
                if not 0 <= plot_value <= 24:
                    raise ValueError("Plot 需介於 0（目前位置，測試用）到 24")
                if not 開關:
                    raise ValueError("F8 目前是關閉的，請先開啟再除蟲")
                if not _feature_on("pest"):
                    raise ValueError("除蟲功能目前是關閉的")
                _manual_pest_plot = plot_value
                pending["result"] = {"ok": True, "message": f"已排入手動除蟲：{'目前位置' if plot_value == 0 else f'Plot {plot_value}'}"}
            elif action == "resume_anti_fail":
                if not anti_fail_hold.is_set():
                    pending["result"] = {"ok": True, "message": "目前沒有 Anti-Fail 暫停"}
                else:
                    resumed = 恢復防故障暫停()
                    pending["result"] = {"ok": bool(resumed), "message": "已解除 Anti-Fail" if resumed else "位置／視角檢查未通過，仍保持暫停"}
            elif action == "screenshot":
                filename = f"farmbot_{time.time_ns()}.png"
                screenshot(filename)
                game_root = os.path.dirname(os.path.dirname(_farm_log_directory))
                image_path = os.path.join(game_root, "screenshots", filename)
                try:
                    scoreboard = get_scoreboard_lines(timeout=2.0)
                except Exception as exc:
                    scoreboard = None
                    echo(f"Discord 記分板快照讀取失敗：{type(exc).__name__}: {exc}")
                pending["result"] = {
                    "ok": True,
                    "screenshot": image_path,
                    "scoreboard": scoreboard,
                }
            elif action == "restart":
                restarted = _restart_farm_job("Discord 遠端重啟")
                pending["result"] = {
                    "ok": restarted,
                    "message": "已透過 Minescript 啟動新 Farm.py job，舊 job 正在收尾" if restarted else "重啟未執行，請查看 Farm_activity.log",
                }
                pending["event"].set()
                return restarted
            else:
                pending["result"] = {"ok": False, "error": "unknown action"}
        except Exception as exc:
            pending["result"] = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
        finally:
            pending["event"].set()


def 記錄運行結果(reason, elapsed_seconds, planned_deadline=None):
    ended_at = time.time()
    planned_deadline = _run_deadline_at if planned_deadline is None else planned_deadline
    elapsed_hours, remainder = divmod(max(0.0, elapsed_seconds), 3600)
    elapsed_minutes, elapsed_remainder = divmod(remainder, 60)
    record = (
        f"開始：{time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(_run_started_at or ended_at))}\n"
        f"結束：{time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(ended_at))}\n"
        f"運行時間：{int(elapsed_hours)} 小時 {int(elapsed_minutes)} 分 {elapsed_remainder:.1f} 秒\n"
        f"結束原因：{reason}\n"
        f"預計總時數：{((planned_deadline or ended_at) - (_run_started_at or ended_at)) / 3600:.2f}\n\n"
    )
    try:
        with open(FARM_RUNTIME_LOG, "a", encoding="utf-8") as log_file:
            log_file.write(record)
        echo(f"運行日誌已寫入：{FARM_RUNTIME_LOG}")
    except Exception as exc:
        echo(f"⚠️ 寫入運行日誌失敗：{exc}")


def 結束本輪(reason, elapsed_seconds, logout):
    global 開關, _run_deadline_at

    if _run_finished.is_set():
        return
    planned_deadline = _run_deadline_at
    _run_finished.set()
    開關 = False
    _lifecycle["restart_f8"] = False
    _lifecycle["auto_restart_at"] = None
    _lifecycle["run_deadline_at"] = None
    _run_deadline_at = None
    _write_watchdog_state(f8=False, run_started_at=_run_started_at, run_deadline_at=None, auto_restart_at=None)
    _stop_event.set()

    with action_lock:
        停止移動(f"運行結束：{reason}")
        if logout:
            time.sleep(random.uniform(0.2, 0.35))
            execute("/l")
            echo("已執行 /l，準備結束 Farm.py")

    記錄運行結果(reason, elapsed_seconds, planned_deadline)


def 運行計時監控():
    echo(f"運行計時監控啟動：初始設定 {FARM_DURATION_HOURS:g} 小時，可由 Discord 加時")

    while not _stop_event.is_set():
        if _run_started_at is None:
            _stop_event.wait(0.5)
            continue

        elapsed = time.time() - _run_started_at
        if _run_deadline_at is not None and time.time() >= _run_deadline_at:
            結束本輪("達到設定運行時數", elapsed, logout=True)
            return

        _stop_event.wait(0.5)


def 自動重啟監控():
    echo("定時自重啟監控啟動：F8 開啟期間每 3–5 分鐘隨機重啟")
    while not _stop_event.is_set():
        if not 開關:
            _stop_event.wait(0.5)
            continue

        if not _lifecycle.get("auto_restart_at"):
            更新自重啟排程()

        remaining = _lifecycle["auto_restart_at"] - time.time()
        if remaining > 0:
            _stop_event.wait(min(remaining, 1.0))
            continue

        if _restart_farm_job("定時自重啟", force_f8=True, schedule_next=True):
            return
        _lifecycle["auto_restart_at"] = time.time() + 60.0
        echo("§e定時重啟未能啟動，將於 60 秒後重試")


def Python記憶體維護():
    while not _stop_event.wait(300.0):
        reclaimed = gc.collect()
        echo(f"Python 定期回收完成：{reclaimed} 個循環物件")


# ─────────────────────────────────────────────
# 衣櫥：FARM = Rose Dragon + 財富；PEST = Mosquito + 生蟲
# ─────────────────────────────────────────────
def 換衣櫥(目標, 移動已停止=False):
    global 裝備套裝

    if 目標 not in ("FARM", "PEST"):
        return False

    目標狀態 = 目標 == "PEST"

    was_farming = 開關 and 農業中 and not 害蟲處理中 and not anti_fail_hold.is_set()

    # Even when the caller already changed the farming flag (pest flow), release
    # attack before opening the loadout GUI. GUI opening while attack is held can
    # leave Minecraft's attack state stuck after the GUI closes.
    if not 移動已停止:
        停止移動(f"切換衣櫥 {目標}")
    else:
        with _attack_input_lock:
            _farm_attack_monitor_active.clear()
            左鍵(False)
            flush()

    if _stop_event.is_set() or anti_fail_hold.is_set():
        return False

    gui_may_be_open = False
    try:
        if not 開啟介面指令("/loadout"):
            return False
        gui_may_be_open = True
        deadline = time.monotonic() + 12.0
        目標槽位 = None
        while not _stop_event.is_set() and not anti_fail_hold.is_set() and time.monotonic() < deadline:
            slots = get_all_slots(timeout=0.5)
            button = next(
                (
                    slot for slot in slots
                    if slot.get("count", 0) > 0
                    and str(slot.get("name", "")).strip().casefold() == 目標.casefold()
                ),
                None,
            )
            if button is not None:
                目標槽位 = button["slot"]
                break
            等(0.15)

        if 目標槽位 is None:
            echo(f"⚠️ /loadout 介面中找不到「{目標}」按鈕")
            return False

        echo(f"衣櫥點擊 {目標}，依最後一次成功送出的按鈕記錄套裝")
        if not click(目標槽位):
            echo(f"⚠️ 衣櫥「{目標}」按鈕點擊失敗")
            return False

        # The clicked wardrobe button represents the complete loadout.
        裝備套裝 = 目標狀態
        _lifecycle["loadout"] = 目標狀態
        _write_watchdog_state(loadout=目標狀態, loadout_at=time.time())
        echo(f"✅ 衣櫥 {目標} 點擊已送出，套裝狀態記為 {目標}")
        return True
    finally:
        # Always close the wardrobe before any attack input is restored.
        if gui_may_be_open:
            try:
                if not close_gui(resume_attack=was_farming):
                    echo("§e衣櫥介面關閉呼叫未成功")
            except Exception as exc:
                echo(f"§c關閉衣櫥介面失敗：{type(exc).__name__}: {exc}")


# ─────────────────────────────────────────────
# 傳回農田初始點
# ─────────────────────────────────────────────
def 傳回農田初始點():
    global 農業中, 方向

    # 如果除蟲已經接手，不允許這個動作排隊進來搶按鍵。
    if _stop_event.is_set() or 害蟲處理中 or anti_fail_hold.is_set() or not 開關:
        return

    農業中 = False

    with action_lock:
        # 取得鎖後再檢查一次，避免 crop_monitor 與 pest_monitor 發生競速。
        if _stop_event.is_set() or 害蟲處理中 or not 開關 or anti_fail_hold.is_set():
            return

        echo("🔒 傳回初始點取得行動鎖")
        停止移動("傳回初始點開始")

        回程目標 = dict(_return_target)  # 本次回程固定使用同一組設定
        目標Z = 回程目標["z"]
        到位誤差 = 0.1
        每輪AOTV次數 = 3
        最多回位輪數 = 3

        def 目前Z():
            try:
                return float(player_position()[2])
            except (TypeError, ValueError, IndexError, KeyError):
                return None

        def 已離開Garden(階段):
            """被傳到 Hub／伺服器重啟時立刻放棄回位流程並釋放 action_lock，
            讓「回Garden監控」能取得鎖去 /warp garden。"""
            global 農業中
            if _test_mode_flag or (not _evacuating() and 在花園()):
                return False
            echo(f"§e[回農田] {階段}：偵測到已不在 Garden，中止回位並交給 Garden 返回監控")
            停止移動("回農田流程偵測到離開 Garden")
            _release_all()
            if 開關 and not 害蟲處理中 and not anti_fail_hold.is_set() and not _stop_event.is_set():
                農業中 = True   # 回到 Garden 後由作物偵測重新判斷是否需要回位
            return True

        z = 目前Z()
        已抵達 = z is not None and abs(z - 目標Z) <= 到位誤差

        for 回位輪次 in range(最多回位輪數):
            if 已抵達:
                break
            if _stop_event.is_set() or not 開關 or 害蟲處理中 or anti_fail_hold.is_set():
                停止移動("傳回初始點中止")
                return

            if 已離開Garden("回位輪次開始"):
                return
            echo(f"回農田原點：第 {回位輪次 + 1}/{最多回位輪數} 輪，先傳送至 Plot {回程目標['plot']}")
            _release_all()
            execute(f"/tptoplot {回程目標['plot']}")
            等(1.0)

            z = 目前Z()
            if z is not None:
                echo(f"/tptoplot 後位置 Z={z:.3f}")
                已抵達 = abs(z - 目標Z) <= 到位誤差
            if 已抵達:
                break

            for 本輪嘗試 in range(每輪AOTV次數):
                if _stop_event.is_set() or not 開關 or 害蟲處理中 or anti_fail_hold.is_set():
                    停止移動("傳回初始點中止")
                    return

                if 已離開Garden("AOTV 前"):
                    return
                echo(f"回農田原點 AOTV：第 {本輪嘗試 + 1}/{每輪AOTV次數} 次")
                停止移動("傳回初始點準備使用 AOTV")
                蹲下(True)
                if not 安全切換欄位(AOTV):
                    _release_all()
                    echo("§eAOTV 欄位切換失敗，本輪重新傳送")
                    break
                _release_all()

                if 回程目標.get("block"):
                    # 掃描周圍方塊，瞄準最近且準心真的打得到的同類型方塊
                    看點成功 = look_at_the_block(
                        回程目標["block"], reach=float(回程目標["radius"]),
                        exact=True, max_tries=8, timeout=6.0,
                    )
                    if not 看點成功:
                        echo(f"§e半徑 {回程目標['radius']} 格內找不到可瞄準的 {回程目標['block']}")
                else:
                    看點成功 = look_at_block(random.randint(回程目標["x_min"], 回程目標["x_max"]), 回程目標["y"], 目標Z)
                if not 看點成功:
                    _release_all()
                    echo("§e回原點目標無法確認，取消本次 AOTV，稍後重新傳送")
                    continue

                等(0.3)
                右鍵(True)
                等(0.15)
                _release_all()

                等(0.5)
                z = 目前Z()
                if z is None:
                    echo("§e回原點後位置讀取失敗，列為本次未抵達")
                    continue

                echo(f"AOTV 後位置 Z={z:.3f}")
                已抵達 = abs(z - 目標Z) <= 到位誤差
                if 已抵達:
                    break

            if not 已抵達 and 回位輪次 + 1 < 最多回位輪數:
                echo(f"§e連續 3 次 AOTV 未到原點，重新 /tptoplot {回程目標['plot']} 後再試")

        if not 已抵達:
            停止移動("回農田原點多輪失敗")
            啟動防故障暫停("回農田原點連續 3 輪未抵達，已停止 AOTV")
            return

        停止移動("傳回初始點完成位移")

        # 使用安全的視角糾正物品校正朝向，並設逾時避免卡在迴圈。
        校正成功 = False
        for _校正次 in range(ANTI_FAIL_CORRECT_MAX_FAILS):
            if _stop_event.is_set() or not 開關 or 害蟲處理中 or anti_fail_hold.is_set():
                停止移動("回農田視角校正中止")
                return
            if 校正農田視角():
                校正成功 = True
                break
            if 已離開Garden("視角校正"):
                return
            echo(f"§e回農田視角校正失敗（{_校正次 + 1}/{ANTI_FAIL_CORRECT_MAX_FAILS}），重試")
        if not 校正成功:
            停止移動("回農田視角校正失敗")
            啟動防故障暫停("回農田後視角連續校正失敗，已停止以免低頭挖土")
            return

        停止移動("回農田流程完成校正")
        echo("傳回初始點完成")

        _set_direction(False)

        # 回到農田後短暫蹲一下，再恢復農業。
        蹲下(True)
        等(0.2)
        _release_all()
        等(0.5)

        農業中 = True
        停止移動("回農田蹲下動作結束")

# ─────────────────────────────────────────────
# Thread 1：挖田移動
# 每個完整移動／卡住判斷週期都持有 action_lock，避免背景流程中途釋放農田按鍵。
# ─────────────────────────────────────────────
def farming_move():
    global 方向

    echo("挖田移動 Thread 啟動")

    while not _stop_event.is_set():
        if not 開關 or not 農業中 or 害蟲處理中 or anti_fail_hold.is_set() or not 在花園():
            # 背景待機時不碰按鍵，避免與其他控制流程競爭。
            等(0.2)
            continue

        with action_lock:
            if not 開關 or not 農業中 or 害蟲處理中 or anti_fail_hold.is_set() or not 在花園():
                continue

            if get_main_hand_item().get("slot") != HOE:
                安全切換欄位(HOE)
                等(0.1)

            # 選欄位後再次確認主手；讀取失敗或仍不是鋤頭時絕不攻擊。
            if get_main_hand_item().get("slot") != HOE:
                _farm_attack_monitor_active.clear()
                左鍵(False)
                等(0.2)
                continue

            if not 開關 or not 農業中 or 害蟲處理中 or anti_fail_hold.is_set():
                continue

            if not 農田朝向正確():
                echo("§e[挖田] 視角 yaw/pitch 不正確，暫不挖掘，等待視角糾正")
                等(0.3)
                continue

            左鍵(True)
            flush()
            _farm_attack_monitor_active.set()
            w(True)

            if not 方向:
                d(False)
                a(True)
            else:
                a(False)
                d(True)

            # The monitor only performs its scheduled release-and-press refresh;
            # it does not resend attack continuously.
            move_deadline = time.monotonic() + random.uniform(1.8, 2.2)
            next_hoe_check = time.monotonic() + random.uniform(0.55, 0.8)
            hoe_lost = False
            while not _stop_event.is_set() and time.monotonic() < move_deadline:
                if not 開關 or not 農業中 or 害蟲處理中 or anti_fail_hold.is_set():
                    停止移動("農業狀態中止移動")
                    break

                now = time.monotonic()
                if now >= next_hoe_check:
                    hand = get_main_hand_item(timeout=0.6)
                    if not isinstance(hand, dict) or hand.get("slot") != HOE:
                        停止移動("挖田途中主手不再是 HOE")
                        echo("§c主手不是 HOE 或讀取失敗，停止移動以免空走或破壞作物")
                        hoe_lost = True
                        break
                    if not 農田朝向正確():
                        停止移動("挖田途中視角偏離")
                        echo("§c視角 yaw/pitch 偏離，停止挖掘等待糾正")
                        hoe_lost = True
                        break
                    next_hoe_check = now + random.uniform(0.55, 0.8)

                remaining = move_deadline - time.monotonic()
                if remaining > 0:
                    _stop_event.wait(min(random.uniform(0.25, 0.4), remaining))

            if hoe_lost:
                continue

            if _stop_event.is_set() or not 開關 or not 農業中 or 害蟲處理中 or anti_fail_hold.is_set():
                停止移動("農業狀態中止移動")
                continue

            # 每個移動週期都直接依左右方塊決定方向，不以位移或暫停作為判斷條件。
            if not 害蟲處理中:
                if not 開關 or not 農業中 or 害蟲處理中 or anti_fail_hold.is_set():
                    停止移動("卡住判斷時狀態中止")
                    continue

                sides = check_sides(timeout=1.0)
                left_open = sides.get("left")
                right_open = sides.get("right")

                if left_open is True and right_open is not True:
                    _set_direction(False)
                    echo("左側方塊可走，設定左向")
                elif right_open is True and left_open is not True:
                    _set_direction(True)
                    echo("右側方塊可走，設定右向")
                elif left_open is True and right_open is True:
                    echo("左右方塊都可走，維持目前方向")
                elif left_open is False and right_open is False:
                    echo("左右方塊都被擋住，維持目前方向")
                else:
                    echo("左右方塊狀態讀取失敗，維持目前方向")


# ─────────────────────────────────────────────
# Thread 2：作物偵測
# ─────────────────────────────────────────────
def crop_monitor():
    echo("作物偵測 Thread 啟動")
    無作物確認次數 = 0

    while not _stop_event.is_set():
        if not 開關 or not 農業中 or 害蟲處理中 or anti_fail_hold.is_set() or not 在花園():
            無作物確認次數 = 0
            等(0.5)
            continue

        if not check_front_crops(distance=6)["found"]:
            if _stop_event.is_set():
                return
            # 除蟲可能剛好在這一刻接手，因此呼叫前再確認。
            if 害蟲處理中 or not 農業中 or anti_fail_hold.is_set():
                無作物確認次數 = 0
                continue

            無作物確認次數 += 1
            echo(f"前方無作物確認 {無作物確認次數}/3")
            if 無作物確認次數 < 3:
                等(0.25)
                continue

            echo("連續 3 次確認前方沒有農作物，傳回農田初始點")
            傳回農田初始點()
            無作物確認次數 = 0
            等(0.2)
            continue

        無作物確認次數 = 0
        等(0.5)


# ─────────────────────────────────────────────
# Thread 3：害蟲偵測
# 高優先。
# 一旦發現害蟲，先把農業狀態關掉，再取得 action_lock。
# ─────────────────────────────────────────────
def pest_monitor():
    global 農業中, 害蟲處理中, 待處理害蟲_plot, _manual_pest_plot

    echo("害蟲偵測 Thread 啟動")

    while not _stop_event.is_set():
        if not 開關 or anti_fail_hold.is_set() or not 在花園() or not _feature_on("pest") or _reboot_imminent() or _evacuating():
            等(2)
            continue

        manual_plot = _manual_pest_plot
        if manual_plot is not None:
            _manual_pest_plot = None
            plots_str = str(manual_plot)      # Discord 手動除蟲（0 = 目前位置，測試用）
        elif _test_mode_flag:
            plots_str = ""                    # 單人存檔沒有 TAB 的 Plots 欄位
        else:
            plots_str = get_tablist_info_confirmed("Plots").get("Plots") or ""

        if plots_str:
            plot_text = plots_str.split(",")[0].strip()
            plot_match = re.search(r"\d+", plot_text)
            if not plot_match:
                echo(f"無法解析害蟲 Plot 編號：{plot_text!r}，略過本次除蟲")
                等(1.0)
                continue
            # PestCleaner 的 PLOT_CENTERS 使用 int key；傳入字串會讓回中心分支被跳過。
            plot = int(plot_match.group())

            # 暫停農業並通知裝備監控優先處理；此時不先搶 action_lock。
            待處理害蟲_plot = plot
            裝備優先完成.clear()
            害蟲處理中 = True
            農業中 = False

            echo(f"發現害蟲 Plot {plot}，先切換衣櫥套裝")

            # 除蟲時固定使用 FARM（財富／Rose Dragon）；不要等 READY 冷卻自行變動。
            # 換裝功能關閉時（例如測試模式）沒有人會設定 裝備優先完成，直接略過等待。
            while _feature_on("equipment") and not _stop_event.is_set():
                裝備優先完成.wait(PEST_COOLDOWN_POLL_SECONDS)
                if _stop_event.is_set():
                    return
                裝備優先完成.clear()
                if 裝備套裝 is False:
                    break

            if _stop_event.is_set():
                return

            echo(f"裝備處理完成，開始前往 Plot {plot} 除蟲")

            with action_lock:
                if _stop_event.is_set():
                    return
                echo(f"🔒 害蟲清除取得行動鎖 Plot {plot}")

                # 從這裡開始直到整個除蟲流程結束，
                # 沒有其他 Thread 可以控制按鍵。
                停止移動("害蟲除蟲開始")

                execute("/setspawn")
                等(0.5)

                PestCleaner().clean_plot(
                    plot_num=plot,
                    pest_count=4
                )

                if _stop_event.is_set():
                    停止移動("除蟲流程收到停止事件")
                    return

                停止移動("除蟲動作完成")

                execute("/warp garden")
                等(1.0)

                停止移動("返回 Garden 後")

                returned_plot = 確認所在Plot()
                if not _test_mode_flag and (not 在花園() or returned_plot is None):
                    待處理害蟲_plot = None
                    裝備優先完成.clear()
                    害蟲處理中 = False
                    啟動防故障暫停("除蟲返回後未能確認 Garden 與所在 Plot")
                    continue

                if not _test_mode_flag and not 校正農田視角():
                    待處理害蟲_plot = None
                    裝備優先完成.clear()
                    害蟲處理中 = False
                    啟動防故障暫停("除蟲返回後農田視角校正失敗")
                    continue

                蹲下(True)
                等(0.2)
                _release_all()
                等(0.5)

                echo("除蟲完成，已回到除蟲前 /setspawn 位置")

            # 完整流程結束後才交還農田控制權。
            待處理害蟲_plot = None
            裝備優先完成.clear()
            害蟲處理中 = False
            農業中 = True

        等(3.0)


# ─────────────────────────────────────────────
# Thread 4：裝備切換
# ─────────────────────────────────────────────
def equipment_monitor():
    global 裝備套裝, last_switch_time, 裝備切換目標

    echo("裝備切換 Thread 啟動")

    while not _stop_event.is_set():
        if not 開關 or anti_fail_hold.is_set() or not 在花園() or not _feature_on("equipment"):
            等(1.0)
            continue

        pest_work_active = 害蟲處理中 or 待處理害蟲_plot is not None
        if pest_work_active:
            # During pest cleanup always use the FARM wealth/Rose Dragon set,
            # including when the TAB cooldown still says READY.
            target_pest = False
        else:
            if last_switch_time and time.time() - last_switch_time < PEST_COOLDOWN_TAB_REFRESH_WAIT:
                _stop_event.wait(PEST_COOLDOWN_TAB_REFRESH_WAIT - (time.time() - last_switch_time))
                continue

            cooldown_str = get_tablist_info_confirmed("pest_cooldown").get("pest_cooldown") or ""
            if not cooldown_str:
                _stop_event.wait(PEST_COOLDOWN_POLL_SECONDS)
                continue

            if 裝備切換目標 is not None:
                # Outside pest handling, finish an interrupted wardrobe target first.
                target_pest = 裝備切換目標
                echo(f"裝備組切換尚未完成，沿用目標：{'生蟲' if target_pest else '財富'}")
            elif cooldown_str == "READY":
                # READY 代表可以啟動生蟲組。
                target_pest = True
            else:
                m_match = re.search(r"(\d+)m", cooldown_str)
                s_match = re.search(r"(\d+)s", cooldown_str)

                if not m_match and not s_match:
                    echo(f"無法解析害蟲冷卻「{cooldown_str}」，暫不切換裝備")
                    _stop_event.wait(PEST_COOLDOWN_POLL_SECONDS)
                    continue

                minutes = int(m_match.group(1)) if m_match else 0
                seconds = int(s_match.group(1)) if s_match else 0
                current_seconds = minutes * 60 + seconds

                # FARM baseline stays raw; PEST baseline keeps the existing correction.
                cooldown_baseline = current_seconds + (PEST_COOLDOWN_CORRECTION if 裝備套裝 is True else 0)
                target_pest = cooldown_baseline <= PEST_COOLDOWN_CORRECTION
                echo(
                    f"冷卻: {current_seconds}s | 校正基準: {cooldown_baseline}s "
                    f"(PEST 校正 +{PEST_COOLDOWN_CORRECTION}) | PEST 門檻: "
                    f"≤{PEST_COOLDOWN_CORRECTION}s | 目前套裝: "
                    f"{'PEST' if 裝備套裝 is True else 'FARM' if 裝備套裝 is False else '未知'} "
                    f"| 判定目標: {'PEST' if target_pest else 'FARM'}"
                )

        need_loadout = 裝備套裝 != target_pest

        if not need_loadout:
            if 裝備切換目標 == target_pest:
                裝備切換目標 = None
            if 待處理害蟲_plot is not None:
                裝備優先完成.set()
            if pest_work_active:
                _stop_event.wait(0.2)
            else:
                _stop_event.wait(PEST_COOLDOWN_POLL_SECONDS)
            continue

        with action_lock:
            if _stop_event.is_set():
                return
            echo("🔒 衣櫥套裝切換取得行動鎖")
            裝備切換目標 = target_pest
            success = 換衣櫥(
                "PEST" if target_pest else "FARM",
                移動已停止=(害蟲處理中 or not 農業中),
            )

            if success:
                echo("✅ 衣櫥切換完成，已記錄最後點擊的套裝")
                裝備切換目標 = None
                last_switch_time = time.time()
                if 待處理害蟲_plot is not None:
                    裝備優先完成.set()
            else:
                echo("⚠️ 裝備切換未完全成功，保持重試")
                last_switch_time = 0

        if success:
            _stop_event.wait(PEST_COOLDOWN_POLL_SECONDS)
        else:
            _stop_event.wait(1.0)


# ─────────────────────────────────────────────
# Thread 5：背包賣出
# ─────────────────────────────────────────────
def 合成地獄疙瘩():
    """依序批量合成 MUTANT_NETHER_STALK 與 ENCHANTED_NETHER_STALK。"""
    return 合成作物(CROP_COMPACT_CONFIG[0]["recipes"])


def 合成作物(recipes):
    """依序（高階 → 低階）合成；recipes 為 (配方ID, 原料名稱) 清單。

    每個配方開介面前先確認背包原料 >= 160，不足就跳過，不白開 /viewrecipe。
    """
    for recipe, ingredient in recipes:
        if _stop_event.is_set():
            return False

        if _crop_recipe_cooling_down(recipe):
            continue

        have = _count_inventory_item(get_all_slots(timeout=0.8), ingredient)
        if have < CROP_COMPACT_INGREDIENT_COUNT:
            echo(f"略過 {recipe}：{ingredient} 只有 {have} 個（需要 {CROP_COMPACT_INGREDIENT_COUNT}）")
            continue

        echo(f"開始批量合成：{recipe}")
        開啟介面指令(f"/viewrecipe {recipe}")

        deadline = time.monotonic() + 15.0
        supercraft_slot = None
        while not _stop_event.is_set() and time.monotonic() < deadline:
            slots = get_all_slots(timeout=0.5)
            supercraft = next(
                (
                    slot for slot in slots
                    if slot.get("count", 0) > 0
                    and str(slot.get("name", "")).strip().casefold().startswith("supercraft")
                ),
                None,
            )
            if supercraft is not None:
                supercraft_slot = supercraft["slot"]
                echo(f"找到批量合成按鈕：{supercraft.get('name', '')}（槽位 {supercraft_slot}）")
                break
            等(0.15)

        if supercraft_slot is None:
            echo(
                f"⚠️ 配方 {recipe} 介面中找不到 Supercraft（ID 可能無效或配方未解鎖），"
                f"{int(CROP_RECIPE_FAIL_COOLDOWN_SECONDS)} 秒內不再嘗試此配方，繼續下一個"
            )
            _crop_recipe_fail_until[recipe] = time.monotonic() + CROP_RECIPE_FAIL_COOLDOWN_SECONDS
            _save_crop_recipe_cooldowns()
            close_gui(resume_attack=False)
            continue

        # 使用 GUI 的 Shift-click（QUICK_MOVE）切換 Supercraft 數量，不讓玩家角色蹲下。
        toggle_ok = click(supercraft_slot, shift=True)
        if not toggle_ok:
            echo(f"⚠️ 配方 {recipe} 切換批量模式失敗")
            close_gui(resume_attack=False)
            return False

        # 批量模式按鈕仍在剛掃描到的槽位；用同一槽位立刻再點，不重掃拖長間隔。
        if not click(supercraft_slot):
            echo(f"⚠️ 配方 {recipe} 第二次 Supercraft 點擊未送出；合成未執行")
            close_gui(resume_attack=False)
            return False

        close_gui(resume_attack=False)
        echo(f"配方 {recipe} 第二次 Supercraft 點擊已送出；未驗證合成結果")

    return True


def inventory_sell_monitor():
    echo("背包賣出 Thread 啟動")

    def scan_gui_item(item_name, slots=None):
        """以目前開啟的 GUI 快照找物品，接受顯示名稱前後附加文字。"""
        wanted = str(item_name).casefold()
        if slots is None:
            slots = get_all_slots(timeout=0.5)
        for slot in slots:
            # 商店 0–53 是商品區，賣出後出現在那裡的項目可供反悔，不能當成玩家背包。
            if slot.get("slot", -1) < BOOSTERCOOKIE_PLAYER_SLOT_START:
                continue
            if slot.get("count", 0) <= 0:
                continue
            visible_name = str(slot.get("name", "")).casefold()
            visible_id = str(slot.get("id", "")).casefold()
            if wanted in visible_name or wanted in visible_id:
                return slot, slots
        return None, slots

    def wait_for_shop(timeout=20.0):
        deadline = time.monotonic() + timeout
        while not _stop_event.is_set() and time.monotonic() < deadline:
            slots = get_all_slots(timeout=0.5)
            if len(slots) > 46:
                return slots
            等(0.15)
        return None

    def find_gui_button(label, slots):
        wanted = label.casefold()
        return next(
            (
                slot for slot in slots
                if slot.get("count", 0) > 0
                and (
                    wanted in str(slot.get("name", "")).casefold()
                    or wanted in str(slot.get("id", "")).casefold()
                )
            ),
            None,
        )

    def wait_for_gui_button(label, timeout=15.0):
        deadline = time.monotonic() + timeout
        while not _stop_event.is_set() and time.monotonic() < deadline:
            slots = get_all_slots(timeout=0.5)
            button = find_gui_button(label, slots)
            if button is not None:
                return button
            等(0.15)
        return None

    while not _stop_event.is_set():
        if not 開關 or anti_fail_hold.is_set() or not _feature_on("sell"):
            等(10)
            continue

        # 只有 /boostercookie 支援這裡的開啟商店後點物品出售流程。
        inventory_slots = []
        for _ in range(3):
            if _stop_event.is_set():
                return
            inventory_slots = get_all_slots(timeout=0.8)
            if inventory_slots:
                break
            等(0.2)

        total = 0
        found_items = []
        for name in BOOSTERCOOKIE_SELL_ITEMS:
            wanted = name.casefold()
            matching_slots = [
                slot for slot in inventory_slots
                if slot.get("count", 0) > 0
                and (wanted in str(slot.get("name", "")).casefold()
                     or wanted in str(slot.get("id", "")).casefold())
            ]
            if matching_slots:
                found_items.append(name)
                total += sum(slot["count"] for slot in matching_slots)

        bazaar_counts = {}
        for name, threshold in BAZAAR_SELL_THRESHOLDS.items():
            wanted = name.casefold()
            count = sum(
                slot.get("count", 0)
                for slot in inventory_slots
                if slot.get("count", 0) > 0
                and (
                    wanted in str(slot.get("name", "")).casefold()
                    or wanted in str(slot.get("id", "")).casefold()
                )
            )
            if count >= threshold:
                bazaar_counts[name] = count

        crop_to_compact = None
        crop_compact_count = 0
        for crop_config in CROP_COMPACT_CONFIG:
            score = _craftable_crop_score(crop_config, inventory_slots)
            if score > crop_compact_count:
                crop_to_compact, crop_compact_count = crop_config, score

        if crop_to_compact is not None:
            with action_lock:
                if _stop_event.is_set():
                    return
                echo(f"背包 {crop_to_compact['name']} 有足夠原料可壓縮（可合成原料共 {crop_compact_count} 個），開始壓縮")
                停止移動(f"{crop_to_compact['name']} 批量合成開始")
                合成作物(crop_to_compact["recipes"])
                close_gui(resume_attack=False)

                hand = get_main_hand_item()
                if (
                    not _stop_event.is_set()
                    and 開關
                    and 農業中
                    and not 害蟲處理中
                    and isinstance(hand, dict)
                    and hand.get("slot") == HOE
                    and 農田朝向正確()
                ):
                    左鍵(True)
                    flush()
                    echo("✅ 合成流程結束，已恢復鋤頭攻擊")

            等(0.5)
            continue

        if bazaar_counts:
            bazaar_sale_sent = False
            with action_lock:
                if _stop_event.is_set():
                    return
                echo(f"Bazaar 物品達標：{bazaar_counts}；準備整包出售")
                停止移動("Bazaar 整包出售開始")
                try:
                    開啟介面指令("/bz")
                    sell_button = wait_for_gui_button("Sell Inventory Now")
                    if sell_button is None:
                        echo("⚠️ Bazaar 介面中找不到 Sell Inventory Now")
                    elif not click(sell_button["slot"]):
                        echo("⚠️ Bazaar Sell Inventory Now 點擊未送出")
                    else:
                        confirm_button = wait_for_gui_button("Selling whole inventory")
                        if confirm_button is None:
                            echo("⚠️ Bazaar 介面中找不到 Selling whole inventory 確認按鈕")
                        elif not click(confirm_button["slot"]):
                            echo("⚠️ Bazaar Selling whole inventory 確認點擊未送出")
                        else:
                            bazaar_sale_sent = True
                            echo("Bazaar 整包出售確認點擊已送出")
                finally:
                    close_gui(resume_attack=False)
                    flush()

                    hand = get_main_hand_item()
                    if (
                        not _stop_event.is_set()
                        and 開關
                        and 農業中
                        and not 害蟲處理中
                        and isinstance(hand, dict)
                        and hand.get("slot") == HOE
                        and 農田朝向正確()
                    ):
                        左鍵(True)
                        flush()
                        echo("✅ Bazaar 流程結束，已恢復鋤頭攻擊")

            等(random.uniform(0.35, 0.65) if bazaar_sale_sent else 5.0)
            continue

        if total <= BOOSTERCOOKIE_SELL_THRESHOLD:
            found_items.clear()
            等(15.0)
            continue

        echo(f"背包可賣物品合計 {total} 個，超過 {BOOSTERCOOKIE_SELL_THRESHOLD}，準備開啟 Booster Cookie 商店")

        sell_click_sent = False
        with action_lock:
            if _stop_event.is_set():
                return
            echo("🔒 Booster Cookie 賣出取得行動鎖")
            停止移動("背包賣出開始")
            開啟介面指令("/boostercookie")

            # 商店開啟及內容載入時間不固定；逐項重掃，不能只依靠第一次快照。
            slots = wait_for_shop()
            if _stop_event.is_set():
                return

            if slots is None:
                echo("⚠️ 執行 /boostercookie 後 20 秒內沒有偵測到商店介面，保留物品並稍後重試")
            else:
                echo(f"✅ 已偵測到介面（槽位數 {len(slots)}），逐一重新掃描目標物品")
                找到目標物品 = False
                for name in found_items:
                    if _stop_event.is_set():
                        close_gui(resume_attack=False)
                        return
                    item_deadline = time.monotonic() + 3.0
                    item_clicked = False
                    while not _stop_event.is_set():
                        current_slots = get_all_slots(timeout=0.5)
                        if len(current_slots) <= 46:
                            if time.monotonic() < item_deadline:
                                # 單次空掃描可能是伺服器更新延遲；留在同一商店等畫面恢復。
                                等(0.2)
                                continue
                            echo(f"⚠️ 等待後商店仍無有效槽位，停止本輪，不重新開關商店（物品：{name}）")
                            break

                        current, _ = scan_gui_item(name, current_slots)
                        if current is None:
                            if time.monotonic() < item_deadline:
                                等(0.25)
                                continue
                            echo(f"⚠️ 等待載入後仍掃描不到「{name}」，繼續檢查其他物品")
                            break

                        before_count = current["count"]
                        找到目標物品 = True
                        slot_index = current["slot"]
                        echo(f"找到「{name}」：槽位 {slot_index}，數量 {before_count}；使用本次 GUI 快照槽位點擊")
                        if not click(slot_index):
                            echo(f"⚠️ 點擊槽位 {slot_index} 失敗，跳過此物品")
                            break
                        item_clicked = True
                        sell_click_sent = True
                        # 點擊節奏由 BAG.click 統一控制；這裡重掃直到確認伺服器已更新數量。
                        change_deadline = time.monotonic() + 1.5
                        changed = False
                        updated_slots = current_slots
                        updated = current
                        while not _stop_event.is_set() and time.monotonic() < change_deadline:
                            updated_slots = get_all_slots(timeout=0.35)
                            if len(updated_slots) <= 46:
                                changed = True
                                break
                            updated, _ = scan_gui_item(name, updated_slots)
                            if updated is None or updated.get("count", 0) < before_count:
                                changed = True
                                break
                            等(0.08)

                        if not changed:
                            echo(f"⚠️ 點擊「{name}」後數量未變，避免重複點同一槽位，改處理下一項")
                            break
                        if len(updated_slots) <= 46 or updated is None:
                            break
                        if updated.get("count", 0) >= before_count:
                            break

                    if not item_clicked:
                        echo(f"本輪未能點擊「{name}」")

                if not 找到目標物品:
                    echo("⚠️ 商店已開啟，但介面掃描不到任何待售物品；不執行盲點擊")
                    debug_slots = get_all_slots(timeout=1.0)
                    occupied = [
                        f"{slot['slot']}:{slot['name']}×{slot['count']}"
                        for slot in debug_slots
                        if slot.get("count", 0) > 0
                    ]
                    echo("商店目前非空槽位：" + (" | ".join(occupied[:24]) or "無"))
                close_gui(resume_attack=False)
                flush()

            # 關閉 GUI 後由農田流程接手移動；先明確恢復鋤頭攻擊，避免只釋放不重新按住。
            hand = get_main_hand_item()
            if (
                not _stop_event.is_set()
                and 開關
                and 農業中
                and not 害蟲處理中
                and isinstance(hand, dict)
                and hand.get("slot") == HOE
                and 農田朝向正確()
            ):
                左鍵(True)
                flush()
                echo("✅ 商店流程結束，已恢復鋤頭攻擊；農田移動流程接續移動")

        found_items.clear()
        # 成功送出賣出點擊後立即回掃剩餘物品；只有下輪確認無物品才長待機。
        等(random.uniform(0.35, 0.65) if sell_click_sent else 10.0)
# ─────────────────────────────────────────────
# Thread 6：Plots 噴霧監控
# ─────────────────────────────────────────────
def spray_已失效(spray_text):
    if spray_text is None:
        return False
    text = str(spray_text).strip()
    if text.lower() == "none":
        return True
    if not text:
        return False
    # 有倒數視為仍有效；不認得的內容不冒然消耗噴霧。
    if re.search(r"\d+\s*[hms]\b", text, flags=re.IGNORECASE):
        return False
    return False


def 噴霧位置安全():
    if anti_fail_hold.is_set() or not 開關:
        return False
    area = get_tablist_info_confirmed("area").get("area")
    if area != "Garden" or not 農田朝向正確():
        return False
    return 確認所在Plot() is not None


def spray_monitor():
    echo("Plot 噴霧監控 Thread 啟動")
    last_attempt = 0.0

    while not _stop_event.is_set():
        if not 開關 or anti_fail_hold.is_set() or not 在花園() or not _feature_on("spray"):
            等(5.0)
            continue

        spray_text = get_tablist_info_confirmed("spray").get("spray")
        if not spray_已失效(spray_text):
            等(20.0)
            continue

        if not 噴霧位置安全():
            echo("§e[Anti-Fail] 無法確認仍在 Garden 的有效 Plot，本輪略過噴霧")
            等(3.0)
            continue

        # 避免伺服器狀態更新延遲時重複使用噴霧。
        if time.time() - last_attempt < 20.0:
            等(3.0)
            continue

        with action_lock:
            if _stop_event.is_set() or not 噴霧位置安全():
                echo("§e[Anti-Fail] 噴霧操作前位置確認失敗，取消使用")
                等(2.0)
                continue

            echo("目前 plot 沒有有效噴霧，使用噴霧補充")
            停止移動("噴霧補充開始")
            安全切換欄位(噴霧)
            等(0.15)
            if _stop_event.is_set() or anti_fail_hold.is_set() or not 開關:
                右鍵(False)
                安全切換欄位(HOE)
                continue
            右鍵(True)
            等(0.12)
            右鍵(False)
            安全切換欄位(HOE)
            last_attempt = time.time()

        # 下一輪在鎖外讀狀態，讓農田作業可以繼續。
        等(5.0)


# ─────────────────────────────────────────────
# Thread 7：Bonus 藥水續費
# ─────────────────────────────────────────────
def 解析_bonus_剩餘秒數(bonus_text):
    """解析如 '+400 3m 40s' 的 Bonus；INACTIVE 回傳 0，未知格式回傳 None。"""
    text = str(bonus_text or "").strip()
    if not text:
        return None
    if "INACTIVE" in text.upper():
        return 0

    matches = re.findall(r"(\d+)\s*([hms])\b", text, flags=re.IGNORECASE)
    if not matches:
        return None

    seconds = 0
    for value, unit in matches:
        multiplier = {"h": 3600, "m": 60, "s": 1}[unit.lower()]
        seconds += int(value) * multiplier
    return seconds


def bonus_續費_monitor():
    echo("Bonus 藥水續費 Thread 啟動")

    while not _stop_event.is_set():
        if not 開關 or anti_fail_hold.is_set() or not 在花園() or not _feature_on("bonus"):
            等(5.0)
            continue

        tab_info = get_tablist_info_confirmed("bonus")
        bonus_text = tab_info.get("bonus") or ""
        del tab_info

        # 只有遊戲明確回報 INACTIVE 才續費；倒數即使顯示 0 秒也先不呼叫。
        if "INACTIVE" not in bonus_text.upper():
            等(10.0)
            continue

        # 二次讀取也在鎖外進行，這只是等狀態資料，不應讓農田停住。
        latest = get_tablist_info_confirmed("bonus").get("bonus") or ""
        if "INACTIVE" not in latest.upper():
            等(5.0)
            continue

        with action_lock:
            if _stop_event.is_set():
                return

            echo(f"Bonus 藥水已失效 ({latest or 'INACTIVE'})，呼叫 Philip 續費")
            停止移動("Bonus 藥水續費開始")
            開啟介面指令("/call philip")

            # Philip 接電話與開啟交易畫面需要不固定時間，定時尋找續費物品。
            deadline = time.time() + 60.0
            item = 0
            while not _stop_event.is_set() and time.time() < deadline:
                item = Find_Item("Empty Vacuum Bag", timeout=1.0)
                if isinstance(item, dict):
                    break
                等(0.7)

            if _stop_event.is_set():
                return

            if not isinstance(item, dict):
                echo("60 秒內找不到 Empty Vacuum Bag，稍後再檢查 Bonus")
                等(5.0)
                continue

            clicked = click(item["slot"])
            del item
            if not clicked:
                echo("點擊 Empty Vacuum Bag 失敗，稍後重試")
                等(5.0)
                continue

            # 點擊一次後等畫面更新，再確認 Bonus 已重新啟用。
            renewed = False
            confirm_deadline = time.time() + 12.0
            while not _stop_event.is_set() and time.time() < confirm_deadline:
                current_bonus = get_tablist_info_confirmed("bonus").get("bonus") or ""
                current_remaining = 解析_bonus_剩餘秒數(current_bonus)
                if "INACTIVE" not in current_bonus.upper() and current_remaining not in (None, 0):
                    renewed = True
                    break
                等(1.0)

            if renewed:
                echo("✅ Bonus 藥水續費完成")
            else:
                echo("⚠️ 已點擊 Empty Vacuum Bag，但尚未確認 Bonus 更新；稍後重新檢查")

        等(5.0)


# ─────────────────────────────────────────────
# 主函式
# ─────────────────────────────────────────────
def main():
    global 開關, _run_started_at, _run_deadline_at, _farm_heartbeat_at

    if _previous_lifecycle is not None:
        # 通知舊一輪放棄按鍵控制，並給它從可中斷等待中退出的時間。
        for thread in _previous_lifecycle.get("threads", []):
            if thread is not threading.current_thread():
                thread.join(timeout=1.0)

    if _stop_event.is_set():
        return

    if 開關:
        if _run_started_at is None:
            _start_run_timer_if_needed()
        更新自重啟排程()
        echo("新 Farm.py job 已恢復 F8 開啟狀態")
    else:
        _write_watchdog_state(f8=False, run_started_at=_run_started_at, run_deadline_at=_run_deadline_at)

    停止移動("主程式初始化")
    if not _sync_target_from_item(timeout=5.0):
        echo("§e[視角] 5 秒內讀不到糾正物品的 Selected Yaw/Pitch，暫用上次記錄值")
    echo(_view_settings_text())

    if _stop_event.is_set():
        return

    workers = [
        farming_move,
        farm_attack_monitor,
        crop_monitor,
        anti_fail_monitor,
        pest_monitor,
        回Garden監控,
        equipment_monitor,
        inventory_sell_monitor,
        spray_monitor,
        bonus_續費_monitor,
        運行計時監控,
        自動重啟監控,
        Python記憶體維護,
        重啟偵測,
    ]

    control_thread = threading.Thread(target=_discord_control_server, daemon=True)
    control_thread.start()
    _start_survival_watchdog()
    if 開關 and not _test_mode_flag and get_tablist_info_confirmed("area").get("area") != "Garden":
        回到Garden()
    _start_discord_bot()

    _lifecycle["threads"] = []
    for worker in workers:
        thread = threading.Thread(target=worker, daemon=True)
        _lifecycle["threads"].append(thread)
        thread.start()

    echo("主迴圈啟動，F8 開關")

    while not _stop_event.is_set():
        with EventQueue() as eq:
            eq.register_key_listener()

            while not _stop_event.is_set():
                try:
                    e = eq.get(timeout=0.5)
                except queue.Empty:
                    _farm_heartbeat_at = time.monotonic()
                    if _process_discord_requests():
                        return
                    continue

                _farm_heartbeat_at = time.monotonic()
                if _process_discord_requests():
                    return

                if (
                    e.type == EventType.KEY
                    and e.action == 0
                    and e.key == 297
                ):
                    開關 = not 開關

                    if 開關 and anti_fail_hold.is_set():
                        恢復防故障暫停()

                    if 開關 and _run_started_at is None:
                        _start_run_timer_if_needed()
                        echo(f"運行計時開始：設定 {FARM_DURATION_HOURS:g} 小時")

                    更新自重啟排程(reset=開關)

                    if not 開關:
                        停止移動("F8 關閉農田")

                    echo(
                        f"F8 切換: {'開' if 開關 else '關'}"
                    )

                    等()
                elif (
                    e.type == EventType.KEY
                    and e.action == 0
                    and e.key == 298
                ):
                    stop_farm_scripts_from_f9()
                    return

    # 若同一 runtime 中有其他執行緒仍處於非中斷區段，停止前清理農業按鍵。
    停止移動("主迴圈退出")


main()