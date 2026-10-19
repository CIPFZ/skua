# Skua AI 开发者工作手册（AGENT.md）

> 状态：v0.1，2026-10-15
> 读者：承担 Skua 实现工作的 AI 编码 Agent 与人类审阅者
> 前置：先完整阅读 `SPEC.md`（v0.1）。本手册不重复方案，只规定怎么干活。SPEC 与本手册冲突时以 SPEC 为准。
> 分工：方案、技术选型与全局把关由项目所有者与架构 Agent 负责；本手册的读者只负责实现。实现 Agent 不修改 SPEC 的任何决策，发现问题走第 9 章 ADR 流程。

---

## 1. 一句话原则

**先数据后模型，先离线后在线，先证据 A/B 后证据 C，宁可漏跳广告也不错跳正片。**

---

## 2. 开工前自检（每次会话开始）

1. 读 `SPEC.md` 第 2 章数据模型与当前里程碑（第 5 章）对应的模块章节。
2. 读 `docs/decisions/` 下全部 ADR，最新优先。
3. `git log -n 20 --oneline`；读 `docs/progress.md` 当前里程碑的勾选状态。
4. 确认工具链：`uv --version`、`python --version`（3.11.x）、`ffmpeg -version`（≥ 6.0，含 libavcodec）、`fpcalc -version`（Chromaprint ≥ 1.5）。
5. 确认 `models/` 下所需权重已由 `scripts/fetch_models.sh` 下载并通过 SHA256 校验；缺失则先下载，不要在代码里加运行时自动下载。
6. 确认 `SKUA_WORK_DIR` 指向临时目录（测试框架自动设置），不要把 `work/` 与 `cache/` 写进仓库。

---

## 3. 边界与禁区（必须遵守）

| 允许 | 禁止 |
|---|---|
| 用项目所有者提供的 m3u8 地址与本地视频文件做开发与测试 | 在源码、测试、文档、提交信息中硬编码任何真实站点域名、m3u8 URL、剧名 |
| 把真实分片的结构特征（时长、事件序列、哈希）脱敏后存为 fixture | 把任何真实视频分片、音频、截图提交进仓库 |
| 使用 TransNetV2 / DINOv2 / BEATs / RapidOCR / insightface / Chromaprint 的官方权重与许可 | 引入许可证不允许本地再分发的权重；引入闭源 SDK |
| 参考 Comskip、SponsorBlock、PySceneDetect 的公开设计与文档 | 复制其代码片段；实现任何向第三方服务器上报观看记录的功能 |
| 检测结果与用户反馈写本地 SQLite | 任何网络回传、遥测、崩溃上报 |

真实地址只允许出现在 `.env.local`（gitignore）与项目所有者的本地 `golden/` 标注文件中。`scripts/check_secrets.sh` 扫描 `http(s)://` 字面量与 `.m3u8` 字符串，命中非 `127.0.0.1` / `example.com` 即失败。

---

## 4. 仓库约定

### 4.1 提交

- 所有开发直接提交 `main`，不开分支、不开 PR。
- `main` 始终可用：每个提交独立构建通过、`uv run pytest` 全绿、`ruff` / `mypy` 零错误。做不到就不提交。
- 小步提交：一次提交只做一件事；模型权重版本、数据 schema、代码三类变更分开提交。
- 提交信息：`<scope>: <动词开头的一句话>`，scope 为 `schema` / `adapter` / `shots` / `evidence` / `model` / `train` / `serve` / `mpv` / `docs` / `ci`。示例：`adapter: 实现 EXTINF 的 MAD 离群事件`。
- 推送后必须确认 GitHub Actions 实际触发并通过，不得仅凭本地结果声称 CI 全绿。
- 一次推送只带一个提交：CI 只对推送的最新提交运行，批量推送会让中间提交绕过 CI。本地干净 clone 的验证不能替代 CI。

### 4.2 目录职责速查

