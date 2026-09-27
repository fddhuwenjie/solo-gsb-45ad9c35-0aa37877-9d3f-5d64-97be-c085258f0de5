"""围栏事件判定引擎。

将经纬度投影到以围栏质心为原点的局部平面坐标系（等距圆柱近似），
在米制坐标下用 Shapely 做距离计算，配合进入/离开缓冲距离形成迟滞区间，
再叠加停留时长（dwell）做抖动去抖，产出 enter / leave / stay 事件。
"""
from __future__ import annotations

import bisect
import math
from dataclasses import dataclass

from shapely.geometry import Point, Polygon

EARTH_RADIUS_M = 6_371_000.0

EVENT_ENTER = "enter"
EVENT_LEAVE = "leave"
EVENT_STAY = "stay"


def _project(lat: float, lon: float, lat0: float, lon0: float) -> tuple[float, float]:
    """经纬度 -> 以 (lat0, lon0) 为原点的局部米制坐标（等距圆柱近似）。"""
    x = math.radians(lon - lon0) * EARTH_RADIUS_M * math.cos(math.radians(lat0))
    y = math.radians(lat - lat0) * EARTH_RADIUS_M
    return (x, y)


@dataclass
class FenceConfig:
    enter_buffer_m: float = 0.0   # 距多边形 <= 该距离即视为“进入”（提前触发）
    exit_buffer_m: float = 0.0    # 距多边形 > 该距离才视为“离开”（迟滞带）
    enter_dwell_s: float = 0.0    # 持续在内部这么久才确认 enter
    exit_dwell_s: float = 0.0     # 持续在外部这么久才确认 leave
    stay_min_s: float = 300.0     # 单次访问内连续停留达到该时长产生一次 stay


@dataclass
class TrackPoint:
    ts: float   # epoch 秒
    lat: float
    lon: float


@dataclass
class FenceEvent:
    fence_id: int
    type: str   # enter | leave | stay
    ts: float   # 事件发生的轨迹时间（非确认时间）


class ProjectedFence:
    """投影到局部米制坐标系的围栏多边形，支持带孔多边形。"""

    def __init__(self, fence_id: int, polygon: Polygon, config: FenceConfig):
        self.fence_id = fence_id
        self.config = config
        centroid = polygon.centroid
        self._lat0, self._lon0 = centroid.y, centroid.x
        shell = [_project(lat, lon, self._lat0, self._lon0)
                 for lon, lat in polygon.exterior.coords]
        holes = [
            [_project(lat, lon, self._lat0, self._lon0) for lon, lat in ring.coords]
            for ring in polygon.interiors
        ]
        self._poly = Polygon(shell, holes)

    def classify(self, lat: float, lon: float, prev_inside: bool) -> bool:
        """判定点是否在围栏内。

        点到多边形的距离 d（内部为 0，孔洞内 > 0，因此孔洞天然视为外部）：
        - d <= enter_buffer_m  -> 内部
        - d >  exit_buffer_m   -> 外部
        - 两者之间为迟滞灰区   -> 维持上一状态，抑制边界抖动
        """
        x, y = _project(lat, lon, self._lat0, self._lon0)
        d = self._poly.distance(Point(x, y))
        if d <= self.config.enter_buffer_m:
            return True
        if d > self.config.exit_buffer_m:
            return False
        return prev_inside


def compute_events(
    fence: ProjectedFence, points: list[TrackPoint]
) -> tuple[list[FenceEvent], bool, float | None]:
    """对单个 (设备, 围栏) 按时间有序轨迹计算事件。

    points 必须已按 ts 升序排列。返回 (事件列表, 最终是否在围栏内, 当前状态起始时间)。
    状态机：
    - 分类结果与当前状态一致 -> 取消未确认的待定迁移；
    - 分类结果不同 -> 记录待定迁移起点，持续满足 dwell 时长后才确认迁移并产出事件，
      事件时间取条件首次满足的时刻；期间条件被打破则待定迁移作废（去抖）。
    - 进入围栏后连续停留达到 stay_min_s，产出一次 stay（每次访问最多一次）。
    """
    cfg = fence.config
    events: list[FenceEvent] = []
    inside = False
    state_since: float | None = None
    pending_since: float | None = None
    stay_emitted = False

    for pt in points:
        now_inside = fence.classify(pt.lat, pt.lon, inside)
        if now_inside == inside:
            pending_since = None
        else:
            if pending_since is None:
                pending_since = pt.ts
            dwell = cfg.enter_dwell_s if now_inside else cfg.exit_dwell_s
            if pt.ts - pending_since >= dwell:
                inside = now_inside
                state_since = pending_since
                events.append(FenceEvent(
                    fence.fence_id,
                    EVENT_ENTER if inside else EVENT_LEAVE,
                    pending_since,
                ))
                pending_since = None
                stay_emitted = False
        if inside and not stay_emitted and pt.ts - state_since >= cfg.stay_min_s:
            events.append(FenceEvent(fence.fence_id, EVENT_STAY, state_since + cfg.stay_min_s))
            stay_emitted = True

    return events, inside, state_since


