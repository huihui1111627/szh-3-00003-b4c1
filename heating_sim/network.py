"""一次网水力模型与热力输运模型。

水力: 循环泵提供压差 dp,各支路并联,阻抗 R_i,支路流量
    G_i = 有效开度_i * sqrt(dp / R_i);总流量等于各支路之和。
    泵站压差按泵特性随总流量下降;导纳(阀门/限供)突变时,管网惯性
    产生压力暂态(关阀冲高、开阀短时掉压)。

热力: 热源供水以"热媒微团(parcel)"沿管道推进,微团按行进距离产生
    指数温降;到达末端的微团经换热站入口缓冲混合形成该分区供水温度。
    由此天然刻画"热量经管网延迟到达各区域"的过程。
"""
from __future__ import annotations

import math
from collections import deque
from dataclasses import dataclass, field
from typing import Dict, Tuple

from .model import CP, RHO, ZoneDef

TRANSIT_REF_MS = 1.6          # 额定管内流速 m/s
SOIL_T_C = 2.0                # 管道周围介质温度
LOSS_PER_KM = 0.010           # 每公里相对温差损失(指数系数)


@dataclass
class _Parcel:
    temp_c: float
    traveled_m: float


@dataclass
class BranchState:
    valve: float = 1.0
    curtail: float = 0.0
    parcels: deque = field(default_factory=deque)
    tsupply_c: float = 70.0
    transit_min: float = 0.0


class HydraulicNetwork:
    def __init__(self, source, zones: Dict[str, ZoneDef]):
        self.source = source
        self.zones = zones
        self.branches: Dict[str, BranchState] = {
            z.name: BranchState() for z in zones.values()}
        self.flow_m3h: Dict[str, float] = {n: 0.0 for n in zones}
        self.dp_pa: float = source.pump_dp_pa
        self.total_m3h: float = 0.0
        self._prev_admittance: float = 0.0
        self.last_surge_pa: float = 0.0
        self.last_admittance_change: float = 0.0

    def set_valve(self, name: str, opening: float) -> None:
        self.branches[name].valve = max(0.0, opening)

    def set_curtail(self, name: str, curtail_factor: float) -> None:
        self.branches[name].curtail = min(1.0, max(0.0, curtail_factor))

    def nominal_flow(self, z: ZoneDef) -> float:
        """额定流量:按设计负荷、25℃ 供回温差估算 (m3/h)。"""
        return max(z.nominal_load_w / (CP * 35.0 * RHO) * 3600.0, 1.0)

    def solve_hydraulics(self) -> Tuple[float, float, Dict[str, float]]:
        """联立泵特性与并联支路,返回 (暂态压力Pa, 实际泵站压差Pa, 支路流量)。"""
        adm = {}
        for name, z in self.zones.items():
            b = self.branches[name]
            eff_open = b.valve * (1.0 - b.curtail)
            adm[name] = eff_open / math.sqrt(z.hydraulic_r)
        total_adm = sum(adm.values())

        dp0 = self.source.pump_dp_pa
        vr = self.source.pump_rated_m3h
        k = total_adm
        # V = k*sqrt(dp);泵曲线使额定点 (Vr, dp0): dp = dp0*(2-(V/Vr)^2),
        # 在 V<=Vr 段成立,联立解得 dp = 2*dp0 / (1 + dp0*k^2/Vr^2)
        dp_steady = 2.0 * dp0 / (1.0 + dp0 * k * k / (vr * vr))
        dp_steady = min(dp_steady, dp0 * 1.4)
        flows = {n: adm[n] * math.sqrt(dp_steady) for n in self.zones}

        surge = 0.0
        da = 0.0
        if self._prev_admittance > 0:
            da = (total_adm - self._prev_admittance) / self._prev_admittance
            # 关阀(da<0)压差冲高;开阀(da>0)入口短时掉压
            surge = -4.0e5 * da
        self.last_surge_pa = surge
        self.last_admittance_change = da
        self._prev_admittance = total_adm

        self.dp_pa = dp_steady + surge
        self.total_m3h = sum(flows.values())
        self.flow_m3h = flows
        for name, z in self.zones.items():
            v = max(TRANSIT_REF_MS * flows[name] / self.nominal_flow(z), 0.05)
            self.branches[name].transit_min = z.pipe_len_m / v / 60.0
        return surge, self.dp_pa, flows

    def inject(self, t_hot_c: float) -> None:
        """热源向各支路送出一个 t_hot_c 的热媒微团。"""
        for b in self.branches.values():
            b.parcels.append(_Parcel(t_hot_c, 0.0))

    def advance_thermal(self, dt_min: int) -> None:
        """推进微团,到达末端的微团缓冲混合为分区当前供水温度。"""
        for name, z in self.zones.items():
            b = self.branches[name]
            v = max(TRANSIT_REF_MS * self.flow_m3h[name]
                    / self.nominal_flow(z), 0.05)
            step_m = v * dt_min * 60.0
            decay = math.exp(-LOSS_PER_KM * step_m / 1000.0)
            arrived: list[float] = []
            remaining: deque = deque()
            for p in b.parcels:
                p.traveled_m += step_m
                p.temp_c = SOIL_T_C + (p.temp_c - SOIL_T_C) * decay
                if p.traveled_m >= z.pipe_len_m:
                    arrived.append(p.temp_c)
                else:
                    remaining.append(p)
            b.parcels = remaining
            if arrived:
                new_t = sum(arrived) / len(arrived)
                in_pipe = max(len(b.parcels), 1)
                beta = min(0.35 + 0.65 * len(arrived) / (len(arrived) + in_pipe), 0.9)
                b.tsupply_c += beta * (new_t - b.tsupply_c)

    def prefill(self, t_c: float) -> None:
        """稳态预热:管道充满温度 t_c 的水,微团按管长均匀布置。"""
        for name, z in self.zones.items():
            b = self.branches[name]
            n = max(int(z.pipe_len_m / (TRANSIT_REF_MS * 60.0)), 2)
            b.parcels = deque(_Parcel(t_c, z.pipe_len_m * i / n)
                              for i in range(n))
            b.tsupply_c = t_c