| 路径 | 放什么 | 不放什么 |
|---|---|---|
| `skua/adapter/` | 播放列表解析、下载缓存、PyAV 解码、容器与播放列表事件、`Timeline` 构建 | 任何检测逻辑、任何模型调用 |
| `skua/shots/` | TransNetV2 推理、PySceneDetect 兜底、关键帧选取 | 特征提取 |
| `skua/evidence/` | 三路证据各一个模块 + `ood.py`，输入 `Timeline` 与帧/PCM 迭代器，输出 `ShotFeatures` 列 | 决策、阈值、标签判定（`fingerprint.py` 的自匹配规则标签除外） |
| `skua/model/` | 发射模型、Semi-Markov 解码、校准 | 特征提取、IO |
| `skua/train/` | 伪标签、数据集、训练、评测、消融 | 推理服务 |
| `skua/serve/` | HLS 代理、skip list 存取、反馈写入 | 模型逻辑（只调用 `model/` 的推理接口） |
| `skua/cli.py` | `scan` / `train` / `eval` / `serve` 子命令编排 | 业务逻辑 |
| `mpv/` | `skua.lua` | Python |
| `models/` | 下载脚本与 SHA256 清单 | 权重文件本体 |
| `golden/` | 标注 JSON 与标注协议 `PROTOCOL.md` | 视频文件 |
| `tests/` | 每包一个测试目录 + `fixtures/` | 真实媒体文件 |
| `docs/` | `progress.md`、`decisions/`、`reports/` | 代码 |

### 4.3 文件头与注释

- 每个 Python 文件首行 docstring 一句话说明所属包与职责，不写作者日期。
- 注释解释"为什么"而不是"做什么"。涉及 ffmpeg / PyAV / HLS 规范坑点的地方必须留注释并附 RFC 8216 章节号或 issue 链接。
- 所有阈值、窗口、维度数字不得裸写在逻辑里，集中到对应模块的 `Config` dataclass，默认值与 SPEC 一致并注明 SPEC 章节。

---

## 5. Python 编码约定

### 5.1 工程

- `uv` 管理环境与锁文件；`pyproject.toml` 唯一依赖声明处；禁止 `requirements.txt`。
- 新增第三方依赖需 ADR（第 9 章），并写明许可证与是否带原生扩展。
- `ruff` 使用仓库 `ruff.toml`（行宽 100，启用 `E/F/I/B/UP/N/SIM`）；`mypy --strict` 对 `skua/` 全部通过，第三方无 stub 的包在 `pyproject.toml` 的 `[[tool.mypy.overrides]]` 显式 `ignore_missing_imports`，不允许文件级 `# type: ignore`。
- 公开函数必须有类型标注与 docstring；内部函数可省 docstring 不可省标注。

### 5.2 数据与 IO

- 模块间只传 SPEC 第 2 章的 dataclass，序列化统一经 `skua/schema.py` 的 `to_json` / `from_json`，字段变更必须同时 bump `SCHEMA_VERSION` 并写迁移。
- `ShotFeatures` 落 parquet，列名与 dtype 在 `skua/schema.py` 的 `FEATURE_SCHEMA` 中定义，写入前校验。
- 时间一律浮点秒，基于 `Timeline` 统一轴；禁止在 adapter 之外出现 PTS、time_base、分片序号。
- 所有路径用 `pathlib.Path`；所有可能大的中间结果写 `SKUA_WORK_DIR/<media_id>/`，不常驻内存。

### 5.3 解码与外部进程

- 解码只允许在 `skua/adapter/decode.py`，对外暴露 `iter_frames(ts_list | fps, size)` 与 `iter_pcm(sr=16000, mono=True)` 两个生成器。其他模块不得 `import av` 或启动 ffmpeg。
- 外部进程只允许 `ffmpeg`、`ffprobe`、`fpcalc`，通过 `skua/adapter/proc.py` 的 `run(cmd: AllowedCommand, args: list[str])` 调用；禁止 `shell=True`，禁止字符串拼接命令。
- 外部进程输出解析写成纯函数，真实输出脱敏后存为 `tests/fixtures/proc/` 样例。

