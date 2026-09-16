#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""能力登记表与保真度审计（离线、确定性、可作为契约门禁）。

为什么需要这一层
────────────────
本项目已经踩过两次**同一类**事故：schema 声明了参数、MCP 层却从未把它交给引擎，
于是 AI 传了值、拿到的是默认值算出的结果，**全程零提示**（`edge` 一族、以及
`tdlas_das_instrument` 的整族 etalon 几何参数）。

根因不是懒惰，而是**缺一个单一真源**：物理效应、参数、证据分散在代码、文档、测试三处，
人一改就漂。本模块把三者收进一张可执行的表：

  · ``EFFECTS``  物理效应登记表 —— 每一项声明「在哪实现 / 是否真的生效 / 有什么已知缺口」
  · ``audit_parameters()`` 参数可达性审计 —— 用假引擎截获真正进入引擎的 kwargs
  · ``audit_effects()``    证据一致性审计 —— 登记表里写的符号/常量是否真的存在于源码
  · ``missing_effects()``  已知缺口清单 —— 供 AI 与用户判断"这次结果可信到什么程度"

设计纪律
────────
1. **不许手工维护"是否实现"**：``audit_effects`` 会去源码里找证据，找不到就报 FAIL。
2. **合法通道白名单必须显式**：设备引用/会话/出图开关不进引擎是**设计如此**，
   白名单之外的一切"声明了却没进引擎"都是缺陷。
3. 这一层不跑仿真、不访问 HITRAN、不启服务器 —— 与 ``contract_check.py`` 同一纪律。

用法：
    python tools/tdlas_fidelity.py            # 打印审计报告（退出码 0=全通过）
