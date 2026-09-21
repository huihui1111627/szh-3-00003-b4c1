"""热源模型: 出力上限、爬坡速率约束、供水温度调节。"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass
class HeatSource:
    capacity_kw: float = 9000.0
    max_ramp_kw_per_h: float = 1500.0   # 爬坡约束
    supply_temp_set: float = 85.0
    supply_temp_max: float = 95.0
    current_output_kw: float = 4000.0
    supply_temp: float = 85.0

    def set_output_target(self, target_kw: float) -> None:
        self._target = max(0.0, min(target_kw, self.capacity_kw))

    def set_supply_temp(self, temp: float) -> None:
        self.supply_temp_set = max(50.0, min(temp, self.supply_temp_max))

    def step(self, dt_hours: float, return_temp: float,
             network_flow_kg_s: float) -> None:
        target = getattr(self, "_target", self.current_output_kw)
        max_delta = self.max_ramp_kw_per_h * dt_hours
        delta = max(-max_delta, min(max_delta, target - self.current_output_kw))
        self.current_output_kw += delta
        # 供水温度向设定值缓慢逼近(锅炉热惯性)
        self.supply_temp += (self.supply_temp_set - self.supply_temp) * min(
            1.0, dt_hours * 2.0)
        # 若管网所需热量超过热源能力, 实际供水温度被拉低
        needed = network_flow_kg_s * 4.186 * (self.supply_temp - return_temp)
        if needed > self.current_output_kw > 0:
            deficit = (needed - self.current_output_kw) / max(
                network_flow_kg_s * 4.186, 1e-6)
            self.supply_temp -= max(0.0, deficit)