### 5.4 模型推理

- 所有模型通过 `skua/models_registry.py` 加载，键为 SPEC 中的模型名；注册表记录权重文件名、SHA256、输入规格、许可证。
- 推理默认 CPU、`torch.inference_mode()`、固定线程数（`SKUA_THREADS`，默认物理核数）；不写任何 CUDA 分支（M5 之前）。
- 同一输入在同一机器上输出必须可复现：固定 seed，关闭非确定性算子；golden 比对允许 cosine ≥ 0.999。
- Embedding 维度、归一化方式写在注册表里，特征模块从注册表读，不自行假设。

### 5.5 并发

- 下载与解码用 `asyncio` + `concurrent.futures.ThreadPoolExecutor`；CPU 密集特征提取用 `ProcessPoolExecutor`，每进程独立加载模型。
- 任何长任务必须可取消并支持断点：以 `media_id/shot_idx` 为幂等键，重跑跳过已完成项。

### 5.6 日志

- `logging.getLogger("skua.<包名>")`，结构化字段用 `extra=`。
- 记录 `media_id`、分片序号、镜头序号、耗时；永不记录 URL 全文（只记 host 哈希）、永不记录 OCR 文本。
- 决策层对每个 `Segment` 必须写 `evidence` 字段（各路证据贡献），这是用户申诉与消融的依据。

---

## 6. 测试规范

### 6.1 Fixture 优先

- 测试不得访问网络、不得依赖真实媒体。`tests/fixtures/synth/` 下用 ffmpeg 合成测试视频：彩条 + 正弦音做"正片"，纯色闪烁 + 白噪声做"广告"，`scripts/make_synth.py` 生成，参数化时长、分辨率、是否插入 `DISCONTINUITY`。
- 合成素材的视频编码器默认 `mpeg2video`（所有 ffmpeg 构建自带，支持 B 帧，MPEG-2 in TS 是标准组合），音频 ffmpeg 内置 `aac`。基础测试集在任何环境下使用同一编码器、断言同一结果，不按环境切换断言。
- H.264 特有的检查（profile / level / SPS 解析、`video_params` 对 H.264 字段的比较）单独成组，打 `@pytest.mark.h264`，`libx264` 不可用时跳过；CI 必须安装 libx264 并执行该组，本地跳过不算失败。
- 编码器可用性必须通过实际编码一小段来探测，不能只看 `ffmpeg -encoders` 列表（dlopen 构建会列出但运行时缺库）。探测结果写入合成产物旁的 `manifest.json` 并打印到 stderr。
- 合成产物目录以 `make_synth.py` 的内容哈希 + 布局参数作为键，脚本一变旧目录自动失效；测试不得读取未经当前脚本生成的残留目录。
- 不在本机私装 ffmpeg、编码器库或任何外部二进制来凑测试环境；环境差异一律通过标记与跳过表达。
- 合成 HLS：`make_synth.py` 同时产出 m3u8 与分片，由 `tests/conftest.py` 用本地 `aiohttp` 服务器在 `127.0.0.1` 随机端口托管。
- 模型相关测试默认用 `FakeEmbedder`（确定性哈希到向量）；带真实权重的测试标记 `@pytest.mark.model`，CI 默认跳过，本地 `uv run pytest -m model` 跑。

### 6.2 必写测试

