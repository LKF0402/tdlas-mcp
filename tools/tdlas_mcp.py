#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""TDLAS/WMS 仿真 MCP 服务器（stdio JSON-RPC 2024-11-05）。

把 tdlas_sim 的仿真能力暴露为 AI 可直接调用的工具：
  · tdlas_simulate         DAS + 免标定 WMS 正向仿真（1f、2f 谐波、峰高、可选 PNG）
  · tdlas_invert           免标定浓度反演（2f/1f 峰高 → 摩尔分数）
  · tdlas_detection_limit  检测极限 LOD（等效透过率噪声 σ_τ → NEC / LOD）
  · tdlas_selftest         全链路自检

纪律（沿用 hitran-mcp）：纯标准库 stdio JSON-RPC；HITRAN 取数由本仓库提供（HAPI 1.x，免 key）；
所有 print 收进 log 字段，绝不污染 stdout 协议流。

自测：python tools/tdlas_mcp.py --selftest
"""
from __future__ import annotations

import contextlib
import datetime as _dt
import hashlib
import io
import json
import math
import os
import re
import sys
import time
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

import tdlas_sim as ts  # noqa: E402

PROTOCOL_VERSION = "2024-11-05"
SERVER_INFO = {"protocolVersion": PROTOCOL_VERSION,
               "capabilities": {"tools": {}},
               "serverInfo": {"name": "tdlas", "version": "0.2.0"}}
OUT_DIR = _ROOT / "tmp" / "mcp_out"

# 远端直链隐私：MCP 返回值（尤其 tools/call 的 log 字段）会被序列化发给远端客户端，
# 其中可能含本地绝对路径（如 HITRAN 缓存目录 Hitran_Data、PNG 输出目录 tmp/mcp_out）。
# 在返回边界统一脱敏：递归把用户目录/工作区绝对路径替换成中性标记，避免泄露服务器目录结构。
# 注意：不硬编码任何真实路径字符串（符合 push 前安全扫描规则），改用 Path.home() 动态获取。
_HOME = str(Path.home())
_WS_ROOT = str(_ROOT.parent)


def _sanitize_paths(obj):
    """递归把绝对路径（用户目录/工作区）替换成中性标记，避免远端返回值泄露服务器目录结构。"""
    if isinstance(obj, str):
        s = obj
        if _WS_ROOT and _WS_ROOT.lower() in s.lower():
            s = re.sub(re.escape(_WS_ROOT), "<workspace>", s, flags=re.IGNORECASE)
        if _HOME and _HOME.lower() in s.lower():
            s = re.sub(re.escape(_HOME), "<home>", s, flags=re.IGNORECASE)
        return s
    if isinstance(obj, list):
        return [_sanitize_paths(x) for x in obj]
    if isinstance(obj, dict):
        return {k: _sanitize_paths(v) for k, v in obj.items()}
    return obj

# 对话状态机：跨会话记住用户已确认的工况参数（存 JSON，MCP 重启/换会话仍保留）
# 存仓库根目录隐藏文件（中性命名，不暴露宿主工具/IDE）
_SESSION_FILE = _ROOT / ".tdlas_session.json"


def _load_sessions():
    if _SESSION_FILE.exists():
        try:
            return json.loads(_SESSION_FILE.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return {}
    return {}


def _atomic_write_text(path, text, tries=5):
    """原子写盘：先写同目录临时文件再 os.replace 覆盖（带重试与降级）。

    为什么：会话/设备文件的读-改-写不是原子操作，MCP 崩溃或断电时会留下**半截 JSON**
    （下次 _load_* 直接 JSONDecodeError → 静默丢失全部已确认工况）。os.replace 在
    同一文件系统上是原子操作，要么旧内容、要么新内容。

    ⚠ Windows 上的坑（实测）：**两个进程同时写同一个文件**时，`os.replace` 会抛
    `PermissionError WinError 5`（目标被另一方短暂占用）。多客户端（IDE + CLI）或
    并行测试都会踩到 → 这里重试几次；仍失败则降级为直接写（放弃原子性，
    但绝不让一次工具调用因为"写会话状态"而失败）。临时文件名带 pid，避免两进程互相覆盖。
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    tmp.write_text(text, encoding="utf-8")
    for i in range(tries):
        try:
            os.replace(tmp, path)
            return
        except PermissionError:
            time.sleep(0.05 * (i + 1))
    try:                                   # 兜底：直接写，别把工具调用搞崩
        path.write_text(text, encoding="utf-8")
    finally:
        try:
            tmp.unlink()
        except OSError:
            pass


def _save_sessions(sessions):
    _atomic_write_text(_SESSION_FILE, json.dumps(sessions, ensure_ascii=False, indent=2))


# ══════════════════ α 语境的自适应播报 ══════════════════
# α（吸收系数 / αL）峰值随 T、P、网格步长 step、翼截断 wingHW、波数窗口 变化：
# 裸报一个 α 数值不可复现、不可比较。但每次都报又啰嗦 → 做成"自适应"：
# 仅在 ① 本会话首次给出 α 峰值，或 ② 语境相对上次播报发生变化 时，才要求 AI 播报。
# 记录写入会话态（.tdlas_session.json），跨 MCP 重启仍有效。
_ALPHA_CTX_FIELDS = ("T_K", "P_atm", "step_cm-1", "wingHW_cm-1")


def _alpha_report_block(alpha_context, species, session_id="default"):
    """自适应决定是否需在回答中显式播报 α 峰值的计算语境。

    规则（任一命中 → needs_report=True）：
      ① 本会话首次给出 α 峰值（无既往播报记录）；
      ② 语境变化：物种 或 (T, P, step, wingHW, 窗口) 与上次已播报的不同。
    返回 None 表示本次结果不含 α 语境（该工具未报 α）。
    """
    if not alpha_context:
        return None
    snapshot = {"species": str(species).upper(),
                **{k: alpha_context.get(k) for k in _ALPHA_CTX_FIELDS},
                "window_cm-1": alpha_context.get("window_cm-1")}
    sessions = _load_sessions()
    sess = sessions.get(session_id, {"confirmed": {}, "pending": [], "stage": "clarify"})
    prev = sess.get("last_alpha_report")
    reasons = []
    if prev is None:
        reasons.append("本会话首次给出 α 峰值")
    elif prev != snapshot:
        reasons.append("α 计算语境与上次不同")
    needs = bool(reasons)
    if needs:
        sess["last_alpha_report"] = snapshot
        sessions[session_id] = sess
        _save_sessions(sessions)
    return {"alpha_peak_context": alpha_context,
            "needs_report": needs,
            "report_reason": "；".join(reasons) if reasons else "语境未变且已播报过，无需重复",
            "report_rule": "needs_report=True 时：给出 α 峰值**必须同时**列出 T(K)、P(atm)、"
                           "网格步长 step(cm⁻¹)、翼截断 wingHW(cm⁻¹)、波数窗口(cm⁻¹)——缺一不可；"
                           "needs_report=False 时数值口径不变，可省略以保持简洁。"}


def _fringe_report_block(fringe_meta, session_id="default"):
    """自适应决定是否需播报 etalon 条纹的影响评估（首次 或 语境变化）。

    未启用条纹模型时返回固定说明且**不写会话**（默认关 → 不打扰用户）。
    """
    if not (fringe_meta or {}).get("enabled"):
        return {"enabled": False, "needs_report": False,
                "note": "未启用 etalon 条纹模型（默认理想仿真）"}
    fsr = float(fringe_meta["fsr_cm-1"])
    ctr = float(fringe_meta["contrast"])
    snapshot = {"fsr_cm-1": fsr, "contrast": ctr,
                "drift_frac": float(fringe_meta.get("drift_frac") or 0.0)}
    sessions = _load_sessions()
    sess = sessions.get(session_id, {"confirmed": {}, "pending": [], "stage": "clarify"})
    prev = sess.get("last_fringe_report")
    reasons = []
    if prev is None:
        reasons.append("本会话首次启用 etalon 条纹")
    elif prev != snapshot:
        reasons.append("etalon 条纹语境与上次不同")
    needs = bool(reasons)
    if needs:
        sess["last_fringe_report"] = snapshot
        sessions[session_id] = sess
        _save_sessions(sessions)
    return {"enabled": True, "fsr_cm-1": fsr, "contrast": ctr,
            "fringe_context": fringe_meta,
            "needs_report": needs,
            "report_reason": "；".join(reasons) if reasons else "语境未变且已播报过，无需重复",
            "report_rule": "needs_report=True 时须向用户说明：① 条纹 FSR 与吸收线宽的关系"
                           "（决定是否会在 2f 上伪造吸收峰）；② 确定性条纹会被背景扣除消除、"
                           "只有漂移残留；③ 压不掉条纹时该做的物理措施（窗片楔化 / AR 镀膜 / 扫频平均）。"}


# ══════════════ AI 交互契约：结构化动作（可校验 / 按需下发） ══════════════
# 为什么要有这一层：项目的"必须澄清 / 必须披露"一直以**散文**形式写在 AI_INTERACTION_GUIDE
# 里，而散文会漂、漂了不会有任何测试发现 —— 三起真实事故（`edge` 一族被参数白名单丢弃、
# `tdlas_invert` 描述指向 `S2f1f_peak`、`allow_partial_dark` 曾被白名单丢弃）全靠人肉复核
# 才发现。本层把它升级为**机器可读的动作**：
#   ① 每条动作指向返回值里的**具体字段**（evidence_fields），AI 不必自己找数据，也不易漏；
#   ② 三级严重度；"能不能给定量结论"由服务器算出，而不是靠模型自觉；
#   ③ **按需下发**：只下"本会话尚未下发过"的动作。每次返回都重发整张清单等于把散文搬进
#      payload 且反复计费 —— 门控复用 alpha_report / fringe_report 的既有范式；
#   ④ `build_next_actions()` 是**纯函数**，契约测试可直接断言，不必起 MCP。
# 诚实的局限：服务器看不到模型最终的措辞，故本层只保证"要求已下发且不重复骚扰"，
# **不能**保证模型照做。要真正闭环需要模型自报回执（未做：为不可验证的目标增加一次
# 往返不划算；见 docs/VALIDATION.md §8）。
_SEV_BLOCK = "block_conclusion"   # 未满足 → 不得给出定量结论（结果照给，结论受控）
_SEV_DISCLOSE = "must_disclose"   # 结论可给，但必须在同一回答里说明（缺一不可）
_SEV_ADVISORY = "advisory"        # 建议
_SEV_ORDER = {_SEV_BLOCK: 0, _SEV_DISCLOSE: 1, _SEV_ADVISORY: 2}


def _ev_ok(out, path):
    """evidence_fields 路径解析：`a.b` 逐层取值；任一层缺失即 False。

    为什么要它：动作必须只指向**本次返回里真实存在**的字段，否则 AI 按 id 执行时会
    找不到数据（这正是要消灭的那类漂移）。契约测试另有一条断言兜底（见 contract_check）。
    """
    cur = out
    for part in str(path).split("."):
        if isinstance(cur, dict) and part in cur:
            cur = cur[part]
        else:
            return False
    return True


def _fidelity_digest():
    """随结果下发的**精简**保真度声明（真源仍是 tools/tdlas_fidelity.py，此处不复制内容）。"""
    full = _fidelity_summary()
    if "unavailable" in full:
        return {"unavailable": full.get("unavailable"),
                "rule": full.get("disclosure_rule")}
    return {"modeled_count": len(full.get("implemented") or []),
            "optional_available": [e.get("id") for e in (full.get("optional") or [])],
            "not_implemented": [e.get("title") for e in (full.get("not_implemented") or [])],
            "rule": full.get("disclosure_rule"),
            "detail": "完整台账见 tdlas_guide 的 fidelity（含每项的实现位置与缺口说明）"}


def _condition_values(out):
    """取值本次**实际生效**的工况参数（T/P/x/L），兼容两种回显形状。

    仪器链（wms/das）给 `conditions` 块；分析链（invert 等）是平铺的 T_K/P_atm/path_cm。
    `assumptions` 只说"哪些用了默认"，不给数值 —— 缺了本函数，动作就只能要求 AI
    "说明用了默认值"却报不出数值。
    """
    c = out.get("conditions") or {}
    return {"T": c.get("T_K", out.get("T_K")),
            "P": c.get("P_atm", out.get("P_atm")),
            "x": c.get("x", out.get("x_ref", out.get("x"))),
            "L_cm": c.get("L_cm", out.get("path_cm"))}


def _packing_state(out=None):
    """判断"仪器打包"这件事现在该不该提醒用户。

    为什么单独出一个函数：它是 **AI 主动提醒** 的判据源，
    而提醒本身必须可退避（否则每次返回都在叽叨，AI 会忽略它）。

    判据：
      · 设备库里已有设备，但**还没打包成任何 setup** → 该提醒（用户已花力气录过设备，却没用上）；
      · 四类设备缺某一类 → 告知缺什么（AI 好向用户索取）；
      · 已有 setup 且已设默认 → 不提醒（事情已做完）；
      · 已有 setup 但未设默认 → 轻提醒一次（设默认后每次都能自动套用）。

    返回 dict；`should_prompt=False` 时调用方不应下发任何提醒。
    """
    try:
        dev = _load_devices()
    except Exception:                                      # noqa: BLE001
        return {"should_prompt": False}
    counts = {k: len(dev.get(k) or {}) for k in ("laser", "pd", "daq", "optics")}
    setups = dev.get("setup") or {}
    dflt = dev.get("default") or {}
    if sum(counts.values()) == 0:
        # 设备库为空：当前结果全部来自演示默认值，必须引导用户建自己的设备库
        return {"should_prompt": True, "reason": "device_library_empty",
                "note": "设备库为空：当前结果全部来自演示默认参数。",
                "missing_devices": [
                    {"device_type": "laser", "cn": "DFB 激光器",
                     "fields": [{"key": "center_wn", "cn": "中心波数", "unit": "cm⁻¹"},
                                {"key": "tuning_coeff", "cn": "调谐系数 dν/dI", "unit": "cm⁻¹/mA"}]},
                    {"device_type": "pd", "cn": "光电探测器",
                     "fields": [{"key": "responsivity", "cn": "响应度", "unit": "A/W"},
                                {"key": "bandwidth", "cn": "带宽", "unit": "MHz"}]},
                    {"device_type": "daq", "cn": "数据采集卡",
                     "fields": [{"key": "sample_rate", "cn": "采样率", "unit": "kHz"},
                                {"key": "resolution", "cn": "分辨率", "unit": "bit"}]},
                    {"device_type": "optics", "cn": "光学系统",
                     "fields": [{"key": "path_length", "cn": "光程", "unit": "cm"}]}
                ],
                "ask_user": ("⚠️ 当前结果全部来自演示默认参数（不是你的真实设备）。"
                             "建议建立你自己的设备库——只需告诉我你的激光器、探测器、采集卡、光学系统的型号，"
                             "我会自动查规格书并录入。之后每次仿真都会用你的真实硬件参数，结果才准确。"),
                "how": "告诉我你的设备型号（如 NI USB-6211、Thorlabs PDA10D2），我帮你建库"}
    missing = [k for k, v in counts.items() if v == 0]
    # 对 AI 与用户都只认中文名：直接输出 'pd' 不利于转述，也不利于用户理解缺什么。
    missing_cn = [{"device_type": k, "cn": _DEVICE_TYPE_CN[k],
                   "fields": [{"key": kk,
                               "cn": (PARAM_ACQ_GUIDE.get(kk) or (kk, "-", ""))[0],
                               "unit": (PARAM_ACQ_GUIDE.get(kk) or (kk, "-", ""))[1]}
                              for kk in _DEVICE_SALIENT_KEYS.get(k, [])]}
                  for k in missing]
    if not setups:
        return {"should_prompt": True, "reason": "has_devices_no_setup",
                "device_counts": counts, "missing_device_types": missing,
                "missing_devices": missing_cn,
                "ask_user": ("你已经建了设备库，但还没打包成整机配置。"
                             "打包后只需一个 `setup=名字` 就能一键套用全部硬件参数，"
                             "不必每次重填。" +
                             (f"目前还缺："
                              + "、".join(m["cn"] for m in missing_cn) + "。" if missing else "")),
                "how": "tdlas_device action=save_setup name=... laser=... pd=... daq=... optics=..."}
    if not dflt:
        return {"should_prompt": True, "reason": "has_setup_no_default",
                "setups": sorted(setups.keys()),
                "ask_user": ("已有整机配置，但未设默认。设默认后，"
                             "未指定设备时会自动套用你的真实硬件（而非内置默认）。"),
                "how": "tdlas_device action=set_default ..."}
    return {"should_prompt": False, "reason": "packed_and_defaulted",
            "note": f"已打包 {len(setups)} 套且已设默认，无需提醒。"}


def build_next_actions(out):
    """从工具返回**推导**出"AI 接下来必须做什么" —— 纯函数，只读 out，不碰会话/不写盘。

    返回按严重度排序的 list[dict]，每条：{id, severity, action, evidence_fields, values, template}。
    两条供契约测试断言的不变量：
      · 每个 evidence_fields 路径都能在**同一次返回**里解析到（否则 AI 找不到数据）；
      · 存在 block_conclusion 动作时，`interaction.conclusion_allowed` 必须为 False。
    """
    acts = []
    val = out.get("validation") or {}
    overall = val.get("overall")
    title = str(out.get("species") or "").upper() or "该物种"

    def add(_id, sev, action, ev, values=None, template=None):
        if not all(_ev_ok(out, p) for p in ev):        # 数据不在 → 不生成（不指向空气）
            return
        acts.append({"id": _id, "severity": sev, "action": action,
                     "evidence_fields": list(ev), "values": values, "template": template})

    # ★ 仪器打包的**主动提醒**：服务器无法主动发起对话（MCP 没有这个方向），
    #   故把它做成一条随结果下发的动作；_interaction_block 会按"工具+动作"记账，
    #   **同一会话只发一次**，不会每轮叽叨（否则 AI 会忽略它）。
    #   它不阻断结论：仪器未打包不影响数值可用性，只影响空闲度。
    _pk = _packing_state()
    if _pk.get("should_prompt"):
        add("offer_device_packing", _SEV_ADVISORY,
            _pk.get("ask_user") or "提醒用户可打包整机配置",
            [], values={"reason": _pk.get("reason"),
                        "missing_devices": _pk.get("missing_devices"),
                        "setups": _pk.get("setups"),
                        "how": _pk.get("how")},
            template=_pk.get("ask_user"))

    # ① 扫描含暗区：不是措辞问题，而是"这个数不可用"，最高优先级
    _dark = [str(w) for w in (out.get("warnings") or []) if "无激光输出" in str(w)]
    if _dark:
        add("partial_dark_no_conclusion", _SEV_BLOCK,
            "说明本次扫描含无光暗区、2f/1f 已被截断，**不得给出定量结论**",
            ["warnings"], template=_dark[0])
    # ② 物理校验 fail：前提已破
    if overall == "fail":
        _fails = [c.get("check") for c in (val.get("checks") or []) if c.get("status") == "fail"]
        add("fix_validation_fail", _SEV_BLOCK,
            "先处理 validation 的 fail 项；fail 项不得作为可用结果报出",
            ["validation.checks"], values={"fail_checks": _fails},
            template=f"本次校验未通过：{'、'.join(str(f) for f in _fails)}。")
    # ③ 反演结果越界：摩尔分数不在 (0,1] → 结果本身不可用
    _st = out.get("mole_frac_status")
    if _st not in (None, "ok"):
        add("invert_result_unusable", _SEV_BLOCK,
            f"反演结果不可用（mole_frac_status={_st}）：先排除 k 不同源 / 出线性区 / 基线未扣，再报数",
            ["mole_frac_status"], values={"status": _st, "peak_2f1f_in": out.get("peak_2f1f_in")})
    # ④ 缺参数 → **必须是 must_disclose 而不是 block**。理由（实测标定）：
    #    `assumptions` 是"没显式传的一切"（默认工况下 35 项，含全部器件参数），
    #    `clarify.questions` 是 36 问的**题库**（`needed` 只在极端情况才为 False）。若据此
    #    阻断，则**即使显式给全 T/P/x/L，conclusion_allowed 也恒为 false** —— 这个布尔
    #    随即失去信息量、客户端会学会忽略它。项目既有口径也正是"保留默认时须在结论中
    #    明确标注"（见 confirm_note）而非拒绝出结论。真正该阻断的只有"这个数本身不可用"
    #    （扫描含暗区 / 校验 fail / 反演越界，见 ①②③）。
    _qb = out.get("clarify") or {}
    if _qb.get("needed"):
        _qs = _qb.get("questions") or out.get("param_requests") or []
        _names = [str(q.get("param")) for q in _qs if isinstance(q, dict)]
        add("clarify_missing", _SEV_DISCLOSE,
            "用原生结构化提问工具向用户提出这些缺省参数；用户明确说'用默认值'才可跳过，"
            "跳过时必须在结论里标注",
            ["clarify.questions"],
            values={"count": len(_names), "top": _names[:6],
                    "priority_hint": "波段 > 浓度/光程 > T/P > 器件 > 噪声；1 次 1–4 题，超 4 题分批"},
            template=f"有 {len(_names)} 项参数缺省，按优先级前 6：{'、'.join(_names[:6])}。")
    # ⑤ 默认值披露：**工况类逐项报**（直接决定数值），器件类只报个数
    #    （35 项全列出来没法读，AI 也会只挑几条说 —— 等于没报）
    _as = list(out.get("assumptions") or [])
    if _as:
        _cond_keys = [k for k in ("T", "P", "x", "L_cm") if k in _as]
        _cv = _condition_values(out)
        _vals = {k: _cv.get(k) for k in _cond_keys if _cv.get(k) is not None}
        _n_hw = len([k for k in _as if k not in _cond_keys])
        add("disclose_defaults", _SEV_DISCLOSE,
            "向用户说明哪些参数用了默认值（工况类须逐项给出数值），并指出改为实测值的途径",
            ["assumptions"],
            values={"condition_defaults": _vals or None,
                    "condition_params": _cond_keys or None,
                    "hardware_default_count": _n_hw},
            template=(f"本次工况参数 {'、'.join(_cond_keys)} 用了默认值"
                      f"（另有 {_n_hw} 项器件/扫描参数为默认）" if _cond_keys else
                      f"本次有 {len(_as)} 项参数用了默认值（均为器件/扫描设置）"))
    # ⑥ 归一化方法
    if _ev_ok(out, "normalization.method"):
        add("disclose_normalization", _SEV_DISCLOSE,
            "说明本次归一化方法（2f/1f 还是退化为 2f/I0），以及是否做了背景扣除",
            ["normalization.method"],
            values={"method": out["normalization"].get("method"),
                    "background_subtracted": out["normalization"].get("background_subtracted"),
                    "offband_leak_1f": out["normalization"].get("offband_leak_1f")})
    # ⑦ α 语境 / ⑧ etalon 条纹 / ⑨ 激光自动重锚：只在"需要播报"时下发
    if (out.get("alpha_report") or {}).get("needs_report"):
        add("report_alpha_context", _SEV_DISCLOSE,
            "给出 α 峰值时必须同时列出 T / P / step / wingHW / 波数窗口（缺一不可）",
            ["alpha_report.alpha_peak_context"],
            values=out["alpha_report"].get("alpha_peak_context"),
            template=out["alpha_report"].get("report_reason"))
    if (out.get("fringe_report") or {}).get("needs_report"):
        add("report_fringe_impact", _SEV_DISCLOSE,
            "说明 etalon 条纹的 FSR/对比度、是否会在 2f 上伪造吸收、以及该采取的物理措施",
            ["fringe_report.fringe_context"], values=out["fringe_report"].get("fringe_context"),
            template=out["fringe_report"].get("report_reason"))
    if out.get("laser_auto_aligned"):
        _li = out["laser_auto_aligned"]
        add("disclose_laser_realign", _SEV_DISCLOSE,
            "说明本次 wn_ref 是**自动重锚**的理想激光器假设（不是用户手上的器件），"
            "并提示提供实测 wn_ref/dν/dI 或用 tdlas_device 引用真实设备",
            ["laser_auto_aligned"],
            values={"wn_ref_before": _li.get("wn_ref_before"), "wn_ref_auto": _li.get("wn_ref_auto")})
    # ⑩ 保真度缺口：任何定量结论都要说清"哪些没建模"
    _fid = out.get("fidelity") or {}
    if _fid.get("not_implemented"):
        add("disclose_fidelity_gaps", _SEV_DISCLOSE,
            "说明哪些物理效应本次**未建模**（它们可能主导真实误差），并明确点值≠带误差结果",
            ["fidelity.not_implemented"], values={"not_modeled": _fid["not_implemented"]})
    # ⑪ 不确定度：给了浓度就要说清"有没有误差、是什么口径"
    if _ev_ok(out, "mole_frac") and "uncertainty" not in out:
        add("attach_uncertainty", _SEV_DISCLOSE,
            "报出浓度时须说明未附不确定度，或改用 n_repeats>0 得到统计分量",
            ["mole_frac"], values={"mole_frac": out.get("mole_frac"), "n_repeats": 0})
    return sorted(acts, key=lambda a: _SEV_ORDER.get(a["severity"], 9))


def _action_sig(action):
    """动作的"语境指纹"：只取与数值有关的部分，用于判断是否需要重新下发。"""
    payload = {"id": action["id"], "values": action.get("values"),
               "evidence_fields": action.get("evidence_fields")}
    return hashlib.md5(json.dumps(payload, sort_keys=True, ensure_ascii=False,
                                  default=str).encode("utf-8")).hexdigest()[:10]


def _figure_check(png_path, r, multi=None):
    """出图自检：机器检查图片质量，AI 不用读图也能知道有没有问题。

    检查项：
    - file_exists: png 文件存在
    - file_size_kb: 文件大小（<10KB 可能是空图）
    - das_peak_visible: DAS 吸收峰可见（alphaL_cyc 峰值 > 阈值）
    - y_axis_sane: 有效区数据范围占总范围 > 50%（剔除区不撑大纵轴）
    """
    import os
    import numpy as np
    checks = {}
    warnings = []

    # 1. 文件完整性
    checks["file_exists"] = os.path.exists(png_path)
    if checks["file_exists"]:
        size_kb = os.path.getsize(png_path) / 1024
        checks["file_size_kb"] = round(size_kb, 1)
        if size_kb < 10:
            warnings.append(f"图片文件过小（{size_kb:.1f} KB），可能是空图")
    else:
        warnings.append("图片文件不存在")

    # 2. DAS 吸收峰可见性（用 alphaL_cyc）
    try:
        if "alphaL_cyc" in r:
            alpha = np.asarray(r["alphaL_cyc"], dtype=float)
            alpha_peak = float(np.max(alpha))
            checks["alpha_L_peak"] = round(alpha_peak, 6)
            # 阈值：alphaL > 0.001（约 0.1% 透过率凹陷）才算可见
            if alpha_peak < 0.001:
                warnings.append(
                    f"DAS 吸收峰极弱（αL={alpha_peak:.2e}），图上可能看不见——"
                    f"建议加大光程或换更强的线")
    except Exception:
        pass

    # 3. 纵轴合理性（用 S2f_norm_cyc + valid_mask）
    try:
        if "S2f_norm_cyc" in r and "valid_mask" in r:
            s2f = np.asarray(r["S2f_norm_cyc"], dtype=float)
            valid = np.asarray(r["valid_mask"], dtype=bool)
            if len(s2f) > 0 and np.any(valid):
                vmin_total, vmax_total = float(np.min(s2f)), float(np.max(s2f))
                total_range = vmax_total - vmin_total
                if total_range > 0:
                    valid_data = s2f[valid]
                    vmin_valid, vmax_valid = float(np.min(valid_data)), float(np.max(valid_data))
                    valid_range = vmax_valid - vmin_valid
                    ratio = valid_range / total_range
                    checks["valid_range_ratio"] = round(ratio, 3)
                    if ratio < 0.5:
                        warnings.append(
                            f"有效区数据范围仅占总范围 {ratio*100:.1f}%，"
                            f"剔除区可能撑大了纵轴")
    except Exception:
        pass

    # 判定 overall
    if not checks.get("file_exists", False):
        overall = "fail"
    elif len(warnings) > 0:
        overall = "warn"
    else:
        overall = "pass"

    return {
        "overall": overall,
        "checks": checks,
        "warnings": warnings
    }


def _interaction_block(out, tool, session_id="default"):
    """把 build_next_actions 的结果做成**按需下发**的交互契约块。

    返回 (interaction, next_required_actions)。stage 的语义（写死在这里，别再解释成别的）：
      · clarify  —— conclusion_allowed=false：校验 fail / 扫描含暗区 / 反演越界，
                    即"这个数本身不可用"，不得据此给定量结论
      · produced —— 结论可给，但本轮仍有**新**的必须披露项未下发
      · verified —— 结论可给，且本会话该说的都已下发过（本轮无新项）
    ⚠ `verified` 只表示"要求已全部下发过"，**不代表模型已照做**（服务器看不到最终措辞）。
    ⚠ `conclusion_allowed` **故意不把"缺参数未确认"算作阻断**：缺省参数是每个新会话的默认
      状态（默认工况下 assumptions 有 35 项、clarify 题库有 36 问），若据此阻断则该布尔恒为
      false、随即失去信息量。缺参数走 must_disclose（结论可给 + 必须标注），与项目既有口径
      （confirm_note："保留默认时须在结论中明确注明"）一致。
    """
    acts = build_next_actions(out)
    sessions = _load_sessions()
    sess = sessions.get(session_id, {"confirmed": {}, "pending": [], "stage": "clarify"})
    sent = dict(sess.get("interaction_sent") or {})
    fresh, already, changed = [], [], False
    for a in acts:
        # ⚠ 按 **工具+动作** 记账：review 内部会先跑一次 wms，若只按动作 id 记账，
        # 两个工具写同一份 map 会互相抹掉对方的"已下发"记录（实测序列会来回重复下发）。
        key = f"{tool}:{a['id']}"
        sig = _action_sig(a)
        if sent.get(key) == sig:
            already.append(a["id"])
        else:
            fresh.append(a)
            sent[key] = sig
            changed = True
    keep = {f"{tool}:{a['id']}" for a in acts}            # 剪掉已不适用的条目
    pruned = {k: v for k, v in sent.items() if k in keep}
    if pruned != sent:
        changed = True
    if changed:
        sess["interaction_sent"] = pruned
        sessions[session_id] = sess
        _save_sessions(sessions)

    val = out.get("validation") or {}
    physics_ok = val.get("overall") != "fail"
    blockers = [a for a in acts if a["severity"] == _SEV_BLOCK]      # 只剩"这个数不可用"类
    result_usable = not blockers
    allowed = bool(physics_ok and result_usable)
    has_result = ("results" in out) or ("validation" in out) or _ev_ok(out, "mole_frac")
    if not allowed:
        stage = "clarify"
    elif has_result and not fresh:
        stage = "verified"
    else:
        stage = "produced"
    reason = None
    if not allowed:
        _parts = []
        if not physics_ok:
            _fails = [c.get("check") for c in (val.get("checks") or []) if c.get("status") == "fail"]
            _parts.append("物理校验未通过（validation.overall=fail）"
                          + (f"：{'、'.join(str(f) for f in _fails)}" if _fails else ""))
        _parts += [a["action"] for a in blockers]
        reason = "；".join(_parts) or "结果不可用"
    interaction = {"tool": tool, "stage": stage,
                   "physics_ok": bool(physics_ok), "result_usable": bool(result_usable),
                   "conclusion_allowed": allowed, "blocking_reason": reason,
                   "disclosure_pending": already,
                   "rule": "conclusion_allowed=false（见 blocking_reason）时不得给出定量结论；"
                           "next_required_actions 里 must_disclose 项须在同一回答里说明；"
                           "disclosure_pending 是本会话已下发过的项（不重复发大 payload，仍须覆盖）。"}
    return interaction, fresh


