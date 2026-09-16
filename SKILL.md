---
name: tdlas-mcp
description: TDLAS/WMS 光谱仿真 MCP 服务器。触发词：TDLAS、WMS、波长调制、直接吸收、DAS、二次谐波、2f/1f、数字锁相、谐波检测、气体浓度反演、检测限、LOD、调制系数 m。用户要求模拟/绘制吸收光谱或 WMS 信号时必须使用本 Skill。
---

# tdlas-mcp：TDLAS/WMS 仪器级仿真 MCP 服务器

**版本 v0.1.0**

通过 MCP 协议接入 AI 助手，以自然语言完成 TDLAS 实验仿真。对实验全链路逐级建模：DAQ 驱动电压 → 激光调谐 → HITRAN 气体吸收 → 光电探测 → ADC 量化 → 数字锁相 → 谐波提取与归一化。

## 触发条件

| 类别 | 关键词 |
|------|--------|
| 光谱技术 | TDLAS、WMS、DAS、波长调制光谱、直接吸收光谱 |
| 谐波信号 | 2f、1f、二次谐波、一次谐波、2f/1f 归一化、谐波检测 |
| 检测方法 | 数字锁相、锁相放大、正交解调 |
| 分析应用 | 浓度反演、检测限、LOD、NEC、系统选型 |
| 调制参数 | 调制深度、调制频率、调制系数 m、最优调制 |

## 前置条件

本项目自包含，无需 API key，不依赖其他 MCP 服务：

1. `pip install -r requirements.txt`（numpy / matplotlib / scipy / hitran-api）
2. 首次运行自动从 HITRAN 下载线表并缓存至 `Hitran_Data/`

> 线表获取基于 HAPI 1.x（官方接口，免 key）。

## 工具路由

| 场景 | 必选工具 | 说明 |
|------|----------|------|
| **画 WMS 图** | `tdlas_wms_instrument` | 仪器级全链路，标准 3×2 六子图输出 |
| **画 DAS 图** | `tdlas_das_instrument` | 仪器级 DAS，五层链路图 |
| DAS 信号处理 | `tdlas_das_chain` | PD 原始信号 → 基线拟合 → 吸光度 |
| 快速算峰高数值 | `tdlas_simulate` | 解析模型，不出仪器链路图 |
| 结果校验 | `tdlas_review` | 10 项自动检查，不绘图 |
| 浓度反演 | `tdlas_invert` | 2f/1f 峰高 → 摩尔分数 |
| 检测限分析 | `tdlas_detection_limit` / `tdlas_detection_limit_scan` | 单值 / 网格扫描 |
| 硬件参数管理 | `tdlas_device` | 命名保存/引用激光器/PD/DAQ/光学 |
| 工况记忆 | `tdlas_session` | 跨会话参数持久化 |
| 协议查询 | `tdlas_guide` | SOP、术语表、参数指南 |
| 系统自检 | `tdlas_selftest` | 全链路验证 |

## AI 操作规范（必须遵守）

### 0. 交互契约（最高优先级：先读返回值，再谈表达）

每次仿真工具返回里都有两个**机器可读**字段，取代了过去散在文档里的散文规范：

```json
"interaction":  {"stage": "clarify|produced|verified",
                 "physics_ok": true, "result_usable": true,
                 "conclusion_allowed": true, "blocking_reason": null,
                 "disclosure_pending": [...]},
"next_required_actions": [{"id": "disclose_defaults",
                           "severity": "must_disclose",
                           "action": "向用户说明下列参数用了默认值……",
                           "evidence_fields": ["assumptions"],
                           "values": {...}, "template": "……"}]
```

**执行方式：按 `next_required_actions` 逐条照做，`values` 里就是要用的数字（别自己另找）。**

- `severity` 三级：
  - `block_conclusion` → **不得给出定量结论**（只剩"这个数本身不可用"：校验 fail / 扫描含暗区 / 反演越界）；此时 `interaction.conclusion_allowed=false`，看 `blocking_reason`。
  - `must_disclose` → 结论可以给，但**必须在同一回答里说明**；缺一条就是把结果当成比它更可信的东西报出去了。
  - `advisory` → 建议。
- 该列表是**按需下发**的：本会话已下发过的项不再重复（只留在 `disclosure_pending` 里），但你仍须覆盖它们。
- `stage=verified` 只表示"该说的都已下发过"，**不等于**你已经照做 —— 服务器看不到你的最终措辞。
- 缺参数**不阻断**结论（只要求披露）：默认工况下缺省参数有几十项，若据此阻断则 `conclusion_allowed` 恒为 false 就没有意义了；保留默认值时按 `values.condition_defaults` 逐项标注即可。

### 1. 启动协议

任何 TDLAS 任务开始前，**必须先调用 `tdlas_guide`** 获取交互协议，包括：
- 参数索取优先级与澄清流程
- DAS vs WMS 选型决策树
- 出图布局规范（`image_layout`）
- 物理术语表

### 2. 参数澄清（硬约束）

当返回 `clarify.needed=True` 时：
- **必须先向用户提出 `clarify.questions` 中的问题**，获得回答前不得出图或下结论
- 用户明确表示"用默认值"时方可跳过
- 提问使用结构化选择框，每题 2–4 个固定选项 + 自定义输入
- 超过 4 题按优先级分批：波数/分子 → 浓度/光程 → 温度/压力 → 硬件 → 噪声

**参数来源优先级**：① 用户实测值 → ② 器件型号（联网检索规格书）→ ③ 现场标定 → ④ 默认值并显式标注

