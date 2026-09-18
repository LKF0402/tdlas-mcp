# Changelog

本文件精炼记录 tdlas-mcp 的功能演进（按领域归纳，非逐提交罗列）。

## [Unreleased]

### 第五轮：AI 交互契约层（结构化动作 / 按需下发 / 进 CI）
> 依据 `tdlas-mcp-AI交互层设计.md` 落地**第一步+第二步**；§3.1 的状态机与 §8 的两点做了改造（见下"对设计文档的修正"）。

- **交互契约层**：新增纯函数 `build_next_actions(out)` + `_interaction_block()`，把原本写在散文里的"必须澄清/必须披露"变成**机器可读动作**（wms / das / review / invert 均已下发）：
  - 每个仿真类工具返回新增 `interaction`（`stage` / `physics_ok` / `result_usable` / `conclusion_allowed` / `blocking_reason` / `disclosure_pending`）与 `next_required_actions`（`id` / `severity` / `action` / `evidence_fields` / `values` / `template`）
  - `evidence_fields` 让 AI **按 id 执行、无需自己找数据**；`values` 直接给数字（如 `condition_defaults` 逐项给出 T/P/x/L 的实际默认值）
  - **按需下发**：本会话已下发且语境未变的动作不再重复（只留 id 在 `disclosure_pending`），避免每次调用都重发一整张清单
- **消除交互不对称**：`tdlas_das_instrument` 补齐 `clarify`（与 wms 同源同形，此前最需要先问清工况的一条链反而没有问句）；`tdlas_review` 补齐 `assumptions` / `param_requests` / `clarify` / `fidelity`（此前连 `assumptions` 都不返回）
- **保真度随结果披露**：仿真结果新增精简 `fidelity` 块（`not_implemented` 等，真源仍是 `tools/tdlas_fidelity.py`，不复制内容），对应动作 `disclose_fidelity_gaps`
- **工况回显**：`wms` / `das` 新增 `conditions`（T_K / P_atm / x / L_cm）—— 此前 `assumptions` 只说"哪些用了默认"却不给数值，AI 要披露也报不出数
- **契约自检 89 → 103 项**：新增交互契约一节（四工具均下发契约 / DAS-clarify 对称 / review 补齐、动作只指向真实存在的字段、校验 fail 与暗区必须阻断、同语境第二轮不重复下发、`tdlas_invert` 真实返回校验、描述引用的 `results.xxx` 必须真实存在）

**对设计文档的修正**（按实测标定，而非照抄）：
1. **`conclusion_allowed` 不把"缺参数未确认"算作阻断**。文档 §3.2 把 `disclose_defaults` / `clarify_missing` 标为 `blocking: true`；实测按此实现后，**即便显式给全 T/P/x/L，`conclusion_allowed` 仍恒为 false** —— 因为 `assumptions` 是"没显式传的一切"（默认工况 35 项、含全部器件参数）、`clarify.questions` 是 36 问的题库。该布尔随即失去信息量、客户端会学会忽略它。改为：阻断只保留"这个数本身不可用"（校验 fail / 扫描含暗区 / 反演越界），缺参数走 `must_disclose`，与项目既有口径（`confirm_note`："保留默认时须在结论中明确注明"）一致
2. **§8-Q2（把关键定量字段置 `null`）不采纳**。理由：① `conclusion_allowed=false` 的最常见触发条件是"有默认参数未确认"——这是每个新用户的**默认状态**，据此 null 掉结果等于工具在首次调用就不可用；② 用户付了算力却拿到 null，无法自行复核或做探索性使用；③ 与项目"越界不静默截断"的既有范式冲突（应显式标注 `mole_frac_status`/`*_valid` 而非删数据）。改为：数据保留，结论受控
3. **§4-A4 的能力被高估**：`results.xxx` 存在性检查只能拦"引用了**不存在**的量"（拼写/漂移），**拦不住"引用错了量"** —— `S2f1f_peak` 那起事故中该字段确实还在返回里，只是不该与 `k` 配对。这类语义错误只能靠配对说明 + 断言钉住（已由 `S2f_norm_peak_note` 与 `_pairing_ok` 覆盖）。A4 已按此标定落地并注明边界
4. **§3.1 的 `verified` 状态需要回执通道才能成立**，而服务器看不到模型的最终措辞。采用更小的方案：`stage` 由当前状态**派生**（不新增持久状态机），`verified` 定义为"本会话该说的都已下发过"，并在文档与返回值里写明它**不等于**模型已照做；不引入自报回执（为不可验证的目标增加一次往返不划算）
5. 文档说"契约从未被校验（75 项）"已过期：现在 103 项，且**参数可达性审计已经覆盖了事故①③**（正是为"声明了没接住"造的）

