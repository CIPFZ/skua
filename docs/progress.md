# Skua 开发进度

更新时间：2026-10-19（SPEC v0.3）
当前里程碑：M0 时间轴与证据 A
CI 状态：绿至 f9c78d4。自 AGENT.md 4.1 起一次推送一个提交，每次推送确认对应 Run 通过

## M0 条目（对应 AGENT.md 第 10 章，按批次推进；直接提交 main，批次名只用于分组）

### 批次 1 骨架（10.1）✅ 项目所有者 2026-10-16 验收
- [x] 10.1a 工程配置：`pyproject.toml`（依赖、构建后端、mypy / pytest 配置）、`ruff.toml`、`.gitignore`、`LICENSE`（MIT）、ADR-000 模板、ADR-001、本文件
- [x] 10.1b 脚本：`check_secrets.sh`、`check_imports.sh`、`selftest_checks.sh`、`fetch_models.sh` 框架加 `models/manifest.tsv` 格式
- [x] 10.1c CI：`.github/workflows/ci.yml`
- [x] 10.1-gate：ADR-001 已批准（2026-10-16）→ `uv sync` → 提交 `uv.lock` → ruff / mypy / pytest 全绿

### 批次 2 schema（10.2）✅ 项目所有者 2026-10-16 验收
- [x] 10.2 `skua/schema.py`：第 2 章全部 dataclass、`SCHEMA_VERSION = 1`、`to_json` / `from_json`、`FEATURE_SCHEMA`（只含 struct 列）、round-trip 测试与未知字段测试
  - 必须覆盖：ShotFeatures 非 struct 字段为 None 的往返，以及 None 与缺字段两种输入行为一致；顶层 `schema_version` 与 `SkipList.version` 分别测；golden 顶层 `annotator` / `annotated_at` 被接受而不报未知字段

### 批次 3 proc / synth / playlist（10.3、10.1d）
- [x] 10.3a `adapter/proc.py`：`AllowedCommand`（FFMPEG、FFPROBE）与 `run`；参数只收 list[str]、stdin 关闭、超时强杀、错误信息与日志不含 URL（`redact_urls` / `host_hash`）
- [x] 10.1d `scripts/make_synth.py`：mpeg2video + aac 合成 C1 | D C2 | D AD | D C3（AD 换分辨率 / PID / URI 模板，C3 内发生 33 位回绕），另有 same-uri 反例与 tiny 布局；试编码探测结果打印到 stderr 并写入 `manifest.json`；`@pytest.mark.h264` 组用 libx264 断言同样的结构，CI 设 `SKUA_REQUIRE_H264=1`，该组若被跳过即判失败；CI 另有一步单独跑 `-m h264` 并用 junit 断言 tests>0、skipped=0，结果写入 job summary（匿名访问看不到 Actions 日志，所以由步骤成功本身证明该组执行过）
- [x] 10.3b `adapter/playlist.py`：选最高 BANDWIDTH 变体，五种 PlaylistEvent，`uri_signature` / `filename_template` / `robust_z` 单独可测；每种事件都有合成 HLS 正例与不触发的反例（key_change 例外：make_synth 不加密，正反例用手写播放列表，AES 合成随批次 4 decode 补上）

### 批次 4 fetch / decode（10.3）
- [x] 10.3c0 `make_synth.py` 的 `ad-break-aes` 布局：C1、C2 共用 k1（显式 IV，C2 跨 DISCONTINUITY 不触发 key_change），广告明文，C3 换 k2（隐式 IV = 媒体序号，RFC 8216 5.2）；key_change 有了合成正例与反例
- [x] 10.3c `adapter/fetch.py`：并发 8、`.part` 断点（HTTP Range 续传）、重试 3 次指数退避、`media_id`；`tests/conftest.py` 的本地 HLS 服务器（127.0.0.1 随机端口，可注入 503 / 404 / 响应体截断 / 不支持 Range，并记录并发数）
- [x] 10.3d `adapter/decode.py`：`probe_segment`（只 demux）/ `param_changes`（video_params、audio_params、pid_change）/ `iter_frames` / `iter_pcm`，支持 AES-128（`crypto:` 协议）；`bitrate_outlier` 需要 EXTINF 与字节数，随批次 5 的 timeline 实现

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
- 批次 4 全部完成（AES 合成布局、fetch.py、decode.py），待项目所有者验收

