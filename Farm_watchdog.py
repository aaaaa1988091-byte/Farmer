from __future__ import annotations

"""Keep Farm.py supervised across Minescript world/server transitions."""


import json
import os
import queue
import socket
import time
import threading
import builtins
import re
from pathlib import Path

import minescript as m
from system.lib.java import eval_pyjinn_script as eps

from system.lib.minescript import EventQueue, EventType, echo, execute


ROOT = Path(__file__).resolve().parent
STATE_FILE = ROOT / "Farm_watchdog_state.json"
BAN_STATE_FILE = ROOT / "Hypixel_ban_state.json"
STOP_FILE = ROOT / "Farm_watchdog.stop"
LOCK_FILE = ROOT / ".farm_watchdog.lock"
CONTROL_HOST = "127.0.0.1"
CONTROL_PORT = 38751
POLL_SECONDS = 3.0
RECONNECT_GRACE_SECONDS = 8.0
FAILED_CHECKS_BEFORE_RESTART = 3


def read_env() -> dict[str, str]:
    values: dict[str, str] = {}
    try:
        for raw_line in (ROOT / ".env").read_text(encoding="utf-8").splitlines():
            line = raw_line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            values[key.strip()] = value.strip().strip('"').strip("'")
    except OSError:
        pass
    return values


ENV = read_env()
CONTROL_SECRET = ENV.get("FARMBOT_CONTROL_SECRET", "")
try:
    CONTROL_PORT = int(ENV.get("FARMBOT_CONTROL_PORT", str(CONTROL_PORT)))
except ValueError:
    CONTROL_PORT = 38751


def acquire_single_instance_lock():
    stream = LOCK_FILE.open("a+b")
    stream.seek(0, os.SEEK_END)
    if stream.tell() == 0:
        stream.write(b"\0")
        stream.flush()
    stream.seek(0)
    try:
        import msvcrt

        msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
    except ImportError:
        import fcntl

        try:
            fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            stream.close()
            return None
    except OSError:
        stream.close()
        return None
    return stream


def request_health(timeout: float = 2.0) -> dict | None:
    if not CONTROL_SECRET:
        return None
    payload = json.dumps(
        {"action": "health", "secret": CONTROL_SECRET},
        separators=(",", ":"),
    ).encode("utf-8") + b"\n"
    try:
        with socket.create_connection((CONTROL_HOST, CONTROL_PORT), timeout=timeout) as conn:
            conn.settimeout(timeout)
            conn.sendall(payload)
            data = bytearray()
            while len(data) < 4096:
                chunk = conn.recv(1024)
                if not chunk:
                    break
                data.extend(chunk)
                if b"\n" in chunk:
                    break
        line = bytes(data).split(b"\n", 1)[0]
        return json.loads(line.decode("utf-8")) if line else None
    except (OSError, ValueError, TimeoutError):
        return None



# Discord-to-MineScript bridge: execute() is called only by the Watchdog thread.
_DISCORD_RESTART_QUEUE = queue.Queue()

def _queue_restart_from_discord(timeout: float = 12.0) -> dict:
    pending = {"event": threading.Event(), "result": None}
    _DISCORD_RESTART_QUEUE.put(pending)
    if not pending["event"].wait(timeout):
        return {"ok": False, "error": "Watchdog 尚未回應 Farm 重啟要求，請稍後再試。"}
    return pending["result"] or {"ok": False, "error": "Watchdog 未回傳重啟結果。"}

def _process_discord_restart_queue() -> None:
    while True:
        try:
            pending = _DISCORD_RESTART_QUEUE.get_nowait()
        except queue.Empty:
            return
        try:
            health = request_health(timeout=1.0)
            if health is not None and health.get("ok"):
                result = request_farm("restart", timeout=8.0)
                if result is not None:
                    pending["result"] = result
                    continue
            try:
                state = json.loads(STATE_FILE.read_text(encoding="utf-8"))
                if not isinstance(state, dict):
                    state = {}
            except (OSError, ValueError, TypeError):
                state = {}
            resume_f8 = bool(state.get("f8", True))
            state.update(restart_pending=True, resume_f8=resume_f8, f8=resume_f8)
            temporary = STATE_FILE.with_suffix(".json.tmp")
            temporary.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")
            os.replace(temporary, STATE_FILE)
            echo(f"§eDiscord 遠端重啟 Farm.py；恢復 F8={resume_f8}")
            execute("\\\\FarmBot/Farm")
            pending["result"] = {
                "ok": True,
                "message": "Watchdog 已送出 Farm.py 啟動指令；"
                           + ("F8 將恢復開啟。" if resume_f8 else "F8 將維持關閉。")
            }
        except Exception as exc:
            pending["result"] = {
                "ok": False,
                "error": f"Watchdog 處理重啟失敗：{type(exc).__name__}: {exc}"
            }
        finally:
            pending["event"].set()


