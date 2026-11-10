"""Turn a landscape clip into a 1080x1920 short: blurred fill background, the clip centred,
a headline above it, the creator credit below it, loudness-normalised audio."""
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


def filtergraph(font: str, title_file: str, credit_file: str) -> str:
    fg_h = W * 9 // 16  # a 16:9 clip scaled to full width; other ratios just centre
    text = f"fontfile='{_esc(font)}':fontcolor=white:borderw=6:bordercolor=black:text_align=C:x=(w-text_w)/2"
    return (
        f"[0:v]split[a][b];"
        f"[a]scale={W}:{H}:force_original_aspect_ratio=increase,crop={W}:{H},boxblur=30:5,eq=brightness=-0.15[bg];"
        f"[b]scale={W}:-2[fg];"
        f"[bg][fg]overlay=(W-w)/2:(H-h)/2[v0];"
        f"[v0]drawtext={text}:textfile='{_esc(title_file)}':fontsize=72:line_spacing=14:"
        f"y={(H - fg_h) // 2}-text_h-60[v1];"
        f"[v1]drawtext={text}:textfile='{_esc(credit_file)}':fontsize=44:"
        f"y={(H + fg_h) // 2}+50,format=yuv420p[v]"
    )


def render(src: Path, out: Path, title: str, credit: str, max_duration: int = 60, font: str = "") -> Path:
    out.parent.mkdir(parents=True, exist_ok=True)
    headline = "\n".join(textwrap.wrap(title.strip().upper(), width=20)[:4]) or " "
    with tempfile.TemporaryDirectory() as tmp:
        title_file, credit_file = Path(tmp, "title.txt"), Path(tmp, "credit.txt")
        title_file.write_text(headline, encoding="utf-8")
        credit_file.write_text(credit or " ", encoding="utf-8")
        cmd = [
            "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
            "-i", str(src), "-t", str(max_duration),
            "-filter_complex", filtergraph(find_font(font), str(title_file), str(credit_file)),
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
