"""Turn a landscape clip into a 1080x1920 short in one of several layouts, with the creator
credit on screen and loudness-normalised audio."""
from __future__ import annotations

import functools
import os
import shutil
import subprocess
import tempfile
import textwrap
from pathlib import Path

W, H = 1080, 1920
FONT_CANDIDATES = [
    str(Path(__file__).parent / "fonts" / "InterDisplay-Bold.otf"),  # shipped with Clipz
    "/usr/share/fonts/opentype/inter/InterDisplay-Bold.otf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
    "/System/Library/Fonts/Supplemental/Arial Bold.ttf",
    "C:/Windows/Fonts/arialbd.ttf",
]


@functools.cache
def ffmpeg() -> str:
    """The ffmpeg program to use: CLIPZ_FFMPEG if set, else the copy that ships inside the
    imageio-ffmpeg package (a known-good build, and the only option on Vercel), else the system one."""
    if os.environ.get("CLIPZ_FFMPEG"):
        return os.environ["CLIPZ_FFMPEG"]
    try:
        import imageio_ffmpeg
        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:
        found = shutil.which("ffmpeg")
        if not found:
            raise RuntimeError("ffmpeg not found - pip install imageio-ffmpeg, or install ffmpeg 6.1+")
        return found


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
    "hype": "Viral edit - fills the screen, punches in with a bass hit and flash on the loudest moments, hook on top",
}
BACKGROUND_TYPES = (".mp4", ".mov", ".webm", ".mkv")
# Cloud functions get one CPU and five minutes, so encode faster there at a slightly larger file size.
PRESET = os.environ.get("CLIPZ_X264_PRESET") or ("veryfast" if os.environ.get("VERCEL") else "medium")
SAFE_BOTTOM = 300  # keep text clear of the like/comment/caption UI the apps draw over the video


def _fill(w: int, h: int) -> str:
    return f"scale={w}:{h}:force_original_aspect_ratio=increase,crop={w}:{h}"


TITLE_SIZE = 72


def headline(font: str, title_files: list[str], y_top: str, size: int = TITLE_SIZE, box: bool = False) -> str:
    """drawtext filters for a multi-line headline, one per line, each centred on its own (works on
    any ffmpeg; the text_align option only exists in newer builds). y_top is an expression."""
    line_h = int(size * 1.2)
    style = (f"fontfile='{_esc(font)}':fontcolor=white:borderw=6:bordercolor=black:expansion=none:fontsize={size}"
             f":x=(w-text_w)/2" + (":box=1:boxcolor=black@0.45:boxborderw=18" if box else ""))
    return ",".join(f"drawtext={style}:textfile='{_esc(f)}':y=({y_top})+{i * line_h}" for i, f in enumerate(title_files))


def headline_height(title_files: list[str], size: int = TITLE_SIZE) -> int:
    return len(title_files) * int(size * 1.2)


def loud_moments(src: Path, max_duration: int, count: int = 3, gap: float = 4.0) -> list[float]:
    """Times (seconds) of the clip's loudest moments - usually the big reactions. Measures the
    audio level every half second and keeps the peaks that clearly stand out."""
    cmd = [ffmpeg(), "-hide_banner", "-nostats", "-t", str(max_duration), "-i", str(src), "-vn", "-af",
           "aresample=8000,asetnsamples=n=4000,astats=metadata=1:reset=1,"
           "ametadata=mode=print:key=lavfi.astats.Overall.RMS_level:file=-", "-f", "null", "-"]
    out = subprocess.run(cmd, capture_output=True, text=True).stdout
    levels, t = [], None
    for line in out.splitlines():
        if "pts_time:" in line:
            t = float(line.rsplit("pts_time:", 1)[1].split()[0])
        elif "RMS_level=" in line and t is not None:
            value = line.split("=", 1)[1].strip()
            if value not in ("-inf", "inf", "nan"):
                levels.append((t, float(value)))
    usable = [(t, db) for t, db in levels if t >= 0.8]
    if not usable:
        return []
    from statistics import median
    typical = median(db for _, db in usable)
    picks: list[float] = []
    for t, db in sorted(usable, key=lambda x: -x[1]):
        if len(picks) >= count:
            break
        if db < typical + 4 and picks:          # after the loudest one, only clear standouts
            break
        if all(abs(t - p) >= gap for p in picks):
            picks.append(t)
    return sorted(picks)