### 第六轮：多浓度扫描 x_list（自定义更灵活 + 防呆）
> 用户需求："多浓度做成 x_list 参数，允许自定义则更灵活但要防呆"。

- **四工具支持 `x_list`**：`tdlas_simulate` / `tdlas_das_chain` / `tdlas_das_instrument` / `tdlas_wms_instrument`（及透传的 `tdlas_review`）新增 `x_list`（摩尔分数列表，如 `[1e-4,1e-3,1e-2]`）。与单 `x` **二选一**；开启后返回 `multi_conc` 扫描块（按浓度对齐的响应峰高数组 + `mole_ppm`），可直接画标定曲线 / 做多工况对照。
- **防呆（不让错误输入静默跑出误导结果）**：
  - 空列表 / 非有限数 / 超出摩尔分数定义域 `(0,1]` → **直接报错，不静默截断**（截断会掩盖前提已破）
  - 长度超过 64 点 → 报错（防一次扫上千点把会话卡死）
  - 自动**去重（1e-12 容差）并升序**，让扫描曲线单调、好画
  - 接受 `list[number]` 或逗号/空格分隔字符串（如 `"1e-4,1e-3"`），方便命令行与 MCP 文本参数直传
- **线性 / 饱和防呆诊断**：`multi_conc.linearity` 以最小浓度为线性基准，算"响应/浓度"比值；偏离 >10% 即标出 `nonlinear_from_ppm`，提示"该点之后不宜做线性标定"（实测 CH4@2968.5 的 2f/1f 在 1000 ppm 已进非线性区，WMS 在 1e-2 时 2f/1f 反而下降——正是饱和失真的典型）
- **结构保持向后兼容**：多浓度模式仍用首个浓度跑完整详细报告（T/P/L/α/噪声等全保留），`x_list` 只额外叠加扫描汇总；单浓度路径行为不变。
- **契约自检 103 → 113 项**：新增第 8 节（x_list 透传为 x、越界/二选一/空列表三道防呆、multi_conc 结构、线性诊断生效）；`tdlas_fidelity.NON_ENGINE_CHANNELS` 增列 `x_list`（逻辑参数，MCP 层循环驱动、不进引擎）。

### 第四轮：第三轮改动的事后审计修复（判据回归 / 不确定度口径 / 缓存并发）
> 第三轮提交当日做的定向自查，逐条实测复现后修复。凡"改了行为"的都在这条里写清原委。

