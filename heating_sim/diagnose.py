"""告警根因分析:不停留在告警本身,而是沿调度动作与物理轨迹追溯因果链。"""
from __future__ import annotations

from typing import Dict, List, Optional, Tuple

from .model import (Action, Alarm, RootCause, Trace,
                    A_PRESSURE_SURGE, A_TRET_LOW, A_TRET_HIGH, A_ZONE_COLD,
                    A_SUPPLY_HOT, A_SOURCE_CAP)

TOL_STEPS = 1          # 动作与告警允许的时间差(决策步数)


def _idx(trace: Trace, minute: int) -> int:
    return max(0, min(len(trace.minutes) - 1,
                      (minute - trace.minutes[0]) // max(
                          trace.minutes[1] - trace.minutes[0] if len(trace.minutes) > 1 else 1, 1)))


def _actions_near(trace: Trace, minute: int,
                  window_steps: int = TOL_STEPS) -> List[Tuple[int, Action]]:
    out = []
    if len(trace.minutes) < 2:
        return list(enumerate(trace.actions))
    dt = trace.minutes[1] - trace.minutes[0]
    idx = _idx(trace, minute)
    for a in trace.actions:
        ai = _idx(trace, a.minute)
        if abs(ai - idx) <= window_steps:
            out.append((ai, a))
    return out


def diagnose(trace: Trace) -> List[RootCause]:
    causes: List[RootCause] = []
    cap_window = None
    for al in trace.alarms:
        if al.code == A_PRESSURE_SURGE:
            causes.append(_pressure(trace, al))
        elif al.code == A_ZONE_COLD:
            causes.append(_zone_cold(trace, al))
        elif al.code == A_TRET_LOW:
            causes.append(_return_low(trace, al))
        elif al.code == A_TRET_HIGH:
            causes.append(_return_high(trace, al))
        elif al.code == A_SUPPLY_HOT:
            causes.append(_supply_high(trace, al))
        elif al.code == A_SOURCE_CAP:
            causes.append(_source_cap(trace, al))
    return causes


def _find_action(trace, minute, pred, lookback_min: int = 1440) -> Optional[Action]:
    # 告警前 lookback_min 内最近一次满足条件的动作(含输运延迟导致的晚发后果)
    for a in reversed(trace.actions):
        if a.minute <= minute and minute - a.minute <= lookback_min and pred(a):
            return a
    return None


def _pressure(trace: Trace, al: Alarm) -> RootCause:
    act = _find_action(trace, al.minute,
                       lambda a: bool(a.branch_valve or a.curtail))
    chain = [f"泵站压力在 {al.minute//60}h{al.minute%60:02d} "
             f"{'冲高' if al.value > 0 else '掉压'} {abs(al.value)/1000:.0f}kPa"]
    who: List[str] = []
    if act is not None:
        if act.branch_valve:
            for n, v in act.branch_valve.items():
                chain.append(f"策略「{act.source}」于 {act.minute//60}h 将 {n} "
                             f"阀门一步调至 {v:.2f}")
                who.append(f"{act.source}:{act.reason}")
        if act.curtail:
            for n, v in act.curtail.items():
                chain.append(f"策略「{act.source}」于 {act.minute//60}h "
                             f"将 {n} 限供系数一步调至 {v:.0%}")
                who.append(f"{act.source}:{act.reason}")
        chain.append("支路导纳在单步内大幅变化,泵-网惯性来不及平衡 → 压力波"
                     "(快速调节的直接物理后果)")
        conf = "high"
    else:
        chain.append("未发现同期阀门/限供操作,压力波动来自热源侧流量快速变化")
        conf = "medium"
    return RootCause(al, chain, who or ["热源侧流量快速变化"], conf)


