# ADR-001：M0 基线依赖

- 状态：已批准
- 日期：2026-10-15
- 起草：实现 Agent
- 批准：项目所有者 2026-10-16（含构建后端 `uv_build`）
- 关联：SPEC 第 1 章、第 6 章"许可证"条；AGENT.md 5.1、10.1；项目所有者 2026-10-15 评审决定（去掉 pydantic、Skua 自身 MIT、v1 不分发二进制）；项目所有者 2026-10-16 批准本 ADR

## 背景

M0 需要建立 `pyproject.toml` 与 `uv.lock`。AGENT.md 5.1 要求每个第三方依赖都有 ADR，写明许可证与是否带原生扩展。AGENT.md 10.1 已列出 M0 依赖清单：运行时 `m3u8`、`av`、`aiohttp`、`numpy`、`pyarrow`、`typer`；开发 `pytest`、`pytest-asyncio`、`ruff`、`mypy`；不引入 pydantic。

本 ADR 把清单落实到版本范围，并核实许可证。SPEC 第 6 章要求把 PyAV 捆绑 FFmpeg 的许可证核实结果写在这里。

约束：
- Python 固定为 3.11（AGENT.md 5.1）。截至 2026-10-15，PyPI 上 `av` 19.x 与 `numpy` 2.5.x 已要求 Python ≥ 3.12，所以必须给它们设上界。
- 本机系统 ffmpeg 为 7.0.2，只带 libopenh264，没有 libx264。这只影响测试素材的合成，不影响依赖选择。

## 决策

### 运行时依赖（`[project] dependencies`）

| 包 | 版本范围 | 用途 | 许可证 | 原生扩展 |
|---|---|---|---|---|
| `m3u8` | `>=6.0,<7` | 解析 master / media playlist（SPEC 3.1） | MIT | 否（纯 Python；无 `py.typed`，已加 mypy override） |
| `av`（PyAV） | `>=18.1,<19` | demux 探测、帧与 PCM 迭代器、容器事件；只允许在 `skua/adapter/decode.py` 中 import | BSD-3-Clause；wheel 捆绑 FFmpeg 等原生库，见下文 | 是（Cython + 捆绑 libav*） |
| `aiohttp` | `>=3.14,<4` | 并发下载（M0），测试中托管合成 HLS（M0），HLS 代理（M1 起） | Apache-2.0 AND MIT | 是（带可选 C 加速，有纯 Python 回退） |
| `numpy` | `>=2.4,<2.5` | `ShotFeatures.struct` 等数组、MAD 统计 | BSD-3-Clause（含 0BSD / MIT / Zlib / CC0 子组件） | 是 |
| `pyarrow` | `>=26,<27` | `ShotFeatures` 写 parquet，`FEATURE_SCHEMA` 校验（AGENT.md 5.2） | Apache-2.0 | 是（26.0.0 无 `py.typed`，已加 mypy override） |
| `typer` | `>=0.27,<0.28` | `skua scan` / `eval` 子命令 | MIT | 否 |

传递依赖（2026-10-16 `uv sync` 解析结果，共 34 个包，以 `uv.lock` 为准）：
- aiohttp：`multidict`、`yarl`、`frozenlist`、`propcache`（Apache-2.0，原生）；`aiosignal`（Apache-2.0）、`attrs`（MIT）、`aiohappyeyeballs`（PSF-2.0）、`idna`（BSD-3-Clause，经 yarl）、`typing-extensions`（PSF-2.0）。
- typer：`annotated-doc`（MIT）、`rich`（MIT）、`shellingham`（ISC）；rich 带入 `markdown-it-py` / `mdurl`（MIT）、`pygments`（BSD-2-Clause）。`colorama`（BSD-3-Clause）只在 Windows 上安装。PyPI 元数据里的 `click` 在本次解析中没有出现（typer 0.27 已内置）。
- 以上均为宽松许可证，没有 copyleft。

### 开发依赖（`[dependency-groups] dev`，不随包发布）

