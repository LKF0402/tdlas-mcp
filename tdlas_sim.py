#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""TDLAS/WMS 仿真核心（最小可跑原型）。

链路：
    HITRAN α(ν) ──► 扫描 + 高频调制的瞬时波长 ν(t) ──► τ(t) = exp(-αL)
                ──► 免标定 WMS 解析（傅里叶系数）──► 1f、2f 谐波
                ──► 2f/1f 归一化（弱吸收下正比于浓度）

设计纪律：
  · HITRAN 取数**自包含**（本仓库 tools/tdlas_hitran.py，仅用 HAPI 1.x，免 API key）；
  · WMS 解析式按公开成熟公式自行实现（免标定 WMS，Rieker et al., Appl. Opt. 48 (2009) 5546），
    不依赖任何第三方 TDLAS 代码；
  · 单位：波数 cm^-1 / 温度 K / 压力 atm / 光程 cm / 调制深度 cm^-1。

用法：
    python tdlas_sim.py             # 自测 + 出图
    python tdlas_sim.py --selftest  # 只自测
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np

_trapz = np.trapezoid if hasattr(np, "trapezoid") else np.trapz

# ── HITRAN 取数（自包含：本仓库 tools/tdlas_hitran.py；仅用 HAPI 1.x，免 API key）──
_ROOT = Path(__file__).resolve().parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))
from tools import tdlas_hitran as th  # noqa: E402


# ══════════════════ 1. 免标定 WMS 解析模型 ══════════════════

def wms_hk(t, tau, k: int) -> float:
    """τ(t) 的第 k 次傅里叶系数 Hk；t 为归一化的一个调制周期（0→1）。"""
    t = np.asarray(t, dtype=float)
    tau = np.asarray(tau, dtype=float)
    if k == 0:
        return float(_trapz(tau, t))
    return float(2.0 * _trapz(tau * np.cos(2.0 * np.pi * k * t), t))


def wms_calibration_free(t, tau, i0, psi1, i2, psi2):
    """免标定 WMS：由 τ(t) 与强度调制参数给出 1f、2f、4f 的 (X, Y) 分量。

    i0 / i2      : 一 / 二阶归一化强度调制幅度（AM）
    psi1 / psi2  : 一 / 二阶 IM-FM 相位差
    """
    H0 = wms_hk(t, tau, 0); H1 = wms_hk(t, tau, 1); H2 = wms_hk(t, tau, 2)
    H3 = wms_hk(t, tau, 3); H4 = wms_hk(t, tau, 4); H5 = wms_hk(t, tau, 5)
    H6 = wms_hk(t, tau, 6)
    X1f = H1 + i0 * (H0 + H2 / 2) * np.cos(psi1) + (i2 / 2) * (H1 + H3) * np.cos(psi2)
    Y1f = -i0 * (H0 - H2 / 2) * np.sin(psi1) + (i2 / 2) * (H1 - H3) * np.sin(psi2)
    X2f = H2 + (i0 / 2) * (H1 + H3) * np.cos(psi1) + i2 * (H0 + H4 / 2) * np.cos(psi2)
    Y2f = -(i0 / 2) * (H1 - H3) * np.sin(psi1) + i2 * (H0 - H4 / 2) * np.sin(psi2)
    X4f = H4 + (i0 / 2) * (H5 + H3) * np.cos(psi1) + (i2 / 2) * (H6 + H2) * np.cos(psi2)
    Y4f = (i0 / 2) * (H5 - H3) * np.sin(psi1) + (i2 / 2) * (H6 - H2) * np.sin(psi2)
    return X1f, Y1f, X2f, Y2f, X4f, Y4f


# ══════════════════ 2. 吸收谱（经 tools/tdlas_hitran.py 取自 HITRAN）══════════════════

def _check_mixture_specs(specs):
    """混合气摩尔分数的物理校验（**唯一真源**，所有分支都必须经此返回）。

    为什么必须有这一层：此前组分校验写在 `_resolve_mixture_specs` 末尾，但三个分支
    都提前 `return` 了 → 校验是**死代码**，`CH4:1.5` 与"和为 1.2"都被静默放行。
    规则（与 `_resolve_x_list` 的 x∈(0,1]、`invert` 的 x_est≤1 口径保持一致）：
      ① 每个组分 ∈ (0, 1]；
      ② 组分之和 ≤ 1（总摩尔分数必为 1；和 >1 等于分压之和超过总压）。
    """
    tot = 0.0
    for sp, xi in specs:
        if not 0.0 < xi <= 1.0:
            raise ValueError(f"混合气组分 {sp} 摩尔分数须在 (0, 1]，收到 {xi}")
        tot += xi
    if tot > 1.0 + 1e-12:
        detail = "、".join(f"{sp}:{xi:g}" for sp, xi in specs)
        raise ValueError(
            f"混合气各组分摩尔分数之和为 {tot:.6g} > 1，物理上不可能"
            f"（总摩尔分数必为 1；和 >1 意味着分压之和超过总压）。组分：{detail}。"
            f"请按实际配气比例归一化后重传。")
    return specs


def _resolve_mixture_specs(species, x, mixture):
    """归一化混合气组分列表为 [(species, x), ...]（物理纪律参考 hitran-mcp）。

    - mixture=None        → [(species, x)]（单物种，原行为）
    - mixture="CH4:0.01,CO2:0.04" 或 "CH4 0.01, CO2 0.04" → 解析
    - mixture=[(sp,x)] / [{"species":sp,"x":x}] / ["CH4:0.01", ...] → 列表
    混合气纪律：α_total = Σ x_i·α_pure_i(T,P,空气浴)；绝不用 x·P 当分压（会错窄线宽）。
    """
    if mixture is None:
        return _check_mixture_specs([(str(species), float(x))])
    if isinstance(mixture, str):
        out = []
        for part in mixture.replace(";", ",").split(","):
            part = part.strip()
            if not part:
                continue
            if ":" in part:
                sp, xv = part.split(":", 1)
            else:
                _t = part.split()
                if len(_t) != 2:
                    raise ValueError(f"混合气组分无法解析（需 'CH4:0.01' 或 'CH4 0.01'）：'{part}'")
                sp, xv = _t
            out.append((sp.strip().upper(), float(xv)))
        if not out:
            raise ValueError("混合气组分为空")
        return _check_mixture_specs(out)
    if isinstance(mixture, dict):
        mixture = [mixture]
    out = []
    for item in mixture:
        if isinstance(item, dict):
            out.append((str(item["species"]).strip().upper(), float(item["x"])))
        elif isinstance(item, (list, tuple)) and len(item) == 2:
            out.append((str(item[0]).strip().upper(), float(item[1])))
        elif isinstance(item, str):
            sp, xv = (item.split(":", 1) if ":" in item else item.split())
            out.append((sp.strip().upper(), float(xv)))
        else:
            raise ValueError(f"混合气组分无法解析：{item!r}")
    return _check_mixture_specs(out)


def _alpha_grid(specs, wn_lo, wn_hi, T, P, step, wingHW):
    """混合气吸收系数求和：α_total = Σ x_i·α_pure_i（同窗口逐点相加）。

    返回 (nu, alpha_total, per)：per=[{"species","x","alpha_pure","alpha","info"}]。
    各组分窗口一致（same wn_lo/wn_hi/step），网格天然对齐；若个别不一致则插值对齐。
    """
    nu = None
    alpha_total = None
    per = []
    for sp, xi in specs:
        nu_i, apure_i, info_i = th.absorption(sp, wn_lo, wn_hi, T=T, P=P,
                                              step=step, wingHW=wingHW)
        if nu is None:
            nu = nu_i
            alpha_total = apure_i * float(xi)
        else:
            if nu.shape != nu_i.shape or not np.allclose(nu, nu_i):
                apure_i = np.interp(nu, nu_i, apure_i)
            alpha_total = alpha_total + apure_i * float(xi)
        per.append({"species": sp, "x": float(xi), "alpha_pure": apure_i,
                    "alpha": apure_i * float(xi), "info": info_i})
    return nu, alpha_total, per


def get_alpha(species, wn_lo, wn_hi, T, P, x, step=5e-4, wingHW=20.0, mixture=None):
    """取吸收系数 α(ν) [cm^-1]；单物种 α=x·α_pure，混合气 α=Σ x_i·α_pure_i。

    窗口未覆盖 / 空谱由 tdlas_hitran 直接报错，绝不静默返回平谱。
    info["per_species"] 含各组分 α_pure（供混合气在既有标准图内叠加各组分曲线）。
    """
    specs = _resolve_mixture_specs(species, x, mixture)
    nu, alpha_total, per = _alpha_grid(specs, wn_lo, wn_hi, T, P, step, wingHW)
    if mixture is None:
        info = dict(per[0]["info"])
    else:
        info = {"n_lines_in_window": sum(p["info"].get("n_lines_in_window", 0) for p in per),
                "species_list": [p["species"] for p in per],
                "per_info": [p["info"] for p in per]}
    info["mole_frac"] = (None if mixture is not None else float(x))
    info["mixture"] = [{"species": s, "x": xi} for s, xi in specs]
    info["per_species"] = per
    return nu, alpha_total, info


# ══════════════════ 3. 扫描式 WMS 正向仿真 ══════════════════

def simulate(species="H2O", wn0=7185.596, T=296.0, P=1.0, x=0.1, L=30.0,
             a=0.10, i0=0.1671, i2=2.48e-3,
             psi1=1.9356 * np.pi, psi2=4.4138 * np.pi,
             span=0.8, n_scan=401, n_mod=96, step=5e-4,
             sigma_tau=0.0, seed=None, mixture=None):
    """扫描式 WMS 正向仿真。

    species/wn0   : 分子与目标线中心（cm^-1）
    T / P / x / L : 温度 K / 压力 atm / 摩尔分数 / 光程 cm
    a             : 调制深度（cm^-1）
    i0,i2,psi1,psi2: 激光强度调制参数（AM 幅度与 IM-FM 相位差）
    span/n_scan/n_mod: 扫描半宽 cm^-1 / 扫描点数 / 每点调制周期采样数
    sigma_tau/seed: 等效透过率噪声标准差（>0 时注入）与随机种子
    mixture       : 混合气组分（"CH4:0.01,CO2:0.04" 或 [(sp,x)]）；给定时覆盖单物种 x

    返回：nu, alpha, tau, wn_scan, S1f, S2f, S4f, S2f1f, meta（混合气额外含 alpha_per_species）
    """
    if mixture is None and not 0.0 < x <= 1.0:
        raise ValueError(f"摩尔分数必须在 (0, 1]，收到 {x}")
    if a <= 0:
        raise ValueError(f"调制深度 a 必须 > 0，收到 {a}")

    nu, alpha, info = get_alpha(species, wn0 - span, wn0 + span, T, P, x,
                                step=step, mixture=mixture)

    wn_scan = np.linspace(wn0 - span, wn0 + span, int(n_scan))
    tau_das = np.exp(-np.interp(wn_scan, nu, alpha) * L)

    t_mod = np.linspace(0.0, 1.0, int(n_mod))        # 一个调制周期（归一化）
    phase = 2.0 * np.pi * t_mod

    # 背景基线（τ≡1，无吸收）：只含强度调制 AM 残余，供免标定 2f/1f 扣除
    X1f_bg, Y1f_bg, X2f_bg, Y2f_bg, _, _ = wms_calibration_free(
        t_mod, np.ones_like(t_mod), i0, psi1, i2, psi2)
    R1f_bg = float(np.hypot(X1f_bg, Y1f_bg))
    if R1f_bg <= 1e-15:
        R1f_bg = 1.0                                # 纯 FM（无 AM）时 1f 基线为零，退回不归一化

    rng = np.random.default_rng(seed) if sigma_tau > 0 else None
    S1f = np.zeros_like(wn_scan); S2f = np.zeros_like(wn_scan)
    S4f = np.zeros_like(wn_scan); S2f1f = np.zeros_like(wn_scan)
    for j, wn_j in enumerate(wn_scan):
        tau = np.exp(-np.interp(wn_j + a * np.cos(phase), nu, alpha) * L)
        if rng is not None:                 # 等效透过率噪声（RIN/散粒/探测器/电路 综合）
            tau = tau + rng.normal(0.0, sigma_tau, tau.shape)
        X1f, Y1f, X2f, Y2f, X4f, Y4f = wms_calibration_free(t_mod, tau, i0, psi1, i2, psi2)
        R1f = np.hypot(X1f, Y1f)
        S1f[j] = R1f
        S2f[j] = np.hypot(X2f, Y2f)
        S4f[j] = np.hypot(X4f, Y4f)
        # 免标定 2f/1f：各自先用 1f 归一化、再扣背景基线 → 弱吸收下正比于浓度
        S2f1f[j] = (np.hypot(X2f / R1f - X2f_bg / R1f_bg,
                             Y2f / R1f - Y2f_bg / R1f_bg) if R1f > 1e-15 else 0.0)

    _mx = info["mixture"]
    if mixture is not None:
        _sp_label = "+".join(f"{s}@{xi:g}" for s, xi in [(d["species"], d["x"]) for d in _mx])
        meta = {"species": _sp_label, "wn0": wn0, "T": T, "P": P, "x": "混合气", "L": L,
                "a": a, "i0": i0, "i2": i2, "psi1": psi1, "psi2": psi2,
                "n_lines_in_window": info["n_lines_in_window"], "mixture": _mx,
                "alpha_context": info.get("alpha_context")}
        alpha_per = [{"label": f"{p['species']}@{p['x']:g}", "alpha": p["alpha"]}
                     for p in info["per_species"]]
        return {"nu": nu, "alpha": alpha, "tau": tau_das, "wn_scan": wn_scan,
                "S1f": S1f, "S2f": S2f, "S4f": S4f, "S2f1f": S2f1f,
                "alpha_per_species": alpha_per, "meta": meta}
    meta = {"species": species, "wn0": wn0, "T": T, "P": P, "x": x, "L": L,
            "a": a, "i0": i0, "i2": i2, "psi1": psi1, "psi2": psi2,
            "n_lines_in_window": info["n_lines_in_window"], "table": info["table"], "alpha_context": info.get("alpha_context")}
    return {"nu": nu, "alpha": alpha, "tau": tau_das, "wn_scan": wn_scan,
            "S1f": S1f, "S2f": S2f, "S4f": S4f, "S2f1f": S2f1f, "meta": meta}


# ══════════════════ 4. 自测 ══════════════════

