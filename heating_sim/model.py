"""仿真数据模型。"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

CP = 4180.0          # 水的比热 J/(kg·K)
RHO = 980.0          # 高温水密度 kg/m^3

# 告警类型代码
A_PRESSURE_SURGE = "pressure_surge"   # 快速调节导致压力波动
A_TRET_LOW = "return_temp_low"        # 回水温度异常偏低(大温差/欠流偏冷)
A_TRET_HIGH = "return_temp_high"      # 回水温度异常偏高(小温差/过热)
A_ZONE_COLD = "zone_indoor_low"       # 区域室内持续低温
A_SUPPLY_HOT = "supply_temp_high"     # 供水温度超限
A_SOURCE_CAP = "source_capacity"      # 热源出力顶到上限仍欠供


@dataclass
class ZoneDef:
    """分区(二级网 + 建筑群体)定义。"""
    name: str
    critical: bool                 # 是否关键区域(医院、重点民生等)
    area_m2: float
    ua: float                      # 建筑总热损失系数 W/K
    capacitance: float            # 建筑等效热容 J/K
    radiator_k: float             # 散热器放热系数 W/K^n(按平均温差)
    radiator_n: float = 1.28
    nominal_load_w: float = 0.0   # 设计负荷 W
    hydraulic_r: float = 1.0      # 支路阻抗(相对值)
    pipe_len_m: float = 3000.0    # 一次网支管长度
    pipe_loss_k: float = 0.012    # 单程相对温降系数(1/km 量级)


@dataclass
class SourceDef:
    """热源定义。"""
    q_max_w: float
    ramp_w_per_min: float          # 出力爬坡速率 W/min
    t_supply_max_c: float
    pump_dp_pa: float              # 循环泵额定扬程(压差) Pa
    pump_rated_m3h: float          # 额定总流量 m3/h


@dataclass
class WeatherPoint:
    """一个气象时刻。"""
    minute: int
    tout_c: float
    wind_ms: float = 0.0

    def effective_tout(self) -> float:
        # 风速增大围护结构散热(等效降低室外温度)
        return self.tout_c - 0.35 * self.wind_ms


@dataclass
class WeatherProcess:
    """一次寒潮气象过程(分钟序列,线性插值)。"""
    points: List[WeatherPoint]
    late_annotations: Dict[int, str] = field(default_factory=dict)

    def weather_at(self, minute: int) -> WeatherPoint:
        pts = self.points
        if minute <= pts[0].minute:
            return pts[0]
        if minute >= pts[-1].minute:
            return pts[-1]
        for i in range(1, len(pts)):
            if minute <= pts[i].minute:
                a, b = pts[i - 1], pts[i]
                f = (minute - a.minute) / (b.minute - a.minute)
                return WeatherPoint(minute,
                                    a.tout_c + f * (b.tout_c - a.tout_c),
                                    a.wind_ms + f * (b.wind_ms - a.wind_ms))
        return pts[-1]

    @property
    def duration_min(self) -> int:
        return self.points[-1].minute - self.points[0].minute

    def clone(self) -> "WeatherProcess":
        return WeatherProcess([WeatherPoint(p.minute, p.tout_c, p.wind_ms)
                               for p in self.points], dict(self.late_annotations))


@dataclass
class Action:
    """值守人员的一次调度动作。

    - set_source_w: 直接设定热源目标出力(W);None 表示由策略曲线决定
    - set_source_t_c: 设定目标供水温度(℃),与出力二选一,优先级更高
    - branch_valve: {分区名: 开度 0..1.5(1=额定)}
    - curtail: {分区名: 限供系数 0..1}(0 不限,0.3=临时削减30%流量)
    """
    minute: int
    source: str
    reason: str = ""
    set_source_w: Optional[float] = None
    set_source_t_c: Optional[float] = None
    branch_valve: Dict[str, float] = field(default_factory=dict)
    curtail: Dict[str, float] = field(default_factory=dict)
    restore_after_min: Optional[int] = None   # 临时限供自动恢复时长


@dataclass
class Alarm:
    """仿真过程中触发的原始告警。"""
    minute: int
    code: str
    target: str
    message: str
    severity: str = "warn"          # info/warn/critical
    value: float = 0.0
    threshold: float = 0.0


@dataclass
class RootCause:
    """根因分析结论。"""
    alarm: Alarm
    chain: List[str]                # 自因到果的因果链(中文)
    responsible: List[str]          # 责任事件(动作/物理瓶颈)描述
    confidence: str = "high"        # high/medium/low


@dataclass
class ZoneTrace:
    indoor_c: List[float] = field(default_factory=list)
    tsupply_c: List[float] = field(default_factory=list)
    treturn_c: List[float] = field(default_factory=list)
    flow_m3h: List[float] = field(default_factory=list)
    valve: List[float] = field(default_factory=list)
    curtail: List[float] = field(default_factory=list)
    delivered_w: List[float] = field(default_factory=list)
    demand_w: List[float] = field(default_factory=list)


@dataclass
class Trace:
    """一次仿真运行的完整逐分钟轨迹。"""
    minutes: List[int] = field(default_factory=list)
    tout_c: List[float] = field(default_factory=list)
    tset_c: List[float] = field(default_factory=list)
    tsupply_c: List[float] = field(default_factory=list)
    treturn_mix_c: List[float] = field(default_factory=list)
    source_q_w: List[float] = field(default_factory=list)
    source_demand_w: List[float] = field(default_factory=list)
    total_flow_m3h: List[float] = field(default_factory=list)
    dp_station_pa: List[float] = field(default_factory=list)
    zones: Dict[str, ZoneTrace] = field(default_factory=dict)
    actions: List[Action] = field(default_factory=list)
    alarms: List[Alarm] = field(default_factory=list)

    def at(self, minute: int) -> int:
        return minute - self.minutes[0]


@dataclass
class RunResult:
    policy_name: str
    weather: WeatherProcess
    trace: Trace
    root_causes: List[RootCause] = field(default_factory=list)
    metrics: Dict[str, float] = field(default_factory=dict)
    rerun_note: str = ""
