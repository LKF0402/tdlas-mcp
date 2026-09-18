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
import os
import re
import sys
import tempfile
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))
if str(_ROOT / "tools") not in sys.path:
    sys.path.insert(0, str(_ROOT / "tools"))

# 本脚本的断言文案含 → ↔ ⚠ 等符号；Windows 控制台默认 GBK/CP1252 会直接
# UnicodeEncodeError 崩在 print 上（CI 四个 Python 版本全挂过）。这里强制 UTF-8，
# 与 tools/tdlas_mcp.py 的 main() 同一做法，避免依赖调用方是否加了 -X utf8。
for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8")
    except Exception:
        pass

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

# ───────────────────── 4. 推荐波段可达性（判据：整段出光）─────────────────────
print("=== 4. SPECIES_PROFILES 推荐波段可达性（整段出光口径）===")
_dflt = ts.LASER_DEFAULTS
_KW = dict(eta_VI=_dflt["eta_VI"], dnu_dI=_dflt["dnu_dI"], i_ref=_dflt["i_ref"],
           wn_ref=_dflt["wn_ref"], scan_span_cm=1.5, i_th=_dflt["i_th"])


def _usable(wn, **over):
    """按 MCP 层的同一策略判定：默认不可达 ⇒ 用建议的 wn_ref 重锚后再判一次。"""
    kw = {**_KW, **over}
    r0 = ts.laser_reach(wn, **kw)
    if r0["reachable"]:
        return True, r0
    r1 = ts.laser_reach(wn, **{**kw, "wn_ref": r0["wn_ref_needed"]})
    return bool(r1["reachable"]), r1


def _fmt(wn, info):
    off, amp = info.get("offset_V"), info.get("amp_V")
    band = (f"扫描 [{off - amp:.3f}, {off + amp:.3f}] V" if (off is not None and amp is not None)
            else "扫描区间无解")
    return (f"-> wn_ref={info.get('wn_ref_needed'):.6g}, offset_V={off}, {band}, "
            f"reason={info.get('reason')}")


for sp, p in M.SPECIES_PROFILES.items():
    for label, wn in p.get("bands", []):
        ok, info = _usable(wn)
        # 断言不止"不报错"：必须整段扫描都在阈值之上（否则波形被截断、2f/1f 失真）
        check(f"{sp} {label}({wn:g}) 重锚后**整段出光**可用", ok, _fmt(wn, info))

# 判据回归：默认激光的三档边界（曾出现"扫描中心出光即算可达"→ 部分出光被静默放行）
check("≤2971.0 判为可达（整段出光）", _usable(2971.0)[0] is True)
check("2971.5 判为不可达（部分出光）", ts.laser_reach(2971.5, **_KW)["reachable"] is False,
      f'-> reason={ts.laser_reach(2971.5, **_KW)["reason"]}')
check("2972.0 判为不可达（部分出光）",
      ts.laser_reach(2972.0, **_KW)["reachable"] is False)
check("2975.0 判为不可达（全段无输出）",
      ts.laser_reach(2975.0, **_KW)["reachable"] is False)
# 引擎侧必须共用同一判据，否则"引擎放行 / MCP 层认为不可达"会两处漂移
_cfg = {"eta_VI": 24.0, "dnu_dI": -0.088, "i_ref": 120.0, "wn_ref": 2964.7, "i_th": 30.0,
        "amp_V": 0.7102272727272727, "scan_span_cm": 1.5, "d2nu_dI2": 0.0, "offset_V": 1.544}
try:
    ts._require_driver_range(dict(_cfg), 2972.0)
    check("引擎侧拒绝'部分出光'档", False, "却放行了")
except ValueError as e:
    check("引擎侧拒绝'部分出光'档", "未整段高于阈值电压" in str(e), f"-> {str(e)[:46]}")
try:
    ts._require_driver_range(dict(_cfg, offset_V=3.2), 2968.5)
    check("引擎侧放行'整段出光'档", True)