# ══════════════════ 设备库（仪器统一管理）══════════════════
# 用户实验仪器基本固定，把激光器/探测器/采集卡/光学元件命名存下来，
# 仿真时用 setup= / laser= / pd= / daq= / optics= 引用，省去每次手填硬件参数。
_DEVICE_FILE = _ROOT / ".tdlas_devices.json"

# 各类设备允许保存的参数键（对应仿真里的硬件参数）
_DEVICE_TYPES = {
    "laser": ["eta_VI", "dnu_dI", "d2nu_dI2", "wn_ref", "i_ref", "i_th", "eta_IP",
              "am_i0", "am_i2", "am_psi1", "am_psi2"],
    "pd": ["resp", "gain", "bw", "rin"],
    "daq": ["fs", "n_samples", "adc_bits", "v_range"],
    "optics": ["throughput"],
}
_DEVICE_TYPE_CN = {"laser": "激光器", "pd": "探测器", "daq": "采集卡", "optics": "光学元件"}

# 设备参数合法域：(下限, 上限, 是否含下限, 特殊约束)。None = 该端不设限。
# 为什么必须校验：这些键直接进仿真链路 —— bw→噪声带宽（负值让 np.sqrt 出 nan）、
# gain→跨阻（除零/负增益）、adc_bits/v_range→量化（非法值产生虚假分辨率）、
# fs→采样率、throughput→光通量。在"保存"这个最早入口拦下，远比等仿真出 nan 再回溯根因便宜。
_DEVICE_RULES = {
    "eta_VI": (0.0, None, False, None),
    "dnu_dI": (None, None, False, "nonzero"),
    "d2nu_dI2": (None, None, False, None),
    "wn_ref": (0.0, None, False, None),
    "i_ref": (0.0, None, True, None),
    "i_th": (0.0, None, True, None),
    "eta_IP": (0.0, None, False, None),
    "am_i0": (None, None, False, None),
    "am_i2": (None, None, False, None),
    "am_psi1": (None, None, False, None),
    "am_psi2": (None, None, False, None),
    "resp": (0.0, None, False, None),
    "gain": (0.0, None, False, None),
    "bw": (0.0, None, False, None),
    "rin": (0.0, None, True, None),
    "fs": (0.0, None, False, None),
    "n_samples": (1.0, None, True, "int"),
    "adc_bits": (4.0, 32.0, True, "int"),
    "v_range": (0.0, None, False, None),
    "throughput": (0.0, 1.0, False, None),
}


def _validate_device_entry(entry):
    """校验一组设备参数；返回错误说明（None = 通过）。"""
    for k, v in entry.items():
        rule = _DEVICE_RULES.get(k)
        try:
            fv = float(v)
        except (TypeError, ValueError):
            return f"参数 {k}={v!r} 不是数值"
        if not math.isfinite(fv):
            return f"参数 {k}={v!r} 不是有限数值"
        if rule is None:
            continue
        lo, hi, lo_incl, kind = rule
        if kind == "int" and abs(fv - round(fv)) > 1e-9:
            return f"参数 {k}={fv:g} 必须为整数"
        if kind == "nonzero" and fv == 0.0:
            return f"参数 {k} 不能为 0（否则波数不随注入电流变化，无法反算扫描中心）"
        if lo is not None and (fv < lo or (fv == lo and not lo_incl)):
            return f"参数 {k}={fv:g} 必须 {'≥' if lo_incl else '>'} {lo:g}"
        if hi is not None and fv > hi:
            return f"参数 {k}={fv:g} 必须 ≤ {hi:g}"
    return None


