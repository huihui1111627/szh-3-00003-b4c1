"""调度策略接口。策略每个决策时刻可发出一个或多个 Action。"""
from __future__ import annotations

from typing import Dict, List, Optional

from .model import Action, Trace, WeatherProcess, ZoneDef, SourceDef


class Policy:
    name = "策略"
    description = ""
    decision_every_min = 10          # 决策周期

    def init(self, source: SourceDef, zones: Dict[str, ZoneDef]) -> None:
        self.source = source
        self.zones = zones

    def decide(self, minute: int, weather: WeatherProcess,
               trace: Trace) -> List[Action]:
        raise NotImplementedError


def _target_supply_from_curve(tout_eff_c: float,
                              t_design_out_c: float = -22.0) -> float:
    """质调节气候补偿曲线:室外越冷供水越热(85℃ 封顶,70℃ 起调)。"""
    x = max(0.0, min(1.0, (5.0 - tout_eff_c) / (5.0 - t_design_out_c)))
    return 76.0 + 18.0 * x
