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
import farm_instance as fi


ROOT = Path(__file__).resolve().parent
PLAYER = fi.detect_player_name()          # 這個 Minecraft 視窗的帳號名稱（多開用）
fi.migrate_legacy_state(PLAYER)
STATE_FILE = fi.state_file(PLAYER)
STOP_FILE = fi.stop_file(PLAYER)
LOCK_FILE = fi.watchdog_lock_file(PLAYER)
CONTROL_HOST = fi.local_connect_host()   # 自己連自己的埠；跨設備連線由 fi.connect_host(instance) 決定
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
        {"action": "health", "secret": CONTROL_SECRET, "player": PLAYER},
        separators=(",", ":"),
    ).encode("utf-8") + b"\n"
    instance = fi.read_instance(PLAYER)
    if instance is None:
        return None
    try:
        with socket.create_connection((CONTROL_HOST, int(instance["port"])), timeout=timeout) as conn:
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



# Discord-to-MineScript bridge：execute() 只能由「該帳號自己的」Watchdog 呼叫。
# bot 可能跑在別的帳號的視窗裡，所以用檔案傳遞重啟要求：
#   instances/<玩家>.restart_req（bot 寫入）→ 該帳號 Watchdog 處理 → instances/<玩家>.restart_res
def _restart_req_path(player: str) -> Path:
    return fi.INSTANCE_DIR / f"{player}.restart_req"


def _restart_res_path(player: str) -> Path:
    return fi.INSTANCE_DIR / f"{player}.restart_res"


def _queue_restart_from_discord(player: str, timeout: float = 12.0) -> dict:
    try:
        fi.INSTANCE_DIR.mkdir(parents=True, exist_ok=True)
        request_id = str(time.time_ns())
        _restart_res_path(player).unlink(missing_ok=True)
        _restart_req_path(player).write_text(request_id, encoding="utf-8")
    except OSError as exc:
        return {"ok": False, "error": f"無法送出重啟要求：{exc}"}
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            data = json.loads(_restart_res_path(player).read_text(encoding="utf-8"))
            if data.get("id") == request_id:
                return data.get("result") or {"ok": False, "error": "Watchdog 未回傳重啟結果。"}
        except (OSError, ValueError):
            pass
        time.sleep(0.3)
    return {"ok": False, "error": f"{player} 的 Watchdog 尚未回應 Farm 重啟要求，請稍後再試。"}


def _process_discord_restart_queue() -> None:
    req_path = _restart_req_path(PLAYER)
    try:
        request_id = req_path.read_text(encoding="utf-8").strip()
        req_path.unlink()
    except OSError:
        return
    result: dict = {}
    try:
        health = request_health(timeout=1.0)
        if health is not None and health.get("ok"):
            farm_result = request_farm(PLAYER, "restart", timeout=8.0, fallback=False)
            if farm_result.get("ok") or "not responding" not in str(farm_result.get("error", "")):
                result = farm_result
        if not result:
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
            execute("\\FarmBot/Farm")
            result = {
                "ok": True,
                "message": "Watchdog 已送出 Farm.py 啟動指令；"
                           + ("F8 將恢復開啟。" if resume_f8 else "F8 將維持關閉。"),
            }
    except Exception as exc:
        result = {"ok": False, "error": f"Watchdog 處理重啟失敗：{type(exc).__name__}: {exc}"}
    try:
        _restart_res_path(PLAYER).write_text(json.dumps({"id": request_id, "result": result}, ensure_ascii=False), encoding="utf-8")
    except OSError:
        pass


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

    start_embedded_discord_bot()   # 多開時只有搶到鎖的那個視窗會真的跑 bot，其餘持續待命接手
    connected = True  # 由 Farm.py 啟動時，Minecraft 當下已連線至世界。
    next_check_at = time.monotonic() + RECONNECT_GRACE_SECONDS
    failed_checks = 0
    last_f8 = False
    last_health: dict | None = None
    echo(f"Farm watchdog 已啟動（帳號 {PLAYER}），將在跨服重連後檢查 Farm.py socket")

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