def _load_devices():
    if _DEVICE_FILE.exists():
        try:
            return json.loads(_DEVICE_FILE.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return {}
    return {}


def _save_devices(devices):
    _atomic_write_text(_DEVICE_FILE, json.dumps(devices, ensure_ascii=False, indent=2))


def _resolve_devices(setup=None, laser=None, pd=None, daq=None, optics=None):
    """解析设备引用 → 返回要合并进仿真 inst 的硬件参数字典。

    某类设备的选取优先级：显式单设备引用 > setup 整机里的对应项 > 默认设备。
    返回的参数字典在调用方用 setdefault 合并（本次显式给的具体参数仍最优先）。
    """
    dev = _load_devices()
    # ★ 不传 setup 时自动用默认 setup
    if setup is None:
        setup = dev.get("default", {}).get("setup")
    pick = {"laser": laser, "pd": pd, "daq": daq, "optics": optics}
    if setup:
        st = dev.get("setup", {}).get(str(setup))
        if st is None:
            _exist = sorted((dev.get("setup") or {}).keys())
            raise ValueError(
                f"整机配置 {setup!r} 不存在。已有配置：{_exist or '（无）'}。"
                f"如需新建，请告诉用户你需要哪几台设备的**型号或规格书**，"
                f"用 tdlas_device save 建库后再 save_setup 打包（source 必须如实填）。")
        for k in pick:
            if pick[k] is None and st.get(k):
                pick[k] = st[k]
    dflt = dev.get("default", {})
    for k in pick:
        if pick[k] is None:
            pick[k] = dflt.get(k)
    resolved = {}
    for k in ("laser", "pd", "daq", "optics"):
        name = pick[k]
        if not name:
            continue
        params = dev.get(k, {}).get(str(name))
        if params is None:
            _keys = _DEVICE_SALIENT_KEYS.get(k, [])
            _need = "、".join(
                f"{(PARAM_ACQ_GUIDE.get(kk) or (kk, '-', ''))[0]}({(PARAM_ACQ_GUIDE.get(kk) or (kk, '-', ''))[1]})"
                for kk in _keys)
            _exist = sorted((dev.get(k) or {}).keys())
            raise ValueError(
                f"{_DEVICE_TYPE_CN[k]}设备 {name!r} 不存在。已有：{_exist or '（无）'}。"
                f"若要新建该设备，请向用户索取**型号或规格书**（需：{_need}），"
                f"然后用 tdlas_device save device_type={k} name=... source=datasheet|user|ai。")
        # ★ 先把 provenance 元数据与**参数字段**分开：前者是字符串（source），
        #   若混进参数域会被 _validate_device_entry 当成"非数值参数"而报错。
        _PROV_FIELDS = ("source", "source_note", "fields", "updated")
        entry = {kk: vv for kk, vv in params.items()
                 if vv is not None and kk not in _PROV_FIELDS}
        bad = _validate_device_entry(entry)          # 兜住历史/手改过的非法条目
        if bad:
            raise ValueError(f"已保存的{_DEVICE_TYPE_CN[k]}设备 {name!r} 参数非法：{bad}；"
                             f"请用 tdlas_device save 重新保存修正")
        resolved.update({k: v for k, v in entry.items()
                         if k not in ("source", "source_note", "fields", "updated")})
        # 回显来源：让 AI 能如实说出"这些参数来自哪里"
        _meta_src = {k: params.get(k) for k in ("source", "source_note", "updated")
                     if params.get(k) is not None}
        # 即使该条目没有 source（旧条目），也必须如实报出"来源未记录"——
        # 否则 AI 会默认这些硬件参数是“规格书里的”。空白不等于可信。
        resolved.setdefault("__provenance__", {})[k] = {
            "device": str(name),
            "source": _meta_src.get("source", "unspecified"),
            **({"source_note": _meta_src["source_note"]} if _meta_src.get("source_note") else {}),
            **({"updated": _meta_src["updated"]} if _meta_src.get("updated") else {}),
            **({} if _meta_src.get("source") else
               {"warning": "本条目建立于来源追踪之前，未记录 source；"
                           "建议重存以补上（如规格书则 source=datasheet）"}),
        }
    # ★ 从 setup 取出工况参数
    if setup and st.get("conditions"):
        resolved["__conditions__"] = dict(st["conditions"])
    return resolved


#: 各类设备的**显著标识键**：设备名至少要命中一个，否则不可能被区分。
#: 为什么要校验：设备名是用户以后**只能看到的东西**（引用时只传名字）。
#: 叫 "A套"/"测试"/"dev1" 的条目一旦积累，用户无法判断哪个能用、
#: 哪个是上周调过的。这是对用户不友好，也让 AI 无法选对设备。
_DEVICE_SALIENT_KEYS = {
    "laser":  ["dnu_dI", "eta_VI", "wn_ref", "i_ref", "i_th", "eta_IP"],
    "pd":     ["resp", "gain", "bw"],
    "daq":    ["fs", "adc_bits", "v_range"],
    "optics": ["throughput"],
}
#: 无意义的名字（单字符、纯编号、通用词）——直接拒绝
_MEANINGLESS_NAME_RE = None


def _name_is_distinctive(name, device_type=None):
    """判断设备/整机名是否**足够显著**。

    规则（任一即可）：① 含型号类信息（字母+数字混合，如 NI_USB-6211、DFB-1653）；
    ② 含长度/波长类信息（如 3373nm、3.3um、1653）；③ 含中文型号词（如 实验室甲、多通池）。
    **拒绝**：纯编号（a1/dev1/test）、单字符、以及"测试/临时/新建/copy"这类通用词。
    返回 (ok, reason)。
    """
    nm = str(name or "").strip()
    if len(nm) < 3:
        return False, f"名字过短（{nm!r}），无法区分"
    low = nm.lower()
    for bad in ("test", "tmp", "temp", "测试", "临时", "新建", "copy", "副本", "demo", "abc"):
        if low == bad or low.startswith(bad + "_") or low.startswith(bad + "-"):
            return False, f"名字含通用词 {bad!r}，无法区分"
    if __import__("re").fullmatch(r"[a-z]?\d{1,3}", low):
        return False, f"名字 {nm!r} 是纯编号，无法区分"
    has_digit = any(c.isdigit() for c in nm)
    has_alpha = any(c.isalpha() for c in nm)
    has_cjk = any("一" <= c <= "鿿" for c in nm)
    if (has_digit and has_alpha) or has_cjk:
        return True, ""
    return False, (f"名字 {nm!r} 不含型号/波长等可区分信息；"
                   f"建议如 'nanoplus_3373nm_SN5712'、'NI_USB-6211'、'多通池_10m'")


def _suggest_setup_name(dev, laser=None, pd=None, daq=None, optics=None):
    """当用户没给合适名字时，**由已有设备名自动拼一个显著名**。

    这是"AI 负责命名、用户只给素材"的具体落地：用户给了四台设备，
    但不一定想得出合适的整机名——那就用设备名的**显著片段**拼出来。
    """
    parts = []
    for k, v in (("laser", laser), ("pd", pd), ("daq", daq), ("optics", optics)):
        if not v:
            continue
        nm = str(v)
        # 取最能区分的一段：优先含数字的词（型号/波长），否则取前 12 字符
        toks = [t for t in __import__("re").split(r"[_\-\s]+", nm) if t]
        pick = next((t for t in toks if any(c.isdigit() for c in t)), None)
        parts.append(pick or nm[:12])
    return "_".join(parts) if parts else ""


def _device_setup_guide(dev, laser=None, pd=None, daq=None, optics=None):
    """告诉 AI：还缺哪些设备、每台需要哪些参数、该向用户问什么。

    这是本项目"用户只上传 AI 要的东西、AI 负责构建与提问"的接口：
    不要只报"设备不存在，请先 save"（用户不知道要给什么），
    而要给出**缺什么 + 每项的中文名/单位 + 去哪里查 + 实在没有怎么办**。
    """
    missing = []
    for k, v in (("laser", laser), ("pd", pd), ("daq", daq), ("optics", optics)):
        if v and v not in (dev.get(k) or {}):
            keys = _DEVICE_SALIENT_KEYS.get(k, [])
            fields = []
            for kk in keys:
                g = PARAM_ACQ_GUIDE.get(kk)
                fields.append({"key": kk, "cn": g[0] if g else kk,
                               "unit": g[1] if g else "-", "why": g[2] if g else ""})
            missing.append({
                "device_type": k, "cn": _DEVICE_TYPE_CN[k], "name_given": str(v),
                "fields_needed": fields,
                "ask_user": (f"请提供{_DEVICE_TYPE_CN[k]} **{v}** 的型号或规格书"
                             f"（需：" + "、".join(f["cn"] + f"({f['unit']})" for f in fields) + "）"),
                "if_no_datasheet": "无规格书时：① 用型号联网检索；"
                                   "② 引导现场标定；③ 保留默认并在结论中标注。",
            })
    avail = {k: sorted((dev.get(k) or {}).keys()) for k in ("laser", "pd", "daq", "optics")}
    return {"missing": missing, "available": avail,
            "ai_instruction": "请按 missing[].ask_user 向用户索取；"
                              "拿到型号/规格书后用 tdlas_device save 建库"
                              "（**source 必须如实填**：规格书=datasheet、AI 推断=ai），"
                              "然后用 save_setup 打包成整机。用户不需要知道参数名。"}


def _device_provenance(setup=None, laser=None, pd=None, daq=None, optics=None):
    """只取设备引用的**来源元数据**（不取参数），供结果回显。

    为什么单独出一个函数：`_resolve_devices` 的返回值会被 setdefault 合并进引擎参数，
    若在里面塞一个 `__provenance__` 键，它会被当成真实参数传给引擎（已踩过这个坑）。
    """
    try:
        raw = _resolve_devices(setup, laser, pd, daq, optics)
        prov = raw.pop("__provenance__", None)
        return prov
    except Exception:                                      # noqa: BLE001
        return None


def _get_session(session_id="default"):
    return _load_sessions().get(session_id,
                                {"confirmed": {}, "pending": [], "stage": "clarify"})


def _resolve_conditions(T, P, x, L_cm, session_id="default", x_default=1e-3, L_default=50.0):
    """工况参数优先级：本次显式给 > 会话已确认 > 默认。

    会话里存的是 T/P/x/L_cm（与 tdlas_wms_instrument 一致）。返回 (T, P, x, L_cm, assumed)；
    assumed 为「既非本次显式给、也非会话确认、因此用了默认值」的键列表，供返回 assumptions 字段。

    ⚠ 默认值必须与仪器链（_SCENE_DEFAULTS）**同一套**：曾出现分析链 P=1.0 / L_cm=30 而
    仪器链 P=1.01325 / L_cm=50（物种推荐还有 100），同一会话里两条链对"未给光程"给出
    相差 3.3 倍的 L，两个数看起来都"合理"却互相矛盾（见 docs/VALIDATION.md §6）。
    """
    confirmed = _get_session(session_id).get("confirmed", {})
    res, assumed = {}, []
    for k, v, dflt in (("T", T, _SCENE_DEFAULTS["T"]), ("P", P, _SCENE_DEFAULTS["P"]),
                       ("x", x, x_default), ("L_cm", L_cm, L_default)):
        if v is not None:
            res[k] = v
        elif k in confirmed:
            res[k] = confirmed[k]
        else:
            res[k] = dflt
            assumed.append(k)
    return res["T"], res["P"], res["x"], res["L_cm"], assumed


def _species_defaults(species):
    """按物种返回推荐浓度 x_typ 与光程 L_cm（来自 SPECIES_PROFILES，缺省回退 1e-3 / 50 cm）。

    强吸收分子（如 CH4@3.3μm）默认浓度应更低，否则 αL 过大进入饱和区、2f/1f 非线性。
    回退值取 _SCENE_DEFAULTS 的光程，保证与仪器链一致（见 _resolve_conditions 的说明）。
    """
    prof = SPECIES_PROFILES.get(str(species or "").strip().upper(), {})
    return prof.get("x_typ", 1e-3), prof.get("L_cm", _SCENE_DEFAULTS["L_cm"])


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

# 参数索取指南：缺省参数的索取优先级 ——
# ① 用户提供的实测标定值 → ② 用户提供的器件型号（由 AI 检索规格书）
# → ③ 引导用户现场测量（如"驱动电压变化 ΔV → 波数变化 Δν"）
# → ④ 保留内置默认值并在返回中明确标注（assumptions）
PARAM_ACQ_GUIDE = {
    "species": ("待测气体", "—", "HITRAN 分子式（CH4 / H2O / CO2 / CO / C2H2 …）"),
    "wn_center": ("目标波数", "cm⁻¹", "要测的吸收线中心；决定激光调谐到哪条线"),
    "scan_span_cm": ("三角波扫描半宽", "cm⁻¹", "扫描覆盖的波数半宽；amp_V = scan_span_cm/(η_VI·|dν/dI|)，不填用默认 1.5"),
    "amp_V": ("三角波幅值", "V", "**通常无需手填**：由 wn_center + scan_span_cm 自动反算；仅需固定电压时覆盖"),
    "offset_V": ("三角波偏置", "V", "**通常无需手填**：由 wn_center 经电压—波数关系自动反算"),
    "freq_Hz": ("三角波频率", "Hz", "扫描速率；与采样率共同决定每周期采样点数"),
    "phase_deg": ("三角波相位", "°", "起始相位（默认中心对称、先上升后下降）"),
    "eta_VI": ("驱动器跨导 η_VI", "mA/V", "电压—电流转换，取决于驱动器电路"),
    "dnu_dI": ("激光器调谐系数 dν/dI", "cm⁻¹/mA", "波数标定核心；给出激光器型号可查规格书"),
    "eta_IP": ("功率斜率效率", "mW/mA", "决定光功率量级，进而决定 PD 电压与 ADC 量程占用"),
    "wn_ref": ("参考波数", "cm⁻¹", "调谐基准点（通常取规格书中心波长）"),
    "i_ref": ("参考电流", "mA", "与参考波数配对的工作点电流"),
    "i_th": ("阈值电流", "mA", "低于此电流无激光输出"),
    "fs": ("采集卡采样率", "Hz", "常见 16-bit DAQ 上限 250 kS/s"),
    "adc_bits": ("ADC 位数", "bit", "决定量化噪声与动态范围（常见 USB DAQ：16-bit）"),
    "v_range": ("ADC 输入量程", "V", "决定满量程与饱和阈值"),
    "resp": ("PD 响应度 R", "A/W", "光电转换效率（InGaAs 典型 0.9 A/W）"),
    "gain": ("PD 跨阻增益 G", "V/A", "决定 PD 输出电压；过高会导致 ADC 饱和"),
    "bw": ("PD 带宽", "Hz", "与噪声带宽、可解调的最高调制频率相关"),
    "rin": ("激光相对强度噪声 RIN", "1/√Hz", "常为 TDLAS 系统的主导噪声源"),
    "throughput": ("光学元件总透过率", "—", "窗片 / 镜片 / 光纤耦合损耗"),
    "mod_freq_Hz": ("正弦调制频率 fm", "Hz", "把信号搬到高频以规避 1/f 噪声与激光 RIN"),
    "mod_amp_V": ("正弦调制幅值", "V", "决定调制系数 m=a/HWHM；m≈2.2 时 2f 峰值最大"),
    "m_opt": ("目标调制系数 m", "—", "2f 灵敏度最优的调制深度/线宽比，经典值 2.2"),
    "lockin_avg": ("锁相平均周期数", "—", "低通平均的调制周期数，保持 1（>1 会模糊线形且不降残留）"),
    "lockin_stages": ("锁相低通级联级数", "—", "矩形窗频响是 sinc，级联 2 级(sinc²)把非吸收区 2f 残留 4.1%→1.6%"),
    "drift_frac": ("1/f 慢漂移幅度", "相对光强", "激光功率慢漂移；DAS 受害、WMS 锁相抑制；默认 0.005"),
    "flicker_frac": ("1/f 粉红噪声幅度", "相对光强", "频域 1/√f 噪声；默认 0.01"),
    "fringe_n": ("etalon 腔折射率", "—", "etalon 条纹：FSR = 1/(2nd)；空气隙 1.0、玻璃≈1.5"),
    "fringe_d_cm": ("etalon 平行面间距", "cm", "etalon 条纹：决定 FSR；10 mm→1.0、5 mm→0.5"),
    "fringe_R": ("etalon 单面反射率", "—", "etalon 条纹：峰-峰对比度≈4R/(1−R)²；未镀膜玻璃 0.04、AR≈0.005"),
}

# ★ 澄清问题模板：把缺省参数转成"带选项的自然语言问句"，供 AI 主动向用户提问。
CLARIFY_QUESTIONS = {
    "x": {"question": "待测气体浓度（摩尔分数）量级是多少？",
          "options": ["痕量 ~1e-6", "低 ~1e-4", "中 ~1e-2", "不确定，帮我推荐"],
          "why": "决定吸收强弱 αL，弱吸收(1e-5~0.1)下 DAS/2f 才线性"},
    "L_cm": {"question": "吸收池光程是多少？",
             "options": ["10 cm", "30 cm", "100 cm", "多通池（几十米）", "不确定"],
             "why": "与浓度共同决定 αL"},
    "T": {"question": "气体温度？",
          "options": ["室温 296 K", "其他（请填数值）"],
          "why": "决定线强与多普勒线宽"},
    "P": {"question": "气体压力？",
          "options": ["常压 1 atm", "减压（请填数值）", "其他"],
          "why": "决定碰撞展宽（Lorentz HWHM）"},
    "mod_freq_Hz": {"question": "正弦调制频率 fm 用多少？",
                    "options": ["默认 30 kHz", "其他（请填）"],
                    "why": "信号搬到 fm 以避开 1/f 噪声"},
    "drift_frac": {"question": "是否加入 1/f 慢漂移噪声？",
                   "options": ["不加（理想仿真）", "加 0.5%", "加 1%", "其他"],
                   "why": "模拟激光功率慢漂移；DAS 受害、WMS 抑制"},
    "flicker_frac": {"question": "是否加入 1/f 粉红噪声？",
                     "options": ["不加（理想仿真）", "加 1%", "加 3%", "其他"],
                     "why": "模拟 1/f 光强噪声"},
}


def generate_next_suggestions(results, conditions):
    """根据仿真结果生成下一步优化建议（小白友好）。

    results: 仿真结果字典（含 S2f_norm_peak, noise_breakdown_mV, sensitivity_k）
    conditions: 工况（T, P, x, L_cm）
    """
    suggestions = []

    s2f_peak = results.get("S2f_norm_peak")
    noise = results.get("noise_breakdown_mV", {})
    L = conditions.get("L_cm", 10)
    x = conditions.get("x", 1e-4)

    if s2f_peak is not None:
        # 1. 信号太小 → 建议加光程
        if s2f_peak < 1e-4:
            suggestions.append({
                "action": "increase_path_length",
                "message": f"2f 信号较小（{s2f_peak:.2e}），建议加长光程 L。当前 L={L} cm，加到 {L*10:.0f} cm 可提升信号 10 倍。",
                "expected_gain": "信号 ×10，LOD ÷10"
            })

        # 2. 信号饱和 → 建议减浓度
        if s2f_peak > 0.1:
            suggestions.append({
                "action": "decrease_concentration",
                "message": f"2f 信号过大（{s2f_peak:.2e}），可能已饱和。建议降低浓度。当前 x={x:.2e}，可降到 {x/10:.2e}。",
                "expected_gain": "避免非线性失真"
            })

    # 3. 噪声分解建议
    if noise:
        max_noise = max(noise.values()) if noise else 0
        if max_noise > 0:
            dominant = max(noise, key=noise.get)
            if dominant == "rin_white":
                suggestions.append({
                    "action": "use_2f_over_1f_normalization",
                    "message": f"主要噪声是 RIN 白噪声（{max_noise:.3f} mV）。建议做 2f/1f 归一化，可抵消光强波动。",
                    "expected_gain": "RIN 噪声被归一化抵消"
                })
            elif dominant == "shot":
                suggestions.append({
                    "action": "increase_power",
                    "message": f"主要噪声是散粒噪声（{max_noise:.3f} mV）。增加激光功率可提升 SNR。",
                    "expected_gain": "SNR ∝ √功率"
                })

    # 4. 默认建议（如果上面都没触发）
    if not suggestions:
        suggestions.append({
            "action": "try_other_concentrations",
            "message": f"当前工况正常。可以试试不同浓度，看线性区间（建议 5 个浓度点）。",
            "expected_gain": "得到标定曲线 R²"
        })

    return suggestions


def build_clarify_questions(species, wn_center, assumed, used_values, beginner_mode=True):
    """把缺省参数转成结构化澄清问题（含选项），供 AI 主动向用户提问。

    beginner_mode=True 时只返回最关键的 3-5 个问题（小白友好），
    其他参数用默认值即可，不打扰用户。
    """
    questions = []
    # 波段澄清（优先问，因为影响后续一切）
    prof = SPECIES_PROFILES.get(str(species).strip().upper(), {})
    if prof.get("bands"):
        q = {"param": "波段", "question": f"{species} 要测哪个波段？",
             "options": [f"{nm}（{wn} cm⁻¹）" for nm, wn in prof["bands"]],
             "why": "不同波段线强/线密度不同", "current": wn_center,
             "level": "core"}
        questions.append(q)

    # 核心参数（必问，小白模式下只问这些）
    core_params = {"x", "L"}  # 浓度、光程
    # 重要参数（可选问）
    important_params = {"T", "P", "a"}  # 温度、压力、调制深度

    for k in assumed:
        tpl = CLARIFY_QUESTIONS.get(k)
        if k in core_params:
            level = "core"
        elif k in important_params:
            level = "important"
        else:
            level = "advanced"

        if tpl:
            q = {"param": k, "question": tpl["question"],
                 "options": tpl["options"], "why": tpl["why"],
                 "current_default": used_values.get(k), "level": level}
        else:
            cn, unit, why = PARAM_ACQ_GUIDE.get(k, (k, "—", ""))
            q = {"param": k, "中文名": cn, "单位": unit,
                 "question": f"请确认 {cn}（{unit}），当前用默认 {used_values.get(k)}",
                 "options": [], "why": why,
                 "current_default": used_values.get(k), "level": level}
        questions.append(q)

    # 小白模式：只返回核心 + 重要，隐藏高级
    if beginner_mode:
        questions = [q for q in questions if q.get("level") in ("core", "important")]

    return questions


# ★ 物种推荐工况（自适应）：未给工况参数时，按物种/波段自动推荐，而非死板用默认值。
SPECIES_PROFILES = {
    "CH4": {"bands": [("3.3 μm", 3010.0), ("1.65 μm", 6046.9)],
            "x_typ": 1e-4, "L_cm": 100.0, "T": 296.0, "P": 1.0,
            "note": "甲烷近红外 6046.9(2ν3 带 R(3)线，较孤立)、中红外 3010(ν3 带强吸收)"},
    "C2H6": {"bands": [("3.4 μm ν7 带", 2964.5), ("1.7 μm", 5920.0)],
             "x_typ": 1e-4, "L_cm": 100.0, "T": 296.0, "P": 1.0,
             "note": "2964.5 附近 Q 支密集(159 线)，需 L 较小避免 αL 过大"},
    "H2O": {"bands": [("1.39 μm", 7185.6)],
            "x_typ": 0.02, "L_cm": 30.0, "T": 296.0, "P": 1.0,
            "note": "7185.6 为较孤立单线，常用于锁相/谐波标定"},
    "CO": {"bands": [("2.33 μm", 4285.0)],
           "x_typ": 1e-4, "L_cm": 100.0, "T": 296.0, "P": 1.0,
           "note": "基频 R 支，线孤立、线强表成熟"},
    "CO2": {"bands": [("2.0 μm", 5003.0)],
            "x_typ": 5e-4, "L_cm": 100.0, "T": 296.0, "P": 1.0,
            "note": "2.0 μm 组合带"},
    "NO": {"bands": [("5.26 μm", 1900.0)],
           "x_typ": 1e-4, "L_cm": 50.0, "T": 296.0, "P": 1.0,
           "note": "基频带"},
}


def adaptive_condition(species, wn_center=None):
    """工况自适应：未给工况参数时，返回该物种的推荐波段/典型浓度/光程 + **该波段的激光可达性**。

    为什么必须带可达性：推荐表横跨 1900–7185 cm⁻¹，而默认激光只是一支 3.36 μm DFB
    （可达 2964.7–2975.3 cm⁻¹）。旧版只报"推荐 7185.6"，AI 照此调用必然报 offset_V 越界，
    用户会以为是自己选错了线。现在每个波段都附 `可达` 与 `laser_params_needed`，
    AI 可以先告诉用户"该波段需要换激光器"，或直接把这组参数传进仿真（也可依赖
    auto_laser=True 的自动重锚）。
    """
    sp = str(species).strip().upper()
    p = SPECIES_PROFILES.get(sp, {})
    dflt = dict(ts.LASER_DEFAULTS)
    bands_detail = []
    for label, wn in p.get("bands", []):
        r = ts.laser_reach(wn, eta_VI=dflt["eta_VI"], dnu_dI=dflt["dnu_dI"], i_ref=dflt["i_ref"],
                           wn_ref=dflt["wn_ref"], scan_span_cm=1.5, i_th=dflt.get("i_th", 30.0))
        item = {"波段": label, "wn_center_cm-1": wn, "默认激光可达": bool(r["reachable"])}
        if not r["reachable"]:
            item["需要"] = f"wn_ref≈{r['wn_ref_needed']:.6g} cm⁻¹ 的激光器（其余参数可沿用默认）"
            item["laser_params_needed"] = {"wn_ref": round(float(r["wn_ref_needed"]), 6)}
            item["说明"] = ("默认激光（wn_ref=%.6g）覆盖不到该波段；调用 tdlas_wms_instrument 时"
                            "auto_laser=True（默认）会自动重锚 wn_ref 并在 warnings 中标注，"
                            "结论中必须向用户说明这不是真实硬件。" % dflt["wn_ref"])
        bands_detail.append(item)
    rec = {"species": sp,
           "推荐波段": p.get("bands", []),
           "波段可达性": bands_detail,
           "推荐浓度 x": p.get("x_typ"),
           "推荐光程 L_cm": p.get("L_cm"),
           "推荐 T": p.get("T"), "推荐 P": p.get("P"),
           "说明": p.get("note", "无内置资料，请提供工况"),
           "默认激光": {k: dflt[k] for k in ("wn_ref", "dnu_dI", "eta_VI", "i_ref")},
           "提示": "推荐波段未必落在默认激光的调谐范围内；给用户推荐前先看『波段可达性』。"}
    return rec


# ★ AI 主动指导协议：本 MCP 面向**实验新手**，AI 必须主动引导而非被动等参数。
# 每次涉及真实器件的仿真，按此顺序与用户交互（用专业术语，但首次出现给白话解释）。
AI_INTERACTION_GUIDE = {
    "role": "你是 TDLAS 实验设计助手，服务对象多为实验新手。职责是**主动指导**，不是被动等参数。"
            "凡信息不足必须主动索取，绝不能默默用默认值出结果。",
    "default_tool": {
        "兜底": "不确定该用哪个工具时，默认走 `tdlas_wms_instrument`（最完整、最接近真实实验）。",
        "快速估算": "用户说『快速算』『理论值』『扫一下参数』→ 用 `tdlas_simulate`（解析版，秒出结果）。",
        "只看 DAS": "用户明确说『DAS』『直接吸收』→ 用 `tdlas_das_instrument`。",
        "不要默认走 simulate": "simulate 不模拟仪器链路（DAQ/PD/锁相），只适合快速估算，不适合出完整实验图。",
    },
    "priority": [
        "① 先要实测标定值（最可靠）；",
        "② 用户给不出值、但能给**器件型号** → AI **联网检索**该型号官方规格书提取参数（见 datasheet_lookup）；",
        "③ 型号也没有 → 引导用户**现场标定**（例：改变驱动电压 ΔV，记录波数变化 Δν，得 dν/dV）；",
        "④ 以上都做不到 → 用内置默认值，但**必须在结论中显式标注**「以下参数用了默认值 X」。",
    ],
    "workflow": [
        "第 1 步｜确认场景：测什么分子、什么波段、什么工况（T/P/浓度量级/光程）。"
        "用户不确定时主动给推荐（CH4 → 3.3 μm 或 1.65 μm；CO → 2.3 μm；CO2 → 2.0 μm）；"
        "**推荐前先看 adaptive_condition 返回的『波段可达性』**——默认激光只覆盖 2964.7–2975.3 cm⁻¹，"
        "其它波段需要另配激光器：要么把该波段的 laser_params_needed 一并告诉用户（让他知道要换器件），"
        "要么依赖 auto_laser=True 的自动重锚（此时必须按 must_disclose⑦ 说明这属理想假设、非真实硬件）。",
        "第 2 步｜分组索取参数，每次只问一组，并说明该参数控制什么物理量（见 PARAM_ACQ_GUIDE 的 why）。",
        "第 3 步｜拿到结果后做**合理性体检**并主动告知：αL 是否在弱吸收区、ADC 是否饱和、"
        "噪声主导来源（散粒/热/RIN）、检测限量级、调制系数 m 是否接近 2.2。",
        "第 4 步｜若返回值中 warnings / param_requests 非空，**先向用户澄清再解读结论**，"
        "不要拿不合理参数直接下结论。",
    ],
    # ★ 正确 SOP（标准操作流程）——只讲怎么做对，不讲"坑"。AI 必须严格按此 SOP 输出结论。
    "sop": [
        {"step": 1, "name": "技术选型 + 选定孤立单线",
         "rule": "先按谱线密度选技术：窗口内线数 ≤10 → WMS（2f 呈标准双峰、免标定、抗 RIN）；"
                 "线数多（密集谱区）→ 2f 会把每根线都放大成独立峰、看似杂乱，此时 DAS 的直接吸收包络更直观，"
                 "不宜用 WMS 展示标准谐波。WMS 定量/展示标准谐波一律选孤立单线，"
                 "示例：H2O 7185.596、CO 4260.06、N2O 1278.45。线数看 n_lines_in_window。",
         "pass": "已按线密度选对技术 + 用户给定/AI 查到孤立线 → 进 step 2"},
        {"step": 2, "name": "确认激光波数轴覆盖目标线",
         "rule": "若自定义链路，确保 wn_ref 与 wn_center 对齐；本工具已自动处理，"
                 "校验：返回的 scan_window_cm-1 必须包含 wn_center。",
         "pass": "scan_window 包含 wn_center → 进 step 3"},
        {"step": 3, "name": "调用 tdlas_wms_instrument 跑链路",
         "rule": "用默认参数即可（fm=30 kHz, fscan=100 Hz, 自动优化 m=2.2, "
                 "2 级级联低通, edge=rising, trim_frac=0.12）。"
                 "图统一为 5 层：驱动电压 → DAS → 1f → 2f → 归一化 2f（自动选方法）。",
         "pass": "返回无错误 + warnings 非空（含工况说明）→ 进 step 4"},
        {"step": 4, "name": "读 normalization.method 并据此解读",
         "rule": "2f/1f 有效时：使用 2f/1f 读数；1f 失效（1f 过零 或 非吸收区泄漏>30%）："
                 "使用 2f/I0（I0=PD 非吸收区光强均值）读数。**严格按 normalization.method 给出的方法写结论，不要混用**。",
         "pass": "已用同一归一化方法贯穿所有波段读数 → 进 step 5"},
        {"step": 5, "name": "三件校验（形态类判据视工况打折）",
         "rule": "① DAS 提取峰与 HITRAN 理论 αL 一致（偏差 <2%）；"
                 "② 2f 峰位 Δν ≈ ±0.7a（孤立单线下应见标准双峰结构）；"
                 "③ 非吸收区残留/峰 < 2%。"
                 "⚠ **默认工况本身通常就有 1–2 条形态类提醒**（如 CH4@2968.5 密集谱区："
                 "αL=0.133 偏大 + 221 条线非孤立），这属正常，不是失败；"
                 "密集谱区 ② 本就不该按孤立线的标准双峰要求。",
         "pass": "三项都通过 → 物理结论可信；任何一项不通过 → 停止解读，回 step 1 排查（形态类先看线密度）"},
        {"step": 6, "name": "自检 + 二次审核（必须）",
         "rule": "每次出图/下结论前，必须用 tdlas_review（或读取返回的 validation）做自动校验；"
                 "校验 10 项：波数轴对齐、DAS-理论一致、αL 弱吸收、是否孤立线、调制系数 m、"
                 "采样率、ADC 动态范围、归一化方法、噪声、etalon 条纹。overall=fail 时**不得下结论**，"
                 "先修 fail 项；warn 项须在结论中向用户说明。",
         "pass": "validation.overall=pass 或（warn 项已向用户说明）"},
        {"step": 7, "name": "输出结论",
         "rule": "按此顺序：① 工况（物种/波段/T/P/x/L/fm/调制系数）；"
                 "② normalization.method 及 I0；③ 2f（或归一化 2f）峰值与位置；"
                 "④ 噪声分解（shot/thermal/RIN/1f）；⑤ validation 结论（pass/warn/fail）。"
                 "所有用默认值的参数必须显式标注「默认值」。",
         "pass": "结论完整、可复现、附校验结论"},
    ],
    "image_layout": {
        "总则": "图是用户唯一直接看到的产物，**规范性优先于美观**。出图前先确认：技术名、"
                "归一化/基线方法、线型含义、剔除区——缺一项这张图就不能用于汇报。",
        "何时出图": "只有显式 save_png=true 才出图；用户说'画图/给张图/画 WMS/画 DAS'时，"
                    "**必须传 save_png=true**，否则数据库里只有数值、用户拿不到图。"
                    "出图后把返回的 png / png_overlay 路径告诉用户。",
        "★ 自适应选图（按用户需求决定出什么图，不要一律给六子图）": {
            "判断依据": "从用户话里取意图 → 选图型 → 选参数。**用户要什么就给什么，别把标准六图当唯一答案。**",
            "意图 → 图型/参数": {
                "默认 / '给我 CH4 的图' / '画 WMS 图'": "标准 6 子图（panels 缺省 all），单浓度",
                "'画多浓度 / 不同浓度对比 / 标定曲线'": "传 x_list=[...] + save_png=true → 主图 ②~⑤ 面板**按 ppm 叠加各浓度**（纵轴按全部浓度统一）+ 附加 png_overlay 单用途对照",
                "'混合气 / 含 X% 甲烷和 Y% CO2'": "传 mixture='CH4:0.01,CO2:0.04' → αL 面板自动叠加各组分曲线，**不新增图**",
                "'只要相位匹配的 X 分量 / 不正交 / 单通道锁相'": "传 return_xy=true，从 wms_raw_xy 取 X1f_c/X2f_c（相位匹配分量）作图；"
                                                        "**说明差异**：正交解调取 √(X²+Y²)（默认），单通道只取 X，后者对相位失配更敏感",
                "'系统选型 / 光程要多少 / 检测限'": "tdlas_detection_limit_scan（1×2：LOD vs 光程、LOD vs 参考浓度）",
                "'只看 2f 曲线' / '只要吸光度'": "panels 子集（f2 / al）；**注意**非 6 面板时引擎改竖排堆叠",
            },
            "无法判断时": "先按标准 6 子图出，并在回答里说明'如需多浓度叠加/混合气分组/只看某分量请告知'——"
                          "**不要沉默地只给一种**。",
            "禁止": "用户明确要混合气却只画单组分；要标定曲线却不出 png_overlay；要 X 分量却给正交幅值而不说明。",
        },
        "四类图（按工具区分，勿混用）": {
            "① WMS 仪器链路图": {
                "工具": "tdlas_wms_instrument",
                "布局": "3 行 × 2 列共 6 面板，figsize≈14×10，每格约 2:1（横长竖短，利于读线形）",
                "面板顺序（左→右、上→下）": [
                    "① 波长调制 V(t)（三角波+正弦调制；横轴 时间(ms)，纵轴 驱动电压(V)）",
                    "② 直接吸收 DAS（无调制链路）",
                    "DAS 吸光度 vs 数据库理论值（同图，理论值为虚线对照）**← 与 ② 同一行右侧**",
                    "③ 一阶谐波 1f（纵轴 1f(V)）",
                    "④ 二阶谐波 2f（纵轴 2f(V)，单列避免与 1f 混读）",
                    "⑤ 归一化 2f（标题含归一化方法；纵轴 a.u.）",
                ],
                "要点": "**注意 ② 与 'DAS 吸光度' 是同一行的两个面板**——② 是 PD 原始信号，"
                        "右格才是 αL 与理论对照；不要把两者当成一个面板描述。",
            },
            "② DAS 仪器链路图": {
                "工具": "tdlas_das_instrument",
                "布局": "5 行 × 1 列竖排，figsize≈9.5×12",
                "面板顺序": ["drive V (V)", "power (mW)", "PD out (mV)",
                             "absorbance（吸光度）", "（第 5 格为链路补充信息）"],
                "备注": "该图轴标签为**英文**（与 WMS 的中文标签不同），描述时按图实际内容陈述，不要臆造中文标题。",
            },
            "③ DAS 时域链路图": {
                "工具": "tdlas_das_chain",
                "布局": "4 行 × 1 列竖排，figsize≈9×10",
                "面板顺序": ["三角波时序", "PD 原始时序", "拟合基线与对照", "absorbance 提取结果"],
                "横轴": "时间 (ms) 为主",
            },
            "④ 检测限扫描图": {
                "工具": "tdlas_detection_limit_scan",
                "布局": "1 行 × 2 列，figsize≈14×6",
                "面板顺序": ["LOD vs 光程 L（横轴 光程 L(cm)）",
                             "LOD vs 参考浓度 x（横轴 参考浓度，弱吸收下应近似平线）"],
                "用途": "系统选型：看加光程能把 LOD 降到多少",
            },
            "⑤ 多浓度同图（主图叠加 + 附加 png_overlay）": {
                "何时": "传 x_list（多浓度）+ save_png=true",
                "主图": "标准 6 子图的 ②~⑤ 面板按 viridis 配色叠加**全部浓度**，图例标 ppm；"
                        "纵轴按全部浓度的有效区统一（低浓度不会被压成平线）；① 驱动电压共用仍单条",
                "附加图": "png_overlay：单用途对照（归一化 2f/1f 一条曲线看线性/饱和），与主图 png 并存",
                "命名": "主图 png + 附加 png_overlay",
            },
        },
        "线型约定（每张图都必须遵守）": "实线=有效数据；虚线=理论参考（如 HITRAN 理论 αL）；"
                          "点线/灰点=剔除区（三角波转折点，不可信）；"
                          "**每种线型必须在图例里注明含义**。",
        "横轴约定": "驱动电压用**时间**；其余一律用**扫描波数**（不含 ±a 调制摆动的瞬时波数）"
                    "——用瞬时波数会把曲线折成'水平条纹'乱麻。",
        "剔除区": "扫描两端按 trim_frac（默认 12%）剔除，图上以红色带标出；"
                  "纵轴范围按有效区确定，避免剔除区伪影压扁曲线。",
        "图外必须同步给出的解读": [
            "本次用的技术（WMS / DAS）与为什么选它（线密度）",
            "归一化方法（2f/1f 或 2f/I0）及是否背景扣除",
            "基线处理方式（理想 I0 / 多项式拟合 / 是否触发回退）",
            "validation.overall 与其 fail/warn 项（图好看不代表数可用）",
        ],
        "禁止": "① 不传 save_png 就说'已出图'；② 凭印象描述图上没有的内容；"
                "③ 把 1f 与 2f 画进同一个面板；④ 用理想仿真图暗示实测结果。",
        "完整描述契约": "用户要'图里有什么'时，按面板顺序逐个说明：面板标题 → 横轴量 → 纵轴量 → "
                        "该格的结论（如'2f 呈标准双峰，峰位间隔 Δν≈±0.7a'）。",
    },
    "defaults_when_unknown": "所有物理参数未给时按以下顺序索取：① 实测标定值 → "
                             "② 器件型号（AI 自己检索规格书）→ ③ 引导用户现场标定 → ④ 保留内置默认并**显式标注**「默认值 X」。",
    "datasheet_lookup": {
        "何时": "用户提供了器件型号（如 DAQ 型号、DFB 激光器型号、光电探测器型号）时。",
        "动作": "AI **必须**联网检索该型号的官方 datasheet / 规格书提取参数（用 web 搜索 + 打开官方页面），"
                "不要只把型号当标签、也不要凭记忆编数字。",
        "按器件类别要查的参数": {
            "数据采集卡 DAQ": ["采样率上限 fs_max (S/s)", "分辨率 (bit)", "量程 (±V)", "输入噪声/输入阻抗"],
            "激光器驱动 / 电流源": ["V→I 跨导或调谐响应", "电流量程 (mA)", "调制带宽 (kHz)", "输出噪声 (µA/√Hz)"],
            "DFB / 可调谐激光器": ["中心波长 / 波数", "调谐系数 dν/dI (cm⁻¹/mA)", "阈值电流 i_th (mA)",
                              "输出功率 (mW)", "线宽 (MHz)"],
            "光电探测器 PD": ["响应度 resp (A/W)", "带宽 bw (Hz)", "跨阻增益 gain (V/A)",
                          "NEP (W/√Hz)", "饱和功率"],
        },
        "查到后": "把规格书参数填进仿真对应字段，并在 assumptions 里标注「来源：<型号> datasheet」。",
        "查不到或无网络": "退回到优先级 ③（引导现场标定）或 ④（默认值 + 显式标注），**不得编造规格书数值**。",
    },
    "das_vs_wms": {
        "本质": "WMS 通常强于 DAS。DAS 信号在 DC，受害于 1/f 噪声与激光慢漂移；"
                "WMS 把信号搬到 fm（30 kHz）用窄带锁相提取，避开 1/f，"
                "且 2f/1f 归一化抵消光强波动（免标定）。",
        "公平对比前提": "DAS 与 WMS 必须用同一套噪声模型（白噪声 + 1/f 粉红噪声 + 慢漂移）与同源噪声；"
                "DAS 漏加噪声等于作弊，会得出'WMS 反而差'的假象。",
        "正确判据": "看浓度反演稳定性/检测限，而非单周期 σ；2f/1f 不应在非吸收区（2f、1f 都≈0）测噪声。",
        "何时用 DAS": "需要绝对浓度、或谱线密集区想看直观包络时；",
        "何时用 WMS": "痕量检测、抗 1/f 与漂移、在线免标定时。",
        "谱线密度与波形": "孤立单线（n_lines ≤10）→ 2f 呈标准双峰；密集谱区 → 2f 多峰叠加（DAS 包络更直观）。"
                          "这只是'波形形态'问题，不代表 DAS 更强。",
    },
    "edge_definition": "edge 指**驱动电压**方向（daq_triangle 前半=电压上升、后半=电压下降）；"
                       "因 dν/dI<0（真实 DFB 激光：电流↑→波长红移→波数↓），电压上升沿对应波数下降、"
                       "下降沿对应波数上升。工具把所选方向反转为波数递增输出。",
    "pd_baseline_slope": "PD 原始信号含光强斜坡（L-I 曲线），且 dν/dI<0 使'波数递增'对应'光强下降'，"
                        "故默认 edge=rising 的 PD 基线呈**下降沿**——这是激光器真实特性，不是错误。"
                        "如需光强上升沿，用 edge=falling；DAS 吸光度已除以 I0 消除斜坡，不受影响。",
    "das_baseline_method": {
        "仿真做法": "DAS 基线用**理想 I0**（无吸收光强）：I0 = p_laser × throughput × resp × gain，"
                    "直接用 -ln(v_das / I0) 得 αL。因为仿真里 I0 可精确算出，无需拟合。",
        "为何不用非吸收区拟合": "真实实验常用'非吸收区多项式拟合'估 I0，但**密集谱区没有非吸收区**"
                "（如 C2H6 2963-2966 有 159 条线），off 掩码为空 → 拟合退化、基线把吸收也平均进去，"
                "导致 DAS 峰值偏移/为负（已实测：峰值偏 1.4 cm^-1、相关系数仅 0.015）。",
        "真实实验对应": "实测无法直接得 I0，应：① 扫到线翼外取非吸收基线；② 或充纯缓冲气测背景谱扣基线；"
                "③ 或用相邻无非吸收区时做多项式外推。仿真用 I0 是'已知真值的上限'，供验证用。",
        "告知": "解读 DAS 时须向用户说明：本仿真的 DAS 基线用的是理想 I0（非吸收光强），"
                "真实实验需另做背景扣除，DAS 实际精度会低于此理想值。",
    },
    "condition_adaptation": {
        "规则": "工况参数（T/P/x/L_cm）未给时，按物种用 SPECIES_PROFILES 自动推荐（自适应），"
                "不套用全局默认；返回 condition_advice 告知推荐值。"
                "**推荐波段的可达性也在 condition_advice 里**（波段可达性/laser_params_needed），"
                "推荐前先核对，否则用户按推荐选波段会直接撞上激光调谐范围。",
        "主动询问": "出图前必须**主动向用户确认工况**：物种/波段是否正确、浓度量级、光程、"
                    "T/P；用户不确定时给出 condition_advice 的推荐。",
        "弱吸收校验": "推荐工况应使 αL 落在 1e-3 ~ 1e-1（弱吸收线性区）；"
                      "若 αL>0.1 应建议降低 x 或 L_cm，若 <1e-5 应建议增大。",
    },
    "alpha_reporting": {
        "原则": "α（吸收系数 / αL）峰值随 T、P、网格步长 step、翼截断 wingHW、波数窗口 变化——"
                "**裸报一个 α 数值不可复现、不可比较**。故给出 α 峰值时须能同时给出这五项语境。",
        "何时必须报（自适应）": "看返回的 alpha_report.needs_report：True 时必须报"
                                "（本会话首次给出 α 峰值，或语境相对上次发生变化）；"
                                "False 说明语境未变、已播报过，可省略以保持简洁。",
        "报什么": "α 峰值数值 + T(K) + P(atm) + step(cm⁻¹) + wingHW(cm⁻¹) + 窗口(cm⁻¹)，缺一不可；"
                  "具体值见 alpha_report.alpha_peak_context。",
        "注意": "不得为求简洁而省略语境后再对 α 做定量结论；语境不同的两次 α 不可直接比较。",
    },
    "fringe_reporting": {
        "原则": "etalon 干涉条纹是 TDLAS 痕量检测的**头号系统性误差**：两个平行且反射率不可忽略的面"
                "（窗片/透镜/光纤端面）构成 F-P 腔，产生周期 FSR=1/(2nd)、峰-峰对比度≈4R/(1−R)² 的透过率起伏，"
                "其 2f 与吸收 2f 同形。模型**默认关闭**（fringe=False），绝不静默注入系统性误差。",
        "何时问": "用户提到窗片 / 光纤 / 滤光片 / 未镀膜或未楔化窗片时，应主动询问是否建模；"
                  "开启后若未给几何参数，返回的 param_requests 会列出 fringe_n / fringe_d_cm / fringe_R。",
        "汇报内容": "看返回的 fringe_report：needs_report=True（本会话首次启用或条纹语境变化）时，须说明"
                    "FSR 与吸收线宽的关系、确定性条纹可被背景扣除而只有漂移残留、"
                    "以及压不掉条纹时应采取的物理措施（窗片楔化 / AR 镀膜 / 扫频平均）。",
        "禁止": "不得把 etalon 条纹当成可被降噪 / 多次平均消掉的随机噪声——固定腔长的条纹是确定性项。",
    },
    "fidelity_reporting": {
        "原则": "仿真只包含**已建模的效应**；未建模项（自展宽、线混合、暗电流、前放 1/f…）"
                "在当前工况下可能主导真实误差。**点值不等于带误差的结果。**",
        "单一真源": "能力登记表在 tools/tdlas_fidelity.py（EFFECTS）：每项效应的实现位置、"
                    "是否默认生效、已知缺口写在同一张表里，并由 audit 与源码证据交叉核对。"
                    "本工具返回的 fidelity 块即该表的结构化快照，**以它为准**，勿凭记忆复述。",
        "何时报": "① 用户问「这个结果可信吗 / 误差多大」；② 给出任何定量结论（浓度、LOD、灵敏度）时；"
                  "③ 结论涉及 fidelity.not_implemented 中某项时——例：高浓度（自展宽）、"
                  "密集谱区（线混合）、痕量 LOD（前放 1/f 未建模 ⇒ LOD 偏乐观）。",
        "报什么": "① 本次实际生效的效应（fidelity.implemented）；"
                  "② 会影响该结论的未实现项（fidelity.not_implemented）；"
                  "③ 明确「这是点值，未含系统不确定度」。",
        "禁止": "不得把未实现项说成「已建模」；不得把点值当带误差的结果呈现；"
                "不得用 n_repeats 的统计散布冒充总不确定度（它只含 run-to-run 随机分量）。",
    },
    "clarify_protocol": {
        "触发": "返回的 clarify.needed=True（有缺省参数）时，必须进入澄清流程。",
        "流程": "① 按 clarify.questions 向用户提问；② 收到回答后填入对应参数重跑；"
                "③ 用户明确说'用默认值'才可跳过该项；④ 全部确认后才出图/下结论。",
        "强制": "clarify.instruction 明确要求'先问再出结果'，这是硬约束，不是建议。",
        "多轮": "支持多轮：每次用户补充一个参数，就少问一个（questions 会随 assumed 缩小）。",
        "提问工具": "AI 必须用**原生结构化提问工具**（AskUserQuestion 类，点击式选择框）"
                    "把 clarify.questions 渲染成选择题，**禁止用纯文字列表让用户打字回答**；"
                    "1 次 1–4 题、每题 2–4 个固定选项 + '其他'自由输入；"
                    "超过 4 题时按优先级分批多轮：波段 > 浓度/光程 > T/P > 器件 > 噪声。",
    },
    "noise_and_interaction": {
        "默认": "**默认：RIN 与 1/f（漂移/粉红）关，但散粒/热噪声恒在**——它们是物理固有项"
                "（只取决于光电流与带宽，量级通常 0.01–0.1 mV），无法通过参数关闭；"
                "要让噪声严格为零请设 physical_noise=False（此时为纯理论对照，结果逐位可复现）。"
                "随机种子默认 seed=0 → 默认参数下结果可复现；返回里的 seed_used 即实际所用种子。",
        "询问": "MCP 必须在出图/解读前**主动询问用户**：是否需要加噪声？加哪类？多大？"
                "可选：① RIN 白噪声(rin) ② 1/f 慢漂移(drift_frac) ③ 1/f 粉红(flicker_frac)。"
                "散粒/热噪声为物理固有、量级极小，**恒在**（若用户要求绝对无噪，用 physical_noise=False）。",
        "透明化": "每次返回必须把 noise_breakdown_mV + noise_1f 原样告知用户，"
                  "并明确「本次是否加了噪声、加了哪些、幅度多少」。",
        "多交互": "MCP 服务要**多与用户交互**，而非一次性吐结果：① 先确认工况（物种/波段/T/P/浓度/光程）；"
                  "② 主动问噪声需求；③ 解读前确认归一化方法；④ 主动问是否调噪声/换孤立线。",
    },
    "glossary": {
        "αL": "吸光度（吸收系数×光程），无量纲；≪1 才算弱吸收，DAS/2f 才与浓度成正比",
        "HWHM": "吸收线半高半宽（cm⁻¹），决定最优调制深度",
        "m": "调制系数 = 调制深度 a ÷ HWHM，最优约 2.2",
        "fm": "正弦调制频率（把信号搬到高频，避开低频噪声）",
        "fscan": "三角波扫描频率（扫过整条吸收线）",
        "RIN": "激光相对强度噪声，常为 TDLAS 主导噪声源",
        "2f/1f": "用 1f 谐波归一化 2f，抵消光强波动；弱吸收下正比于浓度（免标定核心）",
        "LOD": "检测限（最小可测浓度）",
        "m 优化": "工具会自动按 m≈2.2 反算正弦调制幅值；如需手动指定可用 mod_amp_V",
    },
}


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

# ───────────────────────── 多浓度扫描 x_list（输入解析 + 防呆） ─────────────────────────
_X_LIST_MAX = 64


def _parse_number_list(v, name):
    """接受 list[number] 或 '1e-4,1e-3' / '1e-4 1e-3' 字符串 → float 列表。

    命令行与 MCP 文本参数常只能传字符串，故同时支持两种写法（与 detection_limit_scan 一致）。
    """
    if isinstance(v, (list, tuple)):
        items = list(v)
    elif isinstance(v, str):
        items = [u.strip() for u in re.split(r"[,\s]+", v) if u.strip()]
    else:
        items = [v]
    try:
        return [float(u) for u in items]
    except (TypeError, ValueError):
        raise ValueError(f"{name} 必须是数字列表或可解析的数字串，收到 {v!r}")


def _resolve_x_list(x, x_list, default_x):
    """把浓度输入归一化成「实际要计算的浓度列表」，并做防呆校验。

    - 只给 x（或都不给用默认）：单浓度，返回 (False, [x_single], x_single)。
    - 给 x_list：多浓度扫描，返回 (True, xs_升序去重, None)。
    - 同时给 x 与 x_list：冲突，直接报错（二选一）。

    防呆（防呆 = 不让错误输入静默跑出误导结果）：
      · 空列表 / 非有限数 → 报错
      · 超出摩尔分数定义域 (0, 1] → 报错（**不静默截断**，截断会掩盖前提已破）
      · 长度超过 _X_LIST_MAX → 报错（防一次扫上千点把会话卡死）
      · 自动去重（1e-12 相对容差）并升序，让返回的扫描曲线单调、好画
    """
    if x_list is None:
        xv = default_x if x is None else float(x)
        if not (0.0 < xv <= 1.0):
            raise ValueError(f"摩尔分数 x 必须在 (0, 1]，收到 {xv!r}")
        return False, [xv], xv
    if x is not None:
        raise ValueError("x 与 x_list 二选一，不要同时传（多浓度请用 x_list）")
    xs = _parse_number_list(x_list, "x_list")
    if not xs:
        raise ValueError("x_list 不能为空")
    if len(xs) > _X_LIST_MAX:
        raise ValueError(f"x_list 长度 {len(xs)} 超过上限 {_X_LIST_MAX}，请缩减扫描点数")
    bad = [v for v in xs if not (math.isfinite(v) and 0.0 < v <= 1.0)]
    if bad:
        raise ValueError(f"x_list 中每个浓度必须 ∈ (0, 1]（摩尔分数），非法值：{bad[:5]}")
    # 去重（相对容差 1e-12）+ 升序
    seen, uniq = [], []
    for v in xs:
        if not any(abs(v - u) <= 1e-12 * max(1.0, abs(u)) for u in seen):
            seen.append(v)
            uniq.append(v)
    uniq.sort()
    return True, uniq, None


def _linearity_guard(xs, peak):
    """防呆诊断：弱吸收下响应峰高应正比于 x；偏离线性即提示已进入饱和/非线性区，

    用该区间做标定会得到错误斜率（标定曲线被压低）。以最小浓度为线性基准。
    """
    if len(xs) < 2:
        return {"checked": False, "note": "单点无法判线性"}
    if peak[0] <= 0:
        return {"checked": False, "note": "基准峰高为 0，无法判线性"}
    xref, pref = xs[0], peak[0]
    ratios = [p / x / (pref / xref) for p, x in zip(peak, xs)]
    tol = 0.10
    dev = [i for i, r in enumerate(ratios) if abs(r - 1.0) > tol]
    idx = dev[0] if dev else None
    if idx is None:
        return {"checked": True, "reference_x": xref,
                "ratio_to_reference": ratios, "tolerance": tol,
                "nonlinear_from_index": None, "nonlinear_from_ppm": None,
                "note": "全扫描范围内响应对浓度近似线性（偏差 < 10%），可作线性标定"}
    return {"checked": True, "reference_x": xref,
            "ratio_to_reference": ratios, "tolerance": tol,
            "nonlinear_from_index": idx, "nonlinear_from_ppm": xs[idx] * 1e6,
            "note": (f"以最小浓度 {xref * 1e6:.3g} ppm 为线性基准；响应/浓度 比值在 "
                     f"{xs[idx] * 1e6:.3g} ppm 处偏离 >{tol * 100:.0f}%，已进非线性区——"
                     f"该点之后不宜做线性标定")}


def _build_multi_conc(xs, samples, metric_names, metric_func):
    """把多浓度扫描的引擎结果 samples（与 xs 对齐）汇总成 multi_conc 块。

    metric_func(sample) → dict（键为 metric_names）；首项是「主响应」，用于线性判定/饱和防呆。
    """
    metrics = {k: [] for k in metric_names}
    for s in samples:
        m = metric_func(s)
        for k in metric_names:
            metrics[k].append(m[k])
    prim = metric_names[0]
    return {"enabled": True, "x_list": xs, "mole_ppm": [x * 1e6 for x in xs],
            "response": metrics, "linearity": _linearity_guard(xs, metrics[prim])}


def t_simulate(species="H2O", wn0=7185.596, T=None, P=None, x=None, L=None,
               a=0.10, i0=0.1671, i2=2.48e-3, psi1_pi=1.9356, psi2_pi=4.4138,
               span=0.8, n_scan=401, n_mod=96, sigma_tau=0.0, seed=None,
               save_png=False, session_id="default", x_list=None, mixture=None):
    """DAS + 免标定 WMS 正向仿真：返回 1f、2f 峰高、DAS 最小透过率、线表信息。

    wn0 为目标线中心（cm^-1）；x 为摩尔分数（默认 1e-3 = 1000 ppm，弱吸收）；L 为光程 cm；
    a 为调制深度 cm^-1。sigma_tau 为等效透过率噪声（默认 0=理想无噪）。save_png=True 时出图。
    T/P/x/L 未给出时按「会话已确认 > 默认」取值（复用 tdlas_session 已确认的工况）。
    """
    if mixture is not None and x_list is not None:
        raise ValueError("mixture（混合气多组分）与 x_list（单物种多浓度）不能同时使用，请二选一")
    _xd, _Ld = _species_defaults(species)
    _x_raw = x                                  # 保留原始参数（未解析），供 _resolve_x_list 判"x 与 x_list 二选一"
    T, P, x, L, assumed = _resolve_conditions(T, P, x, L, session_id, x_default=_xd, L_default=_Ld)
    _multi, _xs, _x_single = _resolve_x_list(_x_raw, x_list, x)
    if _multi:
        assumed = [k for k in assumed if k != "x"]
    x = _x_single if not _multi else _xs[0]
    i0, i2, p1, p2 = _common(species, wn0, T, P, x, L, a, i0, i2, psi1_pi, psi2_pi, span)
    with _quiet() as g:
        r = ts.simulate(species, wn0, float(T), float(P), float(x), float(L), float(a),
                        i0, i2, p1, p2, float(span), int(n_scan), int(n_mod),
                        sigma_tau=float(sigma_tau), seed=seed, mixture=mixture)
    k2 = int(r["S2f"].argmax())
    out = {"species": (str(species).upper() if mixture is None else r["meta"]["species"]),
           "wn0_cm-1": float(wn0), "T_K": float(T),
           "P_atm": float(P), "mole_frac": (float(x) if mixture is None else None),
           "mixture": r["meta"].get("mixture"), "path_cm": float(L),
           "mod_depth_cm-1": float(a),
           "alpha_peak_cm-1": float(r["alpha"].max()),
           "das_tau_min": float(r["tau"].min()),
           "wms_1f_peak": float(r["S1f"].max()),
           "wms_2f_peak": float(r["S2f"].max()),
           "wms_2f_peak_nu_cm-1": float(r["wn_scan"][k2]),
           "wms_2f1f_peak": float(r["S2f1f"].max()),
           "n_lines_in_window": r["meta"]["n_lines_in_window"],
           "table": r["meta"].get("table"),
           "assumptions": assumed, "needs_confirm": bool(assumed),
           "log": _sanitize_paths(g.getvalue()).splitlines()}
    if save_png:
        OUT_DIR.mkdir(parents=True, exist_ok=True)
        png = OUT_DIR / f"tdlas_{str(species).upper()}_{float(wn0):g}cm-1_x{float(x):g}.png"
        with _quiet():
            ts.plot(r, png)
        out["png"] = str(png)
    if _multi:
        _samples = [r] + [
            ts.simulate(species, wn0, float(T), float(P), float(xi), float(L), float(a),
                        i0, i2, p1, p2, float(span), int(n_scan), int(n_mod),
                        sigma_tau=float(sigma_tau), seed=seed)
            for xi in _xs[1:]]
        out["multi_conc"] = _build_multi_conc(
            _xs, _samples,
            ["wms_2f1f_peak", "wms_2f_peak", "alpha_peak_cm-1", "das_tau_min"],
            lambda rr: {"wms_2f1f_peak": float(rr["S2f1f"].max()),
                        "wms_2f_peak": float(rr["S2f"].max()),
                        "alpha_peak_cm-1": float(rr["alpha"].max()),
                        "das_tau_min": float(rr["tau"].min())})
        if save_png:                                   # 多浓度同图（兼容性操作）：叠加 α(ν)
            OUT_DIR.mkdir(parents=True, exist_ok=True)
            _ov = OUT_DIR / f"tdlas_{_safe_name(str(species).upper())}_{float(wn0):g}cm-1_xlist_overlay.png"
            ts.plot_multi_x(str(_ov), [xi * 1e6 for xi in _xs],
                            [s["alpha"] for s in _samples], _samples[0]["nu"],
                            r"波数 (cm$^{-1}$)", r"吸收系数 $\alpha$ (cm$^{-1}$)",
                            f"{str(species).upper()} 多浓度吸收谱 (α vs ν)")
            out["png_overlay"] = str(_ov)
    return out


def t_invert(peak_2f1f, species="H2O", wn0=7185.596, T=None, P=None, L=None,
             a=0.10, x_ref=None, k=None, i0=0.1671, i2=2.48e-3,
             psi1_pi=1.9356, psi2_pi=4.4138, span=0.8, session_id="default",
             n_repeats=0, spectrum=None, ref_spectrum=None):
    """免标定浓度反演：由测得的归一化 2f 峰高求摩尔分数 x = peak / k。

    k 是**完整链路**灵敏度：优先用调用方传入的 k（来自 tdlas_wms_instrument 返回的
    results.sensitivity_k）；未给 k 时，内部跑一遍**完整仪器链路**在参考浓度 x_ref 下重算 k。
    与 k **配对**的峰高是 results.**S2f_norm_peak**（k 的分子就是它，故 x = peak/k 精确）；
    results.S2f1f_peak 只在归一化方法为 2f/1f 时才等于它，退化为 2f/I0 时配 k 会算错。
    **勿用解析版 simulate() 的灵敏度**——它与完整链路的 S2f/1f 差 ~6×，混用会严重偏差。
    仅适用弱吸收（αL ≪ 1）。a/i0/i2/psi/span 为解析版遗留参数，完整链路下不再使用。

    ⚠ 闭环精度不等于反演精度：k 的定义就是 S2f_norm_peak / x，故"用同一次仿真的
    peak 配 k"必然精确还原（误差恒为 0），这**不是**验证。真实误差来自换工况 ——
    k 与 T/P/x/L_cm/调制深度/fm/扫描半宽 强绑定，换任一条件都必须重新取 k。

    n_repeats > 0 时额外给出**测量不确定度**（同一工况重复仿真的 run-to-run 散布）；
    返回的 uncertainty 只含统计（随机）分量，不含 k 标定误差、数据库不确定度、
    线型近似等系统项 —— 不得当作总不确定度（见该字段的 scope/note）。
    """
    _xd, _Ld = _species_defaults(species)
    T, P, x_ref, L, assumed = _resolve_conditions(T, P, x_ref, L, session_id, x_default=_xd, L_default=_Ld)
    if not 0.0 < float(x_ref) <= 1.0:
        raise ValueError(f"x_ref 必须在 (0, 1]，收到 {x_ref}")
    # 输入域校验：2f/1f 峰高是**取绝对值**后的量（≥0）；负值/NaN 一律是用法错误，
    # 直接算 peak/k 会静默返回负浓度，必须在这里拦下。
    peak_2f1f = float(peak_2f1f)
    if not math.isfinite(peak_2f1f):
        raise ValueError(f"peak_2f1f 必须是有限数值，收到 {peak_2f1f!r}")
    if peak_2f1f < 0.0:
        raise ValueError(f"peak_2f1f 必须 ≥ 0（2f/1f 峰高取绝对值），收到 {peak_2f1f:g}")
    k_cond = None
    # 与仪器链同一策略：目标波数超出激光"整段出光"范围时把 wn_ref 重锚到目标波数
    # （t_invert 没有激光参数入口，故总是允许重锚），并如实披露。
    # 不加这一步，它的默认参数（H2O@7185.596）会直接撞上"默认激光只覆盖 2964.7–2971.0"。
    _inst = {}
    _laser_note, _laser_info = _maybe_align_laser(float(wn0), _inst, set(), False)
    with _quiet() as g:
        if k is not None:
            k = float(k)
            if not math.isfinite(k) or k <= 0.0:
                raise ValueError(f"灵敏度 k 必须为有限正数，收到 {k:g}"
                                 f"（应用 tdlas_wms_instrument 的 results.sensitivity_k）")
            k_src = "调用方提供（tdlas_wms_instrument.sensitivity_k）"
        else:
            r = ts.simulate_wms_instrument(str(species), wn_center=float(wn0), T=float(T),
                                           P=float(P), x=float(x_ref), L_cm=float(L), **_inst)
            k = float(r["meta"]["sensitivity_k"])
            k_src = "完整链路重算（@x_ref）"
            # 回传内部重算 k 所用的**完整工况**：调用方只有拿到这些才能判断 k 是否与自己的
            # 测量同源（此前只回 x_ref/T/P/L，调制深度/fm/扫描半宽/采样都看不到）。
            _mc = r["meta"]
            k_cond = {"species": str(species).upper(), "wn_center_cm-1": float(wn0),
                      "T_K": float(T), "P_atm": float(P), "L_cm": float(L),
                      "x_ref": float(x_ref),
                      # 取引擎 meta 里**实际生效**的 wn_ref（未重锚时 _inst 里没有该键，
                      # 早先会写成 None，调用方无从判断 k 到底在哪支"激光器"上算的）
                      "wn_ref": _mc.get("cfg", {}).get("wn_ref"),
                      "scan_span_cm-1": _mc.get("cfg", {}).get("scan_span_cm"),
                      "mod_freq_Hz": _mc.get("mod_freq_Hz"), "mod_amp_V": _mc.get("mod_amp_V"),
                      "mod_coeff_m": _mc.get("mod_coeff_m"),
                      "fs_Hz": _mc.get("fs"), "seed_used": _mc.get("seed_used")}
        x_est = float(ts.invert_concentration(peak_2f1f, k))
    # 输出域校验：摩尔分数定义域是 (0, 1]。越界**不静默截断**（截断会掩盖前提已破），
    # 而是显式标注状态 —— 越界本身即说明 k 与本次工况不同源 / 已出弱吸收线性区 / 峰值取错。
    if x_est <= 0.0:
        x_status, x_warn = "zero", ("反演浓度 ≤ 0：peak_2f1f 为 0 或远小于噪声。"
                                    "请确认取的是吸收峰而非非吸收区，且基线已正确扣除。")
    elif x_est > 1.0:
        x_status = "out_of_range_high"
        x_warn = (f"反演浓度 {x_est:.4g} > 1，物理上不可能（摩尔分数 ≤ 1）→ 该结果不可用。"
                  f"常见原因：① k 与本次工况不同源（物种/T/P/光程/调制深度不同）；"
                  f"② αL 已出弱吸收线性区（2f 偏离 x 的线性）；"
                  f"③ 2f 峰值含未扣除的 RAM 基线/背景。")
    else:
        x_status, x_warn = "ok", None
    # ── 谱线级最小二乘（可选）：用整条归一化 2f 谱拟合，而非只用单个峰值 ──
    # 为什么：单点除法丢掉整条谱的信息，抗噪差且无法诊断模型失配。
    # 约定：传 ref_spectrum（同工况 x_ref 下的 S2f_norm_peak 归一谱）最佳；
    #       只传 spectrum 时内部跑一次参考谱（同工况，仅用于归一化形状）。
    ls_fit = None
    if spectrum is not None:
        try:
            import tdlas_fit as _TF
            import numpy as _np          # tdlas_mcp.py 本身不导入 numpy（在引擎侧），故局部导入
            _meas = _np.asarray(spectrum, dtype=float).ravel()
            if ref_spectrum is not None:
                _ref = _np.asarray(ref_spectrum, dtype=float).ravel()
                if _meas.shape != _ref.shape:
                    raise ValueError(f"谱长不匹配：spectrum={_meas.size} vs ref_spectrum={_ref.size}")
                ls_fit = _TF.fit_concentration_ls(_meas, _ref / float(x_ref))
                ls_fit["k_source"] = "调用方提供的 ref_spectrum（同工况）"
            else:
                with _quiet() as _g9:
                    _rr = ts.simulate_wms_instrument(str(species), wn_center=float(wn0),
                                                     T=float(T), P=float(P), x=float(x_ref),
                                                     L_cm=float(L), seed=0)
                _ref = _np.abs(_rr["S2f_norm_cyc"])[_rr["valid_mask"]]
                _k1 = _ref / float(x_ref)
                if _meas.size == _ref.size:
                    ls_fit = _TF.fit_concentration_ls(_meas, _k1)
                    ls_fit["k_source"] = "内部参考谱（@x_ref，同工况）"
                else:
                    ls_fit = {"error": f"谱长不匹配：spectrum={_meas.size} vs 内部参考={_ref.size}；"
                                       f"请改传 ref_spectrum（同工况、同采样）"}
        except Exception as _e:                             # noqa: BLE001
            ls_fit = {"error": f"{type(_e).__name__}: {_e}"}

    out = {"species": str(species).upper(), "mole_frac": x_est,
           "mole_frac_valid": x_status == "ok", "mole_frac_status": x_status,
           "peak_2f1f_in": peak_2f1f, "sensitivity_k": k,
           "k_source": k_src, "x_ref": float(x_ref), "T_K": float(T), "P_atm": float(P),
           "path_cm": float(L), "assumptions": assumed, "needs_confirm": bool(assumed),
           "k_binding": "k 与工况强绑定：T/P/x/L_cm/调制深度/fm/扫描半宽 任一改变都必须重新取 k，"
                        "否则 peak/k 不是同一套物理条件下的比值。",
           "log": _sanitize_paths(g.getvalue()).splitlines()}
    if k_cond:                      # 内部重算 k 时，回传它用的完整工况，供判断 k 是否同源
        out["k_conditions"] = k_cond
    if _laser_info:
        out["laser_auto_aligned"] = _laser_info
    if _laser_note:
        out["warnings"] = [_laser_note]
    if ls_fit is not None:
        out["ls_fit"] = ls_fit
        if isinstance(ls_fit, dict) and ls_fit.get("x") is not None:
            out["mole_frac_ls"] = ls_fit["x"]
            out["mole_frac_ls_sigma"] = ls_fit.get("x_sigma")
            out["ls_fit_note"] = ("最小二乘用整条谱线；χ²_red ≫ 1 说明存在系统失配"
                                  "（线形/基线/工况不同源），此时先查前提而非采信 σ。")
    if x_warn:
        out["warning"] = x_warn

    # ── 测量不确定度（可选）：同工况重复仿真 → run-to-run 相对散布 → 换算到本浓度 ──
    # 默认 0 = 不算（保持原有耗时与返回形状不变）；建议定量结论用 n_repeats≥10。
    if int(n_repeats) > 0:
        # ★ σ 必须在**要报告的那个浓度**上求：噪声里有一部分（热噪声/ADC 量化/RIN）不随
        #   吸收信号等比缩放，故 rel_sigma 随 x 变化（实测跨 4 个量级差约 20×）。
        #   早先固定用 x_ref（参考浓度）评估、再套到 x_est 上，浓度差得远时误差可达一个量级。
        #   x_est 越界（≤0 / >1）时本次结果本身已不可用，退回到参考浓度只作"给个数"的兜底。
        if x_status == "ok":
            _x_eval, _x_src = float(x_est), "本次反演浓度 x_est（与该结论同一工作点）"
        else:
            _x_eval, _x_src = float(x_ref), f"参考浓度 x_ref（x_est 状态={x_status}，本次结果本身不可用）"
        with _quiet() as g2:
            unc = ts.measurement_uncertainty(str(species), float(wn0), float(T), float(P),
                                             float(_x_eval), float(L), seed=0,
                                             n_repeats=int(n_repeats), **_inst)
        unc["x_eval_source"] = _x_src
        if unc.get("valid"):
            _, xs, x95 = ts.rel_uncertainty_for_x(x_est, unc["rel_sigma"], unc["ci_level"])
            unc["mole_frac"] = x_est
            unc["mole_frac_sigma"] = xs
            unc["mole_frac_ci95_halfwidth"] = x95
            unc["interpretation"] = (f"x = {x_est:.4g} ± {xs:.2g}（1σ，**仅统计分量**）；"
                                     f"95% 区间半宽 ±{x95:.2g}")
            unc["caveat"] = ("系统项未计入：k 标定误差、HITRAN 线强/展宽数据库不确定度、"
                             "自展宽与线混合等线型近似、etalon 条纹、带宽钳位、"
                             "光程与温度测量误差。真实总不确定度 ≥ 本值。")
        out["uncertainty"] = unc
        # 追加而不是覆盖：第一个 _quiet() 块里是 k 重算的诊断输出，覆盖会让 log 变空
        out["log"] = (out.get("log") or []) + _sanitize_paths(g2.getvalue()).splitlines()
    out["fidelity"] = _fidelity_digest()          # ⑩ 未建模效应随结果披露
    out["interaction"], out["next_required_actions"] = _interaction_block(out, "invert", session_id)
    return out


def t_detection_limit(species="H2O", wn0=7185.596, T=None, P=None, L=None,
                      a=0.10, sigma_tau=1e-5, x_true=None, n_trials=30,
                      n_sigma=3.0, seed=0, i0=0.1671, i2=2.48e-3,
                      psi1_pi=1.9356, psi2_pi=4.4138, span=0.8, session_id="default"):
    """检测极限 LOD（最小可测摩尔分数）—— 蒙特卡洛。

    sigma_tau 为等效透过率噪声（RIN / 散粒 / 探测器 / 电路的综合，工程上由无吸收基线实测给出）。
    返回噪声等效浓度 NEC 与 LOD = n_sigma × NEC。
    T/P/x_true/L 未给出时按「会话已确认 > 默认」取值。
    """
    _xd, _Ld = _species_defaults(species)
    T, P, x_true, L, assumed = _resolve_conditions(T, P, x_true, L, session_id, x_default=_xd, L_default=_Ld)
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
            "sensitivity_k": float(dl["k"]), "assumptions": assumed,
            "needs_confirm": bool(assumed), "log": _sanitize_paths(g.getvalue()).splitlines()}



