# Skua：视频广告区间检测与跳过 实现方案（SPEC.md）

> 状态：草案 v0.3，2026-10-16。工作代号 Skua（贼鸥，专门把别人塞进来的东西叼走）。代号可零成本替换。
> 本文件是"实现方案"：定义数据模型、模块边界、每个模块的具体算法与选型、训练与评测协议、里程碑与验收标准。架构层面的论证见对话记录，不在此重复。

---

## 0. 一句话定义

输入任意可解码的视频源（v1 为 HLS/m3u8），输出带置信度的时间区间列表 `[start, end, label, confidence]`，供播放器或代理按策略跳过。检测以镜头为基本单元，融合结构信号、重复性、内容理解三路证据，由带时长先验的序列解码器产出区间，并通过指纹库与用户反馈形成自监督闭环。

**明确不做（v1）**：直播流、VLM 推理链路、服务端众包数据库、浏览器扩展、Windows 原生播放器集成。

---

## 1. 技术栈决策

| 层 | 选型 | 理由 |
|---|---|---|
| 研究/检测管线 | Python 3.11，PyTorch 2.x，ONNX Runtime | 所有模型（TransNetV2、DINOv2、BEATs、OCR）都有现成 Python 权重与参考实现；本项目的风险在数据与建模，不在工程性能，先用最短路径把真实效果跑出来 |
| 解码 | PyAV（libav 绑定），ffmpeg CLI 兜底 | 需要同时拿到帧、PCM 与容器事件（PTS 跳变、参数集变化），CLI 拿不到后者 |
| 指纹索引 | SQLite（元数据）+ `usearch`（向量 ANN） | 单机本地、零运维；usearch 支持 Hamming 与 cosine，视频哈希与 embedding 共用一个库 |
| 消费端 | mpv Lua 脚本（跳区间）+ Python aiohttp HLS 代理（丢整分片） | 两者互补：代理只能丢整分片，mpv 脚本可做秒级跳转 |
| 后续迁移边界 | 特征提取与解码 → Rust（Ambra 承接视觉/音频 embedding 推理） | 仅当 M3 验收通过且 Python 端延迟成为瓶颈时才迁移；`skua-core` 的 JSON 中间格式就是迁移边界 |

---

## 2. 数据模型

所有模块之间只通过以下结构通信，全部可序列化为 JSON，落盘到 `work/<media_id>/`。

```python
@dataclass
class Timeline:
    media_id: str            # 播放列表 URL 规范化后的 sha256 前 16 hex（规范化：scheme/host 小写、去默认端口、去 query 与 fragment；本地文件用绝对路径）。只依赖 URL，播放器侧无需下载即可计算；同一内容的不同 URL 由 M1 指纹别名合并（3.4）
    duration: float          # 秒
    fps: float
    shots: list[Shot]
    container_events: list[ContainerEvent]
    playlist_events: list[PlaylistEvent]   # 仅 HLS/DASH

@dataclass
class Shot:
    idx: int
    start: float; end: float               # 统一时间轴，秒
    keyframe_ts: list[float]               # 1–3 个代表帧时间戳
    source_units: list[str]                # 覆盖的分片 URI（HLS）或 byte range（MP4）

@dataclass
class ContainerEvent:
    ts: float
    kind: Literal["pts_jump", "video_params", "audio_params", "pid_change", "bitrate_outlier"]
    payload: dict                          # 必含 unit（所属分片 URI）

@dataclass
class PlaylistEvent:
    ts: float
    kind: Literal["discontinuity", "key_change", "duration_outlier", "uri_pattern_break", "daterange"]
    payload: dict                          # 必含 unit（所属分片 URI）

@dataclass
class ShotFeatures:                        # 每镜头一行，写 parquet。struct 之外的字段按里程碑逐步填充，缺失为 None，下游必须能处理 None（对应 --lite 与训练时的特征 mask）
    shot_idx: int
    struct: np.ndarray                     # 证据 A，10 维（3.3）
    fp_video_hit: float | None = None; fp_audio_hit: float | None = None; fp_hit_label: str | None = None   # 证据 B（M1）
    vis_emb: np.ndarray | None = None      # DINOv2 ViT-S/14，384 维，关键帧均值（M2）
    aud_emb: np.ndarray | None = None      # BEATs，768 维，镜头内窗口均值（M2）
    loudness: np.ndarray | None = None     # LUFS-I、LRA、峰值、静音占比，4 维（M2）
    ood_vis: float | None = None; ood_aud: float | None = None; face_outlier: float | None = None   # 本集 OOD（M2）
    ocr_flags: np.ndarray | None = None    # 二维码、广告关键词、价格/电话模式，约 6 维（M2）

@dataclass
class Segment:
    start: float; end: float
    label: Literal["content", "ad", "intro", "recap", "outro", "trailer"]
    confidence: float                      # 校准后的后验；golden 标注中固定为 1.0
    evidence: dict                         # 各路证据的贡献，便于调试与用户申诉；golden 标注中为空

SkipList = {"media_id": str, "version": int, "segments": list[Segment]}
# version 是同一 media_id 下 skip list 的修订号，从 1 递增；序列化顶层另带 schema_version 元数据（来自 skua/schema.py 的 SCHEMA_VERSION），不属于数据字段。
# segments 只存非 content 区间（ad / intro / recap / outro / trailer），按 start 升序且互不重叠；content 是隐含的补集，不写入文件。理由：消费端只关心"要跳什么"，评测按标签统计也不需要 content 区间，且 content 的边界误差没有独立意义。Semi-Markov 解码内部产出全覆盖序列，落盘前丢弃 content 段。
# golden 标注文件与 SkipList 使用同一 schema，另加顶层 annotator 与 annotated_at 两个元数据字段，评测即 SkipList 对 SkipList 的区间比对。
# 事件 payload 的 unit 字段标识事件所属的最小源单元：HLS 为分片 URI；无分片概念的容器（MP4/MKV，M4）为 "byte:<start>-<end>"，由适配器保证非空。
```

