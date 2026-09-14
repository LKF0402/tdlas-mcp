# Changelog

本文件精炼记录 tdlas-mcp 的功能演进（按领域归纳，非逐提交罗列）。

## [Unreleased]

### 校验与默认参数修正
- 默认跨阻增益 `1e3 → 700`：旧默认使默认工况 v_pd≈10.9 V 超出 ±10 V 量程 → ADC 默认即饱和、`tdlas_review` 默认必 fail；现默认工况 v_pd≈7.7 V
- `tdlas_review` 的 `inputSchema` 补齐硬件/设备参数（`adc_bits/v_range/gain/…` 与 `setup/laser/pd/daq/optics`）；此前实现可透传但 schema 未声明，AI 客户端无从传入
- 「DAS-理论一致」判据口径修正：明确"有效区 vs 全窗峰"两点口径差异；多线窗口大偏差不再硬 fail（降为提醒），pass/warn 两档不受谱线密度影响
- 新增 [docs/VALIDATION.md](./docs/VALIDATION.md)：可信度分层、9 条判据口径、使用口径与已知局限

## [v0.1.0] - 2026-09-13

### 仿真核心
- TDLAS/WMS 仿真内核：DAS 透过率 + 免标定 WMS（1f、2f 谐波、2f/1f 归一化）
- 数字锁相：正交解调 + 2 级级联低通（sinc²，非吸收区残留 4.1%→1.5%）
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
