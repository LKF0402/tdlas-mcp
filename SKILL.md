---
name: tdlas-mcp
description: TDLAS/WMS 光谱仿真 MCP 服务器。触发词：TDLAS、WMS、波长调制、直接吸收、DAS、二次谐波、2f/1f、数字锁相、谐波检测、气体浓度反演、检测限、LOD、调制系数 m。用户要求模拟/绘制吸收光谱或 WMS 信号时必须使用本 Skill。
---

# tdlas-mcp：TDLAS/WMS 仪器级仿真 MCP 服务器

自然语言驱动的 TDLAS 实验仿真。链路逐级建模：DAQ 驱动电压 → 激光调谐 → HITRAN 吸收 → 光电探测 → ADC 量化 → 数字锁相 → 谐波与归一化。

**读法**：开头是跳读索引，正文按 `§` 分节，每节首行即结论。按当前任务跳读，不必顺序通读。

| 你现在要做的 | 读哪节 |
|---|---|
| 任何仿真任务（必读） | **A** 交互契约、**B** 参数澄清、**C** 结果校验 |
| 器件型号 / 建仪器库 / 出现 `offer_device_packing` | **F** 仪器库与打包 |
| 选窗口、选浓度光程、结果不合预期、见到 fail | **G** 物理坑点 |
| 写进论文 / 要可复现 | **H** 引用 |
| 出图、α 播报、etalon 细则 | **D** 出图、**I** 自适应播报 |
| 部署 / 声明 | **J**、**K** |

触发词：TDLAS、WMS、DAS、波长调制、直接吸收、二次/一次谐波、2f/1f、数字锁相、正交解调、浓度反演、检测限、LOD、NEC、系统选型、调制深度/系数 m。
前置：`pip install -r requirements.txt`（numpy/matplotlib/scipy/hitran-api），首次运行自动下载线表缓存（HAPI 1.x，免 key）。

**工具路由**：画 WMS 图 `tdlas_wms_instrument` ｜ 画 DAS 图 `tdlas_das_instrument` ｜ DAS 信号处理 `tdlas_das_chain` ｜ 快速算峰高 `tdlas_simulate`（解析模型，不出链路图）｜ 校验 `tdlas_review` ｜ 反演 `tdlas_invert` ｜ 检测限 `tdlas_detection_limit(_scan)` ｜ 硬件库 `tdlas_device` ｜ 工况记忆 `tdlas_session` ｜ 协议查询 `tdlas_guide` ｜ 自检 `tdlas_selftest`

---

## A. 交互契约（最高优先级：先读返回值，再谈表达）

每次仿真返回都带两个**机器可读**字段：

```json
"interaction": {"stage": "clarify|produced|verified", "conclusion_allowed": true,
                "blocking_reason": null, "disclosure_pending": [...]},
"next_required_actions": [{"id": "...", "severity": "block_conclusion|must_disclose|advisory",
                           "action": "……", "evidence_fields": ["..."],
                           "values": {...}, "template": "……"}]
```

**按 `next_required_actions` 逐条照做；`values` 里就是要用的数字，别自己另找。**

| severity | 你必须做的 |
|---|---|
| `block_conclusion` | **不得给出定量结论**（这个数本身不可用）。看 `blocking_reason` |
| `must_disclose` | 结论可以给，但**必须在同一回答里说明**；少一条就是把结果报得比它实际更可信 |
| `advisory` | 建议 —— 仍须向用户转述 |

- 列表**按需下发**：本会话已发过的不再重复（只留在 `disclosure_pending`），但你仍须覆盖。
- `stage=verified` 只表示"该说的都已下发过"，**不等于**你已照做。
- **缺参数不阻断结论**（只要求披露）——默认工况缺省参数有几十项，据此阻断则 `conclusion_allowed` 恒为 false 就失去意义；按 `values.condition_defaults` 逐项标注即可。
- **报错也是契约**：常带 `need_devices` / `suggested_names` / 可行方案，按它去问用户，**不要反复重试同一参数**。

## B. 参数获取与澄清（硬约束）

优先级：① 用户实测值 → ② 器件型号（联网检索规格书）→ ③ 现场标定 → ④ 默认值并显式标注。**用户给了型号就必须检索官方规格书；查不到退回标定或默认值，不得编造。**

`clarify.needed=True` 时**必须先提出 `clarify.questions` 里的问题**，得到回答前不得出图或下结论（用户明确说"用默认值"才可跳过）。用结构化选择框（每题 2–4 选项 + 自定义）；超过 4 题按优先级分批：波数/分子 → 浓度/光程 → T/P → 硬件 → 噪声。

## C. 结果校验（硬约束）

出图或给定量结论前必须校验（`tdlas_review` 或返回里的 `validation`）。**`overall=fail` 不得下结论**，先修失败项；`warn` 项须在结论中说明。