- **【高】显式 `offset_V` 的越限校验被绕过（第三轮引入的真回归）**：`_require_driver_range()` 改用 `laser_reach(wn_center)` 后，**不再校验实际生效的 `cfg["offset_V"]`**。而 `_resolve_scan()` 只在"用户没显式给 offset_V"时才由 wn_center 反算偏置；用户一旦显式给 `offset_V`，扫描中心就由它决定、**wn_center 只是标签**。实测两个方向都错：① 显式 `offset_V=9 V`（越驱动器上限）+ `wn_center=2968.5` → **静默放行**，引擎照常出谱（ν 轴跑到 2969.5–2972.5 之外、只剩"ADC 饱和"之类的下游症状）；② 显式 `offset_V=2 V`（合法）+ `wn_center=3000` → **误拒**，且报错文本自相矛盾（"扫描区间 [1.29, 2.71] V 未整段高于阈值电压 1.25 V"——该区间明明高于 1.25 V，因为区间用的是实际偏置、而 reason 来自另一个反算偏置）。修法：抽出**唯一判据** `_scan_window_reason(off, amp, v_lo_eff, v_hi)` 供 `laser_reach()` 与 `_require_driver_range()` 共用，且引擎侧一律按**实际** `offset_V ± amp_V` 判定；仅在"偏置确实由 wn_center 反算"时才附 `wn_ref` 重锚建议（显式给 offset_V 时给重锚建议是误导）；`_partial_dark_note()` 同样改为直接读实际偏置（不再经 wn_center 反算，并去掉无用的 `wn_center` 形参）
- **【中】测量不确定度在错误的浓度上评估**：`tdlas_invert(n_repeats>0)` 原先把 σ 算在**参考浓度 `x_ref`** 上、再套到**反演浓度 `x_est`** 上，依据是 docstring 里"rel_sigma 与浓度无关、任意 x 都适用"的说法——**实测该说法不成立**：噪声中有一部分（热噪声、ADC 量化、RIN）不随吸收信号等比缩放，rel_sigma 随 x 明显变化（实测 CH4@2968.5、L=50 cm、n=5：x=1e-3 → 1.08%，1e-5 → 0.21%，1e-7 → 4.04%，跨 4 个量级差约 20 倍）。实测调用里 `x_ref=1e-4` 而 `x_est=6.5e-11`（差 6 个量级）时，报出的 σ 已不可用。修法：改为在 `x_est`（`mole_frac_status == "ok"` 时）处求 σ，并新增 `x_eval` / `x_eval_source` 明示求值点；`x_est` 越界时退回 x_ref 并标注"本次结果本身不可用"
- **【中】`mean_rel_ci` 数值无意义**：原为 `[max(0, rel−half), rel+half]`，把"散布本身"当成了区间中心（实测输出 `[0.0, 0.1389]`，起点被夹断）。改为两个语义明确的标量：`single_rel_ci95_halfwidth`（单次测量）与 `mean_rel_ci95_halfwidth`（n 次平均，÷√n），并注明是"围绕 0 的半宽"而非"围绕 σ"
- **【中】σ=0 时消息里出现 `nan`**：`physical_noise=False` 时各次重复逐位一致 → `rel_sigma=0` → χ² 换算退化为 0/0，`low_sample_warning` 会打印"±nan%"、`rel_sigma_of_sigma_pct` 为 nan。修法：`rel==0` 单独处理（数值归零），并改为明确告知"逐位一致 = 随机噪声没起作用，该散布**不是**实验重复性、不可作为不确定度引用"
- **【中】`t_invert` 的 `log` 被覆盖**：第二个 `_quiet()` 块把 `out["log"]` 直接赋成它自己的内容，于是 k 重算阶段的诊断输出（HAPI 版本横幅、线表加载条数…）被丢弃——冷启动实测"带 n_repeats 的 log 行数 = 0"。改为**追加**（实测冷启动 173 行保留）
- **【中】α 缓存的两处**：① 命中时 `info` 返回的是**同一个 dict**（注释却写"返回副本"）→ 任何调用方给它加键都会污染缓存，实测污染会回传；改为返回 `dict(...)` 浅拷贝。② 淘汰用 `pop(next(iter(_ALPHA_CACHE)))`，而 HTTP 模式是 `ThreadingHTTPServer`——并发下两个线程会取到同一个最老键（`KeyError`）或让迭代器看到 size 变化（`RuntimeError`）；改为只在改字典时加 `threading.Lock`（0.6 s 的 Voigt 计算仍在锁外）
- **【低】`t_invert.k_conditions["wn_ref"]` 在未重锚时是 `None`**（实际用的是默认 2964.7）→ 改为取引擎 `meta.cfg` 里**实际生效**的 wn_ref
- **【低】`_maybe_align_laser` 报错打印的扫描半宽可能不是实际值**（用户只给 `amp_V` 时仍打 `scan_span_cm` 默认值）；同时补上 `invalid_laser_params` 分支——原先它会走到 `fits_span` 分支并对 `None` 做 `:.3g` 格式化而抛 `TypeError`（引擎侧同一处也已补）
- **契约自检 84 → 89 项**：新增"两边都调用 `_scan_window_reason`（结构）"、"三档边界 reason 精确匹配"、"显式 offset_V 越上限必须被拒"、"显式合法 offset_V 不因 wn_center 标签被误拒"、"反算路径的部分出光档仍被拒"
- 文档：`docs/VALIDATION.md` §6/§7、`TECHNICAL.md` §4.10 同步订正"rel_sigma 可跨浓度外推"的旧说法

