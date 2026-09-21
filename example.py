"""库 API 用法示例:多策略对照 + 人工干预动作 + 迟报补算。

运行: python3 example.py
"""
from heating_sim.compare import compare, run_strategy
from heating_sim.model import Action, Trace, WeatherProcess, WeatherPoint
from heating_sim.policies import Policy
from heating_sim.scenario import build_source, build_zones
from heating_sim.weather import LateReport, WeatherProvider, make_cold_wave


class ManualDispatch(Policy):
    """示例自定义策略:常规走补偿曲线,16h 临时限供园区 2 小时保医院。"""
    name = "人工值守干预"
    description = "峰值时临时限供非关键园区,2 小时后自动恢复"

    def decide(self, minute, weather: WeatherProcess, trace: Trace):
        from heating_sim.policies import _target_supply_from_curve
        w = weather.weather_at(minute).effective_tout()
        actions = [Action(minute, self.name, "按补偿曲线供水",
                          set_source_t_c=round(_target_supply_from_curve(w), 1))]
        if minute == 16 * 60:
            actions.append(Action(
                minute, self.name, "峰值保医院,第一步渐进压减园区负荷",
                curtail={"开发园区": 0.2}))
        elif minute == 16 * 60 + 10:
            actions.append(Action(
                minute, self.name, "第二步渐进压减至 40%,2 小时后恢复",
                curtail={"开发园区": 0.4}, restore_after_min=120))
        return actions


def main():
    source, zones = build_source(), build_zones()
    weather = make_cold_wave()

    # 1) 同一气象过程对照运行多个策略
    policy = ManualDispatch()
    results = compare([policy], weather, source=source, zones=zones)
    r = results[0]
    print(f"策略: {r.policy_name} — {policy.description}")
    print(f"  压力波动 {r.metrics['pressure_surge_count']:.0f} 次,"
          f" 关键区欠温 {r.metrics['critical_comfort_deficit_Kh']:.1f} K·h")
    for rc in r.root_causes:
        if rc.alarm.severity == "critical":
            print(f"  根因链[{rc.alarm.target}]: {' -> '.join(rc.chain[-2:])}")

    # 2) 迟报数据补算:更冷空气的报文 22h 才收到
    provider = WeatherProvider(weather, [
        LateReport(12 * 60, 30 * 60, "tout_c", -25.5, 22 * 60,
                   "实况比预报低约 3.5℃")])
    forecast_run = run_strategy(ManualDispatch(), weather, source=source, zones=zones)
    backfill = run_strategy(ManualDispatch(), provider.fully_observed(),
                            source=source, zones=zones)
    print("\n迟报补算前后关键区低温区时: "
          f"{forecast_run.metrics['critical_cold_zone_hours']:.1f} -> "
          f"{backfill.metrics['critical_cold_zone_hours']:.1f}")


if __name__ == "__main__":
    main()
