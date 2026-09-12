#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""TDLAS/WMS 仿真 MCP 服务器（stdio JSON-RPC 2024-11-05）。

把 tdlas_sim 的仿真能力暴露为 AI 可直接调用的工具：
  · tdlas_simulate         DAS + 免标定 WMS 正向仿真（1f/2f 谐波、峰高、可选 PNG）
  · tdlas_invert           免标定浓度反演（2f/1f 峰高 → 摩尔分数）
  · tdlas_detection_limit  检测极限 LOD（等效透过率噪声 σ_τ → NEC / LOD）
  · tdlas_selftest         全链路自检

纪律（沿用 hitran-mcp）：纯标准库 stdio JSON-RPC；HITRAN 取数由本仓库提供（HAPI 1.x，免 key）；
所有 print 收进 log 字段，绝不污染 stdout 协议流。

自测：python tools/tdlas_mcp.py --selftest
"""
from __future__ import annotations

import contextlib
import io
import json
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

import tdlas_sim as ts  # noqa: E402

PROTOCOL_VERSION = "2024-11-05"
SERVER_INFO = {"name": "tdlas", "version": "0.2.0",
               "protocolVersion": PROTOCOL_VERSION,
               "capabilities": {"tools": {}}}
OUT_DIR = _ROOT / "tmp" / "mcp_out"

# 技术知识：随工具返回，供 AI 向用户解释真实实验要点（而非只给数字）
EDGE_TECH_NOTE = (
    "真实实验中三角波上升沿与下降沿常不重合，成因："
    "① 激光调谐非线性（注入电流→波长响应并非完美线性）；"
    "② 扫描期间激光器热漂移（波长与功率随时间漂移）；"
    "③ 探测器与前置放大器带宽有限 → 上升/下降沿的相位滞后不同；"
    "④ 电流源上升/下降沿的扫频响应差异。"
    "处理建议：先用 edge='both' 对照两段；若重合约，任取一段即可；若不重合，取较干净的一段，"
    "或用 edge='average' 两段平均（随机噪声按 √2 改善，但系统性偏差不会被平均抵消）。"
)

# 参数索取指南：缺省参数的索取优先级 ——
# ① 用户提供的实测标定值 → ② 用户提供的器件型号（由 AI 检索规格书）
# → ③ 引导用户现场测量（如"驱动电压变化 ΔV → 波数变化 Δν"）
# → ④ 保留内置默认值并在返回中明确标注（assumptions）
PARAM_ACQ_GUIDE = {
    "amp_V": ("三角波幅值", "V", "决定扫描波数半宽 Δν = amp_V·|η_VI·dν/dI|"),
    "offset_V": ("三角波偏置", "V", "决定扫描中心波数（即落在哪条吸收线上）"),
    "freq_Hz": ("三角波频率", "Hz", "扫描速率；与采样率共同决定每周期采样点数"),
    "phase_deg": ("三角波相位", "°", "起始相位（默认中心对称、先上升后下降）"),
    "eta_VI": ("驱动器跨导 η_VI", "mA/V", "电压—电流转换，取决于驱动器电路"),
    "dnu_dI": ("激光器调谐系数 dν/dI", "cm⁻¹/mA", "波数标定核心；给出激光器型号可查规格书"),
    "eta_IP": ("功率斜率效率", "mW/mA", "决定光功率量级，进而决定 PD 电压与 ADC 量程占用"),
    "wn_ref": ("参考波数", "cm⁻¹", "调谐基准点（通常取规格书中心波长）"),
    "i_ref": ("参考电流", "mA", "与参考波数配对的工作点电流"),
    "i_th": ("阈值电流", "mA", "低于此电流无激光输出"),
    "fs": ("采集卡采样率", "Hz", "NI USB-6211 上限 250 kS/s"),
    "adc_bits": ("ADC 位数", "bit", "决定量化噪声与动态范围（USB-6211：16-bit）"),
    "v_range": ("ADC 输入量程", "V", "决定满量程与饱和阈值"),
    "resp": ("PD 响应度 R", "A/W", "光电转换效率（InGaAs 典型 0.9 A/W）"),
    "gain": ("PD 跨阻增益 G", "V/A", "决定 PD 输出电压；过高会导致 ADC 饱和"),
    "bw": ("PD 带宽", "Hz", "与噪声带宽、可解调的最高调制频率相关"),
    "rin": ("激光相对强度噪声 RIN", "1/√Hz", "常为 TDLAS 系统的主导噪声源"),
    "throughput": ("光学元件总透过率", "—", "窗片 / 镜片 / 光纤耦合损耗"),
    "mod_freq_Hz": ("正弦调制频率 fm", "Hz", "把信号搬到高频以规避 1/f 噪声与激光 RIN"),
    "mod_amp_V": ("正弦调制幅值", "V", "决定调制系数 m=a/HWHM；m≈2.2 时 2f 峰值最大"),
    "m_opt": ("目标调制系数 m", "—", "2f 灵敏度最优的调制深度/线宽比，经典值 2.2"),
    "lockin_avg": ("锁相平均周期数", "—", "正交解调后低通平均的调制周期数，越大越平滑"),
}

# ★ AI 主动指导协议：本 MCP 面向**实验新手**，AI 必须主动引导而非被动等参数。
# 每次涉及真实器件的仿真，按此顺序与用户交互（用专业术语，但首次出现给白话解释）。
AI_INTERACTION_GUIDE = {
    "role": "你是 TDLAS 实验设计助手，服务对象多为实验新手。职责是**主动指导**，不是被动等参数。"
            "凡信息不足必须主动索取，绝不能默默用默认值出结果。",
    "priority": [
        "① 先要实测标定值（最可靠）；",
        "② 用户给不出值、但能给**器件型号** → AI 自行检索该型号规格书提取参数；",
        "③ 型号也没有 → 引导用户**现场标定**（例：改变驱动电压 ΔV，记录波数变化 Δν，得 dν/dV）；",
        "④ 以上都做不到 → 用内置默认值，但**必须在结论中显式标注**「以下参数用了默认值 X」。",
    ],
    "workflow": [
        "第 1 步｜确认场景：测什么分子、什么波段、什么工况（T/P/浓度量级/光程）。"
        "用户不确定时主动给推荐（CH4 → 3.3 μm 或 1.65 μm；CO → 2.3 μm；CO2 → 2.0 μm）。",
        "第 2 步｜分组索取参数，每次只问一组，并说明该参数控制什么物理量（见 PARAM_ACQ_GUIDE 的 why）。",
        "第 3 步｜拿到结果后做**合理性体检**并主动告知：αL 是否在弱吸收区、ADC 是否饱和、"
        "噪声主导来源（散粒/热/RIN）、检测限量级、调制系数 m 是否接近 2.2。",
        "第 4 步｜若返回值中 warnings / param_requests 非空，**先向用户澄清再解读结论**，"
        "不要拿不合理参数直接下结论。",
    ],
    "glossary": {
        "αL": "吸光度（吸收系数×光程），无量纲；≪1 才算弱吸收，DAS/2f 才与浓度成正比",
        "HWHM": "吸收线半高半宽（cm⁻¹），决定最优调制深度",
        "m": "调制系数 = 调制深度 a ÷ HWHM，最优约 2.2",
        "fm": "正弦调制频率（把信号搬到高频，避开低频噪声）",
        "fscan": "三角波扫描频率（扫过整条吸收线）",
        "RIN": "激光相对强度噪声，常为 TDLAS 主导噪声源",
        "2f/1f": "用 1f 谐波归一化 2f，抵消光强波动；弱吸收下正比于浓度（免标定核心）",
        "LOD": "检测限（最小可测浓度）",
        "m 优化": "工具会自动按 m≈2.2 反算正弦调制幅值；如需手动指定可用 mod_amp_V",
    },
}


@contextlib.contextmanager
def _quiet():
    """把 HITRAN/HAPI 的刷屏收进缓冲区，不污染 stdout 协议流。"""
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        yield buf


def _laser(i0, i2, psi1_pi, psi2_pi):
    """激光强度调制参数：相位以 π 为单位传入，内部转弧度。"""
    import numpy as np
    return float(i0), float(i2), float(psi1_pi) * np.pi, float(psi2_pi) * np.pi


def _common(species, wn0, T, P, x, L, a, i0, i2, psi1_pi, psi2_pi, span):
    if not (species and str(species).strip()):
        raise ValueError("species 不能为空")
    if not 0.0 < float(x) <= 1.0:
        raise ValueError(f"mole_frac 必须在 (0, 1]，收到 {x}")
    if float(a) <= 0:
        raise ValueError(f"调制深度 a 必须 > 0，收到 {a}")
    if float(L) <= 0:
        raise ValueError(f"光程 L 必须 > 0，收到 {L}")
    return _laser(i0, i2, psi1_pi, psi2_pi)


# ───────────────────────── 工具 ─────────────────────────

def t_simulate(species="H2O", wn0=7185.596, T=296.0, P=1.0, x=0.1, L=30.0,
               a=0.10, i0=0.1671, i2=2.48e-3, psi1_pi=1.9356, psi2_pi=4.4138,
               span=0.8, n_scan=401, n_mod=96, sigma_tau=0.0, seed=None,
               save_png=False):
    """DAS + 免标定 WMS 正向仿真：返回 1f/2f 峰高、DAS 最小透过率、线表信息。

    wn0 为目标线中心（cm^-1）；x 为摩尔分数；L 为光程 cm；a 为调制深度 cm^-1。
    sigma_tau 为等效透过率噪声（默认 0=理想无噪）。save_png=True 时同时出图。
    """
    i0, i2, p1, p2 = _common(species, wn0, T, P, x, L, a, i0, i2, psi1_pi, psi2_pi, span)
    with _quiet() as g:
        r = ts.simulate(species, wn0, float(T), float(P), float(x), float(L), float(a),
                        i0, i2, p1, p2, float(span), int(n_scan), int(n_mod),
                        sigma_tau=float(sigma_tau), seed=seed)
    k2 = int(r["S2f"].argmax())
    out = {"species": str(species).upper(), "wn0_cm-1": float(wn0), "T_K": float(T),
           "P_atm": float(P), "mole_frac": float(x), "path_cm": float(L),
           "mod_depth_cm-1": float(a),
           "alpha_peak_cm-1": float(r["alpha"].max()),
           "das_tau_min": float(r["tau"].min()),
           "wms_1f_peak": float(r["S1f"].max()),
           "wms_2f_peak": float(r["S2f"].max()),
           "wms_2f_peak_nu_cm-1": float(r["wn_scan"][k2]),
           "wms_2f1f_peak": float(r["S2f1f"].max()),
           "n_lines_in_window": r["meta"]["n_lines_in_window"],
           "table": r["meta"]["table"],
           "log": g.getvalue().splitlines()}
    if save_png:
        OUT_DIR.mkdir(parents=True, exist_ok=True)
        png = OUT_DIR / f"tdlas_{str(species).upper()}_{float(wn0):g}cm-1_x{float(x):g}.png"
        with _quiet():
            ts.plot(r, png)
        out["png"] = str(png)
    return out


def t_invert(peak_2f1f, species="H2O", wn0=7185.596, T=296.0, P=1.0, L=30.0,
             a=0.10, x_ref=1e-3, i0=0.1671, i2=2.48e-3,
             psi1_pi=1.9356, psi2_pi=4.4138, span=0.8):
    """免标定浓度反演：由测得的 2f/1f 峰高求摩尔分数。

    用参考浓度 x_ref 的仿真定出单位浓度灵敏度 k，再 x = peak_2f1f / k。
    **仅适用弱吸收（αL ≪ 1）**：吸收过强时 2f/1f 呈超线性，反演结果偏高。
    建议先用 tdlas_simulate 核验 alpha_peak × L；若 αL 超过约 0.05 需改用完整仿真迭代反演。
    """
    i0, i2, p1, p2 = _common(species, wn0, T, P, x_ref, L, a, i0, i2, psi1_pi, psi2_pi, span)
    with _quiet() as g:
        k = ts.sensitivity_2f1f(species, float(wn0), float(T), float(P), float(L), float(a),
                                x_ref=float(x_ref), i0=i0, i2=i2, psi1=p1, psi2=p2,
                                span=float(span))
        x_est = ts.invert_concentration(float(peak_2f1f), k)
    return {"species": str(species).upper(), "mole_frac": float(x_est),
            "peak_2f1f_in": float(peak_2f1f), "sensitivity_k": float(k),
            "x_ref": float(x_ref), "T_K": float(T), "P_atm": float(P),
            "path_cm": float(L), "log": g.getvalue().splitlines()}


def t_detection_limit(species="H2O", wn0=7185.596, T=296.0, P=1.0, L=30.0,
                      a=0.10, sigma_tau=1e-5, x_true=1e-3, n_trials=30,
                      n_sigma=3.0, seed=0, i0=0.1671, i2=2.48e-3,
                      psi1_pi=1.9356, psi2_pi=4.4138, span=0.8):
    """检测极限 LOD（最小可测摩尔分数）—— 蒙特卡洛。

    sigma_tau 为等效透过率噪声（RIN / 散粒 / 探测器 / 电路的综合，工程上由无吸收基线实测给出）。
    返回噪声等效浓度 NEC 与 LOD = n_sigma × NEC。
    """
    i0, i2, p1, p2 = _common(species, wn0, T, P, x_true, L, a, i0, i2, psi1_pi, psi2_pi, span)
    with _quiet() as g:
        dl = ts.detection_limit(species, float(wn0), float(T), float(P), float(L), float(a),
                                sigma_tau=float(sigma_tau), x_true=float(x_true),
                                n_trials=int(n_trials), n_sigma=float(n_sigma), seed=int(seed),
                                i0=i0, i2=i2, psi1=p1, psi2=p2, span=float(span))
    return {"species": str(species).upper(), "T_K": float(T), "P_atm": float(P),
            "path_cm": float(L), "sigma_tau": float(sigma_tau),
            "x_true": float(x_true), "NEC": float(dl["NEC"]), "LOD": float(dl["LOD"]),
            "n_sigma": float(n_sigma), "n_trials": int(dl["n_trials"]),
            "sensitivity_k": float(dl["k"]), "log": g.getvalue().splitlines()}


def t_das_chain(species="H2O", wn0=7185.596, T=None, P=None, x=None, L=None,
                span=None, fscan=None, fs=None, baseline_slope=None,
                sigma=0.0, seed=None, fit_order=None, fit_frac=None,
                edge=None, save_png=False):
    """三角波扫描 DAS 全链路：PD 原始信号 → 多项式基线拟合扣除 → DAS 吸光度信号。

    默认工况（未显式给出时使用，并列入 assumptions 供复核）：
        **1000 ppm（x=1e-3）、0.5 m 光程（L=50 cm）、296 K、1 atm、上升沿（edge="rising"）**。
    技术知识见返回中的 edge_note（上升/下降沿为何不重合、如何取舍）。
    """
    _DEF = {"T": 296.0, "P": 1.01325, "x": 1e-3, "L": 50.0, "span": 0.8,
            "fscan": 100.0, "fs": 5e5, "baseline_slope": 0.05,
            "fit_order": 3, "fit_frac": 0.3, "edge": "rising"}
    given = {"T": T, "P": P, "x": x, "L": L, "span": span, "fscan": fscan, "fs": fs,
             "baseline_slope": baseline_slope, "fit_order": fit_order,
             "fit_frac": fit_frac, "edge": edge}
    assumed = [f"{k}={v}" for k, v in _DEF.items() if given[k] is None]
    T = _DEF["T"] if T is None else float(T)
    P = _DEF["P"] if P is None else float(P)
    x = _DEF["x"] if x is None else float(x)
    L = _DEF["L"] if L is None else float(L)
    span = _DEF["span"] if span is None else float(span)
    fscan = _DEF["fscan"] if fscan is None else float(fscan)
    fs = _DEF["fs"] if fs is None else float(fs)
    baseline_slope = _DEF["baseline_slope"] if baseline_slope is None else float(baseline_slope)
    fit_order = _DEF["fit_order"] if fit_order is None else int(fit_order)
    fit_frac = _DEF["fit_frac"] if fit_frac is None else float(fit_frac)
    edge = _DEF["edge"] if edge is None else str(edge)

    if not (species and str(species).strip()):
        raise ValueError("species 不能为空")
    if not 0.0 < x <= 1.0:
        raise ValueError(f"mole_frac 必须在 (0, 1]，收到 {x}")
    if L <= 0:
        raise ValueError(f"光程 L 必须 > 0（cm），收到 {L}")
    if fscan <= 0 or fs <= 0:
        raise ValueError("fscan / fs 必须 > 0")

    with _quiet() as g:
        r = ts.simulate_das_td(species, float(wn0), T, P, x, L,
                               span=span, fscan=fscan, fs=fs,
                               baseline_slope=baseline_slope, sigma=float(sigma),
                               seed=seed, fit_order=fit_order, fit_frac=fit_frac, edge=edge)
    tp = float(r["alpha_L_true"].max())
    dp = float(r["das"].max())
    out = {"species": str(species).upper(), "wn0_cm-1": float(wn0), "T_K": T, "P_atm": P,
           "mole_frac": x, "mole_ppm": x * 1e6, "path_cm": L, "path_m": L / 100.0,
           "span_cm-1": span, "fscan_Hz": fscan, "fs_Hz": fs, "edge": edge,
           "baseline_slope": baseline_slope, "sigma_tau": float(sigma),
           "fit_order": fit_order, "fit_frac": fit_frac,
           "alpha_L_peak": float(r["meta"]["alpha_L_peak"]),
           "das_absorbance_peak": dp, "alpha_L_true_peak": tp,
           "error_pct": (abs(dp - tp) / tp * 100.0) if tp > 0 else None,
           "n_lines_in_window": r["meta"]["n_lines_in_window"],
           "assumptions": assumed,
           "needs_confirm": bool(assumed),
           "confirm_note": "assumptions 为本次被迫使用的默认工况；若与用户描述不符，"
                           "请先向用户确认（尤其浓度 / 光程 / 温度 / 取哪一段沿），再解读结果。",
           "warnings": r["meta"]["warnings"],
           "edge_note": EDGE_TECH_NOTE,
           "log": g.getvalue().splitlines()}
    if save_png:
        OUT_DIR.mkdir(parents=True, exist_ok=True)
        png = OUT_DIR / f"das_chain_{str(species).upper()}_{float(wn0):g}cm-1_x{x:g}.png"
        with _quiet():
            ts.plot_das_td(r, png)
        out["png"] = str(png)
    return out


_INSTR_KEYS = ("amp_V", "freq_Hz", "offset_V", "phase_deg", "eta_VI", "dnu_dI",
               "wn_ref", "i_ref", "i_th", "eta_IP", "fs", "n_samples", "adc_bits",
               "v_range", "throughput", "resp", "gain", "bw", "rin")
_SCENE_DEFAULTS = {"T": 296.0, "P": 1.01325, "x": 1e-3, "L_cm": 50.0,
                   "edge": "rising", "fit_order": 3, "fit_frac": 0.3}


def t_das_instrument(species="CH4", wn_center=2968.5, T=None, P=None, x=None, L_cm=None,
                     edge=None, fit_order=None, fit_frac=None, seed=None,
                     save_png=False, **kw):
    """仪器系统级 DAS 仿真：DAQ 电压 → 激光 → 光路 → PD → ADC 量化 → 基线扣除。

    缺省参数按优先级索取：① 用户实测标定值 → ② 器件型号（AI 检索规格书）
    → ③ 引导用户现场测量 → ④ 内置默认（NI USB-6211 + 典型中红外 DFB 激光器）。
    返回 assumptions / param_requests / warnings，供 AI 主动向用户澄清后再解读结论。
    """
    given_scene = {"T": T, "P": P, "x": x, "L_cm": L_cm, "edge": edge,
                   "fit_order": fit_order, "fit_frac": fit_frac}
    scene = {k: (_SCENE_DEFAULTS[k] if v is None else v) for k, v in given_scene.items()}
    assumed = [k for k, v in given_scene.items() if v is None]

    given_inst = {k: kw.get(k) for k in _INSTR_KEYS}
    assumed += [k for k, v in given_inst.items() if v is None]
    inst = {k: v for k, v in given_inst.items() if v is not None}

    if not (species and str(species).strip()):
        raise ValueError("species 不能为空")

    with _quiet() as g:
        r = ts.simulate_das_instrument(species, wn_center=float(wn_center),
                                       T=float(scene["T"]), P=float(scene["P"]),
                                       x=float(scene["x"]), L_cm=float(scene["L_cm"]),
                                       edge=scene["edge"], fit_order=int(scene["fit_order"]),
                                       fit_frac=float(scene["fit_frac"]), seed=seed, **inst)
    m = r["meta"]
    cfg = m["cfg"]

    requests = []
    for k in assumed:
        if k in PARAM_ACQ_GUIDE:
            cn, unit, why = PARAM_ACQ_GUIDE[k]
            requests.append({"param": k, "cn": cn, "unit": unit, "why": why,
                             "value_used": cfg.get(k, _SCENE_DEFAULTS.get(k)),
                             "how": "① 用户实测值 ② 器件型号（AI 检索规格书）"
                                    "③ 引导现场标定 ④ 保留默认并注明"})

    out = {"species": str(species).upper(), "wn_center_cm-1": float(wn_center),
           "scan_window_cm-1": [round(m["scan_lo"], 4), round(m["scan_hi"], 4)],
           "scan_half_span_cm-1": round(m["span_cm-1"], 4),
           "tri_wave": {k: cfg[k] for k in ("amp_V", "freq_Hz", "offset_V", "phase_deg")},
           "laser": {k: cfg[k] for k in ("eta_VI", "dnu_dI", "wn_ref", "i_ref", "i_th", "eta_IP")},
           "pd": {k: cfg[k] for k in ("resp", "gain", "bw", "rin", "throughput")},
           "adc": {"fs_Hz": cfg["fs"], "n_samples": cfg["n_samples"],
                   "bits": cfg["adc_bits"], "v_range_V": cfg["v_range"], "lsb_V": m["lsb_V"]},
           "results": {"alpha_L_peak": m["alpha_L_peak"],
                       "v_pd_mean_V": m["v_pd_mean"],
                       "saturated_points": m["n_sat"],
                       "noise_rms_mV": m["sigma_v_noise"] * 1e3,
                       "noise_breakdown_mV": {"shot": m["sigma_shot"] * 1e3,
                                              "thermal": m["sigma_thermal"] * 1e3,
                                              "rin": m["sigma_rin"] * 1e3},
                       "n_lines_in_window": m["n_lines_in_window"], "table": m["table"]},
           "assumptions": assumed,
           "param_requests": requests,
           "needs_input": bool(requests),
           "confirm_note": "param_requests 为缺省参数：请优先向用户索取实测值或器件型号；"
                           "必要时引导现场标定；保留默认时须在结论中明确注明。",
           "warnings": m["warnings"],
           "edge_note": EDGE_TECH_NOTE,
           "log": g.getvalue().splitlines()}
    if save_png:
        OUT_DIR.mkdir(parents=True, exist_ok=True)
        png = OUT_DIR / f"das_instr_{str(species).upper()}_{float(wn_center):g}cm-1.png"
        with _quiet():
            ts.plot_das_instrument(r, png)
        out["png"] = str(png)
    return out


_INSTR_KEYS_WMS = _INSTR_KEYS + ("mod_freq_Hz", "mod_amp_V", "m_opt", "lockin_avg")


def t_wms_instrument(species="CH4", wn_center=2968.5, T=None, P=None, x=None, L_cm=None,
                     seed=None, save_png=False, **kw):
    """WMS 仪器链路仿真：三角波扫描 + 正弦调制 → 激光 → 光路 → PD → ADC → 数字锁相（2f/1f）。

    调制幅值默认按**最优调制系数 m≈2.2** 自动优化（2f 峰值最大处）。
    未给出的参数列入 assumptions / param_requests；AI 须按 AI_INTERACTION_GUIDE 处理并显式标注。
    """
    import numpy as np
    given_scene = {"T": T, "P": P, "x": x, "L_cm": L_cm}
    scene = {k: (_SCENE_DEFAULTS[k] if v is None else v) for k, v in given_scene.items()}
    assumed = [k for k, v in given_scene.items() if v is None]

    given_inst = {k: kw.get(k) for k in _INSTR_KEYS_WMS}
    assumed += [k for k, v in given_inst.items() if v is None]
    inst = {k: v for k, v in given_inst.items() if v is not None}
    if not (species and str(species).strip()):
        raise ValueError("species 不能为空")

    with _quiet() as g:
        r = ts.simulate_wms_instrument(species, wn_center=float(wn_center),
                                       T=float(scene["T"]), P=float(scene["P"]),
                                       x=float(scene["x"]), L_cm=float(scene["L_cm"]),
                                       seed=seed, **inst)
    m = r["meta"]
    cfg = m["cfg"]
    s2f1f = np.abs(r["S2f1f_cyc"])
    kpk = int(s2f1f.argmax())

    requests = []
    for k in assumed:
        if k in PARAM_ACQ_GUIDE:
            cn, unit, why = PARAM_ACQ_GUIDE[k]
            requests.append({"param": k, "cn": cn, "unit": unit, "why": why,
                             "value_used": cfg.get(k, _SCENE_DEFAULTS.get(k)),
                             "how": "① 实测值 ② 器件型号（AI 检索规格书）③ 引导现场标定 ④ 保留默认并注明"})

    out = {"species": str(species).upper(), "wn_center_cm-1": float(wn_center),
           "scan_window_cm-1": [round(float(r["nu_axis"].min()), 4),
                                round(float(r["nu_axis"].max()), 4)],
           "wms": {"fscan_Hz": m["fscan_Hz"], "mod_freq_Hz": m["mod_freq_Hz"],
                   "mod_amp_V": m["mod_amp_V"], "auto_optimized": m["auto_mod"],
                   "mod_depth_cm-1": m["mod_depth_cm-1"], "HWHM_cm-1": m["hwhm_cm-1"],
                   "mod_coeff_m": m["mod_coeff_m"]},
           "adc": {"fs_Hz": m["fs"], "n_per_scan": r["n_per"], "bits": cfg["adc_bits"],
                   "v_range_V": cfg["v_range"], "lsb_V": m["lsb_V"]},
           "results": {"alpha_L_peak": m["alpha_L_peak"],
                       "S2f1f_peak": float(s2f1f[kpk]),
                       "S2f1f_peak_nu_cm-1": float(r["nu_axis"][kpk]),
                       "v_pd_mean_V": m["v_pd_mean"], "saturated_points": m["n_sat"],
                       "noise_breakdown_mV": {"shot": m["sigma_shot"] * 1e3,
                                              "thermal": m["sigma_thermal"] * 1e3,
                                              "rin": m["sigma_rin"] * 1e3},
                       "n_lines_in_window": m["n_lines_in_window"], "table": m["table"]},
           "assumptions": assumed, "param_requests": requests,
           "needs_input": bool(requests),
           "ai_guidance": {"role": AI_INTERACTION_GUIDE["role"],
                           "priority": AI_INTERACTION_GUIDE["priority"],
                           "next_step": "若 param_requests 非空：先向用户索取实测值或器件型号；"
                                        "保留默认时须在结论中标注。"},
           "warnings": m["warnings"], "edge_note": EDGE_TECH_NOTE,
           "log": g.getvalue().splitlines()}
    if save_png:
        OUT_DIR.mkdir(parents=True, exist_ok=True)
        png = OUT_DIR / f"wms_instr_{str(species).upper()}_{float(wn_center):g}cm-1.png"
        with _quiet():
            ts.plot_wms_instrument(r, png)
        out["png"] = str(png)
    return out


def t_guide(topic=None):
    """返回 AI 主动指导协议（面向实验新手）：参数索取优先级、交互流程、术语表、参数索取指南。"""
    out = dict(AI_INTERACTION_GUIDE)
    out["param_guide"] = {k: {"cn": v[0], "unit": v[1], "why": v[2]}
                          for k, v in PARAM_ACQ_GUIDE.items()}
    if topic:
        t = str(topic).upper()
        out["glossary_hit"] = {k: v for k, v in AI_INTERACTION_GUIDE["glossary"].items()
                               if t in k.upper() or t in str(v).upper()}
    return out


def t_selftest():
    """全链路自检（有线 / 2f 形状 / 弱场线性 / 反演闭环 / 检测限 / 时域交叉验证 / DAS 链路）。"""
    with _quiet() as g:
        ts.selftest()
    return {"ok": True, "log": g.getvalue().splitlines()}


DISPATCH = {"tdlas_simulate": t_simulate,
            "tdlas_das_chain": t_das_chain,
            "tdlas_das_instrument": t_das_instrument,
            "tdlas_wms_instrument": t_wms_instrument,
            "tdlas_guide": t_guide,
            "tdlas_invert": t_invert,
            "tdlas_detection_limit": t_detection_limit,
            "tdlas_selftest": t_selftest}

TOOLS = [
    {"name": "tdlas_simulate",
     "description": "TDLAS/WMS 正向仿真：给定分子、目标线、工况与调制参数，返回 DAS 透过率、"
                    "1f/2f 谐波峰高与线表信息。数据经 HAPI 实时取自 HITRAN（免 API key）。"
                    "未说明 T/P/浓度/光程时应先向用户确认。",
     "inputSchema": {"type": "object",
                     "properties": {
                         "species": {"type": "string", "description": "分子式，如 H2O / CO2 / CO"},
                         "wn0": {"type": "number", "description": "目标线中心 cm^-1"},
                         "T": {"type": "number", "description": "温度 K，默认 296"},
                         "P": {"type": "number", "description": "气压 atm，默认 1.01325"},
                         "x": {"type": "number", "description": "摩尔分数 (0,1]，默认 0.1"},
                         "L": {"type": "number", "description": "光程 cm，默认 30"},
                         "a": {"type": "number", "description": "调制深度 cm^-1，默认 0.1"},
                         "i0": {"type": "number"}, "i2": {"type": "number"},
                         "psi1_pi": {"type": "number"}, "psi2_pi": {"type": "number"},
                         "span": {"type": "number", "description": "扫描半宽 cm^-1，默认 0.8"},
                         "n_scan": {"type": "integer"}, "n_mod": {"type": "integer"},
                         "sigma_tau": {"type": "number", "description": "等效透过率噪声，默认 0"},
                         "seed": {"type": "integer"},
                         "save_png": {"type": "boolean", "description": "是否出图，默认 False"}},
                     "required": ["species", "wn0"]}},
    {"name": "tdlas_das_chain",
     "description": "三角波扫描 DAS 全链路仿真：PD 原始信号 It(t) → 多项式基线拟合扣除 → DAS 吸光度信号。"
                    "对应真实 DAS 实验处理流程。默认工况：1000 ppm / 0.5 m 光程 / 296 K / 1 atm / "
                    "上升沿；未给出的参数会列入返回的 assumptions，需先向用户复核。"
                    "技术知识：真实实验中三角波上升沿与下降沿常不重合（激光调谐非线性、扫描期热漂移、"
                    "探测器带宽引起的相位滞后）——详见返回的 edge_note。",
     "inputSchema": {"type": "object",
                     "properties": {
                         "species": {"type": "string", "description": "分子式，如 CH4 / H2O / CO2"},
                         "wn0": {"type": "number", "description": "扫描中心波数 cm^-1"},
                         "T": {"type": "number", "description": "温度 K，默认 296"},
                         "P": {"type": "number", "description": "气压 atm，默认 1.01325"},
                         "x": {"type": "number", "description": "摩尔分数，默认 1e-3（1000 ppm）"},
                         "L": {"type": "number", "description": "光程 cm，默认 50（0.5 m）"},
                         "span": {"type": "number", "description": "扫描半宽 cm^-1，默认 0.8"},
                         "fscan": {"type": "number", "description": "三角波频率 Hz，默认 100"},
                         "fs": {"type": "number", "description": "采样率 Hz，默认 5e5"},
                         "baseline_slope": {"type": "number", "description": "I0 基线斜率，默认 0.05"},
                         "sigma": {"type": "number", "description": "等效透过率噪声，默认 0"},
                         "seed": {"type": "integer"},
                         "fit_order": {"type": "integer",
                                       "description": "基线多项式阶数，默认 3（过高会过拟合、低估吸收）"},
                         "fit_frac": {"type": "number", "description": "无吸收区占比，默认 0.3"},
                         "edge": {"type": "string",
                                  "description": "取哪一段作 DAS：rising(默认,上升沿) / falling / "
                                                 "average(两段平均) / both(对照两段)"},
                         "save_png": {"type": "boolean",
                                      "description": "是否出四层链路图（含上升/下降沿对照）"}},
                     "required": ["species", "wn0"]}},
    {"name": "tdlas_das_instrument",
     "description": "仪器系统级 DAS 仿真：DAQ 输出电压 → 激光器调谐（V→I→波数/功率）→ 光路损耗与吸收 "
                    "→ PD 光电转换（响应度/跨阻增益/带宽/噪声）→ ADC 量化（位数/量程/饱和）→ 基线扣除。"
                    "默认器件：NI USB-6211 + 典型中红外 DFB（V→I 24 mA/V、dν/dI −0.088 cm⁻¹/mA、"
                    "中心 2964.7 cm⁻¹）+ CH4 @2968.5 cm⁻¹。"
                    "**缺省参数的索取优先级：① 用户实测值 → ② 器件型号（AI 检索规格书）→ "
                    "③ 引导用户现场标定 → ④ 保留默认并注明。**"
                    "未给出的参数列入返回的 param_requests 与 assumptions，须先向用户澄清再解读结论。"
                    "噪声按 散粒/热/RIN 分别给出，可判断系统噪声主导来源。",
     "inputSchema": {"type": "object",
                     "properties": {
                         "species": {"type": "string", "description": "分子式，默认 CH4"},
                         "wn_center": {"type": "number", "description": "扫描中心波数 cm^-1，默认 2968.5"},
                         "T": {"type": "number", "description": "温度 K，默认 296"},
                         "P": {"type": "number", "description": "气压 atm，默认 1.01325"},
                         "x": {"type": "number", "description": "摩尔分数，默认 1e-3（1000 ppm）"},
                         "L_cm": {"type": "number", "description": "光程 cm，默认 50"},
                         "amp_V": {"type": "number", "description": "三角波幅值 V，默认 0.71（→±1.5 cm⁻¹）"},
                         "freq_Hz": {"type": "number", "description": "三角波频率 Hz，默认 100"},
                         "offset_V": {"type": "number", "description": "三角波偏置 V，默认 3.20"},
                         "phase_deg": {"type": "number", "description": "三角波相位 °，默认 0"},
                         "eta_VI": {"type": "number", "description": "驱动器跨导 mA/V，默认 24"},
                         "dnu_dI": {"type": "number", "description": "激光器调谐系数 cm⁻¹/mA，默认 −0.088"},
                         "wn_ref": {"type": "number", "description": "参考波数 cm⁻¹，默认 2964.7"},
                         "i_ref": {"type": "number", "description": "参考电流 mA，默认 120"},
                         "i_th": {"type": "number", "description": "阈值电流 mA，默认 30"},
                         "eta_IP": {"type": "number", "description": "功率斜率效率 mW/mA，默认 0.15"},
                         "fs": {"type": "number", "description": "采样率 Hz，默认 1e5（USB-6211 上限 2.5e5）"},
                         "n_samples": {"type": "integer", "description": "采样点数，默认 1e5"},
                         "adc_bits": {"type": "integer", "description": "ADC 位数，默认 16"},
                         "v_range": {"type": "number", "description": "ADC 输入量程 ±V，默认 10"},
                         "throughput": {"type": "number", "description": "光学总透过率，默认 0.90"},
                         "resp": {"type": "number", "description": "PD 响应度 A/W，默认 0.9"},
                         "gain": {"type": "number", "description": "PD 跨阻增益 V/A，默认 1e3"},
                         "bw": {"type": "number", "description": "PD 带宽 Hz，默认 1e6"},
                         "rin": {"type": "number", "description": "激光 RIN 1/√Hz，默认 1e-5"},
                         "edge": {"type": "string", "description": "rising(默认) / falling / average / both"},
                         "fit_order": {"type": "integer", "description": "基线多项式阶数，默认 3"},
                         "fit_frac": {"type": "number", "description": "无吸收区占比，默认 0.3"},
                         "seed": {"type": "integer"},
                         "save_png": {"type": "boolean", "description": "是否出五层链路图"}},
                     "required": ["species", "wn_center"]}},
    {"name": "tdlas_wms_instrument",
     "description": "WMS 仪器链路仿真：三角波扫描 + 正弦调制 → 激光 → 光路 → PD → ADC → 数字锁相，"
                    "输出 2f/1f（免标定归一化）。**调制幅值默认按最优调制系数 m≈2.2 自动优化**"
                    "（2f 峰值最大处，可用 mod_amp_V 手动覆盖）。默认 fm=30 kHz、fscan=100 Hz。"
                    "采样率不足时会自动提升并提示（30 kHz 调制需 ≥240 kS/s）。"
                    "缺省参数列入 param_requests，AI 须按 ai_guidance 主动向用户澄清。",
     "inputSchema": {"type": "object",
                     "properties": {
                         "species": {"type": "string", "description": "分子式，默认 CH4"},
                         "wn_center": {"type": "number", "description": "扫描中心波数 cm^-1，默认 2968.5"},
                         "T": {"type": "number"}, "P": {"type": "number"},
                         "x": {"type": "number", "description": "摩尔分数，默认 1e-3"},
                         "L_cm": {"type": "number", "description": "光程 cm，默认 50"},
                         "mod_freq_Hz": {"type": "number", "description": "正弦调制频率 Hz，默认 30000"},
                         "mod_amp_V": {"type": "number",
                                       "description": "调制幅值 V；缺省=按 m≈2.2 自动优化"},
                         "m_opt": {"type": "number", "description": "目标调制系数，默认 2.2"},
                         "lockin_avg": {"type": "integer", "description": "锁相平均周期数，默认 1"},
                         "amp_V": {"type": "number", "description": "三角波幅值 V"},
                         "freq_Hz": {"type": "number", "description": "三角波频率 Hz，默认 100"},
                         "offset_V": {"type": "number"}, "phase_deg": {"type": "number"},
                         "eta_VI": {"type": "number"}, "dnu_dI": {"type": "number"},
                         "wn_ref": {"type": "number"}, "i_ref": {"type": "number"},
                         "i_th": {"type": "number"}, "eta_IP": {"type": "number"},
                         "fs": {"type": "number"}, "n_samples": {"type": "integer"},
                         "adc_bits": {"type": "integer"}, "v_range": {"type": "number"},
                         "throughput": {"type": "number"}, "resp": {"type": "number"},
                         "gain": {"type": "number"}, "bw": {"type": "number"},
                         "rin": {"type": "number"}, "seed": {"type": "integer"},
                         "save_png": {"type": "boolean", "description": "是否出五层链路图"}},
                     "required": ["species", "wn_center"]}},
    {"name": "tdlas_guide",
     "description": "返回 AI 主动指导协议（面向实验新手）：参数索取优先级（实测值 → 器件型号检索 → "
                    "现场标定 → 内置默认并标注）、交互流程四步、专业术语表、全部参数索取指南。"
                    "**在开始任何 TDLAS 任务前应先调用本工具**，据此主动引导用户。",
     "inputSchema": {"type": "object",
                     "properties": {
                         "topic": {"type": "string",
                                   "description": "可选：查询特定术语（如 m / RIN / 2f1f / LOD）"}}}},
    {"name": "tdlas_invert",
     "description": "免标定浓度反演：给定测得的 WMS-2f/1f 峰高，返回摩尔分数（用仿真灵敏度 k，无需标气标定）。"
                    "仅适用弱吸收（αL≪1），强吸收时结果偏高。",
     "inputSchema": {"type": "object",
                     "properties": {
                         "peak_2f1f": {"type": "number", "description": "测得的 2f/1f 峰高"},
                         "species": {"type": "string"}, "wn0": {"type": "number"},
                         "T": {"type": "number"}, "P": {"type": "number"},
                         "L": {"type": "number"}, "a": {"type": "number"},
                         "x_ref": {"type": "number", "description": "参考浓度，默认 1e-3"},
                         "i0": {"type": "number"}, "i2": {"type": "number"},
                         "psi1_pi": {"type": "number"}, "psi2_pi": {"type": "number"}},
                     "required": ["peak_2f1f", "species", "wn0"]}},
    {"name": "tdlas_detection_limit",
     "description": "检测极限 LOD：给定等效透过率噪声 σ_τ，蒙特卡洛给出噪声等效浓度 NEC 与 LOD(nσ)。",
     "inputSchema": {"type": "object",
                     "properties": {
                         "species": {"type": "string"}, "wn0": {"type": "number"},
                         "T": {"type": "number"}, "P": {"type": "number"},
                         "L": {"type": "number"}, "a": {"type": "number"},
                         "sigma_tau": {"type": "number", "description": "等效透过率噪声，默认 1e-5"},
                         "x_true": {"type": "number", "description": "用于统计的真值浓度，默认 1e-3"},
                         "n_trials": {"type": "integer", "description": "蒙特卡洛次数，默认 30"},
                         "n_sigma": {"type": "number", "description": "倍数，默认 3"},
                         "seed": {"type": "integer"}},
                     "required": ["species", "wn0"]}},
    {"name": "tdlas_selftest",
     "description": "全链路自检（有线 / 2f 形状 / 弱场线性 / 反演闭环 / 检测限 / 时域交叉验证）。",
     "inputSchema": {"type": "object", "properties": {}}},
]


# ───────────────────────── JSON-RPC ─────────────────────────

def handle_request(req):
    """处理 MCP 请求（initialize / tools/list / tools/call）。"""
    method = req.get("method", "")
    params = req.get("params") or {}
    req_id = req.get("id")

    if method == "initialize":
        return {"jsonrpc": "2.0", "id": req_id, "result": SERVER_INFO}
    if method in ("notifications/initialized", "initialized"):
        return None                                  # 通知无需响应
    if method == "ping":
        return {"jsonrpc": "2.0", "id": req_id, "result": {}}
    if method == "tools/list":
        return {"jsonrpc": "2.0", "id": req_id, "result": {"tools": TOOLS}}
    if method == "tools/call":
        name = params.get("name")
        args = params.get("arguments") or {}
        fn = DISPATCH.get(name)
        if fn is None:
            return {"jsonrpc": "2.0", "id": req_id,
                    "error": {"code": -32601, "message": f"未知工具：{name}"}}
        try:
            result = fn(**args)
            return {"jsonrpc": "2.0", "id": req_id,
                    "result": {"content": [{"type": "text",
                                            "text": json.dumps(result, ensure_ascii=False,
                                                               indent=2)}]}}
        except Exception as e:
            return {"jsonrpc": "2.0", "id": req_id,
                    "result": {"content": [{"type": "text",
                                            "text": f"出错：{type(e).__name__}: {e}"}],
                               "isError": True}}
    return {"jsonrpc": "2.0", "id": req_id,
            "error": {"code": -32601, "message": f"未知方法：{method}"}}


def main():
    # Windows 控制台默认 GBK：协议流必须强制 UTF-8，否则中文会破坏 JSON-RPC
    for _s in (sys.stdin, sys.stdout, sys.stderr):
        try:
            _s.reconfigure(encoding="utf-8")
        except Exception:
            pass
    if "--selftest" in sys.argv:
        print(json.dumps(t_selftest(), ensure_ascii=False, indent=2))
        return
    for line in sys.stdin:                            # stdio 协议循环
        line = line.strip()
        if not line:
            continue
        try:
            resp = handle_request(json.loads(line))
        except Exception as e:
            resp = {"jsonrpc": "2.0", "id": None,
                    "error": {"code": -32700, "message": f"解析失败：{e}"}}
        if resp is not None:
            print(json.dumps(resp, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    main()
