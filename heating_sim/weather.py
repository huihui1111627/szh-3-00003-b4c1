"""寒潮气象过程:构造、迟报数据与补算支持。"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import List, Optional

from .model import WeatherPoint, WeatherProcess


def make_cold_wave(duration_h: float = 48, dt_min: int = 10,
                   base_c: float = 2.0, trough_c: float = -22.0,
                   trough_h: float = 20.0, wind_peak_ms: float = 7.0,
                   extra_cold: bool = False) -> WeatherProcess:
    """构造一次寒潮过程:温和起点 -> 夜间极寒 -> 回暖。"""
    points: List[WeatherPoint] = []
    n = int(duration_h * 60 / dt_min)
    for i in range(n + 1):
        h = i * dt_min / 60.0
        # 以 trough_h 为中心的寒潮低谷(余弦形态)
        x = max(-1.0, min(1.0, (h - trough_h) / 16.0))
        depth = 0.5 * (1.0 + math.cos(x * math.pi))
        tout = base_c + (trough_c - base_c) * depth
        if extra_cold and 14 <= h <= 30:
            tout -= 3.0  # 更冷的实况(迟报后才知晓)
        wind = wind_peak_ms * depth
        points.append(WeatherPoint(int(h * 60), round(tout, 3), round(wind, 2)))
    return WeatherProcess(points)


@dataclass
class LateReport:
    """一条迟报数据:覆盖某时间窗的实测值。"""
    start_minute: int
    end_minute: int
    field: str                  # tout_c / wind_ms
    observed: float
    received_minute: int        # 实际收报时间(分钟)
    note: str = ""


class WeatherProvider:
    """带迟报机制的气象数据源。

    - initial: 预报过程(先验)
    - late_reports: 实测报文,带收报时刻;在 received_minute 之前"尚未收到"
    - observed_at(rt_minute): 返回该运行时刻已知的气象(预报 + 已到报实测)
    - apply_late_data: 全部到齐后的"补算"版本
    """

    def __init__(self, initial: WeatherProcess,
                 late_reports: Optional[List[LateReport]] = None):
        self.initial = initial
        self.late_reports = late_reports or []

    def known_at(self, rt_minute: int) -> WeatherProcess:
        wp = self.initial.clone()
        for r in self.late_reports:
            if r.received_minute <= rt_minute:
                _overlay(wp, r.start_minute, r.end_minute, r.field, r.observed,
                         tag=f"实测@{r.received_minute}min: {r.note}")
        return wp

    def fully_observed(self) -> WeatherProcess:
        wp = self.initial.clone()
        for r in sorted(self.late_reports, key=lambda x: x.received_minute):
            _overlay(wp, r.start_minute, r.end_minute, r.field, r.observed,
                     tag=f"迟报实测(收于{r.received_minute}min): {r.note}")
        return wp

    def pending_at(self, rt_minute: int) -> List[LateReport]:
        return [r for r in self.late_reports if r.received_minute > rt_minute]


def _overlay(wp: WeatherProcess, start: int, end: int, field_name: str,
             value: float, tag: str) -> None:
    """在已有气象序列上叠加实测(线性过渡,避免台阶)。"""
    pts = wp.points
    idxs = [i for i, p in enumerate(pts) if start <= p.minute <= end]
    for i in idxs:
        p = pts[i]
        if field_name == "tout_c":
            p.tout_c = value
        elif field_name == "wind_ms":
            p.wind_ms = value
    if idxs:
        wp.late_annotations[pts[idxs[0]].minute] = tag
        wp.late_annotations[pts[idxs[-1]].minute] = tag