**器件型号处理**：用户给出具体型号（如 DAQ/DFB/PD 具体型号）时，必须联网检索官方 datasheet 提取参数，填入仿真并在 `assumptions` 中标注来源。查不到则退回默认值，不得编造。

**设备库引用**：用户已通过 `tdlas_device` 保存硬件参数时，仿真工具直接传引用名，不再逐项索要。

### 3. 结果校验（硬约束）

每次出图或给出定量结论前，必须通过 `tdlas_review` 或返回值中的 `validation` 字段校验。`overall=fail` 时不得下结论，须先修复失败项。

### 4. 出图规范

- 固定 3×2 六子图布局（详见 `tdlas_guide` 的 `image_layout`）
- 必须标注：技术名称、归一化方法、基线处理方式、线型含义
- 1f 与 2f 子图分离显示（幅值量级差异大）

### 5. α 峰值播报（自适应硬约束）

α（吸收系数 / αL）峰值随 **T、P、网格步长 step、翼截断 wingHW、波数窗口** 变化，裸报数值不可复现、不可比较。

- **何时报**：看返回的 `alpha_report.needs_report`。
  - `true` → **必须**播报（本会话首次给出 α 峰值，或 α 语境相对上次已变化）；
  - `false` → 语境未变且已播报过，可省略以保持简洁。
- **报什么**：α 峰值数值 **＋** `T(K)` **＋** `P(atm)` **＋** `step(cm⁻¹)` **＋** `wingHW(cm⁻¹)` **＋** 窗口(cm⁻¹)——缺一不可，具体值见 `alpha_report.alpha_peak_context`。
- **禁止**：为求简洁省略语境后再对 α 下定量结论；语境不同的两次 α 不得直接比较。

### 6. etalon 条纹（默认关，按需开启）

只有 `fringe=True` 才建模；**默认 False = 理想仿真，绝不静默注入系统性误差**。

- 用户提到窗片 / 光纤 / 滤光片 / 未镀膜或未楔化窗片时，应**主动询问**是否建模；开启后若未给几何参数，`param_requests` 会列出 `fringe_n` / `fringe_d_cm` / `fringe_R`。
- 看 `validation` 第 10 项与 `fringe_report`：`needs_report=True` 时须说明 ① FSR 与吸收线宽的关系；② 确定性条纹可被背景扣除、只有漂移残留；③ 压不掉时该做的物理措施（窗片楔化 / AR 镀膜 / 扫频平均）。
- **不得**把条纹当成可被降噪 / 多次平均消掉的随机噪声——固定腔长的条纹是**确定性项**。

## 关键物理约束（已知坑点）

| 约束 | 说明 |
|------|------|
| α 数值脱离语境 | α 峰值依赖 T/P/step/wingHW/窗口，报数须带上这五项（见 `alpha_report`），否则不可复现 |
| etalon 条纹被当噪声 | 条纹是确定性项：加噪声/多次平均压不掉，只有楔化/AR 镀膜/扫频能消除；`step > FSR/10` 还会混叠 |
| 横轴为扫描波数 | 非瞬时波数（瞬时波数含 ±a 调制摆动） |
| DAS 无调制 | 不能用含调制信号的平均结果伪造 DAS |
| 1f 过零为物理奇点 | 由 AM 与吸收 1f 相消导致，非"1f 失效" |
| 2f/1f 失效判据 | 非吸收区泄漏 > 峰值 30%（1f 基线漂移），非过零点 |
| 密集谱区多线叠加 | 如 CH4 2968.5 cm⁻¹ 窗口内含 221 条线，2f 为多峰叠加；需标准双峰应选择孤立单线 |
| 点值当带误差结果 | 未实现项（自展宽、线混合、PD 暗电流、前放 1/f…）可能主导真实误差：痕量 LOD 因缺"前放 1/f"而**偏乐观**。给定量结论时须按 `tdlas_guide` 的 `fidelity` 台账披露"哪些已建模 / 哪些未实现" |
| run-to-run 散布当总不确定度 | `tdlas_invert(n_repeats=…)` 的 `uncertainty` **只含统计（随机）分量**，不含 k 标定误差、数据库不确定度、线型近似与 etalon 条纹；它答的是"同样仿真跑两遍有多一致"。且 σ **必须在要报告的那个浓度上求**（噪声不随吸收信号等比缩放，跨浓度外推可差一个量级；返回里看 `x_eval`） |
| 明知有暗区仍下定量结论 | 传 `allow_partial_dark=True` 只为诊断"扫描越界后长什么样"：暗区锁相波形被截断 → 2f/1f 失真，定量结论不可用 |

## 部署方式

- **stdio（默认）**：本地 MCP 客户端直连
- **HTTP 远程**：`--http --host 0.0.0.0 --port 8000 --token <密钥>`，端点 `/mcp`
- **公网隧道**：`python tools/remote_link.py`（cloudflared，仅监听 127.0.0.1 + 强制 Bearer token）

## 使用声明

本工具输出为理论仿真结果，**仅用于研究参考、方法验证与系统选型**。

**严禁以下行为**：
- 将仿真数据直接冒充实验测量数据写入学术论文
- 用仿真曲线替代真实实验数据作为结论依据
- 在论文中隐瞒仿真结果的理论性质，误导审稿人与读者

仿真结果必须明确标注为"理论仿真"或"数值计算"，与实验数据严格区分。

## 项目地址

- 仓库：https://github.com/LKF0402/tdlas-mcp
- HAPI：https://github.com/hitranonline/hapi