def t_calibration_curve(species="CH4", wn0=2968.5, T=296.0, P=1.01325,
                        L=10.0, x_list=None, save_png=False):
    """标定曲线：扫多个浓度，看 2f 峰高与浓度的线性关系。

    x_list: 浓度列表（默认 5 个：0.5x, 1x, 2x, 5x, 10x）
    返回：各浓度 2f 峰高 + 线性拟合 k/R² + 可选出图
    """
    import numpy as np

    # 默认浓度列表
    if x_list is None:
        x_typ = 50e-6
        x_list = [x_typ*0.5, x_typ, x_typ*2, x_typ*5, x_typ*10]

    peaks = []
    for x in x_list:
        r = ts.simulate_wms_instrument(species=species, wn_center=wn0,
                                      T=T, P=P, x=x, L_cm=L)
        # 取有效区 2f 峰
        s2f = np.abs(r["S2f_cyc"])
        peaks.append(float(s2f.max()))

    # 线性拟合
    x_arr = np.array(x_list)
    y_arr = np.array(peaks)
    k, b = np.polyfit(x_arr, y_arr, 1)
    y_fit = k * x_arr + b
    ss_res = np.sum((y_arr - y_fit)**2)
    ss_tot = np.sum((y_arr - y_arr.mean())**2)
    r2 = 1 - ss_res / ss_tot if ss_tot > 0 else 0

    out = {
        "species": species, "wn0_cm-1": wn0,
        "concentrations": x_list,
        "s2f_peaks": peaks,
        "linear_fit": {"slope_k": float(k), "intercept_b": float(b), "R2": float(r2)},
        "linearity_note": "R² > 0.99 为优秀线性区间；R² < 0.95 说明已进入非线性区"
    }

    if save_png:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        from matplotlib import font_manager
        for f in ["Microsoft YaHei", "SimHei"]:
            try:
                font_manager.findfont(f, fallback_to_default=False)
                plt.rcParams["font.family"] = f
                break
            except Exception:
                pass
        plt.rcParams["axes.unicode_minus"] = False

        fig, ax = plt.subplots(figsize=(8, 5))
        ax.plot(x_arr*1e6, y_arr, "o", ms=8, color="#1f77b4", label="仿真数据")
        ax.plot(x_arr*1e6, y_fit, "--", color="#d62728",
                label=f"线性拟合: y={k:.2e}x+{b:.2e}\nR²={r2:.4f}")
        ax.set_xlabel("浓度 x (ppm)")
        ax.set_ylabel("|S2f| 峰")
        ax.set_title(f"{species} @ {wn0} cm⁻¹ 标定曲线")
        ax.legend()
        ax.grid(alpha=0.3)

        out_path = str(OUT_DIR / "calibration_curve.png")
        fig.savefig(out_path, dpi=150, bbox_inches="tight")
        plt.close(fig)
        out["png"] = _sanitize_paths(out_path)

    return out


def t_detection_limit_scan(species="CH4", wn0=2968.5, T=296.0, P=1.01325, a=0.10,
                           L_list=None, x_list=None, sigma_tau=1e-5, n_trials=10,
                           n_sigma=3.0, seed=0, save_png=False):
    """检测极限 LOD 随光程 L / 参考浓度 x 的扫描（系统选型：加光程能降多少 LOD）。

    弱吸收下 LOD ∝ 1/L（光程加倍、LOD 减半），且 LOD 与参考浓度 x 基本无关，
    直到 αL≳0.1 进入非线性区才略升。返回 LOD 网格（行=浓度 x，列=光程 L）。
    """
    def _nums(v, default):
        if v is None:
            return default
        if isinstance(v, str):
            v = [u.strip() for u in v.replace("，", ",").split(",") if u.strip()]
        return [float(u) for u in v]
    L_list = _nums(L_list, [5.0, 10.0, 30.0, 50.0, 100.0, 200.0])
    x_list = _nums(x_list, [1e-4, 1e-3, 1e-2])
    with _quiet() as g:
        res = ts.scan_detection_limit(str(species), float(wn0), float(T), float(P), float(a),
                                      L_list=L_list, x_list=x_list, sigma_tau=float(sigma_tau),
                                      n_trials=int(n_trials), n_sigma=float(n_sigma),
                                      seed=int(seed))
    out = {"species": res["species"], "wn0_cm-1": res["wn0"], "T_K": res["T"],
           "P_atm": res["P"], "a_cm-1": res["a"],
           "L_list_cm": res["L_list"], "x_list": res["x_list"],
           "LOD": [[float(v) for v in row] for row in res["LOD"]],
           "sigma_tau": res["sigma_tau"], "n_trials": res["n_trials"],
           "n_sigma": res["n_sigma"],
           "physical_note": ("弱吸收下 LOD ∝ 1/L（光程加倍、LOD 减半），且 LOD 与参考浓度 x "
                             "基本无关；αL≳0.1 进入非线性区后 LOD 略升。"),
           "log": _sanitize_paths(g.getvalue()).splitlines()}
    if save_png:
        OUT_DIR.mkdir(parents=True, exist_ok=True)
        png = OUT_DIR / f"detection_limit_scan_{res['species']}_{res['wn0']:g}cm-1.png"
        with _quiet():
            ts.plot_detection_limit_scan(res, png)
        out["png"] = str(png)
    return out


def t_das_chain(species="H2O", wn0=7185.596, T=None, P=None, x=None, L=None,
                span=None, fscan=None, fs=None, baseline_slope=None,
                sigma=0.0, seed=None, fit_order=None, fit_frac=None,
                edge=None, save_png=False, x_list=None, mixture=None):
    """三角波扫描 DAS 全链路：PD 原始信号 → 多项式基线拟合扣除 → DAS 吸光度信号。

    默认工况（未显式给出时使用，并列入 assumptions 供复核）：
        **1000 ppm（x=1e-3）、0.5 m 光程（L=50 cm）、296 K、1 atm、上升沿（edge="rising"）**。
    技术知识见返回中的 edge_note（上升/下降沿为何不重合、如何取舍）。
    """
    if mixture is not None and x_list is not None:
        raise ValueError("mixture（混合气多组分）与 x_list（单物种多浓度）不能同时使用，请二选一")
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
    _multi, _xs, _x_single = _resolve_x_list(given["x"], x_list, x)
    if _multi:
        assumed = [k for k in assumed if k != "x"]
    x = _x_single if not _multi else _xs[0]

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
                               seed=seed, fit_order=fit_order, fit_frac=fit_frac, edge=edge,
                               mixture=mixture)
    tp = float(r["alpha_L_true"].max())
    dp = float(r["das"].max())
    out = {"species": (str(species).upper() if mixture is None else r["meta"]["species"]),
           "wn0_cm-1": float(wn0), "T_K": T, "P_atm": P,
           "mole_frac": (x if mixture is None else None),
           "mole_ppm": (x * 1e6 if mixture is None else None),
           "mixture": r["meta"].get("mixture"),
           "path_cm": L, "path_m": L / 100.0,
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
           "log": _sanitize_paths(g.getvalue()).splitlines()}
    if save_png:
        OUT_DIR.mkdir(parents=True, exist_ok=True)
        png = OUT_DIR / f"das_chain_{str(species).upper()}_{float(wn0):g}cm-1_x{x:g}.png"
        with _quiet():
            ts.plot_das_td(r, png)
        out["png"] = str(png)
    if _multi:
        _samples = [r] + [
            ts.simulate_das_td(species, float(wn0), T, P, xi, L,
                               span=span, fscan=fscan, fs=fs,
                               baseline_slope=baseline_slope, sigma=float(sigma),
                               seed=seed, fit_order=fit_order, fit_frac=fit_frac, edge=edge)
            for xi in _xs[1:]]
        out["multi_conc"] = _build_multi_conc(
            _xs, _samples, ["das_absorbance_peak", "alpha_L_true_peak"],
            lambda rr: {"das_absorbance_peak": float(rr["das"].max()),
                        "alpha_L_true_peak": float(rr["alpha_L_true"].max())})
        if save_png:                                   # 多浓度同图：叠加 DAS 吸光度
            OUT_DIR.mkdir(parents=True, exist_ok=True)
            _ov = OUT_DIR / f"das_chain_{_safe_name(str(species).upper())}_{float(wn0):g}cm-1_xlist_overlay.png"
            ts.plot_multi_x(str(_ov), [xi * 1e6 for xi in _xs],
                            [s["das"] for s in _samples], _samples[0]["wn_das"],
                            r"波数 (cm$^{-1}$)", "吸光度 (DAS)",
                            f"{str(species).upper()} 多浓度 DAS 吸光度")
            out["png_overlay"] = str(_ov)
    return out


# ══════════════════════════════════════════════════════════════════════
# 契约块的**单一真源**（任务 2）
# ──────────────────────────────────────────
# 为什么需要：此前每个工具在自己函数体里**手拼**这些块，于是
# "同一类工具却拿到不同契约"这种不对称是必然会发生的（实测：
#   DAS 工具曾缺 `validation`，而它是个定量工具）。
# 现在把"该工具应交付哪些块"变成**声明**，并由一个函数
# 统一组装；契约自检里有一条断言：**声明了的块必须真的出现**。
# 这样"忘了给某个工具加某个块"会变成 CI 失败，而不是静默缺失。

#: 每个块的**唯一生产者**（声明式：想加一个新契约块，只改这里 + 消费端声明）
# ══════════════════════════════════════════════════════════════════════
# 输出凭据（provenance）：让一个数可被第三方重建
# ───────────────────────────────────────────────────────────────
# 为什么需要："能承担系统选型与方法验证的责任"的前提是**可被引用**，
# 而可被引用的最低门槛是：别人拿到你的数，能重建出同一个数。
# 此前输出里有条件（T/P/x/L）、seed、线表名，**但没有版本与数据指纹**——
# 而 HITRAN 会定期更新线表，同名表的内容会变。
def _engine_version():
    """引擎版本：优先取仓库内的版本常量，取不到则标未知。"""
    for name in ("__version__", "ENGINE_VERSION", "VERSION"):
        v = globals().get(name)
        if isinstance(v, str) and v:
            return v
    return (SERVER_INFO.get("serverInfo") or {}).get("version") or "unknown"


