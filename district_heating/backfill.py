"""迟报数据补算与重新评估。

场景: 某时段的实测气象/操作数据延迟到达, 与仿真时使用的估计值不同。
流程:
1. 记录迟报修正(按生效步索引);
2. 回滚到修正点之前的最近检查点;
3. 用修正后的数据重放受影响时段(补算);
4. 对补算结果重新计算 KPI 与根因(重新评估)。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional

from .diagnosis import Finding, diagnose
from .simulation import Simulation, Telemetry, evaluate


@dataclass
class Correction:
    """一条迟报修正: 某步的室外温度实测值。"""
    step: int
    outdoor_temp: float
    wind_speed: Optional[float] = None


@dataclass
class BackfillResult:
    corrected_range: tuple
    old_kpis: Dict[str, float]
    new_kpis: Dict[str, float]
    new_findings: List[Finding]
    replayed_steps: int


def apply_corrections(sim: Simulation,
                      corrections: List[Correction],
                      controller=None) -> BackfillResult:
    """对迟报数据执行补算与重新评估。"""
    if not corrections:
        raise ValueError("无修正数据")
    first = min(c.step for c in corrections)
    last = max(c.step for c in corrections)

    # 旧指标(仅受影响时段)
    old_window = [r for r in sim.history if first <= r.step <= last]
    old_kpis = evaluate(old_window, sim.zones)

    # 回滚到 first 之前的最近检查点
    cp = max((s for s in sim._checkpoints if s <= first), default=None)
    if cp is None:
        raise RuntimeError("没有可用的检查点, 无法补算")
    sim.restore_checkpoint(cp)

    # 应用修正到气象过程(直接改写采样, 重放时生效)
    for c in corrections:
        sample = sim.weather._samples[c.step]
        sample.outdoor_temp = c.outdoor_temp
        if c.wind_speed is not None:
            sample.wind_speed = c.wind_speed

    # 重放: 从检查点跑到原历史末尾
    end_step = old_window[-1].step if old_window else last
    sim.run(n_steps=end_step + 1, controller=controller)

    new_window = [r for r in sim.history if first <= r.step <= last]
    design_flows = {n: b.design_flow_kg_s
                    for n, b in sim.network.branches.items()}
    return BackfillResult(
        corrected_range=(first, last),
        old_kpis=old_kpis,
        new_kpis=evaluate(new_window, sim.zones),
        new_findings=diagnose(new_window, sim.zones, sim.actions, design_flows),
        replayed_steps=end_step + 1 - cp,
    )
