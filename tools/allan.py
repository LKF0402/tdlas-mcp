#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Allan 方差分析（重叠版 Overlapping Allan Deviation, OA-VAR）。

物理背景
========
浓度/电压时序 s(t) 中混有三种典型噪声：
  · 白噪声（RIN/散粒/热噪声）：σ(τ) ∝ τ^{-1/2}
  · 1/f 闪烁噪声（低频漂移）：σ(τ) ∝ τ^0（平台）
  · 线性漂移（系统温漂/激光老化）：σ(τ) ∝ τ^{+1}

Allan 偏差 σ(τ) 在双对数图上把三者分开：
  - 短 τ 斜率 -1/2  → 白噪声主导
  - 中 τ 斜率 0     → 1/f 闪烁主导（最优平均时间在这一带）
  - 长 τ 斜率 +1    → 线性漂移主导（再平均也没用了）

最优平均时间 τ_opt 就是 σ(τ) 最小值对应的 τ。

算法（重叠 Allan 方差 OA-VAR）
=============================
σ²(τ) = 1 / [2·(N-2m)] · Σ_{j=0}^{N-2m-1} [X_{j+m} - X_j]²

其中：
  m     = τ/Δt（平均窗口点数）
  X_j   = mean(s[j : j+m])（长度 m 的滑动窗口均值，步长 1）
  τ     = m · Δt（平均时间）

文献依据
========
- Werle et al. (1993) 首次将 Allan 方差引入激光光谱学（Allan-Werle 图）
- 重叠版（OA-VAR）与 Werle 原式（非重叠）**期望值相同**，
  但重叠版数据利用率高（N-2m 个样本 vs 仅 N/m 个），长 τ 端置信度更窄
- 时频计量界已将重叠版作为默认（Stable32: "1st choice"）
- 论文写作：注明 "overlapping Allan deviation (OA-VAR)"，引 Werle 1993/2011
"""

from __future__ import annotations
import numpy as np
from typing import Optional, Dict


def overlapping_allan(signal: np.ndarray, fs: float,
                      max_tau: Optional[float] = None,
                      tau_points: int = 100) -> Dict:
    """重叠 Allan 偏差（OA-VAR）。

    参数
    ────
    signal    : 时序浓度/电压（一维）
    fs        : 采样率 (Hz)
    max_tau   : 最长平均时间 (s)，None = T/2（总时长一半）
    tau_points: 对数轴上 τ 的采样点数

    返回
    ────
    {
      "taus":    np.ndarray  # 平均时间轴 (s)
      "adev":    np.ndarray  # Allan 偏差 σ(τ)
      "avar":    np.ndarray  # Allan 方差 σ²(τ)
      "tau_opt": float       # 最优平均时间 (s)
      "adev_opt": float     # 最优平均时间下的 σ
      "n_tau":   int
    }
    """
    s = np.asarray(signal, dtype=float).ravel()
    N = len(s)
    if N < 16:
        raise ValueError(f"时序过短（{N} 点），至少需要 16 点")
    if fs <= 0:
        raise ValueError(f"采样率必须 > 0，收到 {fs}")

    dt = 1.0 / fs
    T = N * dt
    if max_tau is None:
        max_tau = T / 2.0

    max_m = int(max_tau / dt)
    max_m = min(max_m, N // 2 - 1)
    if max_m < 2:
        raise ValueError("max_tau 太小，无法计算")

    # 对数均匀取 m（平均窗口点数）
    m_values = np.unique(np.logspace(
        np.log10(1), np.log10(max_m), tau_points).astype(int))

    taus, avar, adev = [], [], []
    for m in m_values:
        # 长度 m 的滑动平均（步长 1，重叠）
        cumsum = np.cumsum(np.insert(s, 0, 0.0))
        X = (cumsum[m:] - cumsum[:-m]) / m
        # 相邻窗口差分：X_{j+m} - X_j
        diff = X[m:] - X[:-m]
        if len(diff) == 0:
            continue
        # σ² = mean(diff²) / 2
        av = np.mean(diff ** 2) / 2.0
        taus.append(m * dt)
        avar.append(av)
        adev.append(np.sqrt(av))

    taus = np.array(taus)
    avar = np.array(avar)
    adev = np.array(adev)

    idx_min = int(np.argmin(adev))
    tau_opt = float(taus[idx_min])
    adev_opt = float(adev[idx_min])

    return {
        "taus": taus, "adev": adev, "avar": avar,
        "tau_opt": tau_opt, "adev_opt": adev_opt,
        "n_tau": len(taus),
        "fs": fs, "dt": dt, "N": N,
    }


def allan_noise_diag(result: Dict) -> Dict:
    """根据 σ(τ) 斜率诊断噪声类型。"""
    taus = result["taus"]
    adev = result["adev"]
    log_t = np.log10(taus)
    log_a = np.log10(adev)
    slopes = np.gradient(log_a, log_t)

    diag = {"white_noise_region": None, "flicker_region": None, "drift_region": None}
    white_mask = slopes < -0.3
    if np.any(white_mask):
        idx = np.where(white_mask)[0]
        diag["white_noise_region"] = (float(taus[idx[0]]), float(taus[idx[-1]]))
    drift_mask = slopes > 0.5
    if np.any(drift_mask):
        idx = np.where(drift_mask)[0]
        diag["drift_region"] = (float(taus[idx[0]]), float(taus[idx[-1]]))
    flicker_mask = (slopes >= -0.3) & (slopes <= 0.5)
    if np.any(flicker_mask):
        idx = np.where(flicker_mask)[0]
        diag["flicker_region"] = (float(taus[idx[0]]), float(taus[idx[-1]]))
    return diag
