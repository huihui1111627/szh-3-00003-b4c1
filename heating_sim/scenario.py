"""典型城市供热场景:1 个热源 + 4 个并联分区,管长不同(输运延迟不同)。"""
from __future__ import annotations

from .model import SourceDef, ZoneDef

CP, RHO = 4180.0, 980.0


def _zone(name, critical, qnom_mw, tau_h, pipe_len, r):
    # UA 按实际设计供热量标定:设计供回 95/60(35K)、设计流量,保证设计点供需相等
    g = qnom_mw * 1e6 / (CP * 35.0 * RHO) * 3600.0
    ua = qnom_mw * 1e6 / 40.0                      # 室内外温差 18-(-22)=40K
    cap = ua * tau_h * 3600.0
    mean_excess = (95.0 + 55.0) / 2.0 - 18.0
    kr = qnom_mw * 1e6 / mean_excess ** 1.28
    return ZoneDef(name=name, critical=critical, area_m2=qnom_mw * 50_000,
                   ua=ua, capacitance=cap, radiator_k=kr, radiator_n=1.28,
                   nominal_load_w=qnom_mw * 1e6, hydraulic_r=r,
                   pipe_len_m=pipe_len,
                   pipe_loss_k=0.006)


def build_zones():
    # R_i 按额定流量、600kPa 工况反算(各分区阻抗不同,近端小、远端大)
    dp = 1_000_000.0
    def r_of(qnom_mw):
        g = qnom_mw * 1e6 / (CP * 35.0 * RHO) * 3600.0
        return dp / (g * g)

    return {
        "医院片区": _zone("医院片区", True, 8.0, 36, 1500, r_of(8.0)),
        "住宅片区": _zone("住宅片区", True, 14.0, 30, 2500, r_of(14.0)),
        "商业片区": _zone("商业片区", False, 9.0, 12, 3500, r_of(9.0)),
        "开发园区": _zone("开发园区", False, 11.0, 8, 6000, r_of(11.0)),
    }


def build_source(q_max_mw: float = 42.0):
    total_nom_m3h = sum(
        z.nominal_load_w / (CP * 35.0 * RHO) * 3600.0
        for z in build_zones().values())
    return SourceDef(q_max_w=q_max_mw * 1e6,
                     ramp_w_per_min=0.8e6,
                     t_supply_max_c=96.0,
                     pump_dp_pa=1.0e6,
                     pump_rated_m3h=total_nom_m3h)
