"""
pest_utils.py — 常數、工具函式、Java bridge、獨立 API
"""

import socket
import queue as _queue
import minescript
import math
import time
import random
import threading
import re
import os
import json
from system.lib.java import eval_pyjinn_script as eps

# ─────────────────────────────────────────────
# 測試模式 / 蟲血量範圍（由 Discord 面板寫入 Farm_watchdog_state.json）
#   test_mode       : True 時視為「單人存檔測試」，不要求 Hypixel 的 plot / 計分板
#   pest_min_health : 蟲血量下限（預設 100，不含）
#   pest_max_health : 蟲血量上限（預設 2000，不含）
# 測試模式下若沒有自訂血量，預設接受任何 >0 的血量。
# ─────────────────────────────────────────────
try:
    _STATE_DIR = os.path.dirname(os.path.abspath(__file__))
except NameError:
    _STATE_DIR = os.getcwd()
_STATE_FILE = os.path.join(_STATE_DIR, "Farm_watchdog_state.json")
DEFAULT_PEST_MIN_HEALTH = 100.0
DEFAULT_PEST_MAX_HEALTH = 2000.0
_settings_cache = {"at": 0.0, "data": {}}


def _read_test_settings():
    now = time.monotonic()
    if now - _settings_cache["at"] < 1.0:
        return _settings_cache["data"]
    data = {}
    try:
        with open(_STATE_FILE, "r", encoding="utf-8") as stream:
            raw = json.load(stream)
        if isinstance(raw, dict):
            data = raw
    except (OSError, ValueError):
        pass
    _settings_cache["at"] = now
    _settings_cache["data"] = data
    return data


def pest_test_mode() -> bool:
    return bool(_read_test_settings().get("test_mode", False))


def pest_health_range():
    """回傳 (min, max)；判定為 min < health < max。"""
    data = _read_test_settings()
    test = bool(data.get("test_mode", False))
    default_min = 0.0 if test else DEFAULT_PEST_MIN_HEALTH
    default_max = 1_000_000_000.0 if test else DEFAULT_PEST_MAX_HEALTH
    try:
        low = float(data.get("pest_min_health", default_min))
        high = float(data.get("pest_max_health", default_max))
    except (TypeError, ValueError):
        low, high = default_min, default_max
    if high <= low:
        low, high = default_min, default_max
    return low, high

# ─────────────────────────────────────────────
# 常數
# ─────────────────────────────────────────────
PLAYER_EYE_HEIGHT   = 1.62
FLY_HEIGHT          = 78.0
ARRIVE_BOX          = 4.5
STUCK_TIMEOUT       = 3.0
PLOT_STUCK_SEC      = 15.0
NO_MOB_TIMEOUT      = 20.0
AOTE_SLOT           = 0
VACUUM_SLOT         = 1
AOTE_SHOT_MIN       = 9.0
AOTE_SHOT_MAX       = 10.0
AOTE_SMOOTH_STEPS   = 8
AIM_TICK_SEC        = 0.016
WANDER_BOX_YAW      = 30.0
WANDER_BOX_PITCH    = 20.0
WANDER_NOISE_Y      = 0.20
WANDER_NOISE_P      = 0.10
SPRING_K            = 85.0
SPRING_DAMP         = 19.0
WANDER_INTERVAL_MIN = 1.8
WANDER_INTERVAL_MAX = 4.5
JITTER_FREQ         = 1.8
FOV_THRESHOLD_DEG   = 110.0

_MIN_X, _MAX_X = -239, 239
_MIN_Z, _MAX_Z = -239, 239
_SZ   = (_MAX_X - _MIN_X) / 5
_HALF = _SZ / 2
_LAYOUT = [
    [21, 13,  9, 14, 22],
    [15,  5,  1,  6, 16],
    [10,  2, 25,  3, 11],
    [17,  7,  4,  8, 18],
    [23, 19, 12, 20, 24],
]
PLOT_CENTERS: dict = {}
for _r, _row in enumerate(_LAYOUT):
    for _c, _plot in enumerate(_row):
        PLOT_CENTERS[_plot] = (_MIN_X + _c * _SZ + _HALF, _MIN_Z + _r * _SZ + _HALF)

# ─────────────────────────────────────────────
# 工具函式
# ─────────────────────────────────────────────
def _clamp(val, lo, hi):
    return max(lo, min(hi, val))

def _angle_diff(a, b):
    return (b - a + 180) % 360 - 180

def _dist3(a, b):
    return math.sqrt((a[0]-b[0])**2 + (a[1]-b[1])**2 + (a[2]-b[2])**2)

def _perlin_1d(t, seed=0):
    t = t + seed * 1000.0
    i = int(t); f = t - i
    u = f * f * (3 - 2 * f)
    r0 = random.Random(i).uniform(-1, 1)
    r1 = random.Random(i + 1).uniform(-1, 1)
    return r0 + (r1 - r0) * u

def _calc_yaw_pitch(from_pos, to_pos, eye=PLAYER_EYE_HEIGHT):
    dx = to_pos[0] - from_pos[0]
    dy = to_pos[1] - (from_pos[1] + eye)
    dz = to_pos[2] - from_pos[2]
    yaw   = math.degrees(math.atan2(-dx, dz))
    pitch = math.degrees(math.atan2(-dy, math.sqrt(dx**2 + dz**2)))
    return yaw, pitch

def _in_fov(player_yaw, target_pos, player_pos):
    dx = target_pos[0] - player_pos[0]
    dz = target_pos[2] - player_pos[2]
    ty = math.degrees(math.atan2(-dx, dz))
    return abs(_angle_diff(player_yaw, ty)) <= FOV_THRESHOLD_DEG

def _strip_mc(text):
    return re.sub(r'§[0-9a-fk-orA-FK-OR]', '', text).strip()

def _release_all():
    for fn in [
        minescript.player_press_forward, minescript.player_press_backward,
        minescript.player_press_left,    minescript.player_press_right,
        minescript.player_press_jump,    minescript.player_press_sneak,
        minescript.player_press_attack,
        minescript.player_press_use,
    ]:
        try: fn(False)
        except: pass

# ─────────────────────────────────────────────
# 即時玩家狀態追蹤器
# ─────────────────────────────────────────────
_TRACKER_SCRIPT = r"""
Socket=JavaClass("java.net.Socket");PrintWriter=JavaClass("java.io.PrintWriter")
OutputStreamWriter=JavaClass("java.io.OutputStreamWriter")
StandardCharsets=JavaClass("java.nio.charset.StandardCharsets")
mc=JavaClass("net.minecraft.client.Minecraft").getInstance()

writer=None
try:
    s=Socket("127.0.0.1",__PORT__)
    writer=PrintWriter(OutputStreamWriter(s.getOutputStream(),StandardCharsets.UTF_8),True)
except Exception as e:
    pass

def on_tick(_):
    if not writer:return
    try:
        p=mc.player
        if p is None:return
        x=p.getX();y=p.getY();z=p.getZ()
        yaw=p.getYRot();pitch=p.getXRot()
        eye_h=float(p.getEyeHeight())
        try:sneaking=1 if p.isCrouching() else 0
        except:sneaking=0
        try:flying=1 if p.getAbilities().flying else 0
        except:flying=0
        writer.println(str(x)+","+str(y)+","+str(z)+","+str(yaw)+","+str(pitch)+","+str(eye_h)+","+str(sneaking)+","+str(flying))
        writer.flush()
    except:pass

add_event_listener("tick", on_tick)
"""

