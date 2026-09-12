#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""TDLAS/WMS 仿真核心（最小可跑原型）。

链路：
    HITRAN α(ν) ──► 扫描 + 高频调制的瞬时波长 ν(t) ──► τ(t) = exp(-αL)
                ──► 免标定 WMS 解析（傅里叶系数）──► 1f/2f 谐波
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
    """免标定 WMS：由 τ(t) 与强度调制参数给出 1f/2f/4f 的 (X, Y) 分量。

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

def get_alpha(species, wn_lo, wn_hi, T, P, x, step=5e-4, wingHW=20.0):
    """取吸收系数 α(ν) [cm^-1]；混合气按 α = x·α_pure（总压 P、空气浴）缩放。

    窗口未覆盖 / 空谱由 tdlas_hitran 直接报错，绝不静默返回平谱。
    """
    nu, alpha_pure, info = th.absorption(species, wn_lo, wn_hi, T=T, P=P,
                                         step=step, wingHW=wingHW)
    info["mole_frac"] = float(x)
    return nu, alpha_pure * float(x), info


# ══════════════════ 3. 扫描式 WMS 正向仿真 ══════════════════

def simulate(species="H2O", wn0=7185.596, T=296.0, P=1.0, x=0.1, L=30.0,
             a=0.10, i0=0.1671, i2=2.48e-3,
             psi1=1.9356 * np.pi, psi2=4.4138 * np.pi,
             span=0.8, n_scan=401, n_mod=96, step=5e-4,
             sigma_tau=0.0, seed=None):
    """扫描式 WMS 正向仿真。

    species/wn0   : 分子与目标线中心（cm^-1）
    T / P / x / L : 温度 K / 压力 atm / 摩尔分数 / 光程 cm
    a             : 调制深度（cm^-1）
    i0,i2,psi1,psi2: 激光强度调制参数（AM 幅度与 IM-FM 相位差）
    span/n_scan/n_mod: 扫描半宽 cm^-1 / 扫描点数 / 每点调制周期采样数
    sigma_tau/seed: 等效透过率噪声标准差（>0 时注入）与随机种子

    返回：nu, alpha, tau, wn_scan, S1f, S2f, S4f, S2f1f, meta
    """
    if not 0.0 < x <= 1.0:
        raise ValueError(f"摩尔分数必须在 (0, 1]，收到 {x}")
    if a <= 0:
        raise ValueError(f"调制深度 a 必须 > 0，收到 {a}")

    nu, alpha, info = get_alpha(species, wn0 - span, wn0 + span, T, P, x, step=step)

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

    meta = {"species": species, "wn0": wn0, "T": T, "P": P, "x": x, "L": L,
            "a": a, "i0": i0, "i2": i2, "psi1": psi1, "psi2": psi2,
            "n_lines_in_window": info["n_lines_in_window"], "table": info["table"]}
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
    d = simulate_das_td(x=1e-3, baseline_slope=0.05, fit_order=3)
    tp = float(d["alpha_L_true"].max())
    dp = float(d["das"].max())
    derr = abs(dp - tp) / tp
    print(f"  DAS 链路（三角波、基线斜率 0.05、3 阶拟合）：吸光度峰 {dp:.4e} "
          f"vs 真值 αL {tp:.4e}（差 {derr * 100:.2f}%）")
    assert derr < 0.05, f"DAS 基线扣除误差 {derr:.1%} 超过 5%"

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


# ══════════════════ 6. 数字锁相（时域交叉验证）══════════════════

def wms_harmonic_lockin(S, t, fs, fm, n, n_cycle_avg=1):
    """数字锁相：正交解调 + 整数调制周期滑动平均。

    低通用"一个调制周期窗口的滑动平均"实现 —— 在 fc/fs 极低（扫描/调制频率比很大）
    时比 Butterworth 数值更稳健（后者在归一化截止 ~1e-3 时会出现病态），
    对无噪 / 低噪仿真足够。

    S : 时域探测器信号；fs / fm : 采样率 / 调制频率 (Hz)
    n : 谐波阶数；n_cycle_avg : 平均的调制周期数（越大带宽越窄、越平滑）
    """
    w = 2.0 * np.pi * fm
    S = np.asarray(S, dtype=float)
    win = max(1, int(round(n_cycle_avg * fs / fm)))
    ker = np.ones(win) / win
    X = np.convolve(S * np.cos(n * w * t), ker, mode="same")
    Y = np.convolve(S * np.sin(n * w * t), ker, mode="same")
    return np.sqrt(X ** 2 + Y ** 2), X, Y


def simulate_td(species="H2O", wn0=7185.596, T=296.0, P=1.0, x=0.1, L=30.0,
                a=0.10, i0=0.1671, i2=2.48e-3,
                psi1=1.9356 * np.pi, psi2=4.4138 * np.pi,
                i_s=0.1661, psi_s=1.411 * np.pi,
                span=0.8, fscan=100.0, fm=1e4, fs=5e5, n_cycle_avg=1, step=5e-4):
    """时域仿真：生成探测器信号 I(t)=I₀(t)·τ(ν(t))，再用数字锁相提取 1f/2f。

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