def _zone_cold(trace: Trace, al: Alarm) -> RootCause:
    name = al.target
    zt = trace.zones[name]
    idx = _idx(trace, al.minute)
    look = max(1, idx - 5)
    chain = [f"{name}室内温度降至 {al.value:.1f}℃"
             + (f",已进入持续低温状态"
                if al.severity == "critical" else "")]

    curtail_now = zt.curtail[idx]
    valve_now = zt.valve[idx]
    causes: List[str] = []

    # 1) 当前正在被限供
    if curtail_now > 0.05:
        act = _find_action(trace, al.minute, lambda a: name in a.curtail and a.curtail.get(name, 0) > 0.05)
        chain.append(f"该分区当前被限供 {curtail_now:.0%},实际流量被人为压低")
        if act:
            chain.append(f"限供来自策略「{act.source}」: {act.reason}")
            chain.append("根因:峰值热源能力不足时,调度优先保关键区域,主动压减了该区流量"
                         "(需求侧管理),低温是有意取舍的结果而非设备故障")
            causes.append(f"{act.source}:{act.reason}")
        else:
            chain.append("根因:临时限供到点未恢复")
            causes.append("临时限供未按期恢复")

    # 2) 阀门被关小
    if valve_now < 0.9:
        act = _find_action(trace, al.minute, lambda a: name in a.branch_valve)
        chain.append(f"该支路阀门开度仅 {valve_now:.2f},输配能力不足")
        if act:
            chain.append(f"操作来自「{act.source}」: {act.reason}")
            causes.append(f"{act.source}:{act.reason}")

    # 3) 热量侧根因:若当前正被主动限供,低温即调度取舍,不再追加容量根因
    deliberate_curtail = curtail_now > 0.05 and any("限供" in c or "保关键" in c
                                                    for c in causes)
    win = zt.delivered_w[max(look,0):idx + 1]
    dem = zt.demand_w[max(look,0):idx + 1]
    ratio = (sum(win) / sum(dem)) if sum(dem) > 0 else 1.0
    cap = [a for a in trace.alarms
           if a.code == A_SOURCE_CAP and a.minute <= al.minute + 20
           and al.minute - a.minute <= 360]
    if cap and ratio < 0.99 and not deliberate_curtail:
        chain.append(f"近 1h 实供为需求的 {ratio:.0%},热源同期已顶出力上限")
        chain.append("根因:寒潮峰值(含迟报更冷实况)热需求超过热源装机能力,"
                     "全网热量总量不足,各分区被动欠供")
        causes.append("热源装机能力不足")
    elif ratio < 0.95:
        t_sup_src = trace.tsupply_c[idx]
        t_sup_zone = zt.tsupply_c[idx]
        lag = t_sup_src - t_sup_zone
        if lag > 3.0 or _flow_recently_changed(zt, idx):
            chain.append(f"首站供水 {t_sup_src:.0f}℃ 而该区入口仅 "
                         f"{t_sup_zone:.0f}℃,热量尚在管输途中")
            chain.append("根因:调节指令经长输管网延迟到达(热惯性/输运延迟),"
                         "前期未提前蓄热")
            causes.append("管输延迟+未提前蓄热")
        else:
            chain.append(f"近 1h 实供仅为需求的 {ratio:.0%}")
            chain.append("根因:供水温度/流量组合提供的热量不足")
            causes.append("供水温度偏低")
    elif not causes:
        chain.append(f"近 1h 得热/需求比 {ratio:.0%},热量基本平衡")
        if al.severity == "critical":
            chain.append("根因:建筑热惯性有限,前期长时间轻度欠供累积为持续低温")
            causes.append("轻度欠供累积")

    return RootCause(al, chain, causes or ["热量供需失衡"], "high")


def _flow_recently_changed(zt, idx, window=6) -> bool:
    lo = max(0, idx - window)
    seg = zt.flow_m3h[lo:idx + 1]
    return seg and (max(seg) - min(seg)) > 0.15 * max(seg)