---

## 3. 模块与算法

### 3.1 `adapter/`：输入适配器 → Timeline

职责：产出解码帧迭代器、PCM 迭代器、容器事件、播放列表事件，统一到一条时间轴。

**HLS 适配器**
- 文件布局：`playlist.py`（解析与播放列表事件）、`fetch.py`（下载缓存）、`decode.py`（唯一允许 `import av` 的文件：demux 探测、帧与 PCM 迭代器、容器事件）、`timeline.py`（时间轴重建）、`proc.py`（外部进程白名单），`hls.py` 只是门面 `open_hls(src)`，只做编排。adapter 内部的分片表（URI、本地路径、轴偏移）落 `cache/<media_id>/index.json`，不进 schema。
- 用 `m3u8` 库解析 master/media playlist，选最高码率变体。
- 分片下载并发 8，落盘缓存到 `cache/<media_id>/segN.ts`，`.part` + 原子 rename 实现断点，失败重试 3 次指数退避，支持边下边检。加密分片以原始密文落盘，解密只在 decode 读取时经 libav `crypto:` 协议在内存中进行；密钥与 IV 只存在于内存，不落盘、不进日志、不进 `index.json`。IV 缺省时按 RFC 8216 §5.2 取分片 `EXT-X-MEDIA-SEQUENCE` 序号的大端 128 位值。
- M0 支持 MPEG-TS 与 AES-128（经 libav `crypto:` 协议，不加依赖）；fMP4（`EXT-X-MAP`）、`SAMPLE-AES`、`EXT-X-BYTERANGE`、直播列表（无 `ENDLIST`）、未知 `KEY METHOD` 一律明确报错退出。fMP4 与 BYTERANGE 在 M1 一起补齐（fMP4 源常用 BYTERANGE）。需要自定义请求头的源用 `--header`，只从命令行读，不落盘、不写日志。
- 时间轴重建：以 `EXTINF` 累加为参考，再用每分片首帧 PTS 校正（容差 0.5s，为实现常量非策略参数）；遇 `DISCONTINUITY` 时间轴不断，PTS 在此重锚并记 `pts_jump`（payload 带 `tagged=True`）；无标签的 PTS 跳变记 `pts_jump(tagged=False)`。33-bit PTS 回绕按最近同余代表展开。无法 demux 的分片退化为 EXTINF 长度并告警。
- 播放列表事件：
  - `discontinuity`：直接来自 tag，作用于其后分片（RFC 8216 §4.3.2.3）。
  - `duration_outlier`：对 `EXTINF` 做 MAD 鲁棒 z-score，|z| > 3 记事件；最后一个分片不参与也不被判定（HLS 末片合法地短）；MAD = 0 时退回 1.2533 × 平均绝对偏差，仍为 0 则本路不产出事件。
  - `uri_pattern_break`：先去掉 query 与 fragment，再对 host、路径深度、文件名模板做签名。模板化按词进行：文件名以 `-`、`_`、`.` 切词；整词为 ≥ 8 位 hex 的替换为 `#`；整词 ≥ 16 位、只含 base64 字符且同时含大写与小写字母的替换为 `#`；其余词只把其中的数字串替换为 `#`（`segment00012` → `segment#`，`advert00012` → `advert#`，两者不同）。与多数签名不同即记事件。可靠性门槛：多数签名占分片数不足 60% 时本路整体禁用并告警（签名不可靠时宁可不报）。
  - `key_change`：`EXT-X-KEY` 变化；首个 KEY 不算变化，之后任何变化（含有无切换）都算。
  - `daterange`：`EXT-X-DATERANGE` 带 `CLASS=ad` 或任一 `SCTE35-*` 属性的全部上报，payload 记录属性种类（out / cmd / in），取舍由 3.3 负责。时长按 RFC 8216 §4.3.2.7 取 `DURATION`，缺则 `END-DATE − START-DATE`，再缺则 `PLANNED-DURATION`，三者皆无只覆盖所挂分片。锚定：播放列表含 `EXT-X-PROGRAM-DATE-TIME` 时按 `START-DATE` 映射到分片（RFC 规定的语义，标签在列表中的位置不作数）；无 PDT 时才退化为标签所挂分片。
