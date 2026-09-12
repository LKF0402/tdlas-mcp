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


def t_selftest():
    """全链路自检（有线 / 2f 形状 / 弱场线性 / 反演闭环 / 检测限 / 时域交叉验证 / DAS 链路）。"""
    with _quiet() as g:
        ts.selftest()
    return {"ok": True, "log": g.getvalue().splitlines()}


DISPATCH = {"tdlas_simulate": t_simulate,
            "tdlas_das_chain": t_das_chain,
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
