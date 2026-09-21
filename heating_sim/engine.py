"""仿真引擎:热源-泵站-管网-建筑联立逐时间步推进。

热量闭合关系(每个时间步):
    热源供热量 Q_src(受爬坡/装机约束) == 各分区取热量之和 Q_load_total
    混合回水温度由回水管热惯性按能量守恒更新;
    供水温度 = 回水 + Q_src / (总质量流量*cp)。
这样:回水偏低 = 建筑侧大温差取热;回水偏高 = 流量过剩、取热不足;
热源顶上限时供水温度无法继续抬高 -> 全网欠供。
"""
from __future__ import annotations

from typing import Dict, List, Tuple

from .model import (CP, RHO, Action, Alarm, Trace, WeatherProcess, ZoneTrace,
                    A_PRESSURE_SURGE, A_TRET_LOW, A_TRET_HIGH, A_ZONE_COLD,
                    A_SUPPLY_HOT, A_SOURCE_CAP)
from .network import HydraulicNetwork
from .zones import ZoneSystem
from .scenario import build_source, build_zones

SURGE_WARN_PA = 40_000
SURGE_CRIT_PA = 100_000
INDOOR_WARN_C = 16.5
INDOOR_CRIT_C = 15.0
COLD_SUSTAIN_MIN = 60 * 6
TRET_LOW_WARN_C = 43.0
TRET_LOW_CRIT_C = 38.0
DELTA_T_SMALL_K = 14.0
FLOW_OVER_RATIO = 1.08
SUPPLY_WARN_C = 94.0
WARMUP_MIN = 600
RETURN_PIPE_TAU_S = 11.0 * 60.0
SUPPLY_MAIN_M3 = 220.0          # 热源供水母管/分水器蓄水量,决定质调节惯性