def hype_graph(font: str, title_files: list[str], credit_file: str, peaks: list[float], has_audio: bool) -> str:
    """Viral edit: fill the screen, punch in + flash + bass hit on each peak, hook headline on top."""
    text = f"fontfile='{_esc(font)}':fontcolor=white:borderw=7:bordercolor=black:expansion=none:x=(w-text_w)/2"
    # zoompan works on every ffmpeg version (scale with a time expression needs ffmpeg 5+); it counts
    # output frames, so time = on/30.
    zoom = "+".join(f"0.22*gte(on/30,{p:.2f})*max(0,1-(on/30-{p:.2f})/0.6)" for p in peaks) or "0"
    flashes = "".join(f",drawbox=x=0:y=0:w=iw:h=ih:color=white@0.5:t=fill:enable='between(t,{p:.2f},{p + 0.07:.2f})'"
                      for p in peaks)
    graph = (
        f"[0:v]fps=30,{_fill(W, H)},setsar=1,"
        f"zoompan=z='1+{zoom}':x='iw/2-iw/zoom/2':y='ih/2-ih/zoom/2':d=1:s={W}x{H}:fps=30{flashes},"
        f"{headline(font, title_files, str(int(H * 0.11)), 76, box=True)},"
        f"drawtext={text}:textfile='{_esc(credit_file)}':fontsize=40:y={H - SAFE_BOTTOM},format=yuv420p[v]"
    )
    if has_audio:
        booms = "".join(
            f";aevalsrc='0.95*sin(2*PI*(85-45*t)*t)*exp(-5.5*t)':d=0.9:s=48000,"
            f"aformat=channel_layouts=stereo,adelay={int(p * 1000)}|{int(p * 1000)}[b{i}]" for i, p in enumerate(peaks))
        mix_in = "[m]" + "".join(f"[b{i}]" for i in range(len(peaks)))
        graph += (f";[0:a]aresample=48000,aformat=channel_layouts=stereo[m]{booms}"
                  f";{mix_in}amix=inputs={len(peaks) + 1}:duration=first:dropout_transition=0,volume={len(peaks) + 1},"
                  f"loudnorm=I=-14:TP=-1.5:LRA=11[a]")   # amix divides by the input count; volume undoes it (works on ffmpeg 4.x)
    return graph


def filtergraph(layout: str, font: str, title_files: list[str], credit_file: str) -> str:
    text = f"fontfile='{_esc(font)}':fontcolor=white:borderw=6:bordercolor=black:expansion=none:x=(w-text_w)/2"
    credit = f"drawtext={text}:textfile='{_esc(credit_file)}':fontsize=44"
    title_h = headline_height(title_files)
    if layout == "framed":
        fg_h = W * 9 // 16  # a 16:9 clip scaled to full width; other ratios just centre
        return (
            f"[0:v]split[a][b];"
            f"[a]{_fill(W, H)},boxblur=30:5,eq=brightness=-0.15[bg];"
            f"[b]scale={W}:-2[fg];"
            f"[bg][fg]overlay=(W-w)/2:(H-h)/2[v0];"
            f"[v0]{headline(font, title_files, str((H - fg_h) // 2 - 60 - title_h))}[v1];"
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
            f"[v0]{headline(font, title_files, str(half - title_h // 2))}[v1];"
            f"[v1]{credit}:y={H - SAFE_BOTTOM},format=yuv420p[v]"
        )
    raise ValueError(f"unknown layout {layout!r}; choose one of {list(LAYOUTS)}")


def _has_audio(src: Path) -> bool:
    probe = subprocess.run([ffmpeg(), "-hide_banner", "-i", str(src)], capture_output=True, text=True).stderr
    return "Audio:" in probe


def render(src: Path, out: Path, title: str, credit: str, max_duration: int = 60, font: str = "",
           layout: str = "framed", background: Path | None = None) -> Path:
    out.parent.mkdir(parents=True, exist_ok=True)
    inputs = ["-i", str(src)]
    if layout == "split":
        if not background or not Path(background).exists():
            raise RuntimeError("the split layout needs a gameplay video - add one first")
        inputs += ["-stream_loop", "-1", "-i", str(background)]  # loop it under clips of any length
    width = 24 if layout == "split" else 20
    lines = textwrap.wrap(title.strip().upper(), width=width)[:4] or [" "]
    with tempfile.TemporaryDirectory() as tmp:
        title_files = []
        for i, line in enumerate(lines):
            f = Path(tmp, f"title{i}.txt")
            f.write_text(line, encoding="utf-8")
            title_files.append(str(f))
        credit_file = Path(tmp, "credit.txt")
        credit_file.write_text(credit or " ", encoding="utf-8")
        if layout == "hype":
            peaks = loud_moments(src, max_duration)
            has_audio = bool(peaks) or _has_audio(src)
            graph = hype_graph(find_font(font), title_files, str(credit_file), peaks, has_audio)
            audio = ["-map", "[a]"] if has_audio and peaks else ["-map", "0:a?", "-af", "loudnorm=I=-14:TP=-1.5:LRA=11"]
        else:
            graph = filtergraph(layout, find_font(font), title_files, str(credit_file))
            audio = ["-map", "0:a?", "-af", "loudnorm=I=-14:TP=-1.5:LRA=11"]
        cmd = [
            ffmpeg(), "-y", "-hide_banner", "-loglevel", "error", *inputs, "-t", str(max_duration),
            "-filter_complex", graph,
            "-map", "[v]", *audio,
            "-r", "30", "-c:v", "libx264", "-preset", PRESET, "-crf", "20",
            "-c:a", "aac", "-b:a", "160k", "-ar", "48000", "-movflags", "+faststart",
            str(out),
        ]
        proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        raise RuntimeError(f"ffmpeg failed: {proc.stderr.strip()[-800:]}")
    return out
