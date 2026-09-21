"""管网水力/热力模型。

- 每条管段是带输运延迟的活塞流: 出口温度 = 延迟前入口温度经沿程散热。
  用环形缓冲实现延迟线, 直观体现"热量经过管网后延迟到达各区域"。
- 泵站: 母管压力 = 泵扬程 - 沿程阻力(与总流量平方成正比)。
  快速调节支路流量会引起压力波动, 仿真记录压力变化率供根因分析。
- 回水: 各支路回水按流量混合后延迟返回热源。
"""
from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from typing import Deque, Dict, List, Optional

CP_KJ_PER_KG_K = 4.186          # 水的比热
RHO_KG_PER_M3 = 1000.0


@dataclass
class Pipe:
    """一条供水或回水管段(单向)。"""
    name: str
    length_m: float
    diameter_m: float
    u_w_per_m2_k: float = 0.35  # 保温层综合传热
    _line: Deque = field(default=None, repr=False)  # type: ignore

    def volume_m3(self) -> float:
        import math
        return math.pi * (self.diameter_m / 2) ** 2 * self.length_m

    def delay_steps(self, flow_kg_s: float, dt_hours: float) -> int:
        if flow_kg_s <= 1e-6:
            flow_kg_s = 1e-6
        velocity = flow_kg_s / RHO_KG_PER_M3 / (
            3.14159265 * (self.diameter_m / 2) ** 2)
        delay_s = self.length_m / max(velocity, 1e-6)
        return max(1, int(round(delay_s / (dt_hours * 3600.0))))

    def transport(self, t_in: float, flow_kg_s: float, ambient: float,
                  dt_hours: float) -> float:
        """推进一步: 返回本步出口温度, 同时把 t_in 推入延迟线。"""
        n = self.delay_steps(flow_kg_s, dt_hours)
        if self._line is None:
            self._line = deque([t_in] * n, maxlen=None)
        while len(self._line) < n:      # 流量变小 -> 延迟变长
            self._line.appendleft(self._line[0])
        while len(self._line) > n + 1:  # 流量变大 -> 延迟变短
            self._line.popleft()
        self._line.append(t_in)
        t_out = self._line.popleft()
        # 沿程散热(稳态近似)
        area = 3.14159265 * self.diameter_m * self.length_m
        loss_w = self.u_w_per_m2_k * area * (t_out - ambient)
        m_dot = max(flow_kg_s, 1e-6)
        return t_out - loss_w / (m_dot * CP_KJ_PER_KG_K * 1000.0)


@dataclass
class Branch:
    """通往一个分区的支路: 供水管 + 回水管 + 流量阀。"""
    zone_name: str
    supply_pipe: Pipe
    return_pipe: Pipe
    design_flow_kg_s: float
    flow_setpoint: float = 1.0    # 值守可调的流量系数 0..1.2
    flow_kg_s: float = 0.0
    supply_temp_out: float = 70.0  # 支路末端(建筑入口)供水温度
    return_temp_in: float = 45.0   # 建筑出口回水温度


class Network:
    """热源 -> 干管 -> 各支路 -> 回水干管 -> 热源。"""

    def __init__(self, zones: Dict[str, object], dt_hours: float) -> None:
        self.dt = dt_hours
        self.branches: Dict[str, Branch] = {}
        # 干管: 热源到各支路距离不同 -> 各区域延迟不同
        trunk_lengths = {
            "hospital": 1200.0, "school": 2000.0, "residential": 3200.0,
            "mall": 2600.0, "workshop": 4000.0,
        }
        for name, zone in zones.items():
            length = trunk_lengths.get(name, 2000.0)
            design_flow = zone.capacity_kw / (CP_KJ_PER_KG_K * 25.0)  # ΔT=25K
            self.branches[name] = Branch(
                zone_name=name,
                supply_pipe=Pipe(f"sup_{name}", length, 0.30),
                return_pipe=Pipe(f"ret_{name}", length, 0.30),
                design_flow_kg_s=design_flow,
                flow_kg_s=design_flow,
            )
        # 泵站参数
        self.pump_head_kpa = 600.0
        self.resistance_kpa_per_kg2 = 0.0  # 初始化后按设计点标定
        self._calibrate()
        self.pressure_kpa = self.pump_head_kpa
        self.pressure_history: List[float] = []
        self.return_temp_to_source = 45.0

    def _calibrate(self) -> None:
        total = sum(b.design_flow_kg_s for b in self.branches.values())
        # 设计工况下干管压降取扬程的 40%
        self.resistance_kpa_per_kg2 = 0.4 * self.pump_head_kpa / (total ** 2)

    @property
    def total_flow(self) -> float:
        return sum(b.flow_kg_s for b in self.branches.values())

    def set_branch_flow(self, zone_name: str, factor: float) -> None:
        self.branches[zone_name].flow_setpoint = max(0.0, min(1.2, factor))

    def step(self, source_supply_temp: float, ambient: float,
             zone_supply_factors: Dict[str, float]) -> Dict[str, float]:
        """推进一步, 返回 {zone: 实际获得热量 kW}。"""
        delivered: Dict[str, float] = {}
        ret_mix_num, ret_mix_den = 0.0, 0.0
        for name, br in self.branches.items():
            br.flow_kg_s = br.design_flow_kg_s * br.flow_setpoint
            t_sup = br.supply_pipe.transport(
                source_supply_temp, br.flow_kg_s, ambient, self.dt)
            br.supply_temp_out = t_sup
            # 建筑换热: 回水温度由供热量决定; 降供直接削减可用流量
            eff_flow = br.flow_kg_s * zone_supply_factors.get(name, 1.0)
            # 目标散热按设计 ΔT, 实际回水由能量守恒在 simulation 层闭合;
            # 这里先按建筑入口温度与设计回水温差估算
            delta_t = max(5.0, t_sup - 42.0)
            q_kw = eff_flow * CP_KJ_PER_KG_K * delta_t
            t_ret = t_sup - q_kw / max(eff_flow * CP_KJ_PER_KG_K, 1e-6)
            br.return_temp_in = t_ret
            t_ret_src = br.return_pipe.transport(
                t_ret, max(br.flow_kg_s, 1e-6), ambient, self.dt)
            ret_mix_num += t_ret_src * br.flow_kg_s
            ret_mix_den += br.flow_kg_s
            delivered[name] = q_kw
        if ret_mix_den > 1e-6:
            self.return_temp_to_source = ret_mix_num / ret_mix_den
        # 压力: 扬程减去随总流量平方增长的阻力
        self.pressure_kpa = (self.pump_head_kpa
                             - self.resistance_kpa_per_kg2 * self.total_flow ** 2)
        self.pressure_history.append(self.pressure_kpa)
        return delivered
