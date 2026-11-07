
import re
import time
import math
import random
import threading
import minescript

from pest_utils import (
    PLAYER_EYE_HEIGHT, FLY_HEIGHT, ARRIVE_BOX, STUCK_TIMEOUT,
    PLOT_STUCK_SEC, NO_MOB_TIMEOUT, AOTE_SLOT, VACUUM_SLOT,
    AOTE_SHOT_MIN, AOTE_SHOT_MAX, AOTE_SMOOTH_STEPS, AIM_TICK_SEC,
    WANDER_BOX_YAW, WANDER_BOX_PITCH, WANDER_NOISE_Y, WANDER_NOISE_P,
    SPRING_K, SPRING_DAMP, WANDER_INTERVAL_MIN, WANDER_INTERVAL_MAX,
    JITTER_FREQ, FOV_THRESHOLD_DEG, PLOT_CENTERS,
    _clamp, _angle_diff, _dist3, _perlin_1d, _calc_yaw_pitch,
    _in_fov, _release_all,
    is_flying, get_plot_pest_count, find_silverfish, _get_entity_pos, look_at_block,
    get_current_plot, pest_test_mode
)

# ─────────────────────────────────────────────
# 調校參數
# ─────────────────────────────────────────────
LOGIC_DT = 0.03            # 主邏輯週期（秒）
TURN_SPEED_FAR = 9.0       # 遠距離轉向跟手度（越大越快）
TURN_SPEED_NEAR = 14.0     # 吸蟲時追蹤跟手度
TURN_MAX_DPS = 480.0       # 最大角速度（度/秒）
AIM_SYNC_SEC = 0.25        # aim 執行緒多久與實際視角校正一次
AOTE_FAR_DIST = 15.0       # 最低傳送距離：超過此距離才用 AOTE
AOTE_ALIGN_DEG = 4.0       # 視角誤差小於此值才發射 AOTE
AOTE_MIN_GAP = 0.30        # 兩次 AOTE 最短間隔
MOVE_MAX_TURN_ERR = 80.0   # 視角誤差超過此值先不前進
NO_SNEAK_BELOW_Y = 80.0    # 低於此高度絕不蹲下

# ── 群集瞄準 ──
REFRESH_SEC = 0.12         # 多久重掃一次蟲列表
CLUSTER_R = 4.5            # 群集半徑（吸塵器範圍半徑 5，留 0.5 給蟲移動）
DIST_PENALTY = 1.0 / 25.0  # 距離懲罰：每 25 格抵掉 1 隻蟲的價值
SWITCH_MARGIN = 0.35       # 新群集分數要高出多少才換目標（避免來回跳）
STOP_DIST_RANGE = (5.0, 6.3)  # 與群集中心的停止距離（定期重抽，站位不固定）
RESUME_EXTRA = 1.5         # 停下後距離超過「停止距離 + 此值」才重新前進（遲滯）
MIN_HORIZ_FWD = 1.5        # 水平距離小於此值就不再前進（已在群集正上/下方）

# ── 高度變化 ──
ALT_OFF_MIN = -3.0         # 相對群集中心的高度偏移下限（負 = 從下往上看）
ALT_OFF_MAX = 4.0          # 上限（正 = 從上往下看）
ALT_CHANGE_SEC = (2.5, 7.0)   # 多久換一次高度偏好
ALT_EASE_SEC = 1.4         # 偏移平滑時間常數（越大越慢）
ALT_DEADBAND = 0.9         # 高度誤差死區（避免跳/蹲抖動）
ALT_FLOOR_LIFT = (0.8, 7.0)   # 在 80 以上時，目標高度的下限 = 80 + 此範圍內隨機值（避免貼在最低點）

# ── 找不到蟲：回中心 ──
CENTER_ARRIVE_R = 5.0      # 離中心多近算到達
CENTER_FLY_TIMEOUT = 8.0   # 飛回中心最長時間

# ── 傳送後處理 ──
EDGE_NUDGE_SEC = (0.5, 1.2)   # 每次傳送後，先點 W 往前走的時間範圍
NUDGE_MAX = 4              # 每次傳送後，最多點幾次 W 還進不了目標 plot 才放棄這次傳送
TELEPORT_MAX_TRIES = 3     # 最多傳送幾次（走了還進不去才會重傳）
RECOVER_MAX = 3            # RECOVER_WINDOW_SEC 內最多允許幾次「離開 plot 被拉回」
RECOVER_WINDOW_SEC = 45.0
SB_START_TIMEOUT = 1.5     # 開場讀計分板的逾時（秒）

