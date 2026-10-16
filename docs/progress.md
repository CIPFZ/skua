# Skua 开发进度

更新时间：2026-10-16
当前里程碑：M0 时间轴与证据 A
CI 状态：本地质量门全绿（批次 1 合并时）；GitHub Actions 首次运行待推送后确认

## M0 条目（对应 AGENT.md 第 10 章，按批次推进）

### 批次 1 `m0/skeleton`（10.1）
- [x] 10.1a 工程配置：`pyproject.toml`（依赖、构建后端、mypy / pytest 配置）、`ruff.toml`、`.gitignore`、`LICENSE`（MIT）、ADR-000 模板、ADR-001、本文件
- [x] 10.1b 脚本：`check_secrets.sh`、`check_imports.sh`、`selftest_checks.sh`、`fetch_models.sh` 框架加 `models/manifest.tsv` 格式
- [x] 10.1c CI：`.github/workflows/ci.yml`
- [x] 10.1-gate：ADR-001 已批准（2026-10-16）→ `uv sync` → 提交 `uv.lock` → ruff / mypy / pytest 全绿

### 批次 2 `m0/schema`（10.2）
- [ ] 10.2 `skua/schema.py`：第 2 章全部 dataclass、`SCHEMA_VERSION = 1`、`to_json` / `from_json`、`FEATURE_SCHEMA`（只含 struct 列）、round-trip 测试与未知字段测试

### 批次 3 `m0/proc-synth-playlist`（10.3、10.1d）
- [ ] 10.3a `adapter/proc.py`：`AllowedCommand`（FFMPEG、FFPROBE）与 `run`
- [ ] 10.1d `scripts/make_synth.py`
- [ ] 10.3b `adapter/playlist.py`：五种 PlaylistEvent 与签名函数

### 批次 4 `m0/fetch-decode`（10.3）
- [ ] 10.3c `adapter/fetch.py`：并发 8、`.part` 断点、重试、`media_id`
- [ ] 10.3d `adapter/decode.py`：`probe_segment` / `iter_frames` / `iter_pcm` 与容器事件；`tests/conftest.py` 本地 HLS 服务器

### 批次 5 `m0/timeline`（10.3）
- [ ] 10.3e `adapter/timeline.py` 与 `adapter/hls.py` 门面
- [ ] 10.3f 合成 HLS 事件序列 / 时间轴 / PTS 回绕测试，与 ffprobe 真值对比误差在 ±1 帧内

### 批次 6 `m0/structural`（10.4）
- [ ] 10.4a `evidence/structural.py`：10 维 struct 与块级 `hard_prior`
- [ ] 10.4b `evidence/rule_decoder.py`（M3 删除）

### 批次 7 `m0/cli-serve`（10.5）
- [ ] 10.5a `cli.py`：`skua scan` 的 `--out` 与 `--json-events`
- [ ] 10.5b `serve/skiplist.py`：SQLite 存取
- [ ] 10.5c 端到端测试：区间误差 ≤ 一个分片

### 批次 8 `m0/acceptance`（10.6）
- [ ] 10.6a `golden/PROTOCOL.md` 与最小 `skua eval`
- [ ] 10.6b 干净 clone 后 `uv sync && uv run pytest` 全绿；两个检查脚本在故意违规提交上各失败一次（由 `selftest_checks.sh` 覆盖）
- [ ] 10.6c `docs/reports/m0-<date>.md` 与项目所有者本机验证命令；`git tag m0`

## 进行中
- 批次 2 `m0/schema`：待开始

## 已知问题
- 本机没有 `fpcalc`，M0 用不到，M1 开工前需要安装 chromaprint。CI 已安装
- 本机 ffmpeg 7.0.2 只有 libopenh264 编码器，没有 libx264。`make_synth.py` 需要探测可用编码器（批次 3）
- PyAV 18.x 捆绑 FFmpeg 8.x，系统 ffprobe 是 7.x，±1 帧对比可能存在系统偏差（M0 风险 7，ADR-001）
- `check_imports` 只防误用不防恶意：`getattr(os, "system")` 一类反射调用抓不到（项目所有者 2026-10-16 接受）

## 开放问题（需项目所有者决策）
- golden 来源是否有 fMP4、是否需要请求头：项目所有者稍后答复
- `LICENSE` 中著作权人仍是占位「<填这里>」，由项目所有者填写

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
- 从 main 新建 `m0/schema`，实现批次 2（10.2）
