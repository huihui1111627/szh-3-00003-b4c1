"""值守操作: 在指定时刻对系统施加的人工干预。"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Union


@dataclass
class Action:
    step: int                 # 生效步数
    kind: str                 # source_output / branch_flow / curtail
    target: str               # 分区名或 "source"
    value: float
    note: str = ""

    def describe(self) -> str:
        names = {"source_output": "热源出力", "branch_flow": "支路流量",
                 "curtail": "降供"}
        return (f"[step {self.step}] {names.get(self.kind, self.kind)}"
                f" -> {self.target} = {self.value} ({self.note})")


def source_output(step: int, kw: float, note: str = "") -> Action:
    return Action(step, "source_output", "source", kw, note)


def branch_flow(step: int, zone: str, factor: float, note: str = "") -> Action:
    return Action(step, "branch_flow", zone, factor, note)


def curtail(step: int, zone: str, factor: float, note: str = "") -> Action:
    return Action(step, "curtail", zone, factor, note)