| 包 | 版本范围 | 许可证 | 原生扩展 |
|---|---|---|---|
| `pytest` | `>=9.1,<10` | MIT | 否 |
| `pytest-asyncio` | `>=1.4,<2` | Apache-2.0 | 否 |
| `ruff` | `>=0.17,<0.18` | MIT | 是（Rust 二进制） |
| `mypy` | `>=2.4,<3` | MIT | 否（可选 mypyc 编译 wheel） |

开发依赖的传递依赖：
- mypy：`ast-serialize`（MIT，原生）、`librt`（MIT，原生）、`mypy-extensions`（MIT）、`pathspec`（MPL-2.0）。
- pytest：`iniconfig`、`pluggy`（MIT）、`packaging`（Apache-2.0 OR BSD-2-Clause）、`pygments`。
- `pathspec` 是 MPL-2.0（文件级弱 copyleft），只在开发期使用，不随包发布，不受影响。

### 构建后端

`[build-system] requires = ["uv_build>=0.12,<0.14"]`，Astral 出品，许可证为 MIT OR Apache-2.0。`skua` 命令行入口（`[project.scripts]`）需要把项目作为包安装，所以必须有构建后端。选 uv_build 是因为它与已选定的 uv 同源，不额外引入工具链。

### 许可证

- Skua 自身代码采用 MIT：仓库根目录 `LICENSE`，`pyproject.toml` 中 `license = "MIT"`、`license-files = ["LICENSE"]`。著作权人为 CIPFZ（项目所有者 2026-10-16 指定）。
- 版本上界取当前大版本，升级大版本需要新的 ADR 或修订本 ADR。
- **`av <19` 与 `numpy <2.5` 这两个上界是 SPEC 固定 Python 3.11 的派生约束**，不是对这两个库本身的选型限制：`av` 19.x 与 `numpy` 2.5.x 要求 Python ≥ 3.12。将来升级到 Python 3.12 时，这两个上界一并解除，改回"锁当前大版本"的一般规则。

### PyAV 捆绑 FFmpeg 的许可证核实结果

核实对象：`av-18.1.0-cp311-…-manylinux…x86_64.whl`，2026-10-15 从 PyPI 下载到临时目录解包检查，未安装进项目环境。

1. **wheel 元数据**：`License-Expression: BSD-3-Clause`。这只覆盖 PyAV 自身的绑定代码，附带 `auditwheel` 生成的 CycloneDX SBOM。
2. **捆绑的 FFmpeg 版本**：`av.libs/` 下有 libavcodec 62.28、libavformat 62.12、libavutil 60.26，属于 FFmpeg 8.x 系列。系统 ffmpeg 是 7.0.2（libavcodec 61），两者不同，见"代价"。
3. **FFmpeg 构建配置**：从 libavutil 中提取 configure 字符串，包含 `--enable-version3`，**不含** `--enable-gpl` 或 `--enable-nonfree`。libavcodec / libavutil 自报 `LGPL version 3 or later`。
4. **同包分发的其他库**：
   - **GPL**：`libx264`（GPL-2.0+）、`libx265`（GPL-2.0+）。
   - **LGPL**：`libgnutls`、`libnettle`、`libhogweed`（LGPL / GPL 双许可）、`libgmp`、`libmp3lame`、`libunistring`、`libasound`。
   - **宽松许可证**：`libdav1d`、`libopus`、`libvpx`、`libwebp`、`libSvtAv1Enc`、`libvpl`、`libdrm`、`libxcb*`、`libopencore-amr*`（Apache-2.0）。
5. **结论**：
   - FFmpeg 本体是 LGPL-3.0+ 构建。但 wheel 里同时分发了 GPL 的 x264 / x265 编码器库，所以把这个 wheel 整体当作"可能含 GPL 组件"来对待。
   - Skua 只用解码与 demux，不调用 x264 / x265 编码。
   - v1 是本地工具，依赖由用户经 `uv sync` 从 PyPI 自行安装，Skua 不再分发任何二进制，因此不构成阻塞（与 SPEC 第 6 章一致）。
   - 若 M5 做单二进制发布，必须自建只含 LGPL 组件的 FFmpeg，并去掉 x264 / x265，届时另写 ADR。

