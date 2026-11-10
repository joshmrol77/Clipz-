"""Turn a landscape clip into a 1080x1920 short in one of several layouts, with the creator
credit on screen and loudness-normalised audio."""
from __future__ import annotations

import subprocess
import tempfile
import textwrap
from pathlib import Path

W, H = 1080, 1920
FONT_CANDIDATES = [
    "/usr/share/fonts/opentype/inter/InterDisplay-Bold.otf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
    "/System/Library/Fonts/Supplemental/Arial Bold.ttf",
    "C:/Windows/Fonts/arialbd.ttf",
]


def find_font(configured: str = "") -> str:
    for f in [configured, *FONT_CANDIDATES]:
        if f and Path(f).exists():
            return f
    raise RuntimeError("no font found - set [render] font = '/path/to/font.ttf' in config.toml")


def _esc(path: str) -> str:
    """Escape a path for use inside an ffmpeg filter option."""
    return path.replace("\\", "/").replace(":", "\\:").replace("'", "\\'")


LAYOUTS = {
    "framed": "Framed - clip in the middle, headline above, blurred backdrop",
    "full": "Just the clip - fills the whole screen (sides cropped)",
    "split": "Split - clip on top, gameplay video on the bottom half",
}
BACKGROUND_TYPES = (".mp4", ".mov", ".webm", ".mkv")
SAFE_BOTTOM = 300  # keep text clear of the like/comment/caption UI the apps draw over the video


def _fill(w: int, h: int) -> str:
    return f"scale={w}:{h}:force_original_aspect_ratio=increase,crop={w}:{h}"


def filtergraph(layout: str, font: str, title_file: str, credit_file: str) -> str:
    text = f"fontfile='{_esc(font)}':fontcolor=white:borderw=6:bordercolor=black:text_align=C:x=(w-text_w)/2"
    title = f"drawtext={text}:textfile='{_esc(title_file)}':fontsize=72:line_spacing=14"
    credit = f"drawtext={text}:textfile='{_esc(credit_file)}':fontsize=44"
    if layout == "framed":
        fg_h = W * 9 // 16  # a 16:9 clip scaled to full width; other ratios just centre
        return (
            f"[0:v]split[a][b];"
            f"[a]{_fill(W, H)},boxblur=30:5,eq=brightness=-0.15[bg];"
            f"[b]scale={W}:-2[fg];"
            f"[bg][fg]overlay=(W-w)/2:(H-h)/2[v0];"
            f"[v0]{title}:y={(H - fg_h) // 2}-text_h-60[v1];"
            f"[v1]{credit}:y={(H + fg_h) // 2}+50,format=yuv420p[v]"
        )
    if layout == "full":
        return f"[0:v]{_fill(W, H)},{credit}:y={H - SAFE_BOTTOM},format=yuv420p[v]"
    if layout == "split":
        half = H // 2
        return (
            f"[0:v]{_fill(W, half)},setsar=1[top];"
            f"[1:v]{_fill(W, half)},setsar=1,fps=30[bottom];"
            f"[top]fps=30[top30];"
            f"[top30][bottom]vstack=shortest=1[v0];"
            f"[v0]{title}:y={half}-text_h/2[v1];"
            f"[v1]{credit}:y={H - SAFE_BOTTOM},format=yuv420p[v]"
        )
    raise ValueError(f"unknown layout {layout!r}; choose one of {list(LAYOUTS)}")


def render(src: Path, out: Path, title: str, credit: str, max_duration: int = 60, font: str = "",
           layout: str = "framed", background: Path | None = None) -> Path:
    out.parent.mkdir(parents=True, exist_ok=True)
    inputs = ["-i", str(src)]
    if layout == "split":
        if not background or not Path(background).exists():
            raise RuntimeError("the split layout needs a gameplay video - add one first")
        inputs += ["-stream_loop", "-1", "-i", str(background)]  # loop it under clips of any length
    width = 24 if layout == "split" else 20
    headline = "\n".join(textwrap.wrap(title.strip().upper(), width=width)[:4]) or " "
    with tempfile.TemporaryDirectory() as tmp:
        title_file, credit_file = Path(tmp, "title.txt"), Path(tmp, "credit.txt")
        title_file.write_text(headline, encoding="utf-8")
        credit_file.write_text(credit or " ", encoding="utf-8")
        cmd = [
            "ffmpeg", "-y", "-hide_banner", "-loglevel", "error", *inputs, "-t", str(max_duration),
            "-filter_complex", filtergraph(layout, find_font(font), str(title_file), str(credit_file)),
            "-map", "[v]", "-map", "0:a?",
            "-af", "loudnorm=I=-14:TP=-1.5:LRA=11",
            "-r", "30", "-c:v", "libx264", "-preset", "medium", "-crf", "20",
            "-c:a", "aac", "-b:a", "160k", "-ar", "48000", "-movflags", "+faststart",
            str(out),
        ]
        proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        raise RuntimeError(f"ffmpeg failed: {proc.stderr.strip()[-800:]}")
    return out