def write_restart_marker(health: dict | None, resume_f8: bool) -> None:
    try:
        state = json.loads(STATE_FILE.read_text(encoding="utf-8"))
        if not isinstance(state, dict):
            state = {}
    except (OSError, ValueError):
        state = {}
    state.update(
        restart_pending=True,
        resume_f8=resume_f8,
        run_started_at=(health or {}).get("run_started_at") or state.get("run_started_at"),
        run_deadline_at=(health or {}).get("run_deadline_at") or state.get("run_deadline_at"),
        auto_restart_at=(health or {}).get("auto_restart_at") or state.get("auto_restart_at"),
        direction=(health or {}).get("direction", state.get("direction", True)),
        f8=resume_f8,
    )
    temporary = STATE_FILE.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")
    os.replace(temporary, STATE_FILE)



# ===== Hypixel ban-screen countdown (owned by the persistent Watchdog) =====
BAN_F10_KEY = 299
BAN_POLL_INTERVAL = 0.01
BAN_STATE_POLL_INTERVAL = 0.10
BAN_ADMIN_IDS = {1554407173981737045, 999062030101254256}
_DURATION_TOKEN_RE = re.compile(r"(\d+)\s*([dhms])", re.IGNORECASE)
_DURATION_FULL_RE = re.compile(r"\s*\d+\s*[dhms](?:\s+\d+\s*[dhms])*\s*", re.IGNORECASE)
_BAN_DEADLINE_AT: float | None = None
_BAN_STATE_MTIME_NS: int | None = None
_BAN_NEXT_STATE_POLL = 0.0
_BAN_LOCAL_PAUSED = False  # F10 暫停攔截；不刪除 Discord 設定的截止時間


def parse_ban_duration(value: str) -> int:
    """Parse `1145d 15h`; also tolerate the command's visible `time:` label."""
    if not isinstance(value, str):
        raise ValueError("時間格式錯誤。請使用例如：1145d 15h 或 29d 17h 34m 51s")

    value = value.strip()
    # Discord displays this as the parameter name. Accept it if it was copied
    # into the value, but do not require users to type it.
    value = re.sub(r"^(?:/hypixel_ban\s+)?time\s*:\s*", "", value, flags=re.IGNORECASE)
    if not _DURATION_FULL_RE.fullmatch(value):
        raise ValueError("時間格式錯誤。請使用例如：1145d 15h 或 29d 17h 34m 51s")

    units = {"d": 86400, "h": 3600, "m": 60, "s": 1}
    seen: set[str] = set()
    total = 0
    for amount_text, unit_text in _DURATION_TOKEN_RE.findall(value):
        unit = unit_text.lower()
        if unit in seen:
            raise ValueError(f"時間單位 {unit} 重複，請每個單位只填一次。")
        seen.add(unit)
        total += int(amount_text) * units[unit]

    if total <= 0:
        raise ValueError("倒數時間必須大於 0。")
    return total


def format_ban_duration(seconds: float) -> str:
    remaining = max(0, int(seconds))
    days, remaining = divmod(remaining, 86400)
    hours, remaining = divmod(remaining, 3600)
    minutes, secs = divmod(remaining, 60)
    return f"{days}d {hours}h {minutes}m {secs}s"


def save_ban_deadline(duration_text: str, seconds: int, user_id: int) -> float:
    """Persist an absolute deadline; the Watchdog loop owns all game actions."""
    global _BAN_DEADLINE_AT, _BAN_STATE_MTIME_NS, _BAN_NEXT_STATE_POLL, _BAN_LOCAL_PAUSED
    _BAN_LOCAL_PAUSED = False
    now = time.time()
    deadline = now + int(seconds)
    state = {
        "deadline_at": deadline,
        "duration_text": duration_text,
        "created_at": now,
        "created_by": int(user_id),
    }
    temporary = BAN_STATE_FILE.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")
    os.replace(temporary, BAN_STATE_FILE)

    # Discord runs on its own thread. Publish the new deadline immediately,
    # while leaving mtime unset so the next Watchdog tick marks it as changed
    # and performs the first Hypixel-only disconnect if already connected.
    _BAN_DEADLINE_AT = deadline
    _BAN_STATE_MTIME_NS = None
    _BAN_NEXT_STATE_POLL = 0.0
    return deadline


def clear_ban_deadline() -> None:
    global _BAN_DEADLINE_AT, _BAN_STATE_MTIME_NS
    _BAN_DEADLINE_AT = None
    _BAN_STATE_MTIME_NS = None
    try:
        BAN_STATE_FILE.unlink()
    except FileNotFoundError:
        pass
    except OSError as exc:
        echo(f"§c無法清除 Hypixel 倒數狀態：{exc}")


def _load_ban_deadline_if_changed() -> bool:
    'Reload the absolute deadline only when its persisted file changes.'
    global _BAN_DEADLINE_AT, _BAN_STATE_MTIME_NS
    try:
        stat = BAN_STATE_FILE.stat()
        if _BAN_STATE_MTIME_NS == stat.st_mtime_ns:
            return False
        state = json.loads(BAN_STATE_FILE.read_text(encoding="utf-8"))
        deadline = float(state["deadline_at"])
        if deadline <= 0:
            raise ValueError("invalid deadline")
        _BAN_DEADLINE_AT = deadline
        _BAN_STATE_MTIME_NS = stat.st_mtime_ns
        echo(f"§eHypixel 倒數已載入：{format_ban_duration(deadline - time.time())}")
        return True
    except FileNotFoundError:
        changed = _BAN_DEADLINE_AT is not None or _BAN_STATE_MTIME_NS is not None
        _BAN_DEADLINE_AT = None
        _BAN_STATE_MTIME_NS = None
        return changed
    except (OSError, ValueError, TypeError, KeyError, json.JSONDecodeError) as exc:
        echo(f"§cHypixel 倒數狀態讀取失敗：{type(exc).__name__}: {exc}")
        return False