"""

from __future__ import annotations

import inspect
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
for _p in (str(_ROOT), str(_ROOT / "tools")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8")
    except Exception:
        pass

# ══════════════════════════════════════════════════════════════════════
# 1. 物理效应登记表
# ══════════════════════════════════════════════════════════════════════
# evidence : 必须出现在 tdlas_sim.py / tools/*.py 源码里的字面量（证明实现真的存在）
# params   : 控制该项的参数名（用于与 schema 交叉核对）；空 = 无参数开关
# status   : "on" 默认生效 / "opt" 需显式开启 / "off" 未实现
# limit    : 已知近似与缺口（会进 AI 的 must_disclose）

EFFECTS = [
    # ── 取数与线型（源层） ──────────────────────────────────────────
    dict(id="hitran_voigt", cat="source", status="on",
         title="HITRAN 线表 + Voigt 线型（多普勒⊗洛伦兹）",
         evidence=["absorptionCoefficient_Voigt"],
         params=[],
         limit="用 HAPI 的 Voigt 实现，非逐线数值积分；线表只抓窗口内、窗外翼线缺失"
               "（窗口边缘 α 实测可偏低约 6%）"),
    dict(id="self_broadening", cat="source", status="off",
         title="自展宽（组分自身碰撞展宽）",
         evidence=[],
         params=[],
         limit="当前固定用空气浴算 α_pure 再乘 x；实测 HAPI 对 Diluent={'air','self'} "
               "返回相同值，故高浓度（x≳1e-2）离线宽会被低估"),
    dict(id="line_mixing", cat="source", status="off",
         title="线混合（line mixing）",
         evidence=[],
         params=[],
         limit="未建模；高密度谱区（CH4 221 线、C2H6 159 线）的 2f 线形会与实验有偏差"),
    dict(id="dicke_narrowing", cat="source", status="off",
         title="Dicke 变窄（速度依赖碰撞）",
         evidence=[],
         params=[],
         limit="未建模；低压时影响线宽"),

    # ── 仪器链（器件层） ──────────────────────────────────────────
    dict(id="daq_triangle", cat="instrument", status="on",
         title="DAQ 三角波扫描 + 正弦调制",
         evidence=["def daq_triangle"],
         params=["amp_V", "freq_Hz", "phase_deg", "offset_V", "scan_span_cm"],
         limit="理想三角波；未建模上升/下降沿的带宽差异（该现象已在 edge_note 里说明）"),
    dict(id="laser_tuning", cat="instrument", status="on",
         title="激光调谐 V→I→ν→P（二阶泰勒）",
         evidence=["def voltage_to_laser", "d2nu_dI2"],
         params=["eta_VI", "dnu_dI", "d2nu_dI2", "wn_ref", "i_ref", "i_th", "eta_IP"],
         limit="L-I 为严格线性（无热滚降/效率随温度变化）；三阶以上与热迟滞未建模"),
    dict(id="pd_bandwidth", cat="instrument", status="on",
         title="探测器带宽（一阶 RC 低通）",
         evidence=["def pd_lowpass"],
         params=["bw"],
         limit="截止频率被安全钳位到 min(bw, 0.45·fs)：默认 bw=1 MHz / fs=240 kHz 时"
               "实际 fc≈108 kHz，2f 被额外削约 13%"),
    dict(id="adc_quant", cat="instrument", status="on",
         title="ADC 中平量化 + 限幅 + 饱和统计",
         evidence=["np.clip", "lsb"],
         params=["adc_bits", "v_range"],
         limit="理想量化：无 INL/DNL、无 ENOB、无视参考电压噪声"),

    # ── 噪声 ─────────────────────────────────────────────────────
    dict(id="noise_shot", cat="noise", status="on",
         title="散粒噪声 √(2qIΔf)",
         evidence=["2.0 * q * i"],
         params=["bw", "physical_noise"],
         limit="以平均光电流估算（非逐点），弱光下略有偏差"),
    dict(id="noise_thermal", cat="noise", status="on",
         title="跨阻热噪声 √(4kBTΔf/G)",
         evidence=["4.0 * kb * float(T)"],
         params=["bw", "gain", "physical_noise"],
         limit="只含反馈电阻热噪声；未含运放输入电压/电流噪声"),
    dict(id="noise_rin", cat="noise", status="opt",
         title="激光相对强度噪声 RIN",
         evidence=["rin_per_sqrtHz"],
         params=["rin"],
         limit="白噪声形式（与频率无关）；真实 RIN 有弛豫振荡峰"),
    dict(id="noise_drift", cat="noise", status="opt",
         title="1/f 慢漂移（1 Hz 正弦）",
         evidence=["drift_frac"],
         params=["drift_frac"],
         limit="建模为单一 1 Hz 正弦而非随机游走；且 WMS 链路中信号与参考谱含同一实现"
               "→ 背景扣除后会相消（与真实中『漂移扣不掉』的情形不同）"),
    dict(id="noise_flicker", cat="noise", status="opt",
         title="1/f 粉红噪声",
         evidence=["def pink_noise"],
         params=["flicker_frac"],
         limit="全带加权，未与锁相带宽匹配；实测在 2fm±15k 仍有约 23% rms，"
               "会直接污染 2f 锁相带"),
    dict(id="pd_dark_current", cat="detector", status="off",
         title="探测器暗电流及其散粒噪声",
         evidence=[],
         params=[],
         limit="未建模；真实 PD 暗电流是加性直流偏置与噪声源"),
    dict(id="pd_1f_noise", cat="detector", status="off",
         title="探测器/前放 1/f 噪声",
         evidence=[],
         params=[],
         limit="未建模；痕量 TDLAS 中前放 1/f 常为主导项，缺此项使 LOD 偏乐观"),
    dict(id="tia_voltage_noise", cat="detector", status="off",
         title="跨阻放大器输入电压噪声",
         evidence=[],
         params=[],
         limit="未建模；高速/大带宽下常不可忽略"),

    # ── 光路 ─────────────────────────────────────────────────────
    dict(id="etalon_fringe", cat="optics", status="opt",
         title="etalon 干涉条纹（单对平行面 Airy）",
         evidence=["def etalon_transmission", "def fringe_factor"],
         params=["fringe", "fringe_n", "fringe_d_cm", "fringe_R", "fringe_fsr",
                 "fringe_contrast", "fringe_phase_rad", "fringe_drift_frac"],
         limit="只描述一对平行面：忽略楔化、多面叠加、光束发散与偏振 → 条纹幅度是上界"),
    dict(id="throughput_scalar", cat="optics", status="on",
         title="光学总透过率（单一标量）",
         evidence=["throughput"],
         params=["throughput"],
         limit="把窗片/镜片/光纤/连接器折成一个常数；未建模光谱依赖与长期衰减"),
    dict(id="beam_geometry", cat="optics", status="off",
         title="光束几何（发散/孔径/对准失配）",
         evidence=[],
         params=[],
         limit="未建模；准直失配会引起干涉与光程误差"),

    # ── 解调与反演 ────────────────────────────────────────────────
    dict(id="lockin_quadrature", cat="signal", status="on",
         title="数字锁相正交解调 + 整数周期滑动平均",
         evidence=["def wms_harmonic_lockin"],
         params=["lockin_avg", "lockin_stages", "mod_freq_Hz"],
         limit="矩形窗 sinc 旁瓣抑制有限；级联级数越高抑制越好但会模糊线形"),
    dict(id="norm_2f1f", cat="signal", status="on",
         title="归一化 2f/1f 与退化 2f/I0（含背景扣除）",
         evidence=["def wms_calibration_free"],
         params=["background_subtract"],
         limit="1f 实际含吸收分量（默认工况约 50% AM），故 2f/1f 的"
               "「1f∝光强」前提是近似的；k 对 RIN/AM 平衡敏感"),
    dict(id="ram_am", cat="signal", status="opt",
         title="残余幅度调制 RAM（AM/FM 相位差）",
         evidence=["am_psi1", "am_psi2"],
         params=["am_i0", "am_i2", "am_psi1", "am_psi2"],
         limit="**已实现但默认全 0（纯 FM）**：真实 DFB 的 2f/1f 线形与灵敏度 k 强烈依赖 "
               "AM/FM 相位差，故 k 在这一个自由度上尚未被实验验证。调试见 "
               "tools/tdlas_fidelity.py 的 AM/FM 扫描说明"),
    dict(id="inversion_sensitivity", cat="signal", status="on",
         title="免标定浓度反演（灵敏度法）",
         evidence=["def invert_concentration"],
         params=[],
         limit="仅弱吸收成立；k 与工况强绑定，换 T/P/L/调制深度必须重算"),
    dict(id="uncertainty", cat="signal", status="opt",
         title="测量不确定度（统计分量）",
         evidence=["def measurement_uncertainty", "rel_sigma_ci95"],
         params=["n_repeats"],
         limit="**仅含统计（随机）分量**：同工况重复仿真的 run-to-run 散布，"
               "需 `n_repeats>0` 显式开启（默认关）。**不含系统项**：k 标定误差、"
               "数据库不确定度、线型近似、etalon 条纹、光程/温度误差。"
               "小样本（n<10）时 σ 自身不确定度很大，已在返回中给出"),
]

# ── 合法通道白名单 ───────────────────────────────────────────────────
# 这些参数**本就不该**进入引擎：它们是 MCP 层的编排/引用/状态，不是物理量。
# 白名单之外出现"声明了却没进引擎"，即为缺陷。
NON_ENGINE_CHANNELS = {
    "setup", "laser", "pd", "daq", "optics",   # 设备库引用：由 _resolve_devices 解析
    "session_id",                              # 会话态（跨会话记忆）
    "save_png",                                # 出图开关
    "auto_laser",                              # 目标波数越界时的自动重锚开关
}


def _read_sources():
    out = {}
    for rel in ("tdlas_sim.py", "tools/tdlas_mcp.py", "tools/tdlas_hitran.py"):
        p = _ROOT / rel
        if p.exists():
            out[rel] = p.read_text(encoding="utf-8", errors="replace")
    return out


def audit_effects():
    """检查登记表里声称的 evidence 是否真的存在于源码（防"表漂了"）。"""
    src = _read_sources()
    blob = "\n".join(src.values())
    problems = []
    for e in EFFECTS:
        if e["status"] == "off":
            if e["evidence"]:
                problems.append(f"{e['id']}: 标为未实现，却给了 evidence {e['evidence']}")
            continue
        if not e["evidence"]:
            problems.append(f"{e['id']}: 标为已实现，却没有 evidence（无法自证）")
            continue
        for token in e["evidence"]:
            if token not in blob:
                problems.append(f"{e['id']}: evidence 未在源码中找到 → {token!r}")
    return problems


def audit_parameters():
    """用假引擎截获真正进入引擎的 kwargs，找出"声明了却无效"的参数。

    返回 [(工具名, 死参数列表), ...]；`NON_ENGINE_CHANNELS` 不计入。
    """
    import tdlas_mcp as M

    class _Stop(Exception):
        pass

    SENT = -1234567
    cases = [
        ("tdlas_wms_instrument", "simulate_wms_instrument", dict(
            species="CH4", wn_center=2968.5, scan_span_cm=SENT, amp_V=SENT, freq_Hz=SENT,
            offset_V=SENT, phase_deg=SENT, eta_VI=SENT, dnu_dI=SENT, d2nu_dI2=SENT,
            wn_ref=SENT, i_ref=SENT, i_th=SENT, eta_IP=SENT, fs=SENT, n_samples=SENT,
            adc_bits=SENT, v_range=SENT, throughput=SENT, resp=SENT, gain=SENT, bw=SENT,
            rin=SENT, fringe=True, fringe_n=SENT, fringe_d_cm=SENT, fringe_R=SENT,
            fringe_fsr=SENT, fringe_contrast=SENT, fringe_phase_rad=SENT,
            fringe_drift_frac=SENT, mod_freq_Hz=SENT, mod_amp_V=SENT, m_opt=SENT,
            lockin_avg=SENT, lockin_stages=SENT, drift_frac=SENT, flicker_frac=SENT,
            background_subtract=False, trim_frac=SENT, edge="rising", am_i0=SENT,
            am_i2=SENT, am_psi1=SENT, am_psi2=SENT, physical_noise=False, allow_partial_dark=True)),
        ("tdlas_das_instrument", "simulate_das_instrument", dict(
            species="CH4", wn_center=2968.5, edge="rising", fit_order=3, fit_frac=0.3,
            scan_span_cm=SENT, amp_V=SENT, freq_Hz=SENT, offset_V=SENT, phase_deg=SENT,
            eta_VI=SENT, dnu_dI=SENT, d2nu_dI2=SENT, wn_ref=SENT, i_ref=SENT, i_th=SENT,
            eta_IP=SENT, fs=SENT, n_samples=SENT, adc_bits=SENT, v_range=SENT,
            throughput=SENT, resp=SENT, gain=SENT, bw=SENT, rin=SENT, fringe=True,
            fringe_n=SENT, fringe_d_cm=SENT, fringe_R=SENT, fringe_fsr=SENT,
            fringe_contrast=SENT, fringe_phase_rad=SENT, fringe_drift_frac=SENT,
            physical_noise=False, allow_partial_dark=True, drift_frac=SENT, flicker_frac=SENT)),
    ]

    results = []
    for tool, func_name, args in cases:
        fn = M.DISPATCH[tool]
        orig = getattr(M.ts, func_name, None)
        if orig is None:
            results.append((tool, ["<引擎函数缺失>"]))
            continue
        store = {}

        def fake(*a, **k):
            store.update(k)
            for i, p in enumerate(inspect.signature(orig).parameters.values()):
                if i < len(a) and p.kind in (p.POSITIONAL_ONLY, p.POSITIONAL_OR_KEYWORD):
                    store[p.name] = a[i]
            raise _Stop()

        setattr(M.ts, func_name, fake)
        try:
            fn(**args)
        except _Stop:
            pass
        except Exception:
            pass
        finally:
            setattr(M.ts, func_name, orig)

        declared = set((next(t for t in M.TOOLS if t["name"] == tool)
                        .get("inputSchema") or {}).get("properties") or {})
        dead = sorted(declared - set(store) - NON_ENGINE_CHANNELS)
        results.append((tool, dead))
    return results


def missing_effects(statuses=("off",)):
    return [e for e in EFFECTS if e["status"] in statuses]


def fidelity_summary():
    """供 MCP 返回给 AI 的结构化保真度报告。"""
    return {
        "implemented": [
            {"id": e["id"], "title": e["title"], "params": e["params"], "limit": e["limit"]}
            for e in EFFECTS if e["status"] == "on"],
        "optional": [
            {"id": e["id"], "title": e["title"], "params": e["params"], "limit": e["limit"]}
            for e in EFFECTS if e["status"] == "opt"],
        "not_implemented": [
            {"id": e["id"], "category": e["cat"], "title": e["title"], "impact": e["limit"]}
            for e in EFFECTS if e["status"] == "off"],
        "disclosure_rule": "对任何定量结论，必须告知：① 本次用了哪些已实现效应；"
                           "② 是否有未实现项会影响该结论（见 not_implemented）；"
                           "③ 不得把点值当作带误差的结果呈现。",
    }


def main():
    bad = 0
    print("=" * 74)
    print("tdlas-mcp 保真度审计（能力登记表 ↔ 源码证据 ↔ 参数可达性）")
    print("=" * 74)

    print("\n=== 1. 登记表证据一致性 ===")
    problems = audit_effects()
    if problems:
        bad += len(problems)
        for p in problems:
            print(f"  FAIL  {p}")
    else:
        print(f"  PASS  {len(EFFECTS)} 项效应的 evidence 全部在源码中找到")

    print("\n=== 2. 参数可达性（声明了却没进引擎 = 缺陷）===")
    for tool, dead in audit_parameters():
        if dead:
            bad += len(dead)
            print(f"  FAIL  {tool}: 声明的 {len(dead)} 个参数从未进入引擎 → {dead}")
        else:
            print(f"  PASS  {tool}: schema 声明的物理参数全部可达")

    print("\n=== 3. 已知缺口（必须向用户披露）===")
    offs = missing_effects()
    for e in offs:
        print(f"  [{e['cat']:10s}] {e['title']}")
    print(f"\n  共 {len(offs)} 项未实现；另有 "
          f"{len(missing_effects(('opt',)))} 项需显式开启")

    print(f"\n===== 保真度审计：{'全部通过' if not bad else str(bad) + ' 项问题'} =====")
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(main())