### 第三轮：可达性判据收敛 / 保真度台账 / 不确定度
- **可达性判据三版收敛（高）**：`laser_reach()` 的最终判据 = **整段扫描都在阈值之上且不出电压上限**（`offset_V − amp_V ≥ v_th` 且 `offset_V + amp_V ≤ 5`）。前两版分别只查"偏移落在驱动器 0–5 V 内"与"扫描中心出光"，都会让 **2971.0–2974.1 cm⁻¹** 这一档**不报错、但扫描波形被截断**（2f/1f 静默失真）。引擎侧 `_require_driver_range()` 与判据**共用同一实现**，杜绝两处漂移
- **`allow_partial_dark`（新增，默认 False）**：明知扫描段含暗区、就是要看"越界之后长什么样"时可显式放行；放行后 `warnings` 报出暗区占比，返回里声明**定量结论不可用**（不给静默残谱留后门）
- **保真度台账 `tools/tdlas_fidelity.py`（新增）**：把"物理效应 ↔ 实现位置 ↔ 是否真生效 ↔ 已知缺口"收进**一张可执行的表**（`EFFECTS`），并提供三项离线审计：① **证据一致性**——表里声称的符号/常量必须在源码里真的存在（防"表漂了"）；② **参数可达性**——用假引擎截获真正进入引擎的 kwargs，找出"schema 声明了却从未接上"的死参数（本项目已因此栽过两次：`edge` 一族、`tdlas_das_instrument` 的整族 etalon 几何参数）；③ **已知缺口清单**（默认生效 11 项 / 需显式开启 6 项 / **未实现 7 项**）。`tdlas_guide` 新增 `fidelity_reporting` 披露口径与 `fidelity` 台账（与真源同源转发，**不复制内容**），`must_disclose` 增加第 **⑧** 项；`tools/tdlas_fidelity.py` 进入 CI 硬门禁
- **测量不确定度（新增，默认关）**：`tdlas_invert` 新增 `n_repeats`（默认 0 = 不算）。返回 `uncertainty`：同工况重复仿真的 run-to-run 散布、σ、x 的 95% 区间，并在 n<10 时给出"σ 自身的相对不确定度约 ±X%"的小样本警告。**只含统计（随机）分量**，不含 k 标定误差、HITRAN 数据库不确定度、线型近似与 etalon 条纹——description 与 `docs/VALIDATION.md` 均写明**不得当总不确定度用**
- **吸收谱进程内缓存**：`tdlas_hitran.absorption()` 按 (物种, 窗口, T, P, step, wingHW, iso, 单位) 做缓存（命中返回副本，防就地修改污染；FIFO 上限 64），并提供 `alpha_cache_clear()` / `alpha_cache_info()`。蒙特卡洛类调用（`measurement_uncertainty` / `detection_limit`）会反复算**完全相同**的 Voigt 积分（单次实测 0.6–0.9 s），故这一步主要是在给不确定度功能兜底；α(ν) 是参数的纯函数，缓存**不改变任何数值**
- **工程**：原子写增加重试与降级路径（Windows 上 `os.replace` 偶发 WinError 5）；契约自检 78 → **84 项**（新增"guide 台账与真源同源 / must_disclose⑧ / 无死参数 / 证据一致性"6 项）；CI 新增 **Fidelity audit (offline)** 硬门禁