def _return_low(trace: Trace, al: Alarm) -> RootCause:
    name = al.target
    idx = _idx(trace, al.minute)
    zt = trace.zones[name]
    chain = [f"{name}回水温度仅 {al.value:.1f}℃,大温差运行"]
    causes: List[str] = []

    cap = [a for a in trace.alarms
           if a.code == A_SOURCE_CAP and a.minute <= al.minute
           and al.minute - a.minute <= 300]
    src_supply_drop = (trace.tsupply_c[max(0, idx - 6)] - trace.tsupply_c[idx])
    if cap or src_supply_drop > 6:
        chain.append("同期首站供水温度走低且热源出力顶上限")
        chain.append("根因:热源供热能力不足 → 供水温度被迫降低 → "
                     "回水偏冷,而非局部流量问题")
        causes.append("热源能力不足")
    else:
        ratio = zt.flow_m3h[idx]
        tin = zt.tsupply_c[idx]
        chain.append(f"该区入口供水 {tin:.0f}℃、流量 {ratio:.0f} m3/h 量级,回水偏冷")
        chain.append("根因:相对当前流量,散热器取热过度/供水温度不足,"
                     "热量未按需求补足(需核对气候补偿曲线与泵阀匹配)")
        causes.append("供水温度-流量不匹配")
    return RootCause(al, chain, causes, "high" if cap else "medium")


def _return_high(trace: Trace, al: Alarm) -> RootCause:
    name = al.target
    idx = _idx(trace, al.minute)
    zt = trace.zones[name]
    chain = [f"{name}供回温差过小、回水 {al.value:.1f}℃ 偏高"]
    causes: List[str] = []
    act = None
    for a in reversed(trace.actions):
        if (name in a.branch_valve and a.minute <= al.minute
                and al.minute - a.minute <= 720):
            act = a
            break
    valve_open = zt.valve[idx] if idx < len(zt.valve) else 1.0
    if act and any(v > 1.05 for v in act.branch_valve.values()):
        v = act.branch_valve[name]
        chain.append(f"策略「{act.source}」于 {act.minute//60}h 将阀门开到 {v:.2f}")
        chain.append("根因:抢流量式开阀使该区流量超过额定,水在散热器中来不及充分冷却"
                     " → 小温差、高回水,同时抢占其他支路流量(水力失衡)")
        causes.append(f"{act.source}:{act.reason}")
        conf = "high"
    else:
        if valve_open > 1.05:
            chain.append(f"该支路阀门当前开度 {valve_open:.2f},抢流量的后果经"
                         "输运延迟后才在回水温度上显现")
            chain.append("根因:流量过剩使水在换热器中冷却不足 → 小温差、高回水,"
                         "同时挤占其他支路流量(水力失衡)")
            causes.append("支路抢流量(延迟显现)")
        else:
            chain.append("根因:二次侧流量相对热负荷过剩(过热/水力失调),热量在建筑侧过剩")
            causes.append("水力失调/过热")
        conf = "high" if valve_open > 1.05 else "medium"
    return RootCause(al, chain, causes, conf)


def _supply_high(trace: Trace, al: Alarm) -> RootCause:
    act = _find_action(trace, al.minute,
                       lambda a: a.set_source_t_c is not None and a.set_source_t_c >= 90)
    chain = [f"首站供水温度 {al.value:.1f}℃,触及设备上限"]
    causes: List[str] = []
    if act:
        chain.append(f"策略「{act.source}」把供水设定提到 {act.set_source_t_c}℃"
                     f"({act.reason})")
        causes.append(f"{act.source}:{act.reason}")
    chain.append("根因:为弥补寒潮热量缺口而把供水推到设备边界,需关注超温风险")
    causes.append("供水逼近设备上限")
    return RootCause(al, chain, causes, "high")


def _source_cap(trace: Trace, al: Alarm) -> RootCause:
    idx = _idx(trace, al.minute)
    demand = trace.source_demand_w[idx]
    gap = demand - al.threshold
    chain = [f"热源出力顶到 {al.threshold/1e6:.0f}MW,但热需求达 {demand/1e6:.1f}MW",
             f"缺口约 {gap/1e6:.1f}MW,全网热量总量不足"]
    # 寒潮深度证据
    t = trace.tout_c[idx]
    chain.append(f"当时室外 {t:.0f}℃;各分区需求按围护结构热损失同步抬升")
    chain.append("根因:寒潮峰值需求超过装机能力,需提前蓄热、需求侧管理或启用备用热源")
    return RootCause(al, chain, ["热源装机能力不足"], "high")
