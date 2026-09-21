"""策略二:寒潮前提前升温,利用建筑热惯性蓄热;低谷优先保关键分区。"""
from __future__ import annotations

from ..model import Action, Trace, WeatherProcess
from ..policies import Policy, _target_supply_from_curve


class PreHeatBeforeColdWave(Policy):
    name = "提前蓄热保供"
    description = "寒潮抵达前 8 小时逐步拉高供水温度给建筑充热,峰值温和运行"

    def decide(self, minute, weather: WeatherProcess, trace: Trace):
        # 寻找未来 8 小时内最冷点
        future_t = min(
            weather.weather_at(minute + d * 60).effective_tout()
            for d in range(0, 9))
        t_curve = _target_supply_from_curve(future_t)
        # 在最冷点到来之前加预热量
        preheat = 0.0
        for d in range(1, 9):
            wt = weather.weather_at(minute + d * 60).effective_tout()
            now_t = weather.weather_at(minute).effective_tout()
            if wt < now_t - 3.0:
                preheat = max(preheat, 7.0 * (1.0 - d / 9.0))
        t_set = min(t_curve + preheat, self.source.t_supply_max_c - 1.0)
        return [Action(minute, self.name,
                       f"提前蓄热 +{preheat:.1f}℃(前瞻 8h)",
                       set_source_t_c=round(t_set, 1))]