## 已知问题
- 本机没有 `fpcalc`，M0 用不到，M1 开工前需要安装 chromaprint。CI 已安装
- 本机 ffmpeg 7.0.2 是 `--enable-libopenh264-dlopen` 构建：`-encoders` 列出 libopenh264，但运行时缺 `libopenh264.so.7`，实际无法编码；本机也没有 libx264；TencentOS 4 的 dnf 源中没有 openh264 包。本机实测可用的视频编码器只有 mpeg2video、mpeg4、libaom-av1、libsvtav1。结论：编码器探测必须靠试编码，不能只读 `-encoders` 列表
- ffprobe（7.0.2）加 `-correct_ts_overflow 0` 后，回绕点附近个别包仍被展开成 2^33 + x，其后的包又是原始值：ffprobe 的时间戳不是一致的原始 33 位值，批次 5 拿 ffprobe 当真值时必须按模 2^33 比较（样例见 `tests/fixtures/proc/ffprobe_segment_wrap.json`）
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
- schema：JSON 严格解码（未知字段、重复键、NaN/Infinity、bool 当数字、字符串当数字、Literal 大小写不符一律报错）；`to_json` 写出前再解码一次，防止构造后被改成非法状态的对象落盘
- schema：`ShotFeatures` 可选字段缺失与显式 null 解码结果一致，写出时一律写显式 null；`struct` 必填且不可为 null
- schema：顶层 `schema_version` 每个文档必填，不在 dataclass 中；`decode()` 返回 `Document(value, schema_version, golden)`，`from_json()` 只返回值。迁移表 `MIGRATIONS[n]` 将 n 升到 n+1，缺一步即报错，比当前新的版本拒绝
- schema：golden 元数据 `annotator`（非空）与 `annotated_at`（ISO 8601）必须成对出现，只允许出现在 SkipList 上；golden 文件中所有 segment 必须 confidence=1.0、evidence 为空（SPEC 2）
- schema：SkipList 拒绝 `content` 区间（SPEC 2 v0.2：content 是隐含补集，不落盘），`Segment` 本身仍允许 6 类标签。**未升 SCHEMA_VERSION，唯一理由是 v1 格式尚未被任何非测试代码写到磁盘**；以后同类"只是收紧校验"的改动，只要已有落盘文件就必须升版本并写迁移（项目所有者 2026-10-19 决定）
- playlist：`key_change` 以第一个分片的加密状态为基线（首个 KEY 不算变化），之后任何变化都算，包括清流与加密之间的切换；payload 只带 `method`，不带 key URI
- playlist：robust z 用标准定义 `(x - median) / (1.4826 × MAD)`，1.4826 是使 MAD 在正态下等于 σ 的一致性常数，不是调参项；MAD = 0 时按 SPEC 退回 `1.2533 × mean|x - median|`
- playlist（SPEC v0.3）：文件名按 `-` `_` `.` 切词；整词 ≥ 8 位 hex → `#`（不要求含数字，`deadbeef` 也算）；整词 ≥ 16 位、只含 base64 字符（不含 `/`）且大小写都有 → `#`；其余词只替换数字串。`hex_word_min_len` / `base64_word_min_len` 进 `PlaylistConfig`
- playlist：URI 签名中的 host 只以 8 位哈希出现，目录名只计深度、不参与模板；本地路径的 host 记为 `local`
- playlist：除 SPEC 列出的 fMP4、SAMPLE-AES 外，以下输入也直接报错：无 EXT-X-ENDLIST（直播，SPEC 0 不做）、EXT-X-BYTERANGE、未知 KEY METHOD、非正 EXTINF、无分片
- playlist（SPEC v0.3）：DATERANGE 有 PDT 时按 START-DATE 经 PDT 映射到分片（m3u8 库的 `current_program_date_time` 沿分片累加，遇新 PDT 重置），标签位置不作数；整个列表无 PDT 才按标签所挂分片锚定。payload 新增 `anchored_by`（program-date-time / position）与 `duration_source`（DURATION / END-DATE / PLANNED-DURATION / null）
- playlist：以下 DATERANGE 告警后跳过，不产出事件（宁可漏报）：START-DATE 映射不到本列表任何分片；START-DATE 无时区偏移；首个 PDT 之前的分片不可映射（不向前外推）。DURATION=0 视为时间点，只覆盖包含 START-DATE 的分片
- playlist：事件按 (ts, 种类) 排序，满足 Timeline 的有序要求；DATERANGE 对每个被覆盖的分片各发一个事件，`unit` 为该分片
- 合成素材：`ad-break-marked` 布局（10 s 广告，4 + 4 + 2 s，带 PDT 与 DATERANGE CLASS="ad"）提供 daterange 与 duration_outlier 的合成正例；DATERANGE 标签放在列表头部（挂在 seg000 上），START-DATE 指向广告，测试同时验证去掉 PDT 后会退化到 seg000–002
- decode：PyAV 打开时一律加 `correct_ts_overflow=0` 读原始时间戳。libav 默认的溢出校正会把回绕前的包报成负数，而且随 libav 版本变化；分片内回绕由 `unwrap`（取模 2^33 后与锚点最近的代表）展开，跨分片的展开留给 timeline。已验证：PyAV 18.1（捆绑 FFmpeg 8）的原始值与系统 ffprobe 7.0.2 的值模 2^33 全部一致，包括 AES 解密后的分片
- decode：分片的 `first_pts` 取参考流（首个视频流，没有则音频流）所有包里最早的展示时间，不是第一个包的 pts（open GOP 的前导 B 帧先显示）；合成素材是 closed GOP，所以这一点由 `pts_extent` 的单元测试覆盖
- decode：`video_params` 比较 codec / width / height / profile / level / pix_fmt / 平均帧率，`audio_params` 比较 codec / sample_rate / channels / profile（SPEC 3.1，不比 extradata）；`pid_change` 比较 (PID, 媒体类型) 集合。payload 带 changed、before、after，不带 unit（由 timeline 补）
- decode：`iter_pcm` 每个分片以首个音频帧的 pts 定位，分片内时间按已输出的样本数累加，不依赖重采样器的时间戳
- fetch：`media_id` 规范化时同时丢弃 URL 中的 userinfo（凭据不应影响内容身份）；本地文件取 `os.path.abspath`（归一化 `..`，不解析符号链接）；`file:` 视同本地路径；其他 scheme 报错
- fetch：缓存根目录取 `$SKUA_CACHE_DIR`，未设置时为 `./cache`（已 gitignore）；测试由 conftest 指向临时目录
- fetch：已完成的 `seg<N>.ts` 直接复用；残留 `.part` 用 `Range: bytes=<已有>-` 续传，服务器回 200 则从头重下，回 416 则删掉 `.part` 重试；5xx / 408 / 429 / 网络错误重试，其余 4xx 立即失败；本地源不复制，直接用原路径
- fetch：响应体长度与 Content-Length 不符的显式检查是防御代码：aiohttp 3.14 在连接中途断开时自己会抛 ClientPayloadError，测试中走不到这一分支
- 合成素材：AES 加密用 ffmpeg `crypto:` 协议的写模式，配合 `data` 解复用器 / 封装器按原始字节加密（逐字节可逆，已在本机用 openssl 解开比对，openssl 不入仓库）。不用 `-c copy -copyts` 重新封装：ffmpeg 7.0.2 会把接近 33 位回绕的分片时间戳改写成从 0 开始
- 合成素材：编码器策略见 AGENT.md 6.1（项目所有者 2026-10-19 决定，mpeg2video 为默认，h264 组为 CI 增量，探测链不含 libopenh264）。EXTINF 取段内视频帧数 / fps，不用 segment 封装器 csv 的时间（它差一帧）；PTS 偏移用 segment 封装器的 `-initial_offset`（`-output_ts_offset` 会让切点错位）；内层 mpegts 选项须经 `-segment_format_options` 传入
- schema：数据合约上的不变量（不是策略参数）在构造时校验：`media_id` 16 位小写 hex、Shot.idx 连续从 0 起、keyframe_ts 1–3 个且在镜头内、事件与镜头按时间非降序、Segment 0 ≤ start < end 且 confidence ∈ [0, 1]、SkipList 内区间有序不重叠、事件 payload 必含非空 `unit`、自由字典必须是纯 JSON 值。numpy 标量会被转成 Python 标量
- schema：`FEATURE_SCHEMA` 的 list 子字段显式命名为 `element` 且不可为 null，因为 parquet 读回时会改名为 `element`，这样读回的 schema 与定义逐字节一致；parquet 元数据带 `skua_schema_version`；写入用 `.part` + 原子 rename；有值但 FEATURE_SCHEMA 中尚无列的字段（M1/M2）拒绝写入，而不是静默丢弃

## 性能记录
- 合成 HLS 45 分钟等效，`skua scan` 耗时：待批次 7 测量

## 下次会话从这里继续
- 工作流程：直接提交 main（AGENT.md 4.1）；推送前过第 7 章清单；推送后用 Actions 页面确认运行通过再汇报；汇报前先 `git ls-remote origin` 确认远端事实状态
- 本机没有 `gh`，匿名 API 常被限流；改为读取 Actions 网页（`/actions/workflows/ci.yml?query=branch%3Amain` 的 aria-label）确认结果
- 下一步：批次 3 验收后开始批次 4（fetch / decode）