- 容器事件：PyAV 逐包读取。`video_params` 比较解析后的字段（codec、width、height、profile、level、pix_fmt、平均帧率），不比较 extradata 哈希（部分编码器 extradata 抖动会误报）；`audio_params` 比较 codec、sample_rate、channels、profile；`pid_change` 通过 demuxer 的 stream 列表变化检测；`bitrate_outlier` 由 adapter 按分片字节数 / 时长算码率的 MAD z，|z| > 3 时发出，payload 带 z。

**MP4/MKV 适配器（M4）**
- 容器事件来源改为 edit list、chapter、关键帧间隔离群。播放列表事件为空。

### 3.2 `shots/`：镜头切分

- 主模型 TransNetV2（ONNX 导出，输入 48×27 的 100 帧窗），CPU 上约 20× 实时。
- 输出转场概率 > 0.5 为硬切；相邻镜头 < 0.4s 合并。
- 对每镜头取关键帧：时长 < 2s 取中点 1 帧；否则取 25%、50%、75% 三帧。
- 兜底：PySceneDetect `ContentDetector` 阈值 27。
- 解码策略：`-skip_frame nokey` 不可用（TransNetV2 需要连续帧），改为统一解到 48×27 低分辨率流跑切分，再对关键帧时间戳用 seek 解原分辨率帧。两次解码仍比 1fps 全量解高分辨率便宜。

### 3.3 `evidence/structural.py`：证据 A

每镜头特征（10 维，事件归属按"事件 ts 落在镜头起点 ±0.5s"判定，只看起点：RFC 语义下标签作用于其后的分片，这样同一事件不会同时打到前后两个镜头）：
- 镜头起点是否有 `discontinuity` / `pts_jump` / `video_params` / `audio_params`（4 个 0/1）。
- 所覆盖分片的 `duration_outlier` 最大 |z|、`uri_pattern_break` 是否命中、`key_change` 是否命中（3 维）。
- 所覆盖分片的 `bitrate_outlier` 最大 |z|，无则 0（1 维）。
- 距离最近结构事件的时间 `log1p(秒)`（1 维）。
- 覆盖分片是否带 `daterange` 广告标记（1 维，同时输出硬标签）。

