#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""HITRAN 线表取数与吸收谱计算 —— HAPI 1.x 精简封装（自包含，不依赖外部仓库）。

只用 HAPI 1.x（`hapi.py`，pip 包名 `hitran-api`）的官方接口：
    hapi.db_begin / hapi.fetch / hapi.absorptionCoefficient_Voigt / hapi.ISO / hapi.moleculeName
**不需要 API key** —— HAPI 1.x 走旧下载接口、不校验 key。
有意**不使用 HAPI2**（其官方 v2 API 需 key，配置麻烦），故本模块完全免 key。

设计纪律（沿用 hitran-mcp 的审计结论）：
  · 窗口安全：线表必须真正覆盖请求窗口，否则按窗口重抓 —— 避免"旧表静默缺线"
    被误读成"该气体无吸收"；
  · 空谱硬报错：窗口内 0 条线 / 抓取失败直接 raise，绝不静默返回平谱；
  · 单位：波数 cm^-1、温度 K、压力 atm、线翼半宽 cm^-1。
"""
from __future__ import annotations

from pathlib import Path

import numpy as np

_DATA_DIR = Path(__file__).resolve().parent.parent / "Hitran_Data"
_DB_READY = False
_SPECIES = None


def _hapi():
    import hapi
    return hapi


def _ensure_db():
    """初始化 HAPI 线表缓存目录（进程内一次）。"""
    global _DB_READY
    if not _DB_READY:
        _DATA_DIR.mkdir(parents=True, exist_ok=True)
        _hapi().db_begin(str(_DATA_DIR))
        _DB_READY = True


def _species_index():
    """官方物种索引：分子式 → {M, main_iso, isotopologues}（来自 hapi.ISO，不硬编码）。"""
    global _SPECIES
    if _SPECIES is None:
        idx = {}
        for (M, I), rec in _hapi().ISO.items():
            formula = str(rec[4]).upper()
            e = idx.setdefault(formula, {"M": M, "isotopologues": []})
            e["isotopologues"].append({"I": I, "abundance": float(rec[2]), "name": rec[1]})
        for e in idx.values():
            e["isotopologues"].sort(key=lambda d: -d["abundance"])
            e["main_iso"] = e["isotopologues"][0]["I"]
        _SPECIES = idx
    return _SPECIES


def species(with_isotopologues=False):
    """物种速查：分子式 → M / 主同位素（可选含全部同位素与丰度）。"""
    idx = _species_index()
    if with_isotopologues:
        return idx
    return {f: {"M": e["M"], "main_iso": e["main_iso"]} for f, e in idx.items()}


def resolve(name):
    """分子式 / HITRAN 分子号 → (formula, M)。"""
    idx = _species_index()
    s = str(name).strip().upper()
    if s.isdigit():
        M = int(s)
        return str(_hapi().moleculeName(M)).upper(), M
    if s in idx:
        return s, idx[s]["M"]
    for f in idx:                                   # 容错：忽略括号差异
        if f.replace("(", "").replace(")", "") == s.replace("(", "").replace(")", ""):
            return f, idx[f]["M"]
    raise ValueError(f"[tdlas] 未知物种 '{name}'（HITRAN 官方表无此分子）")


def _wnum(v):
    """窗口数字 → 表名片段：2172.5 -> 2172p5。"""
    return f"{float(v):g}".replace(".", "p").replace("-", "m")


def _coverage(table):
    """已加载表的覆盖区间 (nu_min, nu_max, n_lines)；未加载返回 None。"""
    h = _hapi()
    try:
        if table not in h.tableList():
            return None
        nu = np.asarray(h.getColumn(table, "nu"), dtype=float)
        return (float(nu.min()), float(nu.max()), int(nu.size)) if nu.size else None
    except Exception:
        return None


def fetch_table(formula, M, I, numin, numax, force=False):
    """保证拿到真正覆盖 [numin, numax] 的线表；返回 (table, coverage)。

    坑：hapi.fetch 见同名表已存在就跳过下载，于是请求新窗口时仍用旧线表
    → 谱线静默缺失。策略：先查覆盖，未覆盖则用带窗口后缀的表名重抓。
    """
    _ensure_db()
    numin, numax = float(numin), float(numax)
    base = f"{formula}_{M}_{I}"
    wtable = f"{base}_{_wnum(numin)}_{_wnum(numax)}"
    if not force:
        cov = _coverage(wtable)                  # 同窗口缓存优先零下载复用
        if cov is not None:
            return wtable, cov
        cov = _coverage(base)
        if cov and cov[0] <= numin and cov[1] >= numax:
            return base, cov
    try:
        _hapi().fetch(wtable, M, I, numin, numax)
    except Exception as e:
        raise RuntimeError(
            f"[tdlas] 抓取 {formula}(M={M}, I={I}) 于 {numin}-{numax} cm-1 失败：{e}。"
            f"常见原因：该窗口无 HITRAN 收录线 / 分子号或同位素不存在 / 无网络 / 官方每日配额超限"
        ) from e
    cov = _coverage(wtable)
    if cov is None:
        raise RuntimeError(f"[tdlas] 抓取失败：{formula} 在 {numin}-{numax} cm-1 无线表")
    if cov[2] == 0:
        raise RuntimeError(
            f"[tdlas] 表 {wtable} 在 {numin}-{numax} cm-1 返回 0 条谱线。"
            f"该分子/同位素在此窗口无 HITRAN 收录，严禁当作'无吸收 / 平谱'使用")
    return wtable, cov


def absorption(name, numin, numax, T=296.0, P=1.01325, step=5e-4, wingHW=20.0,
               iso=None, hitran_units=False):
    """纯组分吸收系数 α(ν)。

    返回 (nu, alpha, info)：
      · hitran_units=False → α [cm^-1]（该组分纯气、总压 P、空气浴）
      · hitran_units=True  → σ [cm^2/molecule]
    混合气请自行按 α_i = x_i · α_pure_i 叠加（本函数不代劳，避免口径混淆）。
    """
    _ensure_db()
    h = _hapi()
    formula, M = resolve(name)
    I = _species_index()[formula]["main_iso"] if iso is None else int(iso)
    table, cov = fetch_table(formula, M, I, numin, numax)
    nu, coef = h.absorptionCoefficient_Voigt(
        Components=[(M, I, 1.0)],
        SourceTables=[table],
        WavenumberRange=(float(numin), float(numax)),
        WavenumberStep=float(step),
        WavenumberWingHW=float(wingHW),
        HITRAN_units=bool(hitran_units),
        Environment={"T": float(T), "p": float(P), "Diluent": {"air": 1.0}})
    nu_all = np.asarray(h.getColumn(table, "nu"), dtype=float)
    n_in = int(((nu_all >= numin) & (nu_all <= numax)).sum())
    info = {"molecule": formula, "M": M, "iso": I, "table": table,
            "coverage_cm-1": [round(cov[0], 3), round(cov[1], 3)],
            "n_lines_table": cov[2], "n_lines_in_window": n_in,
            # α(ν) 的计算语境：α 峰值随 T/P/网格步长/翼截断/窗口 变化，
            # 报告 α 峰值时须能同时给出这五项，否则数值不可复现、不可比较。
            "alpha_context": {"T_K": float(T), "P_atm": float(P),
                              "window_cm-1": [float(numin), float(numax)],
                              "step_cm-1": float(step), "wingHW_cm-1": float(wingHW),
                              "diluent": "air"}}
    return np.asarray(nu, dtype=float), np.asarray(coef, dtype=float), info