class PlayerStateTracker:
    """
    長連線即時追蹤玩家狀態，每 tick 從 Java 端推送。
    使用：
        tracker = PlayerStateTracker()
        tracker.start()
        state = tracker.get()  # {"x","y","z","yaw","pitch","eye_h","sneaking","flying"}
        tracker.stop()
    """
    def __init__(self):
        self._state = {
            "x": 0.0, "y": 0.0, "z": 0.0,
            "yaw": 0.0, "pitch": 0.0,
            "eye_h": 1.62,
            "sneaking": False,
            "flying": False,
        }
        self._lock    = threading.Lock()
        self._running = False
        self._srv     = None
        self._thread  = None

    def start(self):
        if self._running and self._thread is not None and self._thread.is_alive():
            return
        self._running = True
        self._srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._srv.bind(("127.0.0.1", 0))
        self._srv.listen(1)
        port = self._srv.getsockname()[1]
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()
        script = _TRACKER_SCRIPT.replace("__PORT__", str(port))
        eps(script)

    def _run(self):
        try:
            conn, _ = self._srv.accept()
            reader = conn.makefile(encoding="utf-8", mode="r")
            while self._running:
                line = reader.readline()
                if not line:
                    break
                parts = line.strip().split(",")
                if len(parts) != 8:
                    continue
                try:
                    with self._lock:
                        self._state["x"]        = float(parts[0])
                        self._state["y"]        = float(parts[1])
                        self._state["z"]        = float(parts[2])
                        self._state["yaw"]      = float(parts[3])
                        self._state["pitch"]    = float(parts[4])
                        self._state["eye_h"]    = float(parts[5])
                        self._state["sneaking"] = parts[6] == "1"
                        self._state["flying"]   = parts[7] == "1"
                except:
                    continue
            conn.close()
        except:
            pass
        finally:
            try: self._srv.close()
            except: pass

    def get(self) -> dict:
        with self._lock:
            return dict(self._state)

    def position(self) -> tuple:
        with self._lock:
            return (self._state["x"], self._state["y"], self._state["z"])

    def orientation(self) -> tuple:
        with self._lock:
            return (self._state["yaw"], self._state["pitch"])

    def eye_height(self) -> float:
        with self._lock:
            return self._state["eye_h"]

    def is_flying(self) -> bool:
        with self._lock:
            return self._state["flying"]

    def is_sneaking(self) -> bool:
        with self._lock:
            return self._state["sneaking"]

    def stop(self):
        self._running = False
        try: self._srv.close()
        except: pass


# 全域單例，import 後呼叫 player_tracker.start() 啟動
player_tracker = PlayerStateTracker()

# ─────────────────────────────────────────────
# Java bridge 通用封裝
# ─────────────────────────────────────────────
def _bridge_call(script_template, port_token="__PORT__", timeout=3.0):
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.bind(("127.0.0.1", 0))
    srv.listen(1)
    srv.settimeout(timeout)
    port = srv.getsockname()[1]
    rq   = _queue.Queue()
    conn_holder = {"conn": None}
    script = script_template.replace(port_token, str(port))

    def _recv():
        conn = None
        reader = None
        try:
            conn, _ = srv.accept()
            conn_holder["conn"] = conn
            conn.settimeout(timeout)
            reader = conn.makefile(encoding="utf-8", mode="r")
            line = reader.readline()
            if line: rq.put(line.strip())
        except Exception as e:
            rq.put(f"ERROR|||{e}")
        finally:
            if reader:
                try: reader.close()
                except: pass
            if conn:
                try: conn.close()
                except: pass
            try: srv.close()
            except: pass

    threading.Thread(target=_recv, daemon=True).start()
    try:
        eps(script)
        raw = rq.get(timeout=timeout)
        return None if raw.startswith("ERROR|||") else raw
    except _queue.Empty:
        return None
    finally:
        conn = conn_holder["conn"]
        if conn:
            try: conn.close()
            except: pass
        try: srv.close()
        except: pass

# ─────────────────────────────────────────────
# 飛行狀態（從 player_tracker 即時讀取）
# ─────────────────────────────────────────────
def is_flying(timeout=3.0):
    return player_tracker.is_flying()

# ─────────────────────────────────────────────
# 計分板(讀取方式改為與可用腳本相同)
# ─────────────────────────────────────────────
# 與可用腳本相同:判斷 team 用 `is not None`(原本是 `if team:`)
_SCOREBOARD_SCRIPT = r"""
Socket             = JavaClass("java.net.Socket")
PrintWriter        = JavaClass("java.io.PrintWriter")
OutputStreamWriter = JavaClass("java.io.OutputStreamWriter")
StandardCharsets   = JavaClass("java.nio.charset.StandardCharsets")
mc = JavaClass("net.minecraft.client.Minecraft").getInstance()

writer = None
try:
    bridge_socket = Socket("127.0.0.1", __PORT__)
    writer = PrintWriter(OutputStreamWriter(bridge_socket.getOutputStream(), StandardCharsets.UTF_8), True)
except Exception as e:
    pass

try:
    world = mc.level
    if world is None:
        writer.println("ERROR|||no world")
    else:
        DisplaySlot = JavaClass("net.minecraft.world.scores.DisplaySlot")
        scoreboard = world.getScoreboard()
        display_objective = scoreboard.getDisplayObjective(DisplaySlot.SIDEBAR)

        if display_objective is None:
            writer.println("ERROR|||no sidebar objective")
        else:
            entries = scoreboard.listPlayerScores(display_objective)
            lines = []
            for entry in entries:
                try:
                    holder_name = entry.owner()
                    team = scoreboard.getPlayersTeam(holder_name)
                    if team is not None:
                        prefix = team.getPlayerPrefix().getString()
                        suffix = team.getPlayerSuffix().getString()
                        text = prefix + suffix
                        safe = text.replace("\r","").replace("\n"," ").replace("~~~","")
                        lines.append(safe)
                except Exception as ex:
                    pass

            writer.println("OK|||" + "~~~".join(lines))
            writer.flush()
            bridge_socket.close()

except Exception as e:
    if writer:
        try:
            writer.println("ERROR|||" + str(e).replace("\n"," ").replace("\r",""))
        except:
            writer.println("ERROR|||unknown")
        writer.flush()
"""