**硬先验（按"不连续块"而非单分片判定）**
- 不连续块 = 相邻两个 `discontinuity`（或播放列表首尾）之间的连续分片。
- 一个块获得 `ad` 硬先验（解码器对数似然 +4）当且仅当同时满足：块起点带 `discontinuity` 与 `video_params`；块内所有分片均命中 `uri_pattern_break`；块总时长 ≤ 180s（SPEC 3.6 广告块先验上界，更长的块更可能是正片换源）；块不是播放列表中时长占比最大的块。
- 被 `daterange` 标记的范围直接获得硬先验，不受上述条件约束。只有 `CLASS=ad` 与 `SCTE35-OUT` 构成广告标记；`SCTE35-IN` 标记的是广告结束、正片恢复的位置，永不构成硬先验，但用于截断前一个无时长 `SCTE35-OUT` 范围的终点；`SCTE35-CMD` 不解析二进制 `splice_info_section` 时语义不明（可能是 OUT 也可能是 IN），M0 不构成硬先验，只作为 struct 特征记录，解析留到 M1 之后视真实数据决定。
- `uri_pattern_break` 单独永远不构成硬先验；本路被禁用（3.1 的 60% 门槛）时块级硬先验整体失效，只剩 `daterange`。
- M0 的临时决策器 `rule_decoder.py` 仅把硬先验块输出为 `Segment(label="ad", confidence=0.95)`，M3 由 Semi-Markov 解码替换。

### 3.4 `evidence/fingerprint.py`：证据 B

**视频指纹**
- 每关键帧：灰度缩放 32×32 → DCT → 取左上 8×8 低频 → 中值二值化 → 64 bit pHash。
- 每镜头指纹 = 关键帧哈希序列（1–3 个）+ 镜头时长。
- 入库键：`(hash, media_id, shot_idx, ts)`；usearch Hamming 索引。
- 命中：Hamming ≤ 10 且时长差 < 15%，且命中来自不同 `media_id`。

**音频指纹**
- Chromaprint（`fpcalc -raw`）对每镜头音频算 32 bit/帧 的指纹序列，按 1 秒粒度切成子指纹入库。
- 命中：连续 ≥ 3 秒的子指纹比特错误率 < 0.35。

**自匹配规则（无标注产出伪标签）**
- series / episode 归属由调用方提供（`skua scan --series <id> --episode <n>`，mpv 脚本从文件名或用户配置推断），缺省为未知；未知时只执行"跨 media_id 重复"一条规则。
- media_id 别名合并：两个 media_id 的镜头指纹互相命中覆盖任一方 ≥ 50% 时长，视为同一内容（同一集的不同 URL、不同会话 token、SSAI 导致的变化），在 `media_alias` 表合并为一个内容身份，此后"跨 media_id"按内容身份计算。该规则优先于下面所有规则执行，是防止"同一集被当成跨剧重复而整集判 ad"的硬保护。
- 跨 series 重复（不同剧）：`ad` 候选。
- 同 series 跨 episode 重复，且位于每集相对位置 < 10% 或 > 90%：`intro` / `outro` 候选。
- 同 series 跨 episode 重复，位于开头 10%–20%，且与上一集后段内容命中：`recap` 候选。
- 同 series 内与下一集内容命中、位于末尾：`trailer` 候选。
- 任一规则产出的 `ad` 候选在单集内累计时长不得超过该集总时长的 30%，超过则整集的 B 路候选降级为"待人工确认"并告警，不进入伪标签。
- 输出：`fp_video_hit` 与 `fp_audio_hit` 为命中强度（0–1），`fp_hit_label` 为规则给出的标签。

**反馈回流**
- mpv 脚本上报用户手动跳过区间（`u` 键）和误跳回退（`Shift+u`），写入 `feedback` 表，手动跳过的区间全部指纹立即入库并打 `ad` 标签，回退的区间标 `content` 并给对应指纹加负权重。

### 3.5 `evidence/content.py`：证据 C 特征

- 视觉：DINOv2 ViT-S/14（`facebook/dinov2-small`），224 输入，CLS token 384 维，关键帧均值后 L2 归一化。选 DINOv2 而非 CLIP：目标是区分"制作风格"（调色、构图、字幕样式、画幅）而非语义，DINOv2 的自监督特征在风格维度上更可分。
- 音频：BEATs（`BEATs_iter3_plus_AS2M`），镜头内按 1s 窗口 → 768 维均值；加 `pyloudnorm` 的 LUFS-I、LRA、True Peak，加静音占比。
- OCR 与二维码：仅对关键帧跑 `rapidocr-onnxruntime`（CPU 友好），正则匹配 价格/电话/网址/"下载""扫码""APP""客服" 等；二维码用 OpenCV `QRCodeDetector`。输出 0/1 标志位，不输出文本。
- 本集 OOD：
  - 对本集所有镜头的 `vis_emb` 用 Minimum Covariance Determinant 估鲁棒均值与协方差（支持度 0.7），输出每镜头马氏距离的分位数 → `ood_vis`；`aud_emb` 同理 → `ood_aud`。
  - 人脸：`insightface buffalo_sc` 对关键帧检脸并抽 512 维；DBSCAN 聚类，簇大小前 5 为"主演簇"；镜头中所有人脸都不在主演簇则 `face_outlier = 1`。
  - OOD 的意义：不需要知道广告长什么样，只需要知道它不像本集。