def _disconnect_from_hypixel(remaining_seconds: float) -> bool:
    """Poll Minecraft directly through Pyjinn and disconnect only from Hypixel."""
    countdown = format_ban_duration(remaining_seconds)
    script = r"""
mc = JavaClass("net.minecraft.client.Minecraft").getInstance()
server = mc.getCurrentServer()
result = False

if server is not None and server.ip is not None:
    address = str(server.ip).lower().strip()
    host = address.split(":")[0].rstrip(".")

    if host == "hypixel.net" or host.endswith(".hypixel.net"):
        Component = JavaClass("net.minecraft.network.chat.Component")
        Formatting = JavaClass("net.minecraft.ChatFormatting")
        line1 = Component.literal("You are temporarily banned for ").withColor(0xFF5555)
        line1 = line1.append(Component.literal("__COUNTDOWN__").withColor(0xFFFFFF))
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
        message = message.append(Component.literal("\n"))
        message = message.append(line2)
        message = message.append(Component.literal("\n"))
        message = message.append(line3)
        connection = mc.getConnection()
        if connection is not None and connection.getConnection() is not None:
            connection.getConnection().disconnect(message)
            result = True
result
""".replace("__COUNTDOWN__", countdown)
    try:
        return bool(eps(script))
    except Exception as exc:
        echo(f"§cHypixel 斷線畫面執行失敗：{type(exc).__name__}: {exc}")
        return False


def watchdog_ban_tick(force_disconnect: bool = False) -> None:
    'Maintain the deadline independently of Farm.py/test.py.'
    global _BAN_NEXT_STATE_POLL
    now_mono = time.monotonic()
    changed = False
    if now_mono >= _BAN_NEXT_STATE_POLL:
        _BAN_NEXT_STATE_POLL = now_mono + BAN_STATE_POLL_INTERVAL
        changed = _load_ban_deadline_if_changed()

    deadline = _BAN_DEADLINE_AT
    if deadline is None:
        return
    if _BAN_LOCAL_PAUSED:
        return
    remaining = deadline - time.time()
    if remaining <= 0:
        clear_ban_deadline()
        echo("§aHypixel 倒數已結束，停止攔截 Hypixel 連線")
        return

    # 不等待 WORLD 事件：每個 0.01 秒主迴圈都透過 Pyjinn
    # 直接查詢 Minecraft.getCurrentServer()，加入伺服器後立即判斷。
    if _disconnect_from_hypixel(remaining):
        echo(f"§eHypixel 倒數攔截已執行：{format_ban_duration(remaining)}")