def get_scoreboard_lines(timeout=3.0):
    """回傳 sidebar 每一行的文字(已去掉 § 色碼)。失敗回傳 None,並 echo 原因。"""
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.bind(("127.0.0.1", 0))
    srv.listen(1)
    srv.settimeout(timeout)
    p = srv.getsockname()[1]
    q = _queue.Queue()
    conn_holder = {"conn": None}

    script = _SCOREBOARD_SCRIPT.replace("__PORT__", str(p))

    def _recv():
        conn = None
        reader = None
        try:
            conn, _ = srv.accept()
            conn_holder["conn"] = conn
            conn.settimeout(timeout)
            reader = conn.makefile(encoding="utf-8", mode="r")
            line = reader.readline()
            if line:
                q.put(line.strip())
        except Exception as e:
            q.put(f"ERROR|||{e}")
        finally:
            if reader:
                try: reader.close()
                except: pass
            if conn:
                try: conn.close()
                except: pass
            try: srv.close()
            except: pass

    threading.Thread(target=_recv, daemon=True).start()
    try:
        eps(script)
        raw = q.get(timeout=timeout)
    except _queue.Empty:
        return None
    finally:
        conn = conn_holder["conn"]
        if conn:
            try: conn.close()
            except: pass
        try: srv.close()
        except: pass

    if raw.startswith("ERROR|||"):
        minescript.echo(f"[ERROR] {raw[8:]}")
        return None

    content = raw[len("OK|||"):]
    if not content:
        return []
    return [re.sub(r'§[0-9a-fk-or]', '', l, flags=re.IGNORECASE) for l in content.split("~~~")]

def find_scoreboard_value(prefix: str, timeout=3.0):
    """找到含有 prefix 的行,回傳 prefix 後面的內容。找不到回傳 None。"""
    lines = get_scoreboard_lines(timeout=timeout)
    if lines is None: return None
    for line in lines:
        if prefix in line:
            return line.split(prefix)[1].strip()
    return None

# 「Plot」後面接編號;數量 xN 在同一行編號後面即可,中間夾符號或空白都行
_PLOT_RE  = re.compile(r'Plot\s*[-–—:]?\s*(\d+)', re.IGNORECASE)
_COUNT_RE = re.compile(r'(?<![A-Za-z])x\s*(\d+)', re.IGNORECASE)

def get_plot_pest_count(timeout=3.0):
    """
    從計分板解析當前 plot 害蟲數量。
    回傳 (plot_num, count) 或 None。沒有 xN 時 count 為 1(與原本行為相同)。
    """
    lines = get_scoreboard_lines(timeout=timeout)
    if not lines: return None
    for line in lines:
        clean = _strip_mc(line)
        m = _PLOT_RE.search(clean)
        if not m: continue
        c = _COUNT_RE.search(clean[m.end():])
        return (int(m.group(1)), int(c.group(1)) if c else 0)
    return None
def get_current_plot(timeout=3.0):
    """
    從計分板解析當前玩家所在的 Plot 編號。
    回傳 int (plot_num) 或 None。
    """
    lines = get_scoreboard_lines(timeout=timeout)
    if not lines:
        return None
    for line in lines:
        clean = _strip_mc(line)
        m = _PLOT_RE.search(clean)
        if m:
            return int(m.group(1))
    return None
def dump_scoreboard():
    """除錯用:印出每一行(含 repr,看得出隱藏字元)以及解析結果。"""
    lines = get_scoreboard_lines()
    if lines is None:
        minescript.echo("取得失敗")
        return
    minescript.echo("=== Scoreboard ===")
    for l in lines:
        minescript.echo(repr(l))
    minescript.echo(f"get_plot_pest_count() = {get_plot_pest_count()}")

# ─────────────────────────────────────────────
# 主手物品
# ─────────────────────────────────────────────
_MAINHAND_SCRIPT = r"""
Socket=JavaClass("java.net.Socket");PrintWriter=JavaClass("java.io.PrintWriter")
OutputStreamWriter=JavaClass("java.io.OutputStreamWriter")
StandardCharsets=JavaClass("java.nio.charset.StandardCharsets")
mc=JavaClass("net.minecraft.client.Minecraft").getInstance()
writer=None
try:
    s=Socket("127.0.0.1",__PORT__)
    writer=PrintWriter(OutputStreamWriter(s.getOutputStream(),StandardCharsets.UTF_8),True)
    player=mc.player
    if player is None:writer.println("ERROR|||no player")
    else:
        inventory=player.getInventory()
        selected=inventory.getSelectedSlot()
        item_stack=inventory.getItem(selected)
        if item_stack.isEmpty():
            writer.println("EMPTY|||"+str(selected)+"|||null|||null")
        else:
            item=item_stack.getItem()
            item_id="unknown"
            try:
                BuiltInRegistries=JavaClass("net.minecraft.core.registries.BuiltInRegistries")
                loc=BuiltInRegistries.ITEM.getKey(item)
                item_id=str(loc) if loc else "unknown"
            except:pass
            display_name="null"
            try:display_name=item_stack.getDisplayName().getString()
            except:
                try:display_name=item_stack.getHoverName().getString()
                except:pass
            safe_name=display_name.replace("\r","").replace("\n"," ")
            writer.println(str(selected)+"|||"+item_id+"|||"+safe_name)
        writer.flush();s.close()
except Exception as e:
    if writer:writer.println("ERROR|||"+str(e).replace("\n"," "));writer.flush()
"""

def get_main_hand_item(timeout=3.0) -> dict:
    raw = _bridge_call(_MAINHAND_SCRIPT, timeout=timeout)
    if raw is None: return {"error": "failed"}
    if raw.startswith("EMPTY|||"):
        parts = raw.split("|||")
        return {"slot": int(parts[1]), "item_id": "air", "display_name": "null"}
    parts = raw.split("|||", 2)
    return {
        "slot":         int(parts[0]),
        "item_id":      parts[1] if len(parts) > 1 else "unknown",
        "display_name": parts[2] if len(parts) > 2 else "null",
    }

# ─────────────────────────────────────────────
# Entity 掃描
# ─────────────────────────────────────────────
_ENTITY_SCRIPT = r"""
Socket=JavaClass("java.net.Socket");PrintWriter=JavaClass("java.io.PrintWriter")
OutputStreamWriter=JavaClass("java.io.OutputStreamWriter")
StandardCharsets=JavaClass("java.nio.charset.StandardCharsets")
mc=JavaClass("net.minecraft.client.Minecraft").getInstance()
writer=None
try:
    s=Socket("127.0.0.1",__PORT__)
    writer=PrintWriter(OutputStreamWriter(s.getOutputStream(),StandardCharsets.UTF_8),True)
    player=mc.player;level=mc.level
    if player is None or level is None:writer.println("ERROR|||no player/level")
    else:
        px=player.getX();py=player.getY();pz=player.getZ()
        results=[]
        for entity in level.entitiesForRendering():
            try:
                ex=entity.getX();ey=entity.getY();ez=entity.getZ()
                dist=((ex-px)**2+(ey-py)**2+(ez-pz)**2)**0.5
                if dist>__MAXDIST__:continue
                # 篩選：type 包含 silverfish
                try:typ=entity.getType().toString()
                except:continue
                if "bat" not in typ.lower() and "silverfish" not in typ.lower() :continue
                # 篩選：血量範圍由 Discord 面板 / 測試模式決定
                try:health=float(entity.getHealth())
                except:continue
                if not (__HMIN__ < health < __HMAX__):continue
                eid=entity.getId()
                try:name=entity.getDisplayName().getString()
                except:
                    try:name=entity.getName().getString()
                    except:name=typ
                safe_name=name.replace("|","").replace(";","").replace("\r","").replace("\n","")
                results.append(f"{eid}|{ex:.2f}|{ey:.2f}|{ez:.2f}|{health:.1f}|{safe_name}")
            except:continue
        writer.println(";".join(results) if results else "EMPTY");writer.flush();s.close()
except Exception as e:
    if writer:writer.println("ERROR|||"+str(e).replace("\n"," "));writer.flush()
"""