# ═════════════════════════════════════════════
# Discord bridge（多帳號版）
#   - 一支 bot 控制多個 Minecraft 視窗；每個視窗的 Farm.py 以玩家名稱登記在 instances/
#   - 每個指令都有 player 參數（可自動完成）；只有一個帳號在線時可省略
#   - /farm_panel 為每個帳號各開一個面板，面板上的按鈕只作用於該帳號
#   - bot 本體只會在一個視窗執行（檔案鎖）；該視窗關掉後，其他視窗的 Watchdog 會接手
# ═════════════════════════════════════════════
import asyncio
from datetime import datetime, timedelta, timezone

import discord
from discord import app_commands
from discord.ext import commands, tasks


ENV_FILE = ROOT / ".env"
TARGET_CHANNEL_ID = 1554414633627160656


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
BOT_TOKEN = os.environ.get("DISCORD_BOT_TOKEN", "").strip()
CONTROL_SECRET = os.environ.get("FARMBOT_CONTROL_SECRET", "").strip()
ALLOWED_USER_IDS = {
    int(value.strip())
    for value in os.environ.get("DISCORD_ALLOWED_USER_IDS", "").split(",")
    if value.strip().isdigit()
}


def request_farm(player: str, action: str, timeout: float = 8.0, fallback: bool = True, **parameters) -> dict:
    """對指定帳號的 Farm.py 送出控制要求。"""
    if not CONTROL_SECRET:
        return {"ok": False, "error": "FARMBOT_CONTROL_SECRET is not configured"}
    instance = fi.read_instance(player)
    if instance is None:
        return {"ok": False, "error": f"找不到 {player} 的控制埠（該帳號尚未啟動 Farm.py）"}

    payload = json.dumps(
        {"action": action, "secret": CONTROL_SECRET, "player": player, **parameters},
        ensure_ascii=False,
    ).encode("utf-8") + b"\n"
    try:
        with socket.create_connection((fi.connect_host(instance), int(instance["port"])), timeout=timeout) as conn:
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
        if action == "restart" and fallback:
            return _queue_restart_from_discord(player)
        return {"ok": False, "error": f"Farm.py is not responding: {type(exc).__name__}: {exc}"}


intents = discord.Intents.none()
bot = commands.Bot(command_prefix=commands.when_mentioned, intents=intents)
_report_ok: dict[str, bool] = {}


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


async def player_autocomplete(interaction: discord.Interaction, current: str) -> list[app_commands.Choice[str]]:
    needle = current.lower()
    choices = []
    for inst in fi.list_instances():
        if needle in inst["name"].lower():
            label = inst["name"] if inst["online"] else f"{inst['name']}（離線）"
            choices.append(app_commands.Choice(name=label, value=inst["name"]))
    return choices[:25]


PLAYER_HELP = "要控制的 Minecraft 帳號（只有一個帳號在線時可省略）"


async def run_on_player(
    interaction: discord.Interaction,
    player: str | None,
    action: str,
    timeout: float = 8.0,
    **params,
) -> None:
    """共用流程：驗證 → 選對象 → 送指令 → 回覆（訊息前面標示帳號名稱）。"""
    if not await is_authorized(interaction):
        return
    await interaction.response.defer(ephemeral=True, thinking=True)
    inst, error = fi.find_instance(player)
    if inst is None:
        await interaction.followup.send(error, ephemeral=True)
        return
    name = inst["name"]
    result = await asyncio.to_thread(request_farm, name, action, timeout, **params)
    text = result.get("message", result.get("error", "操作失敗"))
    await interaction.followup.send(f"**[{name}]** {text}", ephemeral=True)


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


