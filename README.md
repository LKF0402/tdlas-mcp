<div align="center">

<h1>tdlas-mcp</h1>

<p><b>TDLAS/WMS 仪器级仿真 MCP 服务器</b><br>
DAQ → 激光 → 光路 → PD → ADC → 数字锁相 · 自然语言驱动</p>

<img src="https://img.shields.io/badge/Python-3.10%2B-3776AB" alt="Python">
<img src="https://img.shields.io/badge/MCP-stdio%20HTTP-6B8FD4" alt="MCP">
<img src="https://img.shields.io/badge/HITRAN-HAPI%201.x-4C8C4A" alt="HITRAN">

</div>

---

## 是什么

对 TDLAS 实验全链路逐级建模，通过 MCP 协议让 AI 助手用自然语言完成 WMS/DAS 仿真：

```
DAQ 电压(三角波+正弦) → 激光调谐 → HITRAN 吸收 → PD → ADC → 锁相 → 1f/2f/2f1f
```

自包含，无需 API key，首次运行自动下载线表缓存。

## 快速上手

```bash
pip install -r requirements.txt

# 自检
python tools/tdlas_mcp.py --selftest

# 接入 MCP 客户端（stdio）
# 将 mcp.config.example.json 中 args 路径改为实际路径即可
```

> **推荐同时安装 [SKILL.md](./SKILL.md)** 作为独立 Skill：AI 可读取交互协议、参数优先级与出图规范，避免选错工具或瞎编参数。

## 工具一览（12 个）

| 工具 | 用途 |
|------|------|
| **tdlas_wms_instrument** | **画 WMS 图首选**：仪器级全链路，标准 6 子图输出 |
| tdlas_das_instrument | 仪器级 DAS 仿真 |
| tdlas_simulate | 简化解析模型，快速算峰高数值（非画图工具） |
| tdlas_das_chain | 三角波 DAS 链路：PD 信号 → 基线扣除 → 吸光度 |
| tdlas_review | 结果二次审核（9 项校验，不画图） |
| tdlas_invert | 2f/1f 峰高 → 摩尔分数反演 |
| tdlas_detection_limit | 噪声 → NEC / LOD 计算 |
| tdlas_detection_limit_scan | LOD 随光程/浓度网格扫描（选型用） |
| tdlas_session | 跨会话工况参数持久化 |
| tdlas_device | 硬件参数库（激光器/PD/DAQ/光学） |
| tdlas_guide | AI 交互协议与术语表 |
| tdlas_selftest | 全链路自检 |

## 远程调用

```bash
# HTTP 模式
python tools/tdlas_mcp.py --http --host 0.0.0.0 --port 8000 --token <密钥>

# 公网隧道（cloudflared，推荐）
python tools/remote_link.py
```

详见 [mcp.config.example.json](./mcp.config.example.json)。

## 说明

- **无多次扫描平均**：N 次平均仅对白噪声（散粒/热/RIN）按 √N 降，1/f 漂移与粉红噪声不降。模拟时白噪声参数按 1/√N 减小即可。
- 不依赖其他 MCP 服务，可独立部署。

## 文档

| 文件 | 内容 |
|------|------|
| [TECHNICAL.md](./TECHNICAL.md) | 物理原理、算法实现、参数表、已知近似 |
| [SKILL.md](./SKILL.md) | AI 交互规范 |
| [CHANGELOG.md](./CHANGELOG.md) | 版本记录 |

## 引用

```
The TDLAS/WMS instrument-level simulations were performed using tdlas-mcp
(https://github.com/LKF0402/tdlas-mcp, GPLv3).
```

底层数据：HAPI (Kochanov et al., JQSRT 2016, DOI: 10.1016/j.jqsrt.2016.03.005) · HITRAN2024 (Gordon et al., JQSRT 2026, DOI: 10.1016/j.jqsrt.2026.109807)

## License

GPLv3
