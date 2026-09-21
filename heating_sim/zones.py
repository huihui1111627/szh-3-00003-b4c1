"""换热站(水-水换热器)与建筑热惯性(一阶 RC 集总参数)模型。

- 应供热负荷按设定室温(18℃)计算,与当前室温无关,避免"越冷越不供热"的假反馈;
- 取热量一阶追赶负荷,受流量换热能力(m*cp*(tin-tr_floor))与换热器能力约束;
- 回水温度由 tr = tin - Q/(m*cp) 反推:限供/欠流取热少 -> 回水低(大温差);
  流量过剩但负荷封顶 -> 回水高(小温差、水力失衡);
- 建筑: C*dT/dt = Q - UA*(Tin-Tout),解析一阶推进。
首站混合回水与供水母管的蓄能惯性在 engine 中建模,保证能量守恒。
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Dict

from .model import CP, RHO, ZoneDef
from .network import HydraulicNetwork

T_RETURN_FLOOR_C = 37.0
Q_TAU_S = 12.0 * 60.0


@dataclass
class ZoneRuntime:
    indoor_c: float
    q_actual_w: float


class ZoneSystem:
    def __init__(self, zones: Dict[str, ZoneDef], network: HydraulicNetwork,
                 init_indoor_c: float = 18.0):
        self.zones = zones
        self.net = network
        self.rt: Dict[str, ZoneRuntime] = {
            n: ZoneRuntime(init_indoor_c, 0.0) for n in zones}

    def demand_w(self, z: ZoneDef, tout_eff_c: float,
                 indoor_set_c: float = 18.0) -> float:
        return max(z.ua * (indoor_set_c - tout_eff_c), 0.0)

    def settle(self, t_supply_c: float, tout_eff_c: float) -> None:
        for name, z in self.zones.items():
            self.rt[name].indoor_c = 18.0
            self.rt[name].q_actual_w = self.demand_w(z, tout_eff_c)

    def step(self, dt_min: int, tout_eff_c: float):
        dt = dt_min * 60.0
        out = {}
        for name, z in self.zones.items():
            b = self.net.branches[name]
            mass = self.net.flow_m3h[name] / 3600.0 * RHO
            tin = b.tsupply_c
            rt = self.rt[name]
            indoor = rt.indoor_c

            # 应供负荷按设定室温 18℃ 计算(建筑热损失需求),
            # 与当前室温无关——否则室温下跌会被模型误判为"不需要热",形成错误正反馈
            q_load = self.demand_w(z, tout_eff_c)
            q_flow_cap = max(0.0, mass * CP * (tin - T_RETURN_FLOOR_C))
            q_exch_cap = z.nominal_load_w * 1.12
            q_target = min(q_load, q_exch_cap, q_flow_cap)

            if mass > 1e-6:
                f = 1.0 - math.exp(-dt / Q_TAU_S)
                q_delivered = rt.q_actual_w + f * (q_target - rt.q_actual_w)
                q_delivered = min(q_delivered, q_flow_cap)
                tr = tin - q_delivered / (mass * CP)
            else:
                q_delivered = 0.0
                tr = max(T_RETURN_FLOOR_C, tin - 30.0)
            rt.q_actual_w = q_delivered

            a = z.ua / z.capacitance
            steady = tout_eff_c + q_delivered / z.ua
            new_indoor = steady + (indoor - steady) * (1.0 - math.exp(-a * dt))
            new_indoor = min(new_indoor, 24.5)
            rt.indoor_c = new_indoor

            demand = self.demand_w(z, tout_eff_c)
            out[name] = (q_delivered, tr, demand, q_delivered)
        return out