def simulate_das_td(species="H2O", wn0=7185.596, T=296.0, P=1.0, x=0.1, L=30.0,
                    span=0.8, fscan=100.0, fs=5e5, baseline_slope=0.05,
                    sigma=0.0, seed=None, fit_order=3, fit_frac=0.3, step=5e-4):
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

    返回：t, wn, tri, I0, It, baseline, das, alpha, alpha_L_true, meta
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

    nu, alpha, info = get_alpha(species, wn0 - span - 0.2, wn0 + span + 0.2, T, P, x, step=step)
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
    das = -np.log(np.maximum(It, 1e-12) / np.maximum(baseline, 1e-12))

    meta = {"species": str(species).upper(), "wn0": float(wn0), "T": float(T), "P": float(P),
            "x": float(x), "L": float(L), "span": float(span), "fscan": float(fscan),
            "fs": float(fs), "baseline_slope": float(baseline_slope), "sigma": float(sigma),
            "fit_order": int(fit_order), "fit_frac": float(fit_frac),
            "n_lines_in_window": info["n_lines_in_window"], "table": info["table"]}
    return {"t": t, "wn": wn, "tri": tri, "I0": I0, "It": It, "baseline": baseline,
            "das": das, "alpha": alpha, "alpha_L_true": alpha_wn * float(L), "meta": meta}


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
                    f"T={m['T']:g} K, P={m['P']:g} atm, x={m['x']:g}, L={m['L']:g} cm, "
                    f"slope={m['baseline_slope']:g}, baseline fit order={m['fit_order']}")
    ax[1].plot(t_ms, r["I0"])
    ax[1].set_ylabel(r"$I_0$ (a.u.)")
    ax[1].set_xlabel("time (ms)")
    ax[2].plot(t_ms, r["It"], ".", ms=2, label=r"PD raw  $I_t(t)$")
    ax[2].plot(t_ms, r["baseline"], "-", lw=1.2, label="fitted baseline")
    ax[2].set_ylabel(r"$I_t$ (a.u.)")
    ax[2].set_xlabel("time (ms)")
    ax[2].legend()
    ax[3].plot(r["wn"], r["das"], ".", ms=2, label="DAS (baseline-subtracted)")
    ax[3].plot(r["wn"], r["alpha_L_true"], "-", lw=1, alpha=0.7, label=r"true $\alpha L$")
    ax[3].set_ylabel("absorbance")
    ax[3].set_xlabel(r"wavenumber (cm$^{-1}$)")
    ax[3].legend()
    for a_ in ax:
        a_.grid(alpha=0.3)
    fig.tight_layout()
    fig.savefig(out_png, dpi=150)
    return out_png


# ══════════════════ 8. 绘图 ══════════════════

def plot(r, out_png):
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    m = r["meta"]
    fig, ax = plt.subplots(3, 1, figsize=(9, 8), sharex=True)
    ax[0].plot(r["nu"], r["alpha"])
    ax[0].set_ylabel(r"$\alpha$ (cm$^{-1}$)")
    ax[0].set_title(f"{m['species']} @ {m['wn0']:.3f} cm$^{{-1}}$   "
                    f"T={m['T']:g} K, P={m['P']:g} atm, x={m['x']:g}, L={m['L']:g} cm, "
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


def main():
    r = selftest()
    if "--selftest" in sys.argv:
        return
    out_dir = Path(__file__).resolve().parent / "tmp"
    out_dir.mkdir(parents=True, exist_ok=True)
    png = plot(r, out_dir / "wms_demo.png")
    print(f"  WMS 图已保存：{png}")
    d = simulate_das_td(x=1e-3)
    png2 = plot_das_td(d, out_dir / "das_chain.png")
    print(f"  DAS 链路图已保存：{png2}")


if __name__ == "__main__":
    main()
