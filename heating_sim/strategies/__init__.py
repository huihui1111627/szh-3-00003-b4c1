"""内置供热策略。"""
from .baseline import WeatherCompensation
from .preheat import PreHeatBeforeColdWave
from .aggressive import AggressiveFastRamp
from .curtail import PeakCurtail

ALL = [WeatherCompensation, PreHeatBeforeColdWave, AggressiveFastRamp, PeakCurtail]
