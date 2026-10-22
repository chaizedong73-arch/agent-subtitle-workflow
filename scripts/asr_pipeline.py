#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
通用音视频转写流水线：链接 / 本地文件 -> 音频 -> 云端 ASR -> 纯文本

设计目标
--------
1. **只做转写**：不写笔记、不碰任何知识库、不依赖特定目录结构。输出就是一个 .txt。
2. **视频源可换**：下载层用 yt-dlp，只要能拿到音频的站点都能试；某站不通就去改
   `resolve_source()`，见 reference/porting.md。
3. **平台特定逻辑尽量薄**：B 站 / 抖音各有自己的坑，放在 workflows/ 下单独写。

依赖
----
    pip install yt-dlp imageio-ffmpeg

运行
----
    export ARK_API_KEY=...            # 火山方舟 API Key
    python asr_pipeline.py <URL或文件> [-o out.txt] [--lang zh] [--seg 240]

    # 可选：本地已有音频/视频文件
    python asr_pipeline.py ./meeting.m4a -o meeting.txt

    # 只用本地 Whisper，不调云端（需自行安装 faster-whisper）
    python asr_pipeline.py ./a.mp3 --backend whisper

费用说明
--------
云端按音频 token 计费，实际金额取决于所配模型和你账号的价目，脚本只打印 token 数，
不估算金额——报价请以服务商当时的官方价为准。
"""
import argparse
import base64
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

SEG_LEN = 240          # 单段秒数。240s 的 16k/mono/16bit wav 编码后约 9.8MB，多数服务端吃得住
SEG_GAP = 15           # 段间冷却秒数，避免触发限流
MAX_RETRY = 6
BACKOFF = 20

ASR_PROMPT = (
    "请识别音频中的语音内容，并按以下要求整理成易读文本：\n"
    "1）补全并规范中文标点（逗号、句号、问号、叹号等），让句子清晰可读；\n"
    "2）按说话的自然停顿和语义进行断句，不要一长串连写；\n"
    "3）根据内容的话题推进，将文本分成若干段落，段落之间用空行隔开；\n"
    "4）视频中出现的原文外语（如英文台词）请保留原句，并在其后用括号附上中文含义；\n"
    "5）忠实转写，不要总结、不要省略原意，尽量逐句还原。\n"
    "直接以纯文本返回即可。"
)


# ---------------------------------------------------------------- 工具函数

def get_ffmpeg() -> str:
    """优先用 imageio-ffmpeg 自带的二进制；PATH 里没有 ffmpeg 也不会挂。"""
    try:
        import imageio_ffmpeg
        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:
        return "ffmpeg"


def run(cmd, **kw):
    return subprocess.run(cmd, check=True, capture_output=True, **kw)


def get_duration(media: Path) -> float:
    """用 ffprobe（或 ffmpeg -i 的 stderr）取时长，拿不到返回 0。"""
    ff = get_ffmpeg()
    probe = str(Path(ff).with_name("ffprobe" + (".exe" if os.name == "nt" else "")))
    try:
        out = run([probe, "-v", "error", "-show_entries", "format=duration",
                   "-of", "default=nw=1:nk=1", str(media)]).stdout
        return float(out.decode("utf-8", "replace").strip() or 0)
    except Exception:
        pass
    try:
        p = subprocess.run([ff, "-i", str(media)], capture_output=True)
        import re
        m = re.search(rb"Duration: (\d+):(\d+):(\d+\.\d+)", p.stderr)
        if m:
            h, mnt, s = m.groups()
            return int(h) * 3600 + int(mnt) * 60 + float(s)
    except Exception:
        pass
    return 0.0


# ---------------------------------------------------------------- 下载

def download_media(url: str, workdir: Path) -> Path:
    """下载最佳音频轨。yt-dlp 支持大量站点；不支持时这里会抛错，转到 porting.md。"""
    out_tmpl = str(workdir / "src.%(ext)s")
    cmd = [sys.executable, "-m", "yt_dlp",
           "-f", "bestaudio/best",
           "--no-playlist",              # 单集优先，合集请自己循环
           "-o", out_tmpl,
           "--no-progress",
           url]
    # 需要登录的站点（如部分 B 站视频）可传 cookie 文件：
    #   export YTDLP_COOKIES=/path/to/cookies.txt
    cookie = os.environ.get("YTDLP_COOKIES")
    if cookie and Path(cookie).exists():
        cmd += ["--cookies", cookie]
    run(cmd)
    files = sorted(workdir.glob("src.*"))
    if not files:
        raise RuntimeError("下载完成但没有找到媒体文件，检查 yt-dlp 输出")
    return files[0]


def extract_audio(media: Path, workdir: Path) -> Path:
    """抽成 16kHz / 单声道 / 16bit wav —— 语音识别通用且体积小。"""
    wav = workdir / "audio.wav"
    run([get_ffmpeg(), "-y", "-i", str(media),
         "-vn", "-ac", "1", "-ar", "16000", "-sample_fmt", "s16",
         str(wav)])
    return wav


# ---------------------------------------------------------------- 切段

def split_audio(wav: Path, workdir: Path, seg_len: int) -> list:
    dur = get_duration(wav)
    if dur <= 0:
        raise RuntimeError("取不到音频时长，无法切段")
    if dur <= seg_len:
        return [(0.0, wav)]
    ff = get_ffmpeg()
    segs, start, idx = [], 0.0, 0
    while start < dur:
        seg = workdir / f"seg_{idx:03d}.wav"
        # 用 -ss/-t + aresample 保证每段独立可解，不用 -acodec copy（会带入容器依赖）
        run([ff, "-y", "-ss", str(start), "-t", str(seg_len), "-i", str(wav),
             "-vn", "-ac", "1", "-ar", "16000", "-sample_fmt", "s16", str(seg)])
        segs.append((start, seg))
        start += seg_len
        idx += 1
    return segs


# ---------------------------------------------------------------- 云端 ASR

def ark_transcribe(seg_path: Path, model: str, base_url: str, api_key: str) -> str:
    """单段 wav -> 文本。走 OpenAI 兼容的 chat/completions + input_audio 通道。"""
    b64 = base64.b64encode(Path(seg_path).read_bytes()).decode("ascii")
    url = base_url.rstrip("/") + "/chat/completions"
    body = {
        "model": model,
        # 关键优化：关掉思考。开思考时模型会先推理，转写任务白白多花 40% 左右 token，
        # 文本相似度几乎不变。各家参数名不同，不支持就删掉这一行。
        "thinking": {"type": "disabled"},
        "messages": [{
            "role": "user",
            "content": [
                {"type": "input_audio", "input_audio": {"data": b64, "format": "wav"}},
                {"type": "text", "text": ASR_PROMPT},
            ],
        }],
    }
    tmp = Path(tempfile.mkstemp(suffix=".json")[1])
    tmp.write_text(json.dumps(body, ensure_ascii=False), encoding="utf-8")
    cmd = ["curl", "-sS", "--fail", "--connect-timeout", "15", "--max-time", "900",
           "-X", "POST", url,
           "-H", f"Authorization: Bearer {api_key}",
           "-H", "Content-Type: application/json",
           "-d", f"@{tmp}"]
    proxy = os.environ.get("HTTPS_PROXY") or os.environ.get("HTTP_PROXY")
    cmd += ["--proxy", proxy] if proxy else ["--noproxy", "*"]

    last = "未知错误"
    try:
        for attempt in range(1, MAX_RETRY + 1):
            try:
                # 不要用 subprocess 的 text=True：curl 的错误输出走系统代码页（中文 Windows 是 GBK），
                # 按 UTF-8 解码会在 reader 线程抛 UnicodeDecodeError，把真实报错吞成空串。
                p = subprocess.run(cmd, capture_output=True, timeout=600)
                stdout = p.stdout.decode("utf-8", "replace")
                if p.returncode != 0:
                    err = p.stderr.decode("utf-8", "replace") or stdout
                    last = err[:400]
                    raise RuntimeError(last)
                data = json.loads(stdout)
                text = (data.get("choices", [{}])[0]
                            .get("message", {}).get("content", "") or "").strip()
                if text:
                    usage = data.get("usage") or {}
                    if usage:
                        print(f"      tokens: prompt={usage.get('prompt_tokens')} "
                              f"completion={usage.get('completion_tokens')} "
                              f"total={usage.get('total_tokens')}")
                    return text
                last = f"返回空内容: {stdout[:300]}"
                raise RuntimeError(last)
            except Exception as e:  # noqa: BLE001
                msg = str(e)
                if attempt >= MAX_RETRY:
                    raise RuntimeError(f"重试耗尽: {msg}") from e
                wait = BACKOFF * (2 ** (attempt - 1)) if "429" in msg else BACKOFF
                tag = "限流" if "429" in msg else "失败"
                print(f"      [{tag}] {msg[:180]} | {wait}s 后重试 ({attempt}/{MAX_RETRY})")
                time.sleep(wait)
        raise RuntimeError(last)
    finally:
        tmp.unlink(missing_ok=True)


# ---------------------------------------------------------------- 本地 ASR

def whisper_transcribe(wav: Path, model_size: str = "small") -> str:
    """可选后路：不联网。需要 pip install faster-whisper。"""
    try:
        from faster_whisper import WhisperModel
    except ImportError as e:
        raise SystemExit("未安装 faster-whisper，先 pip install faster-whisper") from e
    m = WhisperModel(model_size, device="auto", compute_type="auto")
    segments, _ = m.transcribe(str(wav), vad_filter=True)
    return "".join(s.text for s in segments).strip()


# ---------------------------------------------------------------- 主流程

def main() -> int:
    ap = argparse.ArgumentParser(description="音视频 -> 文本（通用转写流水线）")
    ap.add_argument("source", help="视频链接，或本地音频/视频文件路径")
    ap.add_argument("-o", "--out", help="输出 txt 路径，默认与源同名")
    ap.add_argument("--backend", choices=["ark", "whisper"], default="ark")
    ap.add_argument("--model", default=os.environ.get("ARK_ASR_MODEL", ""),
                    help="ASR 模型 ID；不填则读 ARK_ASR_MODEL")
    ap.add_argument("--base-url", default=os.environ.get(
        "ARK_BASE_URL", "https://ark.cn-beijing.volces.com/api/v3"))
    ap.add_argument("--seg", type=int, default=SEG_LEN, help="单段秒数")
    ap.add_argument("--keep-wav", action="store_true", help="保留中间音频便于排查")
    args = ap.parse_args()

    src = args.source
    is_url = src.startswith(("http://", "https://"))
    workdir = Path(tempfile.mkdtemp(prefix="asr_"))
    try:
        if is_url:
            print(f"[1/4] 下载: {src}")
            media = download_media(src, workdir)
        else:
            media = Path(src)
            if not media.exists():
                raise SystemExit(f"文件不存在: {media}")
            print(f"[1/4] 本地文件: {media}")

        print("[2/4] 抽取音频（16k / mono / s16）")
        wav = extract_audio(media, workdir)

        print("[3/4] 切段")
        segs = split_audio(wav, workdir, args.seg)
        print(f"      共 {len(segs)} 段，单段 {args.seg}s")

        if args.backend == "whisper":
            print("[4/4] 本地 Whisper 转写")
            text = whisper_transcribe(wav)
        else:
            key = os.environ.get("ARK_API_KEY", "")
            if not key:
                raise SystemExit("缺少 ARK_API_KEY 环境变量（或改用 --backend whisper）")
            model = args.model or "doubao-seed-2-0-mini-260428"
            print(f"[4/4] 云端 ASR（{model}）")
            parts = []
            for i, (start, seg) in enumerate(segs, 1):
                print(f"      段 {i}/{len(segs)} @ {int(start)}s")
                parts.append(ark_transcribe(seg, model, args.base_url, key))
                if i < len(segs):
                    time.sleep(SEG_GAP)
            text = "\n\n".join(p for p in parts if p).strip()

        out = Path(args.out) if args.out else Path(
            (Path(src).stem if not is_url else "transcript") + ".txt")
        out.write_text(text, encoding="utf-8")
        print(f"[完成] {len(text)} 字 -> {out.resolve()}")
        print("       转写结果未经人工校对，人名、术语、数字建议对照原音抽查。")
        return 0
    finally:
        if args.keep_wav:
            print(f"[保留] 中间文件在 {workdir}")
        else:
            shutil.rmtree(workdir, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