### 明确不引入

- pydantic：项目所有者 2026-10-15 评审决定。
- `pytest-aiohttp`：测试服务器直接用 `aiohttp.web` 加 `pytest-asyncio` 实现。
- `requests` / `httpx`：下载统一用 aiohttp。
- `ffmpeg-python` 等 CLI 包装库：外部进程只经 `skua/adapter/proc.py`。
- `types-*` / `pyarrow-stubs` 等 stub 包：缺 stub 的包按 AGENT.md 5.1 在 `[[tool.mypy.overrides]]` 中显式 `ignore_missing_imports`。

## 理由

- 运行时清单与 AGENT.md 10.1 逐项一致，没有新增也没有替换。
- 版本范围的下界取 2026-10-15 时支持 cp311 的最新版。上界锁大版本：一是防止 resolver 选到要求 Python 3.12 的版本，二是防止大版本 API 变化破坏 `mypy --strict`。
- PyAV 选 18.x，这是最后一个支持 Python 3.11 的系列。它捆绑的 FFmpeg 8.x 自带 `crypto:` 协议（AES-128，SPEC 3.1）和 HTTPS（gnutls），M0 不需要再加解密依赖。

## 代价

- **原生扩展**：`av`、`numpy`、`pyarrow`、`aiohttp` 的 C 加速部分、`ruff` 都带原生扩展，依赖 PyPI 上的平台 wheel。已核实 `av` 18.1.0 与 `pyarrow` 26.0.0 在 linux x86_64（manylinux_2_28）、linux aarch64、macOS arm64（`av` 要求 macOS ≥ 14，`pyarrow` 要求 ≥ 12）上都有 cp311 wheel。
- **体积**：
  - `av` wheel 18–36 MB，因平台而异，因为捆绑了 libav*。
  - `pyarrow` wheel 36–54 MB。
  - 只用于本地开发，可以接受。
- **两套 FFmpeg**：PyAV 捆绑的是 FFmpeg 8.x，系统 ffmpeg / ffprobe 是 7.x 或更新。两者在 demux 时间戳上的细节可能不同，影响"镜头时间与 ffprobe 一致（±1 帧）"这条测试（AGENT.md 6.2），已作为 M0 风险 7 跟踪。测试一律以 ffprobe 为真值，出现系统性偏差时如实汇报，不调整容差。
- **GPL 组件**：见上文核实结果。v1 不分发二进制，不阻塞；M5 有约束。
- **Python 3.11 上界**：`av` 与 `numpy` 已出现只支持 3.12 及以上的新版本，3.11 的维护窗口会逐步收窄。是否升级 Python 版本由项目所有者另行决定。

## 影响

- 新增或修改的文件：`pyproject.toml`（依赖、构建后端、mypy / pytest 配置）、`uv.lock`（批准后由 `uv sync` 生成并提交，依赖与锁文件单独一个提交）。
- `[[tool.mypy.overrides]]`（2026-10-16 实测回填）：`m3u8`、`m3u8.*`、`pyarrow`、`pyarrow.*` 设 `ignore_missing_imports`。原因是 pyarrow 26.0.0 不带 `py.typed`，`import pyarrow` 在 `--strict` 下报 `import-untyped`。`av` 18.1.0 自带 `py.typed`，按严格模式检查，不加 override。其余运行时依赖未在 M0 代码中触发缺 stub 报错。
- `scripts/check_secrets.sh` 对 `uv.lock` 单独校验：只允许 `pypi.org` 与 `files.pythonhosted.org` 两个 host。
- 后续里程碑新增依赖（M1 的 `usearch`，M2 的 `torch`、`onnxruntime` 等）各自另起 ADR，不在本 ADR 范围内。
- 回滚：删除对应依赖行并重新 `uv lock`。
