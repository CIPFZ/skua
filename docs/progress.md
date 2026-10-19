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
- [x] 10.3e1 `adapter/timeline.py`：EXTINF 累加 + 首帧 PTS 校正（容差 0.5 s）、DISCONTINUITY 重锚记 `pts_jump(tagged=True)`、无标签跳变记 `tagged=False`、跨分片按最近同余代表展开回绕、不可 demux 分片退回 EXTINF、`video_params` / `audio_params` / `pid_change` / `bitrate_outlier`、播放列表事件按 unit 映射到校正后的轴、M0 每分片一个镜头（关键帧按 SPEC 3.2）
- [x] 10.3e2 `adapter/hls.py` 门面 `open_hls`：master 选变体 → 解析 → 按 KEY URI 把密钥取进内存 → 下载 → 按 IV 属性或媒体序号（RFC 8216 5.2）构造 AesParams → demux 探测 → Timeline；写 `cache/<media_id>/index.json`（不含密钥、IV、密钥 URI）
- [x] 10.3f 合成 HLS 事件序列 / 时间轴 / PTS 回绕测试：真值由 PyAV 原始 PTS 经测试内独立纯函数推出，ffprobe 交叉校验一次，误差 ±1 帧（AGENT.md 6.2）；h264 组在 CI 上执行同样的断言

### 批次 6 structural（10.4）
- [x] 10.4a `evidence/structural.py`：10 维 struct（列序 `STRUCT_FIELDS`）、块级与 daterange 硬先验（`hard_prior: dict[shot_idx, float]`）及其来源 `PriorRange`
- [x] 10.4b `evidence/rule_decoder.py`（M3 删除）：连续硬先验镜头合并为 `Segment(label="ad", confidence=0.95)`；`evidence` = {decoder, hard_prior, sources[]}，每个来源含 source / first_unit / last_unit / duration_s，块来源另含 conditions（起点 discontinuity、起点 video_params、uri 命中数、分片数、≤180 s、是否最大块），daterange 来源另含 daterange（id / class / scte35 / anchored_by / duration_source / closed_by_scte35_in）；测试断言各层字段集合

### 批次 7 cli / serve（10.5）
- [ ] 10.5a `cli.py`：`skua scan` 的 `--out` 与 `--json-events`
- [ ] 10.5b `serve/skiplist.py`：SQLite 存取
- [ ] 10.5c 端到端测试：区间误差 ≤ 一个分片

### 批次 8 验收（10.6）
- [ ] 10.6a `golden/PROTOCOL.md` 与最小 `skua eval`
- [ ] 10.6b 干净 clone 后 `uv sync && uv run pytest` 全绿；两个检查脚本在故意违规提交上各失败一次（由 `selftest_checks.sh` 覆盖）
- [ ] 10.6c `docs/reports/m0-<date>.md` 与项目所有者本机验证命令；`git tag m0`

## 进行中
- 批次 6 全部完成（SPEC 3.1 两项修正、structural.py、rule_decoder.py），待项目所有者验收

## 已知问题
- 2026-10-19 Run 26–27 红：`test_struct_vectors` 写死了本机合成素材的"距最近事件"值，CI（ffmpeg 6.1）的合成码率略有不同，第 2 个镜头起点多出一个事件。已改为由测试从 Timeline 事件独立重算码率相关维度，结构维度仍写死（AGENT.md 6.1：基础测试集不得依赖编码器的具体字节）。CI 新增"失败用例转注解"步骤，匿名可在运行页读到失败断言
- 已知盲区：开放 GOP（前导 B 帧先于关键帧显示）只由 `pts_extent` 纯函数单元测试覆盖；合成素材是 closed GOP，本机 mpeg2video 做不出前导 B 帧。真实开放 GOP 源要到 M0 golden 验收时才第一次被真正测到
- 本机没有 `fpcalc`，M0 用不到，M1 开工前需要安装 chromaprint。CI 已安装
- 本机 ffmpeg 7.0.2 是 `--enable-libopenh264-dlopen` 构建：`-encoders` 列出 libopenh264，但运行时缺 `libopenh264.so.7`，实际无法编码；本机也没有 libx264；TencentOS 4 的 dnf 源中没有 openh264 包。本机实测可用的视频编码器只有 mpeg2video、mpeg4、libaom-av1、libsvtav1。结论：编码器探测必须靠试编码，不能只读 `-encoders` 列表
- ffprobe（7.0.2）加 `-correct_ts_overflow 0` 后，回绕点附近个别包仍被展开成 2^33 + x，其后的包又是原始值：ffprobe 的时间戳不是一致的原始 33 位值，批次 5 拿 ffprobe 当真值时必须按模 2^33 比较（样例见 `tests/fixtures/proc/ffprobe_segment_wrap.json`）
- PyAV 18.x 捆绑 FFmpeg 8.x，系统 ffprobe 是 7.x，±1 帧对比可能存在系统偏差（M0 风险 7，ADR-001）
- `check_imports` 只防误用不防恶意：`getattr(os, "system")` 一类反射调用抓不到（项目所有者 2026-10-16 接受）

