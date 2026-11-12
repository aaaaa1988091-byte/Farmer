"""
pest_nav.py — 體素地圖 + A* 尋路 + 連續跟隨 + AOTE 長距離跳躍（v2）

相對舊版的主要修正：
  1. 碰撞改用「真實高度」：每格記錄碰撞箱頂端高度（耕地 0.9375、半磚 0.5…），
     站在耕地上不再被判定為「卡在方塊裡」，田道內拉直/尋路恢復正常。
  2. 飛行控制以碰撞預測為準：前方腳下會撞到就「只往上、不前進」，
     直到實際清掉方塊高度（再多升一小段）才繼續前進，不再用腳趾頭硬撞。
  3. 連續跟隨：按鍵不再每步放開；路徑存成折線，邊走邊跳過可直達的點，
     只有路被擋 / 偏離 / 卡住才重規劃，動作連貫。
  4. AOTE 只在「到下一個轉折點 ≥ 最大跳距」時使用（不再傳過頭來回彈），
     對準期間照常用 WASD 飛，不會停下來等；連續失敗會暫時停用 AOTE。
  5. 地圖改為分塊快取（16x8x16），走過的區域不再重掃，大幅減少 Java bridge 呼叫。
  6. 卡住偵測：沒位移就先跳、再把前方格子暫時標為障礙後重規劃，
     而不是原地繞圈；長時間無進展會清快取重來，仍失敗才放棄。
  7. A* 加入「貼牆懲罰」與水平斜走，路徑會走通道中央，較像人。
"""

import heapq
import math
import time

import minescript

from pest_utils import (
    _bridge_call, _calc_yaw_pitch, _dist3,
    PLOT_CENTERS, PLAYER_EYE_HEIGHT,
    AOTE_SHOT_MIN, AOTE_SHOT_MAX,
)

# ── 調校 ──
NO_SNEAK_BELOW_Y = 80.0     # 低於此高度預設不蹲下（與 pest_main.py 的設定保持一致）
ALLOW_LOW_DESCENT = True    # 低於上面高度時，只要離地夠高仍允許往下飛（與 pest_main.py 一致）
MIN_GROUND_CLEARANCE = 2.5  # 往下飛時至少保持離地這麼高（避免落地掉飛）
AOTE_HOP = AOTE_SHOT_MAX    # 相容舊名稱
AOTE_HOP_MIN = AOTE_SHOT_MIN
HOP_ALIGN_DEG = 3.0         # 視角誤差小於此值才發射 AOTE
HOP_MIN_GAP = 0.30          # 兩次 AOTE 最短間隔
AOTE_FAIL_LIMIT = 2         # AOTE 連續沒位移幾次就暫時停用
AOTE_OFF_SEC = 4.0

WIN_H = 24.0                # 單次規劃的水平距離上限
WIN_V = 14.0                # 單次規劃的垂直距離上限
PAD_H = 5                   # 掃描範圍水平外擴
PAD_V = 4                   # 掃描範圍垂直外擴
RESCAN_GAP = 1.0            # need_path 被動檢查時，兩次掃描的最短間隔（秒）
NAV_FAIL_LIMIT = 3          # 連續失敗幾次就暫時停用尋路
NAV_DISABLE_SEC = 10.0      # 停用多久（期間回到舊的直線逼近）

HALF_W = 0.3                # 玩家半寬
HEIGHT = 1.8                # 玩家身高
PATH_MARGIN = 0.12          # 規劃/拉直時額外的半寬餘裕（避免擦牆角）
SAMPLE = 0.25               # 直線檢查取樣間距
STEP_EPS = 0.02             # 腳底高於方塊頂端 0.02 以內視為站在上面
PLOT_HALF = 95.6 / 2 - 1.0

TILE_XZ = 16
TILE_Y = 8
TILE_TTL = 120.0            # 分塊快取壽命（秒）

# 控制
LOOK_AHEAD = 0.55           # 前進前往前預測碰撞的距離
RISE_HOLD = 0.18            # 前方清空後，再多升這麼久（讓腳完全離開方塊高度）
REPLAN_SEC = 2.0            # 路徑最長使用時間
MIN_REPLAN = 0.25           # 兩次重規劃最短間隔
SKIP_CHECK_SEC = 0.12       # 跳過可直達路徑點的檢查間隔
STALL_CHECK_SEC = 0.6       # 卡住偵測視窗
STALL_MOVE = 0.15           # 視窗內位移小於此值視為沒動
BLOCK_TTL = 8.0             # 被標為障礙的格子保留秒數
PROGRESS_TIMEOUT = 4.0      # 這麼久離目標沒更近就清快取重來
PATH_HW = HALF_W + PATH_MARGIN