class Simulator:
    def __init__(self, dt_min: int = 10, source=None, zones=None):
        self.dt = dt_min
        self.source = source or build_source()
        self.zones = zones or build_zones()
        self.net = HydraulicNetwork(self.source, self.zones)
        self.zsys = ZoneSystem(self.zones, self.net)
        self.t_target_c = 70.0
        self.t_source_c = 70.0
        self.t_return_mix_c = 53.0
        self.q_source_w = 0.0
        self.q_demand_w = 0.0
        self.q_override_w = None
        self.restore_events: List[Tuple[int, dict]] = []
        self._alarm_active: Dict[Tuple[str, str], Alarm] = {}
        self._cold_since: Dict[str, int] = {}

    def run(self, policy, weather: WeatherProcess,
            warmup: bool = True, start_minute: int = 0) -> Trace:
        policy.init(self.source, self.zones)
        if warmup:
            self._warmup(weather, start_minute)
        trace = Trace()
        trace.zones = {n: ZoneTrace() for n in self.zones}

        end = start_minute + weather.duration_min
        minute = start_minute
        while minute <= end:
            for act in policy.decide(minute, weather, trace):
                self._apply_action(act, trace)
            self._expire_restores(minute, trace)

            surge, dp, flows = self.net.solve_hydraulics()
            self.net.inject(self.t_source_c)
            self.net.advance_thermal(self.dt)

            w = weather.weather_at(minute)
            zone_out = self.zsys.step(self.dt, w.effective_tout())
            self._step_source(zone_out, flows)

            self._record(trace, minute, w.tout_c, flows, dp, zone_out)
            self._check_alarms(trace, minute, surge, flows, zone_out)
            minute += self.dt
        return trace

    # ------------------------------------------------------------------
    def _warmup(self, weather: WeatherProcess, start_minute: int) -> None:
        """以起始气象平稳预热管网与建筑至近稳态。"""
        from .policies import _target_supply_from_curve
        w0 = weather.weather_at(start_minute)
        warm_target = _target_supply_from_curve(w0.effective_tout())
        self.net.prefill(warm_target)
        self._settle_initial(warm_target, w0.effective_tout())
        minute = -WARMUP_MIN
        while minute < start_minute:
            self.t_target_c = warm_target
            self.net.solve_hydraulics()
            self.net.inject(self.t_source_c)
            self.net.advance_thermal(self.dt)
            zone_out = self.zsys.step(self.dt, w0.effective_tout())
            self._step_source(zone_out, self.net.flow_m3h)
            minute += self.dt

    def _settle_initial(self, t_supply_c: float, tout_eff_c: float) -> None:
        self.net.solve_hydraulics()
        self.t_source_c = t_supply_c
        self.t_target_c = t_supply_c
        self.zsys.settle(t_supply_c, tout_eff_c)
        num = den = 0.0
        for name, z in self.zones.items():
            mass = self.net.flow_m3h[name] / 3600.0 * RHO
            tr = t_supply_c - self.zsys.rt[name].q_actual_w / (mass * CP)
            num += tr * self.net.flow_m3h[name]
            den += self.net.flow_m3h[name]
        self.t_return_mix_c = num / den
        total_mass = sum(self.net.flow_m3h.values()) / 3600.0 * RHO
        self.q_source_w = sum(rt.q_actual_w for rt in self.zsys.rt.values())
        self.q_demand_w = self.q_source_w
        self.t_source_c = self.t_return_mix_c + self.q_source_w / (total_mass * CP)

    # ------------------------------------------------------------------
    def _apply_action(self, act: Action, trace: Trace) -> None:
        trace.actions.append(act)
        snap = {}
        if act.set_source_t_c is not None:
            self.t_target_c = act.set_source_t_c
            self.q_override_w = None
        if act.set_source_w is not None:
            self.q_override_w = act.set_source_w
        for name, v in act.branch_valve.items():
            snap.setdefault("valve", {})[name] = self.net.branches[name].valve
            self.net.set_valve(name, v)
        for name, v in act.curtail.items():
            snap.setdefault("curtail", {})[name] = self.net.branches[name].curtail
            self.net.set_curtail(name, v)
        if act.restore_after_min and snap:
            self.restore_events.append((act.minute + act.restore_after_min, snap))

    def _expire_restores(self, now: int, trace: Trace) -> None:
        due = [e for e in self.restore_events if e[0] <= now]
        self.restore_events = [e for e in self.restore_events if e[0] > now]
        for _, snap in due:
            for name, v in snap.get("valve", {}).items():
                self.net.set_valve(name, v)
            for name, v in snap.get("curtail", {}).items():
                self.net.set_curtail(name, v)

    def _step_source(self, zone_out, flows: Dict[str, float]) -> None:
        total_m3s = sum(flows.values()) / 3600.0
        mass = total_m3s * RHO
        q_load = sum(o[0] for o in zone_out.values())
        if mass <= 1e-9:
            self.q_demand_w = 0.0
            self.q_source_w *= 0.9
            return

        if self.q_override_w is not None:
            q_target = self.q_override_w
        else:
            # 为抬高回水到目标供水温度,热源需补的热;并受当前取热需求牵引
            q_for_target = mass * CP * (self.t_target_c - self.t_return_mix_c)
            q_target = max(q_for_target, q_load)

        q_cmd = min(q_target, self.source.q_max_w)
        ramp = self.source.ramp_w_per_min * self.dt
        if q_cmd > self.q_source_w:
            self.q_source_w = min(q_cmd, self.q_source_w + ramp)
        else:
            self.q_source_w = max(q_cmd, self.q_source_w - ramp)
        self.q_demand_w = q_target

        # 回水管热惯性:混合回水一阶跟随流量加权瞬时回水
        import math
        num = sum(o[1] * flows[n] for n, o in zone_out.items())
        den = sum(flows.values())
        t_return_inst = num / den
        fr = 1.0 - math.exp(-(self.dt * 60.0) / RETURN_PIPE_TAU_S)
        self.t_return_mix_c += fr * (t_return_inst - self.t_return_mix_c)

        # 供水母管能量守恒(显式蓄能):
        # C_m * dT_s/dt = Q_src - m*cp*(T_s - T_ret)
        # 热源爬坡时差额由母管蓄热补充,因此 Q_src 与建筑侧取热不必逐拍相等,
        # 但供水温度的变化严格由能量平衡决定。
        c_main = SUPPLY_MAIN_M3 * RHO * CP
        dt_s = self.dt * 60.0
        q_out = mass * CP * (self.t_source_c - self.t_return_mix_c)
        t_new = self.t_source_c + dt_s * (self.q_source_w - q_out) / c_main
        # 温度上限代表换热器/锅炉安全限值:超额热量无法送出
        self.t_source_c = min(max(t_new, self.t_return_mix_c + 1.0),
                              self.source.t_supply_max_c)

    # ------------------------------------------------------------------
    def _record(self, trace, minute, tout, flows, dp, zone_out):
        trace.minutes.append(minute)
        trace.tout_c.append(tout)
        trace.tset_c.append(self.t_target_c)
        trace.tsupply_c.append(self.t_source_c)
        trace.treturn_mix_c.append(self.t_return_mix_c)
        trace.source_q_w.append(self.q_source_w)
        trace.source_demand_w.append(self.q_demand_w)
        trace.total_flow_m3h.append(sum(flows.values()))
        trace.dp_station_pa.append(dp)
        for name, (q, tr, dem, _) in zone_out.items():
            b = self.net.branches[name]
            zt = trace.zones[name]
            zt.indoor_c.append(self.zsys.rt[name].indoor_c)
            zt.tsupply_c.append(b.tsupply_c)
            zt.treturn_c.append(tr)
            zt.flow_m3h.append(flows[name])
            zt.valve.append(b.valve)
            zt.curtail.append(b.curtail)
            zt.delivered_w.append(q)
            zt.demand_w.append(dem)

    # ------------------------------------------------------------------
    def _check_alarms(self, trace, minute, surge, flows, zone_out):
        if abs(surge) >= SURGE_WARN_PA:
            sev = "critical" if abs(surge) >= SURGE_CRIT_PA else "warn"
            direction = "冲高" if surge > 0 else "入口掉压"
            trace.alarms.append(Alarm(
                minute, A_PRESSURE_SURGE, "热源首站",
                f"泵站压力短时{direction} {surge/1000:.0f} kPa,疑似支路导纳快速变化",
                sev, surge,
                SURGE_CRIT_PA if sev == "critical" else SURGE_WARN_PA))

        if (self.q_demand_w > self.source.q_max_w * 1.02
                and self.q_source_w >= self.source.q_max_w * 0.995):
            key = (A_SOURCE_CAP, "热源")
            if key not in self._alarm_active:
                al = Alarm(minute, A_SOURCE_CAP, "热源",
                           f"热源出力已顶上限 {self.source.q_max_w/1e6:.0f}MW,"
                           f"需求 {self.q_demand_w/1e6:.1f}MW,全网欠供",
                           "critical", self.q_source_w, self.source.q_max_w)
                trace.alarms.append(al)
                self._alarm_active[key] = al

        if self.t_source_c >= SUPPLY_WARN_C:
            key = (A_SUPPLY_HOT, "热源")
            if key not in self._alarm_active:
                trace.alarms.append(Alarm(
                    minute, A_SUPPLY_HOT, "热源",
                    f"供水温度 {self.t_source_c:.1f}℃ 接近/达到设备上限",
                    "warn", self.t_source_c, SUPPLY_WARN_C))
                self._alarm_active[key] = trace.alarms[-1]

        for name, (q, tr, dem, _) in zone_out.items():
            z = self.zones[name]
            zt = trace.zones[name]
            indoor = zt.indoor_c[-1]
            tin = zt.tsupply_c[-1]
            ratio = flows[name] / self.net.nominal_flow(z)

            if ratio > 0.35 and tr <= TRET_LOW_WARN_C:
                sev = "critical" if tr <= TRET_LOW_CRIT_C else "warn"
                key = (A_TRET_LOW, name)
                if key not in self._alarm_active:
                    al = Alarm(minute, A_TRET_LOW, name,
                               f"{name}回水温度 {tr:.1f}℃ 异常偏低,大温差运行",
                               sev, tr, TRET_LOW_WARN_C)
                    trace.alarms.append(al)
                    self._alarm_active[key] = al
            elif (A_TRET_LOW, name) in self._alarm_active and tr > TRET_LOW_WARN_C + 2:
                self._alarm_active.pop((A_TRET_LOW, name), None)

            dt_t = tin - tr
            if dt_t < DELTA_T_SMALL_K and ratio > FLOW_OVER_RATIO:
                key = (A_TRET_HIGH, name)
                if key not in self._alarm_active:
                    al = Alarm(minute, A_TRET_HIGH, name,
                               f"{name}供回温差仅 {dt_t:.1f}K、回水 {tr:.1f}℃,"
                               f"流量为额定 {ratio:.0%},水力失衡/过热",
                               "warn", tr, tin - DELTA_T_SMALL_K)
                    trace.alarms.append(al)
                    self._alarm_active[key] = al
            elif (A_TRET_HIGH, name) in self._alarm_active and dt_t > DELTA_T_SMALL_K + 2:
                self._alarm_active.pop((A_TRET_HIGH, name), None)

            if indoor < INDOOR_WARN_C:
                since = self._cold_since.setdefault(name, minute)
                sustained = minute - since
                key = (A_ZONE_COLD, name)
                sev = "critical" if (indoor < INDOOR_CRIT_C or
                                     sustained >= COLD_SUSTAIN_MIN) else "warn"
                if key not in self._alarm_active:
                    al = Alarm(minute, A_ZONE_COLD, name,
                               f"{name}室内温度 {indoor:.1f}℃ 低于 {INDOOR_WARN_C}℃",
                               sev, indoor, INDOOR_WARN_C)
                    trace.alarms.append(al)
                    self._alarm_active[key] = al
                elif sev == "critical" and self._alarm_active[key].severity != "critical":
                    reason = ("已持续低温 6 小时以上" if sustained >= COLD_SUSTAIN_MIN
                              else f"温度跌破 {INDOOR_CRIT_C}℃")
                    al = Alarm(minute, A_ZONE_COLD, name,
                               f"{name}{reason}(当前 {indoor:.1f}℃),升级为严重",
                               "critical", indoor, INDOOR_WARN_C)
                    trace.alarms.append(al)
                    self._alarm_active[key] = al
            else:
                self._cold_since.pop(name, None)
                self._alarm_active.pop((A_ZONE_COLD, name), None)


def run_policy(policy, weather: WeatherProcess, dt_min: int = 10,
               warmup: bool = True, source=None, zones=None) -> Trace:
    sim = Simulator(dt_min, source=source, zones=zones)
    return sim.run(policy, weather, warmup=warmup)