# ── 空列表處理 ──
EMPTY_GRACE_SEC = 0.2      # 列表突然變空：這段時間內先維持現狀
EMPTY_EXIT_SEC = 0.5       # 持續空超過此時間，交回外層（回中心 / 雙確認）


def human_delay(min_sec=0.05, max_sec=0.15):
    mean = (min_sec + max_sec) / 2
    stdev = (max_sec - min_sec) / 4
    val = random.gauss(mean, stdev)
    time.sleep(_clamp(val, min_sec, max_sec))


# ─────────────────────────────────────────────
# 視角控制器（獨立執行緒）
# ─────────────────────────────────────────────
class _Aim:
    """在背景持續把視角平滑轉向 target；本地追蹤角度，每步只呼叫一次 set_orientation。"""

    def __init__(self):
        self._target = None
        self._speed = TURN_SPEED_FAR
        self._alive = False
        self._thread = None
        self.cur = None            # (yaw, pitch)

    def start(self):
        if self._thread is not None and self._thread.is_alive():
            return
        self._alive = True
        self.cur = None
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    def stop(self):
        self._alive = False
        self._target = None

    def clear(self):
        self._target = None

    def set_target(self, yaw, pitch, speed=None):
        if speed is not None:
            self._speed = speed
        self._target = (yaw, _clamp(pitch, -90, 90))

    def error_to(self, yaw, pitch):
        c = self.cur
        if c is None:
            return 999.0
        return math.hypot(_angle_diff(c[0], yaw), pitch - c[1])

    def _sync(self):
        try:
            ori = minescript.player_orientation()
        except Exception:
            return
        if ori:
            self.cur = (ori[0], ori[1])

    def _loop(self):
        last = time.time()
        last_sync = 0.0
        had_target = False
        while self._alive:
            tgt = self._target
            now = time.time()

            if tgt is None:
                had_target = False
                time.sleep(0.02)
                last = time.time()
                continue

            if (not had_target) or self.cur is None or now - last_sync > AIM_SYNC_SEC:
                self._sync()
                last_sync = time.time()
                had_target = True
                last = time.time()
                if self.cur is None:
                    time.sleep(0.02)
                    continue

            dt = _clamp(now - last, 0.003, 0.06)
            last = now

            cy, cp = self.cur
            dy = _angle_diff(cy, tgt[0])
            dp = tgt[1] - cp

            k = 1.0 - math.exp(-self._speed * dt)
            sy, sp = dy * k, dp * k
            cap = TURN_MAX_DPS * dt
            mag = math.hypot(sy, sp)
            if mag > cap and mag > 1e-6:
                s = cap / mag
                sy *= s
                sp *= s

            ny = cy + sy
            npi = _clamp(cp + sp, -90, 90)
            try:
                minescript.player_set_orientation(ny, npi)
            except Exception:
                pass
            self.cur = (ny, npi)
            time.sleep(0.005)


class _Keys:
    """只在狀態改變時才送出按鍵。"""

    def __init__(self):
        self._s = {}

    def set(self, name, down):
        down = bool(down)
        if self._s.get(name) is not down:
            getattr(minescript, f"player_press_{name}")(down)
            self._s[name] = down

    def release(self, *names):
        for n in names:
            getattr(minescript, f"player_press_{n}")(False)
        self._s.clear()

    def reset(self):
        self._s.clear()