_SOLID_SCRIPT = r"""
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
        x0=__X0__;y0=__Y0__;z0=__Z0__
        sx=__SX__;sy=__SY__;sz=__SZ__
        out=[]
        for x in range(sx):
            for y in range(sy):
                for z in range(sz):
                    bp=BlockPos(x0+x,y0+y,z0+z)
                    st=level.getBlockState(bp)
                    if st.isAir():
                        out.append("0")
                    else:
                        shp=st.getCollisionShape(level,bp)
                        if shp.isEmpty():
                            out.append("0")
                        else:
                            try:
                                n=int(shp.bounds().maxY*16+0.5)
                                if n<1:
                                    n=1
                                if n>24:
                                    n=24
                                out.append(chr(48+n))
                            except Exception:
                                out.append("@")
        writer.println("OK|||"+"".join(out))
        writer.flush();s.close()
except Exception as e:
    if writer:
        writer.println("ERROR|||"+str(e).replace("\n"," "));writer.flush()
"""


class Grid:
    """分塊快取的碰撞高度表。

    每格存「碰撞箱頂端高度」(0=無碰撞, 1=整格, >1=圍欄類會溢出到上一格)。
    未掃描的區域與 plot 外一律當實心。
    """

    def __init__(self):
        self.tiles = {}        # (tx, ty, tz) -> (bytes, scan_time)
        self.bounds = None     # (minx, maxx, minz, maxz) 或 None
        self.extra = None      # 暫時障礙 {cell: 到期時間}；與地圖一起影響 A*、拉直、碰撞預測

    def clear(self):
        self.tiles.clear()

    def put_box(self, x0, y0, z0, sx, sy, sz, data, now):
        raw = data.encode("ascii")
        for ti in range(sx // TILE_XZ):
            for tj in range(sy // TILE_Y):
                for tk in range(sz // TILE_XZ):
                    rows = []
                    for i in range(TILE_XZ):
                        for j in range(TILE_Y):
                            base = ((ti * TILE_XZ + i) * sy + (tj * TILE_Y + j)) * sz + tk * TILE_XZ
                            rows.append(raw[base:base + TILE_XZ])
                    key = (x0 // TILE_XZ + ti, y0 // TILE_Y + tj, z0 // TILE_XZ + tk)
                    self.tiles[key] = (b"".join(rows), now)

    def h(self, x, y, z):
        ex = self.extra
        if ex and (x, y, z) in ex:
            return 1.0
        b = self.bounds
        if b is not None and not (b[0] <= x <= b[1] and b[2] <= z <= b[3]):
            return 1.0
        if y < 0:
            return 1.0
        if y > 255:
            return 0.0
        t = self.tiles.get((x >> 4, y >> 3, z >> 4))
        if t is None:
            return 1.0
        code = t[0][(((x & 15) * TILE_Y) + (y & 7)) * TILE_XZ + (z & 15)]
        if code == 48:
            return 0.0
        return (code - 48) / 16.0

    def cell_top(self, x, y, z):
        """這格內方塊頂端（相對該格底部），含下方圍欄類溢出。"""
        t = self.h(x, y, z)
        if t < 1.0:
            below = self.h(x, y - 1, z)
            if below > 1.0:
                t = max(t, below - 1.0)
        return t

    def free(self, x, y, z):
        """腳在 (x,y,z)、頭在 (x,y+1,z) 兩格都完全沒有碰撞。"""
        return self.cell_top(x, y, z) == 0.0 and self.cell_top(x, y + 1, z) == 0.0

    def collides(self, px, py, pz, hw=HALF_W):
        """玩家（腳底座標 px,py,pz）站在這裡會不會卡進方塊。"""
        x0, x1 = math.floor(px - hw), math.floor(px + hw)
        z0, z1 = math.floor(pz - hw), math.floor(pz + hw)
        ya = math.floor(py)
        yb = math.floor(py + HEIGHT - 1e-6)
        lim = py + STEP_EPS
        for x in range(x0, x1 + 1):
            for z in range(z0, z1 + 1):
                for y in range(ya, yb + 1):
                    top = self.cell_top(x, y, z)
                    if top > 0.0 and y + top > lim:
                        return True
        return False

    def clear_line(self, a, b, hw=HALF_W):
        """碰撞箱從 a 直線移到 b 是否暢通（起點 0.6 格內用嚴格半寬，避免貼牆時全判失敗）。"""
        d = _dist3(a, b)
        n = max(1, int(d / SAMPLE) + 1)
        for s in range(n + 1):
            t = s / n
            w = HALF_W if t * d < 0.6 else hw
            if self.collides(a[0] + (b[0] - a[0]) * t,
                             a[1] + (b[1] - a[1]) * t,
                             a[2] + (b[2] - a[2]) * t, w):
                return False
        return True


def scan_grid(x0, y0, z0, sx, sy, sz, timeout=10.0):
    script = (_SOLID_SCRIPT
              .replace("__X0__", str(x0)).replace("__Y0__", str(y0)).replace("__Z0__", str(z0))
              .replace("__SX__", str(sx)).replace("__SY__", str(sy)).replace("__SZ__", str(sz)))
    raw = _bridge_call(script, timeout=timeout)
    if not raw or not raw.startswith("OK|||"):
        return None
    data = raw[5:]
    if len(data) != sx * sy * sz:
        return None
    return data


# 6 面鄰居 + 4 個水平斜走（斜走需兩側格都自由，避免切角）
_NB = (
    (1, 0, 0, 1.0), (-1, 0, 0, 1.0), (0, 0, 1, 1.0), (0, 0, -1, 1.0),
    (0, 1, 0, 1.0), (0, -1, 0, 1.0),
    (1, 0, 1, 1.4142), (1, 0, -1, 1.4142), (-1, 0, 1, 1.4142), (-1, 0, -1, 1.4142),
)
_WALL_COST = 0.35


def astar(grid, start, goal, blocked=None, max_nodes=15000):
    blocked = blocked if blocked is not None else {}
    fcache = {}

    def free(c):
        v = fcache.get(c)
        if v is None:
            v = (c not in blocked) and grid.free(c[0], c[1], c[2])
            fcache[c] = v
        return v

    pcache = {}

    def wall_pen(c):
        v = pcache.get(c)
        if v is None:
            n = 0
            for dx, dz in ((1, 0), (-1, 0), (0, 1), (0, -1)):
                if not free((c[0] + dx, c[1], c[2] + dz)):
                    n += 1
            v = _WALL_COST * min(n, 2)
            pcache[c] = v
        return v

    def h(c):
        return math.sqrt((c[0] - goal[0]) ** 2 + (c[1] - goal[1]) ** 2 + (c[2] - goal[2]) ** 2)

    openh = [(1.3 * h(start), 0.0, start)]
    best_g = {start: 0.0}
    came = {}
    closed = set()
    while openh and len(closed) < max_nodes:
        _, gc, cur = heapq.heappop(openh)
        if cur in closed:
            continue
        if cur == goal:
            path = [cur]
            while cur in came:
                cur = came[cur]
                path.append(cur)
            path.reverse()
            return path
        closed.add(cur)
        for dx, dy, dz, cost in _NB:
            nb = (cur[0] + dx, cur[1] + dy, cur[2] + dz)
            if nb in closed or not free(nb):
                continue
            if dx != 0 and dz != 0:
                if not free((cur[0] + dx, cur[1], cur[2])) or not free((cur[0], cur[1], cur[2] + dz)):
                    continue
            if dy > 0:
                cost = 1.2          # 往上略貴：同樣長度優先繞過去，不要一直爬高
            ng = gc + cost + wall_pen(nb)
            if ng < best_g.get(nb, 1e18):
                best_g[nb] = ng
                came[nb] = cur
                heapq.heappush(openh, (ng + 1.3 * h(nb), ng, nb))
    return None


def nearest_free(grid, cell, blocked=None, r=5):
    blocked = blocked if blocked is not None else {}

    def ok(c):
        return c not in blocked and grid.free(c[0], c[1], c[2])

    if ok(cell):
        return cell
    best, best_d = None, 1e18
    for dx in range(-r, r + 1):
        for dy in range(-r, r + 1):
            for dz in range(-r, r + 1):
                c = (cell[0] + dx, cell[1] + dy, cell[2] + dz)
                if ok(c):
                    d = dx * dx + dy * dy + dz * dz
                    if d < best_d:
                        best, best_d = c, d
    return best


def smooth(grid, pts, max_look=40):
    """貪心拉直：從目前點盡量往後找到碰撞箱走得過去的最遠點（帶餘裕）。"""
    out = [pts[0]]
    i = 0
    while i < len(pts) - 1:
        j = i + 1
        while j + 1 < len(pts) and j - i < max_look and grid.clear_line(pts[i], pts[j + 1], PATH_HW):
            j += 1
        out.append(pts[j])
        i = j
    return out


def _mark_blocked(blocked, until, ax, az, fy, ux, uz):
    """把 (ax,az) 前方的柱狀區域（左右各擴 1 格、上下 4 層）暫時標為障礙，
    讓重規劃一次就學到整面牆，而不是每次只繞開一格。"""
    fx, fz = math.floor(ax), math.floor(az)
    px, pz = (-uz, ux)          # 垂直於前進方向（水平）
    for side in (-1, 0, 1):
        sx = math.floor(ax + px * side)
        sz = math.floor(az + pz * side)
        for yy in range(fy - 1, fy + 3):
            blocked[(sx, yy, sz)] = until
    for yy in range(fy - 1, fy + 3):
        blocked[(fx, yy, fz)] = until


def _cell(p):
    return (math.floor(p[0]), math.floor(p[1] + 1e-3), math.floor(p[2]))


class Navigator:
    def __init__(self, cleaner):
        self.c = cleaner
        self.grid = Grid()
        self._scan_t = 0.0
        self._fails = 0
        self._disabled_until = 0.0
        self._last_shot = 0.0
        self._aote_fail = 0
        self._aote_off_until = 0.0

    # ── 地圖 ──
    def invalidate(self):
        self.grid.clear()

    def _bounds(self):
        center = PLOT_CENTERS.get(self.c._plot_num)
        if center is None:
            return None
        cx, cz = center
        return (math.floor(cx - PLOT_HALF), math.floor(cx + PLOT_HALF),
                math.floor(cz - PLOT_HALF), math.floor(cz + PLOT_HALF))

    def _ensure(self, a, b, force=True):
        """確保 a、b 周圍的分塊都已掃描。回傳是否可用。"""
        g = self.grid
        g.bounds = self._bounds()
        now = time.time()
        lo = (min(a[0], b[0]) - PAD_H, min(a[1], b[1]) - PAD_V, min(a[2], b[2]) - PAD_H)
        hi = (max(a[0], b[0]) + PAD_H, max(a[1], b[1]) + PAD_V, max(a[2], b[2]) + PAD_H)
        tx0, tx1 = math.floor(lo[0]) >> 4, math.floor(hi[0]) >> 4
        ty0, ty1 = max(0, math.floor(lo[1]) >> 3), min(31, math.floor(hi[1]) >> 3)
        tz0, tz1 = math.floor(lo[2]) >> 4, math.floor(hi[2]) >> 4
        keys = [(tx, ty, tz) for tx in range(tx0, tx1 + 1)
                for ty in range(ty0, ty1 + 1) for tz in range(tz0, tz1 + 1)]
        if len(keys) > 120:
            return False
        missing = [k for k in keys if k not in g.tiles or now - g.tiles[k][1] > TILE_TTL]
        if not missing:
            return True
        if not force and now - self._scan_t < RESCAN_GAP:
            return False
        self._scan_t = now

        def scan_tiles(kx0, ky0, kz0, kx1, ky1, kz1):
            sx = (kx1 - kx0 + 1) * TILE_XZ
            sy = (ky1 - ky0 + 1) * TILE_Y
            sz = (kz1 - kz0 + 1) * TILE_XZ
            data = scan_grid(kx0 * TILE_XZ, ky0 * TILE_Y, kz0 * TILE_XZ, sx, sy, sz)
            if data is None:
                return False
            g.put_box(kx0 * TILE_XZ, ky0 * TILE_Y, kz0 * TILE_XZ, sx, sy, sz, data, time.time())
            return True

        mx0, mx1 = min(k[0] for k in missing), max(k[0] for k in missing)
        my0, my1 = min(k[1] for k in missing), max(k[1] for k in missing)
        mz0, mz1 = min(k[2] for k in missing), max(k[2] for k in missing)
        box_n = (mx1 - mx0 + 1) * (my1 - my0 + 1) * (mz1 - mz0 + 1)
        if box_n <= max(12, 3 * len(missing)):
            return scan_tiles(mx0, my0, mz0, mx1, my1, mz1)
        ok = True
        for k in missing:
            ok = scan_tiles(k[0], k[1], k[2], k[0], k[1], k[2]) and ok
        return ok

    # ── 離地高度 ──
    def ground_y(self, pos, depth=30.0, force=True):
        """腳下最近的地面高度。None=地圖未知；-inf=腳下 depth 格內都沒有地面。"""
        if not self._ensure(pos, (pos[0], pos[1] - depth, pos[2]), force=force):
            return None
        g = self.grid
        top_y = math.floor(pos[1] + 1e-3)
        low_y = max(0, math.floor(pos[1] - depth))
        for yy in range(top_y, low_y - 1, -1):
            m = 0.0
            for ox, oz in ((0, 0), (-HALF_W, -HALF_W), (HALF_W, HALF_W),
                           (-HALF_W, HALF_W), (HALF_W, -HALF_W)):
                t = g.cell_top(math.floor(pos[0] + ox), yy, math.floor(pos[2] + oz))
                if t > m:
                    m = t
            if m > 0.0:
                return yy + m
        return float("-inf")

    def descent_ok(self, pos):
        """現在能不能往下飛：80 以上一律可以；以下要確認離地夠高，免得落地掉飛。"""
        if pos[1] > NO_SNEAK_BELOW_Y:
            return True
        if not ALLOW_LOW_DESCENT:
            return False
        cache = getattr(self, "_gy_cache", None)
        now = time.time()
        if cache is None or now - cache[0] > 0.4 or math.hypot(pos[0] - cache[1], pos[2] - cache[2]) > 1.5:
            cache = (now, pos[0], pos[2], self.ground_y(pos))
            self._gy_cache = cache
        gy = cache[3]
        if gy is None:
            return False
        return gy < -1e8 or pos[1] - gy > MIN_GROUND_CLEARANCE + 0.3

    @staticmethod
    def _clip(pos, P):
        dx, dy, dz = P[0] - pos[0], P[1] - pos[1], P[2] - pos[2]
        s = min(1.0,
                WIN_H / max(abs(dx), abs(dz), 1e-6),
                WIN_V / max(abs(dy), 1e-6))
        return (pos[0] + dx * s, pos[1] + dy * s, pos[2] + dz * s)

    # ── 被動檢查：直線逼近會不會撞牆？ ──
    def need_path(self, pos, P, stop_d):
        if time.time() < self._disabled_until:
            return False
        eye = (pos[0], pos[1] + PLAYER_EYE_HEIGHT, pos[2])
        d = _dist3(eye, P)
        if d <= stop_d:
            return False
        k = (d - stop_d * 0.8) / d
        tgt = (pos[0] + (P[0] - eye[0]) * k,
               pos[1] + (P[1] - eye[1]) * k,
               pos[2] + (P[2] - eye[2]) * k)
        tgt = self._clip(pos, tgt)
        if not self._ensure(pos, tgt, force=False):
            return False
        return not self.grid.clear_line(pos, tgt)

    # ── 規劃 ──
    def _plan(self, pos, goal, blocked):
        if not self._ensure(pos, goal, force=True):
            return None
        g = self.grid
        s = nearest_free(g, _cell(pos), blocked)
        e = nearest_free(g, _cell(goal), blocked)
        if not s or not e:
            return None
        path = astar(g, s, e, blocked)
        if not path:
            return None
        return path

    # ── 主動導航：走到離 P 不超過 arrive ──
    def go_near(self, P, arrive, timeout=25.0):
        c = self.c
        k = c.k
        g = self.grid
        g.bounds = self._bounds()
        t0 = time.time()

        blocked = {}                 # 格子 -> 到期時間
        g.extra = blocked
        wps = None                   # 目前路徑折線（wps[0] 是規劃時的位置）
        wp_i = 1
        plan_t = 0.0
        plan_fail = 0
        skip_t = 0.0
        stall_pos = None
        stall_t = t0
        stuck = 0
        best_d = 1e18
        best_t = t0
        stale_resets = 0
        rise_until = 0.0
        was_blocked = False

        try:
            while not c._stop_flag and time.time() - t0 < timeout:
                if c._out_of_plot:
                    return False
                now = time.time()
                pos = tuple(minescript.player_position())
                eye = (pos[0], pos[1] + PLAYER_EYE_HEIGHT, pos[2])
                d_eye = _dist3(eye, P)
                if d_eye <= arrive:
                    self._fails = 0
                    return True

                # 進度監看
                if d_eye < best_d - 0.5:
                    best_d, best_t = d_eye, now
                elif now - best_t > PROGRESS_TIMEOUT:
                    stale_resets += 1
                    if stale_resets >= 2:
                        return self._fail()
                    self.invalidate()
                    blocked.clear()
                    wps = None
                    best_t = now
                for cell in [cc for cc, exp in blocked.items() if exp < now]:
                    del blocked[cell]

                # 是否需要重規劃
                need = wps is None or now - plan_t > REPLAN_SEC
                if not need and wp_i < len(wps) and now - skip_t >= SKIP_CHECK_SEC:
                    skip_t = now
                    # 先嘗試直接跳到更遠、仍可直達的路徑點（邊走邊拉直）
                    for j in range(min(len(wps) - 1, wp_i + 4), wp_i, -1):
                        if g.clear_line(pos, wps[j], PATH_HW):
                            wp_i = j
                            break
                    else:
                        if not g.clear_line(pos, wps[wp_i], PATH_HW):
                            need = True

                if need and now - plan_t >= MIN_REPLAN:
                    plan_t = now
                    goal = self._clip(pos, P)
                    path = self._plan(pos, goal, blocked)
                    if path is None or len(path) < 2:
                        plan_fail += 1
                        if plan_fail == 1:
                            self.invalidate()
                        blocked.clear()
                        k.set("forward", False)
                        k.set("jump", False)
                        k.set("sneak", False)
                        if plan_fail >= 3:
                            return self._fail()
                        time.sleep(0.05)
                        continue
                    plan_fail = 0
                    pts = [pos] + [(x + 0.5, float(y), z + 0.5) for x, y, z in path[1:]]
                    wps = smooth(g, pts)
                    wp_i = 1 if len(wps) > 1 else 0
                    skip_t = now
                if wps is None or wp_i >= len(wps) or len(wps) < 2:
                    time.sleep(0.02)
                    continue

                # 前進到下一個路徑點
                tgt = wps[wp_i]
                last = wp_i == len(wps) - 1
                dx, dy, dz = tgt[0] - pos[0], tgt[1] - pos[1], tgt[2] - pos[2]
                hd = math.hypot(dx, dz)
                can_down = self.descent_ok(pos)
                dy_eff = dy if (can_down or dy > 0) else 0.0   # 不能蹲下時，比我低的目標視為已達成
                if hd < (0.3 if last else 0.45) and abs(dy_eff) < 0.45:
                    if last:
                        wps = None
                    else:
                        wp_i += 1
                    continue
                if not last and wp_i >= 1:
                    prev = wps[wp_i - 1]
                    sx, sz = tgt[0] - prev[0], tgt[2] - prev[2]
                    if (sx * sx + sz * sz) > 0.09 and (dx * sx + dz * sz) <= 0 and hd < 1.3 and abs(dy_eff) < 0.8:
                        wp_i += 1
                        continue

                # ── AOTE：只有整段跳距都在這條直線內才傳 ──
                d3 = _dist3(pos, tgt)
                ty, tp = _calc_yaw_pitch(pos, (tgt[0], tgt[1] + PLAYER_EYE_HEIGHT, tgt[2]))
                if hd > 0.35:
                    tp_fly = max(-35.0, min(35.0, tp))
                    c.aim.set_target(ty, tp_fly, speed=9.0)
                    err = c.aim.error_to(ty, tp_fly)
                else:
                    err = 0.0

                hop_possible = (
                    now >= self._aote_off_until
                    and d3 >= AOTE_SHOT_MAX + 0.3
                    and now - self._last_shot >= HOP_MIN_GAP
                )
                if hop_possible:
                    ux, uy, uz = dx / d3, dy / d3, dz / d3
                    hop_end = (pos[0] + ux * AOTE_SHOT_MAX,
                               pos[1] + uy * AOTE_SHOT_MAX,
                               pos[2] + uz * AOTE_SHOT_MAX)
                    if g.clear_line(pos, hop_end, PATH_HW):
                        c.aim.set_target(ty, tp, speed=9.0)   # AOTE 需要精確視角（不夾角）
                        if c.aim.error_to(ty, tp) < HOP_ALIGN_DEG:
                            k.set("forward", False)
                            k.set("jump", False)
                            k.set("sneak", False)
                            c._aote_shot()
                            self._last_shot = time.time()
                            time.sleep(0.12)
                            new = tuple(minescript.player_position())
                            if _dist3(new, pos) > 2.0:
                                self._aote_fail = 0
                            else:
                                self._aote_fail += 1
                                _mark_blocked(blocked, time.time() + BLOCK_TTL,
                                              pos[0] + ux * 1.5, pos[2] + uz * 1.5,
                                              math.floor(pos[1] + 1e-3 + uy * 1.5),
                                              ux / max(math.hypot(ux, uz), 1e-6),
                                              uz / max(math.hypot(ux, uz), 1e-6))
                                if self._aote_fail >= AOTE_FAIL_LIMIT:
                                    self._aote_fail = 0
                                    self._aote_off_until = time.time() + AOTE_OFF_SEC
                            wps = None
                            plan_t = 0.0
                            stall_pos, stall_t = None, time.time()
                            continue
                        err = c.aim.error_to(ty, tp)   # 對準中：照常用下面的 WASD 往那個方向飛

                # ── 飛行控制（以碰撞預測為準）──
                fwd = hd > 0.3 and err < 40.0
                want_up = dy > 0.25
                want_down = dy < -0.35 and can_down

                if hd > 0.05:
                    ux2, uz2 = dx / hd, dz / hd
                else:
                    ux2 = uz2 = 0.0
                ax, az = pos[0] + ux2 * LOOK_AHEAD, pos[2] + uz2 * LOOK_AHEAD
                blocked_ahead = hd > 0.3 and g.collides(ax, pos[1], az)

                if blocked_ahead:
                    rise = None
                    r = 0.25
                    while r <= 3.01:
                        if not g.collides(ax, pos[1] + r, az):
                            rise = r
                            break
                        r += 0.25
                    if rise is None:
                        # 上方也過不去：把前方格子標為障礙，馬上重規劃
                        _mark_blocked(blocked, now + BLOCK_TTL, ax, az,
                                      math.floor(pos[1] + 1e-3), ux2, uz2)
                        wps = None
                        plan_t = 0.0
                        k.set("forward", False)
                        continue
                    fwd = False
                    want_up = True
                    want_down = False
                    rise_until = now + RISE_HOLD
                    was_blocked = True
                elif was_blocked or now < rise_until:
                    # 前方剛清空：多升一小段，讓腳底完全離開方塊高度再前進
                    fwd = False
                    want_up = True
                    want_down = False
                    if now >= rise_until:
                        was_blocked = False

                if want_down and g.collides(pos[0], pos[1] - 0.3, pos[2]):
                    want_down = False   # 腳下就是地面，不必蹲

                k.set("forward", fwd)
                k.set("jump", want_up)
                k.set("sneak", want_down)

                # ── 卡住偵測 ──
                if stall_pos is None:
                    stall_pos, stall_t = pos, now
                elif now - stall_t >= STALL_CHECK_SEC:
                    moved = _dist3(pos, stall_pos)
                    if moved < STALL_MOVE and (fwd or want_up or want_down):
                        stuck += 1
                        if stuck == 1:
                            rise_until = now + 0.35          # 先往上抬一下
                            was_blocked = True
                        else:
                            _mark_blocked(blocked, now + BLOCK_TTL, ax, az,
                                          math.floor(pos[1] + 1e-3), ux2, uz2)
                            wps = None
                            plan_t = 0.0
                            if stuck >= 5:
                                return self._fail()
                    elif moved > 0.5:
                        stuck = 0
                    stall_pos, stall_t = pos, now

                time.sleep(0.03)
        finally:
            g.extra = None
            c._stop_movement()
        return False

    def _fail(self):
        self._fails += 1
        if self._fails >= NAV_FAIL_LIMIT:
            self._fails = 0
            self._disabled_until = time.time() + NAV_DISABLE_SEC
            minescript.echo("§e[PestNav] 尋路連續失敗，暫時改回直線逼近")
        return False
