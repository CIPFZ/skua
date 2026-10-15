# 图标资产说明

选定方案 A「叼走分片」（2026-10-15）：贼鸥侧影掠过视频胶片条，叼走一个分片，胶片上留下只剩背景色的缺口。

## 文件

| 路径 | 用途 |
|---|---|
| `skua-icon-1024.png` | 微调后的主图标源图（白底，生成模型输出） |
| `skua-glyph-1024.png` | 单色 glyph 源图（生成模型输出，带假棋盘格，需经脚本抠图） |
| `make_pack.py` | 打包脚本：裁透明 squircle、抠 glyph、产出多尺寸 PNG / `.icns` / `.iconset` / `.ico` |
| `pack/skua.icns` | macOS 应用图标（icp4 / ic11 / icp5 / ic12 / ic07 / ic13 / ic08 / ic14 / ic09 / ic10） |
| `pack/skua.iconset/` | 同上的 Apple `iconutil` 输入目录 |
| `pack/skua.ico` | Windows 图标（16 / 24 / 32 / 48 / 64 / 128 / 256） |
| `pack/skua-icon-{16..1024}.png` | Linux `hicolor` 各尺寸与网页用 PNG，透明背景，含约 10% 边距 |
| `pack/skua-glyph-{16,22,24,32,44,64,128,256}.png` | 纯白透明底单色 glyph，用于 mpv OSD 提示、托盘、菜单栏模板图（macOS 模板图需命名为 `*Template.png`） |
| `pack/_preview-*.png` | 多尺寸预览，仅供核对 |
| `skua-icon-b.png`、`skua-icon-c.png` | 未采用的方案草图，保留备查 |

重新生成：`python3 make_pack.py`（依赖 Pillow、numpy、icnsutil）。

## 色板（已写入 SPEC 3.8 OSD 样式）

| 名称 | 色值 | 含义 |
|---|---|---|
| `navy` | `#0F1B2D` | 背景，也是 OSD 提示框底色 |
| `cream` | `#F1EDE3` | 鸟身主体，OSD 正文 |
| `slate` | `#5C6B7A` | 胶片条与翼影，OSD 次要文字 |
| `coral` | `#F0695A` | 被叼走的分片，OSD 中代表"广告区间"与自动跳过提示 |

## 来源

全部由图像生成模型按文字描述生成并经脚本后处理，无第三方素材，可自由用于本项目。