def _input_hash(out):
    """输入参数指纹（sha256 前 16 位）：由气体、波数、工况与硬件参数共同决定。

    只取**影响数值**的字段，不取描述性块（warnings / validation…），
    否则同一工况在不同描述下会算出不同指纹。
    """
    import hashlib as _h, json as _j
    keys = ("species", "mixture", "wn_center_cm-1", "conditions", "tri_wave", "laser", "pd",
            "adc", "wms", "physical_noise", "seed_used", "fringe_report", "results")
    keep = {}
    for k in keys:
        if k in out:
            v = out[k]
            if k == "results" and isinstance(v, dict):
                v = {kk: vv for kk, vv in v.items() if not kk.startswith("noise_")}
            keep[k] = v
    blob = _j.dumps(keep, sort_keys=True, ensure_ascii=False, default=str)
    return _h.sha256(blob.encode("utf-8")).hexdigest()[:16]


def provenance_block(out, table=None):
    """给定结果产出"这是什么算出来的"的凭据块。

    语义：把 engine_version + 线表指纹 + 输入指纹 一并给出，
    使一个数在论文里被引用时可被独立重建与核对。
    """
    prov = {"engine_version": _engine_version(),
            "input_hash": _input_hash(out)}
    if table:
        try:
            prov["line_table"] = ts.th.table_fingerprint(table) \
                if hasattr(ts, "th") and hasattr(ts.th, "table_fingerprint") else {"table": table}
        except Exception:                                  # noqa: BLE001
            prov["line_table"] = {"table": table}
    _dv = (out or {}).get("device_provenance")
    if _dv:
        prov["device_source"] = _dv
        prov["device_source_note"] = (
            "本次使用的硬件参数来自设备库：**请如实告知来源**"
            "（datasheet=U用户提供的规格书；ai=AI 推断；"
            "unspecified=未声明——此时不得称其为“型号规格”）。")
    prov["note"] = ("引用本结果时请一并给出 engine_version 与 line_table.sha256_16："
                    "同名线表会随 HITRAN 更新而变化，无指纹则无法重建。")
    return prov


CONTRACT_BLOCK_PRODUCERS = {
    "assumptions":            lambda r, m: r.get("assumptions"),
    "param_requests":         lambda r, m: r.get("param_requests"),
    "needs_input":            lambda r, m: bool(r.get("param_requests")),
    "needs_confirm":          lambda r, m: bool(r.get("assumptions")),
    "warnings":               lambda r, m: (r.get("warnings") if r.get("warnings") is not None
                                             else (r.get("meta") or {}).get("warnings")),
    "validation":             lambda r, m: (_validate_any(r) if r.get("validation") is None
                                             else r.get("validation")),
    "alpha_report":           lambda r, m: _alpha_report_block(
                                  (r.get("meta") or {}).get("alpha_context"),
                                  r.get("species") or m.get("species"),
                                  m.get("session_id", "default")),
    "fringe_report":          lambda r, m: _fringe_report_block(
                                  (r.get("meta") or {}).get("fringe"),
                                  m.get("session_id", "default")),
    "fidelity":               lambda r, m: _fidelity_digest(),
    # 凭据块：从结果里取线表名（tools/results 两种布局都兼容）
    "provenance":             lambda r, m: provenance_block(
                                  r, (r.get("results") or {}).get("table")
                                     or (r.get("meta") or {}).get("table")),
}

#: 各工具**声明**自己要哪些块。定量工具的模板必须一致（由契约断言强制）。
#: 声明的语义：**该块一定会被交付**。故只列"总是非空"的块；
#: 可为空的布尔（needs_input / needs_confirm）不得列入——否则"假值即缺席"
#: 与"声明了就必须出现"直接矛盾。它们的信息由契约断言单独保障。
QUANTITATIVE_BLOCKS = ("assumptions", "param_requests",
                       "warnings", "validation", "alpha_report", "fringe_report", "fidelity",
                       "provenance")
CONTRACT_BLOCKS = {
    "tdlas_wms_instrument":  QUANTITATIVE_BLOCKS,
    "tdlas_review":          QUANTITATIVE_BLOCKS,
    "tdlas_das_instrument":  QUANTITATIVE_BLOCKS,
    # 下列工具不走定量链路，只声明它们真正会产生的块
    "tdlas_simulate":        ("assumptions", "warnings", "alpha_report", "fidelity"),
    "tdlas_das_chain":       ("assumptions", "warnings", "alpha_report", "fidelity"),
    "tdlas_invert":          ("assumptions", "needs_confirm", "warnings", "fidelity"),
    "tdlas_detection_limit": ("assumptions", "needs_confirm", "fidelity"),
}


def _validate_any(r):
    """尝试用 WMS 校验器处理任意结果；不适用则返回 None。

    用于不得已的兼容：不同链路的结果字典形状不同，
    强行调用会 KeyError（不能把"校验不适用"当成"校验失败"）。
    """
    try:
        return ts.validate_wms_result(r)
    except Exception:                                      # noqa: BLE001
        return None


def apply_contract_blocks(out, tool, session_id="default", extra=None):
    """按声明把契约块统一装进返回体（显式声明，不做隐式推断）。

    只写入 **非 None** 的值：不能为了"字段齐全"而造出一个看着有、实则空的块，
    那会比缺失更糟（模型会以为已拥有该信息）。
    返回：实际写入的块名列表（供契约断言比对）。
    """
    meta = {"session_id": session_id, "species": (extra or {}).get("species")}
    written = []
    for name in CONTRACT_BLOCKS.get(tool, ()):
        prod = CONTRACT_BLOCK_PRODUCERS.get(name)
        if prod is None:
            continue
        try:
            val = prod(out, meta)
        except Exception:                                  # noqa: BLE001
            val = None
        # 假值即缺席（None / False / 空列表 / 空字典）：
        # 不为"字段齐全"而写一个看着有、实则空的块——那会让模型以为已拥有该信息。
        if not val:
            continue
        out[name] = val
        written.append(name)
    # 交互契约需要前面的块已就位才能生成动作清单
    if "interaction" in (extra or {}):
        try:
            inter, acts = _interaction_block(out, tool, session_id)
            out["interaction"] = inter
            out["next_required_actions"] = acts
            written += ["interaction", "next_required_actions"]
        except Exception:                                  # noqa: BLE001
            pass
    if extra and extra.get("clarify") is not None:
        out["clarify"] = extra["clarify"]
        written.append("clarify")
    return written


_INSTR_KEYS = ("scan_span_cm", "amp_V", "freq_Hz", "offset_V", "phase_deg", "eta_VI", "dnu_dI",
               "wn_ref", "i_ref", "i_th", "eta_IP", "fs", "n_samples", "adc_bits",
               "v_range", "throughput", "resp", "gain", "bw", "rin",
               # 调谐二阶非线性（两条链都经 voltage_to_laser 使用）
               "d2nu_dI2",
               # 严格理想仿真开关：False = 连散粒/热也不注入
               "physical_noise",
               # 明知扫描段有暗区（激光未出光）仍要计算的显式开关：
               # 默认 False ⇒ 引擎拒绝"部分出光"的扫描（那里的 2f/1f 是失真的，且旧版不报错）
               "allow_partial_dark",
               # etalon 干涉条纹（默认关，仅显式 fringe=True 时生效）
               "fringe", "fringe_n", "fringe_d_cm", "fringe_R", "fringe_fsr",
               "fringe_contrast", "fringe_phase_rad", "fringe_drift_frac")

# etalon 条纹相关键：默认不参与"缺省参数确认"（默认关 → 不该反复追问）；
# 但用户一旦开启（fringe=True）且未给几何参数，则列入 assumptions / param_requests 主动索取。
_FRINGE_KEYS = ("fringe", "fringe_n", "fringe_d_cm", "fringe_R", "fringe_fsr",
                "fringe_contrast", "fringe_phase_rad", "fringe_drift_frac")

# 以下参数有明确默认值或自动反算，无需用户确认：
#   amp_V / offset_V 由 wn_center + scan_span_cm 经 V-ν 关系反算；
#   background_subtract 是修正开关：默认 False（出原始谱，不替用户静默修正基线），
#   需要定量结论时由用户显式开启。
_NO_CONFIRM = {"amp_V", "offset_V", "background_subtract"}
_SCENE_DEFAULTS = {"T": 296.0, "P": 1.01325, "x": 1e-3, "L_cm": 50.0,
                   "edge": "rising", "fit_order": 3, "fit_frac": 0.3}

# 返回里回显**实际生效**的激光参数（含 auto_laser 重锚后的 wn_ref）：数值本身不受回显影响，
# 但没有它就无法核对"引擎到底按哪组激光参数算的"，尤其在自动重锚发生后。
_LASER_ECHO_KEYS = ("eta_VI", "dnu_dI", "d2nu_dI2", "wn_ref", "i_ref", "i_th", "eta_IP")


def _safe_name(s):
    """净化拼进 PNG 文件名的字段：只留字母数字._-，长度上限 40。

    species 是字符串参数，直接拼进路径时 `species="../../x"` 会越出 tmp/mcp_out/。
    """
    return re.sub(r"[^A-Za-z0-9_.-]", "_", str(s))[:40] or "x"


def _partial_dark_note(cfg):
    """扫描段存在"无激光输出"的暗区时返回警告（含暗区占比与处置建议），否则 None。

    为什么必须警告：暗区里的 PD 信号为 0，锁相解调的是**被截断的波形**，2f/1f 归一化失真，
    而引擎**不会报错** —— 属"不报错但结果不可信"。默认已由引擎拒绝；只有用户显式
    `allow_partial_dark=True`（或历史工况复算）时才会走到这里，故必须把话说明白。

    ⚠ 用 cfg 里**实际生效**的 offset_V / amp_V 直接算，不拿 wn_center 反算：用户显式给
    offset_V 时扫描中心由它决定、wn_center 只是标签（与 `_require_driver_range` 同一个坑）。
    """
    off = float(cfg["offset_V"])
    amp = float(cfg.get("amp_V") or 0.0)
    eta_VI = float(cfg["eta_VI"])
    i_th = float(cfg.get("i_th", 30.0))
    if amp <= 0.0:
        return None
    vth = i_th / eta_VI if eta_VI > 0 else 0.0
    lo, hi = off - amp, off + amp
    dark = 0.0
    if lo < vth:                                     # 扫描下端落进"阈值以下"的暗区
        dark += (min(vth, hi) - lo) / (2.0 * amp)
    if hi > ts.DRIVER_V_HI:                          # 扫描上端越过驱动器上限
        dark += (hi - max(ts.DRIVER_V_HI, lo)) / (2.0 * amp)
    dark = min(1.0, max(0.0, dark))
    if dark <= 0.0:
        return None
    return (f"⚠ 扫描段有约 {dark * 100:.0f}% 无激光输出（扫描电压区间 "
            f"[{lo:.3g}, {hi:.3g}] V，阈值电压 {vth:.3g} V，驱动器上限 {ts.DRIVER_V_HI:.0f} V）："
            f"该段 PD 信号为 0，锁相解调的是被截断的波形，2f/1f 失真 —— **定量结论不可用**。"
            f"建议：减小 scan_span_cm、抬高 offset_V、换到更靠近 wn_ref 的谱线，"
            f"或改用匹配该波段的激光参数；本结果属「明知有暗段仍要算」（allow_partial_dark=True）。")


def _maybe_align_laser(wn_center, inst, explicit_keys, device_used=False):
    """目标波数超出当前激光可达范围时，把 wn_ref 重锚到该波数（理想激光器假设）。

    为什么需要：SPECIES_PROFILES 给 6 个物种推荐了 8 个波段（1900–7185 cm⁻¹），
    而默认激光只是一支 3.36 μm DFB（wn_ref=2964.7、驱动器 0–5 V）——按文档推荐选波段，
    第一次调用必报"offset_V 越界"，用户会误以为是自己选错了线。

    为什么合理：wn_ref 只是"参考电流处的波数"这个标定基准点，重锚不改变扫描窗口与
    吸收物理，语义上等于声明"有一支中心落在该波数的理想激光器"。但**绝不静默**：
    重锚会写进 assumptions 与 warnings，AI 须按 must_disclose② 告知用户这不是真实硬件。

    返回 (note|None, info|None)。用户显式给了 wn_ref/offset_V 或引用了设备库时不介入。
    """
    if device_used or ("wn_ref" in explicit_keys) or ("offset_V" in explicit_keys):
        return None, None
    eta_VI = inst.get("eta_VI", 24.0)
    dnu_dI = inst.get("dnu_dI", -0.088)
    i_ref = inst.get("i_ref", 120.0)
    wn_ref0 = inst.get("wn_ref", 2964.7)
    span_cm = inst.get("scan_span_cm", 1.5)
    if inst.get("amp_V"):                       # 显式给了三角波幅值 → 由它反推等效扫描半宽
        span_cm = abs(float(inst["amp_V"]) * float(eta_VI) * float(dnu_dI))
    d2 = inst.get("d2nu_dI2", 0.0)
    i_th = inst.get("i_th", 30.0)
    r = ts.laser_reach(float(wn_center), eta_VI=eta_VI, dnu_dI=dnu_dI, i_ref=i_ref,
                       wn_ref=wn_ref0, scan_span_cm=span_cm, d2nu_dI2=d2, i_th=i_th)
    if r.get("reachable"):
        return None, None
    if r.get("reason") == "invalid_laser_params":
        raise ValueError(
            f"激光参数非法：η_VI={float(eta_VI):g} 必须 >0、dν/dI={float(dnu_dI):g} 必须非零"
            f"（否则电压与波数之间没有可用的映射）。")
    if not r.get("fits_span"):
        # ⚠ 报错里必须用**实际生效**的扫描半宽 span_cm（可能来自 amp_V 反推），
        #   不能用 inst.get("scan_span_cm") —— 用户只给 amp_V 时它还是默认值，会报错数字。
        raise ValueError(
            f"扫描半宽 {float(span_cm):g} cm⁻¹ 需要 ±{r['amp_V']:.3g} V 驱动，"
            f"超出驱动器 {ts.DRIVER_V_LO:.0f}–{ts.DRIVER_V_HI:.0f} V 量程 → 任何 wn_ref 都盖不住，"
            f"请减小 scan_span_cm 或核对 η_VI。")
    old = float(wn_ref0)
    # 自校验：重锚候选是按线性关系算的，二次模型（d2nu_dI2≠0）下必须复算确认真的可达，
    # 不能"算出个候选就直接用"——那正是本项目要消灭的静默伪造。
    inst["wn_ref"] = float(r["wn_ref_needed"])
    r2 = ts.laser_reach(float(wn_center), eta_VI=eta_VI, dnu_dI=dnu_dI, i_ref=i_ref,
                        wn_ref=inst["wn_ref"], scan_span_cm=span_cm, d2nu_dI2=d2, i_th=i_th)
    if not r2.get("reachable"):
        inst["wn_ref"] = old
        raise ValueError(
            f"目标波数 {float(wn_center):g} cm⁻¹ 无法通过重锚 wn_ref 达到"
            f"（重锚到 {r['wn_ref_needed']:.6g} 后仍 {r2['reason']}）→ 请核对 dν/dI / d²ν/dI² / η_VI。")
    _off = r.get("offset_V")
    _off_txt = (f"{_off:.4g} V" if _off is not None
                else f"无实解（{r.get('reason')}）")
    info = {"wn_ref_before": old, "wn_ref_auto": float(r["wn_ref_needed"]),
            "offset_V_before": r["offset_V"], "reason": r["reason"],
            "note": "理想激光器假设：仅重锚 wn_ref（参考电流处的波数），其余激光参数未变"}
    note = (f"⚠ 目标波数 {float(wn_center):g} cm⁻¹ 超出当前激光器可达范围"
            f"（wn_ref={old:g}、dν/dI={float(dnu_dI):g}、驱动器 "
            f"{ts.DRIVER_V_LO:.0f}–{ts.DRIVER_V_HI:.0f} V，原参数反算 offset_V：{_off_txt}）"
            f"→ **已自动把 wn_ref 重锚到 {info['wn_ref_auto']:.6g} cm⁻¹**（理想激光器假设，"
            f"其余激光参数未变）。这不是你的真实硬件：请提供实测 wn_ref/dν/dI，或用 tdlas_device "
            f"保存真实激光器后以 laser= 引用；确认后本提示消失。")
    return note, info


def t_das_instrument(species="CH4", wn_center=2968.5, T=None, P=None, x=None, L_cm=None,
                     edge=None, fit_order=None, fit_frac=None, seed=0, session_id="default",
                     auto_laser=True, save_png=False, setup=None, laser=None, pd=None, daq=None,
                     optics=None, x_list=None, mixture=None, **kw):
    """仪器系统级 DAS 仿真：DAQ 电压 → 激光 → 光路 → PD → ADC 量化 → 基线扣除。

    缺省参数按优先级索取：① 用户实测标定值 → ② 器件型号（AI 检索规格书）
    → ③ 引导用户现场测量 → ④ 内置默认（常见 16-bit USB DAQ + 典型中红外 DFB 激光器）。
    返回 assumptions / param_requests / warnings，供 AI 主动向用户澄清后再解读结论。
    mixture（可选）：混合气多组分，α=Σ x_i·α_pure_i（"CH4:0.01,CO2:0.04" 或 [{"species","x"}]）；
    与单物种 x 二选一，且不能与 x_list 同用。
    """
    if mixture is not None and x_list is not None:
        raise ValueError("mixture（混合气多组分）与 x_list（单物种多浓度）不能同时使用，请二选一")
    given_scene = {"T": T, "P": P, "x": x, "L_cm": L_cm, "edge": edge,
                   "fit_order": fit_order, "fit_frac": fit_frac}
    # 工况优先级：本次显式给 > 会话已确认(跨会话记忆) > 全局默认（与 t_wms_instrument 一致）
    confirmed = _get_session(session_id).get("confirmed", {})
    scene, assumed = {}, []
    for k, v in given_scene.items():
        if v is not None:
            scene[k] = v
        elif k in confirmed:
            scene[k] = confirmed[k]
        else:
            scene[k] = _SCENE_DEFAULTS[k]
            assumed.append(k)

    # 多浓度扫描：x_list 覆盖单 x（显式或会话确认的 x 都让位），并从缺省清单移除 x
    _multi, _xs, _ = _resolve_x_list(x, x_list, scene["x"])
    if _multi:
        scene["x"] = _xs[0]
        assumed = [k for k in assumed if k != "x"]

    given_inst = {k: kw.get(k) for k in _INSTR_KEYS}
    inst = {k: v for k, v in given_inst.items() if v is not None}
    # 设备引用：解析设备库，填充未显式给出的硬件参数（本次显式给的值仍最优先）
    dev_params = _resolve_devices(setup, laser, pd, daq, optics)
    for k, v in dev_params.items():
        inst.setdefault(k, v)
    # ★ 从 setup 取出工况参数合并（显式参数优先）
    _conds = dev_params.pop("__conditions__", None)
    if _conds:
        for k, v in _conds.items():
            inst.setdefault(k, v)
        assumed.append(f"工况参数来自 setup={setup!r}：{list(_conds.keys())}")
    assumed += [k for k, v in given_inst.items()
                if v is None and k not in _NO_CONFIRM and k not in inst
                and (k not in _FRINGE_KEYS or inst.get("fringe"))]

    if not (species and str(species).strip()):
        raise ValueError("species 不能为空")

    laser_info = None
    if auto_laser:
        _note, laser_info = _maybe_align_laser(wn_center, inst, set(kw), bool(dev_params))
    else:
        _note = None
    ignored = sorted(k for k in kw if k not in _INSTR_KEYS)

    with _quiet() as g:
        r = ts.simulate_das_instrument(species, wn_center=float(wn_center),
                                       T=float(scene["T"]), P=float(scene["P"]),
                                       x=float(scene["x"]), L_cm=float(scene["L_cm"]),
                                       edge=scene["edge"], fit_order=int(scene["fit_order"]),
                                       fit_frac=float(scene["fit_frac"]), seed=seed,
                                       mixture=mixture, **inst)
    m = r["meta"]
    cfg = m["cfg"]
    _dark_note = _partial_dark_note(cfg)     # 暗区披露（allow_partial_dark 时才可能非空）

    requests = []
    for k in assumed:
        if k in PARAM_ACQ_GUIDE:
            cn, unit, why = PARAM_ACQ_GUIDE[k]
            requests.append({"param": k, "cn": cn, "unit": unit, "why": why,
                             "value_used": cfg.get(k, _SCENE_DEFAULTS.get(k)),
                             "how": "① 用户实测值 ② 器件型号（AI 检索规格书）"
                                    "③ 引导现场标定 ④ 保留默认并注明"})

    # 澄清问句：与 wms 同源同形（此前 DAS **没有** clarify，最需要先问清工况的一条链
    # 反而拿不到问句 → 交互不对称，见 tools/tdlas_mcp.py 的交互契约说明）
    _used = {k: (scene[k] if k in scene else cfg.get(k)) for k in assumed}
    _clarify_qs = build_clarify_questions(species, wn_center, assumed, _used)

    out = {"species": m["species"], "wn_center_cm-1": float(wn_center),
           "mixture": m.get("mixture"),
           "mole_frac": (None if mixture is not None else float(cfg["x"])),
           "scan_window_cm-1": [round(m["scan_lo"], 4), round(m["scan_hi"], 4)],
           "scan_half_span_cm-1": round(m["span_cm-1"], 4),
           "tri_wave": {k: cfg[k] for k in ("amp_V", "freq_Hz", "offset_V", "phase_deg")},
           "laser": {k: cfg[k] for k in _LASER_ECHO_KEYS},
           "pd": {k: cfg[k] for k in ("resp", "gain", "bw", "rin", "throughput")},
           "adc": {"fs_Hz": cfg["fs"], "n_samples": cfg["n_samples"],
                   "bits": cfg["adc_bits"], "v_range_V": cfg["v_range"], "lsb_V": m["lsb_V"]},
           "results": {"alpha_L_peak": m["alpha_L_peak"],
                       "v_pd_mean_V": m["v_pd_mean"],
                       "saturated_points": m["n_sat"],
                       "noise_rms_mV": m["sigma_v_noise"] * 1e3,
                       "noise_breakdown_mV": {"shot": m["sigma_shot"] * 1e3,
                                              "thermal": m["sigma_thermal"] * 1e3,
                                              "rin": m["sigma_rin"] * 1e3},
                       "n_lines_in_window": m["n_lines_in_window"], "table": m["table"],
                       "next_suggestions": generate_next_suggestions(
                           {"alpha_L_peak": float(m["alpha_L_peak"]),
                            "noise_breakdown_mV": {"shot": float(m["sigma_shot"]) * 1e3,
                                                   "thermal": float(m["sigma_thermal"]) * 1e3,
                                                   "rin_white": float(m["sigma_rin"]) * 1e3}},
                           {"L_cm": float(cfg["L_cm"]), "x": float(cfg["x"])})},
           "conditions": {"T_K": float(cfg["T"]), "P_atm": float(cfg["P"]),
                          "x": (None if mixture is not None else float(cfg["x"])),
                          "L_cm": float(cfg["L_cm"])},
           "assumptions": assumed,
           "param_requests": requests,
           "needs_input": bool(requests),
           "needs_confirm": bool(assumed),   # 与 WMS 同口径：有默认值就需用户确认
           "clarify": {"needed": bool(requests),
                       "instruction": "needed=True 时：**先向用户提出 questions 里的澄清问题，"
                                      "收到回答前不要直接出图/下结论**。用户明确说'用默认值'才可跳过。"
                                      "**用原生结构化提问工具（AskUserQuestion 类点击式选择框）渲染，"
                                      "禁止纯文字列表**；1 次 1–4 题，超过 4 题按优先级分批多轮。",
                       "questions": _clarify_qs},
           "confirm_note": "param_requests 为缺省参数：请优先向用户索取实测值或器件型号；"
                           "必要时引导现场标定；保留默认时须在结论中明确注明。",
           "seed_used": m.get("seed_used"),
           "physical_noise": m.get("physical_noise", True),
           "warnings": ([_note] if _note else []) + ([_dark_note] if _dark_note else []) + m["warnings"],
           "edge_note": EDGE_TECH_NOTE,
           "log": _sanitize_paths(g.getvalue()).splitlines()}
    if laser_info:
        out["laser_auto_aligned"] = laser_info
    if ignored:
        out["ignored_params"] = ignored
        out["ignored_note"] = (f"以下参数不属于本工具、已被忽略（疑似拼写错误）：{ignored}；"
                               f"允许的键见 tools/list 的 inputSchema")
    out["alpha_report"] = _alpha_report_block(m.get("alpha_context"), species, session_id)
    out["fringe_report"] = _fringe_report_block(m.get("fringe"), session_id)
    # DAS 结果校验（与 WMS 的 validate_wms_result 同位）：DAS 是定量技术，此前**没有**
    # 任何自动校验，只有场景级 warnings。判据见 tdlas_sim.validate_das_result 的文档。
    out["validation"] = ts.validate_das_result(r)
    if _multi:
        _samples = [r] + [
            ts.simulate_das_instrument(species, wn_center=float(wn_center),
                                       T=float(scene["T"]), P=float(scene["P"]),
                                       x=float(xi), L_cm=float(scene["L_cm"]),
                                       edge=scene["edge"], fit_order=int(scene["fit_order"]),
                                       fit_frac=float(scene["fit_frac"]), seed=seed, **inst)
            for xi in _xs[1:]]
        out["multi_conc"] = _build_multi_conc(
            _xs, _samples, ["alpha_L_peak", "v_pd_mean_V"],
            lambda rr: {"alpha_L_peak": rr["meta"]["alpha_L_peak"],
                        "v_pd_mean_V": rr["meta"]["v_pd_mean"]})
        if save_png:                                   # 多浓度同图：叠加吸收光学厚度 αL
            OUT_DIR.mkdir(parents=True, exist_ok=True)
            _ov = OUT_DIR / f"das_instr_{_safe_name(str(species).upper())}_{float(wn_center):g}cm-1_xlist_overlay.png"
            # ⚠ DAS 仪器链的返回键是 das / nu_das（不是 WMS 的 alphaL_cyc / nu_axis）：
            #   此前照抄 WMS 的键 → x_list+save_png 时 KeyError，整次调用失败。
            ts.plot_multi_x(str(_ov), [xi * 1e6 for xi in _xs],
                            [s["das"] for s in _samples], _samples[0]["nu_das"],
                            r"波数 (cm$^{-1}$)", r"吸收光学厚度 $\alpha L$",
                            f"{str(species).upper()} 多浓度吸收谱 (αL vs ν)")
            out["png_overlay"] = str(_ov)
    out["fidelity"] = _fidelity_digest()
    out["interaction"], out["next_required_actions"] = _interaction_block(out, "das", session_id)
    if save_png:
        OUT_DIR.mkdir(parents=True, exist_ok=True)
        png = OUT_DIR / f"das_instr_{_safe_name(str(species).upper())}_{float(wn_center):g}cm-1.png"
        with _quiet():
            ts.plot_das_instrument(r, png)
        out["png"] = str(png)
    # 设备来源：回显本次用的硬件参数来自哪里（datasheet/user/ai/...）
    _dprov = _device_provenance(setup, laser, pd, daq, optics)
    if _dprov:
        out["device_provenance"] = _dprov
    # 凭据块：让本结果可被第三方重建（引擎版本 + 线表指纹 + 输入指纹）
    # ⚠ 必须在 device_provenance **之后** 生成，否则 provenance 里看不到设备来源。
    if "provenance" not in out:
        out["provenance"] = provenance_block(out, m.get("table"))
    return out


# WMS 专属参数。⚠ 这里曾漏掉 edge / trim_frac / d2nu_dI2 / am_* —— 而 schema 声明了它们，
# 于是 AI 传 edge="falling" 被静默丢弃、返回里 normalization.edge 仍是 "rising"，
# 用户以为改了、实际没改（见 CHANGELOG「参数透传」）。**新增 schema 键必须同时加到这里**，
# 契约测试 tools/contract_check.py 会做 schema↔透传键的双向核对，防止再次漂移。
_WMS_ONLY_KEYS = ("mod_freq_Hz", "mod_amp_V", "mod_phase_deg", "m_opt", "lockin_avg",
                  "lockin_stages", "drift_frac", "flicker_frac", "background_subtract",
                  "edge", "trim_frac", "am_i0", "am_i2", "am_psi1", "am_psi2",
                  "norm_lock", "lock_kind", "zero_phase")
_INSTR_KEYS_WMS = _INSTR_KEYS + _WMS_ONLY_KEYS