def run_watchdog() -> None:
    if not CONTROL_SECRET:
        echo("§cFarm watchdog 未啟動：.env 缺少 FARMBOT_CONTROL_SECRET")
        return

    start_embedded_discord_bot()
    started_wall = time.time()
    connected = True  # 由 Farm.py 啟動時，Minecraft 當下已連線至世界。
    next_check_at = time.monotonic() + RECONNECT_GRACE_SECONDS
    failed_checks = 0
    last_f8 = False
    last_health: dict | None = None
    health_job: dict | None = None      # 背景 health 檢查，避免 socket timeout 卡住 ban 偵測
    next_stop_check = 0.0
    echo("Farm watchdog 已啟動，將在跨服重連後檢查 Farm.py socket")
    watchdog_ban_tick(force_disconnect=True)  # 重啟 Watchdog 後恢復已保存的絕對截止時間

    with EventQueue() as events:
        events.register_world_listener()
        events.register_key_listener()   # Farm.py 沒回應時，F9 也要能關掉 watchdog + Discord
        while True:
            _t = time.monotonic()
            if _t >= next_stop_check:
                next_stop_check = _t + 0.25
                if stop_requested(started_wall):
                    shutdown_everything("F9")
                    return
            _process_discord_restart_queue()
            try:
                event = events.get(timeout=BAN_POLL_INTERVAL)
            except queue.Empty:
                event = None

            if (
                event is not None
                and event.type == EventType.KEY
                and event.action == 1
                and event.key == BAN_F10_KEY
            ):
                global _BAN_LOCAL_PAUSED
                _BAN_LOCAL_PAUSED = not _BAN_LOCAL_PAUSED
                if _BAN_LOCAL_PAUSED:
                    echo("§eF10：暫時暫停 Hypixel 攔截；倒數仍持續，重新按 F10 可恢復")
                else:
                    echo("§aF10：已恢復 Hypixel 攔截")
                    watchdog_ban_tick(force_disconnect=True)
                continue

            if (
                event is not None
                and event.type == EventType.KEY
                and event.action == 0
                and event.key == 298
            ):
                try:
                    STOP_FILE.write_text(str(time.time()), encoding="utf-8")
                except OSError:
                    pass
                shutdown_everything("F9")
                return

            if event is not None and event.type == EventType.WORLD:
                connected = bool(event.connected)
                failed_checks = 0
                health_job = None
                if connected:
                    watchdog_ban_tick(force_disconnect=True)
                    next_check_at = time.monotonic() + RECONNECT_GRACE_SECONDS
                    echo("Farm watchdog 偵測到世界連線，等待載入後檢查 Farm.py")
                else:
                    echo("Farm watchdog 偵測到離開世界，等待重新連線")
                continue

            watchdog_ban_tick()
            now = time.monotonic()

            if health_job is None:
                if not connected or now < next_check_at:
                    continue
                next_check_at = now + POLL_SECONDS
                job = {"done": False, "result": None}

                def _health_worker(box=job) -> None:
                    try:
                        box["result"] = request_health()
                    finally:
                        box["done"] = True

                threading.Thread(target=_health_worker, name="FarmBot-Health", daemon=True).start()
                health_job = job
                continue

            if not health_job["done"]:
                continue
            health = health_job["result"]
            health_job = None
            if health is not None:
                last_health = health
                if "f8" in health:
                    last_f8 = bool(health["f8"])
                if health.get("ok"):
                    failed_checks = 0
                    continue
            else:
                try:
                    saved_state = json.loads(STATE_FILE.read_text(encoding="utf-8"))
                    if "f8" in saved_state:
                        last_f8 = bool(saved_state["f8"])
                    if saved_state.get("run_started_at"):
                        last_health = {
                            "run_started_at": saved_state.get("run_started_at"),
                            "run_deadline_at": saved_state.get("run_deadline_at"),
                            "auto_restart_at": saved_state.get("auto_restart_at"),
                            "direction": saved_state.get("direction", True),
                        }
                except (OSError, ValueError, TypeError):
                    pass

            failed_checks += 1
            if failed_checks < FAILED_CHECKS_BEFORE_RESTART:
                continue

            if not last_f8:
                echo("Farm.py socket 未回應，但離線前 F8 為關；不自動啟動農田")
                failed_checks = 0
                next_check_at = now + 15.0
                continue

            try:
                write_restart_marker(last_health, resume_f8=True)
                echo("§eFarm.py socket 連續無回應，重新啟動 Farm.py 並恢復 F8")
                execute("\\FarmBot/Farm")
                failed_checks = 0
                next_check_at = time.monotonic() + RECONNECT_GRACE_SECONDS
            except Exception as exc:
                echo(f"§cFarm watchdog 重啟 Farm.py 失敗：{type(exc).__name__}: {exc}")
                next_check_at = time.monotonic() + 10.0


