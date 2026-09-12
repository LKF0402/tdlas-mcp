---
name: tdlase-mcp
description: TDLAS/WMS spectroscopic simulation MCP server. Use when the user mentions TDLAS, WMS, wavelength modulation spectroscopy, harmonic detection, 2f signal, lock-in detection, gas concentration inversion, or asks to simulate wavelength modulation spectroscopy signals.
---

# tdlase-mcp：TDLAS/WMS 光谱仿真 MCP 服务器

通过 MCP 协议接入 AI 助手，用自然语言完成 TDLAS（可调谐二极管激光吸收光谱）仿真，包括 DAS（直接吸收光谱）和 WMS（波长调制光谱）。

## 触发条件

当用户提到以下关键词时触发：

- TDLAS / WMS / 波长调制光谱
- 二次谐波 / 2f 信号 / 一次谐波 / 1f 信号
- 锁相放大 / 谐波检测
- 气体浓度反演
- 调制深度 / 调制频率
- 免定标 WMS / calibration-free WMS

## 前提条件

本项目**自包含**：无需 API key，也不依赖其它仓库。只需：

1. `pip install -r requirements.txt`（numpy / matplotlib / scipy / hitran-api）
2. 首次运行会自动从 HITRAN 下载线表并缓存到 `Hitran_Data/`（后续复用缓存）

> HITRAN 取数仅用 **HAPI 1.x**（官方接口，不校验 key）；刻意不用 HAPI2（其 v2 API 需 key）。

## 工具列表

| 工具 | 说明 |
|------|------|
| `tdlas_simulate` | DAS + 免标定 WMS 正向仿真 → 1f/2f 谐波峰高、DAS 透过率、线表信息（可选出图） |
| `tdlas_das_chain` | 三角波 DAS 全链路：PD 原始信号 → 多项式基线拟合扣除 → 吸光度（可出四层链路图） |
| `tdlas_invert` | 免标定浓度反演：2f/1f 峰高 → 摩尔分数（仅弱吸收 αL ≪ 1） |
| `tdlas_detection_limit` | 检测极限：等效透过率噪声 σ_τ → NEC / LOD（默认 3σ） |
| `tdlas_selftest` | 全链路自检 |

## 使用规范

1. **单位**：
   - 波数：cm⁻¹
   - 温度：K
   - 压力：atm
   - 浓度：摩尔分数（0~1）或 ppm
   - 调制深度：cm⁻¹
   - 调制频率：kHz

2. **典型工作流**：
   ```
   ① 选择分子与波数窗口 → ② 配置 T/P/浓度 → ③ 设置调制参数 → ④ 运行 WMS 仿真 → ⑤ 分析 2f 信号
   ```

3. **2f 是核心**：WMS 的主要输出是 2f 信号，与气体浓度成正比；1f 主要用于背景归一化和光源强度校正。

## 注意事项

- 本项目是 **MCP 服务器**，没有桌面 GUI
- 数据基于 HITRAN 数据库，结果可复现
- 首次使用会下载线表数据，后续自动复用缓存

## 项目地址

- GitHub: https://github.com/LKF0402/tdlas-mcp
- 取数依赖: HAPI — https://github.com/hitranonline/hapi（HITRAN 官方接口，免 key）