@bot.tree.command(name="farm_list", description="列出偵測到的所有 Minecraft 帳號與狀態")
async def farm_list(interaction: discord.Interaction) -> None:
    if not await is_authorized(interaction):
        return
    await interaction.response.defer(ephemeral=True, thinking=True)
    instances = fi.list_instances()
    if not instances:
        await interaction.followup.send("尚未偵測到任何帳號（還沒有 Farm.py 登記過）。", ephemeral=True)
        return

    async def one(inst: dict):
        if not inst["online"]:
            return inst, None
        res = await asyncio.to_thread(request_farm, inst["name"], "panel", 4.0)
        return inst, (res.get("info") if res.get("ok") else None)

    rows = await asyncio.gather(*(one(inst) for inst in instances))
    lines = []
    for inst, info in rows:
        if info:
            extras = ""
            if info.get("test_mode"):
                extras += " · 🧪測試模式"
            if info.get("anti_fail"):
                extras += " · 🔴Anti-Fail"
            lines.append(
                f"🟢 **{inst['name']}** · F8 {'開' if info.get('f8') else '關'}"
                f" · 農業 {'進行中' if info.get('farming') else '暫停'}"
                f" · 剩餘 {info.get('remaining') or '—'}{extras}"
            )
        elif inst["online"]:
            lines.append(f"🟡 **{inst['name']}** · 有心跳但沒有回應")
        else:
            lines.append(f"⚫ **{inst['name']}** · 離線")
    embed = discord.Embed(title="FarmBot 帳號列表", description="\n".join(lines), color=0x3498DB)
    await interaction.followup.send(embed=embed, ephemeral=True)


@bot.tree.command(name="farm_toggle", description="遠端切換 Farm.py 的 F8 開關")
@app_commands.describe(player=PLAYER_HELP)
@app_commands.autocomplete(player=player_autocomplete)
async def farm_toggle(interaction: discord.Interaction, player: str | None = None) -> None:
    await run_on_player(interaction, player, "toggle")


@bot.tree.command(name="farm_restart", description="遠端重新啟動 Farm.py")
@app_commands.describe(player=PLAYER_HELP)
@app_commands.autocomplete(player=player_autocomplete)
async def farm_restart(interaction: discord.Interaction, player: str | None = None) -> None:
    await run_on_player(interaction, player, "restart", 15.0)


@bot.tree.command(name="farm_status", description="查看 Farm.py 開關與工作狀態")
@app_commands.describe(player=PLAYER_HELP)
@app_commands.autocomplete(player=player_autocomplete)
async def farm_status(interaction: discord.Interaction, player: str | None = None) -> None:
    await run_on_player(interaction, player, "status")


@bot.tree.command(name="farm_add_time", description="延長目前 Farm.py 的運行時間")
@app_commands.describe(minutes="要增加的分鐘數（1 到 1440）", player=PLAYER_HELP)
@app_commands.autocomplete(player=player_autocomplete)
async def farm_add_time(
    interaction: discord.Interaction,
    minutes: app_commands.Range[int, 1, 1440],
    player: str | None = None,
) -> None:
    await run_on_player(interaction, player, "add_time", minutes=minutes)


@bot.tree.command(name="farm_yaw", description="選擇視角校正的目標 yaw（會存入該帳號的 json）")
@app_commands.describe(yaw="目標 yaw", player=PLAYER_HELP)
@app_commands.autocomplete(player=player_autocomplete)
@app_commands.choices(
    yaw=[
        app_commands.Choice(name="180（預設）", value=180),
        app_commands.Choice(name="90", value=90),
        app_commands.Choice(name="0", value=0),
        app_commands.Choice(name="-90", value=-90),
    ]
)
async def farm_yaw(
    interaction: discord.Interaction,
    yaw: app_commands.Choice[int],
    player: str | None = None,
) -> None:
    await run_on_player(interaction, player, "set_view", yaw=yaw.value)


@bot.tree.command(name="farm_return_target", description="設定回程 AOTV 目標點（存入 json；不填任何參數則顯示目前值）")
@app_commands.describe(
    plot="回程先傳送到的 Plot（1 到 24，預設 13）",
    z="目標 Z（預設 -144.5）",
    y="看向的 Y（預設 66）",
    x_min="看向的 X 隨機範圍下限（預設 -95）",
    x_max="看向的 X 隨機範圍上限（預設 -80）",
    reset="還原所有預設值（視角與回程目標）",
    player=PLAYER_HELP,
)
@app_commands.autocomplete(player=player_autocomplete)
async def farm_return_target(
    interaction: discord.Interaction,
    plot: app_commands.Range[int, 1, 24] | None = None,
    z: float | None = None,
    y: float | None = None,
    x_min: int | None = None,
    x_max: int | None = None,
    reset: bool = False,
    player: str | None = None,
) -> None:
    values = {"plot": plot, "z": z, "y": y, "x_min": x_min, "x_max": x_max}
    values = {key: value for key, value in values.items() if value is not None}
    if reset:
        await run_on_player(interaction, player, "set_view", reset=True)
    elif values:
        await run_on_player(interaction, player, "set_view", **values)
    else:
        await run_on_player(interaction, player, "get_view")