def t_wms_instrument(species="CH4", wn_center=2968.5, T=None, P=None, x=None, L_cm=None,
                     seed=0, session_id="default", auto_laser=True, save_png=False,
                     return_xy=False, x_list=None,
                     setup=None, laser=None, pd=None, daq=None, optics=None, mixture=None, **kw):
    """WMS 仪器链路仿真：三角波扫描 + 正弦调制 → 激光 → 光路 → PD → ADC → 数字锁相（2f/1f）。

    调制幅值默认按**最优调制系数 m≈2.2** 自动优化（2f 峰值最大处）。
    未给出的参数列入 assumptions / param_requests；AI 须按 AI_INTERACTION_GUIDE 处理并显式标注。
    mixture（可选）：混合气多组分，α=Σ x_i·α_pure_i（"CH4:0.01,CO2:0.04" 或 [{"species","x"}]）；
    与单物种 x 二选一，不能与 x_list 同用；给定时 species 仅作标签。各组分 αL 叠加在标准图的 αL 面板。
    """
    if mixture is not None and x_list is not None:
        raise ValueError("mixture（混合气多组分）与 x_list（单物种多浓度）不能同时使用，请二选一")
    import numpy as np
    sp = str(species).strip().upper()
    prof = SPECIES_PROFILES.get(sp, {})
    scene_rec = {"T": prof.get("T", _SCENE_DEFAULTS["T"]),
                 "P": prof.get("P", _SCENE_DEFAULTS["P"]),
                 "x": prof.get("x_typ", _SCENE_DEFAULTS["x"]),
                 "L_cm": prof.get("L_cm", _SCENE_DEFAULTS["L_cm"])}
    given_scene = {"T": T, "P": P, "x": x, "L_cm": L_cm}
    # 工况优先级：本次显式给 > 会话已确认(跨会话记忆) > 物种推荐 > 全局默认
    confirmed = _get_session(session_id).get("confirmed", {})
    scene = {}
    for k, v in given_scene.items():
        if v is not None:
            scene[k] = v
        elif k in confirmed:
            scene[k] = confirmed[k]
        else:
            scene[k] = scene_rec[k]
    assumed = [k for k, v in given_scene.items() if v is None and k not in confirmed]

    # 多浓度扫描：x_list 覆盖单 x（显式或会话确认的 x 都让位），并从缺省清单移除 x
    _multi, _xs, _ = _resolve_x_list(x, x_list, scene["x"])
    if _multi:
        scene["x"] = _xs[0]
        assumed = [k for k in assumed if k != "x"]

    given_inst = {k: kw.get(k) for k in _INSTR_KEYS_WMS}
    inst = {k: v for k, v in given_inst.items() if v is not None}
    # 设备引用：解析设备库，填充未显式给出的硬件参数（本次显式给的值仍最优先）
    dev_params = _resolve_devices(setup, laser, pd, daq, optics)
    for k, v in dev_params.items():
        inst.setdefault(k, v)
    # ★ 从 setup 取出工况参数合并（显式参数优先）
    _conds = dev_params.pop("__conditions__", None)
    if _conds:
        for k, v in _conds.items():
            inst.setdefault(k, v)
        assumed.append(f"工况参数来自 setup={setup!r}：{list(_conds.keys())}")
    assumed += [k for k, v in given_inst.items()
                if v is None and k not in _NO_CONFIRM and k not in inst
                and (k not in _FRINGE_KEYS or inst.get("fringe"))]
    if not (species and str(species).strip()):
        raise ValueError("species 不能为空")

    # 目标波数越界时自动重锚 wn_ref（理想激光器假设；已显式给激光参数/引用设备时不介入）
    laser_info = None
    if auto_laser:
        _note, laser_info = _maybe_align_laser(wn_center, inst, set(kw), bool(dev_params))
    else:
        _note = None
    ignored = sorted(k for k in kw if k not in _INSTR_KEYS_WMS)

    with _quiet() as g:
        r = ts.simulate_wms_instrument(species, wn_center=float(wn_center),
                                       T=float(scene["T"]), P=float(scene["P"]),
                                       x=float(scene["x"]), L_cm=float(scene["L_cm"]),
                                       seed=seed, return_xy=bool(return_xy),
                                       mixture=mixture, **inst)
    m = r["meta"]
    cfg = m["cfg"]
    _dark_note = _partial_dark_note(cfg)     # 暗区披露（allow_partial_dark 时才可能非空）
    s2f1f = np.abs(r["S2f1f_cyc"])
    valid_i = r["valid_mask"]
    # ★ 取峰值必须限定在**有效区**（valid_mask，已剔除扫描两端 trim_frac）内：
    #   sensitivity_k 就是在有效区取 max（tdlas_sim.simulate_wms_instrument），
    #   若这里用全窗 argmax，被剔除的三角波转折点伪影可能成为"峰值" →
    #   返回的 results.S2f1f_peak 与 sensitivity_k 不是同一区间，
    #   把这对不自洽的数直接喂给 tdlas_invert 就会算出错误浓度。
    if not valid_i.any():
        raise ValueError("有效区为空（valid_mask 全 False）：trim_frac 过大，请减小后重试")
    _vidx = np.flatnonzero(valid_i)
    kpk = int(_vidx[int(np.argmax(s2f1f[valid_i]))])

    requests = []
    for k in assumed:
        if k in PARAM_ACQ_GUIDE:
            cn, unit, why = PARAM_ACQ_GUIDE[k]
            requests.append({"param": k, "cn": cn, "unit": unit, "why": why,
                             "value_used": cfg.get(k, _SCENE_DEFAULTS.get(k)),
                             "how": "① 实测值 ② 器件型号（AI 检索规格书）③ 引导现场标定 ④ 保留默认并注明"})

    # 澄清问题：把缺省参数转成带选项的问句，供 AI 主动向用户提问
    _used = {k: (scene[k] if k in scene else cfg.get(k)) for k in assumed}
    _clarify_qs = build_clarify_questions(species, wn_center, assumed, _used)

    out = {"species": m["species"], "wn_center_cm-1": float(wn_center),
           "mixture": (m.get("mixture") if mixture is not None else None),
           "mole_frac": (None if mixture is not None else float(cfg["x"])),
           "condition_advice": adaptive_condition(species, wn_center),
           "scan_window_cm-1": [round(float(r["nu_axis"].min()), 4),
                                round(float(r["nu_axis"].max()), 4)],
           # 实际生效的激光参数（含 auto_laser 重锚后的 wn_ref）——与 tdlas_das_instrument 同键
           "laser": {k: cfg[k] for k in _LASER_ECHO_KEYS},
           "wms": {"fscan_Hz": m["fscan_Hz"], "mod_freq_Hz": m["mod_freq_Hz"],
                   "mod_amp_V": m["mod_amp_V"], "auto_optimized": m["auto_mod"],
                   "mod_depth_cm-1": m["mod_depth_cm-1"], "HWHM_cm-1": m["hwhm_cm-1"],
                   "mod_coeff_m": m["mod_coeff_m"]},
           "modulation_adaptive": m["modulation"],
           "adc": {"fs_Hz": m["fs"], "n_per_scan": r["n_per"], "bits": cfg["adc_bits"],
                   "v_range_V": cfg["v_range"], "lsb_V": m["lsb_V"]},
           "normalization": {"method": m["norm_method"], "background_subtracted": m["bg_subtracted"],
                             "background_subtract_note": (
                                 "已用无吸收参考谱（τ≡1）复减 RAM/AM 基线：2f/1f 可直接用于定量。"
                                 if m["bg_subtracted"] else
                                 "⚠ **未做背景扣除（默认）**：给出的是**未经修正的原始谱**——"
                                 "2f/1f 仍含 L-I 非线性与 RAM 的基线。仅供诊断；要做浓度反演或检测限结论，"
                                 "请传 background_subtract=true 复减无吸收参考谱，或自行扣除基线。"),
                             "onef_valid": m["onef_valid"],
                             "I0_pd_V": m["I0_pd_V"],
                             "offband_leak_1f": m["offband_leak_1f"],
                             "peak_2f_1f": m["peak_2f_1f"],
                             "trim_frac": m["trim_frac"], "edge": m["edge"],
                             "note": "2f/1f 的前提是 1f∝光强；但 1f 实际∝L-I 斜率，"
                                     "非吸收区泄漏超峰值 30% 即判失效，自动退化为 2f/I0"},
           "results": {"alpha_L_peak": m["alpha_L_peak"],
                       "S2f_norm_peak": float(np.abs(r["S2f_norm_cyc"])[valid_i].max())
                       if valid_i.any() else None,
                       "S2f_norm_peak_note": "**这就是 tdlas_invert 的 peak_2f1f 应传的量**："
                                             "sensitivity_k 的分子就是本值（k = S2f_norm_peak / x），"
                                             "故 x = S2f_norm_peak / k 精确成立",
                       "S2f1f_peak": float(s2f1f[kpk]),
                       "S2f1f_peak_interval": {
                           "region": "valid_mask（已剔除扫描两端 trim_frac 的转折点区）",
                           "trim_frac": m["trim_frac"], "n_points_used": int(valid_i.sum()),
                           "n_points_total": int(valid_i.size),
                           "full_window_peak_in_trimmed_edge": bool(int(s2f1f.argmax()) != kpk),
                           "note": "与 sensitivity_k 同区间取峰。⚠ 但配反演用的量是 S2f_norm_peak，"
                                   "**不是**本值：只有归一化方法为 2f/1f 时二者才相等，"
                                   "1f 失效自动退化为 2f/I0 时二者不同"},
                       "sensitivity_k": (m["sensitivity_k"] if mixture is None else None),
                       "sensitivity_note": ("sensitivity_k = S2f_norm_peak / x（完整链路）；"
                                            "tdlas_invert 的 k 传本值、peak_2f1f 传 S2f_norm_peak，"
                                            "二者成对（勿用解析版灵敏度）"
                                            if mixture is None else
                                            "混合气下「单物种浓度 x」无定义 → sensitivity_k 不给出"
                                            "（=null，不做无据外推）；反演请按组分逐一建模或改用 DAS 全谱拟合"),
                       "S2f1f_peak_nu_cm-1": float(r["nu_axis"][kpk]),
                       # ★ 与 S2f_norm_peak **同工况配套**的两条数组，供
                       #   tdlas_invert(spectrum=, ref_spectrum=) 做谱线级最小二乘。
                       #   约定：ref = S2f_norm_cyc / x（单位浓度谱形 k₁），两者同长同掩膜。
                       "S2f_norm_spectrum": [float(v) for v in np.abs(r["S2f_norm_cyc"])[valid_i]],
                       "k1_spectrum_per_unit_x": [
                           float(v) / float(scene["x"] if scene.get("x") else 1.0)
                           for v in np.abs(r["S2f_norm_cyc"])[valid_i]],
                       "spectrum_note": ("S2f_norm_spectrum 是前景谱（含测试浓度）；"
                                         "k1_spectrum_per_unit_x = 同谱 / x，即单位浓度谱形。"
                                         "把它们作为 tdlas_invert 的 spectrum / ref_spectrum 传入即可"
                                         "做最小二乘（因为满足 x = peak / k 的同源要求）。"),
                       "v_pd_mean_V": m["v_pd_mean"], "saturated_points": m["n_sat"],
                       "noise_breakdown_mV": {"shot": m["sigma_shot"] * 1e3,
                                              "thermal": m["sigma_thermal"] * 1e3,
                                              "rin_white": m["sigma_rin"] * 1e3},
                       "noise_1f": {"drift_frac": m["drift_frac"],
                                    "flicker_frac": m["flicker_frac"]},
                       "noise_disclosure": "默认未加噪声（理想仿真）。如需注入：rin=相对强度噪声，"
                                           "drift_frac=1/f 慢漂移，flicker_frac=1/f 粉红。散粒/热为物理固有（极小）。",
                       "n_lines_in_window": m["n_lines_in_window"], "table": m["table"],
                       "next_suggestions": generate_next_suggestions(
                           {"alpha_L_peak": float(m["alpha_L_peak"]),
                            "noise_breakdown_mV": {"shot": float(m["sigma_shot"]) * 1e3,
                                                   "thermal": float(m["sigma_thermal"]) * 1e3,
                                                   "rin_white": float(m["sigma_rin"]) * 1e3}},
                           {"L_cm": float(cfg["L_cm"]), "x": float(cfg["x"])})},
           "validation": ts.validate_wms_result(r),
           # 实际生效的工况：assumptions 只说"哪些用了默认"，数值必须一起回显，
           # 否则 AI 只能写"用了默认值"却报不出数值（= 拿不到数据）
           "conditions": {"T_K": float(cfg["T"]), "P_atm": float(cfg["P"]),
                          "x": (None if mixture is not None else float(cfg["x"])),
                          "L_cm": float(cfg["L_cm"])},
           "assumptions": assumed, "param_requests": requests,
           "needs_input": bool(requests),
           "clarify": {"needed": bool(requests),
                       "instruction": "needed=True 时：**先向用户提出 questions 里的澄清问题，"
                                      "收到回答前不要直接出图/下结论**。用户明确说'用默认值'才可跳过。"
                                      "**用原生结构化提问工具（AskUserQuestion 类点击式选择框）渲染，"
                                      "禁止纯文字列表**；1 次 1–4 题，超过 4 题按优先级分批多轮。",
                       "questions": _clarify_qs},
           "ai_guidance": {"role": AI_INTERACTION_GUIDE["role"],
                           "priority": AI_INTERACTION_GUIDE["priority"],
                           "datasheet_lookup": AI_INTERACTION_GUIDE["datasheet_lookup"],
                           "sop": AI_INTERACTION_GUIDE["sop"],
                           "image_layout": AI_INTERACTION_GUIDE["image_layout"],
                           "das_vs_wms": AI_INTERACTION_GUIDE["das_vs_wms"],
                           "edge_definition": AI_INTERACTION_GUIDE["edge_definition"],
                           "pd_baseline_slope": AI_INTERACTION_GUIDE["pd_baseline_slope"],
                           "noise_and_interaction": AI_INTERACTION_GUIDE["noise_and_interaction"],
                           "condition_adaptation": AI_INTERACTION_GUIDE["condition_adaptation"],
                           "clarify_protocol": AI_INTERACTION_GUIDE["clarify_protocol"],
                           "das_baseline_method": AI_INTERACTION_GUIDE["das_baseline_method"],
                           "alpha_reporting": AI_INTERACTION_GUIDE["alpha_reporting"],
                           "fringe_reporting": AI_INTERACTION_GUIDE["fringe_reporting"],
                           "fidelity_reporting": AI_INTERACTION_GUIDE["fidelity_reporting"],
                           "next_step": "若 param_requests 非空：先向用户索取实测值或器件型号；"
                                        "保留默认时须在结论中标注；alpha_report.needs_report=True 时"
                                        "按 alpha_reporting 播报 α 语境。",
                           "must_disclose": ["① 本次用了哪些噪声（白噪声散粒/热/RIN + 1/f 漂移/粉红，见 noise_disclosure）",
                                             "② 哪些参数用了默认值",
                                             "③ 归一化方法 normalization.method",
                                             "④ 谱线是否孤立（n_lines_in_window）",
                                             "⑤ 当 alpha_report.needs_report=True 时：α 峰值须连同 "
                                             "T/P/step/wingHW/窗口 一并给出",
                                             "⑥ 当 fringe_report.needs_report=True 时：说明 etalon 条纹的 FSR/对比度、"
                                             "是否会在 2f 上伪造吸收、以及该采取的物理措施",
                                             "⑦ 当 laser_auto_aligned 非空时：必须说明本次 wn_ref 是**自动重锚**的"
                                             "理想激光器假设（不是你手上的器件），并提示用户提供实测 "
                                             "wn_ref/dν/dI，或用 tdlas_device 保存真实激光器后以 laser= 引用",
                                             "⑧ 给出任何定量结论时：按 tdlas_guide 的 fidelity_reporting 与其中的 "
                                             "fidelity 台账，说明**哪些效应已建模、哪些未实现**；"
                                             "未实现项可能主导真实误差，点值≠带误差结果",
                                             "⑨ 未做背景扣除（`normalization.background_subtracted=false`，**默认**）"
                                             "时：必须说明给出的是**未经修正的原始谱**——2f/1f 仍含 L-I 非线性与 RAM 基线，"
                                             "仅供诊断；要出浓度/检测限结论须先开 background_subtract 或自行扣除基线"],
                           "interaction_rule": "MCP 必须**多与用户交互**：主动告知以上 must_disclose 各项，"
                                               "并在解读结果前先向用户确认工况（物种/波段/T/P/浓度/光程）。"},
           "seed_used": m.get("seed_used"), "physical_noise": m.get("physical_noise", True),
           "warnings": ([_note] if _note else []) + ([_dark_note] if _dark_note else []) + m["warnings"],
           "edge_note": EDGE_TECH_NOTE,
           "log": _sanitize_paths(g.getvalue()).splitlines()}
    if laser_info:
        out["laser_auto_aligned"] = laser_info
    if ignored:
        out["ignored_params"] = ignored
        out["ignored_note"] = (f"以下参数不属于本工具、已被忽略（疑似拼写错误）：{ignored}；"
                               f"允许的键见 tools/list 的 inputSchema")
    out["alpha_report"] = _alpha_report_block(m.get("alpha_context"), species, session_id)
    out["fringe_report"] = _fringe_report_block(m.get("fringe"), session_id)
    if _multi:
        # ★ 多浓度必须共用**同一调制深度**：各浓度的谱形相同（只差一个缩放），最优 m 本应
        #   一致；但自适应是在逐次扫描里挑峰的，注入噪声后每次可能选出略不同的 m，
        #   于是"浓度 ×2、2f 却 ×2.07"——幅值对比被调制差异污染，看不出真实线性度。
        #   故自适应只在基准浓度跑一次，其余浓度复用它的 m（与 measurement_uncertainty
        #   的"重复工况复用首次 m"同一手法）。用户显式给了 m_opt / mod_amp_V 时不介入。
        _mod_locked = None
        _norm_locked = None
        _inst_lock = dict(inst)
        if inst.get("mod_amp_V") is None and str(inst.get("m_opt", "auto")).strip().lower() == "auto":
            try:
                _mod_locked = float(r["meta"]["mod_coeff_m"])
                _inst_lock["m_opt"] = _mod_locked
            except Exception:                              # noqa: BLE001
                _mod_locked = None
        # 归一化方法同样要锁定：2f/1f 是否可用靠"非吸收区泄漏 ≤30% 峰值"判，噪声下
        # 这条判据会随浓度翻转 → 一个浓度 2f/1f、另一个 2f/I0，量级差两个数量级，
        # 叠加图作废。故统一用基准浓度判出来的方法（用户显式给 norm_lock 时不介入）。
        if inst.get("norm_lock") is None:
            _nm0 = str(r["meta"].get("norm_method") or "")
            _norm_locked = "2f/1f" if _nm0.startswith("2f/1f") else "2f/I0"
            _inst_lock["norm_lock"] = _norm_locked
        _samples = [r] + [
            ts.simulate_wms_instrument(species, wn_center=float(wn_center),
                                       T=float(scene["T"]), P=float(scene["P"]),
                                       x=float(xi), L_cm=float(scene["L_cm"]),
                                       seed=seed, **_inst_lock)
            for xi in _xs[1:]]

        def _wms_metrics(rr):
            _vi = rr["valid_mask"]
            _s = np.abs(rr["S2f_norm_cyc"])
            return {"S2f_norm_peak": (float(_s[_vi].max()) if _vi.any() else None),
                    "alpha_L_peak": rr["meta"]["alpha_L_peak"],
                    "sensitivity_k": rr["meta"]["sensitivity_k"]}
        out["multi_conc"] = _build_multi_conc(
            _xs, _samples, ["S2f_norm_peak", "alpha_L_peak", "sensitivity_k"], _wms_metrics)
        if _mod_locked is not None:
            out["multi_conc"]["modulation_locked"] = {
                "m_opt": _mod_locked,
                "mod_amp_V": float(r["meta"].get("mod_amp_V") or 0.0),
                "note": "多浓度共用同一调制深度：自适应只在基准浓度算一次，其余浓度复用该 m。"
                        "（各浓度谱形只差一个缩放，最优 m 本应一致；不锁定时噪声会让每次自适应"
                        "选出略不同的 m，幅值对比被调制差异污染）"}
            out["warnings"].append(
                f"多浓度已锁定调制系数 m={_mod_locked:g}（自适应只在基准浓度跑一次），"
                f"保证各浓度幅值可比")
        if _norm_locked is not None:
            out["multi_conc"]["normalization_locked"] = {
                "method": _norm_locked,
                "note": "多浓度共用同一归一化方法（由基准浓度判定后锁定）：2f/1f 是否可用靠"
                        "「非吸收区泄漏 ≤30% 峰值」判，噪声下该判据可能随浓度翻转，"
                        "不锁定会出现一个浓度 2f/1f、另一个 2f/I0（量级差两个数量级），叠加图作废"}
            out["warnings"].append(
                f"多浓度已锁定归一化方法 = {_norm_locked}（各浓度统一，保证纵轴可比）")
        if save_png:                                   # 多浓度同图：叠加归一化 2f/1f（含有效区着色）
            OUT_DIR.mkdir(parents=True, exist_ok=True)
            _ov = OUT_DIR / f"wms_instr_{_safe_name(str(species).upper())}_{float(wn_center):g}cm-1_xlist_overlay.png"
            ts.plot_multi_x(str(_ov), [xi * 1e6 for xi in _xs],
                            [np.abs(s["S2f_norm_cyc"]) for s in _samples],
                            _samples[0]["nu_axis"],
                            r"波数 (cm$^{-1}$)", "归一化 2f/1f (a.u.)",
                            f"{str(species).upper()} 多浓度 WMS 2f/1f",
                            valid=[s["valid_mask"] for s in _samples])
            out["png_overlay"] = str(_ov)
    if mixture is not None and r.get("alphaL_per_species"):
        # 各组分对总吸收的贡献（供 AI 说明"谁主导"）；混合气纪律见返回 note
        out["mixture_breakdown"] = {
            "note": "α = Σ x_i·α_pure_i(T, P, 空气浴)：每个组分单独在 T/P 下按空气展宽算 α_pure，"
                    "再乘自己的摩尔分数后相加；**绝不可把分压设成 x·P**（那会算错窄线宽）。"
                    "N2/O2/Ar 在 3.3 μm 无吸收线，只贡献碰撞展宽（已含在 HITRAN γ_air 内），"
                    "不作为吸收组分叠加。",
            "per_species": [{"label": p["label"], "alphaL_peak": float(np.max(p["alphaL"]))}
                            for p in r["alphaL_per_species"]],
            "alphaL_total_peak": m["alpha_L_peak"]}
    out["fidelity"] = _fidelity_digest()
    out["interaction"], out["next_required_actions"] = _interaction_block(out, "wms", session_id)
    if save_png:
        OUT_DIR.mkdir(parents=True, exist_ok=True)
        png = OUT_DIR / f"wms_instr_{_safe_name(str(species).upper())}_{float(wn_center):g}cm-1.png"
        # 多浓度时标准 6 子图本身按浓度叠加（viridis + ppm 图例）；单浓度保持原样
        _multi_draw = list(zip([xi * 1e6 for xi in _xs], _samples)) if _multi else None
        with _quiet():
            ts.plot_wms_instrument(r, png, multi=_multi_draw)
        out["png"] = str(png)
        # 出图自检：机器检查图片质量，AI 不用读图也能知道有没有问题
        out["figure_check"] = _figure_check(png, r, _multi_draw)
    if return_xy and "X1f_c" in r:                  # BUG-D 修复：透出锁相正交 X/Y 分量
        # ⚠ 必须转 list：这些是 numpy ndarray，直接入返回体会在 JSON-RPC
        #   序列化时抛 TypeError（整次调用失败）—— Python 直接调用不会暴露，
        #   故必须在这里转成可序列化的纯量。
        out["wms_raw_xy"] = {k: [float(v) for v in r[k]]
                             for k in ("X1f_c", "Y1f_c", "X2f_c", "Y2f_c",
                                       "X1f_bg_c", "Y1f_bg_c", "X2f_bg_c", "Y2f_bg_c")}
        out["wms_raw_xy_note"] = ("锁相正交分量（信号支路 X/Y 与无吸收参考谱 X/Y_bg），"
                                  "是背景扣除的输入：2f/1f 复减 = X2f_c/X1f_c − X2f_bg_c/X1f_bg_c。")
    # 凭据块：让本结果可被第三方重建（引擎版本 + 线表指纹 + 输入指纹）
    # 设备来源：回显本次用的硬件参数来自哪里（datasheet/user/ai/...）
    _dprov = _device_provenance(setup, laser, pd, daq, optics)
    if _dprov:
        out["device_provenance"] = _dprov
    out.setdefault("provenance", provenance_block(out, m.get("table")))
    return out


def t_review(species="CH4", wn_center=2968.5, T=None, P=None, x=None, L_cm=None,
             seed=0, session_id="default", x_list=None, return_xy=False, **kw):
    """二次审核 / 多轮核对：跑一遍 WMS 并只返回自动校验报告（不画图）。

    用于在给出最终结论前，对结果做独立复核；非专业用户可据此判断"是否可信"。
    返回 validation（overall + 逐项 checks）+ warnings + 关键指标。
    参数与 tdlas_wms_instrument **完全同源**（实现上就是透传）。
    """
    out = t_wms_instrument(species=species, wn_center=wn_center, T=T, P=P, x=x, L_cm=L_cm,
                           seed=seed, session_id=session_id, save_png=False,
                           x_list=x_list, return_xy=bool(return_xy), **kw)
    _rev = {"species": out["species"], "wn_center_cm-1": out["wn_center_cm-1"],
            "validation": out["validation"], "warnings": out["warnings"],
            "key_metrics": {"alpha_L_peak": out["results"]["alpha_L_peak"],
                            "n_lines_in_window": out["results"]["n_lines_in_window"],
                            "norm_method": out["normalization"]["method"],
                            "scan_window_cm-1": out["scan_window_cm-1"]},
            "alpha_report": out.get("alpha_report"),
            "fringe_report": out.get("fringe_report"),
            # ★ 复核工具是最需要"先确认工况再下结论"的一环，此前它连 assumptions 都不返回
            #   （交互相对于 wms 是残缺的）→ 这里补齐同源交互字段：
            "assumptions": out.get("assumptions") or [],
            "param_requests": out.get("param_requests") or [],
            "clarify": out.get("clarify"),
            "fidelity": out.get("fidelity"),
            "suggestion": "若 validation.overall != 'pass'，先处理 fail 项再下结论；"
                          "warn 项须向用户说明。"}
    _rev["interaction"], _rev["next_required_actions"] = _interaction_block(_rev, "review",
                                                                           session_id)
    # 凭据块：让本结果可被第三方重建（引擎版本 + 线表指纹 + 输入指纹）
    _rev["provenance"] = provenance_block(out, (out.get("results") or {}).get("table"))
    return _rev


def t_session(action="view", session_id="default", **fields):
    """对话状态机：跨会话记住用户已确认的工况参数。

    action:
      view  — 查看当前会话状态（已确认参数 / 待确认 / 阶段）
      set   — 记录已确认的参数（fields 为键值，None 值跳过）
      reset — 清空该会话
    持久化在仓库根目录 .tdlas_session.json（隐藏文件，中性命名），MCP 重启/换会话仍保留。
    """
    sessions = _load_sessions()
    sess = sessions.get(session_id, {"confirmed": {}, "pending": [], "stage": "clarify"})

    if action == "view":
        return {"session_id": session_id, **sess}
    if action == "reset":
        sessions.pop(session_id, None)
        _save_sessions(sessions)
        return {"session_id": session_id, "reset": True}
    if action == "set":
        for k, v in fields.items():
            if v is not None and k != "session_id":
                sess["confirmed"][k] = v
        sess["stage"] = "confirmed" if sess["confirmed"] else "clarify"
        sessions[session_id] = sess
        _save_sessions(sessions)
        return {"session_id": session_id,
                "confirmed": sess["confirmed"],
                "stage": sess["stage"],
                "hint": "confirmed 参数会在后续 tdlas_wms_instrument 中自动复用"}
    return {"error": f"unknown action {action!r}"}