def stop_requested(since_wall: float) -> bool:
    """F9 會寫入 Farm_watchdog.stop（內容為時間戳）；只認本次啟動之後寫入的。"""
    try:
        stamp = float(STOP_FILE.read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return False
    return stamp >= since_wall - 1.0


def shutdown_everything(reason: str) -> None:
    """F9：關閉 watchdog 並一併關閉 Discord bot。"""
    echo(f"§c{reason}：Farm watchdog 與 Discord bot 即將關閉")
    stop_embedded_discord_bot()


def main() -> None:
    runtime_key = "_farmbot_survival_watchdog_running"
    if getattr(builtins, runtime_key, False):
        return
    setattr(builtins, runtime_key, True)
    lock = acquire_single_instance_lock()
    if lock is None:
        setattr(builtins, runtime_key, False)
        return
    try:
        run_watchdog()
    finally:
        lock.close()
        setattr(builtins, runtime_key, False)



# ===== Discord bridge copied from original discord_control.py =====
"""Discord bridge for FarmBot. Run with the same Python installation as Minescript."""

import asyncio
import json
import os
import socket
from datetime import datetime, timedelta, timezone
from pathlib import Path

import discord
from discord import app_commands
from discord.ext import commands, tasks


ROOT = Path(__file__).resolve().parent
ENV_FILE = ROOT / ".env"
TARGET_CHANNEL_ID = 1554414633627160656   # 可在 .env 用 DISCORD_CHANNEL_ID 覆蓋（更新腳本時不會被蓋掉）


def load_env_file() -> None:
    if not ENV_FILE.exists():
        return
    for raw_line in ENV_FILE.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key:
            os.environ.setdefault(key, value)


load_env_file()
try:
    TARGET_CHANNEL_ID = int(os.environ.get("DISCORD_CHANNEL_ID", "") or TARGET_CHANNEL_ID)
except ValueError:
    pass
BOT_TOKEN = os.environ.get("DISCORD_BOT_TOKEN", "").strip()
CONTROL_SECRET = os.environ.get("FARMBOT_CONTROL_SECRET", "").strip()
ALLOWED_USER_IDS = {
    int(value.strip())
    for value in os.environ.get("DISCORD_ALLOWED_USER_IDS", "").split(",")
    if value.strip().isdigit()
}


def request_farm(action: str, timeout: float = 8.0, **parameters) -> dict:
    if not CONTROL_SECRET:
        return {"ok": False, "error": "FARMBOT_CONTROL_SECRET is not configured"}

    payload = json.dumps(
        {"action": action, "secret": CONTROL_SECRET, **parameters},
        ensure_ascii=False,
    ).encode("utf-8") + b"\n"
    try:
        with socket.create_connection((CONTROL_HOST, CONTROL_PORT), timeout=timeout) as conn:
            conn.settimeout(timeout)
            conn.sendall(payload)
            chunks = bytearray()
            while len(chunks) < 65536:
                part = conn.recv(4096)
                if not part:
                    break
                chunks.extend(part)
                if b"\n" in part:
                    break
        line = bytes(chunks).split(b"\n", 1)[0]
        return json.loads(line.decode("utf-8")) if line else {"ok": False, "error": "empty response"}
    except Exception as exc:
        if action == "restart":
            return _queue_restart_from_discord()
        return {"ok": False, "error": f"Farm.py is not responding: {type(exc).__name__}: {exc}"}


intents = discord.Intents.none()
bot = commands.Bot(command_prefix=commands.when_mentioned, intents=intents)
last_delivery_failed = False


async def is_authorized(interaction: discord.Interaction) -> bool:
    if interaction.channel_id != TARGET_CHANNEL_ID:
        await interaction.response.send_message("這個指令只能在指定的 FarmBot 控制房間使用。", ephemeral=True)
        return False
    if not ALLOWED_USER_IDS or interaction.user.id not in ALLOWED_USER_IDS:
        await interaction.response.send_message(
            "此帳號尚未列入 `DISCORD_ALLOWED_USER_IDS` 白名單，操作已拒絕。",
            ephemeral=True,
        )
        return False
    return True


@bot.tree.command(name="hypixel_ban_stop", description="遠端停止 Hypixel 斷線倒數")
async def hypixel_ban_stop(interaction: discord.Interaction) -> None:
    if interaction.user.id not in BAN_ADMIN_IDS:
        await interaction.response.send_message("你沒有使用此指令的權限。", ephemeral=True)
        return

    clear_ban_deadline()
    global _BAN_LOCAL_PAUSED
    _BAN_LOCAL_PAUSED = False
    await interaction.response.send_message(
        "已遠端停止 Hypixel 倒數攔截，並清除保存的截止時間。"
    )


@bot.tree.command(name="hypixel_ban", description="設定 Hypixel 斷線倒數")
@app_commands.rename(duration="time")
@app_commands.describe(duration="倒數時間，例如 1145d 15h 或 29d 17h 34m 51s")
async def hypixel_ban(interaction: discord.Interaction, duration: str) -> None:
    if interaction.user.id not in BAN_ADMIN_IDS:
        await interaction.response.send_message("你沒有使用此指令的權限。", ephemeral=True)
        return

    try:
        seconds = parse_ban_duration(duration)
    except ValueError as exc:
        await interaction.response.send_message(str(exc), ephemeral=True)
        return

    deadline = save_ban_deadline(duration, seconds, interaction.user.id)
    await interaction.response.send_message(
        f"Hypixel 倒數已設定：`{format_ban_duration(seconds)}`\n"
        f"截止時間：`<t:{int(deadline)}:F>`\n"
        "Watchdog 會獨立維護倒數；F10 可解除攔截。",
        ephemeral=True,
    )


def _discord_log(message: str) -> None:
    print(message)
    try:
        with (ROOT / "Farm_watchdog_discord.log").open("a", encoding="utf-8") as stream:
            stream.write(f"{datetime.now().isoformat(timespec='seconds')} {message}\n")
    except OSError:
        pass


@bot.event
async def on_ready() -> None:
    try:
        channel = bot.get_channel(TARGET_CHANNEL_ID)
        if channel is None:
            channel = await bot.fetch_channel(TARGET_CHANNEL_ID)
        guild = getattr(channel, "guild", None)
        if guild is not None:
            bot.tree.copy_global_to(guild=guild)
            synced = await bot.tree.sync(guild=guild)
        else:
            synced = await bot.tree.sync()
        _discord_log(f"指令同步成功：{', '.join(c.name for c in synced)}")
    except Exception as exc:
        _discord_log(f"指令同步失敗：{type(exc).__name__}: {exc}")
    if not getattr(bot, "_panel_view_registered", False):
        bot.add_view(PanelView())     # 讓重啟後舊面板的按鈕仍可使用
        bot._panel_view_registered = True
    if not periodic_report.is_running():
        periodic_report.start()
    print(f"FarmBot Discord bridge ready as {bot.user}")


@bot.tree.command(name="farm_toggle", description="遠端切換 Farm.py 的 F8 開關")
async def farm_toggle(interaction: discord.Interaction) -> None:
    if not await is_authorized(interaction):
        return
    await interaction.response.defer(ephemeral=True, thinking=True)
    result = await asyncio.to_thread(request_farm, "toggle")
    await interaction.followup.send(
        result.get("message", result.get("error", "操作失敗")),
        ephemeral=True,
    )


@bot.tree.command(name="farm_restart", description="遠端重新啟動 Farm.py")
async def farm_restart(interaction: discord.Interaction) -> None:
    if not await is_authorized(interaction):
        return
    await interaction.response.defer(ephemeral=True, thinking=True)
    result = await asyncio.to_thread(request_farm, "restart")
    await interaction.followup.send(
        result.get("message", result.get("error", "操作失敗")),
        ephemeral=True,
    )


@bot.tree.command(name="farm_status", description="查看 Farm.py 開關與工作狀態")
async def farm_status(interaction: discord.Interaction) -> None:
    if not await is_authorized(interaction):
        return
    await interaction.response.defer(ephemeral=True, thinking=True)
    result = await asyncio.to_thread(request_farm, "status")
    await interaction.followup.send(
        result.get("message", result.get("error", "操作失敗")),
        ephemeral=True,
    )


@bot.tree.command(name="farm_add_time", description="延長目前 Farm.py 的運行時間")
@app_commands.describe(minutes="要增加的分鐘數（1 到 1440）")
async def farm_add_time(interaction: discord.Interaction, minutes: app_commands.Range[int, 1, 1440]) -> None:
    if not await is_authorized(interaction):
        return
    await interaction.response.defer(ephemeral=True, thinking=True)
    result = await asyncio.to_thread(request_farm, "add_time", minutes=minutes)
    await interaction.followup.send(
        result.get("message", result.get("error", "操作失敗")),
        ephemeral=True,
    )


@bot.tree.command(name="farm_return_target", description="設定回程 AOTV 目標點（存入 json；不填任何參數則顯示目前值）")
@app_commands.describe(
    plot="回程先傳送到的 Plot（1 到 24，預設 13）",
    block="AOTV 要瞄準的方塊 id，例如 minecraft:gold_block（填 none 停用，改回座標瞄準）",
    crosshair="把準心目前對準的方塊設為回程瞄準方塊",
    radius="掃描周圍方塊的半徑（4 到 32，預設 16）",
    z="到位判定用的 Z（預設 -144.5）；未設定方塊時也是瞄準座標",
    y="（座標模式）看向的 Y（預設 66）",
    x_min="（座標模式）看向的 X 隨機範圍下限（預設 -95）",
    x_max="（座標模式）看向的 X 隨機範圍上限（預設 -80）",
    reset="還原所有預設值（視角與回程目標）",
)
async def farm_return_target(
    interaction: discord.Interaction,
    plot: app_commands.Range[int, 1, 24] | None = None,
    block: str | None = None,
    crosshair: bool = False,
    radius: app_commands.Range[int, 4, 32] | None = None,
    z: float | None = None,
    y: float | None = None,
    x_min: int | None = None,
    x_max: int | None = None,
    reset: bool = False,
) -> None:
    if not await is_authorized(interaction):
        return
    await interaction.response.defer(ephemeral=True, thinking=True)
    values = {"plot": plot, "block": block, "radius": radius, "z": z, "y": y, "x_min": x_min, "x_max": x_max}
    values = {key: value for key, value in values.items() if value is not None}
    if crosshair:
        values["crosshair"] = True
    if reset:
        result = await asyncio.to_thread(request_farm, "set_view", reset=True)
    elif values:
        result = await asyncio.to_thread(request_farm, "set_view", **values)
    else:
        result = await asyncio.to_thread(request_farm, "get_view")
    await interaction.followup.send(
        result.get("message", result.get("error", "操作失敗")),
        ephemeral=True,
    )


# ═════════════════════════════════════════════
# 完整控制面板：/farm_panel
# ═════════════════════════════════════════════
PANEL_FEATURES = (
    ("pest", "除蟲"),
    ("equipment", "換裝"),
    ("sell", "賣出/合成"),
    ("spray", "噴霧"),
    ("bonus", "Bonus"),
)


async def fetch_panel() -> tuple[dict | None, str | None]:
    result = await asyncio.to_thread(request_farm, "panel")
    if result.get("ok") and isinstance(result.get("info"), dict):
        return result["info"], None
    return None, result.get("error") or result.get("message") or "未知錯誤"


def _health_text(info: dict) -> str:
    low, high = (info.get("pest_health") or [None, None])[:2]
    if low is None or high is None:
        return "預設（一般 100~2000；測試模式任何 HP > 0）"
    return f"{low:g} < HP < {high:g}"


def build_panel_embed(info: dict | None, error: str | None = None) -> discord.Embed:
    if info is None:
        return discord.Embed(
            title="FarmBot 控制面板",
            description=f"❌ Farm.py 沒有回應\n{error or ''}".strip(),
            color=0xE74C3C,
        )
    embed = discord.Embed(title="FarmBot 控制面板", color=0x2ECC71 if info.get("f8") else 0x95A5A6)
    anti_fail = info.get("anti_fail")
    state_lines = [
        f"F8：{'🟢 開' if info.get('f8') else '⚪ 關'}",
        f"農業：{'進行中' if info.get('farming') else '暫停'} · 除蟲：{'進行中' if info.get('pest_active') else '否'}",
        f"計時剩餘：{info.get('remaining') or '尚未開始計時'}",
        f"Anti-Fail：{'🔴 鎖定 — ' + str(anti_fail) if anti_fail else '正常'}",
        f"測試模式：{'🧪 開（單人存檔）' if info.get('test_mode') else '關'}",
    ]
    embed.add_field(name="狀態", value="\n".join(state_lines), inline=False)
    features = info.get("features") or {}
    embed.add_field(
        name="功能開關",
        value=" · ".join(f"{'🟢' if features.get(key, True) else '⚪'} {label}" for key, label in PANEL_FEATURES),
        inline=False,
    )
    embed.add_field(name="蟲血量範圍", value=_health_text(info), inline=True)
    target = info.get("return_target") or {}
    embed.add_field(
        name="視角／回程",
        value=(
            f"yaw {info.get('yaw'):g} / pitch "
            + (f"{info.get('pitch'):g}" if info.get("pitch") is not None else "?")
            + (" (糾正物品)" if info.get("view_live") else " (上次記錄)")
            + f" · Plot {target.get('plot')} · "
            + (f"瞄準 {target['block']}（r={target.get('radius')}）" if target.get("block") else "座標瞄準")
        ) if info.get("yaw") is not None else "—",
        inline=True,
    )
    taiwan_time = timezone(timedelta(hours=8), name="Taiwan")
    embed.set_footer(text=datetime.now(taiwan_time).strftime("更新於 %H:%M:%S 台灣時間"))
    return embed


class PanelButton(discord.ui.Button):
    def __init__(self, key: str, label: str, style: discord.ButtonStyle, row: int) -> None:
        super().__init__(label=label, style=style, custom_id=f"farmpanel:{key}", row=row)
        self.key = key

    async def callback(self, interaction: discord.Interaction) -> None:
        await panel_dispatch(interaction, self.key)


class PanelView(discord.ui.View):
    def __init__(self, info: dict | None = None) -> None:
        super().__init__(timeout=None)
        info = info or {}
        features = info.get("features") or {}
        f8_on = bool(info.get("f8"))
        S = discord.ButtonStyle
        self.add_item(PanelButton("f8", "F8 關閉" if f8_on else "F8 開啟", S.danger if f8_on else S.success, 0))
        self.add_item(PanelButton("refresh", "重新整理", S.secondary, 0))
        self.add_item(PanelButton("restart", "重啟 Farm", S.secondary, 0))
        for key, label in PANEL_FEATURES:
            on = features.get(key, True)
            self.add_item(PanelButton(f"feat:{key}", f"{label} {'ON' if on else 'OFF'}", S.success if on else S.secondary, 1))
        test_on = bool(info.get("test_mode"))
        self.add_item(PanelButton("test", f"測試模式 {'ON' if test_on else 'OFF'}", S.primary if test_on else S.secondary, 2))
        self.add_item(PanelButton("pestnow", "立即除蟲", S.primary, 2))
        self.add_item(PanelButton("antifail", "解除 Anti-Fail", S.secondary, 2))
        self.add_item(PanelButton("add30", "加時 30 分", S.secondary, 2))
        self.add_item(PanelButton("health", "蟲血量", S.secondary, 2))

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        return await is_authorized(interaction)


class PestNowModal(discord.ui.Modal, title="立即除蟲"):
    plot = discord.ui.TextInput(label="Plot（1~24；測試模式可填 0＝目前位置）", default="0", max_length=2)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            plot = int(str(self.plot.value).strip())
        except ValueError:
            await interaction.followup.send("Plot 必須是數字。", ephemeral=True)
            return
        result = await asyncio.to_thread(request_farm, "pest_now", plot=plot)
        await interaction.followup.send(result.get("message", result.get("error", "操作失敗")), ephemeral=True)


class PestHealthModal(discord.ui.Modal, title="蟲血量範圍"):
    low = discord.ui.TextInput(label="下限（HP 必須大於此值）", placeholder="例：0（兩格都留空＝還原預設）", required=False, max_length=9)
    high = discord.ui.TextInput(label="上限（HP 必須小於此值）", placeholder="例：1000000", required=False, max_length=12)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)
        low_text, high_text = str(self.low.value).strip(), str(self.high.value).strip()
        if not low_text and not high_text:
            result = await asyncio.to_thread(request_farm, "set_pest_health", reset=True)
        else:
            try:
                low, high = float(low_text), float(high_text)
            except ValueError:
                await interaction.followup.send("下限與上限都要填數字（或兩格都留空還原）。", ephemeral=True)
                return
            result = await asyncio.to_thread(request_farm, "set_pest_health", min=low, max=high)
        await interaction.followup.send(result.get("message", result.get("error", "操作失敗")), ephemeral=True)