### 第二轮审核修复：MCP 接口层（参数透传 / 推荐波段 / 可复现性）
- **参数不再静默丢弃（高）**：`_INSTR_KEYS_WMS` 漏掉 `edge` / `trim_frac` / `d2nu_dI2` / `am_i0..am_psi2` / `mod_phase_deg`，而 `inputSchema` 声明了它们 —— AI 传 `edge="falling"` 得到的是 `rising` 的结果、返回体里还写着 `rising`，全程无提示（"以为改了、实际没改"）。现已透传；并新增**未知键直接报错**（`_validate_args`，按 inputSchema 校验必填/类型/未知键，未知键会列出允许的键），工具内亦保留 `ignored_params` 回报作为第二道防线
- **推荐波段可跑通（高）**：`SPECIES_PROFILES` 的 8 个推荐波段横跨 1900–7185 cm⁻¹，而默认激光只是一支 3.36 μm DFB（阈值电流约束下实际覆盖 2964.7–2972.6 cm⁻¹）→ 按文档推荐选波段**必然报错**。新增 `laser_reach()`（离线可达性判据，含阈值电流约束）+ `auto_laser`（默认开）：越界时把 `wn_ref` 重锚到"重锚后确实可达"的值并**如实写入 `warnings` 与 `laser_auto_aligned`**（理想激光器假设，须按 `must_disclose⑦` 告知用户）；`adaptive_condition` 新增「波段可达性 / laser_params_needed」，让 AI 能先告诉用户"该波段需要换激光器"。显式给 `wn_ref`/`offset_V` 或引用设备库时不介入；`auto_laser=False` 保留严格报错
- **`session_id` 真正可达（高）**：`tdlas_wms_instrument` / `tdlas_review` 的 schema 从未声明 `session_id` → 按 schema 驱动的客户端永远传不进来，跨会话工况记忆形同虚设。已补，并让 `tdlas_das_instrument` 也支持会话；`tdlas_review` 的 schema 现在**由 wms 自动同步**，两者不会再脱节
- **默认可复现 + 噪声语义如实（中）**：`seed` 默认 `None`（每次随机）→ 改为 **0**，两次同参调用逐位相同（此前跑-跑散布实测 2–8%），并在返回里给出 `seed_used`；新增 `physical_noise`（默认 True=只含物理固有的散粒/热；False=严格理想仿真，连散粒/热也不注入）。校验第 9 项原写"未加噪声（理想仿真）"与事实不符，现如实报出散粒/热数值并说明其恒在
- **驱动量程边界与二次调谐**：`offset_V` 判据加 1e-9 容差（实测 `wn_center=2975.26` 因浮点误差 -1.89e-13 被误拒）；可达性判据纳入**阈值电流**约束（V 在 0–5 V 内不等于出光）
- **HITRAN 取数重试与归因（中）**：抓取失败改为 3 次指数退避重试；并用 HAPI 离线索引区分"服务端故障"与"同位素不存在" —— 此前一次 502 会被并列报成"该窗口无 HITRAN 收录"，可能被读成"该气体无吸收"（正是模块开头纪律要避免的静默错误）
- **工程**：PNG 文件名净化（`species="../../x"` 不再越出 `tmp/mcp_out/`）；会话/设备文件改**原子写**（临时文件 + `os.replace`，避免崩溃留下半截 JSON）；JSON-RPC **支持批量请求**且解析错误不再回显 Python 异常文本；分析链默认值与仪器链统一（`P=1.01325`、`L_cm` 缺省 50，消除了同一会话里"未给光程"得到 30/50/100 三套值的矛盾）；`remote_link.py` 提示不要把含 token 的输出重定向到日志
- **CI 加固**：矩阵补 **3.13**（实际验证环境）；新增 `tools/contract_check.py`（**离线契约自检**，63 项）作为硬门禁：schema↔签名一致性、参数透传、越界重锚、未知键拒绝、批量请求、文件名净化、原子写、默认 seed 等；`--selftest` 因依赖 hitran.org 抖动，作为非硬门禁单独列出