| 变更类型 | 必写 |
|---|---|
| 新播放列表 / 容器事件 | 合成 HLS 正例 + 不触发的反例 |
| 时间轴重建 | 含 `DISCONTINUITY` 与 PTS 回绕的合成流。真值由 PyAV 读出的原始 PTS 经独立纯函数计算得到（不经 timeline.py），断言镜头时间一致（±1 帧，按模 `2^33` 比较）；ffprobe 只做一次交叉校验，不作为 CI 真值，避免 ffprobe 版本差异成为测试噪声。ffprobe 即使加 `-correct_ts_overflow 0` 在回绕点附近仍会把个别包显示成 `2^33 + x`，样例见 `tests/fixtures/proc/ffprobe_segment_wrap.json` |
| 指纹匹配 | 同素材转码（码率减半、重缩放、响度 +6 dB）后仍命中；不同素材不命中 |
| 自匹配规则 | 构造三部合成"剧"，断言 ad / intro / outro / recap / trailer 五类标签各至少一例正确 |
| 发射模型 | 固定 seed 过拟合 10 条样本至 loss < 0.05（管线正确性）；输入特征缺失（OCR / 人脸 mask）不崩 |
| Semi-Markov 解码 | 手工构造发射矩阵的 golden 区间；违反位置约束的标签不出现；`content` 最短 15 s 生效 |
| 校准与策略 | 三档阈值边界用例 |
| 代理 | 改写后的 m3u8 用 `m3u8` 库可解析，分片数与 `DISCONTINUITY` 位置符合预期 |
| mpv 脚本 | 用 `lua` 解释器跑 `mpv/tests/`，mock `mp.get_property` |

### 6.3 真实数据验证

- 真实 m3u8 只在项目所有者本机跑，命令与结果摘要（P/R、耗时、无 URL）写入 `docs/reports/m<N>-<date>.md`。
- 每个里程碑的消融表（SPEC 3.7）必须附在该里程碑的报告中。

---

## 7. 每次推送前的检查清单

```
- [ ] uv run ruff check . && uv run ruff format --check . 通过
- [ ] uv run mypy skua 零错误
- [ ] uv run pytest 全绿；新增代码有对应测试；-m model 本地已跑
- [ ] scripts/check_secrets.sh 与 scripts/check_imports.sh（adapter 之外无 av / subprocess）通过
- [ ] 新阈值 / 维度已进 Config 并注明 SPEC 章节
- [ ] schema 变更已 bump SCHEMA_VERSION 并有迁移测试
- [ ] 偏离 SPEC 的地方已有 ADR
- [ ] docs/progress.md 已勾选对应条目
- [ ] 推送后 GitHub Actions 已实际触发并通过
```

---

## 8. 汇报与停顿规则

- 一次只做 `docs/progress.md` 里一到三个相邻条目，做完汇报并停下等确认，不一口气推进整个里程碑。
- 汇报格式固定四段：做了什么 / 测试结果 / 偏离或疑问 / 下一步建议。
- SPEC 没写清楚的地方：列出选项、各自代价、你的倾向，然后停下来问；不要自行猜测后继续。
- 任何涉及"是否错跳正片"的阈值、权重、先验调整，一律不得自行修改，必须汇报并等待决定。

---

## 9. ADR 写法

文件名 `docs/decisions/ADR-NNN-<slug>.md`，章节固定：背景 / 决策 / 理由 / 代价 / 影响。一个 ADR 一个决定。触发条件：新增第三方依赖或模型权重、修改第 2 章数据模型、修改标签集或时长先验、修改跳过策略阈值、新增 `AllowedCommand`、任何 SPEC 中的选型需要替换。ADR 由实现 Agent 起草，由项目所有者批准后才能改 SPEC 与代码。

---

## 10. M0 启动清单

M0 目标：HLS 适配器跑通，`Timeline` 与证据 A 产出区间，所有质量门就位。验收标准见 SPEC 第 5 章。

### 10.1 工程骨架

- [ ] `pyproject.toml`：包名 `skua`，Python 3.11，依赖 `m3u8`、`av`、`aiohttp`、`numpy`、`pyarrow`、`typer`；dev 依赖 `pytest`、`pytest-asyncio`、`ruff`、`mypy`。不引入 pydantic。
- [ ] `ruff.toml`、`mypy` 配置、`.gitignore`（`work/`、`cache/`、`models/*.onnx`、`models/*.pt`、`.env.local`）。
- [ ] `scripts/fetch_models.sh`（M0 只需脚本框架与 SHA256 清单格式）、`scripts/check_secrets.sh`、`scripts/check_imports.sh`、`scripts/make_synth.py`。
- [ ] `docs/progress.md` 按里程碑列勾选表；`docs/decisions/ADR-000-template.md`。
- [ ] `.github/workflows/ci.yml`：ubuntu 安装 ffmpeg + chromaprint，步骤 ruff → mypy → pytest → 两个检查脚本。