# ---------- 容量占用核算 ----------

@dataclass
class OccupancySegment:
    """[start, end) 内（容量上限, 在场设备集合）均恒定的分段。"""

    start: float
    end: float
    capacity: int | None      # 该时段生效的容量上限；None 表示无规则覆盖（不限）
    devices: tuple[str, ...]  # 在场设备，按 ID 排序

    @property
    def occupancy(self) -> int:
        return len(self.devices)

    @property
    def excess(self) -> int:
        """超出上限的人数；无生效上限时恒为 0。"""
        if self.capacity is None:
            return 0
        return max(0, len(self.devices) - self.capacity)


def reconstruct_intervals(
    events: list[tuple[str, str, float]], start: float, end: float
) -> dict[str, list[tuple[float, float]]]:
    """从 enter/leave 事件流还原每台设备在 [start, end) 内的半开在场区间。

    events 为 (device_id, type, ts)，仅 enter/leave 参与（stay 不改变在场状态）。
    半开语义：t 时刻 enter 的设备自 t 起在场，t 时刻 leave 的设备自 t 起不在场。
    start 之前已进入的访问截断到 start；end 时仍未离开的访问截断到 end。
    """
    intervals: dict[str, list[tuple[float, float]]] = {}
    open_since: dict[str, float] = {}
    for device_id, etype, ts in sorted(events, key=lambda e: e[2]):
        if ts >= end:
            break  # 窗口外的事件不影响 [start, end)
        if etype == EVENT_ENTER:
            open_since[device_id] = ts
        elif etype == EVENT_LEAVE:
            enter_ts = open_since.pop(device_id, None)
            if enter_ts is None:
                continue  # 无配对 enter 的 leave，忽略
            lo = max(enter_ts, start)  # 查询前已进入 -> 截断到 start
            if lo < ts:
                intervals.setdefault(device_id, []).append((lo, ts))
    # 查询结束仍未离开 -> 截断到 end
    for device_id, enter_ts in open_since.items():
        lo = max(enter_ts, start)
        if lo < end:
            intervals.setdefault(device_id, []).append((lo, end))
    return intervals


def compute_occupancy(
    events: list[tuple[str, str, float]],
    rules: list[tuple[float, int]],
    start: float,
    end: float,
) -> list[OccupancySegment]:
    """计算围栏在 [start, end) 内的容量占用分段。

    - events: (device_id, type, ts) 事件流（仅 enter/leave 参与）
    - rules:  (effective_from, capacity) 容量规则；某时刻的上限取生效时刻不晚于
      该时刻的最后一条规则，无规则覆盖时上限为 None（不限）
    - 同一时刻的设备进出与规则切换按一个边界原子处理：半开语义下该时刻的
      全部变化同时生效，不产生中间态或零长度分段
    返回尽可能长的恒定状态分段（相邻分段的 (capacity, devices) 必不相同）。
    """
    intervals = reconstruct_intervals(events, start, end)

    bounds = {start, end}
    for ivs in intervals.values():
        for lo, hi in ivs:
            if start < lo < end:
                bounds.add(lo)
            if start < hi < end:
                bounds.add(hi)
    ordered_rules = sorted(rules)
    for eff, _ in ordered_rules:
        if start < eff < end:
            bounds.add(eff)
    cuts = sorted(bounds)

    effs = [eff for eff, _ in ordered_rules]
    caps = [cap for _, cap in ordered_rules]

    def capacity_at(t: float) -> int | None:
        i = bisect.bisect_right(effs, t) - 1
        return caps[i] if i >= 0 else None

    segments: list[OccupancySegment] = []
    for lo, hi in zip(cuts, cuts[1:]):
        devices = tuple(sorted(
            d for d, ivs in intervals.items()
            if any(a <= lo < b for a, b in ivs)
        ))
        capacity = capacity_at(lo)
        if segments and segments[-1].capacity == capacity and segments[-1].devices == devices:
            segments[-1].end = hi  # 状态未变，延长当前分段
        else:
            segments.append(OccupancySegment(lo, hi, capacity, devices))
    return segments