def t_device(action="list", device_type=None, name=None,
             laser=None, pd=None, daq=None, optics=None, **params):
    """设备库统一管理：把固定的仪器（激光器/探测器/采集卡/光学）命名保存，
    仿真工具里用 setup= / laser= / pd= / daq= / optics= 直接引用，省去每次手填硬件参数。

    action:
      list        列出所有设备（按类别）+ 整机配置 + 默认设备
      save        保存/更新一个设备（device_type: laser/pd/daq/optics；name；其余参数键值）
      view        查看某个设备
      delete      删除某个设备（同步清理整机配置与默认里的引用）
      set_default 设某类设备的默认（不指定设备时自动用默认）
      save_setup  打包整机配置（name + laser/pd/daq/optics 各给设备名）
    持久化在仓库根目录 .tdlas_devices.json，MCP 重启/换会话仍保留。
    """
    dev = _load_devices()

    if action == "list":
        def _brief(k, v):
            keys_map = {"laser": ["wn_ref", "eta_VI", "dnu_dI", "i_th"],
                        "pd": ["resp", "gain", "bw"],
                        "daq": ["fs", "adc_bits", "v_range"],
                        "optics": ["throughput"]}
            keys = keys_map.get(k, [])
            return {kk: v[kk] for kk in keys if kk in v}

        devices_brief = {}
        for k in ("laser", "pd", "daq", "optics"):
            devices_brief[k] = {n: _brief(k, v) for n, v in dev.get(k, {}).items()}

        setups_brief = {}
        for n, st in dev.get("setup", {}).items():
            setups_brief[n] = {
                "laser": st.get("laser"),
                "pd": st.get("pd"),
                "daq": st.get("daq"),
                "optics": st.get("optics"),
                "conditions": st.get("conditions", {}),
            }

        return {
            "summary": {
                "n_laser": len(dev.get("laser", {})),
                "n_pd": len(dev.get("pd", {})),
                "n_daq": len(dev.get("daq", {})),
                "n_optics": len(dev.get("optics", {})),
                "n_setup": len(dev.get("setup", {})),
                "default_setup": dev.get("default", {}).get("setup"),
            },
            "devices": devices_brief,
            "setups": setups_brief,
            "default": dict(dev.get("default", {})),
            "hint": "用 view_setup name=xxx 看完整工况参数；用 set_default_setup name=xxx 设默认",
        }

    if action == "view":
        dt, nm = str(device_type), str(name)
        return {dt: {nm: dev.get(dt, {}).get(nm)}}

    if action == "delete":
        dt, nm = str(device_type), str(name)
        if dt in dev and nm in dev[dt]:
            del dev[dt][nm]
            for st in dev.get("setup", {}).values():          # 清理整机配置里的引用
                for k in list(st):
                    if st[k] == nm:
                        st[k] = None
            for k in list(dev.get("default", {})):            # 清理默认引用
                if dev["default"][k] == nm:
                    del dev["default"][k]
            _save_devices(dev)
            return {"deleted": f"{dt}:{nm}"}
        return {"error": f"设备 {nm!r} 不存在（类型 {dt}）"}

    if action == "save":
        dt = str(device_type)
        if dt not in _DEVICE_TYPES:
            return {"error": f"device_type 必须是 {list(_DEVICE_TYPES)} 之一，收到 {dt!r}"}
        nm = str(name or "").strip()
        if not nm:
            return {"error": "save 需要非空的设备名 name"}
        if len(nm) > 64:
            return {"error": f"设备名过长（{len(nm)} 字符，限 64）"}
        _ok_nm, _why = _name_is_distinctive(nm, dt)
        if not _ok_nm:
            _sug = _suggest_setup_name(dev, **{dt: nm})
            return {"error": f"设备名不够显著：{_why}",
                    "why_it_matters": "设备名是以后引用时**唯一看得到的东西**；"
                                      "名字分不出来，用户与 AI 都选不对设备。",
                    "suggested_names": [n for n in (
                        _sug, f"{dt}_{nm}", f"{nm}_SN0001") if n and len(n) >= 3]}
        entry = {k: params[k] for k in _DEVICE_TYPES[dt] if k in params and params[k] is not None}
        if not entry:
            return {"error": f"未提供任何可用参数；{_DEVICE_TYPE_CN[dt]}允许的键："
                             f"{_DEVICE_TYPES[dt]}"}
        bad = _validate_device_entry(entry)
        if bad:
            return {"error": f"设备 {dt}:{nm} 参数非法：{bad}；"
                             f"{_DEVICE_TYPE_CN[dt]}允许的键：{_DEVICE_TYPES[dt]}"}
        _prev = dev.get(dt, {}).get(nm) or {}
        # ★ 来源追踪：用户会把规格书/说明书/混杂信息交给 AI，AI 负责
        #   构建仪器库。若不记来源，三个月后无人能判断某个 dnu_dI
        #   是**查到的**还是**AI 推的** —— 而这正是 AI 幻觉最容易藏身的地方。
        #   不给 source 时记为 "unspecified"（而非默认成 datasheet），
        #   避免把"不知道哪来的"包装成"有出处"。
        _src = str(params.get("source") or _prev.get("source") or "unspecified").strip()
        if _src not in ("datasheet", "user", "ai", "calibration", "unspecified"):
            _src = "unspecified"
        _note = params.get("source_note") or _prev.get("source_note")
        entry = dict(entry)
        entry["source"] = _src
        if _note:
            entry["source_note"] = str(_note)[:200]
        entry["fields"] = sorted(k for k in _DEVICE_TYPES[dt] if k in params and params[k] is not None) \
            or sorted(k for k in _prev.get("fields", []) if k in _DEVICE_TYPES[dt])
        entry["updated"] = _dt.datetime.now().strftime("%Y-%m-%d")
        dev.setdefault(dt, {})[nm] = entry
        _save_devices(dev)
        out = {"saved": f"{dt}:{nm}", "params": entry,
               "hint": f"引用：{dt}={nm!r}（或打包进 setup 后 setup= 一键加载）"}
        ignored = sorted(k for k in params if k not in _DEVICE_TYPES[dt])
        if ignored:
            out["ignored_params"] = ignored
            out["ignored_note"] = (f"以下键不属于{_DEVICE_TYPE_CN[dt]}、未保存（疑似拼写错误）："
                                   f"{ignored}；允许的键：{_DEVICE_TYPES[dt]}")
        return out

    if action == "set_default":
        dt, nm = str(device_type), str(name)
        if dt not in _DEVICE_TYPES:
            return {"error": f"device_type 必须是 {list(_DEVICE_TYPES)} 之一"}
        if nm not in dev.get(dt, {}):
            return {"error": f"设备 {nm!r} 不存在（类型 {dt}），请先 save"}
        dev.setdefault("default", {})[dt] = nm
        _save_devices(dev)
        return {"default": dict(dev["default"])}

    if action == "set_default_setup":
        """设默认 setup：不传 setup= 时自动用这个"""
        nm = str(name or "").strip()
        if not nm:
            dev["default"].pop("setup", None)
            _save_devices(dev)
            return {"default_setup": None, "hint": "已清除默认 setup"}
        if nm not in dev.get("setup", {}):
            _exist = sorted((dev.get("setup") or {}).keys())
            return {"error": f"setup {nm!r} 不存在",
                    "available": _exist,
                    "hint": "请先 save_setup 打包"}
        dev.setdefault("default", {})["setup"] = nm
        _save_devices(dev)
        return {"default_setup": nm, "hint": f"以后不传 setup= 时自动用 {nm!r}"}

    if action == "view_setup":
        """查看某个 setup 的完整内容（设备 + 工况）"""
        nm = str(name or "").strip()
        st = dev.get("setup", {}).get(nm)
        if not st:
            _exist = sorted((dev.get("setup") or {}).keys())
            return {"error": f"setup {nm!r} 不存在", "available": _exist}
        return {"setup": nm, "content": st}

    if action == "save_setup":
        st = {k: v for k, v in {"laser": laser, "pd": pd, "daq": daq, "optics": optics}.items() if v}
        # ① 缺设备：不只报错，而是告诉 AI **该向用户问什么**
        guide = _device_setup_guide(dev, laser, pd, daq, optics)
        if guide["missing"]:
            return {"error": "打包整机失败：有设备尚未建库",
                    "need_devices": guide["missing"],
                    "available": guide["available"],
                    "ai_instruction": guide["ai_instruction"]}
        if not st:
            return {"error": "save_setup 需要至少一台设备（laser/pd/daq/optics）"}
        # ② 名字：未给或不够显著时，**AI 自动拼一个显著名**（用户不需要想名字）
        nm = str(name or "").strip()
        auto_named = False
        if not nm:
            nm = _suggest_setup_name(dev, laser, pd, daq, optics)
            auto_named = True
        _ok_nm, _why = _name_is_distinctive(nm)
        if not _ok_nm:
            _sug = _suggest_setup_name(dev, laser, pd, daq, optics)
            return {"error": f"整机名不够显著：{_why}",
                    "why_it_matters": "整机名是用户以后选设备时**唯一看得到的东西**；"
                                      "名字分不出来，会选错设备。",
                    "suggested_names": [n for n in (_sug, f"{nm}_2026") if n and len(n) >= 3]}
        if nm in (dev.get("setup") or {}):
            return {"error": f"整机名 {nm!r} 已存在（避免覆盖已有配置）",
                    "hint": "请换一个显著且不同的名字，或先用 action=view 查看已有配置"}
        full = {"laser": st.get("laser"), "pd": st.get("pd"),
                "daq": st.get("daq"), "optics": st.get("optics")}
        # ★ 工况参数打包
        _COND_PARAMS = ("T", "P", "L_cm", "wn_center", "mod_freq_Hz", "freq_Hz",
                        "drift_frac", "flicker_frac", "scan_span_cm", "am_i0")
        cond = {k: params.get(k) for k in _COND_PARAMS if params.get(k) is not None}
        if cond:
            full["conditions"] = cond
        dev.setdefault("setup", {})[nm] = full
        _save_devices(dev)
        out = {"saved_setup": nm, "setup": full, "hint": f"引用：setup={nm!r}"}
        if cond:
            out["conditions_saved"] = list(cond.keys())
            out["conditions_hint"] = "工况参数已打包进整机配置，加载 setup 时自动套用（显式传参仍优先）"
        if auto_named:
            out["name_auto_generated"] = True
            out["name_note"] = ("未给合适整机名，已按设备名自动拼接。"
                                "若你有更好的名字（如“实验室A_多通池10m”），"
                                "可用同名重存或换名重建。")
        return out

    return {"error": f"unknown action {action!r}"}


def _fidelity_summary():
    """保真度审计（能力登记表）的结构化快照。

    真源是 ``tools/tdlas_fidelity.py`` 的 ``EFFECTS``；此处只做转发，不复制内容，
    避免"表漂了"这一本项目已踩过两次的坑。模块不可用时降级为最小披露规则，不阻断 guide。
    """
    try:
        import tdlas_fidelity as F
        return F.fidelity_summary()
    except Exception as exc:                     # pragma: no cover - 仅作兜底
        return {"unavailable": f"{type(exc).__name__}: {exc}",
                "disclosure_rule": "必须说明本次哪些效应已建模、哪些未实现；点值≠带误差结果。"}


def t_guide(topic=None):
    """返回 AI 主动指导协议（面向实验新手）：参数索取优先级、交互流程、术语表、参数索取指南。"""
    out = dict(AI_INTERACTION_GUIDE)
    out["param_guide"] = {k: {"cn": v[0], "unit": v[1], "why": v[2]}
                          for k, v in PARAM_ACQ_GUIDE.items()}
    out["fidelity"] = _fidelity_summary()
    if topic:
        t = str(topic).upper()
        out["glossary_hit"] = {k: v for k, v in AI_INTERACTION_GUIDE["glossary"].items()
                               if t in k.upper() or t in str(v).upper()}
    return out




def t_export(data, filename, format="csv"):
    """导出仿真结果到文件（CSV 或 JSON）。

    data     : 要导出的数据（dict 或 list of dict）
    filename : 输出文件名（不含路径，自动存到 tmp/mcp_out/）
    format   : "csv" 或 "json"
    """
    import json
    import csv

    out_dir = OUT_DIR
    out_dir.mkdir(parents=True, exist_ok=True)

    # 清洗文件名（防路径注入）
    safe_name = "".join(c for c in filename if c.isalnum() or c in "._-")
    if not safe_name.endswith(f".{format}"):
        safe_name += f".{format}"

    out_path = out_dir / safe_name

    if format == "json":
        with open(out_path, 'w', encoding='utf-8') as f:
            json.dump(data, f, ensure_ascii=False, indent=2, default=str)
    elif format == "csv":
        if isinstance(data, dict):
            # 单个 dict → 一行
            with open(out_path, 'w', newline='', encoding='utf-8-sig') as f:
                writer = csv.writer(f)
                writer.writerow(data.keys())
                writer.writerow(data.values())
        elif isinstance(data, list):
            # list of dict → 多行
            keys = data[0].keys() if data else []
            with open(out_path, 'w', newline='', encoding='utf-8-sig') as f:
                writer = csv.DictWriter(f, fieldnames=keys)
                writer.writeheader()
                writer.writerows(data)
        else:
            raise ValueError("data 必须是 dict 或 list of dict")
    else:
        raise ValueError(f"不支持的格式：{format}（只支持 csv/json）")

    return {"file": str(out_path), "format": format, "rows": len(data) if isinstance(data, list) else 1}


def t_allan(signal, fs, max_tau=None, tau_points=100, save_png=False):
    """Allan 方差分析（重叠版 OA-VAR）：给定时序信号，求最优平均时间与噪声类型诊断。"""
    from allan import overlapping_allan, allan_noise_diag
    import numpy as np

    s = np.asarray(signal, dtype=float)
    result = overlapping_allan(s, fs=float(fs), max_tau=max_tau, tau_points=int(tau_points))
    diag = allan_noise_diag(result)

    # τ=1s 时的噪声水平（白噪声底）
    idx_1s = int(np.argmin(np.abs(result["taus"] - 1.0)))
    sigma_1s = float(result["adev"][idx_1s])

    out = {
        "method": "OA-VAR (overlapping)",
        "N": int(result["N"]),
        "fs_Hz": float(result["fs"]),
        "tau_opt_s": float(result["tau_opt"]),
        "sigma_opt": float(result["adev_opt"]),        # 1σ at optimal averaging
        "LOD_3sigma": float(3.0 * result["adev_opt"]),  # 3σ detection limit
        "sigma_1s": sigma_1s,                           # 1σ at τ=1s (white noise floor)
        "noise_diagnosis": diag,
        "reference": "Werle et al. 1993, 2011",
    }

    if save_png:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        from matplotlib import font_manager
        for f in ["Microsoft YaHei", "SimHei"]:
            try:
                font_manager.findfont(f, fallback_to_default=False)
                plt.rcParams["font.family"] = f
                break
            except Exception:
                pass
        plt.rcParams["axes.unicode_minus"] = False

        t_axis = np.arange(result["N"]) * result["dt"]
        fig, axes = plt.subplots(2, 1, figsize=(11, 7.5))
        fig.subplots_adjust(hspace=0.38)

        axes[0].plot(t_axis, s, lw=0.8, color="#1f77b4")
        axes[0].set_title(f"时序信号（N={result['N']}, fs={result['fs']} Hz）")
        axes[0].set_xlabel("时间 t (s)")
        axes[0].set_ylabel("信号")
        axes[0].grid(alpha=0.2)

        axes[1].loglog(result["taus"], result["adev"], "o-", ms=4, lw=1.2,
                       color="#d62728", label="重叠 Allan 偏差 σ(τ)")
        t0 = result["taus"][1]
        s0 = result["adev"][1]
        axes[1].loglog(result["taus"], s0 * (result["taus"] / t0) ** (-0.5),
                       "--", color="gray", lw=0.8, label="白噪声 斜率 -1/2")
        axes[1].axvline(result["tau_opt"], color="green", ls="--", lw=1.2)
        axes[1].scatter([result["tau_opt"]], [result["adev_opt"]], s=80, color="green", zorder=5)
        axes[1].text(result["tau_opt"], result["adev_opt"] * 1.4,
                     f"τ_opt={result['tau_opt']:.1f}s\nσ={result['adev_opt']:.1e}",
                     color="green", fontsize=9)
        axes[1].set_title("Allan 偏差（OA-VAR）")
        axes[1].set_xlabel("平均时间 τ (s)")
        axes[1].set_ylabel("Allan 偏差 σ(τ)")
        axes[1].legend(fontsize=9)
        axes[1].grid(alpha=0.2, which="both")

        fig.suptitle("Allan-Werle 稳定性分析", fontsize=12)
        out_path = str(OUT_DIR / "allan_analysis.png")
        fig.savefig(out_path, dpi=150, bbox_inches="tight")
        plt.close(fig)
        out["png"] = _sanitize_paths(out_path)

    return out


def t_selftest():
    """全链路自检（有线 / 2f 形状 / 弱场线性 / 反演闭环 / 检测限 / 时域交叉验证 / DAS 链路）。"""
    with _quiet() as g:
        ts.selftest()
    return {"ok": True, "log": _sanitize_paths(g.getvalue()).splitlines()}


DISPATCH = {"tdlas_simulate": t_simulate,
            "tdlas_das_chain": t_das_chain,
            "tdlas_das_instrument": t_das_instrument,
            "tdlas_wms_instrument": t_wms_instrument,
            "tdlas_review": t_review,
            "tdlas_session": t_session,
            "tdlas_device": t_device,
            "tdlas_guide": t_guide,
            "tdlas_invert": t_invert,
            "tdlas_detection_limit": t_detection_limit,
            "tdlas_detection_limit_scan": t_detection_limit_scan,
            "tdlas_calibration_curve": t_calibration_curve,
            "tdlas_export": t_export,
            "tdlas_allan": t_allan,
            "tdlas_selftest": t_selftest}

# 混合气多组分声明。四个仿真工具共用同一份定义，避免各写一遍后漂移
# （历史上 review/wms 的属性表就是这样脱节的）。
_MIXTURE_PROP = {
    "description": "混合气多组分：α = Σ x_i·α_pure_i(T, P，空气浴展宽)。"
                   "写法 \"CH4:0.01,CO2:0.04\"，或 [{\"species\":\"CH4\",\"x\":0.01}, …]。"
                   "与单物种 x 二选一，且不能与 x_list 同用；给定时 species 仅作标签。"
                   "N2/O2/Ar 无吸收线、只贡献碰撞展宽（已含在 HITRAN γ_air 内），不要写成组分。",
    "anyOf": [
        {"type": "string"},
        {"type": "array", "items": {"type": "object",
                                    "properties": {"species": {"type": "string"},
                                                   "x": {"type": "number"}},
                                    "required": ["species", "x"]}},
        {"type": "array", "items": {"type": "string"}},
    ],
}