## D. 出图规范（图是用户唯一直接看到的产物）

**先做出图这个动作**：用户说"画图 / 给张图 / 画 WMS / 画 DAS"时**必须传 `save_png=true`**——不传就只有数值、用户拿不到图。出图后把返回的 `png` / `png_overlay` 路径告诉用户。

**★ 先按用户需求选图型，不要一律给六子图**：

| 用户意图 | 出什么图 / 传什么 |
|---|---|
| 默认、"画 WMS 图"、"给我 CH4 的图" | 标准 6 子图，单浓度 |
| "多浓度对比 / 标定曲线" | `x_list=[...]` + `save_png=true` → **主图 ②~⑤ 面板即按 ppm 叠加**（纵轴按全部浓度统一）+ 附加 `png_overlay` 单用途对照 |
| "混合气 / 含 X% A 和 Y% B" | `mixture="CH4:0.01,CO2:0.04"` → αL 面板自动叠加各组分，**不新增图** |
| "只要相位匹配的 X 分量 / 非正交 / 单通道锁相" | `return_xy=true` → 从 `wms_raw_xy` 取 `X1f_c`/`X2f_c` 作图；**必须说明差异**：正交解调取 √(X²+Y²)（默认），单通道只取 X，对相位失配更敏感 |
| "系统选型 / 光程要多少 / 检测限" | `tdlas_detection_limit_scan`（1×2：LOD vs 光程、LOD vs 参考浓度） |
| "只看 2f" / "只要吸光度" | `panels` 子集（`f2` / `al`）；非 6 面板时引擎改**竖排堆叠** |

无法判断时先给标准六图，**并主动说明**"如需多浓度叠加 / 混合气分组 / 只看某分量请告知"——不要沉默地只给一种。**禁止**：用户要混合气却只画单组分；要标定曲线却不出 `png_overlay`；要 X 分量却给正交幅值而不说明。

**四类图按工具区分，勿混用**（完整面板顺序与轴标签见 `tdlas_guide` 的 `image_layout`）：

| 工具 | 布局 | 关键点 |
|---|---|---|
| `tdlas_wms_instrument` | 3×2 六面板，≈14×10 | 面板序：波长调制 V(t) → DAS 原始 → **DAS 吸光度 vs 理论（与上一格同行）** → 1f → 2f → 归一化 2f |
| `tdlas_das_instrument` | 5×1 竖排，≈9.5×12 | 轴标签为**英文**，按图实际内容描述，别臆造中文标题 |
| `tdlas_das_chain` | 4×1 竖排，≈9×10 | 横轴以时间(ms)为主 |
| `tdlas_detection_limit_scan` | 1×2，≈14×6 | 左：LOD vs 光程；右：LOD vs 参考浓度 |
| 多浓度 `x_list` + `save_png` | 主图 ②~⑤ 面板按 ppm 叠加 + 附加图 `png_overlay` | 主图即叠加；`png_overlay` 为单用途对照，额外输出 |

**每条图都必须**：标注技术名、归一化方法、基线处理方式、线型含义；**1f 与 2f 不得同格**；剔除区（trim_frac 默认 12%）标红带，纵轴范围按有效区确定。

**线型**：实线=有效数据；虚线=理论参考；点线/灰点=剔除区。每种线型须在图例注明。

**横轴**：驱动电压用时间；其余一律用**扫描波数**（不是含 ±a 摆动的瞬时波数）。

**图外必须同步给出**：① 技术选型（WMS/DAS 及为何）；② 归一化方法与是否背景扣除；③ 基线处理方式；④ `validation.overall` 及 fail/warn 项 —— **图好看不代表数可用**。

**禁止**：不传 `save_png` 却说"已出图"；凭印象描述图上没有的内容；用理想仿真图暗示实测结果。

**描述契约**：用户问"图里有什么"时按面板顺序逐个说明：面板标题 → 横轴量 → 纵轴量 → 该格结论。

## E. 结论最小清单

按序覆盖：① 工况（物种/波段/T/P/x/L/fm/m）；② `normalization.method`；③ 峰值与位置；④ 噪声分解；⑤ `validation` 结论；⑥ 用默认值的参数逐项标注；⑦ 若 `alpha_report`/`fringe_report` 的 `needs_report=true`，按 §I 播报。

---

## F. 仪器库与整机打包（AI 负责，用户只提供素材）

**用户只上传素材（型号/规格书/光路描述），建库、命名、打包、披露全部由你完成。**

**流程**：问有哪几台设备与型号（没型号 → 请其拍规格书或口述关键参数）→ 逐台 `tdlas_device save`（`source` 如实填、名字显著）→ `save_setup` 打包 → `set_default` 设默认 → 用 `setup=<名字>` 跑一次验证。**打包前自问：用户给的硬件信息都进库了吗？** 没进库的参数不会被仿真使用。

