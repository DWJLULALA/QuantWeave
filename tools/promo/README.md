# QuantWeave 宣传片制作工具

## v4：问题驱动的技术宣传片

`storyboard-v4.json` 控制完整片，`storyboard-short.json` 控制独立竖屏短片。`render_v4.py` 绘制动态机制图、来源明确的研究摘要，并穿插真实 demo 录屏。图解、实验摘要和冻结界面分别标注。

环境：Python 3.11+、Pillow、NumPy、edge-tts、ffmpeg、ffprobe。素材目录与补录说明见下文。配音工具需要联网；渲染可离线运行。

```bash
python tools/promo/voice.py --work-dir /path/to/promo-work --storyboard tools/promo/storyboard-v4.json
python tools/promo/render_v4.py --work-dir /path/to/promo-work
python tools/promo/voice.py --work-dir /path/to/promo-work --storyboard tools/promo/storyboard-short.json
python tools/promo/render_v4.py --work-dir /path/to/promo-work --portrait --storyboard tools/promo/storyboard-short.json
```

输出 `QuantWeave-v4.mp4`、`QuantWeave-Douyin.mp4`、各自 SRT 和时间轴。使用 `--preview` 先检查图形关键帧；`--only scene_id` 重渲单场，再以 `--assemble-only` 重新合成。UI 录屏须在最终视频内检查，静态预览不载入视频。

v4 默认使用 macOS Hiragino Sans GB（粗体 index 2、正文 index 0）与 Arial Bold。其他系统传入 `--zh-font --zh-bold-index --zh-regular-index --en-font`；单字体文件通常使用 index 0。字体文件不随仓库分发。

配音缓存按文本和 voice/rate/pitch 配置匹配。修改文本或音色配置会重录对应句子。研究数字的来源与验证状态见 [RESEARCH_EVIDENCE.md](../../docs/RESEARCH_EVIDENCE.md)。程序内的 RankIC 展示数据应与该文档和 CSV 同步修改。

## 保留的 v3 工程


v3 用一轮研究流程串起数据、因子、结构、回放、模型、模拟账户和日志，配男声、实录操作、独立字幕区。所有画面都来自 demo；不改数字，不模拟鼠标事件以外的业务结果。

大文件放在仓库外，通过 `--work-dir` 传入。当前编辑决策在 `storyboard.json`，包括裁切位置与源视频时间。重建需要这份决策对应的素材包；本仓库不包含原始录屏。

## 环境

- Python 3.10+，`Pillow`、`numpy`、`edge-tts`
- `ffmpeg` 和 `ffprobe`
- 补录时需要 Node.js、`playwright` 和 Chromium
- 默认字体使用 macOS 系统字体；其他系统用 `--zh-font --zh-index --en-font --en-bold` 指定已安装字体

## 目录

```text
promo-work/
  source-v2/qv2_clips/   # 原有无字幕实录镜头；home/data/factors/trends/intraday 等
  raw-v3/               # 补录的 replay.webm、model.webm、evidence.webm
  audio/                # 男声 WAV/MP3 与句子时间信息
  graphics/             # 标题、字幕及片尾图层（自动生成）
  rendered/             # 裁切镜头与章节（自动生成）
  QuantWeave-v3.mp4     # 1080p / 30 fps / AAC 48 kHz stereo
  QuantWeave-v3.srt     # 独立字幕
  timeline.json         # 完整编辑时间轴
```

## 合成

在项目根目录执行；`/path/to/promo-work` 是本机素材工作目录：

```bash
python tools/promo/voice.py --work-dir /path/to/promo-work
python tools/promo/render.py --work-dir /path/to/promo-work
```

旁白文本改变后自动重录对应句子。画面微调可只重建指定章节，再重新合成：

```bash
python tools/promo/render.py --work-dir /path/to/promo-work --only replay,evidence
python tools/promo/render.py --work-dir /path/to/promo-work --assemble-only
```

剪辑以 `storyboard.json` 为输入，实录按章节时长适度调整速度，剪掉加载/等待，输出并保留编辑时间轴。标题和字幕使用 PNG 图层，不依赖 libass。配乐由固定音符和合成器生成，无外部音乐素材。

## 补录

先启动 `python demo_server.py --port 7072`，再执行：

```bash
node tools/promo/record.cjs --work-dir /path/to/promo-work --base-url http://127.0.0.1:7072/ --scenes cases,model,runs
```

可以用 `CHROMIUM_PATH` 指定现有 Chromium。`recordings.json` 会记录原视频和操作时间点；新录制的时序会有差异，需要将所选片段和裁切更新到 `storyboard.json` 后再合成。此补录命令只读取日模型结果、回放案例和查看日志，不修改纸上账户。

## 验证

```bash
ffprobe -v error -show_entries format=duration,size:stream=codec_name,width,height,r_frame_rate,sample_rate,channels -of json /path/to/promo-work/QuantWeave-v3.mp4
ffmpeg -v error -i /path/to/promo-work/QuantWeave-v3.mp4 -f null -
```

视觉验收要检查每个关键动作前后：输入和按钮是否在裁切内、结果是否对应操作、字幕是否遮挡内容、是否残留 spinner。音频验收检查响度和峰值、对白是否完整，以及字幕与句子时点。最终剪辑时长由旁白和镜头共同计算，不能只按脚本字数估算。