async def panel_dispatch(interaction: discord.Interaction, key: str, value=None) -> None:
    if key == "pestnow":
        await interaction.response.send_modal(PestNowModal())
        return
    if key == "health":
        await interaction.response.send_modal(PestHealthModal())
        return

    await interaction.response.defer()
    result: dict = {"ok": True}
    if key == "f8":
        result = await asyncio.to_thread(request_farm, "toggle")
    elif key == "restart":
        result = await asyncio.to_thread(request_farm, "restart", 15.0)
    elif key.startswith("feat:"):
        result = await asyncio.to_thread(request_farm, "set_feature", name=key.split(":", 1)[1], value="toggle")
    elif key == "test":
        result = await asyncio.to_thread(request_farm, "set_test_mode", value="toggle")
    elif key == "antifail":
        result = await asyncio.to_thread(request_farm, "resume_anti_fail")
    elif key == "add30":
        result = await asyncio.to_thread(request_farm, "add_time", minutes=30)

    if key == "restart":
        await asyncio.sleep(2.5)   # 新 job 需要一點時間開 socket
    info, error = await fetch_panel()
    try:
        await interaction.edit_original_response(embed=build_panel_embed(info, error), view=PanelView(info))
    except discord.HTTPException as exc:
        print(f"Panel refresh failed: {type(exc).__name__}: {exc}")
    if key != "refresh":
        message = result.get("message") or result.get("error")
        if message:
            await interaction.followup.send(message, ephemeral=True)