### 代码审计修复（正确性与输入校验）
- **峰值区间与灵敏度统一（高）**：`tdlas_wms_instrument` 的 `results.S2f1f_peak` 此前在**全窗**取 `argmax`，与在**有效区**（`trim_frac` 剔除两端后）计算的 `sensitivity_k` 不同区间，被剔除的边缘伪影可能被当成"峰值"。现统一在 `valid_mask` 内取峰；新增 `S2f1f_peak_interval`（`trim_frac` / 取峰点数 / `full_window_peak_in_trimmed_edge` 诊断位）供核对
  - **配对口径澄清（后续修正）**：与 `sensitivity_k` 真正成对的是 **`results.S2f_norm_peak`**（k 的分子），**不是** `S2f1f_peak` —— 二者仅在归一化方法为 `2f/1f` 时相等；1f 失效自动退化为 `2f/I0` 时两者可达 **2 倍**之差（实测 `background_subtract=False, am_i0=1.0` 工况：`S2f_norm_peak=0.166994` vs `S2f1f_peak=0.333511`，用后者反演浓度偏高 **99.7%**）。`tdlas_invert` 的 description / `peak_2f1f` 说明 / 返回里的 `S2f_norm_peak_note` 均已按此改写，并有契约断言钉死（防再漂移）
- **二次调谐无实根不再伪造（中）**：`_resolve_scan` 此前用 `max(discriminant, 0)` 把判别式 < 0 截成"重根"，凭空造出一个驱动电压、下游照常出谱；现抛 `ValueError`，报错给出可达极值（调谐曲线顶点）与该核对的参数，并提示可用 `d2nu_dI2=0` 退回线性模型
- **浓度反演输入/输出域校验（中）**：`tdlas_invert` 现校验 `peak_2f1f` 为有限值且 ≥0、`k` 为有限正数（此前负峰高会静默返回负浓度）；结果超出摩尔分数定义域 `(0,1]` 时**不静默截断**，改以 `mole_frac_status` / `mole_frac_valid` / `warning` 显式标注越界（越界即 k 与工况不同源 / 已出弱吸收线性区）

### 工程加固
- **设备库参数校验**：`tdlas_device save` 拒绝空设备名与非法硬件参数（`gain/bw/fs/v_range/eta_*` 须 > 0、`adc_bits` 须为 4–32 整数、`throughput ∈ (0,1]`、`dnu_dI ≠ 0`、须为有限数值）；**设备引用时再校验一次**，兜住历史或手改过的非法条目；拼错的键回报在 `ignored_params`（防静默忽略）
- **缺依赖可操作提示**：缺少 `hitran-api`（`import hapi`）时给出安装指引与当前解释器路径；`--selftest` 遇依赖缺失以非零码退出（便于 CI 判断）
- **自检**新增第 10 项「二次调谐可达性」（不可达必报错 + 可达时取近根正常反算），自检项数 9 → 10（注：与下文 etalon 的「**自动校验**第 10 条」是**两个不同体系**：前者是 `--selftest` 的自测项，后者是 `validation` 的判据）；`docs/VALIDATION.md` 同步（自检清单、已知局限、修订记录）
- `requirements.txt` 加注 `hitran-api` ↔ `import hapi` 的对应关系与自检命令

### etalon 干涉条纹（新增，默认关）
- 物理模型 `etalon_transmission()`：两平行面 F-P 腔 Airy 透过率 `T = 1/(1 + F·sin²(δ/2))`，`F = 4R/(1−R)²`、`FSR = 1/(2nd)`（或直接给 `fringe_fsr` / `fringe_contrast` 覆盖）
- 注入为**光路乘性透过率**：信号支路含漂移，参考谱（背景 / DAS 的 I0）用确定性条纹 → 背景扣除后只留漂移残留
- **默认关闭**（`fringe=False`），绝不静默注入系统性误差；`tdlas_wms_instrument` / `tdlas_das_instrument` / `tdlas_review` 的 `inputSchema` 均已暴露条纹参数
- 新增校验第 10 项「etalon 条纹」：`step > FSR/10` 判 **fail**（条纹混叠）；`线宽 ≤ FSR ≤ 10×线宽` 判 **warn**（会与 2f 吸收混淆）
- 新增自适应 `fringe_report`（首次启用 / 语境变化时播报）+ `AI_INTERACTION_GUIDE["fringe_reporting"]` + `must_disclose⑥`
- 自动校验 9 项 → **10 项**（README / SKILL / TECHNICAL / VALIDATION 同步）