except ValueError as e:
    check("引擎侧放行'整段出光'档", False, f"-> {str(e)[:46]}")
check("_require_driver_range 与 laser_reach 共用判据（防两处漂移）",
      "laser_reach(" in inspect.getsource(ts._require_driver_range))
# 加固：不只是"出现过 laser_reach("，而是两边都真的走共享判据 _scan_window_reason
check("laser_reach 与 _require_driver_range 都调用共享判据 _scan_window_reason",
      "_scan_window_reason(" in inspect.getsource(ts.laser_reach)
      and "_scan_window_reason(" in inspect.getsource(ts._require_driver_range))
check("三档边界 reason 精确匹配（ok / 部分出光 / 全段无光 / 越上限）",
      ts.laser_reach(2970.0, **_KW)["reason"] == "ok"
      and ts.laser_reach(2972.0, **_KW)["reason"] == "scan_partially_dark"
      and ts.laser_reach(2975.0, **_KW)["reason"] == "below_threshold_current"
      and ts.laser_reach(2900.0, **_KW)["reason"] == "out_of_driver_range",
      f"-> 2970:{ts.laser_reach(2970.0, **_KW)['reason']} "
      f"2972:{ts.laser_reach(2972.0, **_KW)['reason']} "
      f"2975:{ts.laser_reach(2975.0, **_KW)['reason']} "
      f"2900:{ts.laser_reach(2900.0, **_KW)['reason']}")

# ★ 显式 offset_V 必须按**实际生效**的偏置校验（不得拿 wn_center 反算的偏置代替）。
#   曾踩到：`_require_driver_range` 改用 laser_reach(wn_center) 后，offset_V=9 V（越上限）
#   被静默放行（ν 轴跑到线表窗口外，只剩 ADC 饱和之类的下游症状）；而显式合法 offset_V
#   配上不可达的 wn_center 标签又会被误拒，报错文本自相矛盾。
try:
    ts._require_driver_range(dict(_cfg, offset_V=9.0), 2968.5, False)
    check("显式 offset_V 越驱动器上限必须被拒", False, "offset_V=9 V 被静默放行")
except ValueError as e:
    check("显式 offset_V 越驱动器上限必须被拒", "越出驱动器上限" in str(e), f"-> {str(e)[:40]}")
try:
    ts._require_driver_range(dict(_cfg, offset_V=2.0), 3000.0, False)
    check("显式合法 offset_V 不因 wn_center 标签不可达被误拒", True)
except ValueError as e:
    check("显式合法 offset_V 不因 wn_center 标签不可达被误拒", False, f"-> {str(e)[:40]}")
# 未显式给 offset_V 时，偏置由 wn_center 反算 → 该档必须仍然被拒（整段出光判据不许放松）
try:
    ts._require_driver_range(dict(_cfg), 2972.0, True)
    check("反算路径的‘部分出光’档仍被拒", False, "却放行了")
except ValueError as e:
    check("反算路径的‘部分出光’档仍被拒", "未整段高于阈值电压" in str(e), f"-> {str(e)[:40]}")

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
# 反演的**配对量**必须是 S2f_norm_peak（sensitivity_k 的分子），不是 S2f1f_peak ——
# 后者只在归一化方法为 2f/1f 时才相等，退化为 2f/I0 时配 k 会算错浓度。
# 该指引曾漂移过一次，用断言钉死：允许提到 S2f1f_peak，但必须在"否定/仅当/退化"语境里。
def _pairing_ok(text, where):
    if "S2f_norm_peak" not in text:
        return False, f"{where} 未提到配对量 S2f_norm_peak"
    for sent in re.split(r"[。；\n]", text):
        if "S2f1f_peak" in sent and not any(w in sent for w in
                                            ("不是", "不等于", "仅当", "才等于", "退化", "勿", "而配")):
            return False, f"{where} 疑似仍把 S2f1f_peak 当配对量：{sent.strip()[:48]}"
    return True, ""


