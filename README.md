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

`tdlas-mcp` 是面向 TDLAS（可调谐二极管激光吸收光谱）的仪器系统级仿真 MCP 服务器，对完整实验链路逐级建模：

```
DAQ 驱动电压（三角波 + 正弦调制）
  → 激光器调谐（V → 电流 → 波数/功率）
  → 光路损耗 + 气体吸收（HITRAN 数据库）
  → 探测器 PD（响应度/跨阻增益/带宽/噪声）
  → ADC 量化
  → 数字锁相（正交解调 + 多级低通）
  → 1f / 2f / 2f-1f 归一化
```

通过 MCP 协议接入 AI 助手，以自然语言描述实验条件即可完成仿真计算。

> 本项目自包含：HITRAN 线表获取与吸收谱计算由仓库内 `tools/tdlas_hitran.py` 实现（基于 HAPI 1.x，无需 API key，不依赖其他 MCP 服务）。

## 核心特性

| 特性 | 说明 |
|------|------|
| 仪器系统级仿真 | 覆盖完整信号链路（电压→激光→光路→PD→ADC→锁相），而非仅计算光谱 |
| 数字锁相 | 正交解调 + 2 级级联低通（sinc² 抑制，残留 4.1%→1.5%），调制系数 m≈2.2 自动寻优 |
| AI 主动交互 | 内置交互协议：正向 SOP、参数索取优先级、术语表、澄清问题生成 |
| 自动校验 | 每次结果附 9 项校验报告（DAS-理论一致性、2f 峰位、αL 弱吸收判据、采样率等） |
| 跨会话状态机 + 设备库 | `tdlas_session` 持久化已确认工况参数；`tdlas_device` 统一管理仪器硬件参数 |
| 标准化出图 | 固定 3×2 六子图布局，标注技术名称与线型含义 |

## 工具列表（12 个）

| 工具 | 说明 |
|------|------|
| `tdlas_simulate` | DAS + 免标定 WMS 正向仿真：输出 1f/2f 峰高、透过率、线表信息 |
| `tdlas_das_chain` | 三角波 DAS 全链路：PD 原始信号 → 多项式基线拟合 → 吸光度 |
| `tdlas_das_instrument` | 仪器级 DAS：DAQ 电压 → 激光调谐 → 光路吸收 → PD → ADC |
| `tdlas_wms_instrument` | WMS 仪器链路：扫描+调制 → 锁相 → 1f/2f/归一化（含背景扣除、校验、参数澄清） |
| `tdlas_review` | 二次审核：返回自动校验报告（不绘图） |
| `tdlas_session` | 对话状态机：跨会话持久化工况参数 |
| `tdlas_device` | 设备库管理：命名保存激光器/探测器/DAQ/光学参数，支持整机配置导出 |
| `tdlas_guide` | AI 交互指导协议（SOP、参数优先级、术语表、参数检索规则） |
| `tdlas_invert` | 免标定浓度反演：由 2f/1f 峰高计算摩尔分数 |
| `tdlas_detection_limit` | 检测限分析：由噪声水平计算 NEC / LOD |
| `tdlas_detection_limit_scan` | 检测限扫描：LOD 随光程 L / 浓度 x 的二维网格（系统选型用） |
| `tdlas_selftest` | 全链路自检 |

## 快速开始

```bash
pip install -r requirements.txt   # numpy / matplotlib / scipy / hitran-api

python tdlas_sim.py                    # 全链路自测 + 出图
python tdlas_sim.py --selftest         # 仅运行自测
python tools/tdlas_mcp.py --selftest   # MCP 服务器自检
```

接入 AI 助手：将 `mcp.config.example.json` 中 `args` 路径修改为实际安装路径，添加至 MCP 客户端配置即可。

> **建议同时安装 SKILL.md**：将仓库根目录的 [`SKILL.md`](./SKILL.md) 作为独立 Skill 安装至 AI Agent（如 Claude Code / CodeBuddy / 豆包）。
> 仅通过 MCP 工具描述，AI 仅能获知可用工具列表；安装 SKILL.md 后，AI 可读取完整的交互协议、参数索取优先级、澄清流程、出图规范与物理约束，输出质量显著提升。

## HTTP 模式（远程直链）

默认模式为 stdio（适用于本机 MCP 客户端）。若需远端 MCP 客户端通过 URL 直链调用，使用 `--http` 参数启动 HTTP 服务：

```bash
python tools/tdlas_mcp.py --http --host 0.0.0.0 --port 8000 --token <密钥>
```

