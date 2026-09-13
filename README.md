<div align="center">

<h1>tdlas-mcp</h1>

<p><b>TDLAS/WMS 光谱仿真 MCP 服务器</b><br>
仪器系统级链路仿真 · 波长调制 · 数字锁相 · 谐波分析 · AI 主动交互</p>

<img src="https://img.shields.io/badge/Python-3.10%2B-3776AB" alt="Python">
<img src="https://img.shields.io/badge/MCP-stdio%20JSON--RPC-6B8FD4" alt="MCP">
<img src="https://img.shields.io/badge/HITRAN-HAPI%201.x-4C8C4A" alt="HITRAN">

</div>

---

## 项目简介

`tdlas-mcp` 是 TDLAS（可调谐二极管激光吸收光谱）的**仪器系统级仿真 MCP 服务器**，把真实实验链路逐级建模：

```
DAQ 驱动电压（三角波 + 正弦调制）
  → 激光器调谐（V → 电流 → 波数/功率）
  → 光路损耗 + 气体吸收（HITRAN 数据库）
  → 探测器 PD（响应度/跨阻/带宽/噪声）
  → ADC 量化
  → 数字锁相（正交解调 + 多级低通）
  → 1f / 2f / 2f-1f 归一化
```

通过 MCP 协议接入 AI 助手，用自然语言描述实验即可完成仿真。

> 💡 本项目**自包含**：HITRAN 线表取数与吸收谱计算由仓库内 `tools/tdlas_hitran.py` 提供（仅用 HAPI 1.x，**无需 API key**，不依赖 Hitran MCP）。

## 核心特性

| 特性 | 说明 |
|------|------|
| **仪器系统级仿真** | 不只看"光谱"，而是完整链路（电压→激光→光路→PD→ADC→锁相） |
| **数字锁相** | 正交解调 + 2 级级联低通（sinc²，残留 4.1%→1.5%），调制系数 m≈2.2 自动优化 |
| **AI 主动交互** | 内置 `AI_INTERACTION_GUIDE`：正向 SOP、参数索取优先级、术语表、澄清问题生成器 |
| **自动校验** | 每次结果附 9 项校验报告（DAS-理论一致性、2f 峰位、αL 弱吸收、采样率…） |
| **跨会话状态机 + 设备库** | `tdlas_session` 记住已确认工况参数；`tdlas_device` 统一管理固定仪器（激光器/探测器/DAQ/光学），一键引用 |
| **统一出图** | 固定 3×2 六子图（驱动/PD原始/DAS+理论/1f/2f/归一化），技术名+线型必标 |

## 工具列表（12 个）

| 工具 | 说明 |
|------|------|
| `tdlas_simulate` | DAS + 免标定 WMS 正向仿真 → 1f/2f 峰高、透过率、线表信息 |
| `tdlas_das_chain` | 三角波 DAS 全链路：PD 原始信号 → 基线拟合 → 吸光度 |
| `tdlas_das_instrument` | 仪器级 DAS：DAQ 电压 → 激光 → 光路 → PD → ADC |
| `tdlas_wms_instrument` | **WMS 仪器链路**：扫描+调制 → 锁相 → 1f/2f/归一化（含背景扣除/校验/澄清/工况推荐） |
| `tdlas_review` | 二次审核：返回自动校验报告（不画图） |
| `tdlas_session` | 对话状态机：跨会话记住已确认参数 |
| `tdlas_device` | 设备库管理：命名保存激光器/探测器/DAQ/光学，打包整机配置，设默认设备 |
| `tdlas_guide` | AI 主动指导协议（SOP/优先级/术语表/参数指南/器件检索） |
| `tdlas_invert` | 免标定浓度反演：2f/1f 峰高 → 摩尔分数 |
| `tdlas_detection_limit` | 检测限：噪声 → NEC / LOD |
| `tdlas_detection_limit_scan` | 检测限扫描：LOD 随光程 L / 浓度 x 的网格（选型用） |
| `tdlas_selftest` | 全链路自检 |

## 快速开始

```bash
pip install -r requirements.txt   # numpy / matplotlib / scipy / hitran-api

python tdlas_sim.py                    # 全链路自测 + 出图
python tdlas_sim.py --selftest         # 只跑自测
python tools/tdlas_mcp.py --selftest   # MCP 服务器自检
```

接入 AI 助手：把 `mcp.config.example.json` 的 `args` 路径改成你的实际路径，加入 MCP 客户端配置。

## 依赖

- 自包含 HITRAN 取数（`tools/tdlas_hitran.py`，HAPI 1.x，免 key，首次运行自动下载线表缓存到 `Hitran_Data/`）
- **不依赖 Hitran MCP server**，可独立部署运行

## 文档

| 文档 | 定位 |
|------|------|
| [TECHNICAL.md](./TECHNICAL.md) | **技术文档**：物理原理、逐级链路实现、关键算法、默认参数表、已知近似、**实现陷阱（踩坑记录）** |
| [SKILL.md](./SKILL.md) | AI 使用规范（触发条件、工具列表、必须遵守的交互规则） |
| [CHANGELOG.md](./CHANGELOG.md) | 功能演进记录 |

## 引用（Citing）

若你在论文或研究中使用了本工具，请：

1. **声明代码可用性**（可粘贴到论文的 Code availability 段）：

   > The TDLAS/WMS instrument-level simulations were performed using the open-source package **tdlas-mcp** (https://github.com/LKF0402/tdlas-mcp, GPLv3).

2. **引用底层数据与方法**（必须，否则学术不规范）：

   - **HAPI**：R.V. Kochanov, I.E. Gordon, L.S. Rothman, P. Wcislo, C. Hill, J.S. Wilzewski, *J. Quant. Spectrosc. Radiat. Transfer* 177, 15–30 (2016). DOI: 10.1016/j.jqsrt.2016.03.005
   - **HITRAN2024**：I.E. Gordon et al., *J. Quant. Spectrosc. Radiat. Transfer* (2026). DOI: 10.1016/j.jqsrt.2026.109807

3. **作者身份**：本仓库由 GitHub 账号 `LKF0402` 维护。若论文署名与该账号名不一致，请在论文脚注或本仓库注明对应关系，便于审稿人确认。

## 许可证

GPLv3

## References

- HAPI: https://github.com/hitranonline/hapi
- HITRAN 数据库: https://hitran.org