# ═════════════════════════════════════════════
# 完整控制面板：/farm_panel（每個帳號一個面板）
# ═════════════════════════════════════════════
PANEL_FEATURES = (
    ("pest", "除蟲"),
    ("equipment", "換裝"),
    ("sell", "賣出/合成"),
    ("spray", "噴霧"),
    ("bonus", "Bonus"),
)
PANEL_YAWS = (180, 90, 0, -90)
PANEL_TITLE = "FarmBot 控制面板"


async def fetch_panel(player: str) -> tuple[dict | None, str | None]:
    result = await asyncio.to_thread(request_farm, player, "panel")
    if result.get("ok") and isinstance(result.get("info"), dict):
        return result["info"], None
    return None, result.get("error") or result.get("message") or "未知錯誤"


def _health_text(info: dict) -> str:
    low, high = (info.get("pest_health") or [None, None])[:2]
    if low is None or high is None:
        return "預設（一般 100~2000；測試模式任何 HP > 0）"
    return f"{low:g} < HP < {high:g}"


def build_panel_embed(player: str, info: dict | None, error: str | None = None) -> discord.Embed:
    # 標題格式「FarmBot 控制面板 · 玩家名」：按鈕觸發時靠它判斷要控制哪個帳號
    title = f"{PANEL_TITLE} · {player}"
    if info is None:
        return discord.Embed(
            title=title,
            description=f"❌ Farm.py 沒有回應\n{error or ''}".strip(),
            color=0xE74C3C,
        )
    embed = discord.Embed(title=title, color=0x2ECC71 if info.get("f8") else 0x95A5A6)
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
        value=f"yaw {info.get('yaw'):g} · Plot {target.get('plot')}" if info.get("yaw") is not None else "—",
        inline=True,
    )
    taiwan_time = timezone(timedelta(hours=8), name="Taiwan")
    embed.set_footer(text=f"{player} · " + datetime.now(taiwan_time).strftime("更新於 %H:%M:%S 台灣時間"))
    return embed


def player_from_message(interaction: discord.Interaction) -> str | None:
    message = interaction.message
    if message is not None and message.embeds:
        title = message.embeds[0].title or ""
        if title.startswith(PANEL_TITLE) and " · " in title:
            return title.split(" · ", 1)[1].strip() or None
    return None


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

    def __init__(self, player: str) -> None:
        super().__init__()
        self.player = player
        self.title = f"立即除蟲 · {player}"[:45]

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            plot = int(str(self.plot.value).strip())
        except ValueError:
            await interaction.followup.send("Plot 必須是數字。", ephemeral=True)
            return
        result = await asyncio.to_thread(request_farm, self.player, "pest_now", plot=plot)
        await interaction.followup.send(f"**[{self.player}]** " + result.get("message", result.get("error", "操作失敗")), ephemeral=True)


class PestHealthModal(discord.ui.Modal, title="蟲血量範圍"):
    low = discord.ui.TextInput(label="下限（HP 必須大於此值）", placeholder="例：0（兩格都留空＝還原預設）", required=False, max_length=9)
    high = discord.ui.TextInput(label="上限（HP 必須小於此值）", placeholder="例：1000000", required=False, max_length=12)

    def __init__(self, player: str) -> None:
        super().__init__()
        self.player = player
        self.title = f"蟲血量範圍 · {player}"[:45]

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)
        low_text, high_text = str(self.low.value).strip(), str(self.high.value).strip()
        if not low_text and not high_text:
            result = await asyncio.to_thread(request_farm, self.player, "set_pest_health", reset=True)
        else:
            try:
                low, high = float(low_text), float(high_text)
            except ValueError:
                await interaction.followup.send("下限與上限都要填數字（或兩格都留空還原）。", ephemeral=True)
                return
            result = await asyncio.to_thread(request_farm, self.player, "set_pest_health", min=low, max=high)
        await interaction.followup.send(f"**[{self.player}]** " + result.get("message", result.get("error", "操作失敗")), ephemeral=True)


