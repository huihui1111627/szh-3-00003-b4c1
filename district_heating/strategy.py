"""供热策略: 同一寒潮气象过程下多策略对照运行。

策略以 controller(sim, step) 形式注入仿真, 每步可读取状态并下发操作。
对照运行时各策略使用相同 seed 的 ColdSnap, 保证气象过程完全一致。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Dict, List

from .diagnosis import Finding, diagnose
from .simulation import Simulation, Telemetry, evaluate
from .weather import ColdSnap

Controller = Callable[[Simulation, int], None]


@dataclass
class StrategyResult:
    name: str
    history: List[Telemetry]
    kpis: Dict[str, float]
    findings: List[Finding]


def reactive_controller(sim: Simulation, step: int) -> None:
    """被动策略: 室温低了才加出力, 不考虑管网延迟。"""
    coldest = min(z.indoor_temp for z in sim.zones.values())
    if coldest < 18.0:
        sim.source.set_output_target(sim.source.capacity_kw)
        sim.source.set_supply_temp(92.0)
    elif coldest > 21.0:
        sim.source.set_output_target(3500.0)
        sim.source.set_supply_temp(80.0)


def predictive_controller(sim: Simulation, step: int) -> None:
    """协同策略: 按气象预报与管网延迟提前提升出力, 寒潮峰期削减非关键区。"""
    horizon = 36  # 按最长管线延迟(~3h)向前看
    future = sim.weather.at(min(step + horizon, sim.weather.n_steps))
    demand = sum(z.heat_demand_kw(future.outdoor_temp, future.wind_speed)
                 for z in sim.zones.values())
    sim.source.set_output_target(min(demand * 1.05, sim.source.capacity_kw))
    # 气温越低供水温度越高, 提前补偿输运温降
    sim.source.set_supply_temp(85.0 + max(0.0, -future.outdoor_temp) * 0.8)
    # 预测能力不足时, 临时降低非关键区供应
    if demand > sim.source.capacity_kw * 0.95:
        for z in sim.zones.values():
            if not z.critical:
                z.supply_factor = 0.6
    else:
        for z in sim.zones.values():
            z.supply_factor = 1.0


STRATEGIES: Dict[str, Controller] = {
    "reactive": reactive_controller,
    "predictive": predictive_controller,
}


def run_strategy(name: str, controller: Controller,
                 weather_seed: int = 2026) -> StrategyResult:
    sim = Simulation(weather=ColdSnap(seed=weather_seed))
    history = sim.run(controller=controller)
    design_flows = {n: b.design_flow_kg_s
                    for n, b in sim.network.branches.items()}
    return StrategyResult(
        name=name, history=history,
        kpis=evaluate(history, sim.zones),
        findings=diagnose(history, sim.zones, sim.actions, design_flows))


def compare(strategies: Dict[str, Controller] = None,
            weather_seed: int = 2026) -> List[StrategyResult]:
    """同一气象过程下对照运行多个策略。"""
    strategies = strategies or STRATEGIES
    return [run_strategy(name, ctrl, weather_seed)
            for name, ctrl in strategies.items()]