### 10.2 schema

- [ ] `skua/schema.py`：SPEC 第 2 章全部 dataclass，`SCHEMA_VERSION = 1`，`to_json` / `from_json`，`FEATURE_SCHEMA`（M0 只需 `struct` 列）。
- [ ] 测试：round-trip 等价；未知字段报错而非静默丢弃。

### 10.3 adapter

- [ ] `playlist.py`：解析 master / media playlist，选最高码率变体，产出 `PlaylistEvent` 五种（SPEC 3.1）；`uri_pattern_break` 的签名函数单独可测。
- [ ] `fetch.py`：并发 8 下载，落盘 `cache/<media_id>/segN.ts`，断点续传，`media_id` 计算函数。
- [ ] `decode.py`：PyAV 打开分片序列，`iter_frames` / `iter_pcm` 生成器，逐包比较 codec 参数产出 `ContainerEvent`。
- [ ] `timeline.py`：EXTINF 累加 + 首帧 PTS 校正，`DISCONTINUITY` 处不断轴并记 `pts_jump`；M0 的 `shots` 退化为"每分片一个镜头"（真正的镜头切分在 M2）。
- [ ] `proc.py`：`AllowedCommand` 枚举（M0 含 `ffprobe`）与 `run`。
- [ ] 测试：合成 HLS 含 3 段正片 + 1 段广告（不同分辨率 + `DISCONTINUITY`），断言事件序列与时间轴。

### 10.4 evidence/structural

- [ ] SPEC 3.3 的 12 维特征与硬先验判定，输出 `ShotFeatures.struct` 列与 `hard_prior: dict[shot_idx, float]`。
- [ ] M0 临时决策器 `rule_decoder.py`：仅依据硬先验合并相邻镜头为 `Segment(label="ad", confidence=0.95)`，M3 由 Semi-Markov 替换；文件头注明"M3 删除"。

### 10.5 cli 与 serve 最小集

- [ ] `skua scan <url|path> --out skiplist.json`：跑 adapter → structural → rule_decoder，输出 `SkipList`，耗时打印到 stderr。
- [ ] `skua scan --json-events`：把 `Timeline` 事件完整导出，供人工核对。
- [ ] `serve/skiplist.py`：SQLite 存取 `SkipList`，表结构带 `schema_version`。
- [ ] mpv 脚本与代理推迟到 M1。

### 10.6 M0 验收

- [ ] 干净 clone 后 `uv sync && uv run pytest` 全绿。
- [ ] 合成 HLS 上 `skua scan` 输出的广告区间与注入位置误差 ≤ 一个分片。
- [ ] 项目所有者在本机对 golden 的"整分片广告"子集跑 `skua scan`，P ≥ 0.95、R ≥ 0.7、单集 < 10 s，结果写 `docs/reports/m0-<date>.md`。
- [ ] 两个检查脚本在故意违规的提交上各失败一次。
- [ ] `docs/progress.md` M0 全部勾选，`git tag m0`。

---

## 11. M1 之后的节奏

- 每个里程碑开始前，先把 SPEC 对应模块拆成 `docs/progress.md` 勾选条目，经项目所有者确认后再动代码。
- M1 的指纹库先做视频 pHash 再做 Chromaprint，两者各一个 PR；自匹配规则单独一个 PR 并附三部合成剧的测试。
- M2 的模型接入顺序：TransNetV2 → DINOv2 → BEATs → OOD → OCR / 人脸，每接入一个模型先过 `-m model` 的复现测试，再写特征列。
- M3 之前不得调整任何与跳过策略相关的阈值；M3 的消融报告是进入 M4 的前提。
- 每个里程碑结束打 tag `m<N>`，`docs/progress.md` 写不超过 10 行回顾：做了什么、什么没按计划、下个里程碑要先处理什么。