**`offer_device_packing` 出现时**（`severity=advisory`，不阻断结论）**必须向用户转述**，并按其中的 `missing_devices` 逐类索取型号。它携带 `{"reason", "missing_devices": [{"cn","fields":[{"cn","unit"}]}], "how"}`。**该提示同一会话只下发一次**，看到就处理。服务器何时提示：设备库空 → 不提示（此时追问是骚扰）；有设备无 setup → 提示打包；有 setup 无默认 → 提示设默认；都配好 → 静默。

**★ 来源必须如实填（最不能妥协的一条）**

| 实际情况 | `source` |
|---|---|
| 用户给的规格书 / 你检索到的官方 datasheet | `datasheet`（`source_note` 写页码） |
| 用户口述或实测值 | `user` |
| **你推断/估计的值（含"典型值"）** | `ai` |
| 现场标定 | `calibration` |
| 说不清 | `unspecified`（**不要**默认成 datasheet） |

**禁止**把推断值标成 `datasheet`：工具会在 `provenance.device_source` 回显来源、对 `unspecified` 给警告 —— 那是用户的**追溯依据**，糊弄它等于把"编造"包装成"查到"。`ai` 推断值的 note 写「由典型值推断，待确认」。

**参数定位表**（拿到规格书直接找这几栏）：`dnu_dI` ← Tuning coefficient（DFB 约 −0.01~−0.1 cm⁻¹/mA，**负号=电流↑波数↓**）｜ `eta_VI` ← 驱动器 V→I 跨导（10–50 mA/V；无规格书时用电流量程÷电压量程反推）｜ `wn_ref`/`i_ref` ← Center wavenumber / Operating current（配对）｜ `i_th` ← Threshold current（DFB 10–40 mA）｜ `eta_IP` ← Slope efficiency（0.05–0.3 mW/mA）｜ `am_i0`/`am_psi1` ← Residual amplitude modulation（0.01–0.05，相位常 π/2）｜ PD `resp` ← Responsivity（InGaAs 0.8–1.0 A/W）｜ PD `gain` ← Transimpedance（10³–10⁶ V/A）｜ PD `bw` ← Bandwidth 3 dB ｜ DAQ `fs`/`adc_bits`/`v_range` ← Sample rate / Resolution / Input range（250 kS/s、16 bit、±10 V 常见）｜ `throughput` ← 窗片+镜片+光纤耦合透过率乘积（0.3–0.95）。
换算：`ν[cm⁻¹] = 1e7 / λ[nm]`；注意规格书常用 mA/A、µW/mW。

**命名必须显著**（设备名是以后引用时唯一看得到的东西）：✅ `nanoplus_3373nm_SN5712`、`NI_USB-6211`、`thorlabs_PDA10CS2`、`多通池_10m`；❌ `a`/`a1`/`dev1`/`test`/`临时`/`新建`。工具会拒绝不显著名并回 `suggested_names`。打包整机**可不传 name**（自动拼显著名，返回 `name_auto_generated=true`）；重名会被拒绝以免覆盖。

**验证真实性**：跑一次 `setup=<名字>`，确认返回的 `laser`/`pd`/`adc` 段是你填的值（不是内置默认 `eta_VI=24.0 / i_ref=120 / gain=700`）、`provenance.device_source` 与素材一致；若有 `unspecified` 警告，告知用户建议补来源。

## G. 物理坑点（按需查）

**选窗口/浓度**
- **弱吸收带选错窗口**：同分子不同波段线强可差 2–4 个数量级（实测 CH4：2698 cm⁻¹ 弱组合带 sw 6.7e-22 vs 2968.5 cm⁻¹ ν3 基频带 7.1e-20，**差约 100 倍**）。选窗口前先看 `n_lines_in_window` 与 `alpha_L_peak`，**先算 αL 再选浓度/光程**。
- **信号埋在 ADC 量化地板**：校验项「信号/量化」`2f < 3 LSB` 直接 fail。实测 CH4@2698/L=100cm/10–30 ppm 时 2f 仅 0.11–0.26 mV（1 LSB=0.305 mV）→ k 在三档浓度差 **23 倍**的假非线性。**提增益救不了**（放大的是直流基底≈3 V，先撞量程饱和）：正确手段是**加光程或换强线**。
- **真实器件可达范围**：由 `wn_ref/i_ref/i_th/dν/dI + 0–5 V` 共同决定，通常仅几 cm⁻¹。实测两支 DFB 恰好错开（nanoplus 2963.5–2966.5、内置默认 2966.5–2971.0）。选范围外的线会硬报错并给出所需 `wn_ref` —— 改选可达范围内的线，别重试。
- 密集谱区多线叠加（如 CH4 2968.5 含 221 条线）→ 2f 是多峰叠加，要标准双峰请选孤立单线。

