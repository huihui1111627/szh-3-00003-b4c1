"""命令行演示:多策略同气象对照、根因链、迟报补算重评估。"""
from __future__ import annotations

import argparse
import json
from dataclasses import asdict
from typing import Dict, List

from .compare import compare, run_strategy
from .model import RunResult
from .scenario import build_source, build_zones
from .strategies import ALL
from .weather import LateReport, WeatherProcess, WeatherProvider, make_cold_wave

METRIC_LABELS = [
    ("critical_cold_zone_hours", "关键区低温·区时"),
    ("noncritical_cold_zone_hours", "非关键区低温·区时"),
    ("critical_comfort_deficit_Kh", "关键区欠温累积 K·h"),
    ("pressure_surge_count", "压力波动次数"),
    ("max_surge_kPa", "最大压力波动 kPa"),
    ("critical_alarm_count", "严重告警数"),
    ("source_capacity_hours", "热源顶上限时长 h"),
    ("max_supply_c", "最高供水 ℃"),
    ("min_return_c", "最低回水 ℃"),
]


def sparkline(values, width=48, lo=None, hi=None):
    blocks = " ▁▂▃▄▅▆▇█"
    if not values:
        return ""
    lo = min(values) if lo is None else lo
    hi = max(values) if hi is None else hi
    if hi - lo < 1e-9:
        hi = lo + 1
    step = max(1, len(values) // width)
    out = []
    for i in range(0, len(values), step):
        v = sum(values[i:i + step]) / len(values[i:i + step])
        idx = int((v - lo) / (hi - lo) * (len(blocks) - 1))
        out.append(blocks[min(max(idx, 0), len(blocks) - 1)])
    return "".join(out)


def print_compare(results: List[RunResult]):
    print("=" * 104)
    print("同一寒潮气象过程下的多策略对照运行")
    print("=" * 104)
    header = f"{'指标':<22}" + "".join(f"{r.policy_name:>21}" for r in results)
    print(header)
    print("-" * 104)
    for key, label in METRIC_LABELS:
        row = f"{label:<22}"
        for r in results:
            v = r.metrics[key]
            row += f"{v:>21.1f}"
        print(row)
    print()
    for r in results:
        print(f"【{r.policy_name}】{strategy_desc(r.policy_name)}")


def strategy_desc(name: str) -> str:
    from .strategies import WeatherCompensation, PreHeatBeforeColdWave, \
        AggressiveFastRamp, PeakCurtail
    return {
        WeatherCompensation.name: WeatherCompensation.description,
        PreHeatBeforeColdWave.name: PreHeatBeforeColdWave.description,
        AggressiveFastRamp.name: AggressiveFastRamp.description,
        PeakCurtail.name: PeakCurtail.description,
    }.get(name, "")


def print_root_causes(results: List[RunResult], limit_per_type: int = 1):
    print("=" * 104)
    print("告警 → 根因链(不只显示告警,而是沿动作记录与物理轨迹追溯)")
    print("=" * 104)
    # 每种告警类型至少展示一条最典型(严重优先、最早出现)的因果链
    order = ["pressure_surge", "source_capacity", "return_temp_high",
             "return_temp_low", "zone_indoor_low", "supply_temp_high"]
    picked = []
    used = set()
    for code in order:
        cands = [rc for r in results for rc in r.root_causes
                 if rc.alarm.code == code]
        cands.sort(key=lambda rc: (rc.alarm.severity != "critical",
                                   rc.alarm.minute))
        for rc in cands[:limit_per_type]:
            sig = (rc.alarm.code, rc.alarm.target, rc.alarm.minute // 60,
                   rc.alarm.severity)
            if sig not in used:
                picked.append((next(r.policy_name for r in results
                                    for x in r.root_causes if x is rc), rc))
                used.add(sig)
    for policy_name, rc in picked:
        al = rc.alarm
        t = f"{al.minute//60:02d}h{al.minute%60:02d}"
        print(f"\n▶ [{policy_name}] {t} 〈{sev_cn(al.severity)}〉{al.message}")
        for i, step in enumerate(rc.chain):
            print(f"  {'└─' if i == len(rc.chain) - 1 else '├─'} {step}")


def sev_cn(s: str) -> str:
    return {"critical": "严重", "warn": "预警", "info": "提示"}.get(s, s)


def print_weather_chart(wp: WeatherProcess):
    ts = [p.tout_c for p in wp.points]
    every = max(1, len(ts) // 48)
    ts = ts[::every]
    print("\n寒潮室外温度过程  " + sparkline(ts, lo=min(ts), hi=max(ts)))
    print(f"  {wp.points[0].tout_c:6.1f}℃" + " " * 44 + f"{wp.points[-1].tout_c:.1f}℃"
          + f"   最低 {min(p.tout_c for p in wp.points):.1f}℃")


def print_late_rerun(initial: RunResult, rerun: RunResult, received_min: int):
    print("=" * 104)
    print(f"迟报数据补算:实测更冷空气在 {received_min//60}h 才收报(寒潮低谷期间),到齐后用同一策略重跑")
    print("=" * 104)
    print(f"{'指标':<26}{'预报版运行':>18}{'补算重评估':>18}{'差异':>14}")
    print("-" * 80)
    for key, label in METRIC_LABELS[:7]:
        a, b = initial.metrics[key], rerun.metrics[key]
        print(f"{label:<26}{a:>18.1f}{b:>18.1f}{b-a:>+14.1f}")

    def alarm_sig(r):
        return sorted({(a.code, a.target, a.minute // 60) for a in r.trace.alarms})
    only_new = sorted(set(alarm_sig(rerun)) - set(alarm_sig(initial)))
    print("\n补算后新暴露的问题(原预报版运行未出现):")
    code_cn = {"pressure_surge": "压力波动", "return_temp_low": "回水偏低",
               "return_temp_high": "回水偏高", "zone_indoor_low": "区域低温",
               "supply_temp_high": "供水超限", "source_capacity": "热源顶上限"}
    if only_new:
        for code, target, h in only_new:
            print(f"  - {h}h {target}:{code_cn.get(code, code)}")
    else:
        print("  - 无新增告警类型")
    print("\n补算后新增根因链:")
    n = 0
    for rc in rerun.root_causes:
        sig = (rc.alarm.code, rc.alarm.target, rc.alarm.minute // 60)
        if sig in set(alarm_sig(initial)):
            continue
        al = rc.alarm
        print(f"\n▶ {al.minute//60:02d}h 〈{sev_cn(al.severity)}〉{al.message}")
        for step in rc.chain:
            print(f"  ├─ {step}")
        n += 1
        if n >= 4:
            break


def result_to_dict(r: RunResult):
    return {
        "policy": r.policy_name,
        "note": r.rerun_note,
        "metrics": r.metrics,
        "alarms": [{"minute": a.minute, "code": a.code, "target": a.target,
                    "severity": a.severity, "message": a.message}
                   for a in r.trace.alarms],
        "root_causes": [{"alarm": rc.alarm.message, "chain": rc.chain,
                         "responsible": rc.responsible,
                         "confidence": rc.confidence} for rc in r.root_causes],
    }


def export_trace_csv(result: RunResult, path: str):
    import csv
    t = result.trace
    fields = ["minute", "tout_c", "tset_c", "tsupply_c", "treturn_mix_c",
              "source_q_MW", "source_demand_MW", "total_flow_m3h", "dp_station_kPa"]
    zone_fields = []
    for name in t.zones:
        zone_fields += [f"{name}_indoor_c", f"{name}_tsupply_c",
                        f"{name}_treturn_c", f"{name}_flow_m3h",
                        f"{name}_delivered_MW", f"{name}_demand_MW",
                        f"{name}_valve", f"{name}_curtail"]
    with open(path, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(fields + zone_fields)
        for i, m in enumerate(t.minutes):
            row = [m, t.tout_c[i], t.tset_c[i], t.tsupply_c[i],
                   t.treturn_mix_c[i], t.source_q_w[i] / 1e6,
                   t.source_demand_w[i] / 1e6, t.total_flow_m3h[i],
                   t.dp_station_pa[i] / 1000.0]
            for name, zt in t.zones.items():
                row += [zt.indoor_c[i], zt.tsupply_c[i], zt.treturn_c[i],
                        zt.flow_m3h[i], zt.delivered_w[i] / 1e6,
                        zt.demand_w[i] / 1e6, zt.valve[i], zt.curtail[i]]
            w.writerow(row)


def main(argv=None):
    ap = argparse.ArgumentParser(description="集中供热寒潮协同调度仿真系统")
    ap.add_argument("--json", action="store_true", help="以 JSON 输出结果")
    ap.add_argument("--export-csv", default="",
                    help="将首个策略的逐分钟轨迹导出为 CSV 文件")
    ap.add_argument("--policy", default="preheat",
                    choices=["baseline", "preheat", "aggressive", "curtail"],
                    help="迟报补算演示使用的策略")
    args = ap.parse_args(argv)

    zones = build_zones()
    source = build_source()
    weather = make_cold_wave()

    policies = [P() for P in ALL]
    results = compare(policies, weather, source=source, zones=zones)

    # 迟报:14h-26h 实际比预报冷 4℃,报文 24h(寒潮已过半)才收到
    provider = WeatherProvider(
        weather,
        [LateReport(12 * 60, 30 * 60, "tout_c", -25.5, 22 * 60,
                    "12-30 时实测比预报低约 3.5℃ 的强冷空气")])

    pol_map = {p.name: p for p in policies}
    chosen = {"baseline": "气候补偿基线", "preheat": "提前蓄热保供",
              "aggressive": "激进快调(反面)", "curtail": "峰值限供保关键"}[args.policy]
    pol = pol_map[chosen]
    initial = run_strategy(pol, weather, source=source, zones=zones)
    rerun = run_strategy(pol, provider.fully_observed(), source=source, zones=zones,
                         note="迟报实测补算重评估")

    if args.export_csv:
        export_trace_csv(results[0], args.export_csv)

    if args.json:
        print(json.dumps({
            "compare": [result_to_dict(r) for r in results],
            "late_data_rerun": {
                "forecast_run": result_to_dict(initial),
                "backfilled_run": result_to_dict(rerun)},
        }, ensure_ascii=False, indent=2))
        return

    print_weather_chart(weather)
    print_compare(results)
    print_root_causes(results)
    print_late_rerun(initial, rerun, 22 * 60)
    print("\n完成:同一气象过程可复算;迟报数据补算后告警与根因链被重新评估。")


if __name__ == "__main__":
    main()