class _SilverfishEntity:
    __slots__ = ("id", "position", "health", "name")

    def __init__(self, entity_id, position, health, name):
        self.id = entity_id
        self.position = position
        self.health = health
        self.name = name


def find_silverfish(max_dist=300, timeout=3.0):
    h_min, h_max = pest_health_range()
    script = (
        _ENTITY_SCRIPT
        .replace("__MAXDIST__", str(max_dist))
        .replace("__HMIN__", f"{h_min:.1f}")
        .replace("__HMAX__", f"{h_max:.1f}")
    )
    raw = _bridge_call(script, timeout=timeout)
    if not raw or raw == "EMPTY": minescript.echo('沒找到');return []
    result = []
    for entry in raw.split(";"):
        parts = entry.split("|")
        if len(parts) != 6: continue
        try:
            e = _SilverfishEntity(
                int(parts[0]),
                (float(parts[1]), float(parts[2]), float(parts[3])),
                float(parts[4]),
                parts[5],
            )
            result.append(e)
        except: continue
    return result

def _get_entity_pos(entity_id, retries=3):
    for attempt in range(retries):
        for e in find_silverfish():
            if e.id == entity_id: return e.position
        if attempt < retries - 1: time.sleep(0.1)
    return None

# ─────────────────────────────────────────────
# 通用 entity 掃描（不過濾害蟲）
# ─────────────────────────────────────────────
_SCAN_ALL_SCRIPT = r"""
Socket=JavaClass("java.net.Socket");PrintWriter=JavaClass("java.io.PrintWriter")
OutputStreamWriter=JavaClass("java.io.OutputStreamWriter")
StandardCharsets=JavaClass("java.nio.charset.StandardCharsets")
mc=JavaClass("net.minecraft.client.Minecraft").getInstance()
writer=None
try:
    s=Socket("127.0.0.1",__PORT__)
    writer=PrintWriter(OutputStreamWriter(s.getOutputStream(),StandardCharsets.UTF_8),True)
    world=mc.level
    if world is None:writer.println("ERROR|||no world")
    else:
        entities=world.entitiesForRendering()
        lines=[]
        for entity in entities:
            try:
                etype=entity.getClass().getSimpleName()
                uuid=str(entity.getUUID())
                ex=entity.getX();ey=entity.getY();ez=entity.getZ()
                
                # 取得血量 (若實體無血量屬性如 ItemEntity / ArmorStand，則標記為 0.0)
                try:health=float(entity.getHealth())
                except:health=0.0
                
                try:name=entity.getDisplayName().getString()
                except:
                    try:name=entity.getName().getString()
                    except:name=etype
                safe_name=name.replace("\r","").replace("\n"," ").replace("~~~","").replace("|||","")
                lines.append(etype+"|||"+uuid+"|||"+safe_name+"|||"+str(ex)+"|||"+str(ey)+"|||"+str(ez)+"|||"+str(health))
            except:pass
        writer.println("OK~~~"+"~~~".join(lines));writer.flush();s.close()
except Exception as e:
    if writer:writer.println("ERROR|||"+str(e).replace("\n"," "));writer.flush()
"""

def scan_entities(timeout=3.0) -> list[dict] | None:
    raw = _bridge_call(_SCAN_ALL_SCRIPT, timeout=timeout)
    if raw is None: return None
    parts = raw.split("~~~")
    if not parts or parts[0] != "OK": return None
    result = []
    for entry in parts[1:]:
        if not entry: continue
        fields = entry.split("|||")
        if len(fields) != 7: continue  # 欄位增加至 7 個
        try:
            result.append({
                "type":   fields[0],
                "uuid":   fields[1],
                "name":   fields[2],
                "x":      float(fields[3]),
                "y":      float(fields[4]),
                "z":      float(fields[5]),
                "health": float(fields[6]),  # 新增血量欄位
            })
        except: continue
    return result

# ─────────────────────────────────────────────
# look / look_at_block
# ─────────────────────────────────────────────
def look(yaw, pitch, steps=35):
    sy, sp = player_tracker.orientation()
    dy = (yaw - sy + 180) % 360 - 180   # 最短角度差
    dp = pitch - sp
    end_yaw = sy + dy                    # 連續終點,不要用原始 yaw
    cx  = sy + dy * 0.45 + random.uniform(-3,   3)
    cpx = sp + dp * 0.45 + random.uniform(-1.5, 1.5)
    for i in range(1, steps + 1):
        t  = i / steps
        te = t * t * (3 - 2 * t)
        jy = _perlin_1d(t * 4 + random.random() * 0.01, 0) * 0.15
        jp = _perlin_1d(t * 4 + random.random() * 0.01, 1) * 0.07
        cy_ = (1-te)**2 * sy + 2*(1-te)*te * cx  + te**2 * end_yaw + jy
        cp_ = (1-te)**2 * sp + 2*(1-te)*te * cpx + te**2 * pitch   + jp
        minescript.player_set_orientation(cy_, _clamp(cp_, -90, 90))
        time.sleep(0.007)

