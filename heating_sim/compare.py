"""同一气象过程下多策略对照运行、指标评估与迟报数据补算重评估。"""
from __future__ import annotations

from typing import Dict, List, Optional

from .diagnose import diagnose
from .engine import Simulator
from .model import RunResult, Trace, WeatherProcess
from .weather import WeatherProvider


def compute_metrics(trace: Trace, zones) -> Dict[str, float]:
    n = len(trace.minutes)
    # 关键/非关键分区分别统计低温分钟与舒适度缺口
    cold_min_critical = 0.0
    cold_min_non = 0.0
    deficit_critical = 0.0
    deficit_non = 0.0
    for name, z in zones.items():
        zt = trace.zones[name]
        for i, t in enumerate(zt.indoor_c):
            gap = max(0.0, 18.0 - t)
            if z.critical:
                cold_min_critical += 1 if t < 16.5 else 0
                deficit_critical += gap
            else:
                cold_min_non += 1 if t < 16.5 else 0
                deficit_non += gap

    dt = (trace.minutes[1] - trace.minutes[0]) if n > 1 else 10
    surge_alarms = [a for a in trace.alarms if a.code == "pressure_surge"]
    crit = [a for a in trace.alarms if a.severity == "critical"]
    cap_min = sum(1 for q, d in zip(trace.source_q_w, trace.source_demand_w)
                  if d > 0 and q / max(d, 1) < 0.985)
    return {
        "hours": n * dt / 60.0,
        "critical_cold_zone_hours": cold_min_critical * dt / 60.0,
        "noncritical_cold_zone_hours": cold_min_non * dt / 60.0,
        "critical_comfort_deficit_Kh": deficit_critical * dt / 60.0,
        "noncritical_comfort_deficit_Kh": deficit_non * dt / 60.0,
        "pressure_surge_count": float(len(surge_alarms)),
        "max_surge_kPa": max((abs(a.value) for a in surge_alarms), default=0.0) / 1000.0,
        "critical_alarm_count": float(len(crit)),
        "total_alarm_count": float(len(set((a.code, a.target, a.minute // 30)
                                           for a in trace.alarms))),
        "source_capacity_hours": cap_min * dt / 60.0,
        "max_supply_c": max(trace.tsupply_c),
        "min_return_c": min(trace.treturn_mix_c),
        "avg_source_load_ratio": sum(
            q / max(d, 1) for q, d in zip(trace.source_q_w, trace.source_demand_w)
            if d > 0) / max(sum(1 for d in trace.source_demand_w if d > 0), 1),
    }


def run_strategy(policy, weather: WeatherProcess, dt_min: int = 10,
                 source=None, zones=None, note: str = "") -> RunResult:
    sim = Simulator(dt_min, source=source, zones=zones)
    trace = sim.run(policy, weather)
    causes = diagnose(trace)
    metrics = compute_metrics(trace, sim.zones)
    return RunResult(policy.name, weather, trace, causes, metrics, note)


def compare(policies, weather: WeatherProcess, dt_min: int = 10,
            source=None, zones=None) -> List[RunResult]:
    """用完全相同的气象过程和初始条件运行所有策略。"""
    return [run_strategy(p, weather, dt_min, source, zones) for p in
            (P() if isinstance(P, type) else P for P in policies)]

