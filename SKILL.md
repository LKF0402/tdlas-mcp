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

本项目依赖 **hitran-mcp** 的 HITRAN 线表获取与吸收谱计算引擎。请确保：

1. hitran-mcp 已配置完成（见 https://github.com/LKF0402/hitran-mcp）
2. HITRAN 线表缓存已就绪

## 工具列表

### 🔬 光谱计算

| 工具 | 说明 | 关键参数 |
|------|------|---------|
| `tdlas_das_spectrum` | 直接吸收光谱（DAS）计算 | `name`, `numin`, `numax`, `T`, `P`, `mole_frac` |
| `tdlas_wms_spectrum` | 波长调制光谱（WMS）仿真 | `name`, `center`, `mod_depth`, `mod_freq`, `T`, `P`, `mole_frac` |

### 📈 谐波分析

| 工具 | 说明 | 关键参数 |
|------|------|---------|
| `tdlas_harmonic_2f` | 2f 二次谐波提取（核心） | 同 WMS，输出 2f 信号 |
| `tdlas_harmonic_1f` | 1f 一次谐波提取 | 同 WMS，输出 1f 信号（背景归一化用） |

### 🔧 其他

| 工具 | 说明 |
|------|------|
| `tdlas_status` | 服务器状态与配置检查 |

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
- 依赖: https://github.com/LKF0402/hitran-mcp
