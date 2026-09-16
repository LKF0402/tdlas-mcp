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
import io
import json
import math
import os
import re
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

import tdlas_sim as ts  # noqa: E402

PROTOCOL_VERSION = "2024-11-05"
SERVER_INFO = {"protocolVersion": PROTOCOL_VERSION,
               "capabilities": {"tools": {}},
               "serverInfo": {"name": "tdlas", "version": "0.1.0"}}
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


def _atomic_write_text(path, text):
    """原子写盘：先写同目录临时文件再 os.replace 覆盖。

    为什么：会话/设备文件的读-改-写不是原子操作，MCP 崩溃或断电时会留下**半截 JSON**
    （下次 _load_* 直接 JSONDecodeError → 静默丢失全部已确认工况）。os.replace 在
    同一文件系统上是原子操作，要么旧内容、要么新内容，不会出现半截文件。
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


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
    pick = {"laser": laser, "pd": pd, "daq": daq, "optics": optics}
    if setup:
        st = dev.get("setup", {}).get(str(setup))
        if st is None:
            raise ValueError(f"整机配置 {setup!r} 不存在，请先用 tdlas_device 保存")
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
            raise ValueError(f"{_DEVICE_TYPE_CN[k]}设备 {name!r} 不存在，请先用 tdlas_device 保存")
        entry = {kk: vv for kk, vv in params.items() if vv is not None}
        bad = _validate_device_entry(entry)          # 兜住历史/手改过的非法条目
        if bad:
            raise ValueError(f"已保存的{_DEVICE_TYPE_CN[k]}设备 {name!r} 参数非法：{bad}；"
                             f"请用 tdlas_device save 重新保存修正")
        resolved.update(entry)
    return resolved


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


def build_clarify_questions(species, wn_center, assumed, used_values):
    """把缺省参数转成结构化澄清问题（含选项），供 AI 主动向用户提问。"""
    questions = []
    # 波段澄清（优先问，因为影响后续一切）
    prof = SPECIES_PROFILES.get(str(species).strip().upper(), {})
    if prof.get("bands"):
        q = {"param": "波段", "question": f"{species} 要测哪个波段？",
             "options": [f"{nm}（{wn} cm⁻¹）" for nm, wn in prof["bands"]],
             "why": "不同波段线强/线密度不同", "current": wn_center}
        questions.append(q)
    for k in assumed:
        tpl = CLARIFY_QUESTIONS.get(k)
        if tpl:
            questions.append({"param": k, "question": tpl["question"],
                              "options": tpl["options"], "why": tpl["why"],
                              "current_default": used_values.get(k)})
        else:
            cn, unit, why = PARAM_ACQ_GUIDE.get(k, (k, "—", ""))
            questions.append({"param": k, "中文名": cn, "单位": unit,
                              "question": f"请确认 {cn}（{unit}），当前用默认 {used_values.get(k)}",
                              "options": [], "why": why,
                              "current_default": used_values.get(k)})
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
        "布局": "3 行 × 2 列，共 6 子图，固定顺序（左→右、上→下），"
                "figsize≈14×10，每子图约 2:1（横长竖短，利于读谱线形、贴合黄金矩形）：",
        "subplots": [
            "① 波长调制 V(t)（三角波+正弦调制，横轴时间）",
            "② PD 原始信号（无调制 DAS 链路，横轴波数，左轴 V）",
            "DAS αL（-ln 提取）+ HITRAN 理论 αL（虚线对照，横轴波数）",
            "③ 一阶谐波 1f（单独，横轴波数）",
            "④ 二阶谐波 2f（单独，横轴波数）",
            "⑤ 归一化 2f（标注方法 2f/1f 或 2f/I0）",
        ],
        "rule": "1f 与 2f **必须分开**（幅值量级不同，不可同图）；"
                "PD 原始信号与 DAS αL 分两个子图；理论 αL 与 DAS αL 同图用虚线区分；"
                "每层标题标注技术名 + 归一化/基线处理方法；剔除区红带标出；"
                "横轴：驱动电压用时间，其余用波数（扫描波数，非瞬时）。",
        "标注要求": "用户只说'给我 XX 气体图'时，一律出这套 6 子图；"
                    "技术名、归一化方法、基线处理（拟合 or 背景扣除）必须在图上显式标注。",
        "线型约定": "实线=实测/有效数据；虚线=理论参考（如 HITRAN 理论 αL）；"
                    "点线=剔除区（三角波转折点，不可信）；每种线型必须在图例里注明含义。",
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

def t_simulate(species="H2O", wn0=7185.596, T=None, P=None, x=None, L=None,
               a=0.10, i0=0.1671, i2=2.48e-3, psi1_pi=1.9356, psi2_pi=4.4138,
               span=0.8, n_scan=401, n_mod=96, sigma_tau=0.0, seed=None,
               save_png=False, session_id="default"):
    """DAS + 免标定 WMS 正向仿真：返回 1f、2f 峰高、DAS 最小透过率、线表信息。

    wn0 为目标线中心（cm^-1）；x 为摩尔分数（默认 1e-3 = 1000 ppm，弱吸收）；L 为光程 cm；
    a 为调制深度 cm^-1。sigma_tau 为等效透过率噪声（默认 0=理想无噪）。save_png=True 时出图。
    T/P/x/L 未给出时按「会话已确认 > 默认」取值（复用 tdlas_session 已确认的工况）。
    """
    _xd, _Ld = _species_defaults(species)
    T, P, x, L, assumed = _resolve_conditions(T, P, x, L, session_id, x_default=_xd, L_default=_Ld)
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
           "assumptions": assumed, "needs_confirm": bool(assumed),
           "log": _sanitize_paths(g.getvalue()).splitlines()}
    if save_png:
        OUT_DIR.mkdir(parents=True, exist_ok=True)
        png = OUT_DIR / f"tdlas_{str(species).upper()}_{float(wn0):g}cm-1_x{float(x):g}.png"
        with _quiet():
            ts.plot(r, png)
        out["png"] = str(png)
    return out


def t_invert(peak_2f1f, species="H2O", wn0=7185.596, T=None, P=None, L=None,
             a=0.10, x_ref=None, k=None, i0=0.1671, i2=2.48e-3,
             psi1_pi=1.9356, psi2_pi=4.4138, span=0.8, session_id="default"):
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
    with _quiet() as g:
        if k is not None:
            k = float(k)
            if not math.isfinite(k) or k <= 0.0:
                raise ValueError(f"灵敏度 k 必须为有限正数，收到 {k:g}"
                                 f"（应用 tdlas_wms_instrument 的 results.sensitivity_k）")
            k_src = "调用方提供（tdlas_wms_instrument.sensitivity_k）"
        else:
            r = ts.simulate_wms_instrument(str(species), wn_center=float(wn0), T=float(T),
                                           P=float(P), x=float(x_ref), L_cm=float(L))
            k = float(r["meta"]["sensitivity_k"])
            k_src = "完整链路重算（@x_ref）"
            # 回传内部重算 k 所用的**完整工况**：调用方只有拿到这些才能判断 k 是否与自己的
            # 测量同源（此前只回 x_ref/T/P/L，调制深度/fm/扫描半宽/采样都看不到）。
            _mc = r["meta"]
            k_cond = {"species": str(species).upper(), "wn_center_cm-1": float(wn0),
                      "T_K": float(T), "P_atm": float(P), "L_cm": float(L),
                      "x_ref": float(x_ref), "scan_span_cm-1": _mc.get("cfg", {}).get("scan_span_cm"),
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
    if x_warn:
        out["warning"] = x_warn
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
           "log": _sanitize_paths(g.getvalue()).splitlines()}
    if save_png:
        OUT_DIR.mkdir(parents=True, exist_ok=True)
        png = OUT_DIR / f"das_chain_{str(species).upper()}_{float(wn0):g}cm-1_x{x:g}.png"
        with _quiet():
            ts.plot_das_td(r, png)
        out["png"] = str(png)
    return out


_INSTR_KEYS = ("scan_span_cm", "amp_V", "freq_Hz", "offset_V", "phase_deg", "eta_VI", "dnu_dI",
               "wn_ref", "i_ref", "i_th", "eta_IP", "fs", "n_samples", "adc_bits",
               "v_range", "throughput", "resp", "gain", "bw", "rin",
               # 调谐二阶非线性（两条链都经 voltage_to_laser 使用）
               "d2nu_dI2",
               # 严格理想仿真开关：False = 连散粒/热也不注入
               "physical_noise",
               # etalon 干涉条纹（默认关，仅显式 fringe=True 时生效）
               "fringe", "fringe_n", "fringe_d_cm", "fringe_R", "fringe_fsr",
               "fringe_contrast", "fringe_phase_rad", "fringe_drift_frac")

# etalon 条纹相关键：默认不参与"缺省参数确认"（默认关 → 不该反复追问）；
# 但用户一旦开启（fringe=True）且未给几何参数，则列入 assumptions / param_requests 主动索取。
_FRINGE_KEYS = ("fringe", "fringe_n", "fringe_d_cm", "fringe_R", "fringe_fsr",
                "fringe_contrast", "fringe_phase_rad", "fringe_drift_frac")

# 以下参数有明确默认值或自动反算，无需用户确认：
#   amp_V / offset_V 由 wn_center + scan_span_cm 经 V-ν 关系反算；
#   background_subtract 是诊断开关（默认 True，仅诊断原始基线时关闭）。
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
    d2 = inst.get("d2nu_dI2", 0.0)
    i_th = inst.get("i_th", 30.0)
    r = ts.laser_reach(float(wn_center), eta_VI=eta_VI, dnu_dI=dnu_dI, i_ref=i_ref,
                       wn_ref=wn_ref0, scan_span_cm=span_cm, d2nu_dI2=d2, i_th=i_th)
    if r.get("reachable"):
        return None, None
    if not r.get("fits_span"):
        raise ValueError(
            f"扫描半宽 {float(inst.get('scan_span_cm', 1.5)):g} cm⁻¹ 需要 ±{r['amp_V']:.3g} V 驱动，"
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
                     optics=None, **kw):
    """仪器系统级 DAS 仿真：DAQ 电压 → 激光 → 光路 → PD → ADC 量化 → 基线扣除。

    缺省参数按优先级索取：① 用户实测标定值 → ② 器件型号（AI 检索规格书）
    → ③ 引导用户现场测量 → ④ 内置默认（常见 16-bit USB DAQ + 典型中红外 DFB 激光器）。
    返回 assumptions / param_requests / warnings，供 AI 主动向用户澄清后再解读结论。
    """
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

    given_inst = {k: kw.get(k) for k in _INSTR_KEYS}
    inst = {k: v for k, v in given_inst.items() if v is not None}
    # 设备引用：解析设备库，填充未显式给出的硬件参数（本次显式给的值仍最优先）
    dev_params = _resolve_devices(setup, laser, pd, daq, optics)
    for k, v in dev_params.items():
        inst.setdefault(k, v)
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
                                       fit_frac=float(scene["fit_frac"]), seed=seed, **inst)
    m = r["meta"]
    cfg = m["cfg"]

    requests = []
    for k in assumed:
        if k in PARAM_ACQ_GUIDE:
            cn, unit, why = PARAM_ACQ_GUIDE[k]
            requests.append({"param": k, "cn": cn, "unit": unit, "why": why,
                             "value_used": cfg.get(k, _SCENE_DEFAULTS.get(k)),
                             "how": "① 用户实测值 ② 器件型号（AI 检索规格书）"
                                    "③ 引导现场标定 ④ 保留默认并注明"})

    out = {"species": str(species).upper(), "wn_center_cm-1": float(wn_center),
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
                       "n_lines_in_window": m["n_lines_in_window"], "table": m["table"]},
           "assumptions": assumed,
           "param_requests": requests,
           "needs_input": bool(requests),
           "confirm_note": "param_requests 为缺省参数：请优先向用户索取实测值或器件型号；"
                           "必要时引导现场标定；保留默认时须在结论中明确注明。",
           "seed_used": m.get("seed_used"),
           "physical_noise": m.get("physical_noise", True),
           "warnings": ([_note] if _note else []) + m["warnings"],
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
    if save_png:
        OUT_DIR.mkdir(parents=True, exist_ok=True)
        png = OUT_DIR / f"das_instr_{_safe_name(str(species).upper())}_{float(wn_center):g}cm-1.png"
        with _quiet():
            ts.plot_das_instrument(r, png)
        out["png"] = str(png)
    return out


# WMS 专属参数。⚠ 这里曾漏掉 edge / trim_frac / d2nu_dI2 / am_* —— 而 schema 声明了它们，
# 于是 AI 传 edge="falling" 被静默丢弃、返回里 normalization.edge 仍是 "rising"，
# 用户以为改了、实际没改（见 CHANGELOG「参数透传」）。**新增 schema 键必须同时加到这里**，
# 契约测试 tools/contract_check.py 会做 schema↔透传键的双向核对，防止再次漂移。
_WMS_ONLY_KEYS = ("mod_freq_Hz", "mod_amp_V", "mod_phase_deg", "m_opt", "lockin_avg",
                  "lockin_stages", "drift_frac", "flicker_frac", "background_subtract",
                  "edge", "trim_frac", "am_i0", "am_i2", "am_psi1", "am_psi2")
_INSTR_KEYS_WMS = _INSTR_KEYS + _WMS_ONLY_KEYS


def t_wms_instrument(species="CH4", wn_center=2968.5, T=None, P=None, x=None, L_cm=None,
                     seed=0, session_id="default", auto_laser=True, save_png=False,
                     setup=None, laser=None, pd=None, daq=None, optics=None, **kw):
    """WMS 仪器链路仿真：三角波扫描 + 正弦调制 → 激光 → 光路 → PD → ADC → 数字锁相（2f/1f）。

    调制幅值默认按**最优调制系数 m≈2.2** 自动优化（2f 峰值最大处）。
    未给出的参数列入 assumptions / param_requests；AI 须按 AI_INTERACTION_GUIDE 处理并显式标注。
    """
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

    given_inst = {k: kw.get(k) for k in _INSTR_KEYS_WMS}
    inst = {k: v for k, v in given_inst.items() if v is not None}
    # 设备引用：解析设备库，填充未显式给出的硬件参数（本次显式给的值仍最优先）
    dev_params = _resolve_devices(setup, laser, pd, daq, optics)
    for k, v in dev_params.items():
        inst.setdefault(k, v)
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
                                       seed=seed, **inst)
    m = r["meta"]
    cfg = m["cfg"]
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

    out = {"species": str(species).upper(), "wn_center_cm-1": float(wn_center),
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
                       "sensitivity_k": m["sensitivity_k"],
                       "sensitivity_note": "sensitivity_k = S2f_norm_peak / x（完整链路）；"
                                           "tdlas_invert 的 k 传本值、peak_2f1f 传 S2f_norm_peak，"
                                           "二者成对（勿用解析版灵敏度）",
                       "S2f1f_peak_nu_cm-1": float(r["nu_axis"][kpk]),
                       "v_pd_mean_V": m["v_pd_mean"], "saturated_points": m["n_sat"],
                       "noise_breakdown_mV": {"shot": m["sigma_shot"] * 1e3,
                                              "thermal": m["sigma_thermal"] * 1e3,
                                              "rin_white": m["sigma_rin"] * 1e3},
                       "noise_1f": {"drift_frac": m["drift_frac"],
                                    "flicker_frac": m["flicker_frac"]},
                       "noise_disclosure": "默认未加噪声（理想仿真）。如需注入：rin=相对强度噪声，"
                                           "drift_frac=1/f 慢漂移，flicker_frac=1/f 粉红。散粒/热为物理固有（极小）。",
                       "n_lines_in_window": m["n_lines_in_window"], "table": m["table"]},
           "validation": ts.validate_wms_result(r),
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
                                             "wn_ref/dν/dI，或用 tdlas_device 保存真实激光器后以 laser= 引用"],
                           "interaction_rule": "MCP 必须**多与用户交互**：主动告知以上 must_disclose 各项，"
                                               "并在解读结果前先向用户确认工况（物种/波段/T/P/浓度/光程）。"},
           "seed_used": m.get("seed_used"), "physical_noise": m.get("physical_noise", True),
           "warnings": ([_note] if _note else []) + m["warnings"], "edge_note": EDGE_TECH_NOTE,
           "log": _sanitize_paths(g.getvalue()).splitlines()}
    if laser_info:
        out["laser_auto_aligned"] = laser_info
    if ignored:
        out["ignored_params"] = ignored
        out["ignored_note"] = (f"以下参数不属于本工具、已被忽略（疑似拼写错误）：{ignored}；"
                               f"允许的键见 tools/list 的 inputSchema")
    out["alpha_report"] = _alpha_report_block(m.get("alpha_context"), species, session_id)
    out["fringe_report"] = _fringe_report_block(m.get("fringe"), session_id)
    if save_png:
        OUT_DIR.mkdir(parents=True, exist_ok=True)
        png = OUT_DIR / f"wms_instr_{_safe_name(str(species).upper())}_{float(wn_center):g}cm-1.png"
        with _quiet():
            ts.plot_wms_instrument(r, png)
        out["png"] = str(png)
    return out


def t_review(species="CH4", wn_center=2968.5, T=None, P=None, x=None, L_cm=None,
             seed=0, session_id="default", **kw):
    """二次审核 / 多轮核对：跑一遍 WMS 并只返回自动校验报告（不画图）。

    用于在给出最终结论前，对结果做独立复核；非专业用户可据此判断"是否可信"。
    返回 validation（overall + 逐项 checks）+ warnings + 关键指标。
    参数与 tdlas_wms_instrument **完全同源**（实现上就是透传）。
    """
    out = t_wms_instrument(species=species, wn_center=wn_center, T=T, P=P, x=x, L_cm=L_cm,
                           seed=seed, session_id=session_id, save_png=False, **kw)
    return {"species": out["species"], "wn_center_cm-1": out["wn_center_cm-1"],
            "validation": out["validation"], "warnings": out["warnings"],
            "key_metrics": {"alpha_L_peak": out["results"]["alpha_L_peak"],
                            "n_lines_in_window": out["results"]["n_lines_in_window"],
                            "norm_method": out["normalization"]["method"],
                            "scan_window_cm-1": out["scan_window_cm-1"]},
            "alpha_report": out.get("alpha_report"),
            "fringe_report": out.get("fringe_report"),
            "suggestion": "若 validation.overall != 'pass'，先处理 fail 项再下结论；"
                          "warn 项须向用户说明。"}


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
        return {"devices": {k: {n: dict(v) for n, v in dev.get(k, {}).items()}
                            for k in ("laser", "pd", "daq", "optics")},
                "setups": dict(dev.get("setup", {})),
                "default": dict(dev.get("default", {})),
                "hint": "在 tdlas_wms_instrument / tdlas_das_instrument 里用 setup= 或 "
                        "laser= / pd= / daq= / optics= 引用"}

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
        entry = {k: params[k] for k in _DEVICE_TYPES[dt] if k in params and params[k] is not None}
        if not entry:
            return {"error": f"未提供任何可用参数；{_DEVICE_TYPE_CN[dt]}允许的键："
                             f"{_DEVICE_TYPES[dt]}"}
        bad = _validate_device_entry(entry)
        if bad:
            return {"error": f"设备 {dt}:{nm} 参数非法：{bad}；"
                             f"{_DEVICE_TYPE_CN[dt]}允许的键：{_DEVICE_TYPES[dt]}"}
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

    if action == "save_setup":
        nm = str(name)
        if not nm:
            return {"error": "save_setup 需要 name（整机配置名）"}
        st = {k: v for k, v in {"laser": laser, "pd": pd, "daq": daq, "optics": optics}.items() if v}
        for k, dname in st.items():
            if dname not in dev.get(k, {}):
                return {"error": f"{_DEVICE_TYPE_CN[k]}设备 {dname!r} 不存在，请先 save"}
        full = {"laser": st.get("laser"), "pd": st.get("pd"),
                "daq": st.get("daq"), "optics": st.get("optics")}
        dev.setdefault("setup", {})[nm] = full
        _save_devices(dev)
        return {"saved_setup": nm, "setup": full,
                "hint": f"引用：setup={nm!r}"}

    return {"error": f"unknown action {action!r}"}


def t_guide(topic=None):
    """返回 AI 主动指导协议（面向实验新手）：参数索取优先级、交互流程、术语表、参数索取指南。"""
    out = dict(AI_INTERACTION_GUIDE)
    out["param_guide"] = {k: {"cn": v[0], "unit": v[1], "why": v[2]}
                          for k, v in PARAM_ACQ_GUIDE.items()}
    if topic:
        t = str(topic).upper()
        out["glossary_hit"] = {k: v for k, v in AI_INTERACTION_GUIDE["glossary"].items()
                               if t in k.upper() or t in str(v).upper()}
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
            "tdlas_selftest": t_selftest}

TOOLS = [
    {"name": "tdlas_simulate",
     "description": "【数值计算，不是画图工具】TDLAS/WMS 简化解析模型：返回 DAS 透过率、1f、2f 峰高等数值。"
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
    {"name": "tdlas_das_instrument",
     "description": "仪器系统级 DAS 仿真：DAQ 输出电压 → 激光器调谐（V→I→波数/功率）→ 光路损耗与吸收 "
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
                         "save_png": {"type": "boolean", "description": "是否出五层链路图"}},
                         "required": ["species", "wn_center"]}},
                         {"name": "tdlas_wms_instrument",
     "description": "【默认首选：画 WMS 图就用这个】WMS 仪器级全链路：三角波+正弦调制→激光→光路→PD→ADC→数字锁相，输出标准6子图（V(t)/PD原始/DAS+理论/DAS吸光度/1f/2f/2f/1f归一化）。**用户说画WMS图/2f曲线/波长调制时默认调本工具，不要调 tdlas_simulate**。调制幅值按 m≈2.2 自动优化，fm=30kHz，fscan=100Hz。采样率自动适配硬件（常见 16-bit DAQ 上限 250 kS/s）。缺省参数列入 param_requests，AI 须主动澄清工况。",
     "inputSchema": {"type": "object",
                     "properties": {
                         "species": {"type": "string", "description": "分子式，默认 CH4"},
                         "wn_center": {"type": "number", "description": "扫描中心波数 cm^-1，默认 2968.5"},
                         "T": {"type": "number"}, "P": {"type": "number"},
                         "x": {"type": "number", "description": "摩尔分数，默认 1e-3"},
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
                                                   "description": "是否以无吸收参考谱扣除 RAM 2f 基线，默认 True；False 仅用于诊断原始基线"},
                         "trim_frac": {"type": "number",
                                       "description": "剔除扫描两端比例，默认 0.12（三角波转折点高频谐波会泄漏进 2f）"},
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
                         "am_i0": {"type": "number", "description": "RAM 1f 强度调制幅度，默认 0（纯 FM）"},
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
                         "save_png": {"type": "boolean", "description": "是否出五层链路图"}},
                         "required": ["species", "wn_center"]}},
                         {"name": "tdlas_review",
     "description": "二次审核/多轮核对：跑一遍 WMS 并返回自动校验报告（validation），不画图。"
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
     "description": "对话状态机（跨会话记忆）：记住用户已确认的工况参数，MCP 重启/换会话仍保留。"
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
     "description": "设备库统一管理：把固定的仪器（激光器/探测器/采集卡/光学）命名保存，"
                    "仿真工具里用 setup= / laser= / pd= / daq= / optics= 直接引用，省去每次手填硬件参数。"
                    "action：list 列出全部；save 保存设备（device_type+name+参数）；view 查看；"
                    "delete 删除；set_default 设默认设备；save_setup 打包整机配置。"
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
     "description": "返回 AI 主动指导协议（面向实验新手）：参数索取优先级（实测值 → 器件型号检索 → "
                    "现场标定 → 内置默认并标注）、交互流程四步、专业术语表、全部参数索取指南。"
                    "**在开始任何 TDLAS 任务前应先调用本工具**，据此主动引导用户。",
     "inputSchema": {"type": "object",
                     "properties": {
                         "topic": {"type": "string",
                                   "description": "可选：查询特定术语（如 m / RIN / 2f/1f / LOD）"}}}},
    {"name": "tdlas_invert",
     "description": "免标定浓度反演：给定测得的归一化 2f 峰高，返回摩尔分数（x = peak / k）。"
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
                    "1f 失效自动退化为 2f/I0 时二者不同，拿 S2f1f_peak 配 k 会得到错误浓度。",
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
                         "psi2_pi": {"type": "number", "description": "【遗留参数】完整链路下不生效"}},
                     "required": ["peak_2f1f", "species", "wn0"]}},
    {"name": "tdlas_detection_limit",
     "description": "检测极限 LOD：给定等效透过率噪声 σ_τ，蒙特卡洛给出噪声等效浓度 NEC 与 LOD(nσ)。",
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
     "description": "检测极限 LOD 随光程 L / 参考浓度 x 的扫描（系统选型：加光程能降多少 LOD）。"
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
    {"name": "tdlas_selftest",
     "description": "全链路自检（有线 / 2f 形状 / 弱场线性 / 反演闭环 / 检测限 / 时域交叉验证）。",
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