# ─────────────────────────────────────────────
# PestCleaner (主邏輯)
# ─────────────────────────────────────────────
class PestCleaner:
    def __init__(self):
        self._stop_flag = False
        self._lock = threading.Lock()
        self._done = threading.Event()
        self.k = _Keys()
        self.aim = _Aim()
        self._sb_run = False
        self._sb_count = None
        self._plot_num = None
        self._out_of_plot = False
        self._recover_t = 0.0
        self._last_plot_raw = None
        self._recover_log = []
        self._corner_search_index = 0
        self._worker_thread = None

    def clean_plot(self, plot_num, pest_count=99, timeout=300.0):
        with self._lock:
            self._stop_flag = False
            self._done.clear()
        self._worker_thread = threading.Thread(
            target=self._run,
            args=(plot_num, pest_count),
            daemon=True,
        )
        self._worker_thread.start()
        completed = self._done.wait(timeout=timeout)
        if not completed:
            minescript.echo("§c[PestCleaner] 除蟲逾時，要求停止工作執行緒")
            self.stop()
            self._worker_thread.join(timeout=4.0)
            if self._worker_thread.is_alive():
                minescript.echo("§c[PestCleaner] 停止訊號送出後工作執行緒仍未退出")
            return False
        self._worker_thread.join(timeout=0.2)
        return True

    def pesting(self, plot_num, pest_count=99, timeout=300.0):
        return self.clean_plot(plot_num, pest_count, timeout)

    def stop(self):
        self._stop_flag = True
        self._sb_run = False
        self.aim.stop()
        _release_all()
        self._done.set()

    def _run(self, plot_num, pest_count):
        self._plot_num = plot_num
        self._recover_log = []
        self._corner_search_index = 0
        self.aim.start()
        try:
            res = self._travel(plot_num)
            if self._stop_flag:
                return
            if res in ("clear", "fail"):   # clear = 開場計分板為 0；fail = 進不了目標 plot
                return
            self._vacuum(pest_count)
        except Exception as e:
            minescript.echo(f"§c[PestCleaner] 出錯: {e}")
        finally:
            self._sb_run = False
            self.aim.stop()
            self._stop_movement()
            _release_all()
            self._done.set()

    # ── 飛行：只確認狀態，不爬高 ──
    def _ensure_flying(self):
        if is_flying(timeout=2.0):
            return True
        for _ in range(3):
            if self._stop_flag:
                return False
            minescript.player_press_jump(True)
            human_delay(0.06, 0.10)
            minescript.player_press_jump(False)
            human_delay(0.06, 0.10)
            minescript.player_press_jump(True)
            human_delay(0.06, 0.10)
            minescript.player_press_jump(False)
            human_delay(0.12, 0.20)
            if is_flying(timeout=1.5):
                self.k.reset()
                return True
        self.k.reset()
        return False

    def _travel(self, plot_num):
        """
        回傳 "clear" = 計分板為 0 且掃不到蟲（這個 plot 不用打）；
        "fail"  = 傳送 + 走路都進不了目標 plot；
        其他情況回傳 None（由呼叫端照常往下走 / 或被 stop）。
        """
        _release_all()
        self._recover_t = time.time()

        if self._at_target_now():
            minescript.echo("§a[PestCleaner] 已在目標 plot，略過傳送")
        else:
            minescript.echo(
                f"§7[PestCleaner] 目前 plot 讀到 {self._last_plot_raw!r}，目標 {plot_num}，執行傳送")
            if not self._teleport_and_enter(plot_num):
                return None if self._stop_flag else "fail"
        self._recover_t = time.time()

        # 先看計分板：0 隻蟲（且 entity 也掃不到）就直接結束，不用再飛、不用等
        count = self._read_plot_count(timeout=SB_START_TIMEOUT)
        if count == 0 and not find_silverfish():
            minescript.echo("§a[PestCleaner] 計分板為 0，此 plot 沒有蟲，略過")
            return "clear"
        if self._stop_flag:
            return None

        if not self._ensure_flying():
            minescript.echo("§c[PestCleaner] 無法啟動飛行")
            return None
        if self._stop_flag:
            return None

        # Entry nudge is the first movement; resume aiming shortly after flying
        # is confirmed instead of adding another long idle pause.
        human_delay(0.06, 0.18)
        if plot_num in PLOT_CENTERS and not pest_test_mode() and not find_silverfish(max_dist=60):
            self._return_to_center()
        return None

    def _at_target_now(self):
        """目前是否已在目標 plot（連讀 3 次，任一次是 True 就算，避免單次讀取失誤造成多餘傳送）。"""
        for i in range(3):
            if self._plot_matches() is True:
                return True
            if i < 2:
                time.sleep(0.1)
        return False

    def _teleport_and_enter(self, plot_num):
        """
        /plottp 後一律先點 W 走進目標 plot（落點可能在目標旁邊 plot 的邊緣）。
        走了 NUDGE_MAX 次還沒進去才會重傳，最多傳 TELEPORT_MAX_TRIES 次。
        """
        for attempt in range(TELEPORT_MAX_TRIES):
            minescript.execute(f"/plottp {plot_num}")
            human_delay(1.2, 1.8)
            for _ in range(NUDGE_MAX):
                if self._stop_flag:
                    return False
                self._nudge_forward()
                time.sleep(0.1)
                if self._plot_matches() is not False:      # True 或讀取失敗(未知)都視為已進入
                    return True
            if attempt < TELEPORT_MAX_TRIES - 1:
                minescript.echo(
                    f"§e[PestCleaner] 走了 {NUDGE_MAX} 次仍不在目標 plot（讀到 {self._last_plot_raw!r}），重傳")
        minescript.echo(
            f"§c[PestCleaner] 傳送 {TELEPORT_MAX_TRIES} 次仍進不了目標 plot（最後讀到 {self._last_plot_raw!r}），放棄")
        return False

    def _nudge_forward(self):
        """先面向目標 plot 中心，再點 W 0.5~1.2 秒。"""
        dur = random.uniform(*EDGE_NUDGE_SEC)
        plot = self._plot_num
        if plot in PLOT_CENTERS:
            try:
                pos = minescript.player_position()
                tx, tz = PLOT_CENTERS[plot]
                ty, _ = _calc_yaw_pitch(pos, (tx, pos[1] + PLAYER_EYE_HEIGHT, tz))
                self.aim.set_target(ty, 3.0, speed=TURN_SPEED_FAR)
                t0 = time.time()
                while time.time() - t0 < 0.5 and not self._stop_flag:
                    time.sleep(0.02)
                    if self.aim.error_to(ty, 3.0) < 8.0:
                        break
            except Exception:
                pass
        try:
            self.k.set("forward", True)
            t0 = time.time()
            while not self._stop_flag and time.time() - t0 < dur:
                time.sleep(LOGIC_DT)
        finally:
            self._stop_movement()
            self.aim.clear()

    def _read_plot_count(self, timeout=1.5):
        """讀目標 plot 的計分板蟲數；不在目標 plot 或讀取失敗回傳 None。"""
        if self._plot_matches() is False:
            return None
        try:
            sb = get_plot_pest_count(timeout=timeout)
        except Exception:
            return None
        if sb is None:
            return None
        # 讀完再確認一次仍在目標 plot，數字才可信
        if self._plot_matches() is False:
            return None
        return sb[1]

    # ── 所在 plot 判定 ──
    def _plot_matches(self):
        """True = 在目標 plot；False = 不在；None = 讀取失敗（未知）。"""
        if pest_test_mode():          # 單人存檔沒有 plot，一律視為在目標內
            self._last_plot_raw = "test-mode"
            return True
        try:
            cur = get_current_plot()
        except Exception:
            self._last_plot_raw = "<error>"
            return None
        self._last_plot_raw = cur
        if cur is None:
            return False
        m = re.search(r"\d+", str(cur))       # 容許 13 / "13" / "Plot 13" 之類的格式
        if not m:
            return False
        try:
            return int(m.group()) == int(self._plot_num)
        except (TypeError, ValueError):
            return False

    def _in_target_plot(self):
        """同步確認（不在時會再複查一次，避免單次讀取失誤）。"""
        r = self._plot_matches()
        if r is None or r:
            return True
        time.sleep(0.15)
        r = self._plot_matches()
        return True if r is None else r

    def _recover_plot(self):
        """飛出目標 plot：放開按鍵，/plottp 回去（含點 W 進 plot），重新確認飛行並回中心。
        短時間內反覆離開會直接停止，避免無限傳送循環。"""
        now = time.time()
        self._recover_log = [t for t in self._recover_log if now - t < RECOVER_WINDOW_SEC]
        self._recover_log.append(now)
        if len(self._recover_log) > RECOVER_MAX:
            minescript.echo("§c[PestCleaner] 短時間內反覆離開目標 plot，停止以免無限傳送")
            self._stop_flag = True
            return False

        minescript.echo(
            f"§e[PestCleaner] 偵測到離開目標 plot（讀到 {self._last_plot_raw!r}），回到 plot 重新開始")
        self._stop_movement()
        minescript.player_press_use(False)
        self._sb_count = None
        res = self._travel(self._plot_num)
        if res == "fail":
            self._stop_flag = True
        self._out_of_plot = False
        self._recover_t = time.time()
        self.k.reset()
        minescript.player_inventory_select_slot(VACUUM_SLOT)
        return res != "fail"

    # ── 背景監看（不阻塞獵殺迴圈）：所在 plot + 計分板 ──
    def _watch_worker(self):
        next_sb = time.time() + random.uniform(2.2, 3.5)
        bad = 0
        while self._sb_run and not self._stop_flag:
            time.sleep(0.4)
            if time.time() - self._recover_t < 3.0:     # 剛傳送回來，給讀取時間更新
                bad = 0
                continue

            ok = self._plot_matches()
            if ok is None:
                continue
            if not ok:
                bad += 1
                self._sb_count = None                    # 在別的 plot 讀到的計分板不可信
                if bad >= 2:                             # 連續兩次才算，避免誤判
                    self._out_of_plot = True
                continue
            bad = 0
            self._out_of_plot = False

            if time.time() >= next_sb:
                next_sb = time.time() + random.uniform(2.2, 3.5)
                try:
                    sb = get_plot_pest_count(timeout=1.5)
                except Exception:
                    continue
                # 讀完再確認一次仍在目標 plot，數字才採用
                if sb is not None and self._plot_matches():
                    self._sb_count = sb[1]

    def _vacuum(self, pest_count):
        minescript.player_inventory_select_slot(VACUUM_SLOT)
        human_delay(0.08, 0.15)
        locked_once = False
        no_mob_since = None

        self._sb_count = None
        self._out_of_plot = False
        self._sb_run = True
        threading.Thread(target=self._watch_worker, daemon=True).start()

        try:
            while not self._stop_flag:
                # 先確認人還在目標 plot 內，否則別的 plot 沒蟲會被誤判為「清空」
                if not self._in_target_plot():
                    self._recover_plot()
                    continue

                sfs = find_silverfish()

                if not sfs:
                    if locked_once:
                        if no_mob_since is None:
                            no_mob_since = time.time()
                        elif time.time() - no_mob_since >= NO_MOB_TIMEOUT:
                            minescript.echo("§c[PestCleaner] entity 掃描逾時，結束")
                            return

                    # 計分板還有蟲但掃不到：立刻回中心找，不要原地等
                    sb = get_plot_pest_count(timeout=2.0)
                    count = sb[1] if sb is not None else None
                    if sb is not None and count is not None and count > 0:
                        no_mob_since = None
                        if not self._return_to_center():
                            # 已在中心仍掃不到時，沿 plot 四角巡查，讓遠角害蟲進入實體載入範圍。
                            self._search_plot_corner()
                            human_delay(0.2, 0.35)
                        continue

                    # 計分板為 0 / 讀不到：用 entity 再確認 3 次才結束
                    confirm = 0
                    for _ in range(3):
                        human_delay(0.35, 0.45)
                        if self._stop_flag:
                            return
                        if find_silverfish():
                            confirm = -1
                            break
                        confirm += 1
                    if confirm < 0:
                        continue

                    if not self._in_target_plot():
                        self._recover_plot()
                        continue

                    minescript.echo("§a[PestCleaner] Plot 清空（entity + 計分板雙確認）")
                    return

                if not locked_once:
                    human_delay(0.12, 0.25)

                no_mob_since = None
                locked_once = True

                result = self._hunt(sfs)
                minescript.player_press_use(False)

                if result == "outside":
                    self._recover_plot()
                    continue
                if result == "clear":
                    if not self._in_target_plot():
                        self._recover_plot()
                        continue
                    minescript.echo("§a[PestCleaner] 計分板確認清空，結束")
                    return
                if result in ("empty", "stuck"):
                    continue
                break

        finally:
            self._sb_run = False
            self.aim.clear()
            self._stop_movement()
            minescript.player_press_use(False)

    # ── 群集挑選：找「範圍內蟲最多」的位置 ──
    def _pick_cluster(self, bugs, p_pos, prev_anchor):
        pts = [(e.id, e.position) for e in bugs]
        eye = (p_pos[0], p_pos[1] + 1.62, p_pos[2])

        best = None
        prev = None
        for bid, pos in pts:
            members = [q for _, q in pts if _dist3(pos, q) <= CLUSTER_R]
            n = len(members)
            c = (
                sum(m[0] for m in members) / n,
                sum(m[1] for m in members) / n,
                sum(m[2] for m in members) / n,
            )
            cnt = sum(1 for _, q in pts if _dist3(c, q) <= CLUSTER_R)
            score = cnt - _dist3(eye, c) * DIST_PENALTY
            item = (score, bid, c, cnt)
            if best is None or score > best[0]:
                best = item
            if bid == prev_anchor:
                prev = item

        # 遲滯：沒有明顯更好就不換，避免視角在群集之間來回跳
        if prev is not None and best[0] < prev[0] + SWITCH_MARGIN:
            best = prev
        return best[2], best[1], best[3]

    def _aote_shot(self):
        minescript.player_press_use(False)
        minescript.player_inventory_select_slot(AOTE_SLOT)
        human_delay(0.03, 0.05)
        minescript.player_press_use(True)
        human_delay(0.03, 0.05)
        minescript.player_press_use(False)
        minescript.player_inventory_select_slot(VACUUM_SLOT)
        human_delay(0.03, 0.05)
        minescript.player_press_use(True)

    def _hunt(self, bugs):
        try:
            return self._hunt_loop(bugs)
        finally:
            self._stop_movement()

    def _hunt_loop(self, bugs):
        """
        連續獵殺：整個 plot 只跑這一個迴圈。
        每個 tick：（定期）重掃蟲 → 挑群集 → 更新視角目標 → 更新移動鍵。
        使用鍵全程按住，蟲死了不會停。
        回傳: "clear"（計分板為 0）/ "empty"（蟲消失）/ "stuck" / "stop"
        """
        k = self.k
        aim = self.aim
        k.reset()

        aim_seed = random.randint(0, 9999)
        alt_off = random.uniform(ALT_OFF_MIN, ALT_OFF_MAX)
        alt_goal = alt_off
        floor_lift = random.uniform(*ALT_FLOOR_LIFT)
        stop_d = random.uniform(*STOP_DIST_RANGE)
        next_alt_change = time.time() + random.uniform(*ALT_CHANGE_SEC)
        last_loop = time.time()
        last_rp = None
        stuck_start = None
        last_fly_check = 0.0
        last_shot = 0.0
        last_refresh = time.time()
        empty_since = None
        anchor_id = None
        progress_t = time.time()
        last_count = len(bugs)

        # 初次：先開始轉向，再（必要時）skill reset，等待期間視角已經在轉
        p_pos = minescript.player_position()
        P, anchor_id, _ = self._pick_cluster(bugs, p_pos, None)
        ty0, tp0 = _calc_yaw_pitch(p_pos, P)
        aim.set_target(ty0, tp0, speed=TURN_SPEED_FAR)

        ori = minescript.player_orientation()
        if ori and not _in_fov(ori[0], P, p_pos):
            self._stop_movement()
            self._skill_reset()
            if self._stop_flag:
                return "stop"

        minescript.player_inventory_select_slot(VACUUM_SLOT)
        minescript.player_press_use(True)

        while not self._stop_flag:
            time.sleep(LOGIC_DT)
            now = time.time()
            dt = _clamp(now - last_loop, 0.005, 0.2)
            last_loop = now

            # 高度 / 站位偏好：每隔一段時間重抽，並平滑過渡（不規則但不抖動）
            if now >= next_alt_change:
                alt_goal = random.uniform(ALT_OFF_MIN, ALT_OFF_MAX)
                floor_lift = random.uniform(*ALT_FLOOR_LIFT)
                stop_d = random.uniform(*STOP_DIST_RANGE)
                next_alt_change = now + random.uniform(*ALT_CHANGE_SEC)
            alt_off += (alt_goal - alt_off) * (1.0 - math.exp(-dt / ALT_EASE_SEC))

            if self._out_of_plot:
                return "outside"
            if self._sb_count == 0:
                return "clear"
            if now - progress_t > PLOT_STUCK_SEC:
                return "stuck"

            # ── 飛行狀態：掉飛就直接重新開飛 ──
            if now - last_fly_check > 0.5:
                last_fly_check = now
                if is_flying() is False:
                    self._stop_movement()
                    minescript.player_press_use(False)
                    if not self._ensure_flying():
                        minescript.echo("§c[PestCleaner] 飛行中斷且無法恢復")
                        return "stop"
                    minescript.player_inventory_select_slot(VACUUM_SLOT)
                    minescript.player_press_use(True)
                    continue

            p_pos = minescript.player_position()

            # ── 定期重掃蟲列表 + 重新挑群集 ──
            if now - last_refresh >= REFRESH_SEC:
                last_refresh = now
                found = find_silverfish()
                if not found:
                    if empty_since is None:
                        empty_since = now
                    gone = now - empty_since
                    if gone >= EMPTY_EXIT_SEC:
                        return "empty"
                    if gone >= EMPTY_GRACE_SEC:
                        self._stop_movement()      # 蟲全不見：不要繼續亂衝
                    continue
                empty_since = None
                if len(found) < last_count:
                    progress_t = now               # 有蟲被清掉 = 有進展
                last_count = len(found)
                P, anchor_id, _ = self._pick_cluster(found, p_pos, anchor_id)

            if empty_since is not None:
                continue

            cur = aim.cur
            if cur is None:
                ori = minescript.player_orientation()
                if not ori:
                    return "stop"
                cur = (ori[0], ori[1])

            eye_y = p_pos[1] + 1.62
            dx = P[0] - p_pos[0]
            dy = P[1] - eye_y
            dz = P[2] - p_pos[2]
            dist = math.sqrt(dx * dx + dy * dy + dz * dz)

            horiz = math.hypot(dx, dz)
            fwd_on = bool(k._s.get("forward"))
            near = (dist <= (stop_d if fwd_on else stop_d + RESUME_EXTRA)) or horiz < MIN_HORIZ_FWD
            far = dist > AOTE_FAR_DIST

            # ── 更新視角目標（實際轉動由 aim 執行緒負責）──
            ty, tp = _calc_yaw_pitch(p_pos, P)
            amp = 0.1 if near else 0.3
            ty += _perlin_1d(now * 0.7, aim_seed) * amp
            tp += _perlin_1d(now * 0.7, aim_seed + 1) * amp * 0.6
            # 高低差很大且 XZ 很近時，目標俯仰角可能超過 70°；若裁切，
            # aim.error_to 永遠達不到 AOTE_ALIGN_DEG，AOTE 就永遠不會發射。
            tp = _clamp(tp, -90, 90)

            err = aim.error_to(ty, tp)
            aim.set_target(ty, tp, speed=TURN_SPEED_NEAR if near else TURN_SPEED_FAR)

            # ── 遠距離：視角對準就發射 AOTE ──
            if far and err < AOTE_ALIGN_DEG and now - last_shot > AOTE_MIN_GAP:
                self._aote_shot()
                last_shot = time.time()
                continue

            # ── 移動 ──
            k.set("forward", (not near) and err < MOVE_MAX_TURN_ERR)

            # 目標高度 = 群集高度 + 緩慢變動的偏移 + 微幅起伏
            # （偏移為負 = 從下往上看，為正 = 從上往下看）
            bob = _perlin_1d(now * 0.25, aim_seed + 2) * 0.8
            tgt_y = P[1] + alt_off + bob
            if p_pos[1] >= NO_SNEAK_BELOW_Y - 1.0:
                # 已在 80 以上：目標不要老是壓到最低點，下限隨機抬高
                tgt_y = max(tgt_y, NO_SNEAK_BELOW_Y + floor_lift)
            if p_pos[1] < tgt_y - ALT_DEADBAND:
                k.set("sneak", False)
                k.set("jump", True)
            elif p_pos[1] > tgt_y + ALT_DEADBAND and p_pos[1] > NO_SNEAK_BELOW_Y:
                k.set("jump", False)
                k.set("sneak", True)
            else:
                k.set("jump", False)
                k.set("sneak", False)

            # ── 卡住偵測：只有「想前進卻沒動」才算 ──
            if k._s.get("forward"):
                rp = (round(p_pos[0]), round(p_pos[2]))
                if rp == last_rp:
                    if stuck_start is None:
                        stuck_start = now
                    elif now - stuck_start > STUCK_TIMEOUT:
                        return "stuck"
                else:
                    stuck_start = None
                last_rp = rp
            else:
                stuck_start = None
                last_rp = None

        return "stop"

    def _stop_movement(self):
        self.k.release("forward", "jump", "sneak")

    def _return_to_center(self):
        """
        找不到蟲：回 plot 中心。距離 > AOTE_FAR_DIST 用 AOTE，剩下的用飛的。
        回中心途中不因掃到遠處害蟲而取消移動；抵達後交給主迴圈重新掃描。
        回傳是否有實際移動（已在中心則 False）。
        """
        plot = self._plot_num
        if plot not in PLOT_CENTERS:
            return False
        tx, tz = PLOT_CENTERS[plot]
        pos = minescript.player_position()
        if math.hypot(tx - pos[0], tz - pos[2]) <= CENTER_ARRIVE_R:
            return False

        minescript.echo("§7[PestCleaner] 計分板仍有害蟲但掃描不到，正在移動回 plot 中心")
        self._aote_to_xz(tx, tz, interrupt_on_pest=False)
        if self._stop_flag:
            return True
        pos = minescript.player_position()
        if math.hypot(tx - pos[0], tz - pos[2]) > CENTER_ARRIVE_R:
            self._fly_to_xz(tx, tz, interrupt_on_pest=False)
        pos = minescript.player_position()
        arrived = math.hypot(tx - pos[0], tz - pos[2]) <= CENTER_ARRIVE_R
        minescript.echo("§a[PestCleaner] 已回到 plot 中心" if arrived else "§e[PestCleaner] 回中心移動未抵達，下一輪重試")
        return True

    def _plot_pests(self):
        """只保留目前 plot 邊界內的害蟲，避免鄰 plot 實體干擾角落搜尋。"""
        center = PLOT_CENTERS.get(self._plot_num)
        if center is None:
            return []
        tx, tz = center
        half = 95.6 / 2 + 2.0
        return [
            bug for bug in find_silverfish(max_dist=160)
            if abs(bug.position[0] - tx) <= half and abs(bug.position[2] - tz) <= half
        ]

    def _search_plot_corner(self):
        """中心掃不到但計分板仍有害蟲時，逐角移動並在每個角落附近重掃。"""
        center = PLOT_CENTERS.get(self._plot_num)
        if center is None or self._stop_flag:
            return False
        cx, cz = center
        inset = 95.6 * 0.40
        corners = ((-inset, -inset), (inset, -inset), (inset, inset), (-inset, inset))
        index = self._corner_search_index % len(corners)
        self._corner_search_index += 1
        ox, oz = corners[index]
        tx, tz = cx + ox, cz + oz
        minescript.echo(f"§7[PestCleaner] 中心仍掃不到，巡查 plot 角落 {index + 1}/4")
        self._aote_to_xz(tx, tz, interrupt_on_pest=False)
        if self._stop_flag:
            return False
        pos = minescript.player_position()
        if math.hypot(tx - pos[0], tz - pos[2]) > CENTER_ARRIVE_R:
            self._fly_to_xz(tx, tz, interrupt_on_pest=False)
        return bool(self._plot_pests())

    def _fly_to_xz(self, tx, tz, interrupt_on_pest=True):
        """水平飛向 (tx, tz)，不改變高度；可選擇在掃到目標害蟲時中止。"""
        k = self.k
        k.reset()
        seed = random.randint(0, 9999)
        t0 = last_scan = time.time()
        try:
            while not self._stop_flag and time.time() - t0 < CENTER_FLY_TIMEOUT:
                time.sleep(LOGIC_DT)
                now = time.time()
                pos = minescript.player_position()
                if math.hypot(tx - pos[0], tz - pos[2]) <= CENTER_ARRIVE_R:
                    return True
                if interrupt_on_pest and now - last_scan > 0.25:
                    last_scan = now
                    if self._plot_pests():
                        return False

                ty, _ = _calc_yaw_pitch(pos, (tx, pos[1] + 1.62, tz))
                ty += _perlin_1d(now * 0.7, seed) * 2.0
                tp = 3.0 + _perlin_1d(now * 0.7, seed + 1) * 2.0
                err = self.aim.error_to(ty, tp)
                self.aim.set_target(ty, tp, speed=TURN_SPEED_FAR)
                k.set("forward", err < MOVE_MAX_TURN_ERR)
        finally:
            self._stop_movement()
            self.aim.clear()
        return False

    def _aote_to_xz(self, tx, tz, tolerance=AOTE_FAR_DIST, timeout=20.0, max_shots=15, interrupt_on_pest=True):
        """沿水平方向 AOTE 往 (tx, tz)，距離 <= tolerance 就不再傳送；不改變高度。"""
        deadline = time.time() + timeout
        seed = random.randint(0, 9999)
        for _ in range(max_shots):
            if self._stop_flag or time.time() > deadline:
                break
            pos = minescript.player_position()
            if math.sqrt((tx - pos[0]) ** 2 + (tz - pos[2]) ** 2) <= tolerance:
                break
            if interrupt_on_pest and self._plot_pests():
                break

            aim_y, aim_p = _calc_yaw_pitch(pos, (
                tx + random.uniform(-2.0, 2.0),
                pos[1] + 1.62 + random.uniform(-0.5, 0.5),
                tz + random.uniform(-2.0, 2.0)
            ))
            t_now = time.time()
            aim_y += _perlin_1d(t_now * 0.7, seed) * 3.0
            aim_p = _clamp(aim_p + _perlin_1d(t_now * 0.7, seed + 1) * 2.0, -70, 70)

            minescript.player_inventory_select_slot(AOTE_SLOT)
            self.aim.set_target(aim_y, aim_p, speed=TURN_SPEED_FAR)
            t0 = time.time()
            while time.time() - t0 < 0.8 and not self._stop_flag:
                time.sleep(0.02)
                if self.aim.error_to(aim_y, aim_p) < 3.0:
                    break

            minescript.player_press_use(True)
            human_delay(0.02, 0.04)
            minescript.player_press_use(False)
            human_delay(0.01, 0.03)

        self.aim.clear()
        minescript.player_inventory_select_slot(VACUUM_SLOT)

    def _skill_reset(self):
        human_delay(0.08, 0.16)
        minescript.player_press_attack(True)
        human_delay(0.04, 0.09)
        minescript.player_press_attack(False)

        human_delay(0.09, 0.18)
        minescript.player_press_attack(True)
        human_delay(0.04, 0.09)
        minescript.player_press_attack(False)

        human_delay(0.5, 0.8)


if __name__ == "__main__":
    cleaner = PestCleaner()
    cleaner.clean_plot(plot_num=13, pest_count=4)