@bot.tree.command(name="farm_panel", description="開啟 FarmBot 完整控制面板（按鈕＋測試模式）")
async def farm_panel(interaction: discord.Interaction) -> None:
    if not await is_authorized(interaction):
        return
    await interaction.response.defer(thinking=True)
    info, error = await fetch_panel()
    await interaction.followup.send(embed=build_panel_embed(info, error), view=PanelView(info))


@tasks.loop(seconds=60)
async def periodic_report() -> None:
    global last_delivery_failed
    channel = bot.get_channel(TARGET_CHANNEL_ID)
    if channel is None:
        try:
            channel = await bot.fetch_channel(TARGET_CHANNEL_ID)
        except Exception as exc:
            print(f"Cannot access report channel: {type(exc).__name__}: {exc}")
            return

    result = await asyncio.to_thread(request_farm, "screenshot", 12.0)
    # Minescript writes screenshots asynchronously in some runtime builds.
    image_path = Path(result["screenshot"]) if result.get("screenshot") else None
    if image_path:
        for _ in range(10):
            if image_path.is_file():
                break
            await asyncio.sleep(0.2)
    taiwan_time = timezone(timedelta(hours=8), name="Taiwan")
    timestamp = datetime.now(taiwan_time).strftime("%Y-%m-%d %H:%M:%S 台灣時間")
    lines = [f"FarmBot 快照 · {timestamp}"]
    scoreboard = result.get("scoreboard")
    if isinstance(scoreboard, list) and scoreboard:
        lines.extend(["記分板：", *scoreboard])
    else:
        lines.append("記分板：本次讀取不到")
    if result.get("error"):
        lines.append(f"截圖狀態：{result['error']}")

    content = "\n".join(lines)
    if len(content) > 1850:
        content = content[:1847] + "..."

    try:
        if image_path and image_path.is_file():
            await channel.send(content=content, file=discord.File(image_path, filename="farmbot.png"))
            try:
                image_path.unlink()
            except OSError:
                pass
        else:
            await channel.send(content=content)
        last_delivery_failed = False
    except Exception as exc:
        last_delivery_failed = True
        print(f"Periodic report failed: {type(exc).__name__}: {exc}")


