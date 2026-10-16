# Skua 开发进度

更新时间：2026-10-16
当前里程碑：M0 时间轴与证据 A
CI 状态：绿。GitHub Actions `ci` Run 1–3（65d903c、2bd86ae、4a5a4bf）均 completed successfully，Run 1 的 14 个步骤全部 success（含 apt 安装 ffmpeg + chromaprint）

## M0 条目（对应 AGENT.md 第 10 章，按批次推进；直接提交 main，批次名只用于分组）

### 批次 1 骨架（10.1）✅ 项目所有者 2026-10-16 验收
- [x] 10.1a 工程配置：`pyproject.toml`（依赖、构建后端、mypy / pytest 配置）、`ruff.toml`、`.gitignore`、`LICENSE`（MIT）、ADR-000 模板、ADR-001、本文件
- [x] 10.1b 脚本：`check_secrets.sh`、`check_imports.sh`、`selftest_checks.sh`、`fetch_models.sh` 框架加 `models/manifest.tsv` 格式
- [x] 10.1c CI：`.github/workflows/ci.yml`
- [x] 10.1-gate：ADR-001 已批准（2026-10-16）→ `uv sync` → 提交 `uv.lock` → ruff / mypy / pytest 全绿

### 批次 2 schema（10.2）
- [ ] 10.2 `skua/schema.py`：第 2 章全部 dataclass、`SCHEMA_VERSION = 1`、`to_json` / `from_json`、`FEATURE_SCHEMA`（只含 struct 列）、round-trip 测试与未知字段测试
  - 必须覆盖：ShotFeatures 非 struct 字段为 None 的往返，以及 None 与缺字段两种输入行为一致；顶层 `schema_version` 与 `SkipList.version` 分别测；golden 顶层 `annotator` / `annotated_at` 被接受而不报未知字段

### 批次 3 proc / synth / playlist（10.3、10.1d）
- [ ] 10.3a `adapter/proc.py`：`AllowedCommand`（FFMPEG、FFPROBE）与 `run`
- [ ] 10.1d `scripts/make_synth.py`
- [ ] 10.3b `adapter/playlist.py`：五种 PlaylistEvent 与签名函数

### 批次 4 fetch / decode（10.3）
- [ ] 10.3c `adapter/fetch.py`：并发 8、`.part` 断点、重试、`media_id`
- [ ] 10.3d `adapter/decode.py`：`probe_segment` / `iter_frames` / `iter_pcm` 与容器事件；`tests/conftest.py` 本地 HLS 服务器

### 批次 5 timeline（10.3）
- [ ] 10.3e `adapter/timeline.py` 与 `adapter/hls.py` 门面
- [ ] 10.3f 合成 HLS 事件序列 / 时间轴 / PTS 回绕测试，与 ffprobe 真值对比误差在 ±1 帧内

### 批次 6 structural（10.4）
- [ ] 10.4a `evidence/structural.py`：10 维 struct 与块级 `hard_prior`
- [ ] 10.4b `evidence/rule_decoder.py`（M3 删除）

### 批次 7 cli / serve（10.5）
- [ ] 10.5a `cli.py`：`skua scan` 的 `--out` 与 `--json-events`
- [ ] 10.5b `serve/skiplist.py`：SQLite 存取
- [ ] 10.5c 端到端测试：区间误差 ≤ 一个分片

### 批次 8 验收（10.6）
- [ ] 10.6a `golden/PROTOCOL.md` 与最小 `skua eval`
- [ ] 10.6b 干净 clone 后 `uv sync && uv run pytest` 全绿；两个检查脚本在故意违规提交上各失败一次（由 `selftest_checks.sh` 覆盖）
- [ ] 10.6c `docs/reports/m0-<date>.md` 与项目所有者本机验证命令；`git tag m0`

## 进行中
- 批次 2 schema：待开始

## 已知问题
- 本机没有 `fpcalc`，M0 用不到，M1 开工前需要安装 chromaprint。CI 已安装
- 本机 ffmpeg 7.0.2 只有 libopenh264 编码器，CI（ubuntu-24.04 apt）为 libx264。`make_synth.py` 需要探测可用编码器（批次 3），且两种编码器都要在测试中实际跑过：本机跑 libopenh264，CI 跑 libx264
- PyAV 18.x 捆绑 FFmpeg 8.x，系统 ffprobe 是 7.x，±1 帧对比可能存在系统偏差（M0 风险 7，ADR-001）
- `check_imports` 只防误用不防恶意：`getattr(os, "system")` 一类反射调用抓不到（项目所有者 2026-10-16 接受）

## 开放问题（需项目所有者决策）
- golden 来源是否有 fMP4、是否需要请求头：项目所有者稍后答复

## ADR 列表
- ADR-000 模板
- ADR-001 M0 基线依赖（2026-10-16 批准）

## 实现决策记录（非策略参数，自行决定）
- `check_secrets` 用 Python 标准库实现，git 调用放在 shell 外壳中，保证 Python 部分不用 subprocess
- `check_secrets` 中由占位符组成的 host（`{host}`、`${HOST}`）视为模板，不算字面量
- 测试里需要"假真实地址"时，在运行时拼接字符串，保证源码中没有字面量
- `.env.local` 中 URL 的 host 及其可注册域（public suffix + 1，如 a.b.example.com → example.com）、`SKUA_DENYLIST` 中长度 ≥ 3 的词，都会被反查；命中时只报编号，不回显内容（项目所有者 2026-10-16 批准）
- public suffix 判定用内置的多级后缀子集（`MULTI_LABEL_SUFFIXES`，含 com.cn、co.uk、github.io、cloudfront.net 等），不引入 PSL 依赖；遇到未收录的共享托管后缀时补进该集合
- `ruff format` 排除 `*.md`（SPEC/AGENT 中的代码块是说明文字）和 `assets/icon/make_pack.py`（项目所有者提供的图标工具，保留对齐排版，仍然 lint）
- mypy override：`m3u8`、`pyarrow` 无 `py.typed`；`av` 自带类型，严格检查
- pytest 使用 `--import-mode=importlib`，`pythonpath = ["scripts"]`，检查脚本的纯函数可以直接单测
- `mypy` 在 CI 中同时检查 `skua` 与 `scripts`

## 性能记录
- 合成 HLS 45 分钟等效，`skua scan` 耗时：待批次 7 测量

## 下次会话从这里继续
- 工作流程：直接提交 main（AGENT.md 4.1）；推送前过第 7 章清单；推送后用 Actions 页面确认运行通过再汇报；汇报前先 `git ls-remote origin` 确认远端事实状态
- 本机没有 `gh`，匿名 API 常被限流；改为读取 Actions 网页（`/actions/workflows/ci.yml?query=branch%3Amain` 的 aria-label）确认结果
- 下一步：批次 2 schema（10.2）
