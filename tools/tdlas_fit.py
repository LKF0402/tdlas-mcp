#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""谱线级浓度反演（最小二乘）+ 不确定度 —— 对单点除法的严格替代。

为什么需要
──────────
此前 `tdlas_invert` 只用**一个数**（归一化 2f 峰的峰值）除以灵敏度 k：
    x = peak / k
这在理想无噪时精确，但

  · 只用了一个采样点 → **丢掉了整条谱线的信息**，抗噪能力差；
  · 噪声、基线残差、线形失配全部被压缩进那个单点里，无法诊断；
  · 无法给出拟合优度（χ²），因而无法判断"这条谱是否真的符合模型"。

本模块用**整条归一化 2f 谱**做最小二乘：弱吸收下 2f 幅度与浓度成正比，
故模型是线性的 `S(ν) = x · k₁(ν)`（k₁ 为单位浓度的谱形）。于是

    x̂ = Σ(kᵢ·sᵢ) / Σ(kᵢ²)          （普通最小二乘的解析解）
    σ(x̂) = σ_resid / √(Σ kᵢ²)
    χ²_red = Σ(sᵢ - x̂·kᵢ)² / (N - 1) / σ_resid²

好处：① 用上全部采样点，σ 通常比单点法小；② 给出 χ²_red 与残差结构，
可判断**模型是否适用**（χ²_red ≫ 1 说明有系统失配，而非单纯噪声）。

⚠ 适用边界（与项目既有一致）：仅弱吸收（αL ≪ 1）、且要求**同工况**的 k₁
（换 T/P/L/调制深度必须重算 k₁）。
"""

from __future__ import annotations

import math
from typing import Optional


def fit_concentration_ls(measured, k_per_unit_x, valid=None, sigma=None):
    """最小二乘拟合浓度：`measured ≈ x · k_per_unit_x`。

    参数
    ────
    measured       : 实测（或仿真）的归一化 2f 谱，长度 N
    k_per_unit_x   : **单位浓度**下的 2f 谱形 k₁(ν)（同工况！），长度 N
    valid          : 可选布尔掩膜（如剔除区），None = 全部有效
    sigma          : 可选逐点噪声 σ；None 时用残差估计

    返回 dict
    ─────────
    x, x_sigma, chi2_red, n_points, r2, residual_rms,
    x_peak_only（同一数据用"单点除法"的结果，供对照）, note
    """
    import numpy as np

    s = np.asarray(measured, dtype=float).ravel()
    k = np.asarray(k_per_unit_x, dtype=float).ravel()
    if s.shape != k.shape:
        raise ValueError(f"谱与模型长度不一致：measured={s.shape} vs k={k.shape}")
    if valid is not None:
        m = np.asarray(valid, dtype=bool).ravel()
        if m.shape != s.shape:
            raise ValueError("valid 掩膜长度与谱不一致")
        s, k = s[m], k[m]
    n = int(s.size)
    if n < 3:
        raise ValueError(f"有效点数过少（{n}），无法做最小二乘（至少 3）")
    if not np.all(np.isfinite(s)) or not np.all(np.isfinite(k)):
        raise ValueError("谱或模型含非有限值（NaN/Inf）")
    if float(np.dot(k, k)) <= 0.0:
        raise ValueError("模型谱 k₁(ν) 恒为 0：无法定标（请检查工况与窗口是否覆盖吸收线）")

    # 普通最小二乘解析解（模型过原点，与"无吸收时 2f≈0"的物理一致）
    kk = float(np.dot(k, k))
    x = float(np.dot(k, s) / kk)
    resid = s - x * k
    dof = n - 1
    # 逐点 σ：优先用调用方给的，否则由残差估计
    if sigma is not None:
        sg = float(np.asarray(sigma, dtype=float).ravel().mean())
        if not (sg > 0):
            raise ValueError(f"sigma 必须 > 0，收到 {sigma}")
    else:
        sg = float(np.sqrt(float(np.dot(resid, resid)) / dof)) if dof > 0 else float("nan")

    x_sigma = float(sg / math.sqrt(kk)) if sg == sg and kk > 0 else float("nan")
    chi2_red = (float(np.dot(resid, resid)) / dof / (sg * sg)) if (sg > 0 and dof > 0) else float("nan")
    ssm = float(np.dot(s - s.mean(), s - s.mean()))
    r2 = float(1.0 - float(np.dot(resid, resid)) / ssm) if ssm > 0 else float("nan")

    # 对照：单点除法（取峰值点）
    ipk = int(np.argmax(np.abs(s)))
    x_peak = float(s[ipk] / k[ipk]) if k[ipk] != 0 else float("nan")

    note = ("最小二乘用整条谱线，σ 一般小于单点法；χ²_red ≫ 1 说明存在**系统失配**"
            "（线形/基线/工况不同源），此时应先查前提而不是采信 σ。")
    return {"x": x, "x_sigma": x_sigma, "chi2_red": chi2_red, "n_points": n,
            "r2": r2, "residual_rms": float(np.sqrt(float(np.dot(resid, resid)) / n)),
            "x_peak_only": x_peak, "sigma_used": sg, "note": note}
