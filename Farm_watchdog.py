from __future__ import annotations

"""Keep Farm.py supervised across Minescript world/server transitions."""


import json
import os
import queue
import socket
import time
import threading
import builtins
from pathlib import Path

from system.lib.minescript import EventQueue, EventType, echo, execute


ROOT = Path(__file__).resolve().parent
STATE_FILE = ROOT / "Farm_watchdog_state.json"
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


def run_watchdog() -> None:
    if not CONTROL_SECRET:
        echo("§cFarm watchdog 未啟動：.env 缺少 FARMBOT_CONTROL_SECRET")
        return

    start_embedded_discord_bot()
    connected = True  # 由 Farm.py 啟動時，Minecraft 當下已連線至世界。
    next_check_at = time.monotonic() + RECONNECT_GRACE_SECONDS
    failed_checks = 0
    last_f8 = False
    last_health: dict | None = None
    echo("Farm watchdog 已啟動，將在跨服重連後檢查 Farm.py socket")

    with EventQueue() as events:
        events.register_world_listener()
        while True:
            _process_discord_restart_queue()
            try:
                event = events.get(timeout=0.5)
            except queue.Empty:
                event = None


            if event is not None and event.type == EventType.WORLD:
                connected = bool(event.connected)
                failed_checks = 0
                if connected:
                    next_check_at = time.monotonic() + RECONNECT_GRACE_SECONDS
                    echo("Farm watchdog 偵測到世界連線，等待載入後檢查 Farm.py")
                else:
                    echo("Farm watchdog 偵測到離開世界，等待重新連線")
                continue

            now = time.monotonic()
            if not connected or now < next_check_at:
                continue
            next_check_at = now + POLL_SECONDS

            health = request_health()
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


@bot.tree.command(name="farm_yaw", description="選擇視角校正的目標 yaw（會存入 json）")
@app_commands.describe(yaw="目標 yaw")
@app_commands.choices(
    yaw=[
        app_commands.Choice(name="180（預設）", value=180),
        app_commands.Choice(name="90", value=90),
        app_commands.Choice(name="0", value=0),
        app_commands.Choice(name="-90", value=-90),
    ]
)
async def farm_yaw(interaction: discord.Interaction, yaw: app_commands.Choice[int]) -> None:
    if not await is_authorized(interaction):
        return
    await interaction.response.defer(ephemeral=True, thinking=True)
    result = await asyncio.to_thread(request_farm, "set_view", yaw=yaw.value)
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
PANEL_YAWS = (180, 90, 0, -90)


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
            f"yaw {info.get('yaw'):g} · Plot {target.get('plot')} · "
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


class YawSelect(discord.ui.Select):
    def __init__(self, current: float | None) -> None:
        options = [
            discord.SelectOption(
                label=f"視角校正 yaw {value}",
                value=str(value),
                default=current is not None and float(current) == float(value),
            )
            for value in PANEL_YAWS
        ]
        super().__init__(placeholder="視角校正目標 yaw", options=options, custom_id="farmpanel:yaw", row=3)

    async def callback(self, interaction: discord.Interaction) -> None:
        await panel_dispatch(interaction, "yaw", value=int(self.values[0]))


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
        self.add_item(YawSelect(info.get("yaw")))

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
    elif key == "yaw":
        result = await asyncio.to_thread(request_farm, "set_view", yaw=value)

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
        try:
            # Keep the original Bot, commands and event handlers.
            # bot.run() installs signal handlers and expects the main thread.
            asyncio.run(bot.start(BOT_TOKEN))
        except BaseException as exc:
            message = f"Discord Bot 執行失敗：{type(exc).__name__}: {exc}"
            print(message)
            try:
                with (ROOT / "Farm_watchdog_discord.log").open("a", encoding="utf-8") as stream:
                    stream.write(message + "\\n")
                    stream.write(traceback.format_exc() + "\\n")
            except OSError:
                pass

    threading.Thread(target=discord_worker, name="FarmBot-Discord", daemon=True).start()


if __name__ == "__main__":
    main()