@periodic_report.before_loop
async def before_periodic_report() -> None:
    await bot.wait_until_ready()


def acquire_discord_single_instance_lock():
    lock_path = ROOT / ".discord_control.lock"
    stream = lock_path.open("a+b")
    stream.seek(0)
    try:
        import msvcrt

        msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
    except ImportError:
        import fcntl

        fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        stream.close()
        return None
    return stream



_DISCORD_LOOP = None
_DISCORD_THREAD = None


def start_embedded_discord_bot() -> None:
    """Start the original discord_control bot under the Watchdog process."""
    if not BOT_TOKEN:
        echo("§cDiscord Bot 未啟動：.env 缺少 DISCORD_BOT_TOKEN")
        return
    if not CONTROL_SECRET:
        echo("§cDiscord Bot 未啟動：.env 缺少 FARMBOT_CONTROL_SECRET")
        return

    def discord_worker() -> None:
        import traceback

        async def run_bot() -> None:
            global _DISCORD_LOOP
            _DISCORD_LOOP = asyncio.get_running_loop()
            await bot.start(BOT_TOKEN)

        try:
            # Keep the original Bot, commands and event handlers.
            # bot.run() installs signal handlers and expects the main thread.
            asyncio.run(run_bot())
        except BaseException as exc:
            message = f"Discord Bot 執行失敗：{type(exc).__name__}: {exc}"
            print(message)
            try:
                with (ROOT / "Farm_watchdog_discord.log").open("a", encoding="utf-8") as stream:
                    stream.write(message + "\\n")
                    stream.write(traceback.format_exc() + "\\n")
            except OSError:
                pass

    global _DISCORD_THREAD
    _DISCORD_THREAD = threading.Thread(target=discord_worker, name="FarmBot-Discord", daemon=True)
    _DISCORD_THREAD.start()


def stop_embedded_discord_bot() -> None:
    """Gracefully close the Discord bot (used by F9 only)."""
    loop = _DISCORD_LOOP
    if loop is not None and loop.is_running():
        async def _close() -> None:
            try:
                if periodic_report.is_running():
                    periodic_report.cancel()
            finally:
                await bot.close()
        try:
            asyncio.run_coroutine_threadsafe(_close(), loop).result(timeout=10)
        except Exception as exc:
            _discord_log(f"關閉 Discord bot 失敗：{type(exc).__name__}: {exc}")
    thread = _DISCORD_THREAD
    if thread is not None:
        thread.join(timeout=5.0)
    echo("Discord bot 已關閉")


if __name__ == "__main__":
    main()