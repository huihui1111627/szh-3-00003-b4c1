import unittest

from district_heating.actions import branch_flow, curtail, source_output
from district_heating.backfill import Correction, apply_corrections
from district_heating.diagnosis import diagnose
from district_heating.network import Network, Pipe
from district_heating.simulation import Simulation, evaluate
from district_heating.strategy import STRATEGIES, compare
from district_heating.weather import ColdSnap
from district_heating.buildings import default_zones


class TestWeather(unittest.TestCase):
    def test_reproducible(self):
        w1, w2 = ColdSnap(seed=42), ColdSnap(seed=42)
        for s in (0, 100, 500):
            self.assertEqual(w1.at(s).outdoor_temp, w2.at(s).outdoor_temp)

    def test_cold_snap_profile(self):
        w = ColdSnap(seed=1)
        temps = [w.at(s).outdoor_temp for s in range(w.n_steps)]
        self.assertLess(min(temps), -10.0)          # 寒潮谷底
        self.assertGreater(temps[-1], min(temps))   # 后期回升


class TestNetwork(unittest.TestCase):
    def test_pipe_delay_and_loss(self):
        pipe = Pipe("p", length_m=1000.0, diameter_m=0.3)
        dt = 1.0 / 12.0
        flow = 20.0  # kg/s
        n_delay = pipe.delay_steps(flow, dt)
        # 环境温度先取 50°C(无散热), 推入 90°C 阶跃:
        # 延迟未到时出口仍是初始 50°C
        first = pipe.transport(50.0, flow, 50.0, dt)
        self.assertAlmostEqual(first, 50.0, places=1)
        for _ in range(n_delay):
            out = pipe.transport(90.0, flow, 50.0, dt)
            self.assertAlmostEqual(out, 50.0, places=1)
        # 延迟到达后出口升温; 环境温度切到 0°C 检验沿程散热
        out = pipe.transport(90.0, flow, 0.0, dt)
        self.assertGreater(out, 50.0)   # 热量延迟到达
        self.assertLess(out, 90.0)      # 沿程散热

    def test_pressure_drops_with_flow(self):
        zones = default_zones()
        net = Network(zones, 1.0 / 12.0)
        net.step(85.0, -5.0, {n: 1.0 for n in zones})
        p1 = net.pressure_kpa
        for b in net.branches.values():
            b.flow_setpoint = 1.2
        net.step(85.0, -5.0, {n: 1.0 for n in zones})
        self.assertLess(net.pressure_kpa, p1)


class TestSimulation(unittest.TestCase):
    def test_curtail_critical_rejected(self):
        sim = Simulation(weather=ColdSnap(seed=3))
        err = sim._apply(curtail(0, "hospital", 0.5))
        self.assertIsNotNone(err)
        self.assertEqual(sim.zones["hospital"].supply_factor, 1.0)

    def test_run_and_kpis(self):
        sim = Simulation(weather=ColdSnap(seed=3))
        history = sim.run()
        self.assertEqual(len(history), sim.weather.n_steps)
        kpis = evaluate(history, sim.zones)
        for key in ("critical_cold_hours", "pressure_fluct_max_kpa",
                    "return_temp_abnormal_hours"):
            self.assertIn(key, kpis)


class TestDiagnosis(unittest.TestCase):
    def _design_flows(self, sim):
        return {n: b.design_flow_kg_s
                for n, b in sim.network.branches.items()}

    def test_pressure_surge_root_cause(self):
        sim = Simulation(weather=ColdSnap(seed=7))
        sim.schedule(branch_flow(300, "mall", 0.3, "保居民"))
        history = sim.run()
        findings = diagnose(history, sim.zones, sim.actions,
                            self._design_flows(sim))
        surges = [f for f in findings if f.kind == "pressure_surge"]
        self.assertTrue(surges)
        self.assertIn("支路流量", surges[0].root_cause)  # 关联到操作而非只告警

    def test_cold_zone_attributed_to_curtail(self):
        # 热源出力充足, 唯一扰动是对 mall 的降供 -> 根因应指向降供
        sim = Simulation(weather=ColdSnap(seed=7))
        sim.schedule(source_output(0, 9000.0, "提前满出力"))
        sim.schedule(curtail(100, "mall", 0.5, "临时降供"))
        history = sim.run()
        findings = diagnose(history, sim.zones, sim.actions,
                            self._design_flows(sim))
        cold = [f for f in findings
                if f.kind == "cold_zone" and f.zone == "mall"]
        self.assertTrue(cold)
        self.assertIn("降供", cold[0].root_cause)


class TestBackfill(unittest.TestCase):
    def test_backfill_replay(self):
        sim = Simulation(weather=ColdSnap(seed=11))
        sim.run(controller=STRATEGIES["predictive"])
        corrections = [Correction(step=s,
                                  outdoor_temp=sim.weather.at(s).outdoor_temp - 3.0)
                       for s in range(400, 421)]
        result = apply_corrections(sim, corrections,
                                   controller=STRATEGIES["predictive"])
        self.assertEqual(result.corrected_range, (400, 420))
        self.assertGreater(result.replayed_steps, 0)
        # 气温下修 3°C 后, 关键区低温时长应增加
        self.assertGreaterEqual(result.new_kpis["critical_cold_hours"],
                                result.old_kpis["critical_cold_hours"])


class TestStrategyCompare(unittest.TestCase):
    def test_same_weather_fair_comparison(self):
        results = compare(STRATEGIES, weather_seed=2026)
        self.assertEqual(len(results), 2)
        # 同一气象过程: 两策略室外温度序列一致
        for a, b in zip(results[0].history, results[1].history):
            self.assertAlmostEqual(a.outdoor_temp, b.outdoor_temp)
        # 协同策略关键区低温时长不劣于被动策略
        r = {x.name: x for x in results}
        self.assertLessEqual(r["predictive"].kpis["critical_cold_hours"],
                             r["reactive"].kpis["critical_cold_hours"])


if __name__ == "__main__":
    unittest.main()