- 端点固定为 `http://<host>:<port>/mcp`，远端客户端填写该 URL（MCP Streamable HTTP，兼容 `application/json` 与 `text/event-stream`）
- `--host 0.0.0.0` 监听所有网卡（适用于局域网/公网部署）；仅本机测试使用 `127.0.0.1`
- `--token` 为 Bearer 鉴权，远程部署时必须配置；未设置则无访问控制
- 仍支持 `--selftest`；未指定 `--http` 时行为与 stdio 模式一致

客户端配置示例（参见 `mcp.config.example.json` 中 `tdlas-http` 项）：

```json
{ "mcpServers": { "tdlas": { "type": "http", "url": "http://<host>:8000/mcp" } } }
```

> 远端访问需在防火墙/路由器中放行对应端口，或通过反向隧道（如 cloudflared / ngrok）暴露；公网部署必须配合 `--token` 使用。

### 公网暴露（cloudflared 隧道，推荐）

公网/异网客户端直链的推荐方案为 cloudflared 隧道：无需开放防火墙端口，Cloudflare 边缘节点隐藏源 IP，全程加密。`tools/remote_link.py` 已封装「本地服务 + 隧道建立 + URL/Token 输出」流程：

```bash
HTTPS_PROXY=http://127.0.0.1:7892 python tools/remote_link.py
# 或指定固定 token / 端口：
# TDLAS_TOKEN=<密钥> TDLAS_PORT=8000 python tools/remote_link.py
```

- 服务器仅监听 `127.0.0.1`，隧道在本地建立连接，外部无法直接访问本机端口（最小暴露面）
- 强制 Bearer token 鉴权（随机生成或取 `TDLAS_TOKEN`）；无 token 请求返回 401
- 首次建隧道后 Cloudflare 边缘证书签发约需 60–90 秒，客户端重试即可（频繁建/拆隧道会触发边缘限流，导致 TLS 握手失败）
- 经 HTTP 代理出口时，脚本自动使用 `HTTPS_PROXY` 并切换 `--protocol http2`（HTTP 代理仅支持 TCP 转发）
- 停止服务：Ctrl+C（同时关闭服务器与隧道）

> 安全说明：公网 URL 本身公开，但必须携带有效 Bearer token 才能调用工具；请勿将 token 提交至仓库。需要固定 URL 时，建议使用 Cloudflare 账号配置 named tunnel 并设置固定 token。

## 依赖

- 自包含 HITRAN 线表获取模块（`tools/tdlas_hitran.py`，基于 HAPI 1.x，无需 API key，首次运行自动下载线表并缓存至 `Hitran_Data/`）
- 不依赖其他 MCP 服务，可独立部署运行

## 说明

- **不支持多次扫描算术平均**：多次扫描平均仅对白噪声（散粒噪声、热噪声、RIN）有效，按 √N 系数降低；1/f 慢漂移与粉红噪声（低频相关）经平均后基本不衰减。若需模拟 N 次平均效果，仅需将白噪声参数（如 `rin`、`sigma_tau`）按 1/√N 比例减小，1/f 相关参数（`drift_frac`、`flicker_frac`）保持原值即可。

## 文档

| 文档 | 定位 |
|------|------|
| [TECHNICAL.md](./TECHNICAL.md) | 技术文档：物理原理、逐级链路实现、关键算法、默认参数表、已知近似、实现注意事项 |
| [SKILL.md](./SKILL.md) | AI 使用规范：触发条件、工具列表、交互规则 |
| [CHANGELOG.md](./CHANGELOG.md) | 版本更新记录 |

## 引用（Citing）

若在论文或研究中使用了本工具，请：

1. **声明代码可用性**（可粘贴至论文 Code availability 段）：

   > The TDLAS/WMS instrument-level simulations were performed using the open-source package **tdlas-mcp** (https://github.com/LKF0402/tdlas-mcp, GPLv3).

2. **引用底层数据与方法**（必须）：

   - **HAPI**：R.V. Kochanov, I.E. Gordon, L.S. Rothman, P. Wcislo, C. Hill, J.S. Wilzewski, *J. Quant. Spectrosc. Radiat. Transfer* 177, 15–30 (2016). DOI: 10.1016/j.jqsrt.2016.03.005
   - **HITRAN2024**：I.E. Gordon et al., *J. Quant. Spectrosc. Radiat. Transfer* (2026). DOI: 10.1016/j.jqsrt.2026.109807

3. **作者信息**：本仓库由 GitHub 账号 `LKF0402` 维护。若论文署名与该账号名不一致，请在论文脚注或本仓库中注明对应关系，便于审稿人确认。

## 许可证

GPLv3

## References

- HAPI: https://github.com/hitranonline/hapi
- HITRAN 数据库: https://hitran.org
