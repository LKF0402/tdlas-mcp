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
    d = simulate_das_td(x=1e-4, L=30.0, baseline_slope=0.05, fit_order=3)
    tp = float(d["alpha_L_true"].max())
    dp = float(d["das"].max())
    derr = abs(dp - tp) / tp
    print(f"  DAS 链路（三角波·上升沿、基线斜率 0.05、3 阶拟合）：吸光度峰 {dp:.4e} "
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

def wms_harmonic_lockin(S, t, fs, fm, n, n_cycle_avg=1, n_stages=2):
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
    X = S * np.cos(n * w * t)
    Y = S * np.sin(n * w * t)
    # 级联 n_stages 级滑动平均：单级矩形窗频响是 sinc（旁瓣仅 -13 dB），
    # 级联后为 sinc^N（-13N dB），对残留 2fm/4fm 的抑制显著改善。
    # 实测 1→2 级把非吸收区残留从 4.1% 降到 1.6%；再往上收益很小。
    for _ in range(max(1, int(n_stages))):
        X = np.convolve(X, ker, mode="same")
        Y = np.convolve(Y, ker, mode="same")
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

def simulate_das_td(species="H2O", wn0=7185.596, T=296.0, P=1.0, x=1e-3, L=50.0,
                    span=0.8, fscan=100.0, fs=5e5, baseline_slope=0.05,
                    sigma=0.0, seed=None, fit_order=3, fit_frac=0.3, step=5e-4,
                    edge="rising"):
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
    das_full = -np.log(np.maximum(It, 1e-12) / np.maximum(baseline, 1e-12))

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
    aL = float(alpha_wn.max()) * float(L)
    if aL > 0.1:
        warnings.append(f"αL ≈ {aL:.3g} 偏大（>0.1）：DAS 进入非线性区、峰形被压低，"
                        f"建议降低浓度或光程（当前 x={x:g}, L={L:g} cm）")
    elif aL < 1e-4:
        warnings.append(f"αL ≈ {aL:.3g} 偏小（<1e-4）：吸收太弱、信噪比可能不足，"
                        f"建议加大光程或浓度（当前 x={x:g}, L={L:g} cm）")
    if float(span) < 0.2:
        warnings.append(f"扫描半宽 span={span:g} cm^-1 偏窄：可能未完整覆盖吸收线两翼，"
                        f"基线拟合会偏")

    meta = {"species": str(species).upper(), "wn0": float(wn0), "T": float(T), "P": float(P),
            "x": float(x), "L": float(L), "span": float(span), "fscan": float(fscan),
            "fs": float(fs), "baseline_slope": float(baseline_slope), "sigma": float(sigma),
            "fit_order": int(fit_order), "fit_frac": float(fit_frac), "edge": edge,
            "alpha_L_peak": aL, "warnings": warnings,
            "n_lines_in_window": info["n_lines_in_window"], "table": info["table"]}
    return {"t": t, "wn": wn, "tri": tri, "I0": I0, "It": It, "baseline": baseline,
            "das_full": das_full, "das": das, "wn_das": wn_das,
            "alpha": alpha, "alpha_L_true": alpha_wn * float(L), "meta": meta}


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
    half = r["t"].size // 2                    # 三角波：前半周期上升、后半周期下降
    dfl = r["das_full"]
    ax[3].plot(r["wn"][:half], dfl[:half], ".", ms=2, color="C0", alpha=0.45, label="DAS — rising edge")
    ax[3].plot(r["wn"][half:], dfl[half:], ".", ms=2, color="C3", alpha=0.45, label="DAS — falling edge")
    ax[3].plot(r["wn_das"], r["das"], "-", lw=1.2, color="C2", label=f"final DAS ({m['edge']})")
    ax[3].plot(r["wn"], r["alpha_L_true"], "--", lw=1, alpha=0.7, color="k", label=r"true $\alpha L$")
    ax[3].set_ylabel("absorbance")
    ax[3].set_xlabel(r"wavenumber (cm$^{-1}$)")
    ax[3].legend()
    for a_ in ax:
        a_.grid(alpha=0.3)
    fig.tight_layout()
    fig.savefig(out_png, dpi=150)
    return out_png


# ══════════════════ 8. 仪器系统链路（DAQ 电压 → 激光 → 光路 → PD → ADC）══════════════════

# 内置默认参数：NI USB-6211 + 典型中红外 DFB 激光器 + CH4 @2968.5 cm⁻¹ 场景。
# 参数缺省时的索取优先级：① 用户给实测值 → ② 用户给器件型号（由 AI 检索规格书）
# → ③ 用户现场标定（如"电压变化 ΔV → 波数变化 Δν"）→ ④ 用下列默认值并明确标注。
DAQ_DEFAULTS = {
    "fs": 100e3,             # 采样率 Hz（DAQ 助手默认 100 kHz）
    "n_samples": 100_000,    # 采样点数（100k）
    "adc_bits": 16,          # ADC 位数（USB-6211：16-bit）
    "v_range": 10.0,         # ADC 输入量程 ±V（USB-6211：±10 V）
}
SCAN_DEFAULTS = {
    "amp_V": 0.71,           # 三角波幅值 V → 扫描半宽 ≈ amp_V × |η_VI·dν/dI| ≈ 1.5 cm⁻¹
    "freq_Hz": 100.0,        # 三角波频率 Hz
    "offset_V": 3.20,        # 三角波偏置 V → I=76.8 mA → 2968.5 cm⁻¹（对齐 CH4 目标线）
    "phase_deg": 0.0,        # 起始相位°（三角波中心对称、先上升后下降）
}
LASER_DEFAULTS = {
    "eta_VI": 24.0,          # V→I 跨导 mA/V（驱动器线性估算：Vop 5 V / Iop 120 mA）
    "dnu_dI": -0.088,        # I→波数 调谐系数 cm⁻¹/mA（官方 0.10 nm/mA @3373 nm 换算）
    "wn_ref": 2964.7,        # 参考波数 cm⁻¹（10⁷/3373 nm）
    "i_ref": 120.0,          # 参考电流 mA（Iop max）
    "i_th": 30.0,            # 阈值电流 mA（低于此无激光输出）
    "eta_IP": 0.15,          # I→P 斜率效率 mW/mA（工作点 71 mA→6.4 mW 反推）
}
OPTICS_DEFAULTS = {"throughput": 0.90}   # 光学元件总透过率（窗片/镜片）
PD_DEFAULTS = {
    "resp": 0.9,             # 响应度 A/W（InGaAs）
    "gain": 1e3,             # 跨阻增益 V/A（适配 mW 光功率与 ±10 V 量程）
    "bw": 1e6,               # 探测器 + 前放带宽 Hz
    "rin": 0.0,              # 相对强度噪声 /√Hz（默认关 = 理想仿真）
    "drift_frac": 0.0,       # 1/f 慢漂移幅度（默认关）
    "flicker_frac": 0.0,     # 1/f 粉红噪声幅度（默认关）
}
# WMS 调制参数（正弦调制）。调制幅值默认自动优化：使调制系数 m = a/HWHM ≈ 2.2，
# 这是 2f 谐波幅值最大、检测灵敏度最优的经典取值（Arndt 1965 / Reid & Labrie 1981）。
MOD_DEFAULTS = {
    "mod_amp_V": None,       # 正弦调制幅值 V；None = 自动按 m_opt 优化
    "mod_freq_Hz": 30e3,     # 调制频率 Hz
    "mod_phase_deg": 0.0,    # 调制相位°
    "m_opt": 2.2,            # 最优调制系数 m = a/HWHM（2f 峰值最大处）
    "lockin_avg": 1,         # 锁相滑动平均的调制周期数（>1 会模糊线形且不降残留）
    "lockin_stages": 2,      # 低通级联级数：2 级把残留从 4.1% 降到 1.6%（再高收益小）
    "trim_frac": 0.12,       # 剔除扫描两端比例：三角波转折点导数不连续，
                             # 其高频谐波会泄漏进 2f，必须丢弃（WMS 标准做法）
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


def daq_triangle(t, amp_V, freq_Hz, phase_deg=0.0, offset_V=0.0):
    """DAQ 模拟输出通道的三角波驱动电压（中心对称、先上升后下降）。"""
    period = 1.0 / float(freq_Hz)
    ph = (np.asarray(t, dtype=float) / period + float(phase_deg) / 360.0) % 1.0
    tri = 1.0 - 4.0 * np.abs(ph - 0.5)                 # 0→-1, 0.5→+1, 1→-1（先升后降）
    return float(offset_V) + float(amp_V) * tri


def voltage_to_laser(v, eta_VI=24.0, dnu_dI=-0.088, wn_ref=2964.7, i_ref=120.0,
                     i_th=30.0, eta_IP=0.15):
    """驱动电压 → 激光器电流 / 波数 / 输出功率。返回 (i_mA, nu_cm-1, p_mW)。"""
    i = float(eta_VI) * np.asarray(v, dtype=float)
    nu = float(wn_ref) + (i - float(i_ref)) * float(dnu_dI)
    p = np.maximum(float(eta_IP) * (i - float(i_th)), 0.0)   # 低于阈值电流无输出
    return i, nu, p


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


def simulate_das_instrument(species="CH4", wn_center=2968.5, T=296.0, P=1.01325,
                            x=1e-3, L_cm=50.0, edge="rising", fit_order=3,
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
           **OPTICS_DEFAULTS, **PD_DEFAULTS}
    cfg.update({k: v for k, v in kw.items() if v is not None})
    # ★ 同 WMS：让激光波数轴中心落在 wn_center，否则与吸收谱错位
    if kw.get("wn_ref") is None:
        i_mid = float(cfg["eta_VI"]) * float(cfg["offset_V"])
        cfg["wn_ref"] = float(wn_center) - (i_mid - float(cfg["i_ref"])) * float(cfg["dnu_dI"])

    fs = float(cfg["fs"])
    n_samples = int(cfg["n_samples"])
    period = 1.0 / float(cfg["freq_Hz"])
    t = np.arange(n_samples, dtype=float) / fs

    # ① DAQ 三角波驱动电压
    v_drive = daq_triangle(t, cfg["amp_V"], cfg["freq_Hz"], cfg["phase_deg"], cfg["offset_V"])

    # ② V → I → 波数 / 功率
    i_laser, nu_laser, p_laser = voltage_to_laser(
        v_drive, cfg["eta_VI"], cfg["dnu_dI"], cfg["wn_ref"], cfg["i_ref"],
        cfg["i_th"], cfg["eta_IP"])

    # ③ 光路：光学损耗 + 气体吸收
    span = float(cfg["amp_V"]) * abs(float(cfg["eta_VI"]) * float(cfg["dnu_dI"]))
    nu_grid, alpha_pure, info = th.absorption(
        species, wn_center - span - 0.2, wn_center + span + 0.2, T=T, P=P,
        step=min(5e-4, span / 500.0), wingHW=max(10.0, 5.0 * span))
    alpha = np.interp(nu_laser, nu_grid, alpha_pure) * float(x)
    tau = np.exp(-alpha * float(L_cm))
    p_opt = p_laser * float(cfg["throughput"]) * tau          # 到达 PD 的光功率 mW

    # ④ PD：光功率 → 光电流 → 跨阻电压
    i_pd = p_opt * 1e-3 * float(cfg["resp"])                  # A
    v_pd = i_pd * float(cfg["gain"])                          # V

    # ⑤ 噪声
    rng = np.random.default_rng(seed)
    s_shot, s_therm, s_rin = noise_currents(float(np.mean(i_pd)), cfg["bw"],
                                            cfg["gain"], cfg["rin"], T)
    s_total = float(np.sqrt(s_shot ** 2 + s_therm ** 2 + s_rin ** 2))
    v_noisy = v_pd + rng.normal(0.0, s_total * float(cfg["gain"]), v_pd.shape)

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
    aL_peak = float(alpha.max()) * float(L_cm)
    if aL_peak > 0.1:
        warnings.append(f"αL≈{aL_peak:.3g} 偏大（>0.1）：DAS 进入非线性区、峰形被压低，"
                        f"建议降低浓度或光程（x={x:g}, L={L_cm:g} cm）")
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

    meta = {"species": str(species).upper(), "wn_center": float(wn_center), "T": float(T),
            "P": float(P), "x": float(x), "L_cm": float(L_cm), "edge": edge,
            "span_cm-1": span, "scan_lo": float(nu_laser.min()),
            "scan_hi": float(nu_laser.max()), "alpha_L_peak": aL_peak,
            "v_pd_mean": float(np.mean(v_pd)), "lsb_V": lsb, "n_sat": n_sat,
            "sigma_v_noise": v_noise_rms, "sigma_shot": s_shot * cfg["gain"],
            "sigma_thermal": s_therm * cfg["gain"], "sigma_rin": s_rin * cfg["gain"],
            "cfg": dict(cfg), "warnings": warnings,
            "n_lines_in_window": info["n_lines_in_window"], "table": info["table"]}
    return {"t": t, "v_drive": v_drive, "i_laser": i_laser, "nu_laser": nu_laser,
            "p_laser": p_laser, "p_opt": p_opt, "v_pd": v_pd, "v_adc": v_adc,
            "nu_das": nu_das, "das": das, "das_full": das_full, "baseline": baseline,
            "meta": meta}


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
                    f"T={m['T']:g} K, P={m['P']:g} atm, x={m['x']:g}, L={m['L_cm']:g} cm, "
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
    ax[4].set_ylabel("absorbance")
    ax[4].set_xlabel(r"wavenumber (cm$^{-1}$)")
    ax[4].legend()
    for a_ in ax:
        a_.set_xlabel(a_.get_xlabel() or "time (ms)")
        a_.grid(alpha=0.3)
    fig.tight_layout()
    fig.savefig(out_png, dpi=150)
    return out_png


def simulate_wms_instrument(species="CH4", wn_center=2968.5, T=296.0, P=1.01325,
                            x=1e-3, L_cm=50.0, seed=None, **kw):
    """WMS 仪器链路仿真：三角波扫描 + 正弦调制 → 激光 → 光路 → PD → ADC → 数字锁相。

    与 DAS 的差别：驱动电压叠加**高频正弦调制** fm，探测器信号被编码到 fm 及其谐波，
    经正交解调 + 整数周期滑动平均得到 1f/2f 谐波；2f/1f 在弱吸收下 ∝ 浓度，
    且信号被搬离低频 → 天然规避 1/f 噪声与激光 RIN。

    调制幅值默认**自动优化**：使调制系数 m = a/HWHM ≈ 2.2（2f 峰值最大）。

    返回 dict：一个扫描周期内的 t, v_drive, v_scan, v_mod, nu_laser, p_laser, p_opt,
                v_adc, S1f, S2f, S2f1f, meta；另有全局序列供局部放大查看。
    """
    cfg = {**DAQ_DEFAULTS, **SCAN_DEFAULTS, **MOD_DEFAULTS, **LASER_DEFAULTS,
           **OPTICS_DEFAULTS, **PD_DEFAULTS}
    cfg.update({k: v for k, v in kw.items() if v is not None})
    # ★ 关键：让激光波数轴的中心落在 wn_center。否则吸收谱算在 wn_center、
    #   激光却在 wn_ref 附近 → 两者错位，锁相输出全是噪声/伪影（严重假信号）。
    if kw.get("wn_ref") is None:
        i_mid = float(cfg["eta_VI"]) * float(cfg["offset_V"])
        cfg["wn_ref"] = float(wn_center) - (i_mid - float(cfg["i_ref"])) * float(cfg["dnu_dI"])

    warnings = []
    fm = float(cfg["mod_freq_Hz"])
    fs = float(cfg["fs"])
    fs_min = 8.0 * fm                       # 每调制周期 ≥8 点，数字锁相才有足够精度
    if fs < fs_min:
        warnings.append(f"采样率 {fs / 1e3:.0f} kHz 不足以准确解调 {fm / 1e3:.0f} kHz 调制的 2f"
                        f"（建议 ≥ {fs_min / 1e3:.0f} kS/s，即每调制周期 ≥8 点）→ 已自动提升")
        fs = fs_min
    if fs > 250e3:
        warnings.append(f"所需采样率 {fs / 1e3:.0f} kS/s 超过 NI USB-6211 上限（250 kS/s）→ "
                        f"实际采集需更高采样率的 DAQ，或降低调制频率 fm")
    t = np.arange(int(cfg["n_samples"]), dtype=float) / fs
    fscan = float(cfg["freq_Hz"])

    # ① 驱动电压 = 三角波扫描 + 正弦调制
    v_scan = daq_triangle(t, cfg["amp_V"], fscan, cfg["phase_deg"], cfg["offset_V"])
    v_mod_sig = np.cos(2 * np.pi * fm * t + np.deg2rad(float(cfg["mod_phase_deg"])))

    span_scan = float(cfg["amp_V"]) * abs(float(cfg["eta_VI"]) * float(cfg["dnu_dI"]))
    nu_grid, alpha_pure, info = th.absorption(
        species, wn_center - span_scan - 0.3, wn_center + span_scan + 0.3, T=T, P=P,
        step=min(2e-4, span_scan / 2000.0), wingHW=max(10.0, 5.0 * span_scan))
    alpha_pure_mix = alpha_pure * float(x)
    auto_mod = cfg["mod_amp_V"] is None
    if auto_mod:
        mod_amp_V, a_cm1, hwhm, m_act = optimal_mod_amp_V(
            nu_grid, alpha_pure_mix, cfg["eta_VI"], cfg["dnu_dI"], cfg["m_opt"])
    else:
        mod_amp_V = float(cfg["mod_amp_V"])
        a_cm1 = mod_amp_V * abs(float(cfg["eta_VI"]) * float(cfg["dnu_dI"]))
        hwhm = estimate_hwhm(nu_grid, alpha_pure_mix)
        m_act = a_cm1 / hwhm if hwhm > 0 else float("nan")
    v_drive = v_scan + mod_amp_V * v_mod_sig

    # ② V → I → 波数 / 功率
    i_laser, nu_laser, p_laser = voltage_to_laser(
        v_drive, cfg["eta_VI"], cfg["dnu_dI"], cfg["wn_ref"], cfg["i_ref"],
        cfg["i_th"], cfg["eta_IP"])

    # 1/f 噪声：慢漂移（正弦）+ 粉红噪声（1/f RIN）。WMS 锁相是高通、天然抑制；
    # DAS 直接测 DC 光强、受害于它——这是 WMS 通常强于 DAS 的物理来源。
    rng = np.random.default_rng(seed)
    drift_frac = float(cfg.get("drift_frac", 0.0))
    flicker_frac = float(cfg.get("flicker_frac", 0.0))
    if drift_frac > 0:
        p_laser = p_laser * (1.0 + drift_frac * np.sin(2 * np.pi * 1.0 * t + 0.3))
    if flicker_frac > 0:
        p_laser = p_laser * (1.0 + flicker_frac * pink_noise(len(t), fs, rng))

    # ③ 光路
    alpha = np.interp(nu_laser, nu_grid, alpha_pure) * float(x)
    tau = np.exp(-alpha * float(L_cm))
    p_opt = p_laser * float(cfg["throughput"]) * tau

    # ④ PD
    i_pd = p_opt * 1e-3 * float(cfg["resp"])
    v_pd = i_pd * float(cfg["gain"])

    # ⑤ 噪声（白噪声：散粒 + 热 + RIN）
    s_shot, s_therm, s_rin = noise_currents(float(np.mean(i_pd)), cfg["bw"],
                                            cfg["gain"], cfg["rin"], T)
    s_total = float(np.sqrt(s_shot ** 2 + s_therm ** 2 + s_rin ** 2))
    v_noisy = v_pd + rng.normal(0.0, s_total * float(cfg["gain"]), v_pd.shape)

    # ⑥ ADC
    lsb = 2.0 * float(cfg["v_range"]) / float(2 ** int(cfg["adc_bits"]))
    n_sat = int(np.sum(np.abs(v_noisy) >= float(cfg["v_range"])))
    v_adc = np.clip(np.round(v_noisy / lsb) * lsb, -float(cfg["v_range"]), float(cfg["v_range"]))

    # ⑦ 数字锁相 → 1f / 2f
    avg, stg = int(cfg["lockin_avg"]), int(cfg["lockin_stages"])
    S1f, _, _ = wms_harmonic_lockin(v_adc, t, fs, fm, 1, avg, stg)
    S2f, _, _ = wms_harmonic_lockin(v_adc, t, fs, fm, 2, avg, stg)

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
    _, nu_scan_all, _ = voltage_to_laser(v_scan, cfg["eta_VI"], cfg["dnu_dI"],
                                         cfg["wn_ref"], cfg["i_ref"], cfg["i_th"], cfg["eta_IP"])
    nu_cyc = nu_scan_all[idx]
    S1f_cyc, S2f_cyc, v_cyc = S1f[idx], S2f[idx], v_adc[idx]
    alphaL_cyc = np.interp(nu_cyc, nu_grid, alpha_pure) * float(x) * float(L_cm)

    # ⑧ 归一化：优先 2f/1f；若 1f 失效则退化为 PD 非吸收区光强归一化 2f/I0
    #    · 2f/1f 的前提：1f 正比于光强。但 1f 实际正比于 L-I **斜率** dP/dV，随扫描变化，
    #      且扫描转折处 1f→0，相除会把伪影放大成假峰 → 必须检测并弃用。
    #    · 退化方案：I0 = 非吸收区 PD 信号平均，2f/I0 与光强无关且形状稳定（标准 DC 归一化）。
    n_keep = int(round(float(cfg["trim_frac"]) * n_sel))
    valid = np.ones(n_sel, dtype=bool)
    if n_keep > 0:                            # 单方向下两端都紧邻转折点
        valid[:n_keep] = False
        valid[-n_keep:] = False
    off = (alphaL_cyc < 0.02 * float(np.max(alphaL_cyc) + 1e-30)) & valid   # 非吸收区
    I0_pd = float(np.mean(np.abs(v_cyc[off]))) if off.any() else float(np.mean(np.abs(v_cyc[valid])))
    S2f_1f = np.divide(S2f_cyc, S1f_cyc, out=np.zeros_like(S2f_cyc), where=S1f_cyc > 1e-15)
    S2f_I0 = S2f_cyc / max(I0_pd, 1e-30)
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
    S2f_norm = S2f_1f if onef_valid else S2f_I0
    norm_method = "2f/1f" if onef_valid else "2f/I0（PD 非吸收区光强均值）"
    S2f1f = S2f_1f                     # 保留原名以向后兼容（始终输出，供用户自行判断）

    # ⑨ DAS 对照：DAS 是**不带调制**的直接吸收技术，所以不能用"对含调制信号做平均"
    #    来伪造 —— 那样平均窗口内波数仍在摆动，会折出锯齿。这里把调制置零、
    #    走同一条链路重算一次 PD 信号，才是真正的 DAS。
    _, nu_das, p_das = voltage_to_laser(v_scan, cfg["eta_VI"], cfg["dnu_dI"],
                                        cfg["wn_ref"], cfg["i_ref"], cfg["i_th"], cfg["eta_IP"])
    if drift_frac > 0:
        p_das = p_das * (1.0 + drift_frac * np.sin(2 * np.pi * 1.0 * t + 0.3))
    if flicker_frac > 0:
        p_das = p_das * (1.0 + flicker_frac * pink_noise(len(t), fs, rng))
    alpha_das = np.interp(nu_das, nu_grid, alpha_pure) * float(x)
    # ★ DAS 基线必须用"无吸收光强 I0"（仿真里可精确给出）。之前的"非吸收区多项式拟合"
    #   在密集谱区（如 C2H6 159 条线，无非吸收区）会失效，导致 DAS 基线错、吸光度失真。
    v_das_I0 = (p_das * float(cfg["throughput"]) * 1e-3 * float(cfg["resp"]) * float(cfg["gain"]))
    p_das_opt = (p_das * float(cfg["throughput"]) * np.exp(-alpha_das * float(L_cm)))
    i_das = p_das_opt * 1e-3 * float(cfg["resp"])
    # 同源噪声模型（与 WMS 完全一致，公平对比）
    s_sh_d, s_th_d, s_rin_d = noise_currents(float(np.mean(i_das)), cfg["bw"],
                                             cfg["gain"], cfg["rin"], T)
    s_d = float(np.sqrt(s_sh_d ** 2 + s_th_d ** 2 + s_rin_d ** 2))
    v_das_all = i_das * float(cfg["gain"]) + rng.normal(0.0, s_d * float(cfg["gain"]), i_das.shape)
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
    if not (2.0 <= m_act <= 2.6):
        warnings.append(f"调制系数 m={m_act:.2f} 偏离最优 2.2（2f 峰值最大处）→ "
                        f"建议 mod_amp_V≈{2.2 * hwhm / abs(cfg['eta_VI'] * cfg['dnu_dI']):.4g} V")
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

    meta = {"species": str(species).upper(), "wn_center": float(wn_center), "T": float(T),
            "P": float(P), "x": float(x), "L_cm": float(L_cm), "fs": fs,
            "fscan_Hz": fscan, "mod_freq_Hz": fm, "mod_amp_V": mod_amp_V, "auto_mod": auto_mod,
            "mod_coeff_m": m_act, "mod_depth_cm-1": a_cm1, "hwhm_cm-1": hwhm,
            "alpha_L_peak": aL_peak, "v_pd_mean": float(np.mean(v_pd)), "lsb_V": lsb,
            "n_sat": n_sat, "sigma_shot": s_shot * cfg["gain"],
            "sigma_thermal": s_therm * cfg["gain"], "sigma_rin": s_rin * cfg["gain"],
            "drift_frac": drift_frac, "flicker_frac": flicker_frac,
            "norm_method": norm_method, "onef_valid": onef_valid, "I0_pd_V": I0_pd,
            "offband_leak_1f": off_1f, "peak_2f_1f": pk_1f,
            "onef_min_over_median": onef_min / max(onef_med, 1e-30),
            "trim_frac": float(cfg["trim_frac"]), "n_trim": n_keep, "edge": edge,
            "cfg": dict(cfg), "warnings": warnings,
            "n_lines_in_window": info["n_lines_in_window"], "table": info["table"]}
    return {"t": t, "v_drive": v_drive, "v_scan": v_scan, "v_mod": v_mod_sig * mod_amp_V,
            "nu_laser": nu_laser, "p_laser": p_laser, "p_opt": p_opt, "v_pd": v_pd,
            "v_adc": v_adc, "S1f": S1f, "S2f": S2f, "S2f1f": S2f1f,
            "nu_axis": nu_cyc, "S1f_cyc": S1f_cyc, "S2f_cyc": S2f_cyc,
            "S2f1f_cyc": S2f_1f, "S2f_norm_cyc": S2f_norm,
            "v_adc_cyc": v_cyc, "v_drive_cyc": v_drive[idx],
            "alphaL_cyc": alphaL_cyc, "offband_mask": off, "valid_mask": valid,
            "das_cyc": das_cyc, "v_das_cyc": v_das_cyc,
            "t_cyc": t[idx], "n_per": n_sel, "edge": edge, "meta": meta}


def validate_wms_result(r):
    """对 WMS 结果做**自动二次审核**，返回结构化校验报告。

    供 MCP 在每次出结果前调用，把每项 pass/warn/fail 附在返回里，
    让非专业用户也能一眼看出"哪些可信、哪些要复核"。
    """
    m = r["meta"]
    nu, v, off = r["nu_axis"], r["valid_mask"], r["offband_mask"]
    das = r["das_cyc"][v]
    th = r["alphaL_cyc"][v]
    S2 = np.abs(r["S2f_cyc"])[v]
    checks = []

    def add(name, status, detail):
        checks.append({"check": name, "status": status, "detail": detail})

    # 1. 波数轴对齐：扫描窗口是否覆盖目标线
    wc = m["wn_center"]
    if nu.min() <= wc <= nu.max():
        add("波数轴对齐", "pass", f"扫描窗口 [{nu.min():.3f}, {nu.max():.3f}] 覆盖中心 {wc:.3f}")
    else:
        add("波数轴对齐", "fail", f"扫描窗口未覆盖 wn_center={wc}（wn_ref 错位？）")

    # 2. DAS 与理论 αL 一致性
    pk_d, pk_t = float(das.max()), float(th.max())
    if pk_t > 0:
        dev = abs(pk_d - pk_t) / pk_t
        if dev < 0.02:
            add("DAS-理论一致", "pass", f"DAS 峰值 {pk_d:.4g} vs 理论 {pk_t:.4g}，偏差 {dev:.1%}")
        elif dev < 0.10:
            add("DAS-理论一致", "warn", f"DAS 峰值 {pk_d:.4g} vs 理论 {pk_t:.4g}，偏差 {dev:.1%}（>2%）")
        else:
            add("DAS-理论一致", "fail", f"DAS 峰值 {pk_d:.4g} vs 理论 {pk_t:.4g}，偏差 {dev:.1%}（基线/错位可疑）")
    else:
        add("DAS-理论一致", "warn", "理论 αL 峰值为 0，无法校验")

    # 3. 弱吸收区 αL
    aL = m["alpha_L_peak"]
    if 1e-5 <= aL <= 0.1:
        add("弱吸收区", "pass", f"αL={aL:.3g} 在弱吸收线性区 [1e-5, 0.1]")
    elif aL > 0.1:
        add("弱吸收区", "warn", f"αL={aL:.3g} > 0.1：2f 进入非线性，建议降 x 或 L_cm")
    else:
        add("弱吸收区", "warn", f"αL={aL:.3g} < 1e-5：吸收太弱，信噪比可能不足")

    # 4. 孤立线（标准谐波前提）
    nl = m["n_lines_in_window"]
    if nl <= 10:
        add("孤立单线", "pass", f"窗口内 {nl} 条线，2f 应呈标准双峰")
    else:
        add("孤立单线", "warn", f"窗口内 {nl} 条线：2f 是多线叠加，非标准双峰（DAS 包络更直观）")

    # 5. 调制系数 m
    mm = m["mod_coeff_m"]
    if 2.0 <= mm <= 2.6:
        add("调制系数 m", "pass", f"m={mm:.2f} 接近最优 2.2")
    else:
        add("调制系数 m", "warn", f"m={mm:.2f} 偏离 2.2（2f 灵敏度非最优）")

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
    add("归一化方法", "pass", f"采用 {m['norm_method']}（1f 有效={m['onef_valid']}）")

    # 9. 噪声告知（1/f 默认关，须向用户说明）
    if m["drift_frac"] == 0 and m["flicker_frac"] == 0 and m["cfg"]["rin"] == 0:
        add("噪声", "pass", "未加噪声（理想仿真），已按约定告知")
    else:
        add("噪声", "pass", f"已加噪声：rin={m['cfg']['rin']}, drift={m['drift_frac']}, flicker={m['flicker_frac']}")

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


def plot_wms_instrument(r, out_png):
    """TDLAS 仪器链路默认图（3×2 六子图，统一格式）：
    ① 波长调制 → ② PD 原始信号(DAS 无调制) → DAS αL+理论 → ③ 1f → ④ 2f → ⑤ 归一化 2f。"""
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    use_cjk_font(matplotlib)

    m = r["meta"]
    nu, off, ok = r["nu_axis"], r["offband_mask"], m["onef_valid"]
    vd = r["valid_mask"]

    def seg(a_, y, color, label, lw=1.2):
        """有效区实线 + 剔除区灰点线；y 轴范围只看有效区，避免剔除区伪影压扁曲线。

        注意：曲线本身可能有上万点，而画布只有几百像素，逐点连线会互相穿插成
        "乱麻"（绘图混叠）。这里做等间隔抽取，保证每像素至多 ~1 个点。
        """
        stride = max(1, int(round(nu.size / 1200)))
        sl = slice(None, None, stride)
        a_.plot(nu[vd][sl], y[vd][sl], "-", color=color, lw=lw, label=label)
        if (~vd).any():
            a_.plot(nu[~vd][sl], y[~vd][sl], ":", color="0.6", lw=1.0)
        yv = y[vd]
        if yv.size:
            lo, hi = min(float(np.min(yv)), 0.0), max(float(np.max(yv)), 0.0)
            pad = 0.08 * (hi - lo) or 1e-12
            a_.set_ylim(lo - pad, hi + pad)

    # 3×2 布局，每子图约 2:1（宽:高）——谱图横轴是波数、横长竖短更利于读线形，
    # 且贴近黄金矩形，兼顾阅读与美观。
    fig, ax = plt.subplots(3, 2, figsize=(14, 10))
    ax = ax.ravel()

    # ① 驱动电压：三角波 + 正弦调制
    ax[0].plot(r["t_cyc"] * 1e3, r["v_drive_cyc"], lw=0.6, color="0.2")
    ax[0].set_ylabel("驱动电压 (V)")
    ax[0].set_xlabel("时间 (ms)")
    ax[0].set_title(f"① 波长调制 V(t)   fm={m['mod_freq_Hz'] / 1e3:g} kHz, "
                    f"fscan={m['fscan_Hz']:g} Hz")

    # ②a PD 原始信号（无调制 DAS 链路，直接吸收）
    seg(ax[1], r["v_das_cyc"], "C0", "PD 原始信号")
    ax[1].set_ylabel("PD 信号 (V)")
    ax[1].set_xlabel(r"波数 (cm$^{-1}$)")
    ax[1].set_title("② 直接吸收 DAS（无调制）")

    # ②b DAS αL + HITRAN 理论对照
    seg(ax[2], r["das_cyc"], "C0", "DAS αL（-ln 提取）")
    seg(ax[2], r["alphaL_cyc"], "C2", "HITRAN 理论 αL", lw=1.0)
    ax[2].axhline(0.0, color="k", lw=0.5, alpha=0.4)
    ax[2].set_ylabel(r"$\alpha L$")
    ax[2].set_xlabel(r"波数 (cm$^{-1}$)")
    ax[2].set_title("DAS 吸光度 vs 数据库理论值")

    # ③ 1f（单独，幅值独立）
    seg(ax[3], r["S1f_cyc"], "C1", "1f")
    ax[3].set_ylabel("1f (V)")
    ax[3].set_xlabel(r"波数 (cm$^{-1}$)")
    ax[3].set_title("③ 一阶谐波 1f")

    # ④ 2f（单独，幅值独立）
    seg(ax[4], r["S2f_cyc"], "C3", "2f")
    ax[4].axhline(0.0, color="k", lw=0.5, alpha=0.4)
    ax[4].set_ylabel("2f (V)")
    ax[4].set_xlabel(r"波数 (cm$^{-1}$)")
    ax[4].set_title("④ 二阶谐波 2f")

    # ⑤ 归一化 2f（标注方法）
    if ok:
        seg(ax[5], r["S2f_norm_cyc"], "C2", "2f/1f", 1.6)
    else:
        seg(ax[5], r["S2f_norm_cyc"], "C2",
            f"2f/I0（I0={m['I0_pd_V']:.3g} V）", 1.6)
    ax[5].axhline(0.0, color="k", lw=0.5, alpha=0.4)
    ax[5].set_ylabel("归一化 2f (a.u.)")
    ax[5].set_xlabel(r"波数 (cm$^{-1}$)")
    ax[5].set_title(f"⑤ 归一化：{m['norm_method']}")

    # 剔除区（三角波转折点）标红
    nd = int(m.get("n_trim", 0))
    if 0 < nd < r["n_per"]:
        for s_ in (nu[:nd], nu[-nd:]):
            if s_.size:
                for a_ in ax[1:]:
                    a_.axvspan(min(s_.min(), s_.max()), max(s_.min(), s_.max()),
                               color="red", alpha=0.06)

    for a_ in ax:
        a_.grid(alpha=0.3)
        a_.legend(loc="upper right", fontsize=7)
    fig.suptitle(f"TDLAS 仪器链路 — {m['species']} @ {m['wn_center']:.3f} cm$^{{-1}}$   "
                 f"x={m['x']:g}, L={m['L_cm']:g} cm, m={m['mod_coeff_m']:.2f}   "
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
    d = simulate_das_td(species="CH4", wn0=2968.5, span=1.5, x=1e-4)
    png2 = plot_das_td(d, out_dir / "das_chain.png")
    print(f"  DAS 链路图已保存：{png2}")


if __name__ == "__main__":
    main()
