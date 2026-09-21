---
name: tdlas-mcp
description: TDLAS/WMS 光谱仿真 MCP 服务器。触发词：TDLAS、WMS、波长调制、直接吸收、DAS、二次谐波、2f/1f、数字锁相、谐波检测、气体浓度反演、检测限、LOD、调制系数 m。
---

# tdlas-mcp：TDLAS/WMS 仪器级仿真 MCP

## 工具路由（必查表）

| 任务 | 工具 |
|---|---|
| WMS 六子图 | `tdlas_wms_instrument` |
| DAS 五子图 | `tdlas_das_instrument` |
| DAS 链路处理 | `tdlas_das_chain` |
| 快速算峰高（解析模型，不出图） | `tdlas_simulate` |
| 校验 | `tdlas_review` |
| 浓度反演 | `tdlas_invert` |
| 检测限 | `tdlas_detection_limit` / `tdlas_detection_limit_scan` |
| 设备/整机库 | `tdlas_device` |
| 工况记忆 | `tdlas_session` |
| 协议查询 | `tdlas_guide` |
| 自检 | `tdlas_selftest` |
| Allan 方差 | `tdlas_allan` |
| 校准曲线 | `tdlas_calibration_curve` |

## A. 交互契约（硬约束）

**每条返回必查 `next_required_actions`**，按 severity 执行：
- `block_conclusion` → 不得下定量结论
- `must_disclose` → 必须在回答中说明
- `advisory` → 转述给用户

**每条返回必查 `validation.overall`**：
- `fail` → 不得下结论，先修
- `warn` → 须说明

## B. 参数澄清（硬约束）

优先级：① 用户实测 → ② 器件型号（联网查 datasheet）→ ③ 标定 → ④ 默认值+标注。

`clarify.needed=true` → **必须先问，得到回答前不得出图**。
用户说"用默认值"可跳过。

## C. 出图规范（硬约束）

**用户说"画图"必须传 `save_png=true`**，否则用户拿不到图。

**图型选择**：

| 用户意图 | 工具参数 |
|---|---|
| 默认 WMS 图 | `tdlas_wms_instrument` + `save_png=true` |
| 多浓度对比 | `x_list=[...]` + `save_png=true` |
| 混合气 | `mixture="CH4:0.01,CO2:0.04"` |
| 系统选型/检测限 | `tdlas_detection_limit_scan` |
| 只看某面板 | `panels=[...]` 子集 |

**六子图顺序**（WMS 专用）：
① V(t) 驱动电压 → ② DAS 原始 PD → ③ DAS 吸光度 vs 理论 → ④ 1f → ⑤ 2f → ⑥ 2f/1f 归一化

**出图硬约束**：
1. 面板序号完整（①~⑥）
2. DAS 单浓度画原始 PD（斜的 L-I 曲线，不扣基线）；多浓度画归一化 T/T0
3. 纵轴只按**有效区**数据定（剔除区不参与）
4. 剔除区只对 WMS 面板有效，DAS 不画剔除区
5. 横轴用**扫描波数**（非瞬时波数）
6. 1f 与 2f 不同格

## F. 设备库与整机打包

### 设备库 action

| action | 功能 |
|---|---|
| `list` | 简洁摘要（设备名 + 关键参数） |
| `save` | 存设备（`device_type`: laser/pd/daq/optics + `name` + 参数） |
| `view` | 看单个设备 |
| `delete` | 删设备（自动清理 setup 引用） |
| `set_default` | 设某类设备默认 |
| `save_setup` | 打包整机（硬件 + 工况） |
| `set_default_setup` | 设默认整机（不传 setup= 自动用） |
| `view_setup` | 看整机详情（设备 + 工况） |

### save_setup 工况参数

支持打包：`T, P, L_cm, wn_center, mod_freq_Hz, freq_Hz, drift_frac, flicker_frac, scan_span_cm, am_i0`

**显式传参优先**（setup 存的是默认，本次调用覆盖它）。

### 来源标记（必查）

| 来源 | `source` |
|---|---|
| 规格书/datasheet | `datasheet` |
| 用户口述/实测 | `user` |
| AI 推断 | `ai` |
| 现场标定 | `calibration` |
| 未知 | `unspecified` |

**禁止把推断值标成 datasheet。**

### 设备名规范

✅ `nanoplus_3373nm_SN5712`, `NI_USB-6211`, `多通池_10m`
❌ `a`, `test`, `dev1`, `临时`

## G. 物理坑点（必查）

1. **横轴用扫描波数**（非瞬时波数）
2. **2f/1f 失效判据**：非吸收区泄漏 > 峰值 30%（1f 过零不是失效）
3. **密集谱区**（如 CH4 2968.5 有 221 条线）→ 2f 是多峰叠加，标准双峰选孤立单线
4. **弱吸收先算 αL**：先看 `alpha_L_peak` 再选浓度/光程
5. **默认无噪声是误解**：散粒+热噪声恒在，RIN/1f 才可关
6. **seed 默认 0** = 可复现，比较结果务必同 seed
7. **带宽钳位**：`min(bw, 0.45·fs)`
8. **etalon 是确定性项**：降噪压不掉

## H. 引用与可复现

要写进论文的数值，一并给出 `provenance.engine_version` 和 `line_table.sha256_16`。

## K. 使用声明

输出为理论仿真，严禁冒充实验数据。
