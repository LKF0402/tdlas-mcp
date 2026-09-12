<div align="center">

<h1>tdlas-mcp</h1>

<p><b>TDLAS/WMS 光谱仿真 MCP 服务器</b><br>
波长调制光谱 · 谐波分析 · 锁相检测 · 自然语言驱动</p>

<a href="LICENSE"><img src="https://img.shields.io/badge/License-GPLv3-blue.svg" alt="GPLv3"></a>
<img src="https://img.shields.io/badge/Python-3.10%2B-3776AB" alt="Python">
<img src="https://img.shields.io/badge/MCP-stdio%20JSON--RPC-6B8FD4" alt="MCP">

</div>

[English](./README.en.md) | 中文

---

## 项目简介

`tdlas-mcp` 是 TDLAS（可调谐二极管激光吸收光谱）技术的仿真 MCP 服务器，支持：

- **DAS**（直接吸收光谱）—— 基于 HITRAN 数据库的吸收谱计算
- **WMS**（波长调制光谱）—— 波长扫描 + 谐波提取 + 锁相检测仿真

通过 MCP 协议接入 AI 助手，用自然语言描述实验参数即可完成仿真。

> 💡 本项目**自包含**：HITRAN 线表取数与吸收谱计算由仓库内 `tools/tdlas_hitran.py` 提供（仅用 HAPI 1.x，**无需 API key**）。

## 功能规划

| 模块 | 状态 | 说明 |
|------|------|------|
| 直接吸收光谱（DAS） | ✅ 已实现 | `simulate()` 返回透过率 τ(ν)；吸收谱实时取自 HITRAN |
| 波长扫描模型 | ✅ 已实现 | `span` / `n_scan` 控制扫描范围与采样；时域模式为余弦扫描 |
| 调制深度 / 调制频率 | ✅ 已实现 | `a` 调制深度；`i0 / i2 / psi1 / psi2` 强度调制参数 |
| 数字锁相放大器 | ✅ 已实现 | `wms_harmonic_lockin()` 正交解调 + 整数调制周期滑动平均 |
| **2f 谐波提取** | ✅ 已实现 | `wms_calibration_free()` 免标定解析式（1f/2f/4f 的 X/Y） |
| 1f 谐波提取 | ✅ 已实现 | 同上，用于背景归一化 |
| 免定标 WMS 模型 | ✅ 已实现 | 1f 归一化 + 背景扣除（弱场线性度 0.999） |
| 浓度反演 | ✅ 已实现 | `sensitivity_2f1f()` / `invert_concentration()`，闭环误差 < 1% |
| 检测极限（LOD） | ✅ 已实现 | `detection_limit()` 蒙特卡洛 → NEC / LOD |

## 快速开始

```bash
python tdlas_sim.py                    # 全链路自测 + 出图
python tdlas_sim.py --selftest         # 只跑自测（有线/2f形状/弱场线性/反演闭环/检测限/时域交叉验证）
python tools/tdlas_mcp.py --selftest   # MCP 服务器自检
```

无需 API key，也不依赖其它仓库 —— 只需 `pip install -r requirements.txt`（numpy / matplotlib / scipy / hitran-api）。

## 许可证

GPLv3

## References

- HAPI（HITRAN 官方接口，本项目取数依赖）: https://github.com/hitranonline/hapi
- HITRAN 数据库: https://hitran.org
