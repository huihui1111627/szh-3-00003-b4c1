"""策略四:峰值压负荷——热源能力不足时临时限供非关键区域,保关键区域。"""
from __future__ import annotations

from ..model import Action, Trace, WeatherProcess
from ..policies import Policy, _target_supply_from_curve


class PeakCurtail(Policy):
    name = "峰值限供保关键"
    description = "热源逼近上限时,渐进削减商业/园区流量,优先保障医院与住宅"

    def __init__(self, curtail_level: float = 0.35):
        self.level = curtail_level
        self.curtail = {"商业片区": 0.0, "开发园区": 0.0}

    def decide(self, minute, weather: WeatherProcess, trace: Trace):
        w = weather.weather_at(minute + 20).effective_tout()
        t_set = _target_supply_from_curve(w)
        actions = [Action(minute, self.name,
                          f"按曲线供水(前瞻 {w:.0f}℃)",
                          set_source_t_c=round(t_set, 1))]

        if trace.source_q_w:
            load_ratio = trace.source_q_w[-1] / self.source.q_max_w
        else:
            load_ratio = 0.0
        tight = load_ratio > 0.88
        for name in ("商业片区", "开发园区"):
            target = self.level if tight else 0.0
            cur = self.curtail[name]
            # 渐进调节,每 10 分钟最多 0.12,避免压力冲击
            step = max(-0.12, min(0.12, target - cur))
            newv = round(cur + step, 2)
            if abs(newv - cur) > 1e-6:
                actions.append(Action(
                    minute, self.name,
                    f"热源负荷率 {load_ratio:.0%},{'削减' if newv > cur else '恢复'}"
                    f"{name}非关键负荷至 {newv:.0%}",
                    curtail={name: newv}))
                self.curtail[name] = newv
        return actions