### α 语境的自适应播报
- `tdlas_hitran.absorption()` 记录 `info["alpha_context"]`（T/P/窗口/step/wingHW/diluent），经 `meta` 透传到 MCP 返回
- 新增 `alpha_report = {alpha_peak_context, needs_report, report_reason, report_rule}`（`tdlas_wms_instrument` / `tdlas_das_instrument` / `tdlas_review`）：**仅**本会话首次给出 α 峰值、或 α 语境发生变化时 `needs_report=True`，其余静默携带，避免每次啰嗦
- `AI_INTERACTION_GUIDE` 新增 `alpha_reporting`，`must_disclose` 增加第 ⑤ 项：`needs_report=True` 时给出 α 峰值须同时列出 T/P/step/wingHW/窗口
- 修复 `.gitignore` 行内注释导致 `.tdlas_session.json` / `.tdlas_devices.json` 未被忽略的缺陷（gitignore 不支持模式后注释）

### 校验与默认参数修正
- 默认跨阻增益 `1e3 → 700`：旧默认使默认工况 v_pd≈10.9 V 超出 ±10 V 量程 → ADC 默认即饱和、`tdlas_review` 默认必 fail；现默认工况 v_pd≈7.7 V
- `tdlas_review` 的 `inputSchema` 补齐硬件/设备参数（`adc_bits/v_range/gain/…` 与 `setup/laser/pd/daq/optics`）；此前实现可透传但 schema 未声明，AI 客户端无从传入
- 「DAS-理论一致」判据口径修正：明确"有效区 vs 全窗峰"两点口径差异；多线窗口大偏差不再硬 fail（降为提醒），pass/warn 两档不受谱线密度影响
- 新增 [docs/VALIDATION.md](./docs/VALIDATION.md)：可信度分层、9 条判据口径、使用口径与已知局限

## [v0.1.0] - 2026-09-13

### 仿真核心
- TDLAS/WMS 仿真内核：DAS 透过率 + 免标定 WMS（1f、2f 谐波、2f/1f 归一化）
- 数字锁相：正交解调 + 2 级级联低通（sinc²，非吸收区 2f 残留 4.1%→1.6%）
- 免标定浓度反演（灵敏度法，闭环误差 <1%）+ 检测限（蒙特卡洛 NEC/LOD）
- 噪声模型：散粒/热/RIN（白）+ 1/f 慢漂移 + 1/f 粉红（可独立开关）
- 检测极限扫描：LOD 随光程 L / 参考浓度 x 的网格扫描（LOD∝1/L，用于系统选型）

### 仪器级链路
- DAS 仪器链路：DAQ 电压 → 激光调谐（V→I→波数/功率）→ 光路 → PD → ADC
- WMS 仪器链路：三角波扫描 + 正弦调制（fm=30 kHz）→ 锁相 → 1f、2f、2f/1f
- 调制系数 m≈2.2 自动优化；采样率自举（每调制周期 ≥8 点）
- 采样率整数倍铁律：fs 必须 = fm 整数倍且 ≥8 点/周期（自动吸附 + 同步放大 n_samples）
- 电压自适应波数：amp_V/offset_V 由 wn_center + scan_span_cm 经 V→I→ν 关系反算
- GAS_DEFAULTS 工况默认表（species/wn_center/T/P/x/L_cm）
- WMS 背景扣除可开关（`background_subtract`，默认 True；关闭仅用于诊断原始 RAM 基线）