_inv = next(t for t in M.TOOLS if t["name"] == "tdlas_invert")
_ok, _why = _pairing_ok(_inv["description"], "tdlas_invert.description")
check("invert 指引配对量为 S2f_norm_peak（且未推荐 S2f1f_peak）", _ok, f"-> {_why}")
_ok2, _why2 = _pairing_ok(_inv["inputSchema"]["properties"]["peak_2f1f"]["description"],
                          "peak_2f1f.description")
check("peak_2f1f 参数说明点明配对量", _ok2, f"-> {_why2}")
_wms_src = inspect.getsource(M.t_wms_instrument)
check("wms 返回里带 S2f_norm_peak_note 说明与 k 的配对关系",
      "S2f_norm_peak_note" in _wms_src and "sensitivity_k 的分子" in _wms_src)
_ok3, _why3 = _pairing_ok(inspect.getsource(M.t_invert), "t_invert 文档串")
check("t_invert 文档串同样说明配对量", _ok3, f"-> {_why3}")
# 激光参数回显：数值不受影响，但没有它就无法核对"引擎按哪组激光参数算的"（尤其自动重锚后）
check("激光参数回显键含 wn_ref（重锚后会变的那一项）", "wn_ref" in M._LASER_ECHO_KEYS)
check("WMS/DAS 均回显生效的激光参数",
      "_LASER_ECHO_KEYS" in inspect.getsource(M.t_wms_instrument)
      and "_LASER_ECHO_KEYS" in inspect.getsource(M.t_das_instrument))

_old = M._SESSION_FILE
M._SESSION_FILE = Path(tempfile.mkdtemp()) / "_s.json"
M._save_sessions({"a": {"confirmed": {"x": 1}}})
check("会话写盘为原子替换（无 .tmp 残留）",
      json.loads(M._SESSION_FILE.read_text(encoding="utf-8"))["a"]["confirmed"]["x"] == 1
      and not list(M._SESSION_FILE.parent.glob(M._SESSION_FILE.name + "*.tmp")))
# 并发写同一文件不得抛异常（Windows 上 os.replace 会 WinError 5 → 已加重试+降级）
try:
    M._save_sessions({"b": {"confirmed": {"y": 2}}})
    M._save_sessions({"c": {"confirmed": {"z": 3}}})
    check("连续写盘不抛异常（含重试/降级路径）",
          json.loads(M._SESSION_FILE.read_text(encoding="utf-8"))["c"]["confirmed"]["z"] == 3)
except Exception as e:                                  # noqa: BLE001
    check("连续写盘不抛异常（含重试/降级路径）", False, f"-> {type(e).__name__}: {e}")
M._SESSION_FILE = _old

import tdlas_hitran as TH                    # noqa: E402  （离线：HAPI 自带索引表）
check("同位素判据正确（HAPI 的 ISO 以 (M, I) 元组为键）",
      TH._isotope_known(1, 1) is True and TH._isotope_known(999, 1) is False,
      f"-> (1,1)={TH._isotope_known(1, 1)}, (999,1)={TH._isotope_known(999, 1)}")

# ── 保真度台账不得与 MCP 层脱节 ────────────────────────────────────────
# 本项目已因"声明了却没接上"栽过两次（`edge`/`trim_frac` 一族、DAS 的 etalon 几何参数），
# 所以 guide 里发给 AI 的保真度台账也必须与真源同源，否则等于又开一个静默错误入口。
import tdlas_fidelity as FID                  # noqa: E402
_fid = (M.t_guide().get("fidelity") or {})
check("tdlas_guide 返回保真度台账（非降级兜底）",
      "not_implemented" in _fid and "unavailable" not in _fid,
      f"-> keys={sorted(_fid)[:6]}")
check("台账与登记表同源（未实现项数量一致）",
      len(_fid.get("not_implemented") or []) == len(FID.missing_effects()),
      f"-> guide={len(_fid.get('not_implemented') or [])} / table={len(FID.missing_effects())}")
check("AI_INTERACTION_GUIDE 含 fidelity_reporting（披露口径）",
      "fidelity_reporting" in M.AI_INTERACTION_GUIDE)