### 3.6 `model/`：时序模型与解码

**发射模型**
- 输入：每镜头拼接特征（struct 10 + fp 3 + vis 384 + aud 768 + loudness 4 + ood 3 + ocr 6 ≈ 1180 维），先各自 LayerNorm 再拼接，线性降到 256；None 字段以零向量加 1 维"缺失"标志位代入。
- 主干：2 层 BiGRU，隐层 256，dropout 0.3；或 2 层 Transformer encoder（4 头，相对位置编码），镜头数 ≤ 1500 时两者皆可，默认 BiGRU。
- 输出：每镜头 6 类对数概率 + 1 维"边界"概率（镜头起点是否为区间边界）。
- 损失：类别交叉熵（类权重：错判 `content → ad` 权重 5，其余 1）+ 边界 BCE。非对称权重直接编码"错跳正片远比多看广告糟糕"。

**Semi-Markov Viterbi 解码**
- 状态 = 标签；每个区间由连续若干镜头组成，得分 = 镜头发射对数概率之和 + 时长先验对数概率 + 转移对数概率。
- 时长先验（对数正态，按秒）：`ad` μ=ln(20) σ=0.6（5–60s，单条）；`intro` μ=ln(75) σ=0.3；`recap` μ=ln(45) σ=0.5；`outro` μ=ln(60) σ=0.4；`trailer` μ=ln(30) σ=0.5；`content` μ=ln(300) σ=1.0 且硬下界 15s。
- 转移矩阵：`content ↔ ad` 自由；`intro` 只能出现在前 15%，`outro`/`trailer` 只在后 15%，`recap` 只在前 25%，违反给 -inf。
- 复杂度 O(N · Dmax · K)，N≈500 镜头、Dmax≈60 镜头、K=6，毫秒级。
- 证据 A 的硬先验与证据 B 的命中作为额外发射项加入，权重可学习（线性层）或固定（+4 / +3）。

**置信度校准**
- 对解码出的每个区间，取区间内镜头后验均值，经 temperature scaling（在验证集上拟合）校准。
- 跳过策略三档：≥ 0.9 自动跳过；0.6–0.9 屏显"检测到广告，按 Enter 跳过"5 秒；< 0.6 不动作但记录。

### 3.7 `train/`：数据与训练

**冷启动伪标签**
1. 对手头所有剧集跑 adapter + shots + 证据 A + 证据 B。
2. 证据 A 硬命中 ∪ 证据 B 跨剧命中 → `ad` 正例；证据 B 的 intro/recap/outro/trailer 规则 → 对应标签。
3. 距离任何正例 > 60s 且无任何结构事件的镜头 → `content` 负例；其余镜头标 `ignore` 不参与损失。
4. 用伪标签训练发射模型；推理后置信 ≥ 0.95 的新 `ad` 区间写回指纹库（self-training 一轮，不迭代多轮以免漂移）。

**Golden 集（只评测不训练）**
- 人工标注 20 集，覆盖 ≥ 4 个来源站点、≥ 3 种剧集类型，边界精度到 0.5s，标签用上述 6 类。标注工具：mpv + Lua 脚本打点导出 JSON。
- 文件格式与 `SkipList` 同 schema（见第 2 章），一集一个文件 `golden/<media_id>.json`，不含 URL；URL 到 media_id 的映射只存在于项目所有者本机的 `.env.local`。标注协议写在 `golden/PROTOCOL.md`。
- 最小评测工具 `skua eval --pred <skiplist.json> --gold <golden.json>` 在 M0 批次 8 落地，只实现区间级 P/R/F1 与边界误差。

