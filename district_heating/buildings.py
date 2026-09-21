"""建筑热惯性与分区需求。

每个供热分区聚合为一阶 RC 模型:
    C * dT_in/dt = Q_delivered - UA_eff * (T_in - T_out)
UA_eff 随风速增大(冷风渗透)。分区有优先级, 非关键区可被临时降供。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict


@dataclass
class Zone:
    name: str
    critical: bool                # 关键区域不可降供
    capacity_kw: float            # 设计热负荷 kW
    thermal_mass_kj_per_k: float  # 等效热容
    ua_kw_per_k: float            # 基准传热系数
    indoor_target: float = 20.0
    indoor_min: float = 16.0      # 舒适下限, 低于此记为低温事件
    indoor_temp: float = 20.0
    supply_factor: float = 1.0    # 降供系数 0..1, 由值守操作调整

    def heat_demand_kw(self, outdoor_temp: float, wind: float) -> float:
        """维持目标室温所需热量。"""
        ua = self.ua_kw_per_k * (1.0 + 0.03 * max(0.0, wind - 4.0))
        return ua * (self.indoor_target - outdoor_temp)

    def step(self, delivered_kw: float, outdoor_temp: float,
             wind: float, dt_hours: float) -> None:
        ua = self.ua_kw_per_k * (1.0 + 0.03 * max(0.0, wind - 4.0))
        loss = ua * (self.indoor_temp - outdoor_temp)
        d_t = (delivered_kw - loss) * dt_hours * 3600.0 / self.thermal_mass_kj_per_k
        self.indoor_temp += d_t


def default_zones() -> Dict[str, Zone]:
    """示例城区: 医院/学校为关键区, 商业与工业为非关键区。"""
    specs = [
        # name, critical, capacity, mass, ua
        ("hospital",  True,  1800.0, 9.0e6, 60.0),
        ("school",    True,  1200.0, 6.0e6, 45.0),
        ("residential", True, 3000.0, 1.8e7, 95.0),
        ("mall",      False, 1500.0, 5.0e6, 55.0),
        ("workshop",  False, 1000.0, 3.0e6, 40.0),
    ]
    return {
        name: Zone(name, crit, cap, mass, ua)
        for name, crit, cap, mass, ua in specs
    }
