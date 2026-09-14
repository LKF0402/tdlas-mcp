# tdlas-mcp 技术文档

> 面向专业技术员：说明本项目的**物理模型、逐级链路实现、关键算法、默认参数、已知近似与实现陷阱**。
> 配套文档：`README.md`（功能与快速开始）、`SKILL.md`（AI 使用规范）、`CHANGELOG.md`（变更记录）。

## 目录

- [1. 架构概述](#1-架构概述)
- [2. 物理模型](#2-物理模型)
  - [2.1 Beer-Lambert 吸收](#21-beer-lambert-吸收)
  - [2.2 HITRAN 谱线与 Voigt 线型](#22-hitran-谱线与-voigt-线型)
  - [2.3 波长调制光谱（WMS）原理](#23-波长调制光谱wms原理)
  - [2.4 调制系数 m 与最优调制](#24-调制系数-m-与最优调制)
- [3. 仪器链路逐级实现](#3-仪器链路逐级实现)
  - [3.1 DAQ 驱动电压](#31-daq-驱动电压)
  - [3.2 激光器调谐 V→I→ν/P](#32-激光器调谐-vinu)
  - [3.3 光路与吸收](#33-光路与吸收)
  - [3.4 探测器 PD](#34-探测器-pd)
  - [3.5 噪声模型（白 + 1/f）](#35-噪声模型白--1f)
  - [3.6 ADC 量化](#36-adc-量化)
  - [3.7 数字锁相](#37-数字锁相)
- [4. 关键算法](#4-关键算法)
  - [4.1 自适应调制深度](#41-自适应调制深度)
  - [4.2 DAS 直接吸收](#42-das-直接吸收)
  - [4.3 归一化：2f/1f 与 2f/I0](#43-归一化2f1f-与-2fi0)
  - [4.4 扫描方向与剔除区](#44-扫描方向与剔除区)
  - [4.5 浓度反演与检测限](#45-浓度反演与检测限)
  - [4.6 自动校验（9 项）](#46-自动校验9-项)
- [5. 默认参数表](#5-默认参数表)
- [6. 已知近似与局限](#6-已知近似与局限)
- [7. 实现陷阱（踩坑记录）](#7-实现陷阱踩坑记录)
- [8. 参考文献](#8-参考文献)

---

## 1. 架构概述

```
tools/tdlas_hitran.py   HITRAN 取数层（HAPI 1.x 封装，自包含、免 key）
        ↓
tdlas_sim.py            仿真核心（链路建模、锁相、算法、绘图）
        ↓
tools/tdlas_mcp.py      MCP 服务器（12 工具，stdio JSON-RPC）
```

**设计纪律**：三层严格分离。取数层不依赖 MCP；仿真核心不依赖 MCP 协议；MCP 层只做参数编排与结果封装。

---

## 2. 物理模型

### 2.1 Beer-Lambert 吸收

单色光穿过均匀气体：

$$I(\nu) = I_0(\nu)\,\exp\!\big[-\alpha(\nu)\,L\big]$$

- $\alpha(\nu)$：吸收系数（cm⁻¹），由 HITRAN 线参数 + 线型函数计算
- $L$：光程（cm）
- $\alpha L$（记作 **αL**）称为**吸光度**，无量纲

弱吸收（$\alpha L \ll 1$）下 $\tau \approx 1-\alpha L$，信号与浓度近似线性；本项目校验区间为 $\alpha L \in [10^{-5}, 0.1]$。

### 2.2 HITRAN 谱线与 Voigt 线型

`tools/tdlas_hitran.py` 用 HAPI 1.x 官方接口：

- `hapi.fetch()` 下载线表（缓存到 `Hitran_Data/`）
- `hapi.absorptionCoefficient_Voigt()` 计算吸收系数

**Voigt 线型** = 高斯（多普勒展宽）⊗ 洛伦兹（碰撞展宽）：

$$V(\nu;\sigma,\gamma)=\int_{-\infty}^{\infty}G(\nu';\sigma)\,L(\nu-\nu';\gamma)\,d\nu'$$

**两条安全纪律**（实现要点）：

1. **窗口安全**：线表必须真正覆盖请求窗口；未覆盖则按窗口**重新抓取**，避免"旧表静默缺线"
2. **空谱硬报错**：0 条线直接 `raise`，绝不静默返回平谱

### 2.3 波长调制光谱（WMS）原理

在扫描基础上叠加高频正弦调制，瞬时波数：

$$\nu(t) = \nu_c(t) + a\cos(\omega_m t)$$

其中 $\nu_c(t)$ 为慢扫描（三角波），$a$ 为**调制深度**（cm⁻¹），$f_m=\omega_m/2\pi$ 为调制频率。

透过率 $\tau(\nu)$ 被调制后成为周期函数，展开为傅里叶级数：

$$\tau\big(\nu_c + a\cos\omega_m t\big) = \sum_{n=0}^{\infty} H_n(\nu_c, a)\cos(n\omega_m t)$$

数字锁相提取第 $n$ 次谐波分量 $H_n$，即 **nf 信号**。本项目提取 **1f** 与 **2f**。

**为何用 WMS**：把信号从 DC 搬到 $f_m$，避开 $1/f$ 噪声与低频漂移；配合窄带锁相，等效噪声带宽远小于 DAS 的全带宽测量。

### 2.4 调制系数 m 与最优调制

定义**调制系数**：

$$m = \frac{a}{\mathrm{HWHM}}$$

- 经典理论（Arndt 1965；Reid & Labrie 1981）：**洛伦兹线型纯 FM** 下 $m_{\rm opt}\approx 2.2$
- 但**实际最优依赖线型与谱线密度**（见 §4.1 实测数据）

---

## 3. 仪器链路逐级实现

链路（对应 `simulate_wms_instrument`）：

```
DAQ 驱动电压 → 激光器调谐 → 光路 → PD → (+噪声) → ADC → 数字锁相 → 1f、2f、归一化
```

### 3.1 DAQ 驱动电压

$$V_{\rm drive}(t) = V_{\rm scan}(t) + V_{\rm mod}\cos(2\pi f_m t + \phi_{\rm mod})$$

扫描三角波（`daq_triangle`）：

$$\mathrm{tri}(t) = 1 - 4\left|\left(\frac{t}{T_{\rm scan}} + \frac{\phi}{360}\right)\bmod 1 - 0.5\right|$$

- 输出 $= \mathrm{offset} + \mathrm{amp}\cdot\mathrm{tri}(t)$
- **前半周期电压上升、后半下降**（`tri`: −1→+1→−1）

### 3.2 激光器调谐 V→I→ν/P

```python
i   = eta_VI * v                                              # mA
di  = i - i_ref
nu  = wn_ref + di * dnu_dI + 0.5 * di**2 * d2nu_dI2           # cm⁻¹（二阶泰勒）
P   = max(eta_IP * (i - i_th), 0)                             # mW，低于阈值无输出
```

**调谐非线性** `d2nu_dI2`（cm⁻¹/mA²，默认 0）：真实 DFB 的电流-波长响应并非严格线性，该项建模二阶弯曲，会使扫描窗口相对中心**非对称**扩展/收缩。

**关键符号**：默认 `dnu_dI = -0.088 < 0`（真实 DFB 激光：电流↑ → 结温↑ → 波长红移 → 波数↓）。

这导致一个**重要的方向耦合**（见 §7 陷阱）：

> **波数递增 ⟺ 电流递减 ⟺ 光强递减**（三者由 $d\nu/dI<0$ 与 $P\propto I$ 耦合）

### 3.3 光路与吸收

```python
alpha = interp(nu_laser, nu_grid, alpha_pure) * x    # 混合气吸收系数
tau   = exp(-alpha * L_cm)
p_opt = p_laser * throughput * tau
```

### 3.4 探测器 PD

```python
i_pd = p_opt * 1e-3 * resp                      # p_opt 单位 mW → W
v_pd = pd_lowpass(i_pd * gain, bw, fs)          # 跨阻 + 带宽低通 → V
```

**带宽低通**（`pd_lowpass`）：一阶 RC 模型，$f_c=\min(f_{\rm bw},0.45f_s)$

$$\alpha = 1-e^{-2\pi f_c/f_s},\qquad y[n]=y[n-1]+\alpha\,(x[n]-y[n-1])$$

**要点**：探测器有响应时间，会削掉高于带宽的信号分量。若 $f_{\rm bw}$ 接近或低于 $2f_m$，2f 会被明显衰减（实测 bw 1 MHz→40 kHz，2f 从 0.0881 削到 0.0574）。`bw` 同时决定噪声带宽，故它**同时影响信号与噪声**。

### 3.4b 残余幅度调制（RAM）

激光的**固有**强度调制（与 FM 存在相位差）：

$$I_0(t) = 1 + i_0\cos(\omega_m t+\psi_1) + i_2\cos(2\omega_m t+\psi_2)$$

- 参数 `am_i0` / `am_i2` / `am_psi1` / `am_psi2`，**默认 0（纯 FM）**
- 仪器链路的 AM 含 L-I 斜率项（与 FM **同相**）；本组参数额外叠加真实 DFB 的 RAM（可与 FM **异相**）
- RAM 会显著改变 1f（实测 am_i0=0→0.05，1f 峰从 0.177 升到 0.304），进而影响 2f/1f 归一化

### 3.5 噪声模型（白 + 1/f）

**白噪声**（`noise_currents`，等效输入电流噪声 A）：

| 分量 | 公式 |
|---|---|
| 散粒 | $\sigma_{\rm shot}=\sqrt{2qI\,\Delta f}$ |
| 热（跨阻） | $\sigma_{\rm th}=\sqrt{4k_BT\Delta f/G}$ |
| RIN | $\sigma_{\rm RIN}=I\cdot{\rm RIN}\cdot\sqrt{\Delta f}$ |

三者按功率合成：$\sigma=\sqrt{\sigma_{\rm shot}^2+\sigma_{\rm th}^2+\sigma_{\rm RIN}^2}$，加为电压噪声 $\sigma G$。

**1/f 噪声**（可选，默认关）：

- **慢漂移**：$P \leftarrow P\,(1 + d\cdot\sin(2\pi\cdot 1\,\mathrm{Hz}\cdot t))$，其中 $d$ 即参数 `drift_frac`
- **粉红噪声**（`pink_noise`）：频域给白噪声加权 $1/\sqrt{f}$ 再逆 FFT → 功率谱 $\propto 1/f$

> **默认全部噪声关闭**（理想仿真）。MCP 会主动询问用户是否注入（见 `noise_and_interaction`）。

### 3.6 ADC 量化

```python
lsb = 2 * v_range / 2**adc_bits
v_adc = clip(round(v / lsb) * lsb, -v_range, v_range)
```

默认 16-bit、±10 V → `lsb ≈ 0.305 mV`。同时统计饱和点数 `n_sat`。

### 3.7 数字锁相

`wms_harmonic_lockin(S, t, fs, fm, n, n_cycle_avg, n_stages)`：

$$X = \big(S\cdot\cos(n\omega_m t)\big) * h,\qquad Y = \big(S\cdot\sin(n\omega_m t)\big) * h$$

$$S_{nf} = \sqrt{X^2 + Y^2}$$

其中 $h$ 为**矩形滑动平均窗**（长度 $=n_{\rm cycle\_avg}\cdot f_s/f_m$ 点），**级联 $n_{\rm stages}$ 级**。

**要点**：

- 单级矩形窗频响为 $\mathrm{sinc}$，**第一旁瓣仅 −13 dB**，对 $2f_m/4f_m$ 残留抑制不足
- 级联 2 级 → $\mathrm{sinc}^2$（−26 dB）：实测非吸收区残留从 **4.1% 降至 1.6%**
- 再往上（3、4 级）收益很小
- `lockin_avg` 取 **1**：窗口恰为一个调制周期，兼顾线形保真与残留抑制（继续加大窗口只会平滑线形）

> 幅值取 $\sqrt{X^2+Y^2}$（正交幅值），与"系数×2"的严格定义差一个常数因子 2，**不影响波形形状与相对比较**。

---

## 4. 关键算法

### 4.1 自适应调制深度

`adaptive_modulation_index()`：**三路并行**，`m_opt="auto"` 时启用（默认）。

| 路 | 方法 | 用途 |
|---|---|---|
| **A 解析** | Lorentz 2.2 基准，按谱线密度修正（>50 线→1.25；>10 线→1.80） | 理论参考 |
| **D 模型** | 对扫描曲线二次拟合，取连续域峰值 | 模型化最优 |
| **B 扫描** | 完整时域链路（无噪声）**粗扫步长 0.2 + 峰值附近细扫步长 0.05** | **实测最优 ← 采用** |

**选择策略「98% 幅值的最小 m」**：

$$m^{*} = \min\{\,m \mid \mathrm{peak}(m) \ge 0.98\cdot \mathrm{peak}_{\max}\}$$

理由：2f 峰值附近是**平顶**，牺牲 ≤2% 灵敏度可换取更小的 $m$ → 更好的 2f 轮廓、更低过调制风险、更小邻线干扰。

**实测结果**：

| 分子 | 线数 | 采用 m | 损失 |
|---|---|---|---|
| H2O 7185.6 | 77 | 2.00 | 0.84% |
| CH4 2968.5 | 221 | 1.20 | 1.06% |
| C2H6 2964.5 | 158 | 1.60 | 2.00% |

> **采样率要求**：$f_s$ 必须是调制频率 $f_m$ 的**整数倍**且每调制周期 ≥8 点（否则滑动平均窗口不足一个调制周期，直流/调制分量会泄漏进 2f）；不满足时自动吸附到最近的合法整数倍，并同步放大采样点数以保持总采集时间。默认 $f_s=240$ kS/s $=8f_m$。$m$ 自适应扫描内部用 $16f_m$（480 kS/s）以保证判定可靠。

### 4.2 DAS 直接吸收

DAS 是**无调制**技术：调制幅值置零，**独立走一遍完整链路**（DAQ → 激光 → 光路 → PD → ADC），再由 PD 电压反演吸光度。

```python
p_das    = voltage_to_laser(v_scan)[2]                  # 无调制链路光功率
v_das_I0 = p_das * throughput * 1e-3 * resp * gain      # 无吸收光强（基线 I0）
v_das    = v_das_I0 * exp(-alpha * L)                   # 叠加气体吸收
das      = -log(v_das / v_das_I0)                       # = αL
```

**基线取无吸收光强 $I_0$**：仿真中 $I_0$ 可精确给出，直接得到吸光度 $\alpha L$。DAS 链路同样**过 ADC 量化**，与 WMS 保持一致的噪声与量化条件，便于等条件对比。

### 4.3 归一化：2f/1f 与 2f/I0

| 方法 | 定义 | 前提 |
|---|---|---|
| **2f/1f** | $\dfrac{X_{2f}}{R_{1f}}-\dfrac{X_{2f}^{bg}}{R_{1f}^{bg}}$ 复减后的模 | 1f 与光强成正比（AM 主导）且基线平稳 |
| **2f/I0** | $\dfrac{\left|S_{2f}-S_{2f}^{bg}\right|}{I_0}$，$I_0$=PD 非吸收区均值 | 1f 不可用时退化方案 |

**背景扣除**：两条归一化都先用「无吸收参考谱」（$\tau\equiv1$，含相同 RAM/AM 与慢漂移、无白/粉红噪声）走同一条链路算出 $S_{1f}^{bg},S_{2f}^{bg}$，再**复减**。它消除的是 L-I 二阶非线性产生的 RAM 基线（非吸收区 2f 残余），只能靠背景扣除去除，降噪 / 提采样率都压不掉。背景扣除可开关（`background_subtract`，默认 `True`）：设为 `False` 时保留原始 RAM 基线，**仅供诊断**原始基线，不应用于浓度反演或检测限结论。

**失效判据（唯一）**：非吸收区 $2f/1f$ 泄漏 > 峰值的 30%（说明 1f 基线随扫描漂移）→ 自动退化为 2f/I0。

**解读方式**：浓度反演取 **2f 峰值**（吸收线处），避开 1f 过零附近的奇点区。

### 4.4 扫描方向与剔除区

- **edge** 指**驱动电压**方向；`rising`=前半（电压↑，因 $d\nu/dI<0$ 故波数↓），内部反转为波数递增输出
- **trim_frac = 0.12**：剔除扫描两端各 12%（三角波转折点导数不连续，高频谐波泄漏进 2f，伪影可达真信号 7.5 倍）

### 4.5 浓度反演与检测限

- **反演**：`tdlas_invert` 用灵敏度 $k$（= 归一化 2f 峰高 ÷ 摩尔分数）反演 $x=\text{peak}/k$。$k$ 必须来自**完整链路**（`tdlas_wms_instrument` 返回的 `sensitivity_k`，或 invert 内部跑完整链路重算）——解析版 `simulate()` 的灵敏度与完整链路 S2f/1f 差 ~6×，不可混用。仅弱吸收 $\alpha L\ll1$ 成立，闭环误差 <1%
- **检测限**：`tdlas_detection_limit` 蒙特卡洛，由等效透过率噪声 $\sigma_\tau$ → NEC / LOD（默认 3σ）
- **检测限扫描**：`tdlas_detection_limit_scan` 扫 LOD 随光程 $L$ / 参考浓度 $x$ 的网格——弱吸收下 LOD $\propto 1/L$（光程加倍、LOD 减半），且与 $x$ 基本无关；$\alpha L\gtrsim0.1$ 进入非线性区后 LOD 回升，据此选最优光程。

### 4.6 自动校验（9 项）

`validate_wms_result()` 每次返回，供非专业用户判断可信度：

波数轴对齐 · DAS-理论一致 · αL 弱吸收区 · 是否孤立线 · 调制系数 m · 采样率 · ADC 动态范围 · 归一化方法 · 噪声

`overall = pass/warn/fail`；fail 时不得下结论。

### 4.7 设备库引用（MCP 层）

仪器基本固定时，可在 MCP 层用 `tdlas_device` 把激光器/探测器/DAQ/光学四类设备命名保存，仿真时直接引用，**省去每次手填硬件参数**：

- 保存：`tdlas_device action=save device_type=laser name="我的1653nmDFB" eta_VI=24 dnu_dI=-0.088 wn_ref=2964.7 …`
- 整机：`tdlas_device action=save_setup name="实验室A套" laser=… pd=… daq=… optics=…`
- 默认：`tdlas_device action=set_default device_type=laser name=…`（不指定设备时自动用默认）
- 引用：`tdlas_wms_instrument` / `tdlas_das_instrument` 支持 `setup=`（整机）、`laser=`/`pd=`/`daq=`/`optics=`（单设备）

**参数解析优先级**：本次显式参数 > 单设备引用 > setup 整机 > 默认设备。设备库提供的硬件参数在返回里**不再列入 `assumptions`**（默认可信、无需重复确认）。设备库持久化于仓库根 `.tdlas_devices.json`（已 gitignore），MCP 重启/换会话仍保留，与 `tdlas_session`（记工况 T/P/x/L）互补。

### 4.8 α 语境的自适应播报（MCP 层）

α（吸收系数 / αL）峰值由 **T、P、网格步长 `step`、翼截断 `wingHW`、波数窗口** 共同决定；其中 `step`/`wingHW` 还是各调用点自适应选取的（WMS：`step=min(2e-4, span/2000)`、`wingHW=max(10, 5·span)`）。因此**裸报 α 峰值不可复现、不可比较**。

分两处实现：

1. **源头记录语境**：`tools/tdlas_hitran.absorption()` 在 `info["alpha_context"]` 中记录
   `{T_K, P_atm, window_cm-1, step_cm-1, wingHW_cm-1, diluent}`，经仿真 `meta` 透传到 MCP 返回。
2. **自适应播报**：`_alpha_report_block()` 维护会话态 `last_alpha_report`，返回
   `alpha_report = {alpha_peak_context, needs_report, report_reason, report_rule}`：
   - `needs_report=True` 仅当 ① 本会话**首次**给出 α 峰值，或 ② **语境变化**（物种或任一语境量与上次不同）；
   - 其余情况为 `False`，语境仍随结果静默携带，避免每次啰嗦；
   - 记录写入 `.tdlas_session.json`，跨 MCP 重启有效。

AI 侧由 `AI_INTERACTION_GUIDE["alpha_reporting"]` 与 `must_disclose⑤` 约束：`needs_report=True` 时，给出 α 峰值**必须同时**列出 T / P / step / wingHW / 窗口 五项。

---

## 5. 默认参数表

| 模块 | 参数 | 默认值 | 说明 |
|---|---|---|---|
| DAQ | `fs` | 240 kHz | = 8×fm(30 kHz)；须为 fm 整数倍且 ≥8 点/周期，否则自动吸附 |
| | `n_samples` | 120 000 | = fs×0.5 s（50 个扫描周期 @100 Hz）；fs 变化时同步放大以保持总时间 |
| | `adc_bits` / `v_range` | 16 / ±10 V | 常见 16-bit DAQ 规格 |
| 扫描 | `scan_span_cm` | 1.5 cm⁻¹ | 三角波扫描**波数半宽**；`amp_V = scan_span_cm/(η_VI·\|dν/dI\|)` |
| | `freq_Hz` | 100 Hz | 三角波频率 |
| | `amp_V` / `offset_V` | **自动反算** | 由 `wn_center`+`scan_span_cm` 经电压—波数关系反算（一般无需手填；显式给则优先） |
| 激光 | `eta_VI` | 24.0 mA/V | V→I 跨导 |
| | `dnu_dI` | −0.088 cm⁻¹/mA | **负**（DFB 红移） |
| | `d2nu_dI2` | 0 | 调谐二阶非线性 cm⁻¹/mA² |
| | `am_i0`/`am_i2` | 0 / 0 | RAM 强度调制幅度（0=纯 FM） |
| | `am_psi1`/`am_psi2` | 0 / 0 | AM 相对 FM 的相位差 rad |
| | `wn_ref` / `i_ref` | 2964.7 / 120.0 | 激光**固定参考点**（不随 wn_center 变） |
| | `i_th` / `eta_IP` | 30.0 mA / 0.15 mW/mA | 阈值电流 / I→P 斜率 |
| 工况 | `species` | CH4 | 待测气体（HITRAN 分子式，如 CH4 / H2O / CO2） |
| | `wn_center` | 2968.5 cm⁻¹ | 目标吸收线中心（决定激光调谐到哪条线；`offset_V` 由此反算） |
| | `T` / `P` | 296 K / 1.01325 atm | 温度 / 气压 |
| | `x` | 1e-4 | 摩尔分数（= 100 ppm；强吸收分子如 CH4@3.3μm 默认更低，否则 αL 饱和） |
| | `L_cm` | 50.0 cm | 有效光程（气室；多通池 ≈ 增大 L） |
| 光路 | `throughput` | 0.90 | 光学总透过率（窗片/镜片/光纤/连接器**统一折成一个数**）；有效光程见上 `L_cm` |
| PD | `resp` / `gain` | 0.9 A/W / 1e3 V/A | InGaAs / 跨阻 |
| | `bw` | 1 MHz | 带宽 |
| | `rin` / `drift_frac` / `flicker_frac` | **0 / 0 / 0** | 噪声默认全关 |
| 调制 | `mod_freq_Hz` | 30 kHz | 调制频率 |
| | `m_opt` | **"auto"** | 自适应调制深度 |
| | `lockin_avg` / `lockin_stages` | 1 / 2 | 低通参数 |
| | `trim_frac` | 0.12 | 剔除转折点比例 |

> **电压是波数的从变量**：`scan_span_cm`（用户给的波数半宽）与 `wn_center`（目标线）才是自然坐标；
> `amp_V`、`offset_V` 由它们的电压—波数关系（`V→I→ν`）反算，**不写死**。`wn_center` 不填则按上表默认 2968.5；
> 若选了不在此 DFB 调谐范围内的线，反算出的 `offset_V` 会越出驱动器 0–5 V 范围 → 仿真直接报错提示换激光参数。

---

## 6. 已知近似与局限

1. **激光调谐**：用二阶泰勒（含 `d2nu_dI2`，默认 0）；三阶以上与**热迟滞**未建模
2. **L-I 线性**：`P = eta_IP·(i-i_th)`，忽略效率随温度/老化变化
3. **RAM**：已支持显式参数（`am_i0/am_i2/am_psi1/am_psi2`，默认 0=纯 FM）；未建模其温度/频率依赖
4. **Voigt 近似**：用 HAPI 的 `absorptionCoefficient_Voigt`，非逐线精确 Voigt 数值积分
5. **单次扫描**：默认取单一扫描方向，未做多周期平均
6. **DAS 基线为理想 $I_0$**：真实实验需背景扣除，精度低于此

---

## 7. 实现陷阱（踩坑记录）

> 本节记录**实际犯过的错误**，技术员修改代码时务必留意。

| # | 陷阱 | 症状 | 正确做法 |
|---|---|---|---|
| 1 | **`wn_ref` 与 `wn_center` 错位** | 2f 幅度极小、峰位对不上、全像噪声 | `wn_ref` 必须按 `wn_center` 反算对齐 |
| 2 | **横轴用瞬时波数** | 曲线折成"水平条纹" | 横轴用**扫描波数**（不含 ±a 调制摆动） |
| 3 | **DAS 用含调制信号平均伪造** | DAS 呈锯齿扫帚状 | DAS 是无调制独立链路 |
| 4 | **DAS 基线用非吸收区拟合** | 密集谱区 DAS 峰偏 1.4 cm⁻¹、为**负值** | 用理想 $I_0$（仿真） |
| 5 | **DAS 漏加噪声/ADC** | 得出"WMS 反而比 DAS 差"的**假结论** | DAS 与 WMS 必须同源噪声 + 同样过 ADC |
| 6 | **1f 过零误判为"1f 失效"** | 全图弃用 2f/1f | 过零是物理奇点；失效只看"非吸收区泄漏 >30%" |
| 7 | **锁相单级低通** | 残留 4.1% | 级联 2 级（sinc²）→ 1.6% |
| 8 | **`lockin_avg>1`** | 模糊线形且不降残留 | 固定为 1 |
| 9 | **低采样率扫描调制深度** | **最优 m 判断完全反转**（CH4 误选 2.45 而非 1.25） | 扫描必须 ≥16 点/调制周期 |
| 10 | **fs 提升不改 n_samples** | 总时间从 1 s 缩到 0.417 s，扫描周期数 100→42 | fs 提升时**同步放大 n_samples** |
| 11 | **edge 语义混淆** | `rising` 得到"下降"的 PD 基线 | edge 指**电压**方向；$d\nu/dI<0$ 使波数↑↔光强↓ |
| 12 | **`bw` 只参与噪声、不作用于信号** | 带宽参数形同虚设（模型不自洽） | 信号也须过一阶 RC 低通（`pd_lowpass`） |
| 13 | **$f_s$ 非 $f_m$ 整数倍** | 直流/调制分量泄漏进 2f（~3e-3；整数倍时 1e-13） | $f_s$ 吸附到 $f_m$ 的整数倍（且 ≥8 点/周期） |
| 14 | **只按"点数"设采样、不与 $f_s$ 绑定** | $f_s$ 变化后总时间/扫描周期数随之漂移 | 用总时间定义 $n_{samples}=f_s\cdot T$，$f_s$ 变化时同步缩放 |

**第 9 条尤其隐蔽**：低采样率下锁相正交不完备，直流/RAM 泄漏进 2f，使"2f 峰值"主要反映强度调制 AM（调制越深越大）→ 假性单调递增，导致优化方向完全相反。

---

## 8. 参考文献

1. Arndt R. *Analytical line shapes for Lorentzian signals broadened by modulation*. J. Appl. Phys., 1965.
2. Reid J, Labrie D. *Second-harmonic detection with tunable diode lasers*. Appl. Phys. B, 1981.
3. HAPI (HITRAN API): https://github.com/hitranonline/hapi
4. HITRAN Database: https://hitran.org
