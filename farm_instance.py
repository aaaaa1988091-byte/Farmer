"""FarmBot 多開支援：依 Minecraft 玩家名稱區分狀態檔、控制埠與實例註冊表。

Farm.py / Farm_watchdog.py / pest_utils.py 共用。
- 每個 Minecraft 帳號有自己的 state / stop / log / lock 檔（檔名帶玩家名稱）。
- 每個 Farm.py 啟動時會挑一個空的控制埠，並寫入 instances/<玩家>.json（含心跳時間）。
- Discord bot 讀 instances/ 資料夾就知道目前有哪些帳號在線，並用各自的埠控制。

跨設備（每台電腦各跑一個或多個 Minecraft）時，在每台電腦的 .env 設定：
    FARMBOT_INSTANCE_DIR=Z:/FarmBot_shared    # 所有設備都能讀寫的網路共用資料夾（例如映射成磁碟機代號的 NAS）（不要用有延遲的雲端同步）
    FARMBOT_CONTROL_BIND=0.0.0.0                  # 讓其他設備能連進來（預設 127.0.0.1 只限本機）
    FARMBOT_ADVERTISE_HOST=192.168.1.20           # 選填：這台的區網 IP；不填會自動偵測
    FARMBOT_CONTROL_SECRET=同一組密碼              # 所有設備必須相同
    FARMBOT_STALE_SECONDS=30                      # 選填：心跳逾時秒數
並在防火牆放行 BASE_PORT 起算 PORT_SPAN 個 TCP 埠。
"""
from __future__ import annotations

import json
import os
import re
import socket
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
BASE_PORT = 38751          # 第一個帳號預設埠；之後依序往上找空位
PORT_SPAN = 32
STALE_SECONDS = 20.0       # 心跳超過這個秒數視為離線（可用 FARMBOT_STALE_SECONDS 覆寫）


def _read_env() -> dict:
    values = {}
    try:
        for raw in (ROOT / ".env").read_text(encoding="utf-8").splitlines():
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            values[key.strip()] = value.strip().strip('"').strip("'")
    except OSError:
        pass
    return values


_ENV = _read_env()
try:
    BASE_PORT = int(_ENV.get("FARMBOT_CONTROL_PORT", BASE_PORT))
except ValueError:
    pass

try:
    STALE_SECONDS = float(_ENV.get("FARMBOT_STALE_SECONDS", STALE_SECONDS))
except ValueError:
    pass

# 控制埠綁定位址：127.0.0.1 = 只限本機；0.0.0.0 或區網 IP = 允許其他設備連線（務必設定 FARMBOT_CONTROL_SECRET）
CONTROL_BIND = _ENV.get("FARMBOT_CONTROL_BIND", "127.0.0.1").strip() or "127.0.0.1"
_LOOPBACK = {"127.0.0.1", "localhost", "::1"}
MACHINE = socket.gethostname()

# 多個 Minecraft 若使用不同的 minescript 資料夾，可在 .env 設定同一個共用資料夾：
#   FARMBOT_INSTANCE_DIR=D:\FarmBot_shared
INSTANCE_DIR = Path(_ENV.get("FARMBOT_INSTANCE_DIR") or (ROOT / "instances"))


# ───────────── 玩家名稱 ─────────────
_name_cache: str | None = None


def sanitize(name: str) -> str:
    return re.sub(r"[^A-Za-z0-9_]", "_", str(name))[:32] or "unknown"


def detect_player_name(wait: float = 10.0) -> str:
    """取得目前 Minecraft 帳號名稱（Minescript player_name()），結果會快取。"""
    global _name_cache
    if _name_cache:
        return _name_cache
    try:
        from system.lib.minescript import player_name
    except ImportError:
        _name_cache = "unknown"
        return _name_cache
    deadline = time.monotonic() + wait
    while True:
        try:
            name = player_name()
            if name:
                _name_cache = sanitize(name)
                return _name_cache
        except Exception:
            pass
        if time.monotonic() >= deadline:
            _name_cache = "unknown"
            return _name_cache
        time.sleep(0.5)


# ───────────── 各帳號專屬檔案 ─────────────
def state_file(player: str) -> Path:
    return ROOT / f"Farm_watchdog_state_{player}.json"


def stop_file(player: str) -> Path:
    return ROOT / f"Farm_watchdog_{player}.stop"


def watchdog_lock_file(player: str) -> Path:
    return ROOT / f".farm_watchdog_{player}.lock"


def activity_log(player: str) -> Path:
    return ROOT / f"Farm_activity_{player}.log"


def runtime_log(player: str) -> Path:
    return ROOT / f"Farm_runtime_{player}.log"


def migrate_legacy_state(player: str) -> None:
    """第一次使用時，把舊的共用 Farm_watchdog_state.json 複製成這個帳號的狀態檔。"""
    target, legacy = state_file(player), ROOT / "Farm_watchdog_state.json"
    if not target.exists() and legacy.exists():
        try:
            target.write_bytes(legacy.read_bytes())
        except OSError:
            pass