## 开放问题（需项目所有者决策）
- （影响错跳，structural 已按保守一侧实现，待确认）同一 DATERANGE 同时带 CLASS=ad 与 SCTE35-IN 时不构成硬先验（SPEC "IN 永不构成硬先验"优先）
- （特征口径，待确认）struct 第 9 维 daterange_marker：广告标记（CLASS=ad / OUT，含被 IN 截断后延伸的范围之外的锚点本身）与 SCTE35-CMD 记 1，只带 SCTE35-IN 记 0
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
- structural：只读 Timeline；分片之间靠 unit 关联。块 = 起点 unit 带 discontinuity 事件的镜头开始的一段；块起点的 discontinuity / video_params 按 unit 判断，struct 前 4 维按"事件 ts 在镜头起点 ±0.5 s"判断（SPEC 3.3）；duration / bitrate 取所覆盖 unit 的最大 |z|
- structural：URI 签名被禁用时不产出任何 uri_pattern_break，"块内所有分片均命中"自然不成立，块级先验因此整体失效，与 SPEC 3.3 等价，不需额外标志
- structural：几个块并列最长时都算"最大块"（先验更少，偏保守）；首个块没有 discontinuity，永远得不到块级先验
- structural：无时长的 SCTE35-OUT 由其后第一个同 ID 的 SCTE35-IN 截断（没有同 ID 的就取其后第一个 IN），范围到 IN 所在镜头之前为止；其后没有 IN 时只覆盖锚点镜头
- structural：struct 第 8 维 = log1p(镜头起点到最近任何事件 ts 的秒数)，整个 Timeline 没有事件时取 log1p(duration)
- hls / decode：AES-CBC 用错 IV 只破坏首个 16 字节块，libav 会越过它重新同步，demux 照样成功——错 IV 本来是静默的。decode 对每个加密分片单独读取解密后的首个 TS 包（PyAV `data` 格式读原始字节），校验其中 PSI 表段（PID ≤ 0x1F）的 CRC32/MPEG-2（`check_decrypted_head`，读取失败也算失败）。这个检查独立于 demux：错的密钥通常连 PKCS7 填充都通不过、demux 直接失败，若把检查放在 demux 之后就会漏计
- hls（SPEC 3.1）：KEY 范围 = 连续且 EXT-X-KEY 完全相同的分片段；同一范围内 CRC 失败 ≥ 2 片（`HlsConfig.crc_failures_to_abort`）时 `open_hls` 抛 `AesIntegrityError`，消息给出分片区间、媒体序号区间与脱敏后的 KEY URI；恰好 1 片失败只告警，并在 index.json 记 `head_crc_ok=false`（清流分片与无可校验表段的分片为 null），M1 指纹入库须跳过 false 的分片。注意：只覆盖一个分片的 KEY 范围最多失败 1 片，按规则永远不会升级
- hls：KEY URI 相对于所选媒体播放列表解析（`KeyState.absolute_uri`），同一 URI 只取一次；密钥长度不是 16 字节或取不到时直接报错，不退化
- timeline：分片长度取实测跨度（末包 pts + duration − 最早 pts），与 EXTINF 相差超过容差时不信实测、用 EXTINF；分片起点在同一 PTS 段内取 PTS 推算值，与 EXTINF 累加值相差超过容差时记无标签 `pts_jump` 并在累加值处重锚；DISCONTINUITY 处总是在累加值处重锚，前一分片可 demux 时才记 `pts_jump(tagged=True)`（delta 为与前段结束 PTS 的最近同余差）
- timeline（SPEC 3.1）：`bitrate_outlier` 用分片字节数 × 8 / EXTINF；MAD 基准（含 MAD = 0 回退）排除每个不连续块的首片；首片以本块其余分片的中位数为参考、仍用全局尺度判定；只有一片的块没有"其余分片"，按全局中位数判定（实现决定）；末片不排除。payload 增加 `block_head`
- timeline：合成素材实测（新口径）：广告块首片 ad012 不再离群（z ≈ 0）；广告其余两片 z ≈ −14；三个正片块首片仍离群（z ≈ 12.4），因为测试图样的 I 帧使首片比块内中位数高约 28%，远大于各块中段之间的差异。bitrate 只进 struct 特征，阈值未动；struct 取 |z| 会丢掉符号，正片块首片与 discontinuity 同位置仍然离群这一点已汇报
- timeline：测试真值由 `truth_axis`（测试内的独立纯函数）从 PyAV 读出的原始首帧 PTS 推出，ffprobe 只做一次交叉校验（AGENT.md 6.2）；PTS 校正由"同一段内 0.2 s PTS 空隙"用例覆盖
- decode：PyAV 打开时一律加 `correct_ts_overflow=0` 读原始时间戳。libav 默认的溢出校正会把回绕前的包报成负数，而且随 libav 版本变化；分片内回绕由 `unwrap`（取模 2^33 后与锚点最近的代表）展开，跨分片的展开留给 timeline。已验证：PyAV 18.1（捆绑 FFmpeg 8）的原始值与系统 ffprobe 7.0.2 的值模 2^33 全部一致，包括 AES 解密后的分片
- decode：分片的 `first_pts` 取参考流（首个视频流，没有则音频流）所有包里最早的展示时间，不是第一个包的 pts（open GOP 的前导 B 帧先显示）；合成素材是 closed GOP，所以这一点由 `pts_extent` 的单元测试覆盖
- decode：`video_params` 比较 codec / width / height / profile / level / pix_fmt / 平均帧率，`audio_params` 比较 codec / sample_rate / channels / profile（SPEC 3.1，不比 extradata）；`pid_change` 比较 (PID, 媒体类型) 集合。payload 带 changed、before、after，不带 unit（由 timeline 补）
- decode：`iter_pcm` 每个分片以首个音频帧的 pts 定位，分片内时间按已输出的样本数累加，不依赖重采样器的时间戳
- fetch：`media_id` 规范化时同时丢弃 URL 中的 userinfo（凭据不应影响内容身份）；本地文件取 `os.path.abspath`（归一化 `..`，不解析符号链接）；`file:` 视同本地路径；其他 scheme 报错
- fetch：缓存根目录取 `$SKUA_CACHE_DIR`，未设置时为 `./cache`（已 gitignore）；测试由 conftest 指向临时目录
- fetch：已完成的 `seg<N>.ts` 直接复用；残留 `.part` 用 `Range: bytes=<已有>-` 续传，服务器回 200 则从头重下，回 416 则删掉 `.part` 重试；5xx / 408 / 429 / 网络错误重试，其余 4xx 立即失败；本地源不复制，直接用原路径
- fetch：响应体长度与 Content-Length 不符的显式检查是防御代码：aiohttp 3.14 在连接中途断开时自己会抛 ClientPayloadError，测试中走不到这一分支
- 合成素材：产物目录为 `<cache>/<layout>-<encoder>-<key>`，key = sha256(make_synth.py 字节 + 布局 + 编码器 + SynthConfig + ffmpeg 版本) 前 16 位（AGENT.md 6.1）。先在私有临时目录生成，再原子 rename 到最终名，所以最终名下的目录一定完整；manifest 记录 key 与 ffprobe 探测结果，`load_synth` 从 manifest 重建。测试的缓存根是 `.pytest_cache/d/skua-synth`（CI 每次全新）；旧 key 的目录与中断留下的 `.` 前缀临时目录不会被读，也不自动删除
- 合成素材：AES 加密用 ffmpeg `crypto:` 协议的写模式，配合 `data` 解复用器 / 封装器按原始字节加密（逐字节可逆，已在本机用 openssl 解开比对，openssl 不入仓库）。不用 `-c copy -copyts` 重新封装：ffmpeg 7.0.2 会把接近 33 位回绕的分片时间戳改写成从 0 开始
- 合成素材：编码器策略见 AGENT.md 6.1（项目所有者 2026-10-19 决定，mpeg2video 为默认，h264 组为 CI 增量，探测链不含 libopenh264）。EXTINF 取段内视频帧数 / fps，不用 segment 封装器 csv 的时间（它差一帧）；PTS 偏移用 segment 封装器的 `-initial_offset`（`-output_ts_offset` 会让切点错位）；内层 mpegts 选项须经 `-segment_format_options` 传入
- schema：数据合约上的不变量（不是策略参数）在构造时校验：`media_id` 16 位小写 hex、Shot.idx 连续从 0 起、keyframe_ts 1–3 个且在镜头内、事件与镜头按时间非降序、Segment 0 ≤ start < end 且 confidence ∈ [0, 1]、SkipList 内区间有序不重叠、事件 payload 必含非空 `unit`、自由字典必须是纯 JSON 值。numpy 标量会被转成 Python 标量
- schema：`FEATURE_SCHEMA` 的 list 子字段显式命名为 `element` 且不可为 null，因为 parquet 读回时会改名为 `element`，这样读回的 schema 与定义逐字节一致；parquet 元数据带 `skua_schema_version`；写入用 `.part` + 原子 rename；有值但 FEATURE_SCHEMA 中尚无列的字段（M1/M2）拒绝写入，而不是静默丢弃

## 性能记录
- 合成 HLS 45 分钟等效，`skua scan` 耗时：待批次 7 测量

## 下次会话从这里继续
- 工作流程：直接提交 main（AGENT.md 4.1）；推送前过第 7 章清单；推送后用 Actions 页面确认运行通过再汇报；汇报前先 `git ls-remote origin` 确认远端事实状态
- 本机没有 `gh`，匿名 API 常被限流；改为读取 Actions 网页（`/actions/workflows/ci.yml?query=branch%3Amain` 的 aria-label）确认结果
- 下一步：批次 6 验收后开始批次 7（cli scan / serve skiplist / 端到端）
