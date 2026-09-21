"""根因分析: 不只报告警, 而是沿因果链定位到具体环节。

三类事件的诊断规则:
1. 压力波动   -> 回溯近期值守操作, 关联流量突变
2. 回水异常   -> 区分"流量过大/换热不足"与"供水温度不足"
3. 区域持续低温 -> 沿 需求缺口 -> 支路流量/降供 -> 管网延迟与散热
                  -> 热源能力饱和 逐级定位约束环节
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional

from .actions import Action
from .buildings import Zone
from .simulation import Telemetry


@dataclass
class Finding:
    kind: str                 # pressure_surge / return_temp / cold_zone
    zone: Optional[str]
    step: int
    summary: str              # 告警现象
    root_cause: str           # 根因说明
    evidence: Dict[str, float] = field(default_factory=dict)


def _find_actions_near(actions: Dict[int, List[Action]], step: int,
                       window: int) -> List[Action]:
    out = []
    for s, acts in actions.items():
        if step - window <= s <= step:
            out.extend(acts)
    return out


def diagnose(history: List[Telemetry], zones: Dict[str, Zone],
             actions: Dict[int, List[Action]],
             design_flows: Dict[str, float],
             pressure_spike_kpa: float = 15.0,
             cold_streak_steps: int = 24) -> List[Finding]:
    findings: List[Finding] = []
    if len(history) < 2:
        return findings

    # 1. 压力波动: 相邻步压降突变
    for i in range(1, len(history)):
        dp = history[i].pressure_kpa - history[i - 1].pressure_kpa
        if abs(dp) >= pressure_spike_kpa:
            near = _find_actions_near(actions, history[i].step, window=12)
            flow_acts = [a for a in near if a.kind in ("branch_flow", "curtail")]
            if flow_acts:
                cause = "; ".join(a.describe() for a in flow_acts)
                root = (f"快速调节引起总流量突变: {cause}; "
                        f"建议分多步小幅度调节或错时执行")
            else:
                root = "无近期人工操作记录, 疑似设备侧扰动, 需核查泵站"
            findings.append(Finding(
                "pressure_surge", None, history[i].step,
                f"压力突变 {dp:+.1f} kPa (当前 {history[i].pressure_kpa:.0f} kPa)",
                root, {"delta_kpa": dp,
                       "total_flow": history[i].total_flow}))

    # 2. 回水温度异常
    for rec in history:
        if rec.return_temp > 55.0:
            findings.append(Finding(
                "return_temp", None, rec.step,
                f"回水温度偏高 {rec.return_temp:.1f}°C",
                "总流量相对热负荷过大, 供回水温差过小, 换热设备未充分利用; "
                "建议降低循环泵速或关小非关键支路",
                {"return_temp": rec.return_temp,
                 "total_flow": rec.total_flow}))
        elif rec.return_temp < 30.0:
            findings.append(Finding(
                "return_temp", None, rec.step,
                f"回水温度偏低 {rec.return_temp:.1f}°C",
                "供水温度不足或末端过流, 检查热源出力是否饱和及供水温度设定",
                {"return_temp": rec.return_temp,
                 "supply_temp": rec.source_supply_temp}))

    # 3. 区域持续低温: 沿因果链定位
    streak: Dict[str, int] = {n: 0 for n in zones}
    reported: Dict[str, bool] = {n: False for n in zones}
    for rec in history:
        for name, zone in zones.items():
            if rec.zone_indoor[name] < zone.indoor_min:
                streak[name] += 1
            else:
                streak[name] = 0
                reported[name] = False
            if streak[name] >= cold_streak_steps and not reported[name]:
                reported[name] = True
                findings.append(_diagnose_cold_zone(rec, name, zone, history,
                                          design_flows[name]))
    return findings


def _diagnose_cold_zone(rec: Telemetry, name: str, zone: Zone,
                        history: List[Telemetry],
                        design_flow: float) -> Finding:
    delivered = rec.zone_delivered_kw[name]
    demand = rec.zone_demand_kw[name]
    gap = demand - delivered
    evidence = {"indoor": rec.zone_indoor[name], "delivered_kw": delivered,
                "demand_kw": demand}
    summary = (f"{name} 室温 {rec.zone_indoor[name]:.1f}°C "
               f"持续低于下限 {zone.indoor_min}°C")

    flow_ratio = rec.zone_flow[name] / max(design_flow, 1e-6)
    peak_output = max(h.source_output_kw for h in history)
    supply_factor = rec.zone_supply_factor.get(name, 1.0)
    if supply_factor < 1.0:
        root = (f"该区域处于降供状态(系数 {supply_factor:.2f}), "
                f"热量缺口 {gap:.0f} kW; 若非关键区可维持, 否则恢复供应")
    elif flow_ratio < 0.8:
        root = (f"支路流量不足(当前为设计值 {flow_ratio:.0%}), "
                f"供水温度到达正常, 热量缺口 {gap:.0f} kW; 建议开大该支路阀门")
    elif rec.source_output_kw >= 0.98 * peak_output and \
            rec.source_supply_temp < 75.0:
        root = (f"热源出力已接近上限({rec.source_output_kw:.0f} kW), "
                f"供水温度被拉低至 {rec.source_supply_temp:.1f}°C, "
                f"属热源能力不足; 建议提前蓄热或削减非关键区负荷")
    elif rec.source_supply_temp < 70.0:
        root = (f"热源供水温度不足({rec.source_supply_temp:.1f}°C), "
                f"当前出力 {rec.source_output_kw:.0f} kW; "
                f"检查出力设定是否未跟上负荷增长或热源能力受限")
    elif rec.source_supply_temp - rec.zone_supply_temp[name] > 8.0:
        root = (f"管网输运温降过大: 热源 {rec.source_supply_temp:.1f}°C -> "
                f"区域入口 {rec.zone_supply_temp[name]:.1f}°C, "
                f"且长距离管线存在输运延迟; 建议提高供水温度设定提前量")
    else:
        root = (f"综合因素: 缺口 {gap:.0f} kW, 区域入口水温 "
                f"{rec.zone_supply_temp[name]:.1f}°C, 流量为设计值 "
                f"{flow_ratio:.0%}, 需结合操作记录复核")
    return Finding("cold_zone", name, rec.step, summary, root, evidence)