check("wms 的 must_disclose 含保真度项（⑧）", "⑧" in inspect.getsource(M.t_wms_instrument))
check("登记表证据 ↔ 源码一致（审计无问题）", not FID.audit_effects(),
      f"-> {FID.audit_effects()[:3]}")
check("schema 声明的物理参数全部可达（无死参数）",
      all(not dead for _, dead in FID.audit_parameters()),
      f"-> {[d for _, d in FID.audit_parameters() if d]}")

# ── AI 交互契约（结构化动作） ──────────────────────────────────────────
# 为什么要有这一节：交互规范原本是散文（AI_INTERACTION_GUIDE / must_disclose），散文会漂、
# 漂了没有测试会发现。改成机器可读动作后，这些断言就是"防漂"的守门人。
for _t in ("tdlas_wms_instrument", "tdlas_das_instrument", "tdlas_review", "tdlas_invert"):
    _src = inspect.getsource(M.DISPATCH[_t])
    check(f"{_t} 下发交互契约（interaction / next_required_actions / fidelity）",
          "_interaction_block(" in _src and "fidelity" in _src)
# DAS 曾有 clarify 不对称（最需要先问清工况的一条链反而没有问句）
check("DAS 与 WMS 的 clarify 结构对称",
      "clarify" in inspect.getsource(M.t_das_instrument)
      and "build_clarify_questions(" in inspect.getsource(M.t_das_instrument))
check("review 补齐交互字段（assumptions / clarify / fidelity）",
      all(k in inspect.getsource(M.t_review) for k in ("assumptions", "clarify", "fidelity")))

# A4：工具描述里出现的 `results.xxx` 引用必须在该工具源码里真实存在为键（防漂移 / 拼写错）。
# ⚠ 诚实边界：这条只能拦"引用了**不存在**的量"，**拦不住"引用错了量"** —— 后者是语义错误
#   （`S2f1f_peak` 与 `S2f_norm_peak` 那起事故：前者确实还在返回里，只是不该与 k 配对），
#   只能靠配对说明 + 断言钉住（见上文 _pairing_ok / S2f_norm_peak_note 的检查）。
# 判据取"全文件出现过该键名"：描述里合法地会**跨工具**引用别人返回的字段
# （如 tdlas_invert 要你传 wms 的 results.S2f_norm_peak），故不能只在本工具源码里找。
_all_src = Path("tdlas_mcp.py").read_text(encoding="utf-8") \
    if Path("tdlas_mcp.py").exists() else inspect.getsource(M)
_ref_bad = []
for _t in M.TOOLS:
    for _m in re.finditer(r"results\.([A-Za-z_]\w*)", json.dumps(_t, ensure_ascii=False)):
        if f'"{_m.group(1)}"' not in _all_src:
            _ref_bad.append((_t["name"], _m.group(1)))
check("工具描述引用的 results.xxx 必须真实存在（防漂移/拼写错）", not _ref_bad, f"-> {_ref_bad}")

