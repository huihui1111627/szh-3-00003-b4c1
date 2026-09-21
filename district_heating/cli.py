"""命令行演示: 策略对照 + 根因分析 + 迟报补算。

用法:
    python3 -m district_heating.cli compare    # 多策略对照
    python3 -m district_heating.cli diagnose   # 操作扰动下的根因分析
    python3 -m district_heating.cli backfill   # 迟报数据补算
"""
from __future__ import annotations

import sys

from .actions import branch_flow, curtail, source_output
from .backfill import Correction, apply_corrections
from .diagnosis import diagnose
from .simulation import Simulation, evaluate
from .strategy import STRATEGIES, compare
from .weather import ColdSnap


def _print_kpis(results) -> None:
    keys = ["critical_cold_hours", "critical_min_indoor",
            "pressure_fluct_max_kpa", "return_temp_abnormal_hours",
            "source_peak_kw"]
    header = f"{'strategy':<12}" + "".join(f"{k:>26}" for k in keys)
    print(header)
    print("-" * len(header))
    for r in results:
        row = f"{r.name:<12}"
        for k in keys:
            row += f"{r.kpis.get(k, 0.0):>26.2f}"
        print(row)


def cmd_compare() -> None:
    print("== 同一寒潮过程下的策略对照 (72h, 5min 步长) ==")
    results = compare(STRATEGIES)
    _print_kpis(results)
    for r in results:
        cold = [f for f in r.findings if f.kind == "cold_zone"]
        print(f"\n[{r.name}] 持续低温事件 {len(cold)} 起")
        for f in cold[:3]:
            print(f"  step {f.step}: {f.summary}")
            print(f"    根因: {f.root_cause}")


def cmd_diagnose() -> None:
    print("== 值守操作扰动下的根因分析 ==")
    sim = Simulation(weather=ColdSnap(seed=7))
    # 寒潮中期, 值守快速关小商业支路并提升热源出力
    sim.schedule(branch_flow(300, "mall", 0.3, "保居民"))
    sim.schedule(branch_flow(302, "workshop", 0.3, "保居民"))
    sim.schedule(source_output(305, 7500.0, "满出力"))
    sim.schedule(curtail(306, "mall", 0.5, "临时降供"))
    history = sim.run()
    design = {n: b.design_flow_kg_s for n, b in sim.network.branches.items()}
    findings = diagnose(history, sim.zones, sim.actions, design)
    for f in findings[:12]:
        zone = f"[{f.zone}] " if f.zone else ""
        print(f"step {f.step:>4} {f.kind:<15} {zone}{f.summary}")
        print(f"           根因: {f.root_cause}")
    if not findings:
        print("无异常事件")


def cmd_backfill() -> None:
    print("== 迟报数据补算与重新评估 ==")
    sim = Simulation(weather=ColdSnap(seed=11))
    sim.run(controller=STRATEGIES["predictive"])
    # 模拟: step 400~420 的实测气温比预报低 3°C, 迟报到货
    corrections = [Correction(step=s, outdoor_temp=
                              sim.weather.at(s).outdoor_temp - 3.0)
                   for s in range(400, 421)]
    result = apply_corrections(sim, corrections,
                               controller=STRATEGIES["predictive"])
    print(f"补算区间 step {result.corrected_range[0]}~{result.corrected_range[1]}, "
          f"重放 {result.replayed_steps} 步")
    for k in result.new_kpis:
        old = result.old_kpis.get(k, 0.0)
        new = result.new_kpis[k]
        flag = "  <-- 变化" if abs(new - old) > 1e-6 else ""
        print(f"  {k:<30} {old:>10.2f} -> {new:>10.2f}{flag}")


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "compare"
    {"compare": cmd_compare, "diagnose": cmd_diagnose,
     "backfill": cmd_backfill}[cmd]()
