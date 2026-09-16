#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""契约自检（离线，可进 CI）：守住三类"静默错误"不回流。

为什么需要：本项目的两次真实事故都出在 MCP 接口层，而不是物理层 ——
  ① schema 声明了 edge/trim_frac/d2nu_dI2/am_*，MCP 层却从未把它们透传进引擎，
     AI 传 edge="falling" 得到 rising 的结果，全程零提示；
  ② session_id 没写进 wms/review 的 schema，跨会话记忆在按 schema 驱动的客户端上永不生效。
这类错误**不会报错**，只会悄悄给出错误结论，所以必须有自动化守门。

检查项（全部离线，不访问 HITRAN、不启动服务器）：
  1. schema ↔ 函数签名一致性（双向）
  2. 参数真的透传到引擎（用假引擎记录 kwargs，不跑仿真）
  3. 目标波数越界时自动重锚 wn_ref；显式给激光参数/设备引用时不介入
  4. SPECIES_PROFILES 的每个推荐波段在自动重锚后可跑通
  5. 未知键 / 类型错误被拒绝（不再静默丢弃）
  6. JSON-RPC 批量请求、未知工具、ping
  7. 文件名净化、默认 seed 可复现、会话原子写

用法：python tools/contract_check.py   （退出码 0 = 全部通过）
"""
from __future__ import annotations

import inspect
import json
import sys
import tempfile
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))
if str(_ROOT / "tools") not in sys.path:
    sys.path.insert(0, str(_ROOT / "tools"))

import tdlas_sim as ts                      # noqa: E402
import tdlas_mcp as M                       # noqa: E402

_OK = 0
_BAD = 0


def check(name, cond, extra=""):
    global _OK, _BAD
    if cond:
        _OK += 1
        print(f"  PASS  {name} {extra}")
    else:
        _BAD += 1
        print(f"  FAIL  {name} {extra}")


class _Stop(Exception):
    """哨兵：假引擎记录完 kwargs 后抛出，避免真的跑仿真（保持离线）。"""


def _fake_engine(store):
    def f(*a, **k):
        store["args"] = a
        store.update(k)
        raise _Stop()
    return f


# ───────────────────── 1. schema ↔ 签名一致性 ─────────────────────
print("=== 1. schema ↔ 函数签名一致性 ===")
for t in M.TOOLS:
    name = t["name"]
    props = set((t["inputSchema"].get("properties") or {}).keys())
    fn = M.DISPATCH.get(name)
    if fn is None:
        check(f"{name} 在 DISPATCH 中", False)
        continue
    sig = inspect.signature(fn)
    named = {p for p, v in sig.parameters.items()
             if v.kind in (v.POSITIONAL_OR_KEYWORD, v.KEYWORD_ONLY) and p != "self"}
    has_kw = any(v.kind == v.VAR_KEYWORD for v in sig.parameters.values())
    # schema 里的键必须能被接住（命名参数 或 **kw）
    unreachable = sorted(props - named) if not has_kw else []
    # 命名参数必须出现在 schema 里，否则客户端根本传不进来
    undeclared = sorted(named - props)
    check(f"{name}: 每个 schema 键都可被接收", not unreachable, f"-> {unreachable}")
    check(f"{name}: 每个命名参数都在 schema 中声明", not undeclared, f"-> {undeclared}")
print(f"  工具数 = {len(M.TOOLS)} / DISPATCH 数 = {len(M.DISPATCH)}")
check("TOOLS 与 DISPATCH 双向一致",
      {t["name"] for t in M.TOOLS} == set(M.DISPATCH))

# ───────────────────── 2. 参数真的透传到引擎 ─────────────────────
print("=== 2. WMS 参数透传（含曾静默丢弃的一族）===")
_want_wms = ["edge", "trim_frac", "d2nu_dI2", "am_i0", "am_i2", "am_psi1", "am_psi2",
             "mod_phase_deg", "physical_noise", "drift_frac", "flicker_frac"]
store = {}
_orig = ts.simulate_wms_instrument
ts.simulate_wms_instrument = _fake_engine(store)
try:
    try:
        M.t_wms_instrument("CH4", wn_center=2968.5, mod_amp_V=0.1,
                           **{k: v for k, v in (("edge", "falling"), ("trim_frac", 0.3),
                                                ("d2nu_dI2", -1e-3), ("am_i0", 0.05),
                                                ("am_i2", 0.01), ("am_psi1", 0.3),
                                                ("am_psi2", 0.4), ("mod_phase_deg", 15.0),
                                                ("physical_noise", False),
                                                ("drift_frac", 0.01), ("flicker_frac", 0.02))})
    except _Stop:
        pass
finally:
    ts.simulate_wms_instrument = _orig
for k in _want_wms:
    check(f"WMS 透传 {k}", k in store, f"= {store.get(k)!r}")
print(f"  透传样本: " + ", ".join(f"{k}={store.get(k)!r}" for k in
                                ("edge", "trim_frac", "d2nu_dI2", "am_i0", "physical_noise")))

store2 = {}
_orig2 = ts.simulate_das_instrument
ts.simulate_das_instrument = _fake_engine(store2)
try:
    try:
        M.t_das_instrument("CH4", wn_center=2968.5, d2nu_dI2=-2e-3, physical_noise=False)
    except _Stop:
        pass
finally:
    ts.simulate_das_instrument = _orig2
check("DAS 透传 d2nu_dI2", store2.get("d2nu_dI2") == -2e-3, f"= {store2.get('d2nu_dI2')!r}")
check("DAS 透传 physical_noise", store2.get("physical_noise") is False)
check("DAS 透传 edge/会话默认", store2.get("edge") == "rising", f"= {store2.get('edge')!r}")

# ───────────────────── 3. 激光自动重锚 ─────────────────────
print("=== 3. 越界波数的自动重锚 ===")
_dflt_l = ts.LASER_DEFAULTS
store3 = {}
ts.simulate_wms_instrument = _fake_engine(store3)
try:
    try:
        M.t_wms_instrument("H2O", wn_center=7185.6, mod_amp_V=0.1)      # 1.39 μm，远离默认激光
    except _Stop:
        pass
finally:
    ts.simulate_wms_instrument = _orig
_new_ref = store3.get("wn_ref")
_reach = (ts.laser_reach(7185.6, _dflt_l["eta_VI"], _dflt_l["dnu_dI"], _dflt_l["i_ref"],
                         _new_ref, 1.5) if _new_ref is not None else {})
check("越界时自动重锚 wn_ref 到'重锚后确实可达'的值",
      _new_ref is not None and _reach.get("reachable") is True,
      f"wn_ref={_new_ref}（比目标低 {7185.6 - float(_new_ref or 0):.2f} cm⁻¹，属正常："
      f"重锚把工作点放到驱动量程中点）")

store4 = {}
ts.simulate_wms_instrument = _fake_engine(store4)
try:
    try:
        M.t_wms_instrument("H2O", wn_center=7185.6, mod_amp_V=0.1, wn_ref=7185.0)
    except _Stop:
        pass
finally:
    ts.simulate_wms_instrument = _orig
check("显式给 wn_ref 时不介入（尊重用户的真实激光器）", store4.get("wn_ref") == 7185.0,
      f"wn_ref={store4.get('wn_ref')}")

store5 = {}
ts.simulate_wms_instrument = _fake_engine(store5)
try:
    try:
        M.t_wms_instrument("H2O", wn_center=7185.6, mod_amp_V=0.1, auto_laser=False)
    except _Stop:
        pass
finally:
    ts.simulate_wms_instrument = _orig
check("auto_laser=False 时不介入（交给引擎严格报错）",
      store5.get("wn_ref") is None, f"wn_ref={store5.get('wn_ref')}（应为 None = 未改）")

# ───────────────────── 4. 推荐波段可达性 ─────────────────────
print("=== 4. SPECIES_PROFILES 推荐波段可达性 ===")
_dflt = ts.LASER_DEFAULTS
for sp, p in M.SPECIES_PROFILES.items():
    for label, wn in p.get("bands", []):
        info = ts.laser_reach(wn, _dflt["eta_VI"], _dflt["dnu_dI"], _dflt["i_ref"],
                              _dflt["wn_ref"], 1.5)
        check(f"{sp} {label}({wn:g}) 自动重锚后有解",
              bool(info.get("fits_span")) and info.get("wn_ref_needed") is not None,
              f"-> wn_ref_needed={info.get('wn_ref_needed')}")
_adv = M.adaptive_condition("H2O")
check("adaptive_condition 带波段可达性",
      "波段可达性" in _adv and _adv["波段可达性"][0]["默认激光可达"] is False,
      f'-> {_adv["波段可达性"][0].get("需要")}')

# ───────────────────── 5. 输入校验 ─────────────────────
print("=== 5. 未知键 / 类型错误 / 批量请求 ===")
_base_args = {"peak_2f1f": 1.0, "species": "H2O", "wn0": 7185.596}
_r = M.handle_request({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                       "params": {"name": "tdlas_invert",
                                  "arguments": {**_base_args, "bogus_key": 1}}})
_txt = _r["result"]["content"][0]["text"]
check("未知键被拒绝并列出允许的键",
      _r["result"].get("isError") and "未声明的参数" in _txt, f"-> {_txt[:60]}")
_r = M.handle_request({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                       "params": {"name": "tdlas_invert",
                                  "arguments": {**_base_args, "peak_2f1f": "abc"}}})
check("类型错误给出人类可读信息",
      "期望 number" in _r["result"]["content"][0]["text"],
      f'-> {_r["result"]["content"][0]["text"][:60]}')
_r = M.handle_request({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                       "params": {"name": "tdlas_simulate", "arguments": {}}})
check("缺必填参数被拒绝", "缺少必填参数" in _r["result"]["content"][0]["text"])
_b = M.handle_request([{"jsonrpc": "2.0", "id": 1, "method": "ping"},
                       {"jsonrpc": "2.0", "id": 2, "method": "tools/list"}])
check("批量请求返回数组（含 tools/list）",
      isinstance(_b, list) and len(_b) == 2 and len(_b[1]["result"]["tools"]) == len(M.TOOLS))
check("空批量请求返回 -32600",
      M.handle_request([])["error"]["code"] == -32600)
check("非对象请求返回 -32600", M.handle_request("x")["error"]["code"] == -32600)
check("未知工具返回 -32601",
      M.handle_request({"id": 1, "method": "tools/call",
                        "params": {"name": "nope", "arguments": {}}})["error"]["code"] == -32601)

# ───────────────────── 6. 其他不变量 ─────────────────────
print("=== 6. 其他不变量 ===")
check("文件名净化挡穿越", "/" not in M._safe_name("../../x") and "\\" not in M._safe_name("..\\..\\x"),
      f'-> {M._safe_name("../../x")!r}')
check("默认 seed=0（结果可复现）",
      inspect.signature(M.t_wms_instrument).parameters["seed"].default == 0
      and inspect.signature(M.t_das_instrument).parameters["seed"].default == 0)
check("session_id 已在 wms/review/das 的 schema 中",
      all("session_id" in (next(t for t in M.TOOLS if t["name"] == n)["inputSchema"]["properties"])
          for n in ("tdlas_wms_instrument", "tdlas_review", "tdlas_das_instrument")))
check("review 的 schema 与 wms 同源（除出图开关）",
      set(next(t for t in M.TOOLS if t["name"] == "tdlas_review")["inputSchema"]["properties"])
      >= set(next(t for t in M.TOOLS if t["name"] == "tdlas_wms_instrument")["inputSchema"]["properties"]) - {"save_png"})
_old = M._SESSION_FILE
M._SESSION_FILE = Path(tempfile.mkdtemp()) / "_s.json"
M._save_sessions({"a": {"confirmed": {"x": 1}}})
check("会话写盘为原子替换（无 .tmp 残留）",
      json.loads(M._SESSION_FILE.read_text(encoding="utf-8"))["a"]["confirmed"]["x"] == 1
      and not M._SESSION_FILE.with_name(M._SESSION_FILE.name + ".tmp").exists())
M._SESSION_FILE = _old

import tdlas_hitran as TH                    # noqa: E402  （离线：HAPI 自带索引表）
check("同位素判据正确（HAPI 的 ISO 以 (M, I) 元组为键）",
      TH._isotope_known(1, 1) is True and TH._isotope_known(999, 1) is False,
      f"-> (1,1)={TH._isotope_known(1, 1)}, (999,1)={TH._isotope_known(999, 1)}")

print(f"\n===== 契约自检：{_OK} 通过 / {_BAD} 失败 =====")
raise SystemExit(1 if _BAD else 0)