def get_block_aim_point(bx, by, bz, timeout=3.0, eye_height: float | None = None,
                        strafe_margin: float = 0.5):
    """
    在方塊「準心真的打得到的面」上挑一個瞄準點，回傳 (x, y, z)；打不到回傳 None。
    只計算、不轉頭。

    驗證方式 = 模擬準心：從眼睛朝候選點發射射線（用 OUTLINE 外框，跟遊戲準心/挖掘一致，
    不是 COLLIDER），再往方塊裡延伸一小段；射線打到的必須是「這顆方塊」且「進入的面」就是該面。
    被別的方塊（包含沒碰撞箱但有外框的）擋住的面會被濾掉。
    候選點要從「目前位置、左右各移 strafe_margin」三個眼睛位置都成立（貼牆時退回只看目前位置），
    並偏好離目前視角近的點。
    """
    s = player_tracker.get()
    px, py, pz = s["x"], s["y"], s["z"]
    eh = eye_height if eye_height is not None else s["eye_h"]
    ex, ey, ez = px, py + eh, pz

    faces = [((1,0,0),(0,1,0),(0,0,1)), ((-1,0,0),(0,1,0),(0,0,1)),
             ((0,1,0),(1,0,0),(0,0,1)), ((0,-1,0),(1,0,0),(0,0,1)),
             ((0,0,1),(1,0,0),(0,1,0)), ((0,0,-1),(1,0,0),(0,1,0))]

    yr = math.radians(s["yaw"])
    rx, rz = -math.cos(yr), -math.sin(yr)          # 右方（D 鍵）
    m = strafe_margin
    offsets = [(0.0, 0.0), (rx * m, rz * m), (-rx * m, -rz * m)]

    cands = []
    for (nx,ny,nz),(ux,uy,uz),(vx,vy,vz) in faces:
        cx = bx+0.5+nx*0.5; cy = by+0.5+ny*0.5; cz = bz+0.5+nz*0.5   # 面中心（在面上）
        if (ex-cx)*nx + (ey-cy)*ny + (ez-cz)*nz <= 0:
            continue  # 背向玩家的面
        cands.append((cx, cy, cz, nx, ny, nz))            # 面中心一定要試
        for _ in range(8):
            u = random.uniform(-0.35, 0.35)
            v = random.uniform(-0.35, 0.35)
            cands.append((cx+ux*u+vx*v, cy+uy*u+vy*v, cz+uz*u+vz*v, nx, ny, nz))
    if not cands:
        return None

    pts = ";".join("%.4f,%.4f,%.4f,%d,%d,%d" % c for c in cands)

    def _clip(offs):
        script = (_CLIP_SCRIPT
                  .replace("__PTS__", pts)
                  .replace("__TB__", "%d,%d,%d" % (bx, by, bz))
                  .replace("__OFFS__", ";".join("%.3f,%.3f" % o for o in offs)))
        raw = _bridge_call(script, timeout=timeout)
        if not raw or not raw.startswith("OK|||"):
            return None
        return [t for t in raw[5:].split(";") if t]

    vis = _clip(offsets)
    if not vis:                                     # 貼牆之類：退回只看目前位置
        vis = _clip([(0.0, 0.0)])
    if not vis:
        return None

    pts_vis = []
    for t in vis:
        f = t.split(",")
        pt = (float(f[0]), float(f[1]), float(f[2]))
        fc = (bx + 0.5 + int(f[3]) * 0.5, by + 0.5 + int(f[4]) * 0.5, bz + 0.5 + int(f[5]) * 0.5)
        pts_vis.append((pt, math.dist(pt, fc)))
    cur_yaw, cur_pitch = player_tracker.orientation()

    def _cost(item):
        pt, dface = item
        y, p = _calc_yaw_pitch((px, py, pz), pt, eh)
        # 轉動幅度 + 偏離面中心的懲罰：瞄準點盡量靠近面中心，
        # 之後的上下晃動才有完整的空間（點在邊緣的話，大幅晃動會被方塊邊界截掉一半）
        return abs(_angle_diff(cur_yaw, y)) + abs(p - cur_pitch) + 40.0 * dface

    pts_vis.sort(key=_cost)
    best = pts_vis[:max(1, len(pts_vis) // 3)]       # 最好的前 1/3 再隨機，保留擬人感
    return random.choice(best)[0]


def look_at_block(bx, by, bz, timeout=3.0, eye_height: float | None = None):
    """
    eye_height: 手動指定眼高。None 則從 player_tracker 取得即時值（含蹲下補正）。
    """
    pt = get_block_aim_point(bx, by, bz, timeout, eye_height)
    if pt is None:
        return False
    s = player_tracker.get()
    yaw, pitch = _calc_yaw_pitch((s["x"], s["y"], s["z"]), pt)
    look(yaw, pitch)
    return True


# ─────────────────────────────────────────────
# look_at_the_block(block_name)：看向觸及範圍內指定名稱的方塊
# ─────────────────────────────────────────────
_FIND_BLOCK_SCRIPT = r"""
Socket=JavaClass("java.net.Socket");PrintWriter=JavaClass("java.io.PrintWriter")
OutputStreamWriter=JavaClass("java.io.OutputStreamWriter")
StandardCharsets=JavaClass("java.nio.charset.StandardCharsets")
BlockPos=JavaClass("net.minecraft.core.BlockPos")
mc=JavaClass("net.minecraft.client.Minecraft").getInstance()
writer=None
try:
    s=Socket("127.0.0.1",__PORT__)
    writer=PrintWriter(OutputStreamWriter(s.getOutputStream(),StandardCharsets.UTF_8),True)
    level=mc.level
    if level is None:
        writer.println("ERROR|||no level")
    else:
        ex=__EX__;ey=__EY__;ez=__EZ__
        cx=__CX__;cy=__CY__;cz=__CZ__
        R=__R__
        reach2=__REACH__*__REACH__
        wantstr="__NAME__"
        exact=__EXACT__
        out=[]
        for dx in range(-R,R+1):
            for dy in range(-R,R+1):
                for dz in range(-R,R+1):
                    bx=cx+dx;by=cy+dy;bz=cz+dz
                    ddx=bx+0.5-ex;ddy=by+0.5-ey;ddz=bz+0.5-ez
                    d2=ddx*ddx+ddy*ddy+ddz*ddz
                    if d2>reach2:continue
                    bid=str(level.getBlockState(BlockPos(bx,by,bz)).getBlock().defaultBlockState().toString())
                    head=bid.split("}")[0]
                    if exact==1:
                        parts=head.split("{")
                        ok=(("," + parts[len(parts)-1] + ",") in wantstr)
                    else:
                        ok=(wantstr in head)
                    if ok:
                        out.append(str(bx)+","+str(by)+","+str(bz)+","+str(d2))
        writer.println("OK|||"+";".join(out))
        writer.flush();s.close()
except Exception as e:
    if writer:writer.println("ERROR|||"+str(e).replace("\n"," "));writer.flush()
"""

def _scan_blocks(name_arg: str, exact: bool, reach: float, timeout: float):
    """
    呼叫 Java 端掃描一次。exact=True 時 name_arg 形如 ",a,b,"（任一符合即可）；
    exact=False 時為單一名稱，名稱包含即可。
    成功回傳 [(d2, x, y, z), ...]；橋接失敗/逾時回傳 None。
    """
    s = player_tracker.get()
    try:
        px, py, pz = minescript.player_position()
    except Exception:
        px, py, pz = s["x"], s["y"], s["z"]
    ex, ey, ez = px, py + s["eye_h"], pz

    script = (_FIND_BLOCK_SCRIPT
              .replace("__EX__", repr(float(ex)))
              .replace("__EY__", repr(float(ey)))
              .replace("__EZ__", repr(float(ez)))
              .replace("__CX__", str(math.floor(ex)))
              .replace("__CY__", str(math.floor(ey)))
              .replace("__CZ__", str(math.floor(ez)))
              .replace("__R__", str(int(math.ceil(reach))))
              .replace("__REACH__", repr(float(reach)))
              .replace("__NAME__", name_arg)
              .replace("__EXACT__", "1" if exact else "0"))
    raw = _bridge_call(script, timeout=timeout)
    if not raw or not raw.startswith("OK|||"):
        return None

    found = []
    for item in raw[5:].split(";"):
        if not item:
            continue
        try:
            x, y, z, d2 = item.split(",")
            found.append((float(d2), int(x), int(y), int(z)))
        except Exception:
            continue
    return found


def find_blocks_nearby(block_name, reach: float = 4.5,
                       exact: bool = True, timeout: float = 3.0) -> list:
    """
    在玩家觸及範圍(眼睛到方塊中心 <= reach)內搜尋指定名稱的方塊。
    block_name: "wheat"、"minecraft:diamond_ore" 皆可；也可傳清單（如 ["obsidian", "diamond_block"]），
                符合任一個就算（同一個「集群」）。
    exact=True 方塊名稱需完全相符；False 則名稱包含即可（如 "ore"）。
    回傳 [(bx, by, bz), ...]，由近到遠排序；失敗回傳 []。
    除錯：執行後可讀 find_blocks_nearby.last_status，值為 ("ok"|"bridge_failed"|"bad_names", 數量)。
    """
    raw_names = [block_name] if isinstance(block_name, str) else list(block_name)
    names = []
    for n in raw_names:
        n = str(n).strip().lower()
        if not n or not re.fullmatch(r"[a-z0-9_:./-]+", n):
            continue
        if exact:
            if ":" not in n:
                n = "minecraft:" + n
        else:
            n = n.replace("minecraft:", "")
        names.append(n)
    if not names:
        find_blocks_nearby.last_status = ("bad_names", 0)
        return []

    if exact:
        calls = [("," + ",".join(names) + ",", True)]
    else:
        calls = [(n, False) for n in names]    # 部分比對：一個名稱掃一次再合併

    best = {}
    any_ok = False
    for name_arg, ex_flag in calls:
        res = _scan_blocks(name_arg, ex_flag, reach, timeout)
        if res is None:
            continue
        any_ok = True
        for d2, x, y, z in res:
            if (x, y, z) not in best or d2 < best[(x, y, z)]:
                best[(x, y, z)] = d2

    ordered = sorted(best, key=lambda k: best[k])
    find_blocks_nearby.last_status = ("ok" if any_ok else "bridge_failed", len(ordered))
    return ordered

find_blocks_nearby.last_status = ("unused", 0)


def look_at_the_block(block_name: str, reach: float = 4.5, exact: bool = True,
                      max_tries: int = 6, timeout: float = 3.0) -> bool:
    """
    轉頭看向觸及範圍內最近的 block_name 方塊（原理同 look_at_block：
    只瞄準真的看得到的面；被擋住的會換下一顆）。
    範圍內沒有（或全被擋住）→ 不動，回傳 False。
    """
    for bx, by, bz in find_blocks_nearby(block_name, reach, exact, timeout)[:max_tries]:
        if look_at_block(bx, by, bz, timeout=timeout):
            return True
    return False


def look_at_npc(npc_x: float, npc_y: float, npc_z: float,
                npc_height: float = 1.8, aim_ratio: float = 0.85,
                jitter: float = 0.15):
    """
    平滑轉向看向 NPC。
    npc_x/y/z  : NPC 腳底座標（getX/getY/getZ）
    npc_height : NPC 總高度，預設 1.8
    aim_ratio  : 瞄準點高度比例，0.85 約頭部，0.5 腰部
    jitter     : 隨機偏移範圍（格）
    """
    s = player_tracker.get()
    px, py, pz = s["x"], s["y"], s["z"]
    tx = npc_x + random.uniform(-jitter, jitter)
    ty = npc_y + npc_height * aim_ratio + random.uniform(-jitter * 0.5, jitter * 0.5)
    tz = npc_z + random.uniform(-jitter, jitter)
    yaw, pitch = _calc_yaw_pitch((px, py, pz), (tx, ty, tz))
    look(yaw, pitch)
    return True


_CLIP_SCRIPT = r"""
Socket=JavaClass("java.net.Socket");PrintWriter=JavaClass("java.io.PrintWriter")
OutputStreamWriter=JavaClass("java.io.OutputStreamWriter")
StandardCharsets=JavaClass("java.nio.charset.StandardCharsets")
Vec3=JavaClass("net.minecraft.world.phys.Vec3")
ClipContext=JavaClass("net.minecraft.world.level.ClipContext")
CBlock=JavaClass("net.minecraft.world.level.ClipContext$Block")
CFluid=JavaClass("net.minecraft.world.level.ClipContext$Fluid")
mc=JavaClass("net.minecraft.client.Minecraft").getInstance()
writer=None
try:
    s=Socket("127.0.0.1",__PORT__)
    writer=PrintWriter(OutputStreamWriter(s.getOutputStream(),StandardCharsets.UTF_8),True)
    player=mc.player;level=mc.level
    if player is None or level is None:
        writer.println("ERROR|||no player/level")
    else:
        ex0=player.getX();ey0=player.getY()+float(player.getEyeHeight());ez0=player.getZ()
        tb=("__TB__").split(",")
        tbx=int(tb[0]);tby=int(tb[1]);tbz=int(tb[2])
        oxs=[];ozs=[]
        for o in "__OFFS__".split(";"):
            q=o.split(",")
            oxs.append(float(q[0]));ozs.append(float(q[1]))
        out=[]
        for item in "__PTS__".split(";"):
            p=item.split(",")
            tx=float(p[0]);ty=float(p[1]);tz=float(p[2])
            nx=int(p[3]);ny=int(p[4]);nz=int(p[5])
            ok=True
            for i in range(len(oxs)):
                ex=ex0+oxs[i];ez=ez0+ozs[i]
                dx=tx-ex;dy=ty-ey0;dz=tz-ez
                L=(dx*dx+dy*dy+dz*dz)**0.5
                if L<0.001:
                    ok=False
                    break
                eye=Vec3(ex,ey0,ez)
                end=Vec3(tx+dx/L*0.3,ty+dy/L*0.3,tz+dz/L*0.3)
                hit=level.clip(ClipContext(eye,end,CBlock.OUTLINE,CFluid.NONE,player))
                good=False
                if hit is not None and "BLOCK" in str(hit.getType()):
                    bp=hit.getBlockPos()
                    if bp.getX()==tbx and bp.getY()==tby and bp.getZ()==tbz:
                        good=True
                        try:
                            d=hit.getDirection()
                            if d.getStepX()!=nx or d.getStepY()!=ny or d.getStepZ()!=nz:
                                good=False
                        except:
                            pass
                if not good:
                    ok=False
                    break
            if ok:
                out.append(item)
        writer.println("OK|||"+";".join(out))
        writer.flush();s.close()
except Exception as e:
    if writer:
        writer.println("ERROR|||"+str(e).replace("\n"," "));writer.flush()
"""


_VISGRID_SCRIPT = r"""
Socket=JavaClass("java.net.Socket");PrintWriter=JavaClass("java.io.PrintWriter")
OutputStreamWriter=JavaClass("java.io.OutputStreamWriter")
StandardCharsets=JavaClass("java.nio.charset.StandardCharsets")
Vec3=JavaClass("net.minecraft.world.phys.Vec3")
ClipContext=JavaClass("net.minecraft.world.level.ClipContext")
CBlock=JavaClass("net.minecraft.world.level.ClipContext$Block")
CFluid=JavaClass("net.minecraft.world.level.ClipContext$Fluid")
mc=JavaClass("net.minecraft.client.Minecraft").getInstance()
writer=None
try:
    s=Socket("127.0.0.1",__PORT__)
    writer=PrintWriter(OutputStreamWriter(s.getOutputStream(),StandardCharsets.UTF_8),True)
    player=mc.player;level=mc.level
    if player is None or level is None:
        writer.println("ERROR|||no player/level")
    else:
        ex0=player.getX();ey0=player.getY()+float(player.getEyeHeight());ez0=player.getZ()
        tb=("__TB__").split(",")
        tbx=int(tb[0]);tby=int(tb[1]);tbz=int(tb[2])
        pp=("__PT__").split(",")
        tx=float(pp[0]);ty=float(pp[1]);tz=float(pp[2])
        nx=int(pp[3]);ny=int(pp[4]);nz=int(pp[5])
        N=__N__;ST=__ST__
        out=[]
        for ix in range(-N,N+1):
            for iz in range(-N,N+1):
                ex=ex0+ix*ST;ez=ez0+iz*ST
                dx=tx-ex;dy=ty-ey0;dz=tz-ez
                L=(dx*dx+dy*dy+dz*dz)**0.5
                if L<0.001:
                    continue
                eye=Vec3(ex,ey0,ez)
                end=Vec3(tx+dx/L*0.3,ty+dy/L*0.3,tz+dz/L*0.3)
                hit=level.clip(ClipContext(eye,end,CBlock.OUTLINE,CFluid.NONE,player))
                good=False
                if hit is not None and "BLOCK" in str(hit.getType()):
                    bp=hit.getBlockPos()
                    if bp.getX()==tbx and bp.getY()==tby and bp.getZ()==tbz:
                        good=True
                        try:
                            d=hit.getDirection()
                            if d.getStepX()!=nx or d.getStepY()!=ny or d.getStepZ()!=nz:
                                good=False
                        except:
                            pass
                if good:
                    out.append(str(ix)+","+str(iz))
        writer.println("OK|||"+str(ex0)+","+str(ez0)+"|||"+";".join(out))
        writer.flush();s.close()
except Exception as e:
    if writer:
        writer.println("ERROR|||"+str(e).replace("\n"," "));writer.flush()
"""


def aim_visibility_grid(block_pos, aim, half: float = 1.8, step: float = 0.2, timeout: float = 3.0):
    """
    算出「站在哪些位置，準心對著 aim 還能打到 block_pos 的那個面」。
    以目前玩家位置為中心，水平 ±half 格、每 step 格取一個眼睛位置（眼睛高度不變）。
    回傳 (base_x, base_z, step, N, {(ix, iz), ...})；失敗回傳 None。
    位置 (x, z) 對應格點 ((x-base_x)/step, (z-base_z)/step)。
    """
    bx, by, bz = (int(v) for v in block_pos)
    face = None
    for i in range(3):
        for sign, off in ((1, 1.0), (-1, 0.0)):
            e = abs(aim[i] - ((bx, by, bz)[i] + off))
            if face is None or e < face[0]:
                face = (e, i, sign)
    if face is None or face[0] > 0.05:
        return None
    nrm = [0, 0, 0]
    nrm[face[1]] = face[2]
    N = int(round(half / step))
    script = (_VISGRID_SCRIPT
              .replace("__TB__", "%d,%d,%d" % (bx, by, bz))
              .replace("__PT__", "%.4f,%.4f,%.4f,%d,%d,%d" % (aim[0], aim[1], aim[2], nrm[0], nrm[1], nrm[2]))
              .replace("__N__", str(N))
              .replace("__ST__", repr(float(step))))
    raw = _bridge_call(script, timeout=timeout)
    if not raw or not raw.startswith("OK|||"):
        return None
    try:
        base, cells = raw[5:].split("|||", 1)
        base_x, base_z = (float(v) for v in base.split(","))
        vis = set()
        for t in cells.split(";"):
            if t:
                a, b = t.split(",")
                vis.add((int(a), int(b)))
        return (base_x, base_z, float(step), N, vis)
    except Exception:
        return None


# ─────────────────────────────────────────────
# 方塊檢查工具
# ─────────────────────────────────────────────
_BLOCK_CHECK_SCRIPT = r"""
Socket=JavaClass("java.net.Socket");PrintWriter=JavaClass("java.io.PrintWriter")
OutputStreamWriter=JavaClass("java.io.OutputStreamWriter")
StandardCharsets=JavaClass("java.nio.charset.StandardCharsets")
BlockPos=JavaClass("net.minecraft.core.BlockPos")
mc=JavaClass("net.minecraft.client.Minecraft").getInstance()
writer=None
try:
    s=Socket("127.0.0.1",__PORT__)
    writer=PrintWriter(OutputStreamWriter(s.getOutputStream(),StandardCharsets.UTF_8),True)
    level=mc.level
    if level is None:
        writer.println("ERROR|||no level")
    else:
        results=[]
        for item in "__CHECKS__".split(";"):
            p=item.split(",")
            bx=int(float(p[0]));by=int(float(p[1]));bz=int(float(p[2]))
            bp=BlockPos(bx,by,bz)
            state=level.getBlockState(bp)
            block_id=str(state.getBlock().defaultBlockState().toString())
            results.append(p[3]+":"+block_id)
        writer.println("OK|||"+"~~~".join(results))
        writer.flush();s.close()
except Exception as e:
    if writer:writer.println("ERROR|||"+str(e).replace("\n"," "));writer.flush()
"""

# 視為「可通行/忽略」的方塊（水、空氣及水中植物都不算障礙）
_PASSABLE_BLOCKS = {
    "air", "cave_air", "void_air",
    "water", "bubble_column",
    "seagrass", "tall_seagrass", "kelp", "kelp_plant",
}

def _block_name_from_state(block_id: str) -> str:
    """'Block{minecraft:water}[level=0]' -> 'water'（只取方塊本體，不受 [waterlogged=true] 影響）"""
    m = re.search(r'\{([^}]+)\}', block_id)
    if not m:
        return block_id.lower()
    name = m.group(1).lower()
    return name.split(":", 1)[1] if ":" in name else name

def _is_passable_block(block_id: str) -> bool:
    name = _block_name_from_state(block_id)
    return name in _PASSABLE_BLOCKS or name.endswith("_air")

_CROP_BLOCKS = {
    "wheat", "carrots", "potatoes", "beetroots",
    "nether_wart", "melon_stem", "pumpkin_stem",
    "sugar_cane", "cocoa", "cactus",
}

def _yaw_to_forward_right(yaw_deg):
    """從 Minecraft yaw 算出前方和玩家視角右方的單位向量 (dx, dz)。"""
    rad = math.radians(yaw_deg)
    fx = -math.sin(rad)
    fz =  math.cos(rad)
    # Minecraft strafe 的正方向是玩家視角左；右方需取負號。
    rx = -math.cos(rad)
    rz = -math.sin(rad)
    return (fx, fz), (rx, rz)


def check_sides(timeout=2.0) -> dict:
    """
    檢查玩家左右各一格是否為空氣（水、水草、海帶、氣泡柱視為空，不算方塊）。
    回傳 {"left": True/False, "right": True/False}
    True 代表該側為空（可走）。
    """
    # 優先讀即時狀態，避免追蹤器上一 tick 的 yaw/位置造成側向取樣偏差。
    try:
        px, py, pz = minescript.player_position()
        orientation = minescript.player_orientation()
        if not orientation:
            raise RuntimeError("player orientation unavailable")
        yaw = orientation[0]
    except Exception:
        s = player_tracker.get()
        px, py, pz = s["x"], s["y"], s["z"]
        yaw = s["yaw"]

    (fx, fz), (rx, rz) = _yaw_to_forward_right(yaw)

    # 將側向向量吸附到最近的方格軸，避免小數座標或輕微斜角取到錯格。
    if abs(rx) >= abs(rz):
        rx, rz = (1 if rx >= 0 else -1), 0
    else:
        rx, rz = 0, (1 if rz >= 0 else -1)

    bx, by, bz = math.floor(px), math.floor(py), math.floor(pz)

    # 左側為玩家視角右向量的反方向。
    lx, lz = -rx, -rz

    checks = (
        "%d,%d,%d,left_low;%d,%d,%d,left_high;"
        "%d,%d,%d,right_low;%d,%d,%d,right_high"
    ) % (
        bx + lx, by,     bz + lz,
        bx + lx, by + 1, bz + lz,
        bx + rx, by,     bz + rz,
        bx + rx, by + 1, bz + rz,
    )

    script = _BLOCK_CHECK_SCRIPT.replace("__CHECKS__", checks)
    raw = _bridge_call(script, timeout=timeout)
    if not raw or not raw.startswith("OK|||"):
        return {"left": None, "right": None}

    left_blocked = False
    right_blocked = False
    for entry in raw[5:].split("~~~"):
        if not entry: continue
        label, block_id = entry.split(":", 1)
        is_solid = not _is_passable_block(block_id)   # 水/空氣/水草都視為可通行
        if "left" in label and is_solid:
            left_blocked = True
        if "right" in label and is_solid:
            right_blocked = True

    return {
        "left":  not left_blocked,   # True = 左邊空，可走
        "right": not right_blocked,
    }


def check_front_crops(distance=5, timeout=2.0) -> dict:
    """
    檢查視角前方 distance x 1 x 1 格內有無農作物。
    回傳 {"found": True/False, "blocks": [block_id, ...]}
    """
    s = player_tracker.get()
    px, py, pz = s["x"], s["y"], s["z"]
    yaw = s["yaw"]

    (fx, fz), _ = _yaw_to_forward_right(yaw)

    checks_parts = []
    seen = set()
    bx_base = math.floor(px)
    bz_base = math.floor(pz)
    by_base = math.floor(py)
    for i in range(1, distance + 1):
        bx = math.floor(px + fx * i)
        bz = math.floor(pz + fz * i)
        for dy in [-1, 0, 1]:
            by = by_base + dy
            key = (bx, by, bz)
            if key in seen:
                continue
            seen.add(key)
            checks_parts.append("%d,%d,%d,crop_%d_%d" % (bx, by, bz, i, dy))

    script = _BLOCK_CHECK_SCRIPT.replace("__CHECKS__", ";".join(checks_parts))
    raw = _bridge_call(script, timeout=timeout)
    if not raw or not raw.startswith("OK|||"):
        return {"found": False, "blocks": []}

    found_blocks = []
    for entry in raw[5:].split("~~~"):
        if not entry: continue
        _, block_id = entry.split(":", 1)
        for crop in _CROP_BLOCKS:
            if crop in block_id.lower():
                found_blocks.append(block_id)
                break

    return {
        "found":  bool(found_blocks),
        "blocks": found_blocks,
    }

# ─────────────────────────────────────────────
# 當前寵物
# ─────────────────────────────────────────────
_PET_SCRIPT = r"""
Socket=JavaClass("java.net.Socket");PrintWriter=JavaClass("java.io.PrintWriter")
OutputStreamWriter=JavaClass("java.io.OutputStreamWriter")
StandardCharsets=JavaClass("java.nio.charset.StandardCharsets")
mc=JavaClass("net.minecraft.client.Minecraft").getInstance()
writer=None
try:
    s=Socket("127.0.0.1",__PORT__)
    writer=PrintWriter(OutputStreamWriter(s.getOutputStream(),StandardCharsets.UTF_8),True)
    player=mc.player;level=mc.level
    if player is None or level is None:
        writer.println("ERROR|||no player/level")
    else:
        px=player.getX();py=player.getY();pz=player.getZ()
        best_dist=9999.0
        best_line=""
        for entity in level.entitiesForRendering():
            try:
                ex=entity.getX();ey=entity.getY();ez=entity.getZ()
                dist=((ex-px)**2+(ey-py)**2+(ez-pz)**2)**0.5
                if dist>10.0:continue
                try:name=entity.getDisplayName().getString()
                except:
                    try:name=entity.getName().getString()
                    except:name=""
                if not name:continue
                # 只要 [Lv 或 [Lvl 開頭的
                if not (name.startswith("[Lv") or name.startswith("[lv")):continue
                if dist<best_dist:
                    best_dist=dist
                    safe=name.replace("\r","").replace("\n"," ").replace("|||","")
                    best_line=safe+"|||"+str(ex)+"|||"+str(ey)+"|||"+str(ez)+"|||"+str(dist)
            except:continue
        if best_line:
            writer.println("OK|||"+best_line)
        else:
            writer.println("EMPTY")
        writer.flush();s.close()
except Exception as e:
    if writer:writer.println("ERROR|||"+str(e).replace("\n"," "));writer.flush()
"""

def get_current_pet(timeout=3.0) -> dict | None:
    """
    取得當前召喚的寵物（距離玩家最近且名稱以 [Lv/Lvl 開頭的實體）。
    回傳 {"full_name": ..., "name": ..., "level": ..., "x": ..., "y": ..., "z": ..., "dist": ...}
    失敗或無寵物回傳 None
    """
    raw = _bridge_call(_PET_SCRIPT, timeout=timeout)
    if not raw or raw == "EMPTY" or raw.startswith("ERROR|||"):
        return None

    parts = raw[5:].split("|||")  # 去掉 "OK|||"
    if len(parts) != 5:
        return None

    full_name = parts[0]
    try:
        x, y, z, dist = float(parts[1]), float(parts[2]), float(parts[3]), float(parts[4])
    except:
        return None

    # 解析等級和名稱
    m = re.search(r'\[Lvl?\s*(\d+)\]\s*(?:\[\d+[^\]]*\]\s*)?(.+)', full_name, re.IGNORECASE)
    level    = int(m.group(1)) if m else None
    pet_name = m.group(2).strip() if m else full_name

    return {
        "full_name": full_name,
        "name":      pet_name,
        "level":     level,
        "x": x, "y": y, "z": z,
        "dist": dist,
    }