async def panel_dispatch(interaction: discord.Interaction, key: str, value=None) -> None:
    # 目標帳號取自面板標題；舊版面板（標題沒有帳號）且只有一個帳號在線時自動套用
    inst, error = fi.find_instance(player_from_message(interaction))
    if inst is None:
        await interaction.response.send_message(error, ephemeral=True)
        return
    player = inst["name"]

    if key == "pestnow":
        await interaction.response.send_modal(PestNowModal(player))
        return
    if key == "health":
        await interaction.response.send_modal(PestHealthModal(player))
        return

    await interaction.response.defer()
    result: dict = {"ok": True}
    if key == "f8":
        result = await asyncio.to_thread(request_farm, player, "toggle")
    elif key == "restart":
        result = await asyncio.to_thread(request_farm, player, "restart", 15.0)
    elif key.startswith("feat:"):
        result = await asyncio.to_thread(request_farm, player, "set_feature", name=key.split(":", 1)[1], value="toggle")
    elif key == "test":
        result = await asyncio.to_thread(request_farm, player, "set_test_mode", value="toggle")
    elif key == "antifail":
        result = await asyncio.to_thread(request_farm, player, "resume_anti_fail")
    elif key == "add30":
        result = await asyncio.to_thread(request_farm, player, "add_time", minutes=30)
    elif key == "yaw":
        result = await asyncio.to_thread(request_farm, player, "set_view", yaw=value)

    if key == "restart":
        await asyncio.sleep(2.5)   # 新 job 需要一點時間開 socket
    info, error = await fetch_panel(player)
    try:
        await interaction.edit_original_response(embed=build_panel_embed(player, info, error), view=PanelView(info))
    except discord.HTTPException as exc:
        print(f"Panel refresh failed: {type(exc).__name__}: {exc}")
    if key != "refresh":
        message = result.get("message") or result.get("error")
        if message:
            await interaction.followup.send(f"**[{player}]** {message}", ephemeral=True)


@bot.tree.command(name="farm_panel", description="開啟 FarmBot 控制面板（每個 Minecraft 帳號一個）")
@app_commands.describe(player="只開這個帳號的面板（不填＝為每個在線帳號各開一個）")
@app_commands.autocomplete(player=player_autocomplete)
async def farm_panel(interaction: discord.Interaction, player: str | None = None) -> None:
    if not await is_authorized(interaction):
        return
    await interaction.response.defer(thinking=True)
    if player:
        inst, error = fi.find_instance(player)
        if inst is None:
            await interaction.followup.send(error, ephemeral=True)
            return
        targets = [inst["name"]]
    else:
        targets = [inst["name"] for inst in fi.list_instances() if inst["online"]]
        if not targets:
            await interaction.followup.send("目前沒有在線的 FarmBot 帳號（Farm.py 沒有心跳）。", ephemeral=True)
            return
        targets = targets[:5]   # 避免一次洗版；更多帳號請用 player 參數
    for name in targets:
        info, error = await fetch_panel(name)
        await interaction.followup.send(embed=build_panel_embed(name, info, error), view=PanelView(info))


# ═════════════════════════════════════════════
# 每分鐘快照：每個在線帳號各發一則（訊息標明帳號）
# ═════════════════════════════════════════════
async def report_one(channel, name: str) -> None:
    result = await asyncio.to_thread(request_farm, name, "screenshot", 12.0)
    if not result.get("ok") and not result.get("screenshot"):
        # 只在「由正常變失敗」時通知一次，避免每分鐘洗版
        if _report_ok.get(name, False):
            _report_ok[name] = False
            try:
                await channel.send(f"⚠️ **{name}** 沒有回應：{result.get('error', '')}"[:1900])
            except Exception as exc:
                print(f"Periodic report notice failed: {type(exc).__name__}: {exc}")
        return
    _report_ok[name] = True

    # Minescript writes screenshots asynchronously in some runtime builds.
    image_path = Path(result["screenshot"]) if result.get("screenshot") else None
    if image_path:
        for _ in range(10):
            if image_path.is_file():
                break
            await asyncio.sleep(0.2)
    taiwan_time = timezone(timedelta(hours=8), name="Taiwan")
    timestamp = datetime.now(taiwan_time).strftime("%Y-%m-%d %H:%M:%S 台灣時間")
    lines = [f"FarmBot 快照 · **{name}** · {timestamp}"]
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
            await channel.send(content=content, file=discord.File(image_path, filename=f"farmbot_{name}.png"))
            try:
                image_path.unlink()
            except OSError:
                pass
        else:
            await channel.send(content=content)
    except Exception as exc:
        print(f"Periodic report failed ({name}): {type(exc).__name__}: {exc}")