def selftest():
    """最小验证：有线 → 2f 成标准形状 → 浓度单调。"""
    print("== TDLAS/WMS 自测 ==")
    r = simulate(x=0.1)
    ap = float(r["alpha"].max())
    print(f"  α_peak = {ap:.4e} cm^-1 @ {r['nu'][int(r['alpha'].argmax())]:.4f} cm^-1"
          f"  (线表 {r['meta']['table']}，窗口内 {r['meta']['n_lines_in_window']} 条线)")
    assert ap > 0, "吸收谱全零：分子 / 窗口 / 线表有问题"

    s2 = r["S2f"]
    k = int(s2.argmax())
    margin = min(k, s2.size - 1 - k)
    print(f"  WMS-2f 中心峰 @ {r['wn_scan'][k]:.4f} cm^-1（距扫描边界 {margin} 点）")
    assert margin > 2, "2f 峰贴边：扫描窗口太窄，请加大 span"

    # 3) 浓度响应：弱吸收下 2f/1f 应近似正比于浓度（免标定的立命之本）
    xs = [1e-4, 1e-3, 1e-2]
    hs = [float(simulate(x=xx)["S2f1f"].max()) for xx in xs]
    print("  2f/1f 峰高 vs 浓度：" + "，".join(f"x={xx:g}→{hh:.3e}" for xx, hh in zip(xs, hs)))
    assert all(b > a for a, b in zip(hs, hs[1:])), "浓度增大但 2f/1f 峰高未单调增大"
    lin = (hs[1] / hs[0]) / (xs[1] / xs[0])
    print(f"  弱场线性度（x=1e-4 → 1e-3，理想 1.00）：{lin:.3f}")
    assert 0.7 < lin < 1.3, "弱吸收下 2f/1f 未正比于浓度，链路有问题"

    # 4) 浓度反演闭环：真值 → 仿真峰高 → 反演 → 对比
    sens = sensitivity_2f1f(x_ref=1e-3)
    print(f"  灵敏度 k = {sens:.4e}（2f/1f 峰高 ÷ 浓度）")
    for x_true in (1e-4, 1e-3, 1e-2):
        x_est = invert_concentration(float(simulate(x=x_true)["S2f1f"].max()), sens)
        err = abs(x_est - x_true) / x_true
        print(f"  反演闭环：真值 {x_true:g} → 反演 {x_est:.4e}（差 {err * 100:.2f}%）")
        assert err < 0.05, f"反演误差 {err:.1%} 超过 5%"

    # 5) 检测极限：给 τ 加等效噪声 σ_τ，统计反演波动 → NEC / LOD
    dl = detection_limit(sigma_tau=1e-5, x_true=1e-3, n_trials=30)
    print(f"  检测极限（σ_τ=1e-5, x=1e-3）：反演均值 {dl['mean']:.3e}，"
          f"NEC = {dl['NEC']:.3e}，LOD(3σ) = {dl['LOD']:.3e}")
    assert 0 < dl["LOD"] < 1e-2, f"LOD 不合理：{dl['LOD']}"

    # 6) 交叉验证：时域数字锁相 vs 解析模型（2f 形状应一致）
    td = simulate_td()
    idx = np.argsort(td["wn_scan"])
    interp = np.interp(r["wn_scan"], td["wn_scan"][idx], td["S2f"][idx])
    core = slice(r["wn_scan"].size // 10, -r["wn_scan"].size // 10)   # 去两端低通瞬态
    an = r["S2f"][core] / max(float(r["S2f"][core].max()), 1e-30)
    td_n = interp[core] / max(float(interp[core].max()), 1e-30)
    corr = float(np.corrcoef(an, td_n)[0, 1])
    print(f"  交叉验证（时域锁相 vs 解析模型，2f 形状相关）: {corr:.4f}")
    assert corr > 0.9, f"时域锁相与解析模型不一致（corr={corr:.3f}）"

    # 7) DAS 时域链路：三角波 → PD 原始信号 → 基线拟合扣除 → 吸光度应回到 αL
    d = simulate_das_td(x=1e-4, L=30.0, baseline_slope=0.05, fit_order=3)
    tp = float(d["alpha_L_true"].max())
    dp = float(d["das"].max())
    derr = abs(dp - tp) / tp
    print(f"  DAS 链路（三角波·上升沿、基线斜率 0.05、3 阶拟合）：吸光度峰 {dp:.4e} "
          f"vs 真值 αL {tp:.4e}（差 {derr * 100:.2f}%）")
    assert derr < 0.05, f"DAS 基线扣除误差 {derr:.1%} 超过 5%"

    # 8) 电压自适应：amp_V / offset_V 由 wn_center + scan_span_cm 经 V-ν 关系反算
    w = simulate_wms_instrument(mod_amp_V=0.1)      # 手填调制幅值，跳过慢速自适应扫描
    c8 = w["meta"]["cfg"]
    dnu_dV = abs(float(c8["eta_VI"]) * float(c8["dnu_dI"]))
    amp_exp = float(c8["scan_span_cm"]) / dnu_dV
    off_exp = (float(c8["i_ref"]) + (float(w["meta"]["wn_center"]) - float(c8["wn_ref"]))
               / float(c8["dnu_dI"])) / float(c8["eta_VI"])
    print(f"  电压自适应：amp_V={c8['amp_V']:.4g} V（期望 {amp_exp:.4g}），"
          f"offset_V={c8['offset_V']:.4g} V（期望 {off_exp:.4g}）")
    assert abs(float(c8["amp_V"]) - amp_exp) < 1e-6, "amp_V 未按 scan_span_cm 反算"
    assert abs(float(c8["offset_V"]) - off_exp) < 1e-6, "offset_V 未按 wn_center 反算"
    # 背景扣除默认关闭（出原始谱、不替用户静默修正基线）；开启后必须真的生效
    assert w["meta"].get("bg_subtracted") is False, "默认应为未做背景扣除（原始基线口径）"
    assert simulate_wms_instrument(mod_amp_V=0.1,
                                   background_subtract=True)["meta"]["bg_subtracted"] is True, \
        "显式开启 background_subtract 后应真的做了背景扣除"

    # 9) 越界 wn_center：选了不在此 DFB 调谐范围内的线 → 清晰报错（不静默给垃圾）
    try:
        simulate_wms_instrument(wn_center=6500.0)
        raise AssertionError("越界 wn_center 应触发 ValueError，却静默通过")
    except ValueError as e:
        assert ("offset_V" in str(e)) or ("功率" in str(e)), f"报错信息不含关键提示：{e}"
        print(f"  越界保护：wn_center=6500 → ValueError（{str(e)[:42]}…）")

    # 10) 二次调谐不可达：判别式 < 0 必须硬报错（不得用 max(disc,0) 编出一个假根）
    try:
        simulate_wms_instrument(wn_center=float(LASER_DEFAULTS["wn_ref"]) + 5.0,
                                d2nu_dI2=-1e-3, mod_amp_V=0.1)
        raise AssertionError("二次调谐不可达应触发 ValueError，却静默通过")
    except ValueError as e:
        assert "不可达" in str(e), f"报错未点明\"不可达\"：{e}"
        print(f"  二次调谐不可达：判别式<0 → ValueError（{str(e)[:42]}…）")
    w2 = simulate_wms_instrument(wn_center=float(LASER_DEFAULTS["wn_ref"]) + 2.0,
                                 d2nu_dI2=-1e-3, mod_amp_V=0.1)     # 同模型下可达的目标波数
    off2 = float(w2["meta"]["cfg"]["offset_V"])
    assert 0.0 <= off2 <= 5.0, f"二次调谐可达时 offset_V={off2:g} V 越界"
    print(f"  二次调谐可达：offset_V={off2:.4g} V（判别式>0 时按取近根正常反算）")

    # 11) 混合气：α_total = Σ x_i·α_pure_i（**绝不用 x·P 当分压**，否则线宽算错）
    _lo, _hi = 2967.5, 2969.5
    _, _am, _mi = get_alpha("MIX", _lo, _hi, 296.0, 1.01325, 0.0, step=2e-3,
                            mixture="CH4:1e-4,H2O:1e-3")
    _per = _mi["per_species"]
    _, _a1, _ = get_alpha("CH4", _lo, _hi, 296.0, 1.01325, 1e-4, step=2e-3)
    _, _a2, _ = get_alpha("H2O", _lo, _hi, 296.0, 1.01325, 1e-3, step=2e-3)
    _mx = max(float(np.max(np.abs(_am))), 1e-30)
    _rel = float(np.max(np.abs(_am - (_a1 + _a2)))) / _mx
    assert _rel < 1e-9, f"混合气 α ≠ 各组分之和（相对差 {_rel:.2e}）"
    assert len(_per) == 2 and all(
        float(np.max(np.abs(p["alpha"] - p["x"] * p["alpha_pure"]))) == 0.0
        for p in _per), "各组分 α 未按 x_i·α_pure_i 缩放"
    print(f"  混合气 α 求和：CH4(1e-4)+H2O(1e-3) vs 分物种之和 相对差 {_rel:.1e}"
          f"（α_peak={_mx:.4g} cm⁻¹）")
    _wm = simulate_wms_instrument("CH4", wn_center=2968.5, mod_amp_V=0.1,
                                  mixture="CH4:1e-4,H2O:1e-3")
    _ps = _wm.get("alphaL_per_species")
    assert _ps and len(_ps) == 2, "混合气未返回各组分 αL（alphaL_per_species）"
    _d2 = float(np.max(np.abs(sum(p["alphaL"] for p in _ps) - _wm["alphaL_cyc"]))) \
        / max(float(np.max(np.abs(_wm["alphaL_cyc"]))), 1e-30)
    assert _d2 < 1e-9, f"各组分 αL 之和不等于总 αL（相对差 {_d2:.2e}）"
    print(f"  混合气 WMS 链路：各组分 αL 之和 ↔ 总 αL 相对差 {_d2:.1e}，"
          f"组分={[p['label'] for p in _ps]}")

    print("  自测通过")
    return r


# ══════════════════ 5. 免标定浓度反演 ══════════════════

def sensitivity_2f1f(species="H2O", wn0=7185.596, T=296.0, P=1.0, L=30.0,
                     a=0.10, x_ref=1e-3, **kw):
    """单位浓度的 WMS-2f/1f 峰高灵敏度 k（弱吸收下 S2f1f_peak ≈ k·x）。

    k 只取决于 T/P/L、调制参数与谱线参数，与待测浓度无关 —— 这正是"免标定"：
    用一次仿真（或一次已知浓度实验）定出 k，之后由峰高即可直接反演浓度。
    """
    if x_ref <= 0:
        raise ValueError(f"参考浓度 x_ref 必须 > 0，收到 {x_ref}")
    r = simulate(species, wn0, T, P, x=x_ref, L=L, a=a, **kw)
    peak = float(r["S2f1f"].max())
    if peak <= 0:
        raise ValueError("参考浓度下 2f/1f 峰高为零：请检查线选择 / 调制深度 a / 扫描窗口")
    return peak / float(x_ref)


def invert_concentration(peak_2f1f, k):
    """免标定反演：由测得的 2f/1f 峰高与灵敏度 k 求摩尔分数 x = peak / k。"""
    if k <= 0:
        raise ValueError(f"灵敏度 k 必须 > 0，收到 {k}")
    return float(peak_2f1f) / float(k)


def detection_limit(species="H2O", wn0=7185.596, T=296.0, P=1.0, L=30.0, a=0.10,
                    sigma_tau=1e-5, x_true=1e-3, n_trials=30, n_sigma=3.0,
                    seed=0, **kw):
    """检测极限 LOD（最小可测摩尔分数）—— 蒙特卡洛。

    在透过率 τ 上叠加等效噪声 σ_τ（综合 RIN / 散粒 / 探测器 / 前置放大器噪声，
    工程上由无吸收基线的实测噪声给出），跑完整 WMS 链路并免标定反演 n_trials 次；
    取反演值的标准差为噪声等效浓度 NEC，LOD = n_sigma × NEC（默认 3σ）。

    返回 dict：k, mean, NEC, LOD, n_trials, samples
    """
    if n_trials < 2:
        raise ValueError("n_trials 至少 2 才能估计标准差")
    k = sensitivity_2f1f(species, wn0, T, P, L, a, x_ref=x_true, **kw)
    est = np.empty(int(n_trials), dtype=float)
    for i in range(int(n_trials)):
        r = simulate(species, wn0, T, P, x=x_true, L=L, a=a,
                     sigma_tau=sigma_tau, seed=seed + i, **kw)
        est[i] = invert_concentration(float(r["S2f1f"].max()), k)
    nec = float(np.std(est, ddof=1))
    return {"k": k, "x_true": x_true, "sigma_tau": sigma_tau,
            "mean": float(est.mean()), "NEC": nec, "LOD": n_sigma * nec,
            "n_trials": int(n_trials), "samples": est}


def scan_detection_limit(species="CH4", wn0=2968.5, T=296.0, P=1.01325, a=0.10,
                         L_list=None, x_list=None, sigma_tau=1e-5, n_trials=10,
                         n_sigma=3.0, seed=0, **kw):
    """检测极限 LOD 随光程 L / 参考浓度 x 的扫描（用于系统选型）。

    物理（弱吸收 αL≪1 下）：
      · LOD ∝ 1/L —— 光程加倍，LOD 减半（信噪比线性提升）；
      · LOD 与参考浓度 x 基本无关（NEC ∝ σ_τ/α_pure，不含 x），
        直到 αL≳0.1 进入非线性区才因反演饱和而略升。

    返回 dict：species / wn0 / T / P / a / L_list / x_list /
               LOD[i,j]（行=浓度 x，列=光程 L）/ sigma_tau / n_trials / n_sigma
    """
    if L_list is None:
        L_list = [5.0, 10.0, 30.0, 50.0, 100.0, 200.0]
    if x_list is None:
        x_list = [1e-4, 1e-3, 1e-2]
    L_list = [float(L) for L in L_list]
    x_list = [float(x) for x in x_list]
    LOD = np.full((len(x_list), len(L_list)), np.nan)
    for i, x in enumerate(x_list):
        for j, L in enumerate(L_list):
            dl = detection_limit(species, wn0, T, P, L, a, sigma_tau=sigma_tau,
                                 x_true=x, n_trials=int(n_trials),
                                 n_sigma=n_sigma, seed=int(seed), **kw)
            LOD[i, j] = float(dl["LOD"])
    return {"species": str(species).upper(), "wn0": float(wn0), "T": float(T),
            "P": float(P), "a": float(a),
            "L_list": L_list, "x_list": x_list, "LOD": LOD,
            "sigma_tau": float(sigma_tau), "n_trials": int(n_trials),
            "n_sigma": float(n_sigma)}


def plot_detection_limit_scan(res, out_png):
    """检测极限扫描图（1×2）：
    左：LOD vs 光程 L（log-log，不同参考浓度 x 一组曲线）→ 斜率≈−1 印证 LOD∝1/L
    右：LOD vs 参考浓度 x（log-x，不同光程 L 一组曲线）→ 弱吸收下近乎平线
    """
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    use_cjk_font(matplotlib)

    L = np.asarray(res["L_list"]); x = np.asarray(res["x_list"]); LOD = np.asarray(res["LOD"])
    fig, ax = plt.subplots(1, 2, figsize=(14, 6))
    cmap = plt.cm.viridis(np.linspace(0, 0.9, LOD.shape[0]))
    # 左：LOD vs L
    for i, xi in enumerate(x):
        ax[0].loglog(L, LOD[i], "o-", color=cmap[i], lw=1.4, label=f"x={xi:g}")
    ax[0].set_xlabel("光程 L (cm)"); ax[0].set_ylabel("检测极限 LOD（摩尔分数）")
    ax[0].set_title(f"LOD vs 光程（{res['species']} @ {res['wn0']:g} cm⁻¹, σ_τ={res['sigma_tau']:g}）")
    ax[0].grid(True, which="both", alpha=0.3); ax[0].legend(fontsize=8)
    # 右：LOD vs x
    for j, Lj in enumerate(L):
        ax[1].loglog(x, LOD[:, j], "o-", lw=1.4,
                     color=plt.cm.plasma(j / max(len(L) - 1, 1)), label=f"L={Lj:g} cm")
    ax[1].set_xlabel("参考浓度 x（摩尔分数）"); ax[1].set_ylabel("检测极限 LOD（摩尔分数）")
    ax[1].set_title("LOD vs 参考浓度（弱吸收下应近似平线）")
    ax[1].grid(True, which="both", alpha=0.3); ax[1].legend(fontsize=8)
    fig.tight_layout()
    fig.savefig(out_png, dpi=130)
    plt.close(fig)
    return out_png


# ══════════════════ 6. 数字锁相（时域交叉验证）══════════════════

def wms_harmonic_lockin(S, t, fs, fm, n, n_cycle_avg=1, n_stages=2,
                        lock_kind="boxcar", cutoff_ratio=0.25, zero_phase=False):
    """数字锁相：正交解调 + 低通。

    低通实现可选择：
      · lock_kind="boxcar"（默认）：一个调制周期窗口的滑动平均（矩形窗 FIR）。
        频响 sinc，零点精确落在 fm 的整数倍 → 混频后的 2fm/4fm 数值零泄漏；
        级联 n_stages 级 = sinc^n_stages（旁瓣 -13N dB）。在 fc/fs 极低（扫描/调制
        频率比很大）时比 Butterworth 数值更稳健。此为与既有行为**完全一致**的默认。
      · lock_kind="butter"：scipy Butterworth 低通，阶数 = n_stages（建议 4），
        截止 fc = cutoff_ratio·fm（建议 0.25 = fm/4）。无精确零点，靠单调衰减压 2fm；
        n_stages=2 时 2fm 处仅 -36 dB，泄漏≈信号的 130%（见滤波器横向对比）→ 不够，
        至少 4 阶 @ fm/4（2fm 处 -72 dB，泄漏≈2%）。

    zero_phase=True：用 filtfilt（正滤+倒滤）实现零相位；filtfilt 每级 = 2 次滤波，故开启后等效衰减翻倍（与「因果版」阶数/级数相同，但正滤+倒滤一遍）。
        · 对 lock_kind="butter"：消除 lfilter 的群延迟、峰位对齐到真值（因果版峰位后移）。
        · 对 lock_kind="boxcar"：默认（mode='same'）已零相位，**无需开启**；开启后每级改走
          filtfilt（每级 2 次卷积），n_stages=2 时总 4 次 = sinc^4（比默认 2 次=sinc^2 零点更深）、峰位仍不变；仅在你想要更狠的低通时才开。
        注意：零相位 filtfilt 需整段记录、非因果 → **仅离线可用**，默认 False。
    """
    w = 2.0 * np.pi * fm
    S = np.asarray(S, dtype=float)
    X = S * np.cos(n * w * t)
    Y = S * np.sin(n * w * t)
    if str(lock_kind).lower() == "butter":
        from scipy.signal import butter as _butter, filtfilt as _ff, lfilter as _lf
        b, a = _butter(int(n_stages), cutoff_ratio * fm / (0.5 * float(fs)), btype="low")
        if zero_phase:
            X, Y = _ff(b, a, X), _ff(b, a, Y)
        else:
            X, Y = _lf(b, a, X), _lf(b, a, Y)
        return np.sqrt(X ** 2 + Y ** 2), X, Y
    # boxcar（默认）：整数调制周期滑动平均
    win = max(1, int(round(n_cycle_avg * fs / fm)))
    ker = np.ones(win) / win
    if zero_phase:
        from scipy.signal import filtfilt as _ff
        for _ in range(max(1, int(n_stages))):
            X, Y = _ff(ker, [1.0], X), _ff(ker, [1.0], Y)
    else:
        for _ in range(max(1, int(n_stages))):
            X = np.convolve(X, ker, mode="same")
            Y = np.convolve(Y, ker, mode="same")
    return np.sqrt(X ** 2 + Y ** 2), X, Y


def simulate_td(species="H2O", wn0=7185.596, T=296.0, P=1.0, x=0.1, L=30.0,
                a=0.10, i0=0.1671, i2=2.48e-3,
                psi1=1.9356 * np.pi, psi2=4.4138 * np.pi,
                i_s=0.1661, psi_s=1.411 * np.pi,
                span=0.8, fscan=100.0, fm=1e4, fs=5e5, n_cycle_avg=1, step=5e-4):
    """时域仿真：生成探测器信号 I(t)=I₀(t)·τ(ν(t))，再用数字锁相提取 1f、2f。

    与解析模型（simulate）是两条独立实现，互为交叉验证。
    扫描波形为余弦（fscan），叠加高频正弦调制（fm）。
    """
    if not 0.0 < x <= 1.0:
        raise ValueError(f"摩尔分数必须在 (0, 1]，收到 {x}")
    ws, wm = 2.0 * np.pi * fscan, 2.0 * np.pi * fm
    t = np.arange(int(round(fs / fscan)), dtype=float) / fs
    wn_scan = wn0 + span * np.cos(ws * t)
    wn = wn_scan + a * np.cos(wm * t)
    I0 = (1.0 + i_s * np.cos(ws * t + psi_s)
          + i0 * np.cos(wm * t + psi1) + i2 * np.cos(2.0 * wm * t + psi2))
    nu, alpha, info = get_alpha(species, wn0 - span, wn0 + span, T, P, x, step=step)
    tau = np.exp(-np.interp(wn, nu, alpha) * L)
    It = I0 * tau
    S1f, _, _ = wms_harmonic_lockin(It, t, fs, fm, 1, n_cycle_avg)
    S2f, _, _ = wms_harmonic_lockin(It, t, fs, fm, 2, n_cycle_avg)
    return {"t": t, "wn_scan": wn_scan, "It": It, "S1f": S1f, "S2f": S2f,
            "meta": {"species": species, "wn0": wn0, "T": T, "P": P, "x": x, "L": L,
                     "a": a, "fscan": fscan, "fm": fm, "fs": fs,
                     "n_lines_in_window": info["n_lines_in_window"]}}


# ══════════════════ 7. DAS 时域链路（三角波 → PD 信号 → 基线扣除）══════════════════

def simulate_das_td(species="H2O", wn0=7185.596, T=296.0, P=1.0, x=1e-3, L=50.0,
                    span=0.8, fscan=100.0, fs=5e5, baseline_slope=0.05,
                    sigma=0.0, seed=None, fit_order=3, fit_frac=0.3, step=5e-4,
                    edge="rising", mixture=None):
    """三角波扫描 DAS 时域仿真：PD 原始信号 → 多项式基线拟合/扣除 → DAS 信号。

    与真实 DAS 实验一一对应：
        ν(t) 三角波扫描 → I₀(t) 基线（光强随扫描斜坡）→ PD 原始信号 It(t)=I₀·τ
        → 用两端"无吸收区"拟合多项式基线 → 扣除 → 吸光度 A = -ln(It/baseline) ≈ αL

    span          : 扫描半宽 cm^-1（三角波覆盖 wn0±span）
    fscan / fs    : 三角波频率 / 采样率 Hz（取一个完整周期）
    baseline_slope: I₀ 相对扫描的基线斜率（模拟激光 P-I 变化；0 = 平坦）
    sigma         : 等效透过率噪声（加到 PD 信号上）
    fit_order     : 基线拟合多项式阶数
    fit_frac      : 距中心多远处的点算"无吸收区"（占 span 的比例）
    edge          : 用哪一段作最终 DAS（默认 "rising"）——
                    "rising" 上升沿 / "falling" 下降沿 / "average" 两段平均（SNR↑√2）/
                    "both" 两段都留（wn 非单调，供对照）。
                    **真实实验中上升沿与下降沿常不重合**（激光调谐非线性、扫描期热漂移、
                    探测器/电路带宽导致的相位滞后），故需显式选段，不默认合并。

    返回：t, wn, tri, I0, It, baseline, das_full, das, wn_das, alpha, alpha_L_true, meta
      · das_full   : 完整三角波周期的吸光度（两段都在，供画对照图）
      · das/wn_das : 按 edge 选取的最终结果（wn 递增；edge="both" 时即完整周期）
    """
    if span <= 0:
        raise ValueError(f"扫描半宽 span 必须 > 0，收到 {span}")
    if fscan <= 0 or fs <= 0:
        raise ValueError("fscan / fs 必须 > 0")
    period = 1.0 / float(fscan)
    n = int(round(float(fs) * period))
    if n < 8:
        raise ValueError(f"采样点太少（fs/fscan = {float(fs) * period:.1f}），请提高 fs 或降低 fscan")

    t = np.linspace(0.0, period, n, endpoint=False)
    phase = (t / period) % 1.0                           # 三角波：关于 wn0 对称，先上升后下降
    tri = 1.0 - 4.0 * np.abs(phase - 0.5)                # 0→-1, 0.5→+1, 1→-1；值域 [-1, 1]
    wn = float(wn0) + float(span) * tri
    I0 = 1.0 + float(baseline_slope) * tri               # 无吸收时的光强基线（随扫描变化）

    nu, alpha, info = get_alpha(species, wn0 - span - 0.2, wn0 + span + 0.2, T, P, x,
                                step=step, mixture=mixture)
    alpha_wn = np.interp(wn, nu, alpha)
    It = I0 * np.exp(-alpha_wn * float(L))               # PD 原始信号

    rng = np.random.default_rng(seed) if sigma > 0 else None
    if rng is not None:
        It = It + rng.normal(0.0, float(sigma), It.shape)

    mask = np.abs(wn - float(wn0)) > float(span) * (1.0 - float(fit_frac))
    wn_c = wn - float(wn0)                               # 中心化：改善多项式条件数（否则 RankWarning）
    if int(mask.sum()) > int(fit_order) + 1:
        coef = np.polyfit(wn_c[mask], It[mask], int(fit_order))
        baseline = np.polyval(coef, wn_c)
    else:                                                # 拟合点不足 → 退化为常数基线
        baseline = np.full_like(It, float(np.mean(It[mask])))
    das_full = -np.log(np.maximum(It, 1e-12) / np.maximum(baseline, 1e-12))

    # ★ 密集谱区基线拟合失效回退：两端"无吸收区"不干净时，多项式把吸收线拟合进基线，
    #   使 das 峰值被放大数十万倍。拟合后校验，异常则改用已知 I0（无吸收基线）。
    baseline_fallback = False
    _bmax = float(np.max(baseline))
    _ratio = (float(np.max(It)) / _bmax) if _bmax > 0 else 0.0
    _aL_th = float(np.max(alpha_wn) * float(L))
    if _ratio < 0.9 or abs(float(np.max(das_full)) - _aL_th) > 0.5 * max(_aL_th, 1e-30):
        baseline_fallback = True
        baseline = I0                                       # 已知无吸收基线（不含吸收/噪声）
        das_full = -np.log(np.maximum(It, 1e-12) / np.maximum(I0, 1e-12))

    # 按 edge 选取最终 DAS（三角波：前半周期上升、后半周期下降）
    edge = str(edge).strip().lower()
    if edge not in ("rising", "falling", "both", "average"):
        raise ValueError(f"edge 必须是 rising / falling / both / average，收到 {edge!r}")
    half = n // 2
    if edge == "rising":
        wn_das, das = wn[:half], das_full[:half]
    elif edge == "falling":
        wn_das, das = wn[half:][::-1], das_full[half:][::-1]     # 反转 → wn 递增
    elif edge == "average":
        wn_das = wn[:half]
        das = 0.5 * (das_full[:half] + das_full[half:][::-1])    # 两段对应点平均（SNR↑√2）
    else:
        wn_das, das = wn, das_full

    # 替用户主动考虑：参数是否落在合理区间（不合理时明确提示，而非静默给错结果）
    warnings = []
    _x_disp = f"{x:g}" if mixture is None else "混合气"
    if baseline_fallback:
        warnings.append("baseline fallback: dense spectrum, used known I0")
    aL = float(alpha_wn.max()) * float(L)
    if aL > 0.1:
        warnings.append(f"αL ≈ {aL:.3g} 偏大（>0.1）：DAS 进入非线性区、峰形被压低，"
                        f"建议降低浓度或光程（当前 x={_x_disp}, L={L:g} cm）")
    elif aL < 1e-4:
        warnings.append(f"αL ≈ {aL:.3g} 偏小（<1e-4）：吸收太弱、信噪比可能不足，"
                        f"建议加大光程或浓度（当前 x={_x_disp}, L={L:g} cm）")
    if float(span) < 0.2:
        warnings.append(f"扫描半宽 span={span:g} cm^-1 偏窄：可能未完整覆盖吸收线两翼，"
                        f"基线拟合会偏")

    _mx = info["mixture"]
    _sp = (str(species).upper() if mixture is None
           else "+".join(f"{d['species']}@{d['x']:g}" for d in _mx))
    meta = {"species": _sp, "wn0": float(wn0), "T": float(T), "P": float(P),
            "x": (float(x) if mixture is None else "混合气"), "L": float(L),
            "span": float(span), "fscan": float(fscan),
            "fs": float(fs), "baseline_slope": float(baseline_slope), "sigma": float(sigma),
            "fit_order": int(fit_order), "fit_frac": float(fit_frac), "edge": edge,
            "baseline_fallback": baseline_fallback,
            "alpha_L_peak": aL, "warnings": warnings,
            "n_lines_in_window": info["n_lines_in_window"],
            "table": info.get("table"), "alpha_context": info.get("alpha_context")}
    if mixture is not None:
        meta["mixture"] = _mx
    out = {"t": t, "wn": wn, "tri": tri, "I0": I0, "It": It, "baseline": baseline,
           "das_full": das_full, "das": das, "wn_das": wn_das,
           "alpha": alpha, "alpha_L_true": alpha_wn * float(L), "meta": meta}
    if mixture is not None:                    # 各组分 αL（对齐三角波全周期 wn，供标准图叠加）
        out["alphaL_per_species"] = [
            {"label": f"{p['species']}@{p['x']:g}",
             "alphaL": np.interp(wn, nu, p["alpha_pure"]) * float(L)}
            for p in info["per_species"]]
    return out


def plot_das_td(r, out_png):
    """DAS 链路四层图：三角波波长 → 基线 I₀ → PD 原始信号 → 扣除后 DAS。"""
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    m = r["meta"]
    t_ms = r["t"] * 1e3
    fig, ax = plt.subplots(4, 1, figsize=(9, 10))
    ax[0].plot(t_ms, r["wn"])
    ax[0].set_ylabel(r"$\nu$ (cm$^{-1}$)")
    ax[0].set_xlabel("time (ms)")
    ax[0].set_title(f"DAS chain — {m['species']} @ {m['wn0']:.3f} cm$^{{-1}}$   "
                    f"T={m['T']:g} K, P={m['P']:g} atm, x={m['x']}, L={m['L']:g} cm, "
                    f"slope={m['baseline_slope']:g}, baseline fit order={m['fit_order']}")
    ax[1].plot(t_ms, r["I0"])
    ax[1].set_ylabel(r"$I_0$ (a.u.)")
    ax[1].set_xlabel("time (ms)")
    ax[2].plot(t_ms, r["It"], ".", ms=2, label=r"PD raw  $I_t(t)$")
    ax[2].plot(t_ms, r["baseline"], "-", lw=1.2, label="fitted baseline")
    ax[2].set_ylabel(r"$I_t$ (a.u.)")
    ax[2].set_xlabel("time (ms)")
    ax[2].legend()
    half = r["t"].size // 2                    # 三角波：前半周期上升、后半周期下降
    dfl = r["das_full"]
    ax[3].plot(r["wn"][:half], dfl[:half], ".", ms=2, color="C0", alpha=0.45, label="DAS — rising edge")
    ax[3].plot(r["wn"][half:], dfl[half:], ".", ms=2, color="C3", alpha=0.45, label="DAS — falling edge")
    ax[3].plot(r["wn_das"], r["das"], "-", lw=1.2, color="C2", label=f"final DAS ({m['edge']})")
    ax[3].plot(r["wn"], r["alpha_L_true"], "--", lw=1, alpha=0.7, color="k", label=r"true $\alpha L$")
    _mix = r.get("alphaL_per_species")             # 混合气：叠加各组分 αL（不新增图）
    if _mix:
        _cols = plt.cm.viridis(np.linspace(0.1, 0.8, max(len(_mix), 2)))
        for _c, _p in zip(_cols, _mix):
            ax[3].plot(r["wn"], _p["alphaL"], "-.", lw=0.9, color=_c, label=_p["label"])
    ax[3].set_ylabel("absorbance")
    ax[3].set_xlabel(r"wavenumber (cm$^{-1}$)")
    ax[3].legend()
    for a_ in ax:
        a_.grid(alpha=0.3)
    fig.tight_layout()
    fig.savefig(out_png, dpi=150)
    return out_png


# ══════════════════ 8. 仪器系统链路（DAQ 电压 → 激光 → 光路 → PD → ADC）══════════════════

# 内置默认参数：常见 16-bit USB DAQ + 典型中红外 DFB 激光器 + CH4 @2968.5 cm⁻¹ 场景。
# 参数缺省时的索取优先级：① 用户给实测值 → ② 用户给器件型号（由 AI 检索规格书）
# → ③ 用户现场标定（如"电压变化 ΔV → 波数变化 Δν"）→ ④ 用下列默认值并明确标注。
DAQ_DEFAULTS = {
    "fs": 240e3,             # 采样率 Hz = 8×fm(30 kHz)：每调制周期整数 8 点（锁相要求）
    "n_samples": 120_000,    # 采样点数 = 240 kS/s × 0.5 s → 50 个扫描周期 @100 Hz
    "adc_bits": 16,          # ADC 位数（常见 USB DAQ：16-bit）
    "v_range": 10.0,         # ADC 输入量程 ±V（常见 USB DAQ：±10 V）
}
SCAN_DEFAULTS = {
    "scan_span_cm": 1.5,     # 三角波扫描的波数**半宽**（cm⁻¹）；amp_V = scan_span_cm/(η_VI·|dν/dI|)
    "freq_Hz": 100.0,        # 三角波频率 Hz（= 扫描速率）
    "phase_deg": 0.0,        # 起始相位°（中心对称、先上升后下降）
    # amp_V / offset_V 不写死：由 wn_center + scan_span_cm 经电压—波数关系反算（见 _resolve_scan）；
    # 用户显式给出 amp_V / offset_V 时尊重用户、不反算。
}
LASER_DEFAULTS = {
    "eta_VI": 24.0,          # V→I 跨导 mA/V（驱动器线性估算：Vop 5 V / Iop 120 mA）
    "dnu_dI": -0.088,        # I→波数 调谐系数 cm⁻¹/mA（官方 0.10 nm/mA @3373 nm 换算）
    "d2nu_dI2": 0.0,         # I→波数 二阶调谐系数 cm⁻¹/mA²（调谐非线性，默认 0=线性）
    "wn_ref": 2964.7,        # 参考波数 cm⁻¹（10⁷/3373 nm）
    "i_ref": 120.0,          # 参考电流 mA（Iop max）
    "i_th": 30.0,            # 阈值电流 mA（低于此无激光输出）
    "eta_IP": 0.15,          # I→P 斜率效率 mW/mA（工作点 71 mA→6.4 mW 反推）
    # 残余幅度调制 RAM：激光**固有**强度调制（与频率调制 FM 存在相位差）。
    # 真实 DFB 激光固有残余幅度调制（RAM）：I0(t)=1+i0·cos(ωt+ψ1)+i2·cos(2ωt+ψ2)。
    # 默认 am_i0=0.02（真实 DFB 典型量级 0.01–0.05，FEEDBACK-5）——此前默认 0=纯 FM
    # 是理想化，会低估 1f/2f 基线失真。am_psi1 取 π/2 使 RAM 与 FM 正交（典型实测相位）。
    "am_i0": 0.02,           # 1f RAM 强度调制幅度（相对光强，真实 DFB ~0.01–0.05）
    "am_i2": 0.0,            # 2f 强度调制幅度（通常远小于 1f）
    "am_psi1": np.pi / 2,    # RAM 相对 FM 相位差（rad），正交近似
    "am_psi2": 0.0,
}
OPTICS_DEFAULTS = {"throughput": 0.90}   # 光学元件总透过率（窗片/镜片/光纤/连接器，统一折成一个数）
# 说明：throughput 已将光路中所有透过率（镜片反射/镀膜/窗片/光纤耦合）折叠成单一标量；
# 有效光程 L（气体吸收路径）是**独立参数**，见 GAS_DEFAULTS["L_cm"]（多通池≈增大 L、并相应调 throughput）。
PD_DEFAULTS = {
    "resp": 0.9,             # 响应度 A/W（InGaAs）
    "gain": 700.0,           # 跨阻增益 V/A：适配默认光功率与 ±10 V 量程。
                             # 旧默认 1e3 时默认工况 v_pd≈10.9 V，超 ±10 V 量程 → ADC 默认即饱和
                             # （导致 tdlas_review 默认必 fail）；取 700 → v_pd≈7.7 V（~77% 满量程）。
    "bw": 1e6,               # 探测器 + 前放带宽 Hz
    "rin": 0.0,              # 相对强度噪声 /√Hz（默认关 = 理想仿真）
    "drift_frac": 0.0,       # 1/f 慢漂移幅度（默认关）
    "flicker_frac": 0.0,     # 1/f 粉红噪声幅度（默认关）
}
# etalon（法布里-珀罗干涉条纹）：两个平行且反射率不可忽略的光学面（窗片/透镜/光纤端面）
# 构成的 F-P 腔，在扫描中产生周期 = FSR 的透过率起伏。它是 TDLAS 痕量检测的**头号系统性误差**：
# 条纹的 2f 与吸收 2f 同形、可被误当吸收；且确定性条纹加噪声/多次平均都压不掉。
# **默认关闭**（fringe=False = 理想仿真）——绝不静默注入系统性误差，须用户显式开启。
FRINGE_DEFAULTS = {
    "fringe": False,          # 开关（默认关）
    "fringe_n": 1.5,          # 腔介质折射率（空气隙=1.0；玻璃/熔石英≈1.5）
    "fringe_d_cm": 0.5,       # 两平行面间距 cm（10 mm→1.0；5 mm→0.5）
    "fringe_R": 0.04,         # 单面反射率（未镀膜玻璃≈0.04；AR 镀膜≈0.005）
    "fringe_fsr": None,       # 直接给自由光谱范围 cm⁻¹（优先于 n/d）：FSR = 1/(2 n d)
    "fringe_contrast": None,  # 直接给条纹峰-峰对比度（优先于 R）：≈ 4R/(1−R)²
    "fringe_phase_rad": 0.0,  # 条纹初相位
    "fringe_drift_frac": 0.0, # 条纹相对参考谱的漂移幅度（相对 FSR，模拟温度致 d 漂移的残留）
}


def etalon_transmission(nu_cm1, fsr_cm1, contrast, phase_rad=0.0):
    """F-P etalon（两平行面）的 Airy 透过率 T(ν̃)。

        δ = 2π·ν̃/FSR + φ  ⇒  T = 1 / (1 + F·sin²(δ/2))，F = 4R/(1−R)² ≈ 条纹峰-峰对比度

    小反射率下 T ≈ 1 − F·sin²(δ/2)：即在光强上叠加相对幅度 ≈ F、周期 = FSR 的起伏。
    ν̃ 与 FSR 均用 cm⁻¹（FSR = 1/(2 n d)，d 用 cm）。
    """
    delta = (2.0 * np.pi * np.asarray(nu_cm1, dtype=float)
             / np.asarray(fsr_cm1, dtype=float) + float(phase_rad))
    f = max(float(contrast), 0.0)
    return 1.0 / (1.0 + f * np.sin(delta / 2.0) ** 2)


def resolve_fringe(cfg):
    """解析条纹参数 → (fsr_cm-1, contrast)；未启用或参数非法返回 None。

    FSR（cm⁻¹）优先取显式 `fringe_fsr`，否则由 n/d 反算 1/(2nd)；
    对比度优先取显式 `fringe_contrast`，否则由 R 反算 4R/(1−R)²。
    """
    if not cfg.get("fringe"):
        return None
    fsr = cfg.get("fringe_fsr")
    if fsr is None:
        n = float(cfg.get("fringe_n") or FRINGE_DEFAULTS["fringe_n"])
        d = float(cfg.get("fringe_d_cm") or FRINGE_DEFAULTS["fringe_d_cm"])
        if n <= 0 or d <= 0:
            return None
        fsr = 1.0 / (2.0 * n * d)
    try:
        fsr = float(fsr)
    except (TypeError, ValueError):
        return None
    if fsr <= 0:
        return None
    ctr = cfg.get("fringe_contrast")
    if ctr is None:
        r = cfg.get("fringe_R")
        r = FRINGE_DEFAULTS["fringe_R"] if r is None else float(r)
        r = min(max(r, 0.0), 0.999)
        ctr = 4.0 * r / (1.0 - r) ** 2
    return fsr, float(ctr)


def fringe_factor(nu_cm1, cfg, t=None, apply_drift=True):
    """光路中的 etalon 乘性透过率；未启用时恒为 1.0（完全不改变原行为）。

    apply_drift=True 用于**信号支路**：`fringe_drift_frac` 表示条纹相对参考谱的漂移
    （d 相对漂移 ε → FSR 按 1/(1+ε) 变化，等效条纹图案平移若干 FSR）；
    参考谱（背景 / DAS 的 I0）用 apply_drift=False，故确定性条纹被扣除、只留漂移残留——
    这正是实际中"确定性条纹能被扣除、漂移扣不掉"的物理来源。
    """
    res = resolve_fringe(cfg)
    if res is None:
        return 1.0
    fsr, contrast = res
    if apply_drift and t is not None:
        eps = float(cfg.get("fringe_drift_frac", 0.0) or 0.0)
        if eps != 0.0:
            tt = np.asarray(t, dtype=float)
            fsr = fsr / (1.0 + eps * np.sin(2.0 * np.pi * 1.0 * tt + 0.7))
    return etalon_transmission(nu_cm1, fsr, contrast, cfg.get("fringe_phase_rad", 0.0))
# WMS 调制参数（正弦调制）。调制幅值默认自动优化：使调制系数 m = a/HWHM ≈ 2.2，
# 这是 2f 谐波幅值最大、检测灵敏度最优的经典取值（Arndt 1965 / Reid & Labrie 1981）。
MOD_DEFAULTS = {
    "mod_amp_V": None,       # 正弦调制幅值 V；None = 自动按 m_opt 优化
    "mod_freq_Hz": 30e3,     # 调制频率 Hz
    "mod_phase_deg": 0.0,    # 调制相位°
    "m_opt": "auto",         # 目标调制系数 m = a/HWHM。"auto" = 自适应（粗扫+细扫，兼顾幅值与轮廓）；
                             # 也可给数值（如 2.2）禁用自适应。经典 2.2 = 洛伦兹线型纯 FM 的理论最优。
                             # 本项目实测：孤立单线(H2O 7185.6)最优≈2.2；密集多线区最优明显偏小
                             # （CH4 221 线最优≈1.25、C2H6 158 线≈1.8），可用 m_opt 调低以提升灵敏度。
    "lockin_avg": 1,         # 锁相滑动平均的调制周期数（>1 会模糊线形且不降残留）
    "lockin_stages": 2,      # 低通级联级数：2 级把残留从 4.1% 降到 1.6%（再高收益小）
    # ⚠ 默认 **关闭**：背景扣除是一次"替用户修正基线"的静默处理，默认关 = 出的是原始谱，
    #   由用户自己决定要不要扣。开启时用无吸收参考谱（τ≡1）复减 RAM/AM 基线。
    #   关闭时 2f/1f 保留 L-I 非线性与 RAM 的物理基线，仅供诊断，不用于浓度反演/LOD。
    "background_subtract": False,
    "trim_frac": 0.12,       # 剔除扫描两端比例：三角波转折点导数不连续，
                            # 其高频谐波会泄漏进 2f，必须丢弃（WMS 标准做法）
}

# 工况（气体 / 气室 / 环境）默认参数：用户最常改的就是这些，集中于此方便统一修改
GAS_DEFAULTS = {
    "species": "CH4",        # 待测气体（HITRAN 分子式，如 CH4 / H2O / CO2）
    "wn_center": 2968.5,     # 目标吸收线中心波数 cm⁻¹（决定激光调谐到哪条线）
    "T": 296.0,              # 温度 K
    "P": 1.01325,            # 气压 atm（标准大气压 ≈ 1.01325）
    "x": 1e-4,               # 摩尔分数（= 100 ppm；CH4@3.3μm 强吸收，默认 100 ppm 才落在弱吸收区）
    "L_cm": 50.0,            # 有效光程 cm（气室光程；多通池 ≈ 几十~几百 cm，等效增大 L）
}


def estimate_hwhm(nu, alpha):
    """从吸收谱主峰估半高半宽 HWHM（cm^-1）：主峰 3 dB 宽 / 2。"""
    nu = np.asarray(nu, dtype=float)
    alpha = np.asarray(alpha, dtype=float)
    if alpha.size < 3 or not np.any(alpha > 0):
        raise ValueError("吸收谱为空，无法估计线宽")
    i = int(np.argmax(alpha))
    half = 0.5 * float(alpha[i])
    li = i
    while li > 0 and alpha[li] > half:
        li -= 1
    ri = i
    while ri < alpha.size - 1 and alpha[ri] > half:
        ri += 1
    return 0.5 * float(nu[ri] - nu[li])


def optimal_mod_amp_V(nu, alpha, eta_VI, dnu_dI, m_target=2.2):
    """按最优调制系数 m≈2.2 反算正弦调制电压幅值 (V)。

    m = a / HWHM，a 为调制深度(cm^-1)；dν/dV = η_VI·dν/dI，故 mod_amp_V = a / |dν/dV|。
    返回 (mod_amp_V, a_cm-1, hwhm_cm-1, m_actual)。
    """
    hwhm = estimate_hwhm(nu, alpha)
    a = float(m_target) * hwhm
    dnu_dV = abs(float(eta_VI) * float(dnu_dI))
    if dnu_dV <= 0:
        raise ValueError("dν/dV 为零，无法换算调制电压")
    return a / dnu_dV, a, hwhm, float(m_target)


def adaptive_modulation_index(nu_grid, alpha_pure, hwhm, cfg, t, v_scan, v_mod_sig,
                              x, L_cm, n_lines=1, m_lo=1.0, m_hi=2.6, tol=0.98):
    """自适应调制系数 m（等价自适应正弦调制幅值）。

    三路并行：
      A 解析  —— 经典 Lorentz 2.2 基准，按谱线密度修正（密集区最优 m 偏小）
      D 模型  —— 对扫描曲线做二次拟合，取连续域峰值（模型化最优）
      B 扫描  —— 完整时域链路（无噪声，确定性）粗扫 + 细扫，实测最优 ← 采用

    选择策略「tol(95%) 幅值的最小 m」：2f 峰值附近是平顶，牺牲少量幅值换更小的 m，
    从而得到更好的 2f 轮廓、更低过调制风险、更小的邻线干扰。
    """
    fs = float(cfg["fs"])
    fm = float(cfg["mod_freq_Hz"])
    fscan = float(cfg["freq_Hz"])
    # ★ 扫描须在足够采样率下进行：fs/fm<16 时锁相正交不完备，直流/RAM 会泄漏进 2f，
    #   使"2f 峰值"主要反映强度调制 AM 而非真实吸收 2f（实测会让最优 m 判断完全相反）。
    fs_s = fs
    n_s = int(cfg["n_samples"])
    if fs / fm < 16.0:
        fs_s = 16.0 * fm
        n_s = int(round(n_s * fs_s / fs))          # 保持总采集时间不变
    t_s = np.arange(n_s, dtype=float) / fs_s
    v_scan_s = daq_triangle(t_s, cfg["amp_V"], fscan, cfg["phase_deg"], cfg["offset_V"])
    v_mod_s = np.cos(2.0 * np.pi * fm * t_s + np.deg2rad(float(cfg["mod_phase_deg"])))

    n_per = int(round(fs_s / fscan))
    if n_per < 16:
        raise ValueError(f"每扫描周期采样点太少（fs/fscan = {fs_s / fscan:.1f}）")
    half = n_per // 2
    edge = str(cfg.get("edge") or "rising").strip().lower()
    idx = (np.arange(half)[::-1] if edge == "rising"
           else np.arange(n_per) if edge == "both" else np.arange(half, n_per))
    n_sel = int(idx.size)
    n_keep = int(round(float(cfg["trim_frac"]) * n_sel))
    valid = np.ones(n_sel, dtype=bool)
    if n_keep > 0:
        valid[:n_keep] = False
        valid[-n_keep:] = False

    eta_VI = float(cfg["eta_VI"])
    dnu_dI = float(cfg["dnu_dI"])
    dnu_dV = abs(eta_VI * dnu_dI)
    avg = int(cfg["lockin_avg"])
    stg = int(cfg["lockin_stages"])
    lsb = 2.0 * float(cfg["v_range"]) / float(2 ** int(cfg["adc_bits"]))
    vr = float(cfg["v_range"])

    def _peak(m_val):
        """给定 m 跑无噪声链路，返回 (2f 峰值, 归一化波形)"""
        amp_V = (m_val * hwhm) / dnu_dV if dnu_dV > 0 else 0.0
        v_d = v_scan_s + amp_V * v_mod_s
        _, nu_l, p_l = voltage_to_laser(v_d, eta_VI, dnu_dI, cfg["wn_ref"], cfg["i_ref"],
                                        cfg["i_th"], cfg["eta_IP"], cfg.get("d2nu_dI2", 0.0))
        alpha = np.interp(nu_l, nu_grid, alpha_pure) * float(x)
        p_opt = p_l * float(cfg["throughput"]) * np.exp(-alpha * float(L_cm))
        v_pd = p_opt * 1e-3 * float(cfg["resp"]) * float(cfg["gain"])
        # ★ 必须与正式链路**同一传递函数**：此前这里漏了探测器带宽（pd_lowpass），
        #   于是"用扫描选出的最优 m"并非正式链路下的最优 —— 这是实数级缺陷，
        #   也是文档里 loss 百分比对不上的原因（扫描看不见 2f 被带宽削掉的那部分）。
        v_ana = pd_lowpass(v_pd, cfg["bw"], fs_s)
        v_adc = np.clip(np.round(v_ana / lsb) * lsb, -vr, vr)
        S2f, _, _ = wms_harmonic_lockin(v_adc, t_s, fs_s, fm, 2, avg, stg)
        s = np.abs(S2f[idx])[valid]
        return float(s.max()), s

    # B：粗扫（步长 0.2）→ 峰值附近细扫（步长 0.05）
    ms_c = np.round(np.arange(m_lo, m_hi + 1e-9, 0.2), 3)
    pk_c = np.array([_peak(m)[0] for m in ms_c])
    k = int(np.argmax(pk_c))
    lo = max(m_lo, round(ms_c[k] - 0.2, 3))
    hi = min(m_hi, round(ms_c[k] + 0.2, 3))
    ms_f = np.round(np.arange(lo, hi + 1e-9, 0.05), 3)
    pk_f = np.array([_peak(m)[0] for m in ms_f])
    ms = np.concatenate([ms_c, ms_f])
    pk = np.concatenate([pk_c, pk_f])
    o = np.argsort(ms)
    ms, pk = ms[o], pk[o]

    pmax = float(pk.max())
    if pmax <= 0:
        raise ValueError("2f 峰值全为 0（扫描区间激光无有效输出）：请检查 wn_center 与激光 "
                         "wn_ref/dν/dI 是否匹配（可能选了不在此 DFB 调谐范围内的线）。")
    ok = pk >= tol * pmax
    m_best = float(ms[ok][np.argmin(ms[ok])])

    # 多峰检测（密集区 2f_peak(m) 可能多峰）
    d1 = np.diff(pk)
    n_local_max = int(np.sum((d1[:-1] > 0) & (d1[1:] < 0)))

    # D：对扫描曲线二次拟合取连续域峰值
    m_model = float(m_best)
    if ms.size >= 3:
        try:
            c = np.polyfit(ms, pk, 2)
            if c[0] < 0:
                m_model = float(np.clip(-c[1] / (2 * c[0]), m_lo, m_hi))
        except Exception:
            pass

    # A：解析（Lorentz 2.2 基准 + 谱线密度修正）
    if n_lines > 50:
        m_analytic = 1.25
    elif n_lines > 10:
        m_analytic = 1.80
    else:
        m_analytic = 2.20

    return m_best, m_best * hwhm, hwhm, {
        "m_analytic": m_analytic,
        "m_model": round(m_model, 3),
        "m_scan": round(m_best, 3),
        "peak_max": pmax,
        "peak_at_m": float(pk[ok][np.argmin(ms[ok])]),
        "loss_pct": round(100 * (1 - float(pk[ok][np.argmin(ms[ok])]) / pmax), 2),
        "multipeak": n_local_max > 1,
        "n_local_max": n_local_max,
        "scan_m": [round(float(v), 3) for v in ms],
        "scan_peak": [float(v) for v in pk],
    }


def daq_triangle(t, amp_V, freq_Hz, phase_deg=0.0, offset_V=0.0):
    """DAQ 模拟输出通道的三角波驱动电压（中心对称、先上升后下降）。"""
    period = 1.0 / float(freq_Hz)
    ph = (np.asarray(t, dtype=float) / period + float(phase_deg) / 360.0) % 1.0
    tri = 1.0 - 4.0 * np.abs(ph - 0.5)                 # 0→-1, 0.5→+1, 1→-1（先升后降）
    return float(offset_V) + float(amp_V) * tri


def voltage_to_laser(v, eta_VI=24.0, dnu_dI=-0.088, wn_ref=2964.7, i_ref=120.0,
                     i_th=30.0, eta_IP=0.15, d2nu_dI2=0.0):
    """驱动电压 → 激光器电流 / 波数 / 输出功率。返回 (i_mA, nu_cm-1, p_mW)。

    波数用二阶泰勒：ν = wn_ref + di·(dν/dI) + ½·di²·(d²ν/dI²)，di = i - i_ref。
    `d2nu_dI2` 建模**调谐非线性**（真实 DFB 的电流-波长响应并非严格线性），默认 0。
    """
    i = float(eta_VI) * np.asarray(v, dtype=float)
    di = i - float(i_ref)
    nu = float(wn_ref) + di * float(dnu_dI) + 0.5 * di ** 2 * float(d2nu_dI2)
    p = np.maximum(float(eta_IP) * (i - float(i_th)), 0.0)   # 低于阈值电流无输出
    return i, nu, p


DRIVER_V_LO, DRIVER_V_HI = 0.0, 5.0        # 驱动器输出电压典型范围（v_lo/v_hi）
_V_EPS = 1e-9                              # 边界容差：offset_V 恰好落在 0 或 5 V 属合法


def _scan_window_reason(off, amp, v_lo_eff, v_hi):
    """「整段扫描是否都在有效输出区」的**唯一判据** —— `laser_reach()` 与引擎硬校验共用。

    为什么单独抽出来：判据曾在两处各写一遍（一处只看偏移、一处只看中心），于是
    2971.0–2974.1 cm⁻¹ 这一档**不报错、但扫描波形被截断**（2f/1f 静默失真）。共用后不可能再漂。

    返回 None 表示整段出光；否则返回不可达原因：
      out_of_driver_range         偏置本身就越出驱动器上限（连中心都不在量程内）
      scan_exceeds_driver_voltage 扫描上端超出驱动器上限（中心合法、幅值顶出去）
      below_threshold_current     偏置低于阈值电压（全段无光）
      scan_partially_dark         偏置在阈值之上但扫描下端落进暗区（部分出光 → 静默失真）
    """
    if off > v_hi + _V_EPS:
        return "out_of_driver_range"
    if off + amp > v_hi + _V_EPS:
        return "scan_exceeds_driver_voltage"
    if off - amp < v_lo_eff - _V_EPS:
        return "below_threshold_current" if off < v_lo_eff - _V_EPS else "scan_partially_dark"
    return None


def laser_reach(wn_center, eta_VI=24.0, dnu_dI=-0.088, i_ref=120.0, wn_ref=2964.7,
                scan_span_cm=1.5, d2nu_dI2=0.0, i_th=30.0,
                v_lo=DRIVER_V_LO, v_hi=DRIVER_V_HI):
    """判断目标波数能否被该激光器 + 驱动器达到；给出「需要的 wn_ref」与不可达原因。

    判据 = **整段扫描**都落在有效输出区（高于阈值电压 `i_th/η_VI`、且不出驱动器上限），
    由 `_scan_window_reason()` 单点实现。只要求"扫描中心出光"是不够的：扫描段有一段是黑的时，
    锁相解调的是被截断的波形，2f/1f 归一化失真，而引擎**不会报错** —— 属"不报错但结果不可信"的静默档。

    这是**离线可算**的判据（纯算术，不依赖线表），因此既用于报错提示，也用于：
      · MCP 层在目标波数越界时自动重锚 wn_ref（详见 tools/tdlas_mcp.py 的 _maybe_align_laser）；
      · 引擎侧 `_require_driver_range()` 的硬校验（共用 `_scan_window_reason`，避免两处漂移）；
      · 回归测试对 SPECIES_PROFILES 的每个推荐波段做"重锚后整段出光"断言。

    ⚠ 本函数只回答"**按 wn_center 反算出的**工作点是否可行"。用户显式给 offset_V 时扫描中心
    由 offset_V 决定、wn_center 只是标签，那种情况下**不可**用本函数替代对实际偏置的校验。

    返回 dict：
      reachable        整段扫描是否都在有效输出区（= 稳定可定量），此为最终判据
      offset_V         当前参数反算出的三角波偏置
      amp_V            三角波幅值
      v_lo_eff         有效电压下限 = max(v_lo, i_th/η_VI)
      wn_ref_needed    把工作点移到量程中点所需的 wn_ref（重锚建议）
      fits_span        重锚后扫描半宽是否仍在量程内（过大扫宽救不了）
      reason           不可达原因（可达时为 "ok"）：
                       out_of_driver_range / scan_exceeds_driver_voltage /
                       below_threshold_current / scan_partially_dark /
                       quadratic_unreachable / span_too_wide / invalid_laser_params
                       —— 前四个由 `_scan_window_reason()` 给出
      discriminant     仅二次模型（d2nu_dI2≠0）时有值
      nu_extremum      二次模型的可达极值（不可达时说明"最多只能到哪"）
    """
    eta_VI = float(eta_VI); dnu_dI = float(dnu_dI); i_ref = float(i_ref)
    wn_ref = float(wn_ref); span = float(scan_span_cm); d2 = float(d2nu_dI2)
    v_lo = float(v_lo); v_hi = float(v_hi)
    c = wn_ref - float(wn_center)

    info = {"reachable": False, "offset_V": None, "amp_V": None, "wn_ref_needed": None,
            "fits_span": None, "reason": "ok", "discriminant": None, "nu_extremum": None}
    if eta_VI <= 0 or abs(dnu_dI) < 1e-15:
        info["reason"] = "invalid_laser_params"
        return info

    # 有效电压下限还要受**阈值电流**约束：V 落在 0–5 V 内并不代表激光出光，
    # 必须 i = η_VI·V > i_th 才有输出（实测踩到：offset_V≈0 时引擎报"功率≤0"）。
    v_lo_eff = max(v_lo, float(i_th) / eta_VI)
    info["v_lo_eff"] = v_lo_eff

    # 扫描半宽与"重锚后的 wn_ref"永远可算（平移 wn_ref 不改变曲率），先算出来：
    # 这样即使当前参数不可达，调用方也能拿到可执行的修复建议，而不是拿到 None。
    amp_V = span / abs(eta_VI * dnu_dI)
    v_mid = 0.5 * (v_lo_eff + v_hi)
    di_mid = eta_VI * v_mid - i_ref
    # 让"参考电流处波数 = 目标波数"这条要求落在量程中点：解 ν(di_mid) = wn_center 的截距。
    # ⚠ 必须带二次项：只用线性式（漏掉 ½·di_mid²·d²ν/dI²）会算出错误的 wn_ref，
    #   在 d²ν/dI²≠0 时重锚后依然不可达（自检曾抓到该错误）。
    wn_ref_needed = float(wn_center) - di_mid * dnu_dI - 0.5 * di_mid ** 2 * d2
    fits_span = (v_mid - amp_V >= v_lo_eff - _V_EPS) and (v_mid + amp_V <= v_hi + _V_EPS)
    info.update({"amp_V": amp_V, "wn_ref_needed": wn_ref_needed, "fits_span": fits_span})
    if not fits_span:
        info["reason"] = "span_too_wide"
        return info

    if abs(d2) < 1e-15:
        di = -c / dnu_dI
    else:
        disc = dnu_dI * dnu_dI - 2.0 * d2 * c
        info["discriminant"] = disc
        if disc < 0.0:                       # 二次调谐曲线取不到该波数（重锚 wn_ref 可解）
            info["nu_extremum"] = wn_ref - dnu_dI * dnu_dI / (2.0 * d2)
            info["reason"] = "quadratic_unreachable"
            return info
        r1 = (-dnu_dI + np.sqrt(disc)) / d2
        r2 = (-dnu_dI - np.sqrt(disc)) / d2
        di = r1 if abs(r1) <= abs(r2) else r2

    offset_V = (di + i_ref) / eta_VI
    info["offset_V"] = offset_V
    # 判据：**整段扫描**都要在阈值之上、且不出驱动器电压上限（唯一实现见 _scan_window_reason）。
    _why = _scan_window_reason(offset_V, amp_V, v_lo_eff, v_hi)
    if _why is None:
        info["reachable"] = True
    else:
        info["reason"] = _why
    return info


def _resolve_scan(cfg, wn_center, user_keys):
    """由「目标波数 + 扫描半宽」经电压—波数关系反算三角波幅值/偏置。

    物理量级关系：电压→电流→波数是线性（或二阶）映射，而**波数才是用户的自然坐标**，
    电压是其从变量。故 amp_V / offset_V 不写死，而由 wn_center + scan_span_cm 反算：
      · amp_V    = scan_span_cm / |η_VI·dν/dI|   （半宽 → 电压幅值）
      · offset_V 解 wn_center = wn_ref + (i−i_ref)·dν/dI + ½(i−i_ref)²·d²ν/dI²，再 V=(i+i_ref)/η_VI
    若用户显式给了 amp_V / offset_V（在 user_keys 中），则尊重用户、不反算。
    """
    eta_VI = float(cfg["eta_VI"]); dnu_dI = float(cfg["dnu_dI"])
    d2 = float(cfg.get("d2nu_dI2", 0.0)); i_ref = float(cfg["i_ref"]); wn_ref = float(cfg["wn_ref"])
    dnu_dV = abs(eta_VI * dnu_dI)

    if "amp_V" not in user_keys:
        cfg["amp_V"] = float(cfg["scan_span_cm"]) / dnu_dV if dnu_dV > 0 else 0.0

    if "offset_V" not in user_keys:
        c = wn_ref - float(wn_center)
        if abs(d2) < 1e-15:
            di = -c / dnu_dI if abs(dnu_dI) > 1e-15 else 0.0
        else:
            # 0.5·d2·di² + dν/dI·di + (wn_ref − wn_center) = 0
            disc = dnu_dI * dnu_dI - 2.0 * d2 * c
            if disc < 0.0:
                # 判别式 < 0 ⇒ 该二次调谐曲线取不到目标波数。此前用 max(disc, 0) 截断成"重根"，
                # 会凭空造出一个虚假的驱动电压、下游照常出谱（静默伪造），必须硬报错。
                nu_ext = wn_ref - dnu_dI * dnu_dI / (2.0 * d2)   # 调谐曲线顶点（可达极值）
                reach = f"≤ {nu_ext:.6g}" if d2 < 0 else f"≥ {nu_ext:.6g}"
                raise ValueError(
                    f"目标波数 {float(wn_center):g} cm⁻¹ 在二次调谐模型下不可达："
                    f"ν = wn_ref + (dν/dI)·Δi + ½(d²ν/dI²)·Δi² 的判别式 {disc:.4g} < 0，方程无实根，"
                    f"该曲线最多只能到 ν {reach} cm⁻¹"
                    f"（wn_ref={wn_ref:g}, dν/dI={dnu_dI:g}, d²ν/dI²={d2:g}）。"
                    f"请核对 wn_ref / dν/dI 的数值与符号、d²ν/dI² 是否合理；"
                    f"若该波数本应可达，可置 d2nu_dI2=0 改用线性调谐模型。"
                    f"（MCP 层默认 auto_laser=True，会自动把 wn_ref 重锚到该波数并如实标注；"
                    f"这里见到此错说明 auto_laser 被关掉或直接调用了引擎。）")
            r1 = (-dnu_dI + np.sqrt(disc)) / d2
            r2 = (-dnu_dI - np.sqrt(disc)) / d2
            di = r1 if abs(r1) <= abs(r2) else r2     # 取离 0 近的根（更合理驱动电流）
        cfg["offset_V"] = (di + i_ref) / eta_VI if eta_VI > 0 else 0.0


def _require_driver_range(cfg, wn_center, offset_derived=True):
    """激光/驱动可行性校验 → 不可行则硬报错（含可达性诊断与可操作建议）。

    判据 = **整段扫描都在有效输出区**，由 `_scan_window_reason()` 单点实现（与 `laser_reach()`
    同一套），且**用 cfg 里实际生效的 offset_V / amp_V**：

    ⚠ 这里必须用实际值，不能拿 `laser_reach(wn_center)` 的结果代替。`_resolve_scan` 只在
    "用户没显式给 offset_V" 时才由 wn_center 反算偏置；用户一旦显式给了 offset_V，
    **扫描中心就由 offset_V 决定、wn_center 只是标签**。此时若用 wn_center 反算的偏置去校验，
    会同时错两个方向：显式合法的 offset_V 被误拒；显式越限的 offset_V（如 9 V）被放行
    → 引擎照常出谱（ν 轴跑到线表窗口之外，只剩 ADC 饱和之类的下游症状提示）。两条都实测到过。

    `offset_derived=False`（用户显式给了 offset_V）时**不再附加 wn_ref 重锚建议** —— 重锚
    改变不了用户指定的偏置，给出来的建议是误导。

    边界留 1e-9 容差：目标波数恰好落在端点时，浮点误差会把 5.0 算成 5.0000000001，
    严格比较会把**合法**工况误拒（实测 wn_center=2975.26 得 offset_V=-1.89e-13 V 就被拒了）。
    """
    off = float(cfg["offset_V"])
    amp = float(cfg.get("amp_V") or 0.0)
    eta_VI = float(cfg["eta_VI"])
    i_th = float(cfg.get("i_th", 30.0))
    vth = i_th / eta_VI if eta_VI > 0 else 0.0
    v_lo_eff = max(DRIVER_V_LO, vth)
    reason = _scan_window_reason(off, amp, v_lo_eff, DRIVER_V_HI)

    hint = ""
    if offset_derived:
        # 扫描中心确由 wn_center 反算 → 重锚 wn_ref 才是有效解法，附上建议值。
        dnu_dV = abs(eta_VI * float(cfg["dnu_dI"]))
        span_cm = abs(amp) * dnu_dV or float(cfg.get("scan_span_cm", 1.5))
        r = laser_reach(wn_center, eta_VI=eta_VI, dnu_dI=cfg["dnu_dI"], i_ref=cfg["i_ref"],
                        wn_ref=cfg["wn_ref"], scan_span_cm=span_cm,
                        d2nu_dI2=cfg.get("d2nu_dI2", 0.0), i_th=i_th)
        if r.get("reason") == "invalid_laser_params":
            raise ValueError(
                f"激光参数非法：η_VI={eta_VI:g} 必须 >0、dν/dI={float(cfg['dnu_dI']):g} 必须非零"
                f"（否则电压与波数之间没有可用的映射）。")
        if not r.get("fits_span"):
            raise ValueError(
                f"扫描半宽 {span_cm:g} cm⁻¹ 需要 ±{r['amp_V']:.3g} V 驱动，而可用区间只有 "
                f"{v_lo_eff:.3g}–{DRIVER_V_HI:.0f} V → 任何 wn_ref 都放不下。"
                f"请减小 scan_span_cm 或核对 η_VI/i_th/dν/dI。")
        if r.get("wn_ref_needed") is not None:
            # ⚠ 原文案建议"传 laser= 你的真实激光器参数"——但当调用方**已经用了真实器件**时
            #   这句话是错的（实测踩到：用户的 nanoplus 覆盖 2963.5–2966.5、内置默认 DFB
            #   覆盖 2966.5–2971.0，两者正好错开——"换真实参数"反而更差）。
            #   改为给三条可执行方案，并明确点出最常用的"改波数"这一条。
            hint = (f" 该目标波数需 wn_ref≈{r['wn_ref_needed']:.6g} cm⁻¹。"
                    f"可行方案：① 改选本激光器可达范围内的目标线"
                    f"（最建议：真实器件的可达区通常就在 wn_ref 附近）；"
                    f"② 换一台 wn_ref 接近该波数的激光器（用 tdlas_device save 建库）；"
                    f"③ 若只是要看想象中的理想激光器，用 MCP 层的 auto_laser=True"
                    f"（仅重锚 wn_ref，会如实标注为非真实硬件）。")

    if reason is None:
        return
    _win = f"[{off - amp:.4g}, {off + amp:.4g}] V"
    if reason == "below_threshold_current" and off < DRIVER_V_LO - _V_EPS:
        # 偏置连**驱动器下限**都没到（不只是阈值以下）：目标波数整体在调谐范围之外
        raise ValueError(
            f"offset_V={off:.4g} V 低于驱动器下限 {DRIVER_V_LO:.0f} V（扫描区间 {_win}）："
            f"wn_center={float(wn_center):g} cm⁻¹ 已不在此激光器/驱动器的输出范围内"
            f"（wn_ref={cfg['wn_ref']:g}, dν/dI={cfg['dnu_dI']:g}）。"
            f"请改用对应激光器参数（wn_ref/dν/dI/eta_VI）或手填 offset_V。{hint}")
    if reason in ("scan_partially_dark", "below_threshold_current"):
        # 显式接受暗段（allow_partial_dark=True）时放行：用户可能正想看"扫描越界后实际长什么样"。
        # 但**全段无光**永远是错的（引擎随后的功率检查也会拦），所以只对"部分出光"放行。
        if bool(cfg.get("allow_partial_dark")) and (off + amp) > vth:
            return
        raise ValueError(
            f"扫描区间 {_win} 未整段高于阈值电压 {vth:.3g} V（offset_V={off:.4g} V、"
            f"幅值 {amp:.4g} V，阈值电流 {i_th:g} mA）：扫描段无光会让 2f/1f 失真且**不会报错**，"
            f"故默认拒绝。可选：① 抬高 offset_V 或减小 scan_span_cm 让整段落进有效区；"
            f"② 改用对应激光器参数（wn_ref/dν/dI/eta_VI）；"
            f"③ 若你明知有暗段仍要算，显式传 allow_partial_dark=True。{hint}")
    if reason == "scan_exceeds_driver_voltage":
        raise ValueError(
            f"扫描上端 = offset_V + 幅值 = {off:.4g} + {amp:.4g} = {off + amp:.4g} V 超出驱动器上限 "
            f"{DRIVER_V_HI:.0f} V（扫描区间 {_win}）。请减小 scan_span_cm 或降低 offset_V。")
    raise ValueError(
        f"offset_V={off:.4g} V 越出驱动器上限 {DRIVER_V_HI:.0f} V（扫描区间 {_win}）："
        f"wn_center={float(wn_center):g} cm⁻¹ 已不在此激光器/驱动器的输出范围内"
        f"（wn_ref={cfg['wn_ref']:g}, dν/dI={cfg['dnu_dI']:g}）。"
        f"请改用对应激光器参数（wn_ref/dν/dI/eta_VI）或手填 offset_V。{hint}")


def pink_noise(n, fs, rng, f_lo=1.0):
    """生成 1/f（粉红）噪声序列：频域给白噪声加权 1/sqrt(f)，再逆变换。

    真实 RIN / 激光低频漂移的功率谱近似 1/f，在 DC 附近最强、高频衰减。
    DAS 直接测 DC 光强、受害于 1/f；WMS 把信号搬到 fm（如 30 kHz）则天然避开。
    """
    freqs = np.fft.rfftfreq(int(n), d=1.0 / float(fs))
    freqs[0] = float(f_lo)
    spec = (rng.standard_normal(len(freqs)) + 1j * rng.standard_normal(len(freqs)))
    spec = spec / np.sqrt(freqs)
    x = np.fft.irfft(spec, n=int(n))
    return x / (np.std(x) + 1e-30)          # 归一化到单位标准差


def noise_currents(i_mean_A, bw_Hz, gain_V_per_A, rin_per_sqrtHz, T=296.0):
    """PD + 前放的等效输入电流噪声密度（A）：散粒 + 热（跨阻）+ 激光 RIN。"""
    q, kb = 1.602176634e-19, 1.380649e-23
    i = abs(float(i_mean_A))
    shot = np.sqrt(2.0 * q * i * float(bw_Hz))
    thermal = np.sqrt(4.0 * kb * float(T) * float(bw_Hz) / float(gain_V_per_A))
    rin = i * float(rin_per_sqrtHz) * np.sqrt(float(bw_Hz))
    return shot, thermal, rin


def pd_lowpass(v, bw_Hz, fs):
    """探测器 + 前放的**带宽限制**（一阶 RC 低通）。

    真实 PD 有响应时间，会削掉高于带宽的信号分量（此前 bw 只参与噪声计算，
    对信号完全无作用 → 参数定义了却不生效）。截止取 min(bw, 0.45·fs) 避免越 Nyquist。
    """
    from scipy import signal as _sg
    fc = min(float(bw_Hz), 0.45 * float(fs))
    alpha = 1.0 - np.exp(-2.0 * np.pi * fc / float(fs))
    return _sg.lfilter([alpha], [1.0, -(1.0 - alpha)], np.asarray(v, dtype=float))


def simulate_das_instrument(species=GAS_DEFAULTS["species"], wn_center=GAS_DEFAULTS["wn_center"],
                            T=GAS_DEFAULTS["T"], P=GAS_DEFAULTS["P"], x=GAS_DEFAULTS["x"],
                            L_cm=GAS_DEFAULTS["L_cm"], edge="rising", fit_order=3,
                            fit_frac=0.3, seed=None, **kw):
    """完整仪器链路 DAS 仿真：DAQ 电压 → 激光 → 光路 → PD → ADC → 基线扣除。

    链路：
      ① DAQ 输出三角波电压 V(t)（幅值 / 频率 / 相位 / 偏置）
      ② V→I→ν/P：驱动器跨导 → 电流 → 波数调谐 + 功率
      ③ 光路：光学元件透过率 η_opt × 气体吸收 exp(-αL)
      ④ PD：响应度 R → 光电流 → 跨阻增益 G → 电压
      ⑤ 噪声：散粒 + 热 + RIN（等效输入电流噪声 → 电压噪声）
      ⑥ ADC：按位数与量程量化（含饱和/动态范围检查）
      ⑦ 取一个扫描周期 → 多项式基线拟合扣除 → DAS 吸光度

    参数 kw 可覆盖任何内置默认（见 DAQ_DEFAULTS / SCAN_DEFAULTS / LASER_DEFAULTS /
    OPTICS_DEFAULTS / PD_DEFAULTS 的键名）。
    返回 dict：t, v_drive, i_laser, nu_laser, p_laser, p_opt, v_pd, v_adc,
                nu_das, das, das_full, baseline, meta
    """
    cfg = {**DAQ_DEFAULTS, **SCAN_DEFAULTS, **LASER_DEFAULTS,
           **OPTICS_DEFAULTS, **PD_DEFAULTS, **GAS_DEFAULTS, **FRINGE_DEFAULTS}
    user_keys = set(kw)
    mixture = kw.pop("mixture", None)          # 混合气组分（不并入 cfg，单列处理）
    cfg.update({k: v for k, v in kw.items() if v is not None})
    # 显式位置参数覆盖默认（不传则用 GAS_DEFAULTS）
    cfg["species"], cfg["wn_center"] = species, wn_center
    cfg["T"], cfg["P"], cfg["x"], cfg["L_cm"] = T, P, x, L_cm
    species = cfg["species"]; wn_center = cfg["wn_center"]
    T, P, x, L_cm = cfg["T"], cfg["P"], cfg["x"], cfg["L_cm"]
    # 由 wn_center + scan_span_cm 反算 amp_V / offset_V（电压是波数的从变量）
    _resolve_scan(cfg, wn_center, user_keys)

    fs = float(cfg["fs"])
    n_samples = int(cfg["n_samples"])
    period = 1.0 / float(cfg["freq_Hz"])
    t = np.arange(n_samples, dtype=float) / fs

    # ① DAQ 三角波驱动电压
    v_drive = daq_triangle(t, cfg["amp_V"], cfg["freq_Hz"], cfg["phase_deg"], cfg["offset_V"])

    # ② V → I → 波数 / 功率
    i_laser, nu_laser, p_laser = voltage_to_laser(
        v_drive, cfg["eta_VI"], cfg["dnu_dI"], cfg["wn_ref"], cfg["i_ref"],
        cfg["i_th"], cfg["eta_IP"], cfg.get("d2nu_dI2", 0.0))
    if float(np.max(p_laser)) <= 0.0:
        raise ValueError(f"扫描区间内激光功率≤0：offset_V={cfg['offset_V']:.3g} V 可能越出驱动器 0–5 V 范围，"
                         f"或 wn_center 与激光 wn_ref/dν/dI 不匹配（选了不在此 DFB 调谐范围内的线）。"
                         f"请改用对应激光器参数（wn_ref/dν/dI/eta_VI）或手填 offset_V。")

    # ③ 光路：光学损耗 + 气体吸收（混合气 α=Σ x_i·α_pure_i，参考 hitran-mcp）
    span = float(cfg["scan_span_cm"])
    _step = min(5e-4, span / 500.0)
    _wing = max(10.0, 5.0 * span)
    if mixture is None:
        nu_grid, alpha_pure, info = th.absorption(
            species, wn_center - span - 0.2, wn_center + span + 0.2, T=T, P=P,
            step=_step, wingHW=_wing)
        _abs_grid = alpha_pure * float(x)
    else:
        _specs = _resolve_mixture_specs(species, x, mixture)
        nu_grid, alpha_pure, _per = _alpha_grid(
            _specs, wn_center - span - 0.2, wn_center + span + 0.2, T, P, _step, _wing)
        _abs_grid = alpha_pure                       # 已含 Σ x_i 缩放
        info = {"n_lines_in_window": sum(p["info"].get("n_lines_in_window", 0) for p in _per),
                "species_list": [p["species"] for p in _per], "per_species": _per,
                "mixture": [{"species": s, "x": xi} for s, xi in _specs]}
    alpha = np.interp(nu_laser, nu_grid, _abs_grid)
    tau = np.exp(-alpha * float(L_cm))
    _fr = resolve_fringe(cfg)                                 # etalon 条纹参数（未启用为 None）
    p_opt = (p_laser * float(cfg["throughput"]) * fringe_factor(nu_laser, cfg, t) * tau)   # 到达 PD 的光功率 mW

    # ④ PD：光功率 → 光电流 → 跨阻电压（含带宽低通）
    i_pd = p_opt * 1e-3 * float(cfg["resp"])                  # A
    v_pd = pd_lowpass(i_pd * float(cfg["gain"]), cfg["bw"], fs)   # V

    # ⑤ 噪声
    rng = np.random.default_rng(seed)
    # physical_noise=False → 严格理想仿真：连散粒/热也不注入（无噪理论对照）
    if bool(cfg.get("physical_noise", True)):
        s_shot, s_therm, s_rin = noise_currents(float(np.mean(i_pd)), cfg["bw"],
                                                cfg["gain"], cfg["rin"], T)
        s_total = float(np.sqrt(s_shot ** 2 + s_therm ** 2 + s_rin ** 2))
        v_noisy = v_pd + rng.normal(0.0, s_total * float(cfg["gain"]), v_pd.shape)
    else:
        s_shot = s_therm = s_rin = 0.0
        v_noisy = np.array(v_pd, dtype=float)

    # ⑥ ADC 量化（含饱和统计）
    lsb = 2.0 * float(cfg["v_range"]) / float(2 ** int(cfg["adc_bits"]))
    n_sat = int(np.sum(np.abs(v_noisy) >= float(cfg["v_range"])))
    v_adc = np.clip(np.round(v_noisy / lsb) * lsb, -float(cfg["v_range"]), float(cfg["v_range"]))

    # ⑦ 取一个扫描周期 → 基线拟合扣除 → DAS
    n_per = int(round(fs * period))
    if n_per < 16:
        raise ValueError(f"每周期采样点太少（fs/freq = {fs * period:.1f}），请提高 fs 或降低 freq")
    sl = slice(0, n_per)
    wn_cyc = nu_laser[sl]
    wn_c = wn_cyc - float(wn_center)                          # 中心化改善拟合条件数
    v_cyc = v_adc[sl]
    mask = np.abs(wn_c) > span * (1.0 - float(fit_frac))
    if int(mask.sum()) > int(fit_order) + 1:
        coef = np.polyfit(wn_c[mask], v_cyc[mask], int(fit_order))
        baseline = np.polyval(coef, wn_c)
    else:
        baseline = np.full_like(v_cyc, float(np.mean(v_cyc[mask])))
    das_full = -np.log(np.maximum(v_cyc, 1e-12) / np.maximum(baseline, 1e-12))

    # ★ 密集谱区基线拟合失效回退：两端"无吸收区"不干净时，多项式把吸收线拟合进基线，
    #   使 das 峰值被放大数十万倍。拟合后校验，异常则改用已知 I0（τ≡1 走同一条 PD/ADC 链路）。
    alphaL_cyc = np.interp(wn_cyc, nu_grid, _abs_grid) * float(L_cm)
    baseline_fallback = False
    _bmax = float(np.max(baseline))
    _ratio = (float(np.max(v_cyc)) / _bmax) if _bmax > 0 else 0.0
    _aL_th = float(np.max(alphaL_cyc))
    if _ratio < 0.9 or abs(float(np.max(das_full)) - _aL_th) > 0.5 * max(_aL_th, 1e-30):
        baseline_fallback = True
        p_opt_bg = (p_laser * float(cfg["throughput"])         # 无吸收光功率（τ≡1）
                    * fringe_factor(nu_laser, cfg, t, apply_drift=False))   # 同一光路 → 含确定性条纹
        v_pd_bg = pd_lowpass(p_opt_bg * 1e-3 * float(cfg["resp"]) * float(cfg["gain"]),
                             cfg["bw"], fs)
        v_bg_adc = np.clip(np.round(v_pd_bg / lsb) * lsb,
                           -float(cfg["v_range"]), float(cfg["v_range"]))
        I0_das = v_bg_adc[sl]
        baseline = I0_das
        das_full = -np.log(np.maximum(v_cyc, 1e-12) / np.maximum(I0_das, 1e-12))

    # 按 edge 选取：电压前半周期为"上升沿"（注意 dν/dI<0 → 波长是先降后升，故需注明）
    edge = str(edge).strip().lower()
    if edge not in ("rising", "falling", "both", "average"):
        raise ValueError(f"edge 必须是 rising / falling / both / average，收到 {edge!r}")
    half = n_per // 2
    if edge == "rising":
        nu_das, das = wn_cyc[:half], das_full[:half]
    elif edge == "falling":
        nu_das, das = wn_cyc[half:][::-1], das_full[half:][::-1]
    elif edge == "average":
        nu_das = wn_cyc[:half]
        das = 0.5 * (das_full[:half] + das_full[half:][::-1])
    else:
        nu_das, das = wn_cyc, das_full

    # 主动检查：吸收强弱、ADC 饱和、动态范围、噪声量级
    warnings = []
    if baseline_fallback:
        warnings.append("baseline fallback: dense spectrum, used known I0")
    _require_driver_range(cfg, wn_center, "offset_V" not in user_keys)
    _x_disp = f"{x:g}" if mixture is None else "混合气"
    aL_peak = float(alpha.max()) * float(L_cm)
    if aL_peak > 0.1:
        warnings.append(f"αL≈{aL_peak:.3g} 偏大（>0.1）：DAS 进入非线性区、峰形被压低，"
                        f"建议降低浓度或光程（x={_x_disp}, L={L_cm:g} cm）")
    elif aL_peak < 1e-4:
        warnings.append(f"αL≈{aL_peak:.3g} 偏小（<1e-4）：吸收太弱、信噪比可能不足")
    if n_sat > 0:
        warnings.append(f"ADC 饱和：{n_sat} 个采样点超出 ±{cfg['v_range']:g} V 量程 → "
                        f"请降低 PD 跨阻增益（当前 {cfg['gain']:g} V/A）或光功率")
    v_max = float(np.max(np.abs(v_adc)))
    if v_max < 0.05 * float(cfg["v_range"]):
        warnings.append(f"PD 信号仅 {v_max:.3g} V，远小于量程 ±{cfg['v_range']:g} V → "
                        f"ADC 动态范围浪费，可提高跨阻增益（当前 {cfg['gain']:g} V/A）")
    v_noise_rms = float(np.std(v_noisy - v_pd))
    warnings.append(f"输出电压噪声 RMS≈{v_noise_rms * 1e3:.3g} mV（散粒 {s_shot * cfg['gain'] * 1e3:.3g} / "
                    f"热 {s_therm * cfg['gain'] * 1e3:.3g} / RIN {s_rin * cfg['gain'] * 1e3:.3g} mV）")

    _mx = info.get("mixture") or [{"species": str(species).upper(), "x": float(x)}]
    _sp = (str(species).upper() if mixture is None
           else "+".join(f"{d['species']}@{d['x']:g}" for d in _mx))
    meta = {"species": _sp, "wn_center": float(wn_center), "T": float(T),
            "P": float(P), "x": (float(x) if mixture is None else "混合气"),
            "L_cm": float(L_cm), "edge": edge,
            "mixture": _mx,
            "span_cm-1": span, "scan_lo": float(nu_laser.min()),
            "scan_hi": float(nu_laser.max()), "alpha_L_peak": aL_peak,
            "baseline_fallback": baseline_fallback,
            "v_pd_mean": float(np.mean(v_pd)), "lsb_V": lsb, "n_sat": n_sat,
            "sigma_v_noise": v_noise_rms, "sigma_shot": s_shot * cfg["gain"],
            "sigma_thermal": s_therm * cfg["gain"], "sigma_rin": s_rin * cfg["gain"],
            "physical_noise": bool(cfg.get("physical_noise", True)), "seed_used": seed,
            "cfg": dict(cfg), "warnings": warnings,
            "fringe": ({"enabled": True, "fsr_cm-1": _fr[0], "contrast": _fr[1],
                        "n": cfg.get("fringe_n"), "d_cm": cfg.get("fringe_d_cm"),
                        "R": cfg.get("fringe_R"), "phase_rad": cfg.get("fringe_phase_rad"),
                        "drift_frac": cfg.get("fringe_drift_frac")} if _fr else {"enabled": False}),
            "n_lines_in_window": info["n_lines_in_window"], "table": info.get("table"),
            "alpha_context": info.get("alpha_context")}
    out = {"t": t, "v_drive": v_drive, "i_laser": i_laser, "nu_laser": nu_laser,
           "p_laser": p_laser, "p_opt": p_opt, "v_pd": v_pd, "v_adc": v_adc,
           "nu_das": nu_das, "das": das, "das_full": das_full, "baseline": baseline,
           "meta": meta}
    if mixture is not None:                    # 各组分 αL（对齐 DAS 横轴，供标准图叠加）
        out["alphaL_per_species"] = [
            {"label": f"{p['species']}@{p['x']:g}",
             "alphaL": np.interp(nu_das, nu_grid, p["alpha_pure"]) * p["x"] * float(L_cm)}
            for p in info["per_species"]]
    return out


def plot_das_instrument(r, out_png, n_show_periods=2):
    """仪器链路五层图：驱动电压 → 激光波数 → 激光功率 → PD(ADC) 电压 → DAS。"""
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    m = r["meta"]
    fs = float(m["cfg"]["fs"])
    period = 1.0 / float(m["cfg"]["freq_Hz"])
    n_per = int(round(fs * period))
    n = min(r["t"].size, max(n_per * int(n_show_periods), 100))
    t_ms = r["t"][:n] * 1e3

    fig, ax = plt.subplots(5, 1, figsize=(9.5, 12))
    ax[0].plot(t_ms, r["v_drive"][:n])
    ax[0].set_ylabel("drive V (V)")
    ax[0].set_title(f"instrument chain — {m['species']} @ {m['wn_center']:.3f} cm$^{{-1}}$   "
                    f"T={m['T']:g} K, P={m['P']:g} atm, x={m['x']}, L={m['L_cm']:g} cm, "
                    f"edge={m['edge']}")
    ax[1].plot(t_ms, r["nu_laser"][:n])
    ax[1].set_ylabel(r"wavenumber (cm$^{-1}$)")
    ax[2].plot(t_ms, r["p_laser"][:n], label="laser")
    ax[2].plot(t_ms, r["p_opt"][:n], "--", label="at PD (loss+absorption)")
    ax[2].set_ylabel("power (mW)")
    ax[2].legend()
    ax[3].plot(t_ms, r["v_adc"][:n] * 1e3, ".", ms=1.5, label="PD voltage after ADC")
    ax[3].set_ylabel("PD out (mV)")
    ax[3].legend()
    ax[4].plot(r["nu_das"], r["das"], ".", ms=2, label=f"DAS ({m['edge']})")
    _mix = r.get("alphaL_per_species")             # 混合气：叠加各组分 αL（不新增图）
    if _mix:
        _cols = plt.cm.viridis(np.linspace(0.1, 0.8, max(len(_mix), 2)))
        for _c, _p in zip(_cols, _mix):
            ax[4].plot(r["nu_das"], _p["alphaL"], "-.", lw=0.9, color=_c, label=_p["label"])
    ax[4].set_ylabel("absorbance")
    ax[4].set_xlabel(r"wavenumber (cm$^{-1}$)")
    ax[4].legend()
    for a_ in ax:
        a_.set_xlabel(a_.get_xlabel() or "time (ms)")
        a_.grid(alpha=0.3)
    fig.tight_layout()
    fig.savefig(out_png, dpi=150)
    return out_png


def wms_normalize(X1f_c, Y1f_c, X2f_c, Y2f_c, X1f_bg_c, Y1f_bg_c, X2f_bg_c, Y2f_bg_c,
                  S1f_cyc, v_cyc, alphaL_cyc, valid, trim_frac, bg_subtracted,
                  n_sel=None, norm_lock=None):
    """第 8 级：**归一化**（纯函数，可单测）—— 2f/1f 与退化 2f/I0。

    为什么单独成函数：这里承载的是整个 WMS 定量链条里最要紧的物理判据，
    也是本项目唯一一次"阻断级"事故的发生地（`S2f1f_peak` 与 `sensitivity_k`
    曾分属两套归一化，配对反演偏 249 倍且不报错）。抽成纯函数后可以脱离
    整条链路单独断言：喂入构造的 X/Y，检查归一化方法选择、背景扣除与
    退化条件是否都按预期工作。

    输入全部是**逐点数组**（已按所选扫描方向取段）：
      · 信号支路 X/Y 的 1f、2f 复分量及其幅度（R1f = hypot(X1f, Y1f)）
      · 无吸收参考谱（τ≡1）的对应分量，用于背景扣除
      · S1f_cyc / v_cyc / alphaL_cyc / valid：用于失效判据与 I0 估计

    返回 (S2f_1f, S2f_I0, S2f_norm, norm_method, onef_valid, pk_1f, off_1f,
          onef_min, onef_med, I0_pd, off)。
    """
    if valid is None:                         # 允许调用方只给段长，掩膜由本函数置位
        valid = np.ones(int(n_sel if n_sel is not None else np.size(alphaL_cyc)), dtype=bool)
    else:
        valid = np.array(valid, dtype=bool)
    n_keep = int(round(float(trim_frac) * int(valid.size)))
    if n_keep > 0:                            # 单方向下两端都紧邻转折点
        valid[:n_keep] = False
        valid[-n_keep:] = False
    off = (alphaL_cyc < 0.02 * float(np.max(alphaL_cyc) + 1e-30)) & valid   # 非吸收区
    I0_pd = float(np.mean(np.abs(v_cyc[off]))) if off.any() else float(np.mean(np.abs(v_cyc[valid])))
    # ★ 背景扣除（RAM 基线）：默认以无吸收参考谱复减；关闭仅用于诊断原始基线。
    #   · 2f/1f：复分量先各除以 1f 幅度、再复减背景（与解析 wms_calibration_free 同式）
    #   · 2f/I0：复分量直接相减再取模、除 I0（I0 是标量，减法顺序无歧义）
    R1f_c = np.hypot(X1f_c, Y1f_c); R1f_bg = np.hypot(X1f_bg_c, Y1f_bg_c)
    _X2f1f = np.divide(X2f_c, R1f_c, out=np.zeros_like(X2f_c), where=R1f_c > 1e-15)
    _Y2f1f = np.divide(Y2f_c, R1f_c, out=np.zeros_like(Y2f_c), where=R1f_c > 1e-15)
    _X2f1f_bg = np.divide(X2f_bg_c, R1f_bg, out=np.zeros_like(X2f_bg_c), where=R1f_bg > 1e-15)
    _Y2f1f_bg = np.divide(Y2f_bg_c, R1f_bg, out=np.zeros_like(Y2f_bg_c), where=R1f_bg > 1e-15)
    if bg_subtracted:
        S2f_1f = np.hypot(_X2f1f - _X2f1f_bg, _Y2f1f - _Y2f1f_bg)
        S2f_I0 = np.hypot(X2f_c - X2f_bg_c, Y2f_c - Y2f_bg_c) / max(I0_pd, 1e-30)
    else:
        S2f_1f = np.hypot(_X2f1f, _Y2f1f)
        S2f_I0 = np.hypot(X2f_c, Y2f_c) / max(I0_pd, 1e-30)
    # 2f/1f 适用性判据（只在有效区内）：
    #   真正让 2f/1f 不好用的是"1f 基线随扫描漂移"（AM 即 dP/dV 非线性），
    #   表现为**真无吸收处** 2f/1f 背景不平（泄漏接近峰值）。
    #   注意：1f 在共振中心两侧的"过零"是 AM 与吸收 1f 相消的**物理奇点**，
    #   属正常现象（真实 WMS 亦然），**不构成"1f 失效"**，故不参与判据。
    pk_1f = float(np.max(np.abs(S2f_1f[valid]))) if valid.any() else 0.0
    off_1f = float(np.max(np.abs(S2f_1f[off]))) if off.any() else 0.0
    abs1 = np.abs(S1f_cyc[valid]) if valid.any() else np.array([0.0])
    onef_min, onef_med = float(np.min(abs1)), float(np.median(abs1))
    onef_valid = bool(pk_1f > 0 and off_1f <= 0.30 * pk_1f)
    # ★ 多浓度对比必须**锁定同一种归一化**：判据本身带随机性（噪声下一次跑可能
    #   onef_valid 翻转），各浓度各自判 → 一个用 2f/1f、另一个用 2f/I0，两条曲线
    #   量级差两个数量级，叠加图直接失效。故允许调用方强制方法（norm_lock）。
    _lock = str(norm_lock).strip().lower() if norm_lock else ""
    if _lock.startswith("2f/1f"):
        S2f_norm, norm_method = S2f_1f, "2f/1f"
    elif _lock.startswith("2f/i0"):
        S2f_norm, norm_method = S2f_I0, "2f/I0（PD 非吸收区光强均值）"
    else:
        S2f_norm = S2f_1f if onef_valid else S2f_I0
        norm_method = "2f/1f" if onef_valid else "2f/I0（PD 非吸收区光强均值）"
    if not bg_subtracted:
        norm_method += "（未背景扣除）"
    # ⚠ 必须把**实际使用的 valid** 一并返回：
    #   此前只返回数值，而 `valid[:n] = False` 是局部作用域不会传回调用方 →
    #   调用方的 valid_mask 仍是全 True，**trim_frac 剔除完全未生效**，
    #   锁相滤波器两端的瞬态被当成真实 2f 峰（实测虚高 2.5 倍、
    #   峰位被拖到窗口最右端）。这是抽取函数时引入的回归。
    return (S2f_1f, S2f_I0, S2f_norm, norm_method, onef_valid, pk_1f, off_1f,
            onef_min, onef_med, I0_pd, off, valid)


def simulate_wms_instrument(species=GAS_DEFAULTS["species"], wn_center=GAS_DEFAULTS["wn_center"],
                            T=GAS_DEFAULTS["T"], P=GAS_DEFAULTS["P"], x=GAS_DEFAULTS["x"],
                            L_cm=GAS_DEFAULTS["L_cm"], seed=None, return_xy=False, **kw):
    """WMS 仪器链路仿真：三角波扫描 + 正弦调制 → 激光 → 光路 → PD → ADC → 数字锁相。

    与 DAS 的差别：驱动电压叠加**高频正弦调制** fm，探测器信号被编码到 fm 及其谐波，
    经正交解调 + 整数周期滑动平均得到 1f、2f 谐波；2f/1f 在弱吸收下 ∝ 浓度，
    且信号被搬离低频 → 天然规避 1/f 噪声与激光 RIN。

    调制幅值默认**自动优化**：使调制系数 m = a/HWHM ≈ 2.2（2f 峰值最大）。

    返回 dict：一个扫描周期内的 t, v_drive, v_scan, v_mod, nu_laser, p_laser, p_opt,
                v_adc, S1f, S2f, S2f1f, meta；另有全局序列供局部放大查看。
    """
    cfg = {**DAQ_DEFAULTS, **SCAN_DEFAULTS, **MOD_DEFAULTS, **LASER_DEFAULTS,
           **OPTICS_DEFAULTS, **PD_DEFAULTS, **GAS_DEFAULTS, **FRINGE_DEFAULTS}
    user_keys = set(kw)
    mixture = kw.pop("mixture", None)          # 混合气组分（不并入 cfg，单列处理）
    cfg.update({k: v for k, v in kw.items() if v is not None})
    # 显式位置参数覆盖默认（不传则用 GAS_DEFAULTS）
    cfg["species"], cfg["wn_center"] = species, wn_center
    cfg["T"], cfg["P"], cfg["x"], cfg["L_cm"] = T, P, x, L_cm
    species = cfg["species"]; wn_center = cfg["wn_center"]; T = cfg["T"]
    P = cfg["P"]; x = cfg["x"]; L_cm = cfg["L_cm"]
    # 由 wn_center + scan_span_cm 反算 amp_V / offset_V（电压是波数的从变量）
    _resolve_scan(cfg, wn_center, user_keys)

    warnings = []
    _require_driver_range(cfg, wn_center, "offset_V" not in user_keys)
    fm = float(cfg["mod_freq_Hz"])
    fs = float(cfg["fs"])
    # ★ 数字锁相要求采样率是调制频率的**整数倍**，且每调制周期 ≥8 点。
    #   整数倍：滑动平均窗口恰为一个调制周期，正交解调完备（DC/调制分量泄漏 ~1e-13）；
    #   非整数倍：泄漏可达 ~3e-3（差 10 个数量级），并使 m 自适应扫描判断失真。
    #   不满足时吸附到最近的合法整数倍，并同步放大 n_samples 保持总采集时间不变。
    r_ratio = fs / fm
    if r_ratio < 8.0 - 1e-9:
        n_req = 8
        why = f"每调制周期仅 {r_ratio:.2f} 点（<8），正交解调不完备"
    elif abs(r_ratio - round(r_ratio)) > 1e-9:
        n_req = int(round(r_ratio))
        why = f"每调制周期 {r_ratio:.3f} 点（非整数），采样与调制不同步"
    else:
        n_req = None
    if n_req is not None:
        fs_new = n_req * fm
        warnings.append(f"采样率 {fs / 1e3:.1f} kHz 与调制频率 fm={fm / 1e3:g} kHz 不匹配：{why}"
                        f" → 已吸附到 {fs_new / 1e3:.1f} kHz（= {n_req}×fm）")
        # 否则总时间缩短 → 扫描周期数减少 → 锁相平均的有效周期数不足
        cfg["n_samples"] = int(round(float(cfg["n_samples"]) * fs_new / fs))
        fs = fs_new
        cfg["fs"] = fs_new                   # 同步回写，供下游（自适应 m 扫描等）读取
    if fs > 250e3:
        warnings.append(f"所需采样率 {fs / 1e3:.0f} kS/s 超过常见 16-bit DAQ 上限（250 kS/s）→ "
                        f"实际采集需更高采样率的 DAQ，或降低调制频率 fm")
    t = np.arange(int(cfg["n_samples"]), dtype=float) / fs
    fscan = float(cfg["freq_Hz"])

    # ① 驱动电压 = 三角波扫描 + 正弦调制
    v_scan = daq_triangle(t, cfg["amp_V"], fscan, cfg["phase_deg"], cfg["offset_V"])
    v_mod_sig = np.cos(2 * np.pi * fm * t + np.deg2rad(float(cfg["mod_phase_deg"])))

    span_scan = float(cfg["scan_span_cm"])
    _step = min(2e-4, span_scan / 2000.0)
    _wing = max(10.0, 5.0 * span_scan)
    if mixture is None:
        nu_grid, alpha_pure, info = th.absorption(
            species, wn_center - span_scan - 0.3, wn_center + span_scan + 0.3, T=T, P=P,
            step=_step, wingHW=_wing)
        _xscale = float(x)
    else:
        _specs = _resolve_mixture_specs(species, x, mixture)
        nu_grid, alpha_pure, _per = _alpha_grid(
            _specs, wn_center - span_scan - 0.3, wn_center + span_scan + 0.3, T, P, _step, _wing)
        _xscale = 1.0                                # alpha_pure 已含 Σ x_i 缩放
        info = {"n_lines_in_window": sum(p["info"].get("n_lines_in_window", 0) for p in _per),
                "species_list": [p["species"] for p in _per], "per_species": _per,
                "mixture": [{"species": s, "x": xi} for s, xi in _specs]}
    alpha_pure_mix = alpha_pure * _xscale
    auto_mod = cfg["mod_amp_V"] is None
    m_opt_raw = cfg.get("m_opt", 2.2)
    _auto_m = str(m_opt_raw).strip().lower() == "auto"
    if auto_mod and _auto_m:
        # 自适应：扫描选最优 m（A 解析 + D 模型 + B 完整链路扫描）
        m_act, a_cm1, hwhm, mod_info = adaptive_modulation_index(
            nu_grid, alpha_pure, estimate_hwhm(nu_grid, alpha_pure),
            cfg, t, v_scan, v_mod_sig, _xscale, float(L_cm),
            n_lines=info["n_lines_in_window"])
        _dnu_dV = abs(float(cfg["eta_VI"]) * float(cfg["dnu_dI"]))
        mod_amp_V = a_cm1 / _dnu_dV if _dnu_dV > 0 else 0.0
    elif auto_mod:
        # 用纯 alpha（未乘浓度）：HWHM 是谱线形状属性，与浓度无关
        mod_amp_V, a_cm1, hwhm, m_act = optimal_mod_amp_V(
            nu_grid, alpha_pure, cfg["eta_VI"], cfg["dnu_dI"], float(m_opt_raw))
        mod_info = {"m_analytic": float(m_opt_raw), "m_model": float(m_opt_raw),
                    "m_scan": float(m_opt_raw), "note": "手动指定 m_opt，未自适应"}
    else:
        mod_amp_V = float(cfg["mod_amp_V"])
        a_cm1 = mod_amp_V * abs(float(cfg["eta_VI"]) * float(cfg["dnu_dI"]))
        hwhm = estimate_hwhm(nu_grid, alpha_pure)
        m_act = a_cm1 / hwhm if hwhm > 0 else float("nan")
        mod_info = {"m_analytic": m_act, "m_model": m_act, "m_scan": m_act,
                    "note": "手动指定 mod_amp_V，未自适应"}
    v_drive = v_scan + mod_amp_V * v_mod_sig

    # ② V → I → 波数 / 功率
    i_laser, nu_laser, p_laser = voltage_to_laser(
        v_drive, cfg["eta_VI"], cfg["dnu_dI"], cfg["wn_ref"], cfg["i_ref"],
        cfg["i_th"], cfg["eta_IP"], cfg.get("d2nu_dI2", 0.0))
    if float(np.max(p_laser)) <= 0.0:
        raise ValueError(f"扫描区间内激光功率≤0：offset_V={cfg['offset_V']:.3g} V 可能越出驱动器 0–5 V 范围，"
                         f"或 wn_center 与激光 wn_ref/dν/dI 不匹配（选了不在此 DFB 调谐范围内的线）。"
                         f"请改用对应激光器参数（wn_ref/dν/dI/eta_VI）或手填 offset_V。")

    # 1/f 噪声：慢漂移（正弦）+ 粉红噪声（1/f RIN）。WMS 锁相是高通、天然抑制；
    # DAS 直接测 DC 光强、受害于它——这是 WMS 通常强于 DAS 的物理来源。
    rng = np.random.default_rng(seed)
    drift_frac = float(cfg.get("drift_frac", 0.0))
    flicker_frac = float(cfg.get("flicker_frac", 0.0))
    # —— 确定性部分 p_det：慢漂移 + 残余幅度调制 RAM（背景扣除时**保留**，属物理基线）——
    p_det = p_laser
    if drift_frac > 0:
        p_det = p_det * (1.0 + drift_frac * np.sin(2 * np.pi * 1.0 * t + 0.3))
    # 残余幅度调制（RAM）：激光**固有**强度调制，与 FM 存在相位差
    _am_i0, _am_i2 = float(cfg.get("am_i0", 0.0)), float(cfg.get("am_i2", 0.0))
    if _am_i0 != 0.0 or _am_i2 != 0.0:
        _wm = 2.0 * np.pi * fm
        p_det = p_det * (1.0 + _am_i0 * np.cos(_wm * t + float(cfg.get("am_psi1", 0.0)))
                         + _am_i2 * np.cos(2.0 * _wm * t + float(cfg.get("am_psi2", 0.0))))
    # —— 随机粉红噪声只作用于信号（**不进背景**：减随机噪声只会平方相加、更差）——
    if flicker_frac > 0:
        p_laser = p_det * (1.0 + flicker_frac * pink_noise(len(t), fs, rng))
    else:
        p_laser = p_det

    # ③ 光路
    alpha = np.interp(nu_laser, nu_grid, alpha_pure) * _xscale
    tau = np.exp(-alpha * float(L_cm))
    # etalon 条纹：光路乘性透过率（默认关 → 恒 1.0，不改变原行为）。信号支路含漂移残留。
    _fr = resolve_fringe(cfg)
    p_opt = p_laser * float(cfg["throughput"]) * fringe_factor(nu_laser, cfg, t) * tau

    # ④ PD：光电流 → 跨阻电压 → 带宽低通（真实 PD 有响应时间）
    i_pd = p_opt * 1e-3 * float(cfg["resp"])
    v_pd = pd_lowpass(i_pd * float(cfg["gain"]), cfg["bw"], fs)

    # ⑤ 噪声（白噪声：散粒 + 热 + RIN）
    # physical_noise=False → 严格理想仿真：连散粒/热也不注入（无噪理论对照，结果逐位可复现）
    if bool(cfg.get("physical_noise", True)):
        s_shot, s_therm, s_rin = noise_currents(float(np.mean(i_pd)), cfg["bw"],
                                                cfg["gain"], cfg["rin"], T)
        s_total = float(np.sqrt(s_shot ** 2 + s_therm ** 2 + s_rin ** 2))
        v_noisy = v_pd + rng.normal(0.0, s_total * float(cfg["gain"]), v_pd.shape)
    else:
        s_shot = s_therm = s_rin = 0.0
        v_noisy = np.array(v_pd, dtype=float)

    # ⑥ ADC
    # ★ v_ana = 注入噪声**之前**的模拟电压（确定性部分，与 seed 无关）。
    #   它作为接缝暴露给 measurement_uncertainty：后者只需换一个噪声实现，
    #   不必重跑 α 插值 / 光路 / 带宽低通（实测占单次耗时约一半）。
    #   注意：这里取的是"无白噪声"的模拟量；flicker 噪声已在更早的 p_laser 上进入，
    #   故 fast path 仍会重做 p_laser→…→v_pd 这段以保持物理一致（见 _wms_from_bands）。
    v_ana = np.array(v_pd, dtype=float)
    lsb = 2.0 * float(cfg["v_range"]) / float(2 ** int(cfg["adc_bits"]))
    n_sat = int(np.sum(np.abs(v_noisy) >= float(cfg["v_range"])))
    v_adc = np.clip(np.round(v_noisy / lsb) * lsb, -float(cfg["v_range"]), float(cfg["v_range"]))

    # ⑦ 数字锁相 → 1f / 2f（正交解调，保留 X/Y 复分量用于背景扣除）
    avg, stg = int(cfg["lockin_avg"]), int(cfg["lockin_stages"])
    _lk = str(cfg.get("lock_kind", "boxcar")).lower()
    _zp = bool(cfg.get("zero_phase", False))
    S1f, X1f, Y1f = wms_harmonic_lockin(v_adc, t, fs, fm, 1, avg, stg,
                                       lock_kind=_lk, zero_phase=_zp)
    S2f, X2f, Y2f = wms_harmonic_lockin(v_adc, t, fs, fm, 2, avg, stg,
                                       lock_kind=_lk, zero_phase=_zp)
    # 背景参考谱：τ≡1 走同一条链路（含相同 RAM/AM + 慢漂移，**无**白/粉红噪声）。
    # 2f 在非吸收区的基线主要来自 L-I 二阶非线性（RAM），是物理背景，
    # 只能靠"无吸收参考谱"扣除——降噪 / 提采样率都压不掉。
    # 参考谱走**同一光路**：含确定性条纹（apply_drift=False）→ 背景扣除只留漂移残留。
    i_bg = (p_det * float(cfg["throughput"]) * fringe_factor(nu_laser, cfg, t, apply_drift=False)
            * 1e-3 * float(cfg["resp"]))
    v_bg = pd_lowpass(i_bg * float(cfg["gain"]), cfg["bw"], fs)
    v_bg_adc = np.clip(np.round(v_bg / lsb) * lsb, -float(cfg["v_range"]), float(cfg["v_range"]))
    _, X1f_bg, Y1f_bg = wms_harmonic_lockin(v_bg_adc, t, fs, fm, 1, avg, stg,
                                          lock_kind=_lk, zero_phase=_zp)
    _, X2f_bg, Y2f_bg = wms_harmonic_lockin(v_bg_adc, t, fs, fm, 2, avg, stg,
                                          lock_kind=_lk, zero_phase=_zp)

    # 取一个扫描方向（默认上升沿）：三角波往返会让同一波数出现两次、方向相反，
    # 叠加后无法判读；实验中也按"单次扫描"取一条。edge=both 可取完整周期。
    n_per = int(round(fs / fscan))
    if n_per < 16:
        raise ValueError(f"每扫描周期采样点太少（fs/fscan = {fs / fscan:.1f}），请提高 fs 或降低 fscan")
    half = n_per // 2
    edge = str(kw.get("edge") or "rising").strip().lower()
    # 注意 daq_triangle：前半=电压上升(-1→+1)、后半=电压下降(+1→-1)；
    # 因 dν/dI<0，电压上升沿对应 ν 递减、下降沿对应 ν 递增。edge 指**驱动电压**方向。
    if edge == "rising":
        idx = np.arange(half)[::-1]          # 电压上升沿(前半)，ν 递减 → 反转为递增
    elif edge == "both":
        idx = np.arange(n_per)
    else:
        edge = "falling"
        idx = np.arange(half, n_per)          # 电压下降沿(后半)，ν 递增
    n_sel = int(idx.size)
    # ★ 横轴必须用"扫描波数"（不含调制），不能用瞬时波数 nu_laser[idx] ——
    #   后者带 ±a 的摆动，每个点在横轴上来回平移，会把曲线折成"水平条纹"乱麻。
    _, nu_scan_all, _ = voltage_to_laser(v_scan, cfg["eta_VI"], cfg["dnu_dI"], cfg["wn_ref"],
                                         cfg["i_ref"], cfg["i_th"], cfg["eta_IP"],
                                         cfg.get("d2nu_dI2", 0.0))
    nu_cyc = nu_scan_all[idx]
    S1f_cyc, S2f_cyc, v_cyc = S1f[idx], S2f[idx], v_adc[idx]
    X1f_c, Y1f_c, X2f_c, Y2f_c = X1f[idx], Y1f[idx], X2f[idx], Y2f[idx]
    X1f_bg_c, Y1f_bg_c = X1f_bg[idx], Y1f_bg[idx]
    X2f_bg_c, Y2f_bg_c = X2f_bg[idx], Y2f_bg[idx]
    alphaL_cyc = np.interp(nu_cyc, nu_grid, alpha_pure) * _xscale * float(L_cm)

    # ⑧ 归一化：优先 2f/1f；若 1f 失效则退化为 PD 非吸收区光强归一化 2f/I0
    #    · 2f/1f 的前提：1f 正比于光强。但 1f 实际正比于 L-I **斜率** dP/dV，随扫描变化，
    #      且扫描转折处 1f→0，相除会把伪影放大成假峰 → 必须检测并弃用。
    #    · 退化方案：I0 = 非吸收区 PD 信号平均，2f/I0 与光强无关且形状稳定（标准 DC 归一化）。
    # ⑧ 归一化（抽为纯函数 wms_normalize，可单测）
    _bg_sub = bool(cfg.get("background_subtract", False))
    valid = np.ones(n_sel, dtype=bool)      # 有效区掩膜由 wms_normalize 内按 trim_frac 置位
    n_keep = int(round(float(cfg["trim_frac"]) * n_sel))   # 供 meta 上报剔除点数
    (S2f_1f, S2f_I0, S2f_norm, norm_method, onef_valid, pk_1f, off_1f,
     onef_min, onef_med, I0_pd, off, valid) = wms_normalize(
        X1f_c, Y1f_c, X2f_c, Y2f_c, X1f_bg_c, Y1f_bg_c, X2f_bg_c, Y2f_bg_c,
        S1f_cyc, v_cyc, alphaL_cyc, valid,
        float(cfg["trim_frac"]), _bg_sub,
        norm_lock=cfg.get("norm_lock"))
    bg_subtracted = _bg_sub
    S2f1f = S2f_1f                     # 保留原名以向后兼容（始终输出，供用户自行判断）

    # ⑨ DAS 对照：DAS 是**不带调制**的直接吸收技术，所以不能用"对含调制信号做平均"
    #    来伪造 —— 那样平均窗口内波数仍在摆动，会折出锯齿。这里把调制置零、
    #    走同一条链路重算一次 PD 信号，才是真正的 DAS。
    _, nu_das, p_das = voltage_to_laser(v_scan, cfg["eta_VI"], cfg["dnu_dI"], cfg["wn_ref"],
                                        cfg["i_ref"], cfg["i_th"], cfg["eta_IP"],
                                        cfg.get("d2nu_dI2", 0.0))
    if drift_frac > 0:
        p_das = p_das * (1.0 + drift_frac * np.sin(2 * np.pi * 1.0 * t + 0.3))
    if flicker_frac > 0:
        p_das = p_das * (1.0 + flicker_frac * pink_noise(len(t), fs, rng))
    alpha_das = np.interp(nu_das, nu_grid, alpha_pure) * _xscale
    # ★ DAS 基线必须用"无吸收光强 I0"（仿真里可精确给出）。之前的"非吸收区多项式拟合"
    #   在密集谱区（如 C2H6 159 条线，无非吸收区）会失效，导致 DAS 基线错、吸光度失真。
    # ★ I0 也必须过同一条低通 + ADC 量化链路，否则与含吸收的 v_das_all 相位不对齐、
    #   在斜坡上产生系统性偏差（弱吸收下偏差可达 100%+）。噪声不加（I0 是理想无吸收基线）。
    # etalon：I0 支路用确定性条纹，信号支路含漂移残留（与 WMS 同源处理）
    _fr0 = fringe_factor(nu_das, cfg, t, apply_drift=False)
    _fr1 = fringe_factor(nu_das, cfg, t)
    v_das_I0 = np.clip(np.round(pd_lowpass(p_das * float(cfg["throughput"]) * _fr0 * 1e-3
                                           * float(cfg["resp"]) * float(cfg["gain"]),
                                           cfg["bw"], fs) / lsb) * lsb,
                       -float(cfg["v_range"]), float(cfg["v_range"]))
    p_das_opt = (p_das * float(cfg["throughput"]) * _fr1 * np.exp(-alpha_das * float(L_cm)))
    i_das = p_das_opt * 1e-3 * float(cfg["resp"])
    # 同源噪声模型（与 WMS 完全一致，公平对比）
    s_sh_d, s_th_d, s_rin_d = noise_currents(float(np.mean(i_das)), cfg["bw"],
                                             cfg["gain"], cfg["rin"], T)
    s_d = float(np.sqrt(s_sh_d ** 2 + s_th_d ** 2 + s_rin_d ** 2))
    v_das_all = (pd_lowpass(i_das * float(cfg["gain"]), cfg["bw"], fs)
                 + rng.normal(0.0, s_d * float(cfg["gain"]), i_das.shape))
    # 与 WMS 链路一致地过 ADC（量化+限幅），保证 DAS/WMS 公平对比
    # （此前 DAS 跳过量化，使"DAS-理论一致"校验偏乐观）
    v_das_all = np.clip(np.round(v_das_all / lsb) * lsb,
                        -float(cfg["v_range"]), float(cfg["v_range"]))
    v_das_cyc = v_das_all[idx]
    das_cyc = -np.log(np.maximum(v_das_cyc, 1e-12) / np.maximum(v_das_I0[idx], 1e-12))

    # 主动检查
    aL_peak = float(np.max(alpha_pure_mix)) * float(L_cm)
    if aL_peak > 0.1:
        warnings.append(f"αL≈{aL_peak:.3g} 偏大（>0.1）：2f 偏离弱吸收线性区，建议降低浓度或光程")
    elif aL_peak < 1e-5:
        warnings.append(f"αL≈{aL_peak:.3g} 偏小（<1e-5）：2f 信噪比可能不足")
    if n_sat > 0:
        warnings.append(f"ADC 饱和：{n_sat} 个采样点超出 ±{cfg['v_range']:g} V → 降低跨阻增益")
    v_max = float(np.max(np.abs(v_adc)))
    if v_max < 0.05 * float(cfg["v_range"]):
        warnings.append(f"PD 信号仅 {v_max:.3g} V，远小于量程 ±{cfg['v_range']:g} V → 可提高增益")
    # m 最优参考：孤立线 ≈2.2、密集谱偏小（与 adaptive_modulation_index 的 m_analytic 一致）。
    # 只有**用户手动指定 m**（非自适应）时才提示偏差；自适应优化已自行选优，不该再报"偏离"。
    n_lines_w = int(info["n_lines_in_window"])
    m_ref = 2.2 if n_lines_w <= 10 else (1.8 if n_lines_w <= 50 else 1.25)
    if (not (auto_mod and _auto_m)) and not (0.85 * m_ref <= m_act <= 1.15 * m_ref):
        warnings.append(f"手动指定的调制系数 m={m_act:.2f} 偏离该谱线密度下的最优 m≈{m_ref} "
                        f"（孤立线≈2.2、密集谱偏小）→ 建议 mod_amp_V≈"
                        f"{m_ref * hwhm / abs(cfg['eta_VI'] * cfg['dnu_dI']):.4g} V")
    warnings.append(f"调制深度 a={a_cm1:.4g} cm⁻¹（HWHM={hwhm:.4g} cm⁻¹, m={m_act:.2f}），"
                    f"调制电压 {mod_amp_V:.4g} V @ fm={fm / 1e3:g} kHz")
    # ★ 噪声透明化：所有注入的噪声项必须逐一告知用户
    _np = [f"散粒 {s_shot * cfg['gain'] * 1e3:.3g} mV",
           f"热 {s_therm * cfg['gain'] * 1e3:.3g} mV",
           f"RIN(白) {s_rin * cfg['gain'] * 1e3:.3g} mV"]
    if drift_frac > 0:
        _np.append(f"1/f 慢漂移 {drift_frac * 100:.2g}%")
    if flicker_frac > 0:
        _np.append(f"1/f 粉红噪声 {flicker_frac * 100:.2g}%")
    warnings.append("已注入噪声（须告知用户）：" + " + ".join(_np))
    if _fr is not None:
        warnings.append(f"etalon 条纹已启用：FSR={_fr[0]:.4g} cm⁻¹、峰-峰对比度≈{_fr[1]:.1%}。"
                        f"确定性条纹会被背景扣除 / I0 归一化消除，**只有漂移残留**"
                        f"（fringe_drift_frac={float(cfg.get('fringe_drift_frac') or 0):.2g}）进入 2f；"
                        f"如需直接观察条纹本身，可设 background_subtract=False。"
                        f"注意：固定 Etalon 的确定性条纹加噪声/多次平均压不掉，只能靠楔化/AR/扫频。")
    if n_keep > 0:
        warnings.append(f"已剔除扫描两端各 {float(cfg['trim_frac']) * 100:.0f}% 数据"
                        f"（三角波转折点导数不连续，其高频谐波会泄漏进 2f）")
    # 归一化体检：白话说清"2f/1f 能不能用"
    if onef_valid:
        warnings.append(f"归一化采用 **2f/1f**（1f 基线平稳）：非吸收区泄漏 {off_1f:.3g} "
                        f"占峰值 {100 * off_1f / max(pk_1f, 1e-30):.1f}%")
        if onef_min < 0.05 * max(onef_med, 1e-30):
            warnings.append(f"提示：1f 在共振两侧存在过零点（min/中位数="
                            f"{onef_min / max(onef_med, 1e-30):.2f}），该处 2f/1f 有奇点，"
                            f"读取浓度应取 2f 峰值点而非过零区")
    else:
        warnings.append(f"⚠ **2f/1f 不可用 → 已自动改用 2f/I0（PD 非吸收区光强均值 I0={I0_pd:.4g} V）**。"
                        f"原因：非吸收区 2f/1f 泄漏 {off_1f:.3g} 占峰值 "
                        f"{100 * off_1f / max(pk_1f, 1e-30):.0f}%（>30%，1f 基线随扫描漂移）")
    if bg_subtracted:
        warnings.append("已做 **背景扣除**：用无吸收参考谱（τ≡1，含相同 RAM/AM 与慢漂移，无白/粉红噪声）"
                        "复减 2f/1f 与 2f/I0 的 RAM 基线（L-I 二阶非线性残留）")
    else:
        warnings.append("⚠ **未做背景扣除（默认）**：归一化 2f 保留 L-I 非线性与 RAM 的物理基线——"
                        "这是**未经修正的原始谱**，仅供诊断；用于浓度反演或检测限结论前，"
                        "请设 background_subtract=true 复减无吸收参考谱（或自行扣除基线）")

    # 免标定灵敏度：k = 归一化 2f 峰值 ÷ 浓度（弱吸收下与浓度无关）。
    # 这是**完整链路**的 k，供 tdlas_invert 复用；勿与解析版 simulate() 混用（两路 S2f/1f 差 ~6×）。
    # 混合气下"单物种浓度"无定义 → k 置 0（不做无据外推）。
    k_sens = (float(np.max(np.abs(S2f_norm[valid]))) / float(x)
              if (mixture is None and valid.any() and float(x) > 0) else 0.0)

    _mx = info.get("mixture") or [{"species": str(species).upper(), "x": float(x)}]
    _sp = (str(species).upper() if mixture is None
           else "+".join(f"{d['species']}@{d['x']:g}" for d in _mx))
    meta = {"species": _sp, "wn_center": float(wn_center), "T": float(T),
            "P": float(P), "x": (float(x) if mixture is None else "混合气"),
            "L_cm": float(L_cm), "fs": fs, "mixture": _mx,
            "fscan_Hz": fscan, "mod_freq_Hz": fm, "mod_amp_V": mod_amp_V, "auto_mod": auto_mod,
            "mod_coeff_m": m_act, "mod_depth_cm-1": a_cm1, "hwhm_cm-1": hwhm,
            "modulation": mod_info,
            "alpha_L_peak": aL_peak, "v_pd_mean": float(np.mean(v_pd)), "lsb_V": lsb,
            "n_sat": n_sat, "sigma_shot": s_shot * cfg["gain"],
            "sigma_thermal": s_therm * cfg["gain"], "sigma_rin": s_rin * cfg["gain"],
            "drift_frac": drift_frac, "flicker_frac": flicker_frac,
            "bg_subtracted": bg_subtracted, "norm_method": norm_method, "onef_valid": onef_valid, "I0_pd_V": I0_pd,
            "offband_leak_1f": off_1f, "peak_2f_1f": pk_1f, "sensitivity_k": k_sens,
            "onef_min_over_median": onef_min / max(onef_med, 1e-30),
            "trim_frac": float(cfg["trim_frac"]), "n_trim": n_keep, "edge": edge,
            "physical_noise": bool(cfg.get("physical_noise", True)), "seed_used": seed,
            "cfg": dict(cfg), "warnings": warnings,
            "fringe": ({"enabled": True, "fsr_cm-1": _fr[0], "contrast": _fr[1],
                        "n": cfg.get("fringe_n"), "d_cm": cfg.get("fringe_d_cm"),
                        "R": cfg.get("fringe_R"), "phase_rad": cfg.get("fringe_phase_rad"),
                        "drift_frac": cfg.get("fringe_drift_frac")} if _fr else {"enabled": False}),
            "n_lines_in_window": info["n_lines_in_window"], "table": info.get("table"),
            "alpha_context": info.get("alpha_context")}
    # ★ BUG-D 修复：锁相已算出正交 X/Y 复分量（背景扣除的输入），但此前未返回，
    #   导致用户拿不到 1f/2f 的原始同相/正交分量。默认关（每次返回多 8 条等长数组，
    #   会显著增大 MCP 返回体），按需开 return_xy=True 才透出。
    _xy_out = {}
    if return_xy:
        _xy_out = {"X1f_c": X1f_c, "Y1f_c": Y1f_c, "X2f_c": X2f_c, "Y2f_c": Y2f_c,
                   "X1f_bg_c": X1f_bg_c, "Y1f_bg_c": Y1f_bg_c,
                   "X2f_bg_c": X2f_bg_c, "Y2f_bg_c": Y2f_bg_c}
    _out = {"t": t, "v_drive": v_drive, "v_scan": v_scan, "v_mod": v_mod_sig * mod_amp_V,
            "nu_laser": nu_laser, "p_laser": p_laser, "p_opt": p_opt, "v_pd": v_pd,
            "v_adc": v_adc, "S1f": S1f, "S2f": S2f, "S2f1f": S2f1f,
            "nu_axis": nu_cyc, "S1f_cyc": S1f_cyc, "S2f_cyc": S2f_cyc,
            "S2f1f_cyc": S2f_1f, "S2f_norm_cyc": S2f_norm,
            "v_adc_cyc": v_cyc, "v_drive_cyc": v_drive[idx],
            "alphaL_cyc": alphaL_cyc, "offband_mask": off, "valid_mask": valid,
            "v_ana": v_ana,                      # 无噪声模拟电压（接缝：供重复测量复用）
            "das_cyc": das_cyc, "v_das_cyc": v_das_cyc,
            "t_cyc": t[idx], "n_per": n_sel, "edge": edge, "meta": meta, **_xy_out}
    if mixture is not None:                    # 各组分 αL（对齐 nu_axis，供标准图 αL 面板叠加）
        _out["alphaL_per_species"] = [
            {"label": f"{p['species']}@{p['x']:g}",
             "alphaL": np.interp(nu_cyc, nu_grid, p["alpha_pure"]) * p["x"] * float(L_cm)}
            for p in info["per_species"]]
    return _out


def measurement_uncertainty(species, wn_center, T, P, x, L_cm, seed=0, n_repeats=5,
                           ci=0.95, **kw):
    """**测量不确定度**：同一工况重复仿真，给出归一化 2f 峰的散布与相对不确定度。

    为什么需要
    ──────────
    本工具此前只给点值（浓度、k、LOD），用户无法判断"这个数能用几位"。而"仪器级
    逼近真实实验"的核心诉求之一，就是让定量结论**带误差**。

    物理含义与口径（重要，避免误读）
    ──────────────────────────────
    · 本函数量化的是**有限噪声实现带来的统计散布**（run-to-run）：同一工况下改变
      噪声随机种子重复测量，取归一化 2f 峰的标准差。它对应真实实验中"短时重复测量
      的重复性"，**不等于**总不确定度。
    · 它**不包含**系统项：k 的标定误差、线强/展宽系数的数据库不确定度、线型近似
      （自展宽/线混合/Dicke）、etalon 条纹、带宽钳位、光程与温度测量误差等。
      这些须由 instruments/器件手册或实验标定给出（见 tools/tdlas_fidelity.py 的
      not_implemented 清单）。
    · 故本函数返回的 `note` 明确声明其为**统计（随机）分量**，不得当作总误差。

    实现要点
    ────────
    · 逐次重复用 seed+i，其余参数完全一致 → 只改变随机噪声实现；
    · 相对不确定度 rel_sigma = std(peak) / mean(peak)；
    · 用 Student-t 小样本区间，自由度 = n-1。

    ⚠ **必须在"要报告的那个浓度"上求 σ**（返回的 `x_eval` 记录本次求值点）。早先此处写着
    "rel_sigma 与浓度无关、对任意 x 估计都适用"，**实测不成立**：噪声里有相当一部分
    （热噪声、ADC 量化、RIN）并不随吸收信号等比缩放，于是 rel_sigma 随 x 明显变化。
    实测 CH4@2968.5（L=50 cm、n=5）：x=1e-3 → 1.08%、x=1e-5 → 0.21%、x=1e-7 → 4.04%
    （跨 4 个量级差约 20 倍）。若拿在 x_ref 处算出的散布套到浓度差很远的 x_est 上，
    报出的不确定度可以差一个量级以上。
    """
    from scipy import stats as _st

    n = int(n_repeats)
    if n < 2:
        raise ValueError("n_repeats 至少 2 才能估计散布")
    peaks = np.empty(n, dtype=float)
    # ★ 性能：自适应调制深度扫描占单次链路开销的绝大部分（实测 94%），而**本函数的
    #   每一次重复工况完全相同 → 最优 m 必然相同**，重复扫描是纯浪费。
    #   故第 1 次照常自适应（保留原选点逻辑），后续重复固定复用第 1 次选出的 m_opt。
    #   已实测等价：固定 m_opt=<自适应结果> 与自适应重跑在三个 seed 下 peak 与 k **逐位一致**。
    #   用户显式给了 m_opt 时不介入（那是他的实验约束，必须被尊重）。
    _m_fixed = None
    _user_m = kw.get("m_opt", "auto")
    for i in range(n):
        _kw = dict(kw)
        if _m_fixed is not None:
            _kw["m_opt"] = _m_fixed
        r = simulate_wms_instrument(species, wn_center=wn_center, T=T, P=P, x=x,
                                    L_cm=L_cm, seed=int(seed) + i, **_kw)
        if _m_fixed is None and _user_m == "auto":
            try:
                _m_fixed = float(r["meta"]["mod_coeff_m"])
            except Exception:                              # noqa: BLE001
                _m_fixed = None
        peaks[i] = float(np.abs(r["S2f_norm_cyc"])[r["valid_mask"]].max())

    mean_p = float(np.mean(peaks))
    sd = float(np.std(peaks, ddof=1))
    if mean_p <= 0:
        return {"n_repeats": n, "peaks": [float(v) for v in peaks],
                "mean_peak": mean_p, "std_peak": sd, "rel_sigma": None,
                "valid": False,
                "note": "峰值非正，无法给出相对不确定度（检查工况与有效区）"}

    rel = sd / mean_p
    dof = n - 1
    tcrit = float(_st.t.ppf(0.5 + float(ci) / 2.0, dof))

    # ★ σ 自身的统计不确定度：小样本下 σ 的估计很不可靠（n=3 时约 ±50%），
    #   必须一并给出，否则用户会把"7% ± ?"误当成精确的 7%。
    #   σ 的抽样分布近似 χ²(dof)/dof，取其 95% 区间换算回 rel。
    #   ⚠ rel == 0（各次逐位一致，例如 physical_noise=False）时 χ² 换算退化为 0/0，
    #   必须单独处理，否则会往消息里写 "±nan%"。
    if rel > 0.0:
        try:
            chi_lo = float(_st.chi2.ppf(0.025, dof))
            chi_hi = float(_st.chi2.ppf(0.975, dof))
            rel_lo = rel * np.sqrt(dof / chi_hi)
            rel_hi = rel * np.sqrt(dof / chi_lo)
            # σ 自身的相对标准差：直接由上面 95% 区间反推，保证与区间**自洽**
            # （早先用 1/√(2·dof) 的近似式，与 χ² 区间在中小样本下不一致）。
            sigma_sd = (rel_hi - rel_lo) / (2.0 * 1.959964)
        except Exception:
            rel_lo = rel_hi = rel
            sigma_sd = float("nan")
    else:
        rel_lo = rel_hi = sigma_sd = 0.0

    if rel <= 0.0:
        warn = (f"n_repeats={n} 次结果逐位一致（rel_sigma=0）：随机噪声没有起作用"
                f"（通常是 physical_noise=False）。此时该散布**不是**实验重复性，"
                f"不可作为不确定度引用")
    elif n < 10:
        warn = (f"n_repeats={n} 偏少：σ 自身的相对不确定度约 "
                f"±{100.0 * sigma_sd:.0f}%；定量引用前建议 n_repeats≥10")
    else:
        warn = None
    return {
        "n_repeats": n,
        "peaks": [float(v) for v in peaks],
        "mean_peak": mean_p,
        "std_peak": sd,
        "x_eval": float(x),                  # ★ 本次 σ 的求值点：必须与要报告的浓度一致
        "rel_sigma": rel,                    # 单次测量的相对散布（1σ）
        "rel_sigma_pct": 100.0 * rel,
        "rel_sigma_ci95": [float(rel_lo), float(rel_hi)],
        "rel_sigma_of_sigma_pct": 100.0 * sigma_sd,
        # 相对 95% 区间**半宽**（围绕 0，不是围绕 σ 本身）：
        #   single_* = 单次测量落在 ±x 内的半宽；mean_* = n 次取平均后的半宽（÷√n）
        # 早先这里给的是 [rel−half, rel+half]，把"散布本身"当成了区间中心 → 数值无意义
        # （实测给出 [0.0, 0.1389]，起点被 max(0,·) 夹断），已弃用。
        "single_rel_ci95_halfwidth": float(tcrit * rel),
        "mean_rel_ci95_halfwidth": float(tcrit * rel / np.sqrt(n)),
        "ci_level": float(ci),
        "valid": True,
        "scope": "统计（随机）分量：有限噪声实现的 run-to-run 重复性",
        "low_sample_warning": warn,
        "note": "不含系统项（k 标定、线强/展宽数据库不确定度、自展宽/线混合/Dicke 等"
                "线型近似、etalon 条纹、带宽钳位、光程与温度误差）→ **不得当作总不确定度**。"
                "对定量结论须同时说明：这只是随机分量。",
    }


def rel_uncertainty_for_x(x_value, rel_sigma, ci=0.95):
    """把相对散布换算到某个浓度值上的绝对不确定度（同一工况下 x = peak/k 线性）。

    返回 (x, x_sigma, x_ci_halfwidth)：x_sigma = x · rel_sigma，
    x_ci_halfwidth = x_sigma · z，其中 z 是正态分位数（ci=0.95 → 1.96）。
    ⚠ 前提是 rel_sigma 就在该 x 附近求得（噪声不随 x 等比缩放，见 measurement_uncertainty）。
    """
    x = float(x_value)
    rel = float(rel_sigma)
    from scipy import stats as _st
    z = float(_st.norm.ppf(0.5 + float(ci) / 2.0))
    return x, abs(x) * rel, abs(x) * rel * z


def validate_das_result(r):
    """对 DAS 仪器链路结果做自动校验（与 WMS 的 validate_wms_result 同位）。

    为什么单独需要：DAS 是定量技术（给测量吸光度与浓度），
    但此前**没有任何自动校验**：WMS 有 13 项判据，DAS 只有几条
    场景级 warnings。而 DAS 有自己独有的失败模式（基线拟合把吸收吃掉、
    理想 I0 回退、持续性偏差），不能照搬 WMS 的判据。

    判据（硬/软分档，与项目现有口径一致）：
      1. ADC 动态范围   硬—— 饱和点 >0 即 fail（数据已剪幅，吸光度不可用）
      2. 基线回退         硬—— 触发说明多项式拟合已失效（密集谱区），已改用理想 I0
      3. 动态范围浪费     软—— 信号 ≪ 量程 时 warn
      4. 吸收强弱         软—— αL > 0.1 进非线性区；< 1e-5 信噪不足
      5. I0 与信号同路径   硬—— 校验器依赖的 I0 必须与信号同过 ADC（否则吸光度有系统性偏差）
    """
    m = r.get("meta") or {}
    checks = []

    def add(name, status, detail):
        checks.append({"check": name, "status": status, "detail": detail})

    n_sat = int(m.get("n_sat", 0))
    vr = float((m.get("cfg") or {}).get("v_range", 0) or 0)
    if n_sat > 0:
        add("ADC 动态范围", "fail",
            f"{n_sat} 点饱和（量程 ±{vr:g} V）→ 信号已被剪幅，"
            f"吸光度不可用；请降跨阻增益或光功率")
    else:
        add("ADC 动态范围", "pass", "无饱和")

    if m.get("baseline_fallback"):
        add("基线处理", "warn",
            "多项式基线已失效→已回退到理想 I0（仿真专属）；"
            "真实实验需自行背景扣除，精度会低于本结果")
    else:
        add("基线处理", "pass", "多项式基线拟合未触发回退")

    vmax = float(m.get("v_pd_mean", 0) or 0)
    if vr > 0 and vmax < 0.05 * vr:
        add("信号占满度", "warn",
            f"平均信号 {vmax:.3g} V 远小于量程 ±{vr:g} V（浪费动态范围）")
    else:
        add("信号占满度", "pass", f"平均信号 {vmax:.3g} V")

    aL = float(m.get("alpha_L_peak", 0) or 0)
    if aL > 0.1:
        add("吸收强弱", "warn", f"αL={aL:.3g} > 0.1：DAS 已进非线性区，建议降浓度或光程")
    elif aL < 1e-5:
        add("吸收强弱", "warn", f"αL={aL:.3g} < 1e-5：吸收太弱，信噪比可能不足")
    else:
        add("吸收强弱", "pass", f"αL={aL:.3g} 在可用区")

    has_i0 = any(k in m for k in ("baseline_fallback",)) and ("das" in r or "v_das_cyc" in r)
    add("I0与信号同路径", "pass" if has_i0 else "warn",
        "基线 I0 与信号同过低通 + ADC 量化" if has_i0
        else "未能确认 I0 与信号同路径（请人工复核）")

    status = "fail" if any(c["status"] == "fail" for c in checks) else \
             ("warn" if any(c["status"] == "warn" for c in checks) else "pass")
    return {"overall": status, "checks": checks,
            "summary": f"{status.upper()}: {len(checks)} 项 DAS 校验，" +
                       f"{sum(c['status']=='pass' for c in checks)} 通过、" +
                       f"{sum(c['status']=='warn' for c in checks)} 提醒、" +
                       f"{sum(c['status']=='fail' for c in checks)} 失败"}


def validate_wms_result(r):
    """对 WMS 结果做**自动二次审核**，返回结构化校验报告。

    供 MCP 在每次出结果前调用，把每项 pass/warn/fail 附在返回里，
    让非专业用户也能一眼看出"哪些可信、哪些要复核"。
    """
    m = r["meta"]
    nu, v = r["nu_axis"], r["valid_mask"]
    das = r["das_cyc"][v]
    th = r["alphaL_cyc"][v]
    checks = []

    def add(name, status, detail):
        checks.append({"check": name, "status": status, "detail": detail})

    # 1. 波数轴对齐：扫描窗口是否覆盖目标线
    wc = m["wn_center"]
    if nu.min() <= wc <= nu.max():
        add("波数轴对齐", "pass", f"扫描窗口 [{nu.min():.3f}, {nu.max():.3f}] 覆盖中心 {wc:.3f}")
    else:
        add("波数轴对齐", "fail", f"扫描窗口未覆盖 wn_center={wc}（wn_ref 错位？）")

    # 2. DAS 与理论 αL 一致性（同口径：两侧均取**有效区**峰值）
    #    注意口径差异：本判据的"理论峰"取有效区（已按 trim_frac 剔除扫描两端）内 αL 峰值，
    #    而 key_metrics.alpha_L_peak 报告的是**全窗（未剔除）**理论峰——两者不是同一个数，
    #    若最强线落在剔除区，二者可差约 2×，属正常，不应据此判定矛盾。
    pk_d, pk_t = float(das.max()), float(th.max())
    nl_here = int(m["n_lines_in_window"])
    _dom = f"有效区(已剔除两端各 {float(m['cfg']['trim_frac']) * 100:.0f}%)"
    _pk_full = float(m["alpha_L_peak"])
    _extra = (f"；全窗理论峰 {_pk_full:.4g}（含剔除区，口径不同，勿与本项对比）"
              if pk_t > 0 and abs(_pk_full - pk_t) > 0.2 * pk_t else "")
    if pk_t > 0:
        dev = abs(pk_d - pk_t) / pk_t
        if dev < 0.02:
            add("DAS-理论一致", "pass", f"{_dom} DAS 峰值 {pk_d:.4g} vs 理论 {pk_t:.4g}，偏差 {dev:.1%}{_extra}")
        elif dev < 0.10:
            add("DAS-理论一致", "warn", f"{_dom} DAS 峰值 {pk_d:.4g} vs 理论 {pk_t:.4g}，偏差 {dev:.1%}（>2%）{_extra}")
        elif dev > 0.5:
            # ⚠ 密集谱区的"降级为提醒"不得掩盖**极端偏差**：
            #   实测到 αL=3.3e3 的饱和工况偏差达 99.1% 却仍被归为 warn，
            #   而同一次返回的 interaction.conclusion_allowed 是 True —— 自相矛盾。
            #   结论：偏差 > 50% 时"以 DAS 包络为准"的辩护不成立，必须 fail。
            add("DAS-理论一致", "fail",
                f"{_dom} DAS 峰值 {pk_d:.4g} vs 理论 {pk_t:.4g}，偏差 {dev:.1%}（>50%："
                f"超出多线重叠可解释的范围，疑似饱和 / 基线失效 / 窗口错位）{_extra}")
        elif nl_here > 10:
            # 密集谱区中等偏差（10%~50%）：可能来自多线叠加与基线拟合受限，不作硬 fail
            # （pass/warn 两档不受谱线密度影响，避免把"好结果"误报为坏消息）
            add("DAS-理论一致", "warn",
                f"{_dom} DAS 峰值 {pk_d:.4g} vs 理论 {pk_t:.4g}，偏差 {dev:.1%}；"
                f"窗口内 {nl_here} 条线为多线叠加，不作硬 fail（以 DAS 包络 / 2f 多峰形态为准）{_extra}")
        else:
            add("DAS-理论一致", "fail", f"{_dom} DAS 峰值 {pk_d:.4g} vs 理论 {pk_t:.4g}，偏差 {dev:.1%}（基线/错位可疑）{_extra}")
    else:
        add("DAS-理论一致", "warn", "理论 αL 峰值为 0，无法校验")

    # 3. 弱吸收区 αL —— **分级并阻断**
    #    口径与 docs/VALIDATION.md 声明的适用区间 [1e-5, 0.1] 一致：
    #    超出该区间即"定量前提已破"，不能只给提醒就放行结论。
    #    分级：超出 1 个数量级以内 → 非线性但趋势仍有参考价值（warn）；
    #          超出 ≥10 倍 → 严重饱和，k 与浓度已非物理量（**fail**）。
    aL = m["alpha_L_peak"]
    if 1e-5 <= aL <= 0.1:
        add("弱吸收区", "pass", f"αL={aL:.3g} 在弱吸收线性区 [1e-5, 0.1]")
    elif aL > 1.0:
        add("弱吸收区", "fail",
            f"αL={aL:.3g} 超出弱吸收上限 0.1 达 {aL / 0.1:.0f} 倍：2f 严重饱和，"
            f"**k 与浓度已非物理量**，不可用于定量结论；"
            f"请降 x 或 L_cm 一个量级以上")
    elif aL > 0.1:
        add("弱吸收区", "warn", f"αL={aL:.3g} > 0.1：2f 进入非线性，建议降 x 或 L_cm")
    else:
        add("弱吸收区", "warn", f"αL={aL:.3g} < 1e-5：吸收太弱，信噪比可能不足")

    # 3b. 信号与量化噪声的比值 —— **硬判据**（实测踩出的坑）
    #     背景：实测 CH4@2698 cm⁻¹（弱组合带）、L=100 cm、x=10~30 ppm 时，
    #     2f 信号仅 0.11~0.26 mV，而 1 LSB = 0.305 mV → **信号埋在量化里**，
    #     k 在三档浓度间竟差 23 倍（完全不线性）。但 overall 只报 warn、
    #     conclusion_allowed=True —— 与"能否定量"直接矛盾。
    #     判据：2f 绝对幅度 < 3 LSB → fail（量化主导，数值不可用）；
    #           < 10 LSB → warn（可用但余量不足，建议加光程）。
    #     为何用"绝对幅度/一个 LSB"而不是 SNR：量化噪声与信号成比例，
    #     SNR 看不出这个失真；而 LSB 是一个硬门槛。
    try:
        _cfg3 = m["cfg"]
        _lsb3 = 2.0 * float(_cfg3["v_range"]) / float(2 ** int(_cfg3["adc_bits"]))
        _v3 = r["valid_mask"]
        _i0_3 = float(np.mean(np.abs(r["v_adc_cyc"][_v3]))) if _v3.any() else 0.0
        _s2f3 = float(np.abs(r["S2f_norm_cyc"])[_v3].max()) if _v3.any() else 0.0
        _lsb_cnt = (_s2f3 * _i0_3) / _lsb3
        if _lsb_cnt < 3.0:
            add("信号/量化", "fail",
                f"2f 绝对幅度 ≈ {_lsb_cnt:.2f} LSB（< 3）：信号埋在 ADC 量化里，"
                f"k 与浓度不可用；请**加光程**（多通池）或换强吸收线（加增益无效："
                f"增益放大的是直流基底，会先撞量程而饱和）")
        elif _lsb_cnt < 10.0:
            add("信号/量化", "warn",
                f"2f 绝对幅度 ≈ {_lsb_cnt:.1f} LSB（< 10）：可用但余量不足，"
                f"建议加光程以提高信号余量")
        else:
            add("信号/量化", "pass", f"2f 绝对幅度 ≈ {_lsb_cnt:.0f} LSB（充分）")
    except Exception:                                          # noqa: BLE001
        pass

    # 4. 孤立线（标准谐波前提）
    nl = m["n_lines_in_window"]
    if nl <= 10:
        add("孤立单线", "pass", f"窗口内 {nl} 条线，2f 应呈标准双峰")
    else:
        add("孤立单线", "warn", f"窗口内 {nl} 条线：2f 是多线叠加，非标准双峰（DAS 包络更直观）")

    # 5. 调制系数 m（最优值按谱线密度：孤立 2.2 / 中等 1.8 / 密集 1.25）
    mm = m["mod_coeff_m"]
    m_ref = 2.2 if nl <= 10 else (1.8 if nl <= 50 else 1.25)
    if m.get("auto_mod"):
        add("调制系数 m", "pass", f"m={mm:.2f}（自适应，该谱线密度最优≈{m_ref}）")
    elif 0.85 * m_ref <= mm <= 1.15 * m_ref:
        add("调制系数 m", "pass", f"m={mm:.2f} 接近该谱线密度最优 {m_ref}")
    else:
        add("调制系数 m", "warn", f"m={mm:.2f} 偏离该谱线密度最优 {m_ref}"
            f"（孤立≈2.2 / 中等≈1.8 / 密集≈1.25）")

    # 6. 采样率
    fsr = m["fs"] / m["mod_freq_Hz"]
    if fsr >= 8:
        add("采样率", "pass", f"每调制周期 {fsr:.1f} 点（≥8）")
    else:
        add("采样率", "fail", f"每调制周期仅 {fsr:.1f} 点，锁相精度不足")

    # 7. ADC 动态范围
    if m["n_sat"] > 0:
        add("ADC 动态范围", "fail", f"{m['n_sat']} 点饱和，需降增益")
    elif m["v_pd_mean"] < 0.05 * m["cfg"]["v_range"]:
        add("ADC 动态范围", "warn", f"PD 信号 {m['v_pd_mean']:.3g} V 远小于量程，动态范围浪费")
    else:
        add("ADC 动态范围", "pass", "无饱和，动态范围合理")

    # 8. 归一化方法
    if m["bg_subtracted"]:
        add("归一化方法", "pass", f"采用 {m['norm_method']}（1f 有效={m['onef_valid']}；已背景扣除）")
    else:
        add("归一化方法", "warn", f"采用 {m['norm_method']}（1f 有效={m['onef_valid']}；未背景扣除，仅供诊断）")

    # 9. 噪声告知（须向用户说明到底注入了什么）
    #    注意：散粒/热是**物理固有**的，默认就会注入（只取决于光电流与带宽）。
    #    旧文案在 rin=drift=flicker=0 时直接说"未加噪声（理想仿真）"——不准确，会被 AI
    #    转述成"结果可精确复现"，而实测跑-跑散布达 ±2%~8%（随机种子每次不同）。
    if not bool(m["cfg"].get("physical_noise", True)):
        add("噪声", "pass", "严格理想仿真：未注入任何噪声（含散粒/热），结果逐位可复现")
    elif m["drift_frac"] == 0 and m["flicker_frac"] == 0 and m["cfg"]["rin"] == 0:
        add("噪声", "pass",
            f"仅物理固有噪声：散粒 {m['sigma_shot'] * 1e3:.3g} mV + 热 "
            f"{m['sigma_thermal'] * 1e3:.3g} mV（RIN/1f 漂移/粉红 = 0）。"
            f"该噪声**恒在**、且随 seed 变化；需完全无噪请设 physical_noise=False")
    else:
        add("含人为开关的噪声", "pass",
            f"rin={m['cfg']['rin']}, drift={m['drift_frac']}, flicker={m['flicker_frac']}"
            f"（另含物理固有散粒 {m['sigma_shot'] * 1e3:.3g} mV + 热 {m['sigma_thermal'] * 1e3:.3g} mV）")

    # 10. etalon 干涉条纹（默认关）：可解析性 + 是否会在 2f 上伪造吸收
    fr = m.get("fringe") or {}
    if not fr.get("enabled"):
        add("etalon 条纹", "pass", "未启用 etalon 条纹模型（默认理想仿真）")
    else:
        fsr, ctr = float(fr["fsr_cm-1"]), float(fr["contrast"])
        fw = 2.0 * float(m["hwhm_cm-1"])                 # 吸收线 FWHM
        step = float((m.get("alpha_context") or {}).get("step_cm-1") or 0.0)
        scale = fsr / fw if fw > 0 else float("inf")
        base = (f"FSR={fsr:.4g} cm⁻¹（≈{scale:.1f}×线宽）、峰-峰对比度≈{ctr:.1%}、"
                f"漂移={float(fr.get('drift_frac') or 0):.2g}")
        extra = ("" if ctr < 0.05
                 else "；对比度已远超痕量 αL（~1e-3），不做背景扣除会直接淹没信号")
        if step > 0 and step > fsr / 10.0:
            add("etalon 条纹", "fail",
                f"{base}；网格 step={step:.2g} > FSR/10 → 条纹欠采样/混叠，"
                f"须减小 step（更细网格）或增大 FSR，否则条纹形状不可信{extra}")
        elif fsr > 10.0 * fw:
            add("etalon 条纹", "pass", f"{base}；FSR ≫ 线宽 → 表现为缓慢基线，可拟合/背景扣除{extra}")
        elif fsr >= fw:
            add("etalon 条纹", "warn",
                f"{base}；FSR 为线宽的数倍 → 扫描窗内形成密集假结构，极易与 2f 吸收混淆，"
                f"务必物理解决（窗片楔化 / AR 镀膜 / 扫频平均）{extra}")
        else:
            add("etalon 条纹", "pass",
                f"{base}；FSR < 线宽 → 线内快速振荡，提高 fm 可把 2f 移出其频谱{extra}")

    status = "fail" if any(c["status"] == "fail" for c in checks) else \
             ("warn" if any(c["status"] == "warn" for c in checks) else "pass")
    return {"overall": status, "checks": checks,
            "summary": f"{status.upper()}: {len(checks)} 项校验，" +
                       f"{sum(c['status']=='pass' for c in checks)} 通过、" +
                       f"{sum(c['status']=='warn' for c in checks)} 提醒、" +
                       f"{sum(c['status']=='fail' for c in checks)} 失败"}


def use_cjk_font(matplotlib):
    """让 matplotlib 能显示中文：探测系统 CJK 字体，找到就用，找不到静默回退。"""
    from matplotlib import font_manager
    have = {f.name for f in font_manager.fontManager.ttflist}
    for name in ("Microsoft YaHei", "SimHei", "SimSun", "Noto Sans CJK SC",
                 "Source Han Sans SC", "PingFang SC", "Heiti SC"):
        if name in have:
            matplotlib.rcParams["font.sans-serif"] = [name, "DejaVu Sans"]
            matplotlib.rcParams["axes.unicode_minus"] = False
            return name
    matplotlib.rcParams["axes.unicode_minus"] = False
    return None


def _ppm_label(p):
    """把摩尔分数（ppm 数）格式化成图例标签（与 plot_multi_x 同源）。"""
    if p >= 1e-3:
        return f"{p:.3g} ppm"
    if p >= 1e-6:
        return f"{p:.2e} ppm"
    return f"{p:.1e} ppm"


def plot_wms_instrument(r, out_png, panels="all", multi=None):
    """TDLAS 仪器链路标准图（3×2 六子图，统一格式）：
    ① 波长调制 → ② PD 原始信号(DAS 无调制) → DAS αL+理论 → ③ 1f → ④ 2f → ⑤ 归一化 2f。

    panels: 默认 "all"（标准 6 图，布局/外观与既有完全一致）；也可传子集标识列表
    ["drive","das","al","f1","f2","norm"] 只画所需面板（非 6 个时改竖排堆叠）。
    混合气时在 αL 面板（al）叠加各组分 αL 曲线（不新增图，保持出图规格不变）。

    multi: 多浓度同图——list[(ppm: float, rr: dict)]，**含要叠加的全部浓度**（含基准 r）。
    给出后 ②~⑤ 面板按浓度配色叠加（viridis + ppm 图例），纵轴按全部浓度的有效区统一
    定幅；① 驱动电压各浓度共用故仍画一条。不传 = 原有单浓度画法（向后兼容）。
    """
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    use_cjk_font(matplotlib)

    m = r["meta"]
    nu, off, ok = r["nu_axis"], r["offband_mask"], m["onef_valid"]
    vd = r["valid_mask"]
    # 多浓度叠加：multi=[(ppm, result)]；不足 2 个浓度则退化为原单浓度画法
    multi = [it for it in (multi or []) if it and len(it) == 2]
    if len(multi) < 2:
        multi = None
    _mtag = "（多浓度叠加）" if multi else ""

    def seg(a_, y, color, label, lw=1.2, ls="-", _nu=None, _vd=None, _ylim=True):
        """有效区实线 + 剔除区灰点线；y 轴范围只看有效区，避免剔除区伪影压扁曲线。

        注意：曲线本身可能有上万点，而画布只有几百像素，逐点连线会互相穿插成
        "乱麻"（绘图混叠）。这里做等间隔抽取，保证每像素至多 ~1 个点。

        _nu/_vd：多浓度叠加时传该浓度自己的波数轴与有效区掩码（缺省用基准 r 的）；
        _ylim=False：叠加时不逐条定幅，统一交给 `_ylim_multi`。
        """
        nu_ = nu if _nu is None else _nu
        vd_ = vd if _vd is None else _vd
        stride = max(1, int(round(nu_.size / 1200)))
        sl = slice(None, None, stride)
        a_.plot(nu_[vd_][sl], y[vd_][sl], ls, color=color, lw=lw, label=label)
        if (~vd_).any():
            a_.plot(nu_[~vd_][sl], y[~vd_][sl], ":", color="0.6", lw=1.0)
        yv = y[vd_]
        if _ylim and yv.size:
            lo, hi = min(float(np.min(yv)), 0.0), max(float(np.max(yv)), 0.0)
            pad = 0.08 * (hi - lo) or 1e-12
            a_.set_ylim(lo - pad, hi + pad)

    def _ylim_multi(a_, items):
        """多浓度叠加后按**全部**浓度的有效区统一纵轴范围。

        逐条 seg() 会把 ylim 不断覆盖成最后一条曲线的范围，导致低浓度被压成平线；
        这里汇总后再定幅，保证各浓度的相对大小真实可比。
        """
        lo = hi = 0.0
        for y, vd_ in items:
            yv = y[vd_]
            if yv.size:
                lo = min(lo, float(np.min(yv)))
                hi = max(hi, float(np.max(yv)))
        pad = 0.08 * (hi - lo) or 1e-12
        a_.set_ylim(lo - pad, hi + pad)

    def _multi_seg(a_, multi, key):
        """多浓度叠加：按 viridis 配色把各浓度的同名曲线画进同一坐标轴。"""
        cols = plt.cm.viridis(np.linspace(0.08, 0.92, len(multi)))
        items = []
        for (_p, _rr), _col in zip(multi, cols):
            y = _rr[key]
            seg(a_, y, _col, _ppm_label(_p), lw=1.1, _ylim=False,
                _nu=_rr["nu_axis"], _vd=_rr["valid_mask"])
            items.append((y, _rr["valid_mask"]))
        _ylim_multi(a_, items)

    def _draw(key, a_):
        if key == "drive":
            a_.plot(r["t_cyc"] * 1e3, r["v_drive_cyc"], lw=0.6, color="0.2")
            a_.set_ylabel("驱动电压 (V)")
            a_.set_xlabel("时间 (ms)")
            a_.set_title(f"① 波长调制 V(t)   fm={m['mod_freq_Hz'] / 1e3:g} kHz, "
                         f"fscan={m['fscan_Hz']:g} Hz")
        elif key == "das":
            if multi:
                _multi_seg(a_, multi, "v_das_cyc")
            else:
                seg(a_, r["v_das_cyc"], "C0", "PD 原始信号")
            a_.set_ylabel("PD 信号 (V)")
            a_.set_xlabel(r"波数 (cm$^{-1}$)")
            a_.set_title("② 直接吸收 DAS（无调制）" + _mtag)
        elif key == "al":
            mix = r.get("alphaL_per_species")
            if multi:
                _multi_seg(a_, multi, "das_cyc")
                # 理论 αL 只画基准浓度一条作参考（各浓度理论值同形只差缩放，全画会糊）
                _p0, _r0 = multi[0]
                seg(a_, _r0["alphaL_cyc"], "0.35",
                    f"HITRAN 理论 αL（{_ppm_label(_p0)}）", lw=1.0, ls="--", _ylim=False,
                    _nu=_r0["nu_axis"], _vd=_r0["valid_mask"])
                _ylim_multi(a_, [(y, _rr["valid_mask"]) for _p, _rr in multi
                                 for y in (_rr["das_cyc"], _rr["alphaL_cyc"])])
            else:
                seg(a_, r["das_cyc"], "C0", "DAS αL（实测，-ln 提取）")
                seg(a_, r["alphaL_cyc"], "C2", "HITRAN 理论 αL（虚线=数据库参考）", lw=1.2, ls="--")
                if mix:                               # 混合气：叠加各组分 αL
                    cols = plt.cm.viridis(np.linspace(0.1, 0.85, max(len(mix), 2)))
                    for _c, _p in zip(cols, mix):
                        seg(a_, _p["alphaL"], _c, _p["label"], lw=0.9, ls="-.")
            a_.axhline(0.0, color="k", lw=0.5, alpha=0.4)
            a_.set_ylabel(r"$\alpha L$")
            a_.set_xlabel(r"波数 (cm$^{-1}$)")
            a_.set_title("DAS 吸光度 vs 数据库理论值" + (_mtag if multi else
                                                      ("（含混合气各组分）" if mix else "")))
        elif key == "f1":
            if multi:
                _multi_seg(a_, multi, "S1f_cyc")
            else:
                seg(a_, r["S1f_cyc"], "C1", "1f")
            a_.set_ylabel("1f (V)")
            a_.set_xlabel(r"波数 (cm$^{-1}$)")
            a_.set_title("③ 一阶谐波 1f" + _mtag)
        elif key == "f2":
            if multi:
                _multi_seg(a_, multi, "S2f_cyc")
            else:
                seg(a_, r["S2f_cyc"], "C3", "2f")
            a_.axhline(0.0, color="k", lw=0.5, alpha=0.4)
            a_.set_ylabel("2f (V)")
            a_.set_xlabel(r"波数 (cm$^{-1}$)")
            a_.set_title("④ 二阶谐波 2f" + _mtag)
        elif key == "norm":
            if multi:
                _multi_seg(a_, multi, "S2f_norm_cyc")
            elif ok:
                label = "2f/1f" if m["bg_subtracted"] else "2f/1f（未扣背景）"
                seg(a_, r["S2f_norm_cyc"], "C2", label, 1.6)
            else:
                seg(a_, r["S2f_norm_cyc"], "C2", f"2f/I0（I0={m['I0_pd_V']:.3g} V）", 1.6)
            a_.axhline(0.0, color="k", lw=0.5, alpha=0.4)
            a_.set_ylabel("归一化 2f (a.u.)")
            a_.set_xlabel(r"波数 (cm$^{-1}$)")
            a_.set_title(f"⑤ 归一化：{m['norm_method']}" + _mtag)

    _ALL = ["drive", "das", "al", "f1", "f2", "norm"]
    _SPECTRAL = {"das", "al", "f1", "f2", "norm"}
    if panels in (None, "all"):
        keys = _ALL
    elif isinstance(panels, str):
        keys = [p.strip() for p in panels.split(",") if p.strip() in _ALL] or _ALL
    else:
        keys = [p for p in panels if p in _ALL] or _ALL
    n = len(keys)
    # 标准 6 图为 3×2（每子图约 2:1，横长竖短利于读线形，贴近黄金矩形）；
    # 子集时改竖排堆叠，避免留空位。
    if n == 6:
        fig, ax = plt.subplots(3, 2, figsize=(14, 10))
    else:
        fig, ax = plt.subplots(n, 1, figsize=(9, 3.2 * n))
    ax = list(np.ravel(ax)) if n > 1 else [ax]
    for a_, k in zip(ax, keys):
        _draw(k, a_)

    # 剔除区（三角波转折点）标红 + 灰点线图例说明（仅谱图类面板）
    nd = int(m.get("n_trim", 0))
    spec_ax = [a_ for a_, k in zip(ax, keys) if k in _SPECTRAL]
    if 0 < nd < r["n_per"]:
        for s_ in (nu[:nd], nu[-nd:]):
            if s_.size:
                for a_ in spec_ax:
                    a_.axvspan(min(s_.min(), s_.max()), max(s_.min(), s_.max()),
                               color="red", alpha=0.06)
        for a_ in spec_ax:
            a_.plot([], [], ":", color="0.6", lw=1.5,
                    label="点线=剔除区（转折点，不可信）")

    for a_ in ax:
        a_.grid(alpha=0.3)
        a_.legend(loc="upper right", fontsize=6 if multi else 7)
    _x_tag = ("/".join(_ppm_label(_p) for _p, _rr in multi) if multi
              else f"{m['x']:g}")
    fig.suptitle(f"TDLAS 仪器链路 — {m['species']} @ {m['wn_center']:.3f} cm$^{{-1}}$   "
                 f"x={_x_tag}, L={m['L_cm']:g} cm, m={m['mod_coeff_m']:.2f}   "
                 f"归一化 = {m['norm_method']}", fontsize=11)
    fig.tight_layout()
    fig.savefig(out_png, dpi=150)
    return out_png


# ══════════════════ 9. 绘图 ══════════════════

def plot(r, out_png):
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    m = r["meta"]
    fig, ax = plt.subplots(3, 1, figsize=(9, 8), sharex=True)
    ax[0].plot(r["nu"], r["alpha"], label="total α")
    _mix = r.get("alpha_per_species")              # 混合气：叠加各组分 α（不新增图）
    if _mix:
        _cols = plt.cm.viridis(np.linspace(0.1, 0.8, max(len(_mix), 2)))
        for _c, _p in zip(_cols, _mix):
            ax[0].plot(r["nu"], _p["alpha"], "-.", lw=0.9, color=_c, label=_p["label"])
        ax[0].legend(loc="upper right", fontsize=7)
    ax[0].set_ylabel(r"$\alpha$ (cm$^{-1}$)")
    ax[0].set_title(f"{m['species']} @ {m['wn0']:.3f} cm$^{{-1}}$   "
                    f"T={m['T']:g} K, P={m['P']:g} atm, x={m['x']}, L={m['L']:g} cm, "
                    f"a={m['a']:g} cm$^{{-1}}$")
    ax[1].plot(r["wn_scan"], r["tau"])
    ax[1].set_ylabel(r"$\tau$ (DAS)")
    ax[2].plot(r["wn_scan"], r["S2f1f"], label="WMS-2f/1f")
    ax[2].plot(r["wn_scan"], r["S2f"] / max(float(r["S2f"].max()), 1e-30),
               "--", alpha=0.6, label="WMS-2f (norm.)")
    ax[2].set_ylabel("WMS signal (a.u.)")
    ax[2].set_xlabel(r"wavenumber (cm$^{-1}$)")
    ax[2].legend()
    for a_ in ax:
        a_.grid(alpha=0.3)
    fig.tight_layout()
    fig.savefig(out_png, dpi=150)
    return out_png


def plot_multi_x(out_png, ppm, curves, x, xlabel, ylabel, title, valid=None):
    """多浓度同图（兼容性操作）：同一物种不同浓度的一条响应曲线叠加到一张图。

    用于「多浓度标定曲线 / 混合气对照」——把各浓度（或各组分）的谱曲线画在同一坐标轴，
    共享横轴 x（波数 / 扫描波数）等长 curves。valid 为可选的有效区掩码列表，
    用于把剔除区画成灰虚线（与 plot_wms_instrument 同源画法）。
    ppm: 摩尔分数 ppm（图例标签）；curves: list[np.ndarray]。

    ⚠ 与「混合气多组分」的区别：本函数按**浓度**叠同一物种的曲线；多组分叠加走
    `alphaL_per_species`（见各 plot_* 的 per-species 分支），两者不要混用。
    """
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    use_cjk_font(matplotlib)
    n = len(curves)
    fig, ax = plt.subplots(figsize=(9, 5.5))
    cols = plt.cm.viridis(np.linspace(0.08, 0.92, max(n, 2)))
    for i, (c, p) in enumerate(zip(curves, ppm)):
        c = np.asarray(c)
        col = cols[i]
        if p >= 1e-3:
            lab = f"{p:.3g} ppm"
        elif p >= 1e-6:
            lab = f"{p:.2e} ppm"
        else:
            lab = f"{p:.1e} ppm"
        ax.plot(x, c, color=col, lw=1.3, label=lab)
        if valid is not None and valid[i] is not None:
            vmask = np.asarray(valid[i], dtype=bool)
            if (~vmask).any():
                ax.plot(x[~vmask], c[~vmask], ":", color="0.6", lw=1.0)
    ax.set_xlabel(xlabel)
    ax.set_ylabel(ylabel)
    ax.set_title(title)
    ax.legend(title="摩尔分数 x", fontsize=9, loc="best")
    ax.grid(alpha=0.3)
    fig.tight_layout()
    fig.savefig(out_png, dpi=140)
    plt.close(fig)
    return out_png


def main():
    r = selftest()
    if "--selftest" in sys.argv:
        return
    out_dir = Path(__file__).resolve().parent / "tmp"
    out_dir.mkdir(parents=True, exist_ok=True)
    png = plot(r, out_dir / "wms_demo.png")
    print(f"  WMS 图已保存：{png}")
    d = simulate_das_td(species="CH4", wn0=2968.5, span=1.5, x=1e-4)
    png2 = plot_das_td(d, out_dir / "das_chain.png")
    print(f"  DAS 链路图已保存：{png2}")


if __name__ == "__main__":
    main()
