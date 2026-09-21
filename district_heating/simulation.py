"""主仿真循环: 耦合热源-管网-建筑, 记录遥测, 支持检查点回滚(供迟报补算)。"""
from __future__ import annotations

import copy
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional

from .actions import Action
from .buildings import Zone, default_zones
from .network import Network
from .source import HeatSource
from .weather import ColdSnap


@dataclass
class Telemetry:
    """单步遥测记录。"""
    step: int
    t_hours: float
    outdoor_temp: float
    source_output_kw: float
    source_supply_temp: float
    return_temp: float
    pressure_kpa: float
    total_flow: float
    zone_indoor: Dict[str, float]
    zone_delivered_kw: Dict[str, float]
    zone_supply_temp: Dict[str, float]
    zone_flow: Dict[str, float]
    zone_demand_kw: Dict[str, float]
    zone_supply_factor: Dict[str, float]


class Simulation:
    def __init__(self, weather: Optional[ColdSnap] = None,
                 zones: Optional[Dict[str, Zone]] = None,
                 source: Optional[HeatSource] = None) -> None:
        self.weather = weather or ColdSnap()
        self.dt = self.weather.step_hours
        self.zones = zones or default_zones()
        self.network = Network(self.zones, self.dt)
        self.source = source or HeatSource()
        self.actions: Dict[int, List[Action]] = {}
        self.history: List[Telemetry] = []
        self._checkpoints: Dict[int, tuple] = {}

    # ---- 操作调度 ----
    def schedule(self, action: Action) -> None:
        self.actions.setdefault(action.step, []).append(action)

    def _apply(self, action: Action) -> Optional[str]:
        if action.kind == "source_output":
            self.source.set_output_target(action.value)
        elif action.kind == "branch_flow":
            self.network.set_branch_flow(action.target, action.value)
        elif action.kind == "curtail":
            zone = self.zones.get(action.target)
            if zone is None:
                return f"未知分区 {action.target}"
            if zone.critical:
                return f"拒绝: {action.target} 为关键区域, 不允许降供"
            zone.supply_factor = max(0.0, min(1.0, action.value))
        return None

    # ---- 检查点(迟报补算用) ----
    def save_checkpoint(self, step: int) -> None:
        self._checkpoints[step] = (
            copy.deepcopy(self.zones), copy.deepcopy(self.network),
            copy.deepcopy(self.source), len(self.history))

    def restore_checkpoint(self, step: int) -> None:
        zones, network, source, hist_len = self._checkpoints[step]
        self.zones, self.network, self.source = zones, network, source
        del self.history[hist_len:]

    # ---- 主循环 ----
    def run(self, n_steps: Optional[int] = None,
            controller: Optional[Callable[["Simulation", int], None]] = None,
            checkpoint_every: int = 72) -> List[Telemetry]:
        n = n_steps or self.weather.n_steps
        start = self.history[-1].step + 1 if self.history else 0
        for step in range(start, n):
            if step % checkpoint_every == 0:
                self.save_checkpoint(step)
            for act in self.actions.get(step, []):
                self._apply(act)
            if controller is not None:
                controller(self, step)
            w = self.weather.at(step)
            factors = {name: z.supply_factor for name, z in self.zones.items()}
            delivered = self.network.step(self.source.supply_temp,
                                          w.outdoor_temp, factors)
            self.source.step(self.dt, self.network.return_temp_to_source,
                             self.network.total_flow)
            for name, z in self.zones.items():
                z.step(delivered[name], w.outdoor_temp, w.wind_speed, self.dt)
            self.history.append(Telemetry(
                step=step, t_hours=w.t_hours,
                outdoor_temp=w.outdoor_temp,
                source_output_kw=self.source.current_output_kw,
                source_supply_temp=self.source.supply_temp,
                return_temp=self.network.return_temp_to_source,
                pressure_kpa=self.network.pressure_kpa,
                total_flow=self.network.total_flow,
                zone_indoor={n: z.indoor_temp for n, z in self.zones.items()},
                zone_delivered_kw=dict(delivered),
                zone_supply_temp={n: b.supply_temp_out
                                  for n, b in self.network.branches.items()},
                zone_flow={n: b.flow_kg_s
                           for n, b in self.network.branches.items()},
                zone_demand_kw={n: z.heat_demand_kw(w.outdoor_temp, w.wind_speed)
                                for n, z in self.zones.items()},
                zone_supply_factor=dict(factors),
            ))
        return self.history


# ---- KPI 评估 ----
def evaluate(history: List[Telemetry], zones: Dict[str, Zone]) -> Dict[str, float]:
    """对一段历史计算运行指标(对照运行与补算重估共用)。"""
    if not history:
        return {}
    kpis: Dict[str, float] = {}
    crit = [n for n, z in zones.items() if z.critical]
    # 关键区低温累计时长(小时)与最低室温
    cold_hours = 0.0
    min_indoor = 99.0
    dt_h = (history[1].t_hours - history[0].t_hours) if len(history) > 1 else 1.0
    for rec in history:
        for n in crit:
            t = rec.zone_indoor[n]
            min_indoor = min(min_indoor, t)
            if t < zones[n].indoor_min:
                cold_hours += dt_h
    kpis["critical_cold_hours"] = cold_hours
    kpis["critical_min_indoor"] = min_indoor
    # 压力波动: 相邻步压力变化绝对值之和与峰值
    deltas = [abs(history[i + 1].pressure_kpa - history[i].pressure_kpa)
              for i in range(len(history) - 1)]
    kpis["pressure_fluct_total_kpa"] = sum(deltas)
    kpis["pressure_fluct_max_kpa"] = max(deltas) if deltas else 0.0
    # 回水温度异常时长(>55°C 说明流量过大/换热不足, <30°C 说明过流或欠热)
    abnormal = sum(dt_h for r in history
                   if r.return_temp > 55.0 or r.return_temp < 30.0)
    kpis["return_temp_abnormal_hours"] = abnormal
    # 热源出力利用率峰值
    kpis["source_peak_kw"] = max(r.source_output_kw for r in history)
    return kpis
