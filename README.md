# Skua

<p align="center"><img src="assets/icon/pack/skua-icon-256.png" width="128" alt="Skua"></p>

视频广告区间检测与跳过。输入任意可解码的视频源（v1 为 HLS/m3u8），输出带置信度的时间区间列表，供播放器或代理按策略跳过。

以镜头为基本单元，融合三路独立证据（播放列表与容器结构信号、跨集重复性指纹、视觉与音频内容理解），由带时长先验的 Semi-Markov 解码器产出区间，并通过指纹库与用户反馈形成自监督闭环。

## 文档

| 文件 | 读者 | 内容 |
|---|---|---|
| [SPEC.md](SPEC.md) | 所有人 | 实现方案：数据模型、模块与算法、训练与评测协议、里程碑与验收 |
| [AGENT.md](AGENT.md) | 实现 Agent | 工作手册：边界禁区、编码与测试约定、PR 清单、ADR 流程、M0 启动清单 |
| [AGENT_KICKOFF.md](AGENT_KICKOFF.md) | 项目所有者 | 交给实现 Agent 的首次启动与续作 prompt |
| [CLAUDE.md](CLAUDE.md) | 实现 Agent | 入口与铁律摘要 |
| [assets/icon/README.md](assets/icon/README.md) | 所有人 | 图标资产与色板 |

## 状态

方案阶段，尚无代码。实现从 M0（HLS 时间轴与结构信号）开始，见 SPEC 第 5 章。

## 原则

宁可漏跳广告，也不错跳正片。不做任何网络回传。