_sess = {}
_ols, _oss = M._load_sessions, M._save_sessions
M._load_sessions = lambda: _sess          # 内存会话：不碰真实 .tdlas_session.json
M._save_sessions = lambda s: None
try:
    _base = {"species": "CH4", "results": {},
             "conditions": {"T_K": 296.0, "P_atm": 1.013, "x": 1e-4, "L_cm": 50.0},
             "assumptions": ["T", "P", "x", "L_cm"],
             "fidelity": {"not_implemented": ["自展宽"], "rule": "必须说明未建模项"},
             "validation": {"overall": "pass", "checks": []}}
    _it1, _a1 = M._interaction_block(dict(_base), "wms", "ctest")
    check("交互契约：动作只指向本返回里真实存在的字段",
          all(M._ev_ok(_base, p) for a in _a1 for p in a["evidence_fields"]),
          f"-> {[(a['id'], a['evidence_fields']) for a in _a1]}")
    # ★ 回归锁：缺参数是每个新会话的默认状态（默认工况下 assumptions 达 35 项、题库 36 问），
    #   若把它算作阻断，conclusion_allowed 将**恒为 false** 并失去信息量 —— 已实测到。
    check("交互契约：缺参数不阻断结论（只要求披露）",
          _it1["conclusion_allowed"] is True
          and not any(a["severity"] == M._SEV_BLOCK for a in _a1),
          f"-> blocking_reason={_it1.get('blocking_reason')}")
    _it2, _a2 = M._interaction_block(dict(_base), "wms", "ctest")
    check("交互契约：同语境第二轮不再重复下发（按需下发生效）",
          _a2 == [] and _it2["stage"] == "verified" and bool(_it2["disclosure_pending"]),
          f"-> fresh={[a['id'] for a in _a2]} stage={_it2['stage']}")
    _bad = dict(_base)
    _bad["validation"] = {"overall": "fail",
                          "checks": [{"check": "波数轴对齐", "status": "fail"}]}
    _it3, _a3 = M._interaction_block(_bad, "wms", "ctest")
    check("交互契约：校验 fail 必须阻断结论并给出证据",
          _it3["conclusion_allowed"] is False
          and any(a["id"] == "fix_validation_fail" for a in _a3)
          and "波数轴对齐" in (_it3["blocking_reason"] or ""),
          f"-> {_it3.get('blocking_reason')}")
    _dark = dict(_base, warnings=["⚠ 扫描段有约 46% 无激光输出"])
    _it4, _a4 = M._interaction_block(_dark, "wms", "ctest")
    check("交互契约：扫描含暗区必须阻断结论",
          _it4["conclusion_allowed"] is False
          and any(a["id"] == "partial_dark_no_conclusion" for a in _a4))
    # 真实工具（离线路径：显式给 k 时不跑引擎）
    _inv = M.t_invert(peak_2f1f=1e-3, species="CH4", wn0=2968.5, k=1.0, session_id="ctest2")
    check("tdlas_invert 真实返回带交互契约且允许结论",
          {"interaction", "next_required_actions", "fidelity"} <= set(_inv)
          and _inv["interaction"]["conclusion_allowed"] is True)
    check("反演未附不确定度时必须要求说明",
          any(a["id"] == "attach_uncertainty" for a in _inv["next_required_actions"]),
          f"-> {[a['id'] for a in _inv['next_required_actions']]}")
finally:
    M._load_sessions, M._save_sessions = _ols, _oss

# ───────────────────── 8. x_list 多浓度扫描（解析+防呆+结构） ─────────────────────
print("=== 8. x_list 多浓度扫描 ===")
_by = {t["name"]: t for t in M.TOOLS}
for _nm in ("tdlas_simulate", "tdlas_das_chain", "tdlas_das_instrument", "tdlas_wms_instrument"):
    check(f"{_nm} schema 声明 x_list",
          "x_list" in (_by[_nm]["inputSchema"]["properties"] or {}),
          f"-> {_nm}")

# 透传：x_list 的首个浓度（升序去重后）应作为 x 喂给引擎
store = {}
_orig = ts.simulate_wms_instrument
ts.simulate_wms_instrument = _fake_engine(store)
try:
    try:
        M.t_wms_instrument("CH4", wn_center=2968.5, x_list=["2e-3", "1e-3", "1e-4"])
    except _Stop:
        pass
finally:
    ts.simulate_wms_instrument = _orig
check("x_list 透传：升序后首个浓度作为 x 喂给引擎", store.get("x") == 1e-4,
      f"-> x={store.get('x')!r}")

# 防呆：x_list 含越界浓度（>1 或 ≤0）必须被拒绝，而非静默截断
_err = None
try:
    M.t_simulate("CH4", wn0=2968.5, x_list=[1e-3, 2.0])
except ValueError as e:
    _err = e
check("防呆：x_list 含越界浓度被拒绝", _err is not None, f"-> {_err}")

