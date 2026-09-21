"""策略一:气候补偿曲线(量调节关闭,完全按预报质调节)。"""
from __future__ import annotations

from ..model import Action, Trace, WeatherProcess
from ..policies import Policy, _target_supply_from_curve


class WeatherCompensation(Policy):
    name = "气候补偿基线"
    description = "严格按预报气温的质调节曲线供水,不提前蓄热、不主动限流"

    def decide(self, minute, weather: WeatherProcess, trace: Trace):
        w = weather.weather_at(minute + self.decision_every_min)
        t_set = _target_supply_from_curve(w.effective_tout())
        return [Action(minute, self.name,
                       f"按补偿曲线供水(预报 {w.tout_c:.0f}℃)",
                       set_source_t_c=round(t_set, 1))]