**归一化与解调**
- 2f/1f 失效判据**只有一条**：非吸收区泄漏 > 峰值 30%。**1f 过零是物理奇点**（AM 与吸收 1f 相消），不是失效。
- 横轴用**扫描波数**（不含调制），用瞬时波数会把曲线折乱。
- DAS 无调制：不能用含调制信号的平均结果伪造（会折出锯齿）。
- 模型里 L-I 是**严格线性**，不存在"L-I 二阶非线性 RAM"；`background_subtract` 在 `am_*=0` 时近乎无操作。
- 反演配对量：`peak` 必须配 `sensitivity_k`（同源）；**用 `S2f1f_peak` 配 k 会错**（退化为 2f/I0 时不同源，实测偏 249 倍且不报错）。

**噪声与不确定度**
- "默认无噪声"是误解：散粒+热**恒在**（约 0.03 mV），RIN/1f 才可关；严格理想仿真用 `physical_noise=false`。`seed` 默认 0 = 可复现，比较两次结果务必同 seed。
- `uncertainty` **只含统计（随机）分量**，不含 k 标定误差、数据库不确定度、线型近似、etalon；且 σ **必须在要报告的浓度上求**（噪声不随吸收信号等比缩放，跨浓度外推可差一个量级，看 `x_eval`）。`n_repeats<10` 时看 `rel_sigma_of_sigma_pct`。
- 未实现项（自展宽、线混合、PD 暗电流、**前放 1/f**）可能主导真实误差 —— 痕量 LOD 因缺"前放 1/f"而**偏乐观**；给结论时按 `tdlas_guide` 的 `fidelity` 台账披露。

**系统误差**
- etalon 条纹是**确定性项**：降噪/多次平均压不掉，只有楔化/AR 镀膜/扫频能消除；`step > FSR/10` 会混叠（校验判 fail）。默认关；用户提到窗片/光纤/未镀膜窗片时应主动问是否建模。
- 带宽被钳位：`min(bw, 0.45·fs)` —— 默认 bw=1 MHz/fs=240 kHz 时实际 fc≈108 kHz，2f 被额外削约 13%。
- 传 `allow_partial_dark=true` 只为诊断"扫描越界后长什么样"：暗区锁相被截断 → 2f/1f 失真，定量结论不可用。

## H. 引用与可复现

返回里的 `provenance` 含 `engine_version`、`line_table.{table, sha256_16, n_lines}`、`input_hash`、`device_source`。**要写进论文/报告的数值，一并给出 `engine_version` 与 `line_table.sha256_16`**：HITRAN 会更新线表、同名表内容会变，没有指纹别人无法重建同一个数。

反演优先用**谱线级最小二乘**：`tdlas_invert(spectrum=…, ref_spectrum=…)`，传 `tdlas_wms_instrument` 返回的 `results.S2f_norm_spectrum` 与 `results.k1_spectrum_per_unit_x`。它给出 `x_sigma`/`χ²_red`/`R²`，**`χ²_red ≫ 1` 说明前提已破**（线形/基线/工况不同源），先查前提再采信 σ。实测独立双测量：最小二乘 +0.012% vs 单点 +0.036%。

## I. 自适应播报细则

- **α 峰值**：`alpha_report.needs_report=true` 时须同时给出 α 峰值 ＋ `T(K)` ＋ `P(atm)` ＋ `step` ＋ `wingHW` ＋ 窗口（值在 `alpha_peak_context`）——**缺一不可**；语境不同的两次 α 不可直接比较。
- **etalon**：`fringe_report.needs_report=true` 时说明 ① FSR 与线宽关系；② 确定性条纹可被背景扣除、只留漂移残留；③ 压不掉时该做的物理措施。
- **SOP**：`tdlas_guide` 的 `sop` 给出 7 步标准流程。

## J. 部署

stdio（默认，本地直连）｜ HTTP 远程 `--http --host 0.0.0.0 --port 8000 --token <密钥>`，端点 `/mcp` ｜ 公网隧道 `python tools/remote_link.py`（cloudflared，仅监听 127.0.0.1 + 强制 Bearer token）。

## K. 使用声明

输出为**理论仿真**，仅用于研究参考、方法验证与系统选型。严禁：冒充实验测量数据写入论文；用仿真曲线替代真实实验数据作结论依据；隐瞒其理论性质误导审稿人与读者。必须明确标注"理论仿真"或"数值计算"。

## 项目地址

仓库：https://github.com/LKF0402/tdlas-mcp ｜ HAPI：https://github.com/hitranonline/hapi
