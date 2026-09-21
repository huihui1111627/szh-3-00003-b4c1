"""物理内核测试:输运延迟、建筑惯性、水力耦合、热量守恒。"""
import math
import unittest

from heating_sim.model import WeatherPoint, WeatherProcess, Action
from heating_sim.network import HydraulicNetwork, TRANSIT_REF_MS
from heating_sim.zones import ZoneSystem
from heating_sim.engine import Simulator
from heating_sim.scenario import build_source, build_zones
from heating_sim.strategies.baseline import WeatherCompensation
from heating_sim.weather import make_cold_wave


def steady_weather(tout_c=-10.0, hours=2):
    n = hours * 6
    return WeatherProcess([WeatherPoint(i * 10, tout_c, 0.0) for i in range(n + 1)])


class TransportDelayTests(unittest.TestCase):
    def test_parcel_arrival_lag(self):
        """微团必须走过管长才到达末端:远端园区延迟显著大于近端医院。"""
        zones = build_zones()
        net = HydraulicNetwork(build_source(), zones)
        net.prefill(60.0)
        net.solve_hydraulics()
        net.inject(90.0)  # 一个热水脉冲
        near = zones["医院片区"].pipe_len_m / TRANSIT_REF_MS / 60.0
        far = zones["开发园区"].pipe_len_m / TRANSIT_REF_MS / 60.0
        self.assertGreater(far, near * 3)
        # 推进若干步但少于近端渡越时间,末端温度不应立刻等于 90
        net.advance_thermal(10)
        self.assertLess(net.branches["医院片区"].tsupply_c, 80.0)

    def test_temperature_decay_along_pipe(self):
        """长输管道末端温度低于近端(沿程热损失)。"""
        sim = Simulator(zones=build_zones(), source=build_source())
        weather = steady_weather(-22.0, hours=10)
        trace = sim.run(WeatherCompensation(), weather)
        i = len(trace.minutes) // 2
        t_near = trace.zones["医院片区"].tsupply_c[i]
        t_far = trace.zones["开发园区"].tsupply_c[i]
        self.assertGreater(t_near, t_far + 1.0)


class BuildingInertiaTests(unittest.TestCase):
    def test_indoor_changes_slowly(self):
        """室温对阶跃寒冷的响应受时间常数约束,不会一步跌到新稳态。"""
        sim = Simulator(zones=build_zones(), source=build_source())
        trace = sim.run(WeatherCompensation(), steady_weather(-22.0, hours=6))
        zt = trace.zones["住宅片区"]
        drop_first_h = 18.0 - zt.indoor_c[6]
        total_drop = 18.0 - min(zt.indoor_c)
        self.assertLess(drop_first_h, 0.8 * total_drop + 0.5)
        self.assertGreater(total_drop, 1.0)


class HydraulicsTests(unittest.TestCase):
    def test_total_flow_is_sum_of_branches(self):
        net = HydraulicNetwork(build_source(), build_zones())
        _, _, flows = net.solve_hydraulics()
        self.assertAlmostEqual(sum(flows.values()), net.total_m3h, places=6)

    def test_valve_change_causes_pressure_surge(self):
        net = HydraulicNetwork(build_source(), build_zones())
        net.solve_hydraulics()
        net.set_valve("开发园区", 0.3)  # 一步关小
        surge, _, _ = net.solve_hydraulics()
        self.assertGreater(abs(surge), 1.0e4)
        self.assertGreater(surge, 0)  # 关阀 -> 压差冲高

    def test_opening_valve_steals_flow(self):
        """开大远端支路会挤占其他支路流量(并联耦合)。"""
        net = HydraulicNetwork(build_source(), build_zones())
        net.solve_hydraulics()
        before = net.flow_m3h["医院片区"]
        net.set_valve("开发园区", 1.5)
        net.solve_hydraulics()
        self.assertLess(net.flow_m3h["医院片区"], before)


class EnergyBalanceTests(unittest.TestCase):
    def test_supply_temp_consistent_with_heat(self):
        """首站供热量 Q=V*rho*cp*(Ts-Tr) 与记录的热源出力近似一致(母管小惯性)。"""
        from heating_sim.model import CP, RHO
        sim = Simulator(zones=build_zones(), source=build_source())
        trace = sim.run(WeatherCompensation(), make_cold_wave())
        for i in range(20, len(trace.minutes)):
            mass = trace.total_flow_m3h[i] / 3600.0 * RHO
            q_fluid = mass * CP * (trace.tsupply_c[i] - trace.treturn_mix_c[i])
            self.assertLess(abs(q_fluid - trace.source_q_w[i])
                            / max(trace.source_q_w[i], 1), 0.06)

    def test_ramp_limits_source_change(self):
        """热源出力单步变化不超过爬坡速率(蓄能缓冲前的热源侧约束)。"""
        sim = Simulator(zones=build_zones(), source=build_source())
        trace = sim.run(WeatherCompensation(), make_cold_wave())
        ramp = build_source().ramp_w_per_min * 10
        for i in range(1, len(trace.source_q_w)):
            self.assertLessEqual(trace.source_q_w[i] - trace.source_q_w[i - 1],
                                 ramp * 1.05)

    def test_source_capacity_limits_supply(self):
        sim = Simulator(zones=build_zones(), source=build_source())
        trace = sim.run(WeatherCompensation(), make_cold_wave())
        self.assertLessEqual(max(trace.tsupply_c),
                             build_source().t_supply_max_c + 0.1)
        cap_alarms = [a for a in trace.alarms if a.code == "source_capacity"]
        self.assertTrue(cap_alarms)


if __name__ == "__main__":
    unittest.main()
