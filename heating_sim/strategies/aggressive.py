"""策略三:快速激进调节——发现掉温后骤提供水、大幅开远端支路,
以暴露压力波动与回水异常等问题(反面对照策略)。"""
from __future__ import annotations

from ..model import Action, Trace, WeatherProcess
from ..policies import Policy, _target_supply_from_curve


class AggressiveFastRamp(Policy):
    name = "激进快调(反面)"
    description = "发现远端掉温即骤提供水并一次性全开远端阀门,动作快且幅大"

    def init(self, source, zones):
        super().init(source, zones)
        self.valve_opened = False
        self.boosted = False

    def decide(self, minute, weather: WeatherProcess, trace: Trace):
        actions = []
        cold_zones = [n for n, zt in trace.zones.items()
                      if zt.indoor_c and zt.indoor_c[-1] < 17.0]
        # 远端园区一旦偏冷:阀门一步从 1.0 开到 1.45,同时供水骤提
        if not self.valve_opened and "开发园区" in cold_zones:
            actions.append(Action(
                minute, self.name,
                "远端园区掉温,一次性大幅开大支路阀门抢流量",
                branch_valve={"开发园区": 1.45}))
            self.valve_opened = True
        w = weather.weather_at(minute).effective_tout()
        if cold_zones and not self.boosted:
            t_set = min(_target_supply_from_curve(w) + 12.0,
                        self.source.t_supply_max_c)
            actions.append(Action(
                minute, self.name,
                "室内掉温,供水温度一步拉到上限",
                set_source_t_c=round(t_set, 1)))
            self.boosted = True
        elif not actions:
            actions.append(Action(
                minute, self.name, "维持补偿曲线",
                set_source_t_c=round(_target_supply_from_curve(w), 1)))
        return actions
