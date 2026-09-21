"""寒潮气象过程。

同一气象过程用于多策略对照: 给定相同 seed 时输出完全一致,
迟报补算时也可以按时间索引精确重放。
"""
from __future__ import annotations

import math
import random
from dataclasses import dataclass
from typing import List


@dataclass
class WeatherSample:
    t_hours: float          # 距过程起点的小时数
    outdoor_temp: float     # 室外温度 °C
    wind_speed: float       # 风速 m/s (影响建筑散热系数修正)


class ColdSnap:
    """一次寒潮过程: 基线温度按分段曲线下降/回升, 叠加可复现扰动。"""

    def __init__(
        self,
        duration_hours: float = 72.0,
        step_hours: float = 1.0 / 12.0,   # 5 分钟
        base_temp: float = 2.0,
        min_temp: float = -14.0,
        drop_start_h: float = 6.0,
        drop_end_h: float = 24.0,
        recover_start_h: float = 54.0,
        seed: int = 2026,
    ) -> None:
        self.step_hours = step_hours
        self.n_steps = int(round(duration_hours / step_hours))
        rng = random.Random(seed)
        self._samples: List[WeatherSample] = []
        for i in range(self.n_steps + 1):
            t = i * step_hours
            if t < drop_start_h:
                temp = base_temp
            elif t < drop_end_h:
                frac = (t - drop_start_h) / (drop_end_h - drop_start_h)
                temp = base_temp + (min_temp - base_temp) * frac
            elif t < recover_start_h:
                temp = min_temp
            else:
                frac = (t - recover_start_h) / (duration_hours - recover_start_h)
                temp = min_temp + (base_temp - min_temp) * frac
            # 日波动 + 可复现随机扰动
            temp += 1.5 * math.sin(2.0 * math.pi * t / 24.0)
            temp += rng.gauss(0.0, 0.3)
            wind = max(0.5, 4.0 + 2.0 * math.sin(2.0 * math.pi * t / 18.0)
                       + rng.gauss(0.0, 0.5))
            self._samples.append(WeatherSample(t, temp, wind))

    def at(self, step: int) -> WeatherSample:
        step = max(0, min(step, len(self._samples) - 1))
        return self._samples[step]