@tasks.loop(seconds=60)
async def periodic_report() -> None:
    channel = bot.get_channel(TARGET_CHANNEL_ID)
    if channel is None:
        try:
            channel = await bot.fetch_channel(TARGET_CHANNEL_ID)
        except Exception as exc:
            print(f"Cannot access report channel: {type(exc).__name__}: {exc}")
            return
    # 登記過的帳號：線上的照常快照；剛離線的也試一次，才能發出「沒有回應」通知
    names = [inst["name"] for inst in fi.list_instances() if inst["online"] or _report_ok.get(inst["name"])]
    if names:
        await asyncio.gather(*(report_one(channel, name) for name in names), return_exceptions=True)


@periodic_report.before_loop
async def before_periodic_report() -> None:
    await bot.wait_until_ready()


# ═════════════════════════════════════════════
# bot 只在一個視窗執行：搶到檔案鎖的 Watchdog 負責；它結束後其他視窗會接手
# ═════════════════════════════════════════════
def acquire_discord_single_instance_lock():
    try:
        fi.INSTANCE_DIR.mkdir(parents=True, exist_ok=True)
        stream = (fi.INSTANCE_DIR / ".discord_control.lock").open("a+b")
    except OSError:
        return None
    stream.seek(0, os.SEEK_END)
    if stream.tell() == 0:
        stream.write(b"\0")
        stream.flush()
    stream.seek(0)
    try:
        import msvcrt

        try:
            msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError:
            stream.close()
            return None
    except ImportError:
        import fcntl

        try:
            fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            stream.close()
            return None
    return stream


_bot_host_thread_started = False


def start_embedded_discord_bot() -> None:
    """每個視窗的 Watchdog 都呼叫；只有搶到鎖的那個會真的連上 Discord。"""
    global _bot_host_thread_started
    if _bot_host_thread_started:
        return
    if not BOT_TOKEN:
        echo("§cDiscord Bot 未啟動：.env 缺少 DISCORD_BOT_TOKEN")
        return
    if not CONTROL_SECRET:
        echo("§cDiscord Bot 未啟動：.env 缺少 FARMBOT_CONTROL_SECRET")
        return
    _bot_host_thread_started = True

    def discord_worker() -> None:
        import traceback

        announced = False
        while True:
            lock_stream = acquire_discord_single_instance_lock()
            if lock_stream is not None:
                break
            if not announced:
                echo("§7Discord Bot 已由其他帳號視窗執行，本視窗待命（對方關閉後會自動接手）")
                announced = True
            time.sleep(10.0)

        echo(f"§aDiscord Bot 由 {PLAYER} 這個視窗執行")
        try:
            # bot.run() installs signal handlers and expects the main thread.
            asyncio.run(bot.start(BOT_TOKEN))
        except BaseException as exc:
            message = f"Discord Bot 執行失敗：{type(exc).__name__}: {exc}"
            print(message)
            try:
                with (ROOT / "Farm_watchdog_discord.log").open("a", encoding="utf-8") as stream:
                    stream.write(message + "\n")
                    stream.write(traceback.format_exc() + "\n")
            except OSError:
                pass
        finally:
            lock_stream.close()   # 放開鎖，讓其他視窗的 Watchdog 能接手

    threading.Thread(target=discord_worker, name="FarmBot-Discord", daemon=True).start()


if __name__ == "__main__":
    main()