TOOLS = [
    {"name": "tdlas_simulate",
     "description": "【快速算数值】TDLAS/WMS 解析模型：返回 DAS 透过率、1f/2f 峰高。要画图请用 tdlas_wms_instrument。"
                    "save_png=True 仅出 3 子图简图（DAS/2f/2f/1f），无仪器链路。"
                    "⚠️ 用户要画 WMS 图、要看 2f/1f 曲线、要仪器级仿真时，必须改用 tdlas_wms_instrument（标准6子图）。"
                    "本工具仅用于快速算峰高数值或解析对照。",
     "inputSchema": {"type": "object",
                     "properties": {
                         "species": {"type": "string", "description": "分子式，如 H2O / CO2 / CO"},
                         "wn0": {"type": "number", "description": "目标线中心 cm^-1"},
                         "T": {"type": "number", "description": "温度 K；缺省=会话已确认或 296"},
                         "P": {"type": "number", "description": "气压 atm；缺省=会话已确认或 1.0"},
                         "x": {"type": "number", "description": "摩尔分数 (0,1]；缺省=会话已确认或按物种推荐（强吸收如 CH4≈1e-4）"},
                         "x_list": {"type": "array", "items": {"type": "number"},
                                    "description": "多浓度扫描：摩尔分数列表 (0,1]，如 [1e-4,1e-3,1e-2]，与 x 二选一；"
                                                   "开启后返回 multi_conc 扫描曲线 + 线性/饱和防呆诊断"
                                                   "（响应/浓度 偏离线性 >10% 即提示进非线性区）。"
                                                   "防呆：空/越界/超 64 点会被拒绝，自动去重并升序；也接受 '1e-4,1e-3' 字符串。"
                                                   "save_png=true 时额外输出 png_overlay（多浓度同图，兼容性操作，用于混合气/标定对照）。"},
                         "mixture": _MIXTURE_PROP,
                         "L": {"type": "number", "description": "光程 cm；缺省=会话已确认或 30"},
                         "session_id": {"type": "string", "description": "会话标识，默认 default（复用 tdlas_session 已确认工况）"},
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
     "description": "DAS 时域链路：PD 原始信号 → 基线拟合 → 吸光度。"
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
                         "x_list": {"type": "array", "items": {"type": "number"},
                                    "description": "多浓度扫描：摩尔分数列表 (0,1]，如 [1e-4,1e-3,1e-2]，与 x 二选一；"
                                                   "开启后返回 multi_conc 扫描曲线 + 线性/饱和防呆诊断"
                                                   "（响应/浓度 偏离线性 >10% 即提示进非线性区）。"
                                                   "防呆：空/越界/超 64 点会被拒绝，自动去重并升序；也接受 '1e-4,1e-3' 字符串。"
                                                   "save_png=true 时额外输出 png_overlay（多浓度同图，兼容性操作，用于混合气/标定对照）。"},
                         "mixture": _MIXTURE_PROP,
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
    {"name": "tdlas_das_instrument",
     "description": "DAS 仪器级仿真：DAQ 电压 → 激光 → 光路 → PD → ADC。"
                    "→ PD 光电转换（响应度/跨阻增益/带宽/噪声）→ ADC 量化（位数/量程/饱和）→ 基线扣除。"
                    "默认器件：常见 16-bit DAQ + 典型中红外 DFB（V→I 24 mA/V、dν/dI −0.088 cm⁻¹/mA、"
                    "中心 2964.7 cm⁻¹）+ CH4 @2968.5 cm⁻¹。"
                    "**缺省参数的索取优先级：① 用户实测值 → ② 器件型号（AI 检索规格书）→ "
                    "③ 引导用户现场标定 → ④ 保留默认并注明。**"
                    "未给出的参数列入返回的 param_requests 与 assumptions，须先向用户澄清再解读结论。"
                    "噪声按 散粒/热/RIN 分别给出，可判断系统噪声主导来源。",
     "inputSchema": {"type": "object",
                     "properties": {
                         "species": {"type": "string", "description": "分子式，默认 CH4"},
                         "wn_center": {"type": "number", "description": "扫描中心波数 cm^-1，默认 2968.5"},
                         "T": {"type": "number", "description": "温度 K，默认 296"},
                         "P": {"type": "number", "description": "气压 atm，默认 1.01325"},
                         "x": {"type": "number", "description": "摩尔分数，默认 1e-3（1000 ppm）"},
                         "x_list": {"type": "array", "items": {"type": "number"},
                                    "description": "多浓度扫描：摩尔分数列表 (0,1]，如 [1e-4,1e-3,1e-2]，与 x 二选一；"
                                                   "开启后返回 multi_conc 扫描曲线 + 线性/饱和防呆诊断"
                                                   "（响应/浓度 偏离线性 >10% 即提示进非线性区）。"
                                                   "防呆：空/越界/超 64 点会被拒绝，自动去重并升序；也接受 '1e-4,1e-3' 字符串。"
                                                   "save_png=true 时额外输出 png_overlay（多浓度同图，兼容性操作，用于混合气/标定对照）。"},
                         "mixture": _MIXTURE_PROP,
                         "L_cm": {"type": "number", "description": "光程 cm，默认 50"},
                         "scan_span_cm": {"type": "number", "description": "三角波扫描半宽 cm⁻¹，默认 1.5（amp_V 由它自动反算，一般无需手填）"},
                         "amp_V": {"type": "number", "description": "三角波幅值 V（**通常无需手填**：由 wn_center+scan_span_cm 自动反算；需固定电压时覆盖）"},
                         "freq_Hz": {"type": "number", "description": "三角波频率 Hz，默认 100"},
                         "offset_V": {"type": "number", "description": "三角波偏置 V（**通常无需手填**：由 wn_center 自动反算）"},
                         "phase_deg": {"type": "number", "description": "三角波相位 °，默认 0"},
                         "eta_VI": {"type": "number", "description": "驱动器跨导 mA/V，默认 24"},
                         "dnu_dI": {"type": "number", "description": "激光器调谐系数 cm⁻¹/mA，默认 −0.088"},
                         "wn_ref": {"type": "number", "description": "参考波数 cm⁻¹，默认 2964.7"},
                         "i_ref": {"type": "number", "description": "参考电流 mA，默认 120"},
                         "i_th": {"type": "number", "description": "阈值电流 mA，默认 30"},
                         "eta_IP": {"type": "number", "description": "功率斜率效率 mW/mA，默认 0.15"},
                         "fs": {"type": "number",
                                "description": "采样率 Hz，默认 2.4e5（= 8×fm；常见 16-bit DAQ 上限 2.5e5）。"
                                               "必须为 fm 的整数倍且 ≥8 点/周期，否则自动吸附"},
                         "n_samples": {"type": "integer",
                                       "description": "采样点数，默认 1.2e5（= fs × 0.5 s）"},
                         "adc_bits": {"type": "integer", "description": "ADC 位数，默认 16"},
                         "v_range": {"type": "number", "description": "ADC 输入量程 ±V，默认 10"},
                         "throughput": {"type": "number", "description": "光学总透过率，默认 0.90"},
                         "resp": {"type": "number", "description": "PD 响应度 A/W，默认 0.9"},
                         "gain": {"type": "number", "description": "PD 跨阻增益 V/A，默认 700"},
                         "bw": {"type": "number", "description": "PD 带宽 Hz，默认 1e6"},
                         "rin": {"type": "number", "description": "激光 RIN 1/√Hz，默认 1e-5"},
                         "fringe": {"type": "boolean",
                                    "description": "是否启用 etalon 干涉条纹（两平行面 F-P 腔）。默认 False=理想；"
                                                   "开启后条纹按 FSR=1/(2nd) 叠加在光路上，可能伪造 2f 吸收"},
                         "fringe_n": {"type": "number", "description": "腔介质折射率，默认 1.5（空气隙 1.0）"},
                         "fringe_d_cm": {"type": "number", "description": "两平行面间距 cm，默认 0.5（5 mm）"},
                         "fringe_R": {"type": "number", "description": "单面反射率，默认 0.04（未镀膜玻璃）；AR≈0.005"},
                         "fringe_fsr": {"type": "number", "description": "自由光谱范围 cm⁻¹（给则覆盖 n/d）"},
                         "fringe_contrast": {"type": "number", "description": "条纹峰-峰对比度（给则覆盖 R）：≈4R/(1−R)²"},
                         "fringe_phase_rad": {"type": "number", "description": "条纹初相位 rad，默认 0"},
                         "fringe_drift_frac": {"type": "number",
                                               "description": "条纹相对参考谱的漂移幅度（相对 FSR）；只有漂移残留能逃过背景扣除"},
                         "edge": {"type": "string", "description": "rising(默认) / falling / average / both"},
                         "fit_order": {"type": "integer", "description": "基线多项式阶数，默认 3"},
                         "fit_frac": {"type": "number", "description": "无吸收区占比，默认 0.3"},
                         "setup": {"type": "string", "description": "整机配置名（tdlas_device save_setup 打包的 laser+pd+daq+optics 组合），一键加载硬件参数"},
                         "laser": {"type": "string", "description": "激光器设备名（tdlas_device 保存），自动套用 eta_VI/dnu_dI/wn_ref/i_th 等"},
                         "pd": {"type": "string", "description": "探测器设备名（tdlas_device 保存），自动套用 resp/gain/bw/rin"},
                         "daq": {"type": "string", "description": "采集卡设备名（tdlas_device 保存），自动套用 fs/n_samples/adc_bits/v_range"},
                         "optics": {"type": "string", "description": "光学元件设备名（tdlas_device 保存），自动套用 throughput"},
                         "seed": {"type": "integer",
                                  "description": "随机种子，默认 0（**默认结果可复现**）；传 null 则每次随机"},
                         "session_id": {"type": "string",
                                        "description": "会话标识，默认 default（复用 tdlas_session 已确认工况）"},
                         "auto_laser": {"type": "boolean",
                                        "description": "目标波数超出当前激光可达范围时，自动把 wn_ref 重锚到该波数"
                                                       "（理想激光器假设，会在 warnings/assumptions 中标注，默认 True；"
                                                       "显式给 wn_ref/offset_V 或用 laser=/setup= 引用设备时不介入）"},
                         "physical_noise": {"type": "boolean",
                                            "description": "是否注入物理固有噪声（散粒+热），默认 True；"
                                                           "False = 严格理想仿真（连散粒/热也不加，结果逐位可复现）"},
                        "allow_partial_dark": {"type": "boolean",
                                               "description": "明知扫描段有暗区（激光在该电压段未出光）仍要计算，默认 False。"
                                                              "暗区 PD 信号为 0、锁相波形被截断 → 2f/1f 失真、**定量结论不可用**；"
                                                              "仅在诊断「扫描越界后长什么样」时开启（开启后 warnings 会报出暗区占比）"},
                         "save_png": {"type": "boolean", "description": "是否出五层链路图"}},
                         "required": ["species", "wn_center"]}},
                         {"name": "tdlas_wms_instrument",
     "description": "【核心】WMS 仪器级仿真：扫描+调制 → 锁相 → 1f/2f/归一化（六子图）。",
     "inputSchema": {"type": "object",
                     "properties": {
                         "species": {"type": "string", "description": "分子式，默认 CH4"},
                         "wn_center": {"type": "number", "description": "扫描中心波数 cm^-1，默认 2968.5"},
                         "T": {"type": "number"}, "P": {"type": "number"},
                         "x": {"type": "number", "description": "摩尔分数，默认 1e-3"},
                         "x_list": {"type": "array", "items": {"type": "number"},
                                    "description": "多浓度扫描：摩尔分数列表 (0,1]，如 [1e-4,1e-3,1e-2]，与 x 二选一；"
                                                   "开启后返回 multi_conc 扫描曲线 + 线性/饱和防呆诊断"
                                                   "（响应/浓度 偏离线性 >10% 即提示进非线性区）。"
                                                   "防呆：空/越界/超 64 点会被拒绝，自动去重并升序；也接受 '1e-4,1e-3' 字符串。"
                                                   "save_png=true 时额外输出 png_overlay（多浓度同图，兼容性操作，用于混合气/标定对照）。"},
                         "mixture": _MIXTURE_PROP,
                         "return_xy": {"type": "boolean",
                                       "description": "是否返回锁相正交分量 X/Y（信号支路 X1f_c/Y1f_c/X2f_c/Y2f_c "
                                                      "与无吸收参考谱 X1f_bg_c/…）。默认 false：每次返回多 8 条等长数组，"
                                                      "会显著增大返回体。需自行做 2f/1f 复减或诊断 RAM/AM 基线时再开。"},
                         "L_cm": {"type": "number", "description": "光程 cm，默认 50"},
                         "scan_span_cm": {"type": "number", "description": "三角波扫描半宽 cm⁻¹，默认 1.5（amp_V 由它自动反算，一般无需手填）"},
                         "mod_freq_Hz": {"type": "number", "description": "正弦调制频率 Hz，默认 30000"},
                         "mod_amp_V": {"type": "number",
                                       "description": "调制幅值 V；缺省=按 m≈2.2 自动优化"},
                         "m_opt": {"description": "调制系数 m=a/HWHM；'auto'(默认)=自适应(粗扫+细扫，"
                                                   "兼顾2f幅值与轮廓，密集谱区自动选更小m)；或给数值(如2.2)禁用自适应"},
                         "lockin_avg": {"type": "integer",
                                        "description": "锁相平均调制周期数，默认 1（>1 模糊线形且不降残留）"},
                         "lockin_stages": {"type": "integer",
                                           "description": "低通级联级数，默认 2（sinc² 抑制旁瓣，非吸收区 2f 残留 4.1%→1.6%）"},
                         "background_subtract": {"type": "boolean",
                                                   "description": "是否以无吸收参考谱（τ≡1）复减 RAM/AM 基线。"
                                                                  "**默认 False**：出未经修正的原始谱（2f/1f 含 L-I 非线性与 RAM 基线），"
                                                                  "仅供诊断；用于浓度反演/检测限结论须传 true。开启后返回值会标注已扣除"},
                         "trim_frac": {"type": "number",
                                       "description": "剔除扫描两端比例，默认 0.12（三角波转折点高频谐波会泄漏进 2f）"},
                         "norm_lock": {"type": "string", "enum": ["2f/1f", "2f/I0"],
                                       "description": "强制归一化方法（跳过自动判定）：'2f/1f' 或 '2f/I0'。"
                                                      "多浓度扫描时工具会自动锁成基准浓度的方法，用户显式给则尊重其选择"},
                         "lock_kind": {"type": "string", "enum": ["boxcar", "butter"],
                                       "description": "锁相低通实现：'boxcar'（默认，矩形窗滑动平均，零点精确落在 fm 整数倍 → 2fm/4fm 数值零泄漏）"
                                                      "或 'butter'（scipy Butterworth，阶数=lockin_stages、建议 4，截止=cutoff_ratio·fm、建议 0.25=fm/4）。"
                                                      "硬件若只有 Butterworth，传 lock_kind='butter' 即可在仿真里复现你的器件"},
                         "zero_phase": {"type": "boolean",
                                       "description": "零相位锁相（filtfilt 正滤+倒滤，每级=2次滤波、等效衰减翻倍）。对 butter：消除 lfilter 群延迟、峰位对齐真值（n_stages=2→约4阶，|H(2fm)|≈-72dB）；"
                                                      "对 boxcar：默认(mode='same')已零相位、无需开；开启后每级走 filtfilt、n_stages=2 即 4 次卷积=sinc^4（比默认 2 次=sinc^2 零点更深）、峰位不变。"
                                                      "**仅离线可用（非因果，需整段记录）**，默认 False"},
                         "edge": {"type": "string",
                                  "description": "扫描方向 rising/falling/both，默认 rising（避免往返重叠）"},
                         "amp_V": {"type": "number", "description": "三角波幅值 V（**通常无需手填**：由 wn_center+scan_span_cm 自动反算）"},
                         "freq_Hz": {"type": "number", "description": "三角波频率 Hz，默认 100"},
                         "offset_V": {"type": "number"}, "phase_deg": {"type": "number"},
                         "eta_VI": {"type": "number"}, "dnu_dI": {"type": "number"},
                         "wn_ref": {"type": "number"}, "i_ref": {"type": "number"},
                         "i_th": {"type": "number"}, "eta_IP": {"type": "number"},
                         "fs": {"type": "number"}, "n_samples": {"type": "integer"},
                         "adc_bits": {"type": "integer"}, "v_range": {"type": "number"},
                         "throughput": {"type": "number"}, "resp": {"type": "number"},
                         "gain": {"type": "number"}, "bw": {"type": "number"},
                         "rin": {"type": "number"},
                         "fringe": {"type": "boolean",
                                    "description": "是否启用 etalon 干涉条纹（默认 False=理想）。开启后条纹按 FSR=1/(2nd) 叠加，可能伪造 2f 吸收"},
                         "fringe_n": {"type": "number", "description": "腔介质折射率，默认 1.5"},
                         "fringe_d_cm": {"type": "number", "description": "两平行面间距 cm，默认 0.5"},
                         "fringe_R": {"type": "number", "description": "单面反射率，默认 0.04；AR≈0.005"},
                         "fringe_fsr": {"type": "number", "description": "自由光谱范围 cm⁻¹（给则覆盖 n/d）"},
                         "fringe_contrast": {"type": "number", "description": "条纹峰-峰对比度（给则覆盖 R）"},
                         "fringe_phase_rad": {"type": "number"}, "fringe_drift_frac": {"type": "number"},
                         "d2nu_dI2": {"type": "number",
                                      "description": "调谐二阶非线性 cm⁻¹/mA²，默认 0"},
                         "am_i0": {"type": "number", "description": "RAM 1f 强度调制幅度（相对光强），默认 0.02（真实 DFB 典型 0.01–0.05；此前 0=纯 FM 是理想化）"},
                         "am_i2": {"type": "number", "description": "RAM 2f 强度调制幅度，默认 0"},
                         "am_psi1": {"type": "number", "description": "AM 相对 FM 相位差 rad，默认 0"},
                         "am_psi2": {"type": "number", "description": "二阶 AM 相位差 rad，默认 0"},
                         "drift_frac": {"type": "number",
                                        "description": "1/f 慢漂移幅度，默认 0.005（可设 0 关）"},
                         "flicker_frac": {"type": "number",
                                          "description": "1/f 粉红噪声幅度，默认 0.01（可设 0 关）"},
                         "setup": {"type": "string", "description": "整机配置名（tdlas_device save_setup 打包的 laser+pd+daq+optics 组合），一键加载硬件参数"},
                         "laser": {"type": "string", "description": "激光器设备名（tdlas_device 保存），自动套用 eta_VI/dnu_dI/wn_ref/i_th 等"},
                         "pd": {"type": "string", "description": "探测器设备名（tdlas_device 保存），自动套用 resp/gain/bw/rin"},
                         "daq": {"type": "string", "description": "采集卡设备名（tdlas_device 保存），自动套用 fs/n_samples/adc_bits/v_range"},
                         "optics": {"type": "string", "description": "光学元件设备名（tdlas_device 保存），自动套用 throughput"},
                         "seed": {"type": "integer",
                                  "description": "随机种子，默认 0（**默认结果可复现**）；传 null 则每次随机"},
                         "session_id": {"type": "string",
                                        "description": "会话标识，默认 default（复用 tdlas_session 已确认工况）"},
                         "auto_laser": {"type": "boolean",
                                        "description": "目标波数超出当前激光可达范围时，自动把 wn_ref 重锚到该波数"
                                                       "（理想激光器假设，会在 warnings 中标注，默认 True）"},
                         "physical_noise": {"type": "boolean",
                                            "description": "是否注入物理固有噪声（散粒+热），默认 True；"
                                                           "False = 严格理想仿真（结果逐位可复现）"},
                         "allow_partial_dark": {"type": "boolean",
                                                "description": "明知扫描段有暗区（激光在该电压段未出光）仍要计算，默认 False。"
                                                               "暗区 PD 信号为 0、锁相波形被截断 → 2f/1f 失真、**定量结论不可用**；"
                                                               "仅在诊断「扫描越界后长什么样」时开启（开启后 warnings 会报出暗区占比）"},
                         "save_png": {"type": "boolean", "description": "是否出五层链路图"}},
                         "required": ["species", "wn_center"]}},
                         {"name": "tdlas_review",
     "description": "二次审核：自动校验报告（9 项，不画图）。"
                    "用于在给出最终结论前对结果做独立复核；非专业用户据此判断结果是否可信。"
                    "overall=pass/warn/fail，逐项 checks 标注 DAS-理论一致性、2f 峰位、αL 弱吸收、"
                    "是否孤立线、调制系数、采样率、ADC 动态范围、归一化方法、噪声等。"
                    "**硬件参数与 tdlas_wms_instrument 同源、均可注入**（adc_bits/v_range/gain/… 或 "
                    "setup=/laser=/pd=/daq=/optics= 设备引用）；务必传入真实硬件，否则默认量程可能 ADC 饱和并误报 fail。",
     "inputSchema": {"type": "object",
                     "properties": {
                         "species": {"type": "string", "description": "分子式，默认 CH4"},
                         "wn_center": {"type": "number", "description": "波数 cm^-1"},
                         "T": {"type": "number"}, "P": {"type": "number"},
                         "x": {"type": "number"}, "L_cm": {"type": "number"},
                         "amp_V": {"type": "number"}, "freq_Hz": {"type": "number"},
                         "mod_freq_Hz": {"type": "number"}, "mod_amp_V": {"type": "number"},
                         "fs": {"type": "number"}, "seed": {"type": "integer"},
                         "scan_span_cm": {"type": "number"}, "offset_V": {"type": "number"},
                         "n_samples": {"type": "integer"},
                         "adc_bits": {"type": "integer", "description": "ADC 位数，默认 16"},
                         "v_range": {"type": "number", "description": "ADC 输入量程 ±V，默认 10"},
                         "throughput": {"type": "number"}, "resp": {"type": "number"},
                         "gain": {"type": "number", "description": "探测器跨阻增益 V/A，默认 700"},
                         "bw": {"type": "number"}, "rin": {"type": "number"},
                         "fringe": {"type": "boolean", "description": "启用 etalon 干涉条纹（默认 False）"},
                         "fringe_n": {"type": "number"}, "fringe_d_cm": {"type": "number"},
                         "fringe_R": {"type": "number"}, "fringe_fsr": {"type": "number"},
                         "fringe_contrast": {"type": "number"}, "fringe_phase_rad": {"type": "number"},
                         "fringe_drift_frac": {"type": "number"},
                         "eta_VI": {"type": "number"}, "dnu_dI": {"type": "number"},
                         "wn_ref": {"type": "number"}, "i_ref": {"type": "number"},
                         "i_th": {"type": "number"}, "eta_IP": {"type": "number"},
                         "trim_frac": {"type": "number", "description": "剔除扫描两端比例，默认 0.12"},
                         "edge": {"type": "string"}, "lockin_avg": {"type": "integer"},
                         "lockin_stages": {"type": "integer"}, "background_subtract": {"type": "boolean"},
                         "drift_frac": {"type": "number"}, "flicker_frac": {"type": "number"},
                         "setup": {"type": "string", "description": "整机配置名（tdlas_device save_setup），一键加载硬件"},
                         "laser": {"type": "string"}, "pd": {"type": "string"},
                         "daq": {"type": "string", "description": "采集卡设备名（tdlas_device 保存）→ 自动套用 fs/n_samples/adc_bits/v_range"},
                         "optics": {"type": "string"}}}},
    {"name": "tdlas_session",
     "description": "对话状态机：跨会话记住已确认参数。"
                    "action=view 查看；set 记录（键值）；reset 清空。"
                    "已确认参数会在后续 tdlas_wms_instrument 中自动复用，实现多轮补全、少重复问。",
     "inputSchema": {"type": "object",
                     "properties": {
                         "action": {"type": "string",
                                    "description": "view / set / reset，默认 view"},
                         "session_id": {"type": "string", "description": "会话标识，默认 default"},
                         "T": {"type": "number"}, "P": {"type": "number"},
                         "x": {"type": "number"}, "L_cm": {"type": "number"},
                         "species": {"type": "string"}, "wn_center": {"type": "number"}}}},
    {"name": "tdlas_device",
     "description": "设备库管理：激光器/探测器/DAQ/光学器件的存查。"
                    "仿真工具里用 setup= / laser= / pd= / daq= / optics= 直接引用，省去每次手填硬件参数。"
                    "action：list 列出全部；save 保存设备（device_type+name+参数）；view 查看；"
                    "delete 删除；set_default 设默认设备；save_setup 打包整机；set_default_setup 设默认整机；view_setup 看整机详情。"
                    "save 会校验：设备名非空、参数为有限数值且在物理合法域内"
                    "（gain/bw/fs/v_range/eta_* 须 >0，adc_bits 须为 4–32 的整数，"
                    "throughput ∈ (0,1]，dnu_dI ≠ 0），非法即拒绝保存并说明原因；"
                    "不属于该类别的键会被忽略并在 ignored_params 中回报（防拼写错误）。",
     "inputSchema": {"type": "object",
                     "properties": {
                         "action": {"type": "string",
                                    "description": "list / save / view / delete / set_default / save_setup，默认 list"},
                         "device_type": {"type": "string",
                                         "description": "设备类别：laser / pd / daq / optics（save/view/delete/set_default 用）"},
                         "name": {"type": "string", "description": "设备名（save/view/delete/set_default）或整机配置名（save_setup）"},
                         "laser": {"type": "string", "description": "save_setup 时：激光器设备名"},
                         "pd": {"type": "string", "description": "save_setup 时：探测器设备名"},
                         "daq": {"type": "string", "description": "save_setup 时：采集卡设备名"},
                         "optics": {"type": "string", "description": "save_setup 时：光学元件设备名"},
                         "source": {"type": "string",
                                    "enum": ["datasheet", "user", "ai", "calibration", "unspecified"],
                                    "description": "★参数来源（save 时记录，引用时回显）："
                                                   "datasheet=用户提供的规格书/说明书；"
                                                   "user=用户口述或实测；ai=AI 推断；"
                                                   "calibration=现场标定；不填=unspecified。"
                                                   "**请务必如实填写**：AI 推断的值不得标为 datasheet"},
                         "source_note": {"type": "string",
                                         "description": "来源细节（如“规格书页码/表号”、“实测方法”），限 200 字"},
                         "eta_VI": {"type": "number"}, "dnu_dI": {"type": "number"},
                         "d2nu_dI2": {"type": "number"}, "wn_ref": {"type": "number"},
                         "i_ref": {"type": "number"}, "i_th": {"type": "number"},
                         "eta_IP": {"type": "number"}, "am_i0": {"type": "number"},
                         "am_i2": {"type": "number"}, "am_psi1": {"type": "number"},
                         "am_psi2": {"type": "number"}, "resp": {"type": "number"},
                         "gain": {"type": "number"}, "bw": {"type": "number"},
                         "rin": {"type": "number"}, "fs": {"type": "number"},
                         "n_samples": {"type": "integer"}, "adc_bits": {"type": "integer"},
                         "v_range": {"type": "number"}, "throughput": {"type": "number"}}}},
    {"name": "tdlas_guide",
     "description": "AI 指导协议（SOP/参数指南/术语表）。"
                    "现场标定 → 内置默认并标注）、交互流程四步、专业术语表、全部参数索取指南。"
                    "**在开始任何 TDLAS 任务前应先调用本工具**，据此主动引导用户。",
     "inputSchema": {"type": "object",
                     "properties": {
                         "topic": {"type": "string",
                                   "description": "可选：查询特定术语（如 m / RIN / 2f/1f / LOD）"}}}},
    {"name": "tdlas_invert",
     "description": "免标定浓度反演：2f/1f 峰高 → 摩尔分数。"
                    "优先用 k（来自 tdlas_wms_instrument 返回的 results.sensitivity_k）；"
                    "未给 k 时内部跑完整仪器链路在 x_ref 下重算，保证与测量同模型。"
                    "仅适用弱吸收（αL≪1），强吸收时结果偏高。"
                    "输入校验：peak_2f1f 须为有限值且 ≥0（峰高取绝对值）、k 须为有限正数，"
                    "否则直接报错而非返回负浓度。"
                    "输出校验：结果超出摩尔分数定义域 (0,1] 时不静默截断，"
                    "而以 mole_frac_status / mole_frac_valid / warning 显式标注越界（该结果不可用）。"
                    "⚠ 配对量必须是 tdlas_wms_instrument 返回的 **results.S2f_norm_peak** ——"
                    "它才是 sensitivity_k 的分子（k = S2f_norm_peak / x），所以 x = peak / k 精确成立。"
                    "**不是** results.S2f1f_peak：只有归一化方法为 2f/1f 时二者才相等，"
                    "1f 失效自动退化为 2f/I0 时二者不同，拿 S2f1f_peak 配 k 会得到错误浓度。"
                    "n_repeats>0 时额外返回 uncertainty（同工况重复仿真的 run-to-run 散布）："
                    "它**只含统计（随机）分量**，不含 k 标定误差、HITRAN 数据库不确定度、"
                    "自展宽/线混合等线型近似与 etalon 条纹 —— 不得当作总不确定度。",
     "inputSchema": {"type": "object",
                     "properties": {
                         "peak_2f1f": {"type": "number",
                                      "description": "测得的**归一化** 2f 峰高（≥0，取绝对值后的量）；"
                                                     "配对量 = tdlas_wms_instrument 的 results.S2f_norm_peak"
                                                     "（与 sensitivity_k 同源，保证 x=peak/k 精确）。"
                                                     "注意：**不是** S2f1f_peak —— 仅当归一化方法为 2f/1f 时"
                                                     "两者才相等，退化为 2f/I0 时用它会把浓度算错"},
                         "species": {"type": "string"}, "wn0": {"type": "number"},
                         "T": {"type": "number"}, "P": {"type": "number"},
                         "L": {"type": "number"}, "a": {"type": "number"},
                         "x_ref": {"type": "number", "description": "参考浓度；缺省=会话已确认或按物种推荐"},
                         "k": {"type": "number", "description": "完整链路灵敏度（来自 tdlas_wms_instrument.sensitivity_k）；不给则内部重算"},
                         "session_id": {"type": "string", "description": "会话标识，默认 default（复用已确认 T/P/x/L）"},
                         "span": {"type": "number", "description": "【遗留参数】解析版扫描半宽，完整链路下不生效"},
                         "i0": {"type": "number", "description": "【遗留参数】解析版 1f 基线，完整链路下不生效"},
                         "i2": {"type": "number", "description": "【遗留参数】解析版 2f 基线，完整链路下不生效"},
                         "psi1_pi": {"type": "number", "description": "【遗留参数】完整链路下不生效"},
                         "psi2_pi": {"type": "number", "description": "【遗留参数】完整链路下不生效"},
                         "spectrum": {"type": "array", "items": {"type": "number"},
                                      "description": "可选：待反演的**整条归一化 2f 谱**"
                                                     "（通常取 tdlas_wms_instrument 的 results.S2f_norm_peak 连续谱）。"
                                                     "给它即走最小二乘（比单点除法更准、且给 χ² 诊断）"},
                         "ref_spectrum": {"type": "array", "items": {"type": "number"},
                                          "description": "可选：**同工况** x_ref 下的参考谱（与 spectrum 同长）。"
                                                         "缺省时内部跑一次（同工况，但采样必须一致）"},
                         "n_repeats": {"type": "integer",
                                       "description": "测量不确定度：同工况重复仿真次数（默认 0=不算）。建议 ≥10 才可引用；耗时 ≈ n_repeats × 单次链路。只含统计（随机）分量，系统项未计入"}},
                     "required": ["peak_2f1f", "species", "wn0"]}},
    {"name": "tdlas_detection_limit",
     "description": "检测限：噪声 → NEC / LOD。",
     "inputSchema": {"type": "object",
                     "properties": {
                         "species": {"type": "string"}, "wn0": {"type": "number"},
                         "T": {"type": "number"}, "P": {"type": "number"},
                         "L": {"type": "number"}, "a": {"type": "number"},
                         "sigma_tau": {"type": "number", "description": "等效透过率噪声，默认 1e-5"},
                         "x_true": {"type": "number", "description": "用于统计的真值浓度；缺省=会话已确认或 1e-3"},
                         "n_trials": {"type": "integer", "description": "蒙特卡洛次数，默认 30"},
                         "n_sigma": {"type": "number", "description": "倍数，默认 3"},
                         "session_id": {"type": "string", "description": "会话标识，默认 default（复用已确认 T/P/x/L）"},
                         "span": {"type": "number", "description": "【遗留参数】解析版扫描半宽，完整链路下不生效"},
                         "i0": {"type": "number", "description": "【遗留参数】解析版 1f 基线，完整链路下不生效"},
                         "i2": {"type": "number", "description": "【遗留参数】解析版 2f 基线，完整链路下不生效"},
                         "psi1_pi": {"type": "number", "description": "【遗留参数】完整链路下不生效"},
                         "psi2_pi": {"type": "number", "description": "【遗留参数】完整链路下不生效"},
                         "seed": {"type": "integer"}},
                         "required": ["species", "wn0"]}},
                         {"name": "tdlas_detection_limit_scan",
     "description": "检测限扫描：LOD 随光程/浓度的网格（选型用）。"
                    "弱吸收下 LOD∝1/L，且与参考浓度基本无关；返回 LOD 网格（行=浓度 x，列=光程 L）。",
     "inputSchema": {"type": "object",
                     "properties": {
                         "species": {"type": "string", "description": "分子式，默认 CH4"},
                         "wn0": {"type": "number", "description": "目标线中心 cm^-1，默认 2968.5"},
                         "T": {"type": "number"}, "P": {"type": "number"},
                         "a": {"type": "number", "description": "调制深度 cm^-1，默认 0.1"},
                         "L_list": {"type": "array", "items": {"type": "number"},
                                    "description": "光程列表 cm，默认 [5,10,30,50,100,200]"},
                         "x_list": {"type": "array", "items": {"type": "number"},
                                    "description": "参考浓度列表，默认 [1e-4,1e-3,1e-2]"},
                         "sigma_tau": {"type": "number", "description": "等效透过率噪声，默认 1e-5"},
                         "n_trials": {"type": "integer", "description": "每格蒙特卡洛次数，默认 10"},
                         "n_sigma": {"type": "number"}, "seed": {"type": "integer"},
                         "save_png": {"type": "boolean", "description": "是否出图，默认 False"}},
                     "required": []}},
    {"name": "tdlas_calibration_curve",
     "description": "标定曲线：浓度 vs 2f/1f 峰高（线性/非线性拟合）。",
     "inputSchema": {"type": "object", "properties": {
         "species": {"type": "string", "description": "气体分子"},
         "wn0": {"type": "number", "description": "中心波数 cm⁻¹"},
         "L": {"type": "number", "description": "光程 cm"},
         "x_list": {"type": "array", "items": {"type": "number"}, "description": "浓度列表（默认 5 个）"},
         "save_png": {"type": "boolean", "description": "是否出图"}
     }, "required": ["species", "wn0"]}},
    {"name": "tdlas_export",
     "description": "导出结果为 CSV/JSON。",
     "inputSchema": {"type": "object", "properties": {
         "data": {"description": "要导出的数据（dict 或 list of dict）"},
         "filename": {"type": "string", "description": "输出文件名（自动存到 tmp/mcp_out/）"},
         "format": {"type": "string", "enum": ["csv", "json"], "description": "导出格式（默认 csv）"}
     }, "required": ["data", "filename"]}},
    {"name": "tdlas_allan",
     "description": "Allan 方差：评估系统稳定性与最优积分时间。",
     "inputSchema": {"type": "object", "properties": {
         "signal": {"type": "array", "items": {"type": "number"}, "description": "一维时序信号（2f 峰高/浓度/电压）"},
         "fs": {"type": "number", "description": "采样率 (Hz)"},
         "max_tau": {"type": "number", "description": "最长平均时间 (s)，默认总时长一半"},
         "tau_points": {"type": "integer", "description": "τ 轴采样点数（对数均匀，默认 100）"},
         "save_png": {"type": "boolean", "description": "是否出图"}
     }, "required": ["signal", "fs"]}},
    {"name": "tdlas_selftest",
     "description": "全链路自检。",
     "inputSchema": {"type": "object", "properties": {}}},
]


# tdlas_review 与 tdlas_wms_instrument 的参数**完全同源**（实现上就是透传），
# 手工维护两份属性表迟早漂移（历史上就发生过：review 缺硬件参数、wms 缺 session_id）。
# 这里在定义后直接把 wms 的属性集复制过去，只去掉出图开关，保证两者**永不脱节**。
def _sync_review_schema():
    by = {t["name"]: t for t in TOOLS}
    _wms_props = by["tdlas_wms_instrument"]["inputSchema"]["properties"]
    _r_props = by["tdlas_review"]["inputSchema"].setdefault("properties", {})
    for k, v in _wms_props.items():
        if k != "save_png":
            _r_props.setdefault(k, v)


_sync_review_schema()

_SCHEMA_BY_NAME = {t["name"]: t["inputSchema"] for t in TOOLS}


def _validate_args(name, args):
    """按 inputSchema 做轻量校验（未知键 / 必填 / 类型）。返回错误说明，None = 通过。

    为什么必须拦未知键：此前未声明的键被**静默丢弃**，最惨的一次是 schema 声明了
    `edge`/`trim_frac`/`d2nu_dI2`/`am_*` 而 MCP 层从未透传 → AI 传 edge="falling"
    得到的是 rising 的结果、返回体里还写着 rising，全程零提示。用户会拿着归因错误的
    结论去指导真实实验 —— "以为改了、实际没改"比直接报错危险得多。
    """
    if not isinstance(args, dict):
        return "arguments 必须是 JSON 对象"
    sch = _SCHEMA_BY_NAME.get(name) or {}
    props = sch.get("properties") or {}
    missing = [k for k in (sch.get("required") or []) if k not in args]
    if missing:
        return f"缺少必填参数：{missing}"
    unknown = sorted(k for k in args if k not in props)
    if unknown:
        return (f"存在未声明的参数：{unknown}；本工具允许的键：{sorted(props)}。"
                f"（拼写错误会被拒绝而非静默忽略；确需新参数请提 issue）")
    for k, v in args.items():
        if v is None:
            continue
        want = (props.get(k) or {}).get("type")
        if want == "number" and (isinstance(v, bool) or not isinstance(v, (int, float))):
            return f"参数 {k} 期望 number，收到 {type(v).__name__}（{v!r}）"
        if want == "integer" and (isinstance(v, bool) or not isinstance(v, (int, float))
                                  or float(v) != int(v)):
            return f"参数 {k} 期望 integer，收到 {v!r}"
        if want == "boolean" and not isinstance(v, bool):
            return f"参数 {k} 期望 boolean，收到 {v!r}"
        if want == "string" and not isinstance(v, str):
            return f"参数 {k} 期望 string，收到 {type(v).__name__}"
    return None


# ───────────────────────── JSON-RPC ─────────────────────────

def handle_request(req):
    """处理 MCP 请求（initialize / tools/list / tools/call）。

    支持 JSON-RPC 2.0 批量请求（顶层数组）；批量内每个请求独立处理，通知（返回 None）被剔除。
    """
    if isinstance(req, list):                     # 批量请求：此前会把 list 当 dict 用 → AttributeError
        if not req:
            return {"jsonrpc": "2.0", "id": None,
                    "error": {"code": -32600, "message": "空批量请求"}}
        return [r for r in (handle_request(x) for x in req) if r is not None]
    if not isinstance(req, dict):
        return {"jsonrpc": "2.0", "id": None,
                "error": {"code": -32600, "message": "请求必须是 JSON 对象或其数组"}}

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
        bad = _validate_args(name, args)
        if bad:
            return {"jsonrpc": "2.0", "id": req_id,
                    "result": {"content": [{"type": "text", "text": f"参数错误：{bad}"}],
                               "isError": True}}
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


def _arg(name, default=""):
    """读取 `--name value` 形式的命令行参数。"""
    if name in sys.argv:
        i = sys.argv.index(name)
        if i + 1 < len(sys.argv):
            return sys.argv[i + 1]
    return default


def _run_http(host, port, token):
    """HTTP 传输（MCP Streamable HTTP）：把同一套 handle_request 暴露为 URL。

    远端 MCP 客户端直接填 http://<host>:<port>/mcp 即可直链，调用全部 12 个工具。
    - POST /mcp  客户端→服务器 JSON-RPC（支持 application/json 或 text/event-stream）
    - GET  /mcp  服务器→客户端推送通道（本工具服务器无主动推送，返回 405）
    - 可选 --token 做 Bearer 鉴权（远程暴露强烈建议）
    """
    import uuid
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    SESSION_ID = uuid.uuid4().hex

    class _Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *args):  # 协议日志不污染 stdout
            pass

        def _cors(self):
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Access-Control-Allow-Headers",
                              "Content-Type, Authorization, Mcp-Session-Id, Accept")
            self.send_header("Access-Control-Allow-Methods", "POST, GET, OPTIONS")

        def _auth_ok(self):
            if not token:
                return True
            a = self.headers.get("Authorization", "")
            return a == f"Bearer {token}" or a == token

        def _send(self, resp):
            if resp is None:  # 通知：无响应体
                self.send_response(202)
                self.send_header("Content-Length", "0")
                self._cors()
                self.end_headers()
                return
            body = json.dumps(resp, ensure_ascii=False).encode("utf-8")
            use_sse = "text/event-stream" in self.headers.get("Accept", "")
            self.send_response(200)
            self.send_header("Mcp-Session-Id", SESSION_ID)
            self._cors()
            if use_sse:
                payload = b"data: " + body + b"\n\n"
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)
            else:
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        def _read_body(self):
            """可靠读取请求体：同时支持 Content-Length 与 chunked 传输编码。

            cloudflared 等反向代理常发 chunked（无 Content-Length），若只按
            Content-Length 读会漏读，残留字节污染连接复用 → 501 'Unsupported method'。
            """
            te = (self.headers.get("Transfer-Encoding", "") or "").lower()
            if "chunked" in te:
                return self._read_chunked()
            try:
                n = int(self.headers.get("Content-Length", 0))
            except ValueError:
                n = 0
            return self.rfile.read(n) if n > 0 else b""

        def _read_chunked(self):
            buf = b""
            while True:
                line = self.rfile.readline().strip()
                if not line:
                    line = self.rfile.readline().strip()
                try:
                    size = int(line.split(b";")[0], 16)
                except ValueError:
                    break
                if size == 0:
                    while True:
                        h = self.rfile.readline()
                        if h in (b"\r\n", b""):
                            break
                    break
                buf += self.rfile.read(size)
                self.rfile.readline()  # 消费块尾 \r\n
            return buf

        def do_OPTIONS(self):
            self.send_response(204)
            self._cors()
            self.send_header("Content-Length", "0")
            self.end_headers()

        def do_GET(self):
            self.send_response(405)
            self.send_header("Allow", "POST")
            self._cors()
            self.send_header("Content-Length", "0")
            self.end_headers()

        def do_POST(self):
            if self.path not in ("/mcp", "/"):
                self.send_response(404)
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            if not self._auth_ok():
                err = b'{"jsonrpc":"2.0","id":null,"error":{"code":-32000,"message":"unauthorized"}}'
                self.send_response(401)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(err)))
                self.end_headers()
                self.wfile.write(err)
                return
            try:
                req = json.loads(self._read_body() or b"{}")
            except Exception as e:
                self._send({"jsonrpc": "2.0", "id": None,
                            "error": {"code": -32700, "message": f"解析失败：{e}"}})
                return
            self._send(_sanitize_paths(handle_request(req)))

    sys.stderr.write(f"[tdlas-mcp] HTTP 模式已启动：http://{host}:{port}/mcp\n")
    ThreadingHTTPServer((host, port), _Handler).serve_forever()


def main():
    # Windows 控制台默认 GBK：协议流必须强制 UTF-8，否则中文会破坏 JSON-RPC
    for _s in (sys.stdin, sys.stdout, sys.stderr):
        try:
            _s.reconfigure(encoding="utf-8")
        except Exception:
            pass
    if "--selftest" in sys.argv:
        try:
            print(json.dumps(_sanitize_paths(t_selftest()), ensure_ascii=False, indent=2))
        except ImportError as exc:      # 依赖缺失：给可操作的提示 + 非零退出码（便于 CI 判断）
            sys.stderr.write(f"[tdlas-mcp] 自检无法运行：{exc}\n")
            raise SystemExit(2)
        return
    if "--http" in sys.argv:
        host = _arg("--host", "127.0.0.1")
        port = int(_arg("--port", "8000"))
        token = _arg("--token", "")
        if host in ("0.0.0.0", "") and not token:
            sys.stderr.write("[警告] 监听 0.0.0.0 且未设置 --token：任何能访问该端口的客户端都可调用仿真，"
                             "远程暴露请务必加 --token。\n")
        _run_http(host, port, token)
        return
    for line in sys.stdin:                            # stdio 协议循环
        line = line.strip()
        if not line:
            continue
        try:
            resp = _sanitize_paths(handle_request(json.loads(line)))
        except ValueError:                     # JSON 解析失败：不回显 Python 异常文本
            resp = {"jsonrpc": "2.0", "id": None,
                    "error": {"code": -32700, "message": "JSON 解析失败（请检查该行是否为合法 JSON 对象）"}}
        if resp is not None:
            print(json.dumps(resp, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    main()