**指标**
- 区间级 P/R/F1（IoU ≥ 0.5），按标签分别统计。
- 边界误差：匹配区间的起止点绝对误差中位数与 P90。
- 用户代价：每小时错跳正片秒数（目标 < 2s/h）、每小时漏跳广告秒数（目标 < 15s/h）。
- 消融：A 单独 / A+B / A+B+C / 去掉 OOD / 去掉音频 / 阈值平滑 vs Semi-Markov，每个里程碑跑一遍存入 `docs/reports/`。

### 3.8 `serve/`：消费端

**mpv Lua 脚本 `skua.lua`**
- 启动时按 `media_id` 读取 skip list（本地文件或 `http://127.0.0.1:7788/skiplist/<id>`）。
- 监听 `time-pos`，进入高置信区间直接 `seek end`，中置信区间 OSD 提示。
- 热键：`u` 标记"这段是广告"（从上一次按键或镜头起点到当前）；`Shift+u` 撤销上一次自动跳过并上报误跳。
- OSD 样式：提示框底色 `#0F1B2D`（navy，85% 不透明），正文 `#F1EDE3`（cream），次要文字 `#5C6B7A`（slate），广告区间与"已跳过"状态用 `#F0695A`（coral）；提示框左侧放 `assets/icon/pack/skua-glyph-32.png` 单色 glyph。进度条上用 coral 细条标出检测到的区间（高置信实心、中置信描边）。

**品牌资产**
- 应用图标为方案 A「叼走分片」，源文件与打包产物见 `assets/icon/`，说明见 `assets/icon/README.md`。macOS 用 `pack/skua.icns`，Windows 用 `pack/skua.ico`，Linux 用 `pack/skua-icon-{size}.png` 装入 `hicolor`，mpv / 托盘 / 菜单栏用 `pack/skua-glyph-*.png`。

**HLS 代理 `skua-proxy`**
- aiohttp，监听 7788；`/hls?url=<m3u8>` 返回改写后的播放列表：高置信且整分片被覆盖的分片直接删除并补 `EXT-X-DISCONTINUITY`；部分覆盖的分片保留（由 mpv 脚本处理秒级跳转）。
- 后台任务对新 URL 触发检测管线，首个响应用证据 A 的即时结果，后续刷新播放列表时替换为完整结果（HLS 客户端对 VOD 列表只读一次，因此代理需对未完成检测的源返回 `EXT-X-PLAYLIST-TYPE:EVENT` 并持续追加）。

**在线增量**
- 管线按镜头增量运行，lookahead 为 5 个镜头或 30 秒取大者，Semi-Markov 解码对未来做截断（只对已确定的前缀输出），决策领先播放头 ≥ 20 秒即可。

---

## 4. 仓库布局

```
skua/
  pyproject.toml  ruff.toml
  skua/
    schema.py     # 第 2 章 dataclass、SCHEMA_VERSION、to_json/from_json、FEATURE_SCHEMA
    adapter/      playlist.py  fetch.py  decode.py  timeline.py  proc.py  hls.py(门面)  mp4.py(M4)
    shots/        transnet.py  keyframes.py
    evidence/     structural.py  rule_decoder.py(M0 临时，M3 删除)  fingerprint.py  content.py  ood.py
    model/        emission.py  semimarkov.py  calibrate.py
    train/        pseudo_label.py  dataset.py  train.py  eval.py
    serve/        proxy.py  skiplist.py
    cli.py        # skua scan / eval / train / serve
  mpv/skua.lua
  scripts/        fetch_models.sh  check_secrets.sh  check_imports.sh  selftest_checks.sh  make_synth.py
  models/         # 下载脚本与 SHA256 清单，权重不入库
  golden/         # 人工标注 JSON + PROTOCOL.md
  docs/           progress.md  decisions/ADR-*.md  reports/m<N>-<date>.md
  tests/          每包一个目录 + fixtures/（synth 参数、脱敏的 proc 输出样例）
  work/  cache/   # gitignore
```

---

## 5. 里程碑与验收

