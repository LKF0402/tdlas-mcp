#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""TDLAS/WMS 仿真核心（最小可跑原型）。

链路：
    HITRAN α(ν) ──► 扫描 + 高频调制的瞬时波长 ν(t) ──► τ(t) = exp(-αL)
                ──► 免标定 WMS 解析（傅里叶系数）──► 1f/2f 谐波
                ──► 2f/1f 归一化（弱吸收下正比于浓度）

设计纪律：
  · 吸收谱取数 / 线型 / 配分和 **全部复用 hitran-mcp**（同级仓库），不重复造轮子；
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

# ── 复用 hitran-mcp（同级仓库，只取数不算谱）──
# 其仓库根必须在 sys.path 最前，否则 "tools" 会解析到 tdlas-mcp 自己的 tools 包。
_HITRAN_MCP_ROOT = Path(__file__).resolve().parent.parent / "hitran-mcp"
if not _HITRAN_MCP_ROOT.exists():
    raise RuntimeError(f"未找到 hitran-mcp 仓库：{_HITRAN_MCP_ROOT}"
                       "（tdlas-mcp 依赖它提供 HITRAN 取数与吸收谱计算）")
if str(_HITRAN_MCP_ROOT) not in sys.path:
    sys.path.insert(0, str(_HITRAN_MCP_ROOT))
from tools import hitran_mcp as hm  # noqa: E402


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


# ══════════════════ 2. 吸收谱（复用 hitran-mcp）══════════════════

def get_alpha(species, wn_lo, wn_hi, T, P, x, step=5e-4, wingHW=20.0):
    """取吸收系数 α(ν) [cm^-1]。窗口未覆盖 / 空谱由 hitran-mcp 报错，不静默。"""
    res = hm._compute([{"name": species, "mole_frac": x}], wn_lo, wn_hi,
                      T=T, P=P, step=step, wingHW=wingHW, mode="alpha")
    tb = res.get("tables") or {}
    first = next(iter(tb.values()), {}) if tb else {}
    return (np.asarray(res["nu"], dtype=float),
            np.asarray(res["total"], dtype=float),
            {"n_lines_in_window": first.get("n_lines_in_window"),
             "table": first.get("table"), "warnings": res.get("warnings") or []})


# ══════════════════ 3. 扫描式 WMS 正向仿真 ══════════════════

def simulate(species="H2O", wn0=7185.596, T=296.0, P=1.0, x=0.1, L=30.0,
             a=0.10, i0=0.1671, i2=2.48e-3,
             psi1=1.9356 * np.pi, psi2=4.4138 * np.pi,
             span=0.8, n_scan=401, n_mod=96, step=5e-4):
    """扫描式 WMS 正向仿真。

    species/wn0   : 分子与目标线中心（cm^-1）
    T / P / x / L : 温度 K / 压力 atm / 摩尔分数 / 光程 cm
    a             : 调制深度（cm^-1）
    i0,i2,psi1,psi2: 激光强度调制参数（AM 幅度与 IM-FM 相位差）
    span/n_scan/n_mod: 扫描半宽 cm^-1 / 扫描点数 / 每点调制周期采样数

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

    S1f = np.zeros_like(wn_scan); S2f = np.zeros_like(wn_scan)
    S4f = np.zeros_like(wn_scan); S2f1f = np.zeros_like(wn_scan)
    for j, wn_j in enumerate(wn_scan):
        tau = np.exp(-np.interp(wn_j + a * np.cos(phase), nu, alpha) * L)
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

    print("  自测通过")
    return r


# ══════════════════ 5. 绘图 ══════════════════

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
    print(f"  图已保存：{png}")


if __name__ == "__main__":
    main()
