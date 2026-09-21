"""根因分析与策略对照、迟报补算测试。"""
import unittest

from heating_sim.compare import run_strategy, compare
from heating_sim.diagnose import diagnose
from heating_sim.model import (Action, A_PRESSURE_SURGE, A_TRET_HIGH,
                               A_ZONE_COLD, A_SOURCE_CAP)
from heating_sim.scenario import build_source, build_zones
from heating_sim.strategies import (WeatherCompensation, PreHeatBeforeColdWave,
                                    AggressiveFastRamp, PeakCurtail)
from heating_sim.weather import (LateReport, WeatherProvider, make_cold_wave)


class RootCauseTests(unittest.TestCase):
    def test_pressure_surge_traced_to_valve_action(self):
        r = run_strategy(AggressiveFastRamp(), make_cold_wave(),
                         source=build_source(), zones=build_zones())
        surges = [c for c in r.root_causes if c.alarm.code == A_PRESSURE_SURGE]
        self.assertTrue(surges)
        chain_text = " ".join(surges[0].chain)
        self.assertIn("阀门", chain_text)
        self.assertIn("激进快调", chain_text)

    def test_high_return_traced_to_flow_grab(self):
        r = run_strategy(AggressiveFastRamp(), make_cold_wave(),
                         source=build_source(), zones=build_zones())
        high = [c for c in r.root_causes if c.alarm.code == A_TRET_HIGH]
        self.assertTrue(high)
        text = " ".join(high[0].chain)
        self.assertTrue("水力失衡" in text or "抢流量" in text)

    def test_curtail_cold_explained_as_deliberate_tradeoff(self):
        r = run_strategy(PeakCurtail(), make_cold_wave(),
                         source=build_source(), zones=build_zones())
        cold = [c for c in r.root_causes if c.alarm.code == A_ZONE_COLD]
        text = " ".join(step for c in cold for step in c.chain)
        # 至少有一条低温根因说明限供/保关键的调度意图
        self.assertTrue("限供" in text or "保关键" in text or "优先" in text)

    def test_capacity_rootcause_is_installed_gap(self):
        r = run_strategy(WeatherCompensation(), make_cold_wave(),
                         source=build_source(), zones=build_zones())
        cap = [c for c in r.root_causes if c.alarm.code == A_SOURCE_CAP]
        self.assertTrue(cap)
        self.assertIn("装机", " ".join(cap[0].chain))


class ComparisonTests(unittest.TestCase):
    def test_same_weather_all_policies_run_and_differ(self):
        weather = make_cold_wave()
        results = compare([WeatherCompensation(), PreHeatBeforeColdWave(),
                           AggressiveFastRamp(), PeakCurtail()], weather,
                          source=build_source(), zones=build_zones())
        self.assertEqual(len(results), 4)
        # 提前蓄热在关键区舒适度上应不劣于基线
        ph = next(r for r in results if r.policy_name == "提前蓄热保供")
        bl = next(r for r in results if r.policy_name == "气候补偿基线")
        self.assertLessEqual(
            ph.metrics["critical_comfort_deficit_Kh"],
            bl.metrics["critical_comfort_deficit_Kh"] + 1e-6)
        # 激进快调出现压力波动,其他策略不应出现
        agg = next(r for r in results if "激进" in r.policy_name)
        self.assertGreaterEqual(agg.metrics["pressure_surge_count"], 1)
        for r in results:
            if "激进" not in r.policy_name:
                self.assertEqual(r.metrics["pressure_surge_count"], 0)


class LateReportTests(unittest.TestCase):
    def test_late_report_backfill_changes_assessment(self):
        zones, source = build_zones(), build_source()
        forecast = make_cold_wave()
        provider = WeatherProvider(forecast, [
            LateReport(14 * 60, 26 * 60, "tout_c", -17.0, 24 * 60,
                       "更强冷空气")])
        # 24h 前尚未收报:已知气象与预报一致
        before = provider.known_at(20 * 60)
        self.assertAlmostEqual(before.weather_at(20 * 60).tout_c,
                               forecast.weather_at(20 * 60).tout_c)
        # 到齐后补算:极寒窗口温度被修正
        observed = provider.fully_observed()
        self.assertEqual(observed.weather_at(20 * 60).tout_c, -17.0)

        initial = run_strategy(PreHeatBeforeColdWave(), forecast,
                               source=source, zones=zones)
        rerun = run_strategy(PreHeatBeforeColdWave(), observed,
                             source=source, zones=zones)
        # 补算结果可复现
        rerun2 = run_strategy(PreHeatBeforeColdWave(), provider.fully_observed(),
                              source=source, zones=zones)
        self.assertEqual(rerun.metrics, rerun2.metrics)
        # 补算重评估必须与预报版不同(更冷实况改变供热压力/舒适度评估)
        changed = (rerun.metrics["critical_comfort_deficit_Kh"]
                   != initial.metrics["critical_comfort_deficit_Kh"]
                   or rerun.metrics["source_capacity_hours"]
                   != initial.metrics["source_capacity_hours"])
        self.assertTrue(changed)


if __name__ == "__main__":
    unittest.main()
