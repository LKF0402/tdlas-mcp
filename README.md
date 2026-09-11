<div align="center">

<h1>tdlas-mcp</h1>

<p><b>TDLAS/WMS 光谱仿真 MCP 服务器</b><br>
波长调制光谱 · 谐波分析 · 锁相检测 · 自然语言驱动</p>

<a href="LICENSE"><img src="https://img.shields.io/badge/License-GPLv3-blue.svg" alt="GPLv3"></a>
<img src="https://img.shields.io/badge/Python-3.10%2B-3776AB" alt="Python">
<img src="https://img.shields.io/badge/MCP-stdio%20JSON--RPC-6B8FD4" alt="MCP">

</div>

[English](./README.en.md) | 中文

---

## 项目简介

`tdlas-mcp` 是 TDLAS（可调谐二极管激光吸收光谱）技术的仿真 MCP 服务器，支持：

- **DAS**（直接吸收光谱）—— 基于 HITRAN 数据库的吸收谱计算
- **WMS**（波长调制光谱）—— 波长扫描 + 谐波提取 + 锁相检测仿真

通过 MCP 协议接入 AI 助手，用自然语言描述实验参数即可完成仿真。

> 💡 本项目依赖 [hitran-mcp](https://github.com/LKF0402/hitran-mcp) 的 HITRAN 线表获取与吸收谱计算引擎。

## 功能规划

| 模块 | 状态 | 说明 |
|------|------|------|
| 直接吸收光谱（DAS） | 🔲 待开发 | 基于 HITRAN 的吸收系数 / 透过率计算 |
| 波长扫描模型 | 🔲 待开发 | 三角波 / 正弦波波长扫描 |
| 调制深度 / 调制频率 | 🔲 待开发 | WMS 参数配置 |
| 数字锁相放大器 | 🔲 待开发 | 参考信号生成 + 低通滤波 + 解调 |
| **2f 谐波提取** | 🔲 待开发 | **二次谐波信号仿真（核心）** |
| 1f 谐波提取 | 🔲 待开发 | 一次谐波信号（用于背景归一化） |
| 免定标 WMS 模型 | 🔲 待开发 | calibration-free WMS |
| 浓度反演 | 🔲 待开发 | 从谐波信号反演气体浓度 |

## 快速开始

> 🚧 开发中，暂不可用

## 许可证

GPLv3

## References

- https://github.com/LKF0402/hitran-mcp
- https://github.com/hitranonline/hapi
- https://github.com/hitranonline/hapi2