| 里程碑 | 交付 | 验收标准 |
|---|---|---|
| M0 时间轴 | HLS adapter、Timeline、播放列表/容器事件、`skua scan` 产出证据 A 区间、最小 `skua eval` | 对 golden 中"整分片广告"子集，区间级 P ≥ 0.95，R ≥ 0.7；单集耗时 < 10s 按热缓存（分片已在 cache/，只计 demux 与分析）计算，冷缓存耗时如实报告但不作门槛 |
| M1 指纹闭环 | 第一项任务：验证 skip list 时间轴与 mpv `time-pos` 的一致性（含 DISCONTINUITY 与起始偏移），误差 > 0.5s 则先修轴再做指纹。随后 pHash + Chromaprint 入库、media_id 别名合并、跨集自匹配、fMP4 支持、mpv 脚本与反馈回流 | 时间轴与 mpv 误差 ≤ 0.5s；同一来源第二部剧的广告零标注命中 R ≥ 0.8；用户手动标记一次后同广告再现命中率 100% |
| M2 内容证据 | TransNetV2、DINOv2、BEATs、OOD、OCR 特征全量落 parquet；伪标签生成 | 单集 45 分钟 CPU 特征提取 ≤ 3 分钟；伪标签在 golden 上 P ≥ 0.9 |
| M3 序列解码 | 发射模型训练、Semi-Markov 解码、校准、消融报告 | golden 全集 `ad` F1 ≥ 0.9，边界 P90 ≤ 1.5s，错跳正片 < 2s/h |
| M4 在线与扩展 | 代理增量模式、MP4/MKV adapter、lookahead 截断解码 | 边播边检决策领先播放头 ≥ 20s；MP4 本地文件复用 M3 模型 F1 下降 ≤ 0.05 |
| M5 可选 | 特征提取迁 Rust（Ambra 承接 embedding 推理），单二进制发布 | 仅在 M3 通过且 Python 端成为瓶颈时启动 |

---

## 6. 已知风险与应对

- 片源无 `DISCONTINUITY` 且广告与正片同编码参数：证据 A 失效，依赖 B 与 C，这正是 B 必须作为主判据的原因。
- 广告每次动态替换（SSAI）：视频指纹失效，但同一广告主的素材池有限，音频指纹与 OOD 仍有效；此场景是 M3 消融要重点看的。
- 片头与广告特征相近：靠 6 类标签 + 位置约束 + 跨集同位置重复规则区分，不要退化为二分类。
- TransNetV2 对动画/低码率源切分不稳：PySceneDetect 兜底，并把"镜头过长（> 60s）"强制按 10s 切分。
- 人脸与 OCR 模块增加依赖体积：两者均为可选证据，`--lite` 模式下关闭，模型需支持特征缺失（训练时随机 mask 这两组特征）。
- 许可证：Skua 自身代码 MIT。PyAV 为 BSD，但其 wheel 捆绑的 FFmpeg 可能是 GPL 构建；v1 作为本地工具由用户自行安装依赖、不再分发二进制，不构成阻塞，结论与核实结果记入 ADR-001。若 M5 做单二进制发布，需改用 LGPL 构建的 FFmpeg。
- SSAI 贴片导致同一集的 media_id 不稳定：media_id 只由 URL 决定（第 2 章），指纹层用 ≥ 50% 覆盖率别名合并（3.4），两道保护缺一不可。

---

## 7. 变更记录

- 2026-10-15 v0.1 草案定稿。
- 2026-10-15 v0.2 依据 M0 执行计划评审回填：media_id 改为纯 URL 派生；ShotFeatures 非 struct 字段可为 None；struct 定为 10 维且事件只归属镜头起点；硬先验改为不连续块级判定并加 180s 上限与"非最大块"约束；URI 签名增加 hex/base64 模板化与 60% 可靠性门槛；duration_outlier 排除末片并定义 MAD=0 回退；video_params 比较解析字段而非 extradata；M0 范围定为 TS + AES-128，fMP4 移至 M1；新增 media_id 别名合并与单集 ad 占比 30% 保护；golden 与 SkipList 同 schema；报告目录 docs/reports/；M0 耗时按热缓存验收；M1 首项为 mpv 时间轴对齐验证；去掉 pydantic 依赖。
- 2026-10-16 v0.3 依据批次 2、3 评审回填：SkipList 只存非 content 区间；payload.unit 在 MP4/MKV 下的取值；URI 模板化改为按分隔符切词（修复 `segment00012` 与 `advert00012` 同模板的漏报）；DATERANGE 时长取 DURATION → END-DATE−START-DATE → PLANNED-DURATION，锚定优先按 PROGRAM-DATE-TIME；硬先验只认 CLASS=ad 与 SCTE35-OUT，SCTE35-IN 用于截断，SCTE35-CMD M0 不作硬先验；BYTERANGE 归入 M1。
