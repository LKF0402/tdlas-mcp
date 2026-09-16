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

import time
from pathlib import Path

import numpy as np

_DATA_DIR = Path(__file__).resolve().parent.parent / "Hitran_Data"
_DB_READY = False
_SPECIES = None


def _hapi():
    """导入 HAPI 1.x（pip 包名 `hitran-api`，import 名才是 `hapi`）。

    缺依赖时给出可操作的提示，而不是裸 ModuleNotFoundError ——
    常见误用是用别的解释器跑（如 windows 商店占位符 python），报错现场离根因很远。
    """
    try:
        import hapi
    except ImportError as exc:          # 依赖缺失：直接说清装什么、用哪个解释器
        import sys
        raise ImportError(
            "缺少依赖 hitran-api（提供 import hapi）→ 无法从 HITRAN 取线表。"
            f"请先安装：pip install -r requirements.txt（当前解释器：{sys.executable}）"
        ) from exc
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


_FETCH_TRIES = 3
_FETCH_BACKOFF = 1.0            # 秒；重试间隔 1s → 2s（指数退避）


def _is_transient(e):
    """判断抓取失败是否属"网络/服务端暂时故障"（可重试）而非"该窗口无数据"（重试无用）。

    HITRAN 旧接口偶发 502/超时很常见（首次运行与 CI 必踩），此前一次失败就抛错并
    把四种可能原因并列，用户会误读成"该窗口无 HITRAN 收录"。
    """
    import urllib.error
    if isinstance(e, (urllib.error.URLError, TimeoutError, ConnectionError, OSError)):
        return True
    code = getattr(e, "code", None)
    if isinstance(code, int) and code >= 500:
        return True
    msg = str(e).lower()
    # HAPI 把 HTTP 故障统一包成 "Failed to retrieve data for given parameters."，
    # 单看这句话无法区分"服务端 502"与"该窗口无数据"，故另配 _isotope_known() 判别。
    return any(w in msg for w in ("timed out", "timeout", "connection", "temporarily",
                                  "bad gateway", "502", "503", "504", "reset",
                                  "failed to retrieve"))


def _isotope_known(M, I):
    """该分子/同位素是否在 HITRAN 收录范围内（用 HAPI 自带索引，离线判断）。

    用途：把"下载失败"与"该同位素根本不存在"分开。HAPI 的 fetch 把两者都抛成同一句话，
    直接并列成"常见原因：无收录/无网络"会让一次 502 被读成"这个波段 HITRAN 没收"。
    返回 True/False，索引不可用时返回 None。
    """
    try:
        h = _hapi()
        # ⚠ HAPI 的 ISO 以 **(M, I) 元组** 为键（不是嵌套字典）：ISO[(6, 1)] = [id, 'CH4', 丰度, 质量, 'CH4']
        return (int(M), int(I)) in h.ISO
    except Exception:
        return None


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
    last = None
    for attempt in range(_FETCH_TRIES):
        try:
            _hapi().fetch(wtable, M, I, numin, numax)
            last = None
            break
        except Exception as e:
            last = e
            if not _is_transient(e):
                break
            if attempt < _FETCH_TRIES - 1:
                time.sleep(_FETCH_BACKOFF * (2 ** attempt))
    if last is not None:
        # ★ 归因必须分开：网络/服务端故障 ≠ "该窗口没有谱线"。
        #   此前把四种原因并列成一段话，一次 502 会被读成"HITRAN 没收录这条线"，
        #   用户据此换波段甚至得出"该气体无吸收"——这正是本模块开头纪律要避免的静默错误。
        known = _isotope_known(M, I)
        # 优先用**离线索引**下判断（比关键字更可靠）：同位素确实收录 ⇒ 失败必是服务端/网络问题。
        if known is False:
            raise RuntimeError(
                f"[tdlas] 抓取 {formula}(M={M}, I={I}) 于 {numin}-{numax} cm-1 失败：{last}。"
                f"该分子/同位素不在 HITRAN 收录范围（已用离线索引核对），请核对物种与同位素编号。"
            ) from last
        if known is True or _is_transient(last):
            raise RuntimeError(
                f"[tdlas] HITRAN 服务暂时不可用（{type(last).__name__}: {last}），"
                f"已自动重试 {_FETCH_TRIES} 次仍失败。"
                + (f"{formula}(M={M}, I={I}) 是 HITRAN 收录的同位素，"
                   f"失败属网络/服务端故障。" if known else "")
                + "**这不代表该窗口无谱线** —— 请稍后重试，不要据此换波段或判定该气体无吸收。"
            ) from last
        raise RuntimeError(
            f"[tdlas] 抓取 {formula}(M={M}, I={I}) 于 {numin}-{numax} cm-1 失败：{last}。"
            f"该分子号/同位素不在 HITRAN 收录范围（已用离线索引核对），请核对物种与同位素编号。"
        ) from last
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