### 关键修复（踩坑沉淀）
- 波数轴错位：`wn_ref` 自动对齐 `wn_center`（消除"伪影"）
- 横轴用**扫描波数**（非瞬时波数，瞬时波数含 ±a 摆动）
- DAS 走**无调制独立链路**（不能拿含调制信号平均伪造）
- DAS 基线用理想 I0（密集谱区无非吸收区时，多项式拟合失效）
- DAS 补噪声（此前漏加，导致"WMS 反而差"的假象）
- 2f/1f 失效判据：仅"非吸收区泄漏 >30%"；1f 过零是物理奇点、非失效
- 2f/I0 背景扣除（RAM 基线）：用 τ≡1 无吸收参考谱复减 L-I 二阶非线性残留
- m 警告不再硬编码 2.2：按谱线密度修正（孤立 2.2 / 中等 1.8 / 密集 1.25），且仅在手动指定 m 时提示（自适应优化不再误报"偏离最优"）
- 默认浓度按物种推荐：GAS_DEFAULTS x 1e-3→1e-4；t_simulate/t_invert/t_detection_limit 读 SPECIES_PROFILES 的 x_typ/L_cm（强吸收 CH4@3.3μm 避免 αL 饱和）
- `tdlas_invert` 与 `tdlas_wms_instrument` 模型对齐：wms_instrument 返回完整链路 `sensitivity_k`，invert 优先复用该 k、否则内部跑完整链路重算；不再用解析版 `simulate()` 的灵敏度（两路 S2f/1f 差 ~6×，曾致 501% 反演误差）
- edge 语义：电压上升沿 = 波数下降（dν/dI<0）

### AI 交互层
- `AI_INTERACTION_GUIDE`：正向 SOP、参数索取优先级、术语表、DAS/WMS 选型
- 主动澄清机制：`clarify` 字段 + 带选项问句（先问再出图，硬约束）
- 澄清提问改用**原生结构化提问工具**（AskUserQuestion 类点击式选择框），禁止纯文字列表；1 次 1–4 题、按优先级分批多轮
- 自动反算/诊断开关参数（amp_V/offset_V/background_subtract）不进 param_requests，不再让用户确认
- 自动校验：9 项（DAS-理论一致、2f 峰位、αL 弱吸收、采样率…）+ `tdlas_review`
- 跨会话状态机：`tdlas_session` 记住已确认参数，多轮补全
- 工况自适应：`SPECIES_PROFILES` 按物种推荐波段/浓度/光程
- 器件型号 → AI 联网检索官方 datasheet 提取参数（按器件类别列明要查项）

### 设备管理（新增）
- `tdlas_device` 工具：命名保存/查看/删除激光器·探测器·采集卡·光学四类设备，打包整机配置 `save_setup`，设默认设备 `set_default`
- 仿真工具（`tdlas_wms_instrument` / `tdlas_das_instrument`）支持 `setup=`（整机）/`laser=`/`pd=`/`daq=`/`optics=`（单设备）/ 默认设备 三级引用
- 参数解析优先级：显式参数 > 单设备 > setup 整机 > 默认设备；被设备库覆盖的硬件参数不再列入 `assumptions`
- 设备库持久化于 `.tdlas_devices.json`（已 gitignore），与 `tdlas_session`（记工况）互补

### 绘图
- 统一 3×2 六子图：驱动电压 / PD 原始 / DAS+理论 / 1f / 2f / 归一化
- 线型约定：实线=实测、虚线=理论、点线=剔除区（图例必标）
- 中文字体自动探测；绘图降采样防混叠

### 工程
- 自包含 HITRAN 取数（HAPI 1.x，免 key），不依赖 Hitran MCP
- MCP 服务器 12 工具（新增 `tdlas_device`）；配置示例 + README + SKILL 文档
- HTTP 传输（MCP Streamable HTTP）：`--http --host --port --token`，端点 `/mcp`，远端 URL 直链；默认仍 stdio，纯标准库零新增依赖
- 远程直链：`tools/remote_link.py` 一键 cloudflared 隧道暴露（127.0.0.1 绑定 + 强制 Bearer token，经代理自动 http2）；修复 chunked 传输编码读取 bug（cloudflared 转发不再 501 `Unsupported method`）
- 安全修复：MCP 返回值（尤其 tools/call 的 log 字段，HAPI 会打印 Hitran_Data 绝对路径）统一在**返回边界 + 源头**脱敏，绝对路径替换为 `<workspace>`/`<home>`，避免远端直链泄露服务器目录结构；不硬编码真实路径（`Path.home()` 动态获取）
