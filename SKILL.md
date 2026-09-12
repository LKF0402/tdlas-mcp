---
name: tdlas-mcp
description: TDLAS/WMS spectroscopic simulation MCP server. Use when the user mentions TDLAS, WMS, wavelength modulation spectroscopy, harmonic detection, 2f/1f signal, lock-in detection, gas concentration inversion, detection limit, DAS, or asks to simulate/plot absorption spectra.
---

# tdlas-mcp：TDLAS/WMS 光谱仿真 MCP 服务器

通过 MCP 协议接入 AI 助手，用自然语言完成 TDLAS 仿真，包括 DAS（直接吸收光谱）与 WMS（波长调制光谱）。**仪器系统级仿真**：从 DAQ 驱动电压到数字锁相谐波提取，逐级建模真实实验链路。

## 触发条件

- TDLAS / WMS / 波长调制光谱 / 直接吸收光谱 DAS
- 二次谐波 / 2f 信号 / 一次谐波 / 1f 信号 / 2f/1f 归一化
- 锁相放大 / 谐波检测 / 数字锁相
- 气体浓度反演 / 检测限 LOD
- 调制深度 / 调制频率 / 调制系数 m

## 前提条件

本项目**自包含**，无需 API key，不依赖其它仓库：

1. `pip install -r requirements.txt`（numpy / matplotlib / scipy / hitran-api）
2. 首次运行自动从 HITRAN 下载线表并缓存到 `Hitran_Data/`

> HITRAN 取数仅用 HAPI 1.x（官方接口，免 key）；**不依赖 Hitran MCP server**。

## 工具列表（10 个）

| 工具 | 说明 |
|------|------|
| `tdlas_simulate` | DAS + 免标定 WMS 正向仿真 → 1f/2f 峰高、透过率 |
| `tdlas_das_chain` | 三角波 DAS 全链路：PD 原始信号 → 基线拟合 → 吸光度 |
| `tdlas_das_instrument` | 仪器级 DAS：DAQ 电压 → 激光 → 光路 → PD → ADC |
| `tdlas_wms_instrument` | **WMS 仪器链路**（核心）：扫描+调制 → 锁相 → 1f/2f/归一化 |
| `tdlas_review` | 二次审核：自动校验报告（9 项，不画图） |
| `tdlas_session` | 对话状态机：跨会话记住已确认参数 |
| `tdlas_guide` | AI 主动指导协议（SOP/优先级/术语表/参数指南） |
| `tdlas_invert` | 免标定浓度反演：2f/1f 峰高 → 摩尔分数 |
| `tdlas_detection_limit` | 检测限：噪声 → NEC / LOD |
| `tdlas_selftest` | 全链路自检 |

## 使用规范（AI 必须遵守）

### 1. 先调 `tdlas_guide`

开始任何 TDLAS 任务前，先调 `tdlas_guide` 读协议：参数索取优先级、正向 SOP、术语表、`das_vs_wms` 选型、`image_layout` 图格式、`clarify_protocol` 澄清流程。

### 2. 主动澄清（硬约束）

返回的 `clarify.needed=True` 时，**先向用户提出 `clarify.questions` 里的澄清问题，收到回答前不要直接出图/下结论**。用户说"用默认值"才可跳过。

参数索取优先级：① 实测值 → ② 器件型号（AI 检索规格书）→ ③ 现场标定 → ④ 默认值并显式标注。

### 3. 二次审核（硬约束）

每次出图/下结论前用 `tdlas_review` 或返回的 `validation` 校验。`overall=fail` 时不得下结论，先修 fail 项。

### 4. 技术标注

- 出图用固定 **3×2 六子图**格式（见 `tdlas_guide` 的 `image_layout`）
- 技术名、归一化方法、基线处理、线型含义必须标注
- 1f 与 2f 分开（幅值量级不同）

### 5. 关键物理事实（已踩坑沉淀）

- **横轴用扫描波数**（非瞬时波数，瞬时波数含 ±a 调制摆动）
- **DAS 是无调制独立链路**（不能拿含调制信号平均伪造）
- **1f 过零是物理奇点**（AM 与吸收 1f 相消），不是"1f 失效"
- **2f/1f 失效判据**：仅"非吸收区泄漏 > 峰值 30%"（1f 基线漂移），不是过零
- **密集谱区**（如 CH4 2968.5 有 221 条线）2f 是多峰叠加，看标准双峰要选孤立单线

## 注意事项

- 本项目是 MCP 服务器，无桌面 GUI
- 数据基于 HITRAN，结果可复现
- 首次使用下载线表，后续复用缓存

## 项目地址

- GitHub: https://github.com/LKF0402/tdlas-mcp
- HAPI: https://github.com/hitranonline/hapi