# 防呆：x 与 x_list 同时传必须冲突报错（二选一）
_err2 = None
try:
    M.t_simulate("CH4", wn0=2968.5, x=1e-3, x_list=[1e-4, 1e-3])
except ValueError as e:
    _err2 = e
check("防呆：x 与 x_list 同时传被拒绝", _err2 is not None, f"-> {_err2}")

# 防呆：空列表被拒绝
_err3 = None
try:
    M.t_simulate("CH4", wn0=2968.5, x_list=[])
except ValueError as e:
    _err3 = e
check("防呆：空 x_list 被拒绝", _err3 is not None, f"-> {_err3}")

# 结构：真实跑一次，multi_conc 必须含扫描曲线 + 线性/饱和防呆诊断
_mc = M.t_simulate("CH4", wn0=2968.5, x_list=["1e-4", "1e-3"])
_here = _mc.get("multi_conc")
check("x_list 返回 multi_conc 扫描结构",
      _here and _here.get("enabled")
      and len(_here.get("x_list", [])) == 2
      and len(_here.get("mole_ppm", [])) == 2
      and "response" in _here and "linearity" in _here,
      f"-> {sorted(_here.keys()) if _here else None}")
check("multi_conc 线性诊断生效（≥2 点则判线性）",
      _here and _here.get("linearity", {}).get("checked") is True,
      f"-> {_here.get('linearity') if _here else None}")

# ───────────────────── 9. 多浓度同图 png_overlay + BUG-D 透出 X/Y ─────────────────────
print("=== 9. 多浓度同图 png_overlay + BUG-D 透出 X/Y ===")
# 兼容性操作：x_list + save_png 必须产出多浓度同图（覆盖在单浓度详图之外）
_ov = M.t_simulate("CH4", wn0=2968.5, x_list=["1e-4", "1e-3"], save_png=True)
check("x_list+save_png 产出 png_overlay（多浓度同图）",
      "png_overlay" in _ov and os.path.exists(_ov["png_overlay"]),
      f"-> {_ov.get('png_overlay')}")
_ovw = M.t_wms_instrument("CH4", wn_center=2968.5, x_list=["1e-4", "1e-3"], save_png=True)
check("WMS x_list+save_png 产出 png_overlay",
      "png_overlay" in _ovw and os.path.exists(_ovw["png_overlay"]),
      f"-> {_ovw.get('png_overlay')}")
# 向后兼容：单浓度或没有 save_png 都不应产出 png_overlay
_single = M.t_simulate("CH4", wn0=2968.5, save_png=True)
check("单浓度+save_png 不产出 png_overlay（兼容性）", "png_overlay" not in _single,
      f"-> keys={list(_single)}")
_nosvg = M.t_simulate("CH4", wn0=2968.5, x_list=["1e-4", "1e-3"])
check("多浓度但不 save_png 不产出 png_overlay（兼容性）", "png_overlay" not in _nosvg,
      f"-> keys={list(_nosvg)}")
# BUG-D 修复：return_xy=True 透出锁相正交 X/Y 分量
_xy = M.t_wms_instrument("CH4", wn_center=2968.5, return_xy=True)
check("BUG-D 修复：return_xy=True 透出 X/Y 正交分量",
      "wms_raw_xy" in _xy and {"X1f_c", "Y1f_c", "X2f_c", "Y2f_c",
                               "X1f_bg_c", "Y1f_bg_c", "X2f_bg_c", "Y2f_bg_c"}.issubset(
          _xy.get("wms_raw_xy", {}).keys()),
      f"-> {sorted(_xy.get('wms_raw_xy', {}).keys())}")
_xy0 = M.t_wms_instrument("CH4", wn_center=2968.5, return_xy=False)
check("return_xy 默认 false 不返回 X/Y（避免返回体膨胀）", "wms_raw_xy" not in _xy0,
      f"-> keys={list(_xy0)}")

print(f"\n===== 契约自检：{_OK} 通过 / {_BAD} 失败 =====")
raise SystemExit(1 if _BAD else 0)