# ───────────── 網路位址 ─────────────
def _detect_lan_ip() -> str:
    """取得本機區網 IP（UDP connect 不會真的送封包）。"""
    probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        probe.connect(("10.255.255.255", 1))
        return probe.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        probe.close()


def advertise_host() -> str:
    """登記到註冊表、讓其他設備連線用的位址。"""
    explicit = _ENV.get("FARMBOT_ADVERTISE_HOST", "").strip()
    if explicit:
        return explicit
    if CONTROL_BIND in _LOOPBACK:
        return "127.0.0.1"
    if CONTROL_BIND != "0.0.0.0":
        return CONTROL_BIND
    return _detect_lan_ip()


def local_connect_host() -> str:
    """本機自己連自己的控制埠時用的位址（Watchdog 健康檢查）。"""
    if CONTROL_BIND in _LOOPBACK or CONTROL_BIND == "0.0.0.0":
        return "127.0.0.1"
    return CONTROL_BIND


def connect_host(inst: dict) -> str:
    """連到某個登記實例時用的位址；同一台機器一律走 127.0.0.1。"""
    if inst.get("machine") == MACHINE:
        return local_connect_host()
    return str(inst.get("host") or "127.0.0.1")


# ───────────── 實例註冊表 ─────────────
def _instance_path(player: str) -> Path:
    return INSTANCE_DIR / f"{player}.json"


def register_instance(player: str, port: int) -> bool:
    try:
        INSTANCE_DIR.mkdir(parents=True, exist_ok=True)
        tmp = _instance_path(player).with_suffix(".json.tmp")
        tmp.write_text(
            json.dumps({
                "name": player, "port": int(port), "pid": os.getpid(),
                "host": advertise_host(), "machine": MACHINE, "updated": time.time(),
            }),
            encoding="utf-8",
        )
        os.replace(tmp, _instance_path(player))
        return True
    except OSError:
        return False


def name_conflict(player: str) -> str | None:
    """若另一台設備／另一個程序正以同名帳號在線，回傳說明文字（啟動時檢查，避免互相覆蓋）。"""
    try:
        data = json.loads(_instance_path(player).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    same_owner = data.get("machine") == MACHINE and data.get("pid") == os.getpid()
    fresh = (time.time() - float(data.get("updated", 0))) <= STALE_SECONDS
    if fresh and not same_owner and data.get("machine") != MACHINE:
        return f"帳號 {player} 已在設備 {data.get('machine', '?')}（{data.get('host', '?')}）上線，請勿重複登入同一帳號"
    return None


# 用「讀取端本地時間」觀察心跳是否變動，避免兩台電腦時鐘差造成誤判離線
_seen: dict[str, tuple[float, float]] = {}   # name -> (updated 值, 本地 monotonic 時間)


def _is_online(name: str, updated: float) -> bool:
    now = time.monotonic()
    prev = _seen.get(name)
    if prev is None:
        # 第一次看到：先用牆上時間粗判；之後改用變動偵測
        _seen[name] = (updated, now - max(0.0, time.time() - updated))
        return (time.time() - updated) <= STALE_SECONDS
    if prev[0] != updated:
        _seen[name] = (updated, now)
        return True
    return (now - prev[1]) <= STALE_SECONDS


def read_instance(player: str) -> dict | None:
    try:
        data = json.loads(_instance_path(player).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict) or "port" not in data:
        return None
    data.setdefault("host", "127.0.0.1")
    data.setdefault("machine", "")
    data["online"] = _is_online(player, float(data.get("updated", 0)))
    return data


def list_instances() -> list[dict]:
    """所有登記過的帳號（含離線），線上的排前面。"""
    found = []
    try:
        names = sorted(p.stem for p in INSTANCE_DIR.glob("*.json"))
    except OSError:
        names = []
    for name in names:
        info = read_instance(name)
        if info:
            found.append(info)
    found.sort(key=lambda i: (not i["online"], i["name"].lower()))
    return found


def find_instance(query: str | None) -> tuple[dict | None, str | None]:
    """依名稱（不分大小寫）挑目標；未指定時，只有一個在線帳號才自動選它。"""
    instances = list_instances()
    online = [i for i in instances if i["online"]]
    if query:
        for inst in instances:
            if inst["name"].lower() == str(query).strip().lower():
                return inst, None
        names = ", ".join(i["name"] for i in instances) or "（無）"
        return None, f"找不到帳號 `{query}`。已登記的帳號：{names}"
    if len(online) == 1:
        return online[0], None
    if not online:
        return None, "目前沒有在線的 FarmBot 帳號（Farm.py 沒有心跳）。"
    return None, "目前有多個帳號在線，請用 `player` 參數選擇：" + ", ".join(i["name"] for i in online)
