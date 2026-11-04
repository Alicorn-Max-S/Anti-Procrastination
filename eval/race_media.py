"""race_media: the performance diagram and the sped-up video, from the log `python -m eval.race` wrote.

    python -m eval.race_media [--speed 8] [--only diagram|video] [--ffmpeg PATH]

Writes eval/performance.png and eval/performance-dark.png (the diagram: numbers only, no test text)
and data/demo/race.mp4 plus race-final.png (the replay: it names each question's labels, so it stays
under data/). The replay is drawn from the timestamps the race logged, not screen-captured: every
question lights up when it really finished, sped up by --speed, with the real clock beside it.
Needs matplotlib (the `eval` extra; it brings Pillow) and, for the video, ffmpeg (--ffmpeg, the
FFMPEG variable, PATH, or imageio_ffmpeg). Brand colors and Segoe UI, the brand's fallback face.
"""

import argparse
import csv
import json
import os
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
RACE = ROOT / "data" / "demo" / "race.json"
VIDEO = ROOT / "data" / "demo" / "race.mp4"
FINAL = ROOT / "data" / "demo" / "race-final.png"
RESULTS = ROOT / "eval" / "results.csv"
FONTS = Path(os.getenv("WINDIR", "C:/Windows")) / "Fonts"
GPU_HOUR = 1.10  # $ per hour: the comparison's rental proxy for Glimmer's GPU time

# docs/brand.md: zinc greys, Clay for warmstart. Glimmer is the one warmstart is built around, so it
# takes Clay; Spark takes a grey.
LIGHT = {"bg": "#FFFFFF", "ink": "#18181B", "muted": "#52525B", "line": "#E4E4E7", "tile": "#F4F4F5",
         "spark": "#52525B", "glimmer": "#B9624A", "red": "#9A2B26", "on_glimmer": "#FFFFFF"}
DARK = {"bg": "#18181B", "ink": "#F4F4F5", "muted": "#A1A1AA", "line": "#3F3F46", "tile": "#27272A",
        "spark": "#A1A1AA", "glimmer": "#CE9180", "red": "#F7756B", "on_glimmer": "#18181B"}


@dataclass
class Lane:
    key: str
    name: str
    sub: str
    rows: list[dict]
    summary: dict

    @property
    def wall(self) -> float:
        return self.summary["wall_seconds"]

    def done(self, t: float) -> list[dict]:
        return [r for r in self.rows if r["end"] <= t]

    def cost(self, t: float) -> float:
        if self.key == "spark":
            return sum(r.get("cost", 0.0) for r in self.done(t))
        return min(t, self.wall) / 3600 * GPU_HOUR


def load() -> tuple[dict, dict[str, Lane]]:
    if not RACE.exists():
        raise SystemExit("No data/demo/race.json yet: run `python -m eval.race` first.")
    race = json.loads(RACE.read_text(encoding="utf-8"))
    subs = {"spark": ("Muse Spark 1.3", "cloud API, one call per question"),
            "glimmer": ("Muse Glimmer 30B", "local on an RTX 3090, thinking off")}
    lanes = {k: Lane(k, *subs[k], race["lanes"][k], race["summary"][k]) for k in ("spark", "glimmer")}
    return race, lanes


def clock(s: float) -> str:
    return f"{int(s // 60)}:{s % 60:04.1f}"


def span(s: float) -> str:
    return f"{s:.0f} s" if s < 60 else f"{int(s // 60)} min {round(s % 60):02d} s"


def pretty(v: str | None) -> str:
    return (v or "?").replace("_", " ")


def nov1_test_questions() -> dict[str, tuple[int, int]]:
    """Format, topic and credit accuracy on the Nov 1 labeled set's test questions, from results.csv."""
    out: dict[str, list[int]] = {}
    if RESULTS.exists():
        for r in csv.DictReader(RESULTS.open(encoding="utf-8")):
            if r["scope"] in ("format", "topic", "credit"):
                d = out.setdefault(r["setup"], [0, 0])
                d[0] += int(r["correct"])
                d[1] += int(r["fields"])
    return {k: (v[0], v[1]) for k, v in out.items()}


# ---- the diagram -----------------------------------------------------------------------------


def diagram(race: dict, lanes: dict[str, Lane], theme: dict, path: Path) -> None:
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from matplotlib import font_manager
    from matplotlib.patches import FancyBboxPatch

    for f in ("segoeui.ttf", "seguisb.ttf", "segoeuib.ttf"):
        if (FONTS / f).exists():
            font_manager.fontManager.addfont(str(FONTS / f))
    plt.rcParams["font.family"] = ["Segoe UI", "DejaVu Sans"]
    c = theme
    sp, gl = lanes["spark"], lanes["glimmer"]
    n = race["questions"]
    faster = sp.wall / gl.wall

    fig = plt.figure(figsize=(13.333, 7.5), dpi=144, facecolor=c["bg"])
    fig.text(0.045, 0.925, "Spark vs Glimmer on one real test", fontsize=30, fontweight="bold", color=c["ink"])
    fig.text(0.045, 0.877, f"{n} questions from a {race['course']} test, each labeled with its format, topic and "
             "credit. Both models ran at the same time, one question at a time.", fontsize=14, color=c["muted"])

    # Left: questions labeled against time.
    ax = fig.add_axes([0.075, 0.205, 0.49, 0.575], facecolor=c["bg"])
    for lane in (sp, gl):
        xs = [0.0] + [r["end"] for r in lane.rows]
        ys = [0] + [r["n"] for r in lane.rows]
        ax.step(xs, ys, where="post", color=c[lane.key], linewidth=3.2, solid_capstyle="round", zorder=3)
        ax.scatter([xs[-1]], [ys[-1]], s=70, color=c[lane.key], zorder=4)
    by_then = len(sp.done(gl.wall))
    ax.axvline(gl.wall, color=c["muted"], linewidth=1.2, linestyle=(0, (4, 4)), zorder=2)
    ax.scatter([gl.wall], [by_then], s=70, color=c["spark"], zorder=4)
    # Notes go in the empty space: above the lines at their ends, and right of the dashed line.
    ax.text(gl.wall + 4, n * 1.065, f"Glimmer: all {n} at {span(gl.wall)}", fontsize=13, color=c["glimmer"],
            va="center", fontweight="bold")
    ax.text(sp.wall, n * 1.065, f"Spark: all {n} at {span(sp.wall)}", fontsize=13, color=c["spark"], ha="right",
            va="center", fontweight="bold")
    ax.annotate(f"When Glimmer finishes,\nSpark has labeled {by_then}.", (gl.wall, by_then + 0.6),
                xytext=(gl.wall + 10, n * 0.5), fontsize=13, color=c["ink"], va="center",
                arrowprops={"arrowstyle": "-", "color": c["muted"], "linewidth": 1})
    ax.set_xlim(0, sp.wall * 1.03)
    ax.set_ylim(0, n * 1.04)
    ax.set_xlabel("Seconds since the start", fontsize=12, color=c["muted"], labelpad=8)
    ax.set_ylabel("Questions labeled", fontsize=12, color=c["muted"], labelpad=8)
    ax.tick_params(colors=c["muted"], labelsize=11, length=0)
    ax.grid(True, color=c["line"], linewidth=0.8, zorder=0)
    for side in ("top", "right", "left", "bottom"):
        ax.spines[side].set_visible(False)

    # Right: the three numbers that matter, one tile each. Offsets are from the tile's bottom edge.
    h = 0.186

    def tile(y: float, title: str, g: tuple[str, str], s: tuple[str, str]) -> None:
        fig.add_artist(FancyBboxPatch((0.615, y), 0.34, h, boxstyle="round,pad=0,rounding_size=0.012",
                                      transform=fig.transFigure, facecolor=c["tile"], edgecolor="none"))
        fig.text(0.63, y + 0.148, title.upper(), fontsize=10.5, fontweight="bold", color=c["muted"])
        for x, (value, note), key, name in ((0.63, g, "glimmer", "Glimmer"), (0.795, s, "spark", "Spark")):
            fig.text(x, y + 0.103, name, fontsize=11.5, fontweight="bold", color=c[key])
            fig.text(x, y + 0.045, value, fontsize=26, fontweight="bold", color=c["ink"])
            fig.text(x, y + 0.015, note, fontsize=9.5, color=c["muted"])

    top = 0.2 + 2 * (h + 0.02)
    tile(top, "Whole test", (span(gl.wall), f"{gl.summary['mean_seconds']:.2f} s a question"),
         (span(sp.wall), f"{sp.summary['mean_seconds']:.1f} s a question"))
    fig.text(0.94, top + 0.148, f"{faster:.1f}× faster", fontsize=12, fontweight="bold", color=c["on_glimmer"],
             ha="right", va="center",
             bbox={"boxstyle": "round,pad=0.35,rounding_size=0.6", "facecolor": c["glimmer"], "edgecolor": "none"})
    tile(top - h - 0.02, "Labels that match Max's",
         (f"{gl.summary['right']} of {gl.summary['labeled_fields']}", f"{gl.summary['accuracy']:.1%}"),
         (f"{sp.summary['right']} of {sp.summary['labeled_fields']}", f"{sp.summary['accuracy']:.1%}"))
    tile(0.2, "Cost", (f"${gl.summary['cost']:.2f}", "GPU time on one card"),
         (f"${sp.summary['cost']:.2f}", "API usage, Standard prices"))

    nov = nov1_test_questions()
    nov_line = ""
    if "All Spark" in nov and "System One, fast pass only" in nov:
        (sc, sn), (gc, gn) = nov["All Spark"], nov["System One, fast pass only"]
        nov_line = (f" On all of Nov 1's labeled test questions ({sn // 3}): Spark {sc / sn:.1%}, "
                    f"Glimmer {gc / gn:.1%}.")
    notes = [
        f"Spark is {race['spark_model']}, one structured call per question. Glimmer is {race['glimmer_model']} through "
        "the decider with thinking off, the way the app runs it.",
        f"Glimmer's cost is GPU seconds at ${GPU_HOUR:.2f} an hour (a rental proxy; on this card it is electricity). "
        f"Spark's is the API's reported usage: {sp.summary['input_tokens']:,} tokens in, {sp.summary['output_tokens']:,} out.",
        "Glimmer's confidence lines were tuned on this labeled set, so its score here is not held out." + nov_line,
    ]
    for i, line in enumerate(notes):
        fig.text(0.045, 0.118 - i * 0.032, line, fontsize=9.6, color=c["muted"])
    fig.text(0.045, 0.018, f"{race['when'][:10]}  ·  warmstart", fontsize=9.6, color=c["muted"])
    fig.savefig(path, facecolor=c["bg"])
    plt.close(fig)
    print(f"Wrote {path.relative_to(ROOT)}")


# ---- the video -------------------------------------------------------------------------------

W, H, FPS, SS = 1920, 1080, 30, 2  # drawn at SS x and scaled down, so the edges are smooth
COLS = 17
_fonts: dict = {}


def font(size: int, weight: str = "regular"):
    from PIL import ImageFont

    key = (size, weight)
    if key not in _fonts:
        name = {"regular": "segoeui.ttf", "semibold": "seguisb.ttf", "bold": "segoeuib.ttf"}[weight]
        path = FONTS / name
        _fonts[key] = ImageFont.truetype(str(path), size * SS) if path.exists() else ImageFont.load_default()
    return _fonts[key]


def rgb(h: str) -> tuple[int, int, int]:
    return tuple(int(h[i:i + 2], 16) for i in (1, 3, 5))  # type: ignore[return-value]


class Canvas:
    """A PIL drawing surface in 1920x1080 units, drawn at SS x."""

    def __init__(self, bg: str) -> None:
        from PIL import Image, ImageDraw

        self.img = Image.new("RGB", (W * SS, H * SS), rgb(bg))
        self.d = ImageDraw.Draw(self.img)

    def rect(self, x0, y0, x1, y1, fill=None, outline=None, radius=0, width=0) -> None:
        box = [x0 * SS, y0 * SS, x1 * SS, y1 * SS]
        kw = {"fill": rgb(fill) if fill else None, "outline": rgb(outline) if outline else None,
              "width": width * SS}
        if radius:
            self.d.rounded_rectangle(box, radius=radius * SS, **kw)
        else:
            self.d.rectangle(box, **kw)

    def text(self, x, y, s, size, color, weight="regular", anchor="la") -> None:
        self.d.text((x * SS, y * SS), s, font=font(size, weight), fill=rgb(color), anchor=anchor)

    def line(self, pts, color, width) -> None:
        self.d.line([(x * SS, y * SS) for x, y in pts], fill=rgb(color), width=int(width * SS), joint="curve")

    def done(self):
        from PIL import Image

        return self.img.resize((W, H), resample=Image.LANCZOS)


def draw_check(cv: Canvas, cx, cy, r, color, width=3) -> None:
    cv.line([(cx - r * 0.55, cy + r * 0.02), (cx - r * 0.12, cy + r * 0.45), (cx + r * 0.6, cy - r * 0.42)], color, width)


def draw_cross(cv: Canvas, cx, cy, r, color, width=3) -> None:
    cv.line([(cx - r * 0.45, cy - r * 0.45), (cx + r * 0.45, cy + r * 0.45)], color, width)
    cv.line([(cx - r * 0.45, cy + r * 0.45), (cx + r * 0.45, cy - r * 0.45)], color, width)


def frame(race: dict, lanes: dict[str, Lane], theme: dict, t: float, speed: float, banner: float) -> "Image":
    """One frame at race time `t` seconds (real, not sped up). `banner` is how far the end card is in (0..1)."""
    c = theme
    cv = Canvas(c["bg"])
    n = race["questions"]
    cv.text(60, 36, "warmstart", 44, c["ink"], "bold")
    cv.text(300, 52, f"Labeling a real graded test: {race['course']}, {n} questions", 28, c["muted"])
    badge = f"{speed:g}× speed"
    cv.rect(1620, 36, 1860, 92, fill=c["glimmer"], radius=28)
    cv.text(1740, 64, badge, 30, c["on_glimmer"], "bold", "mm")

    panels = {"spark": (60, 130, 940, 960), "glimmer": (980, 130, 1860, 960)}
    for key, (x0, y0, x1, y1) in panels.items():
        lane = lanes[key]
        col = c[key]
        cv.rect(x0, y0, x1, y1, fill=c["tile"], radius=22)
        cv.text(x0 + 36, y0 + 28, lane.name, 42, c["ink"], "bold")
        cv.text(x0 + 36, y0 + 82, lane.sub, 26, c["muted"])
        done = lane.done(t)
        finished = len(done) == n
        shown = min(t, lane.wall)
        cv.text(x1 - 36, y0 + 28, clock(shown), 76, col, "bold", "ra")
        cv.text(x1 - 36, y0 + 112, "done" if finished else "real time", 24, col if finished else c["muted"],
                "bold" if finished else "regular", "ra")

        # Progress.
        cv.text(x0 + 36, y0 + 160, f"{len(done)} of {n} questions", 34, c["ink"], "semibold")
        cv.rect(x0 + 36, y0 + 214, x1 - 36, y0 + 230, fill=c["line"], radius=8)
        if done:
            cv.rect(x0 + 36, y0 + 214, x0 + 36 + (x1 - x0 - 72) * len(done) / n, y0 + 230, fill=col, radius=8)

        # One tile per question, in order.
        size, gap = 44, 6
        left = x0 + 36 + ((x1 - x0 - 72) - (COLS * size + (COLS - 1) * gap)) / 2
        for i, r in enumerate(lane.rows):
            tx = left + (i % COLS) * (size + gap)
            ty = y0 + 262 + (i // COLS) * (size + gap)
            ok = all(r["right"].values())
            if r["end"] <= t:
                if ok:
                    cv.rect(tx, ty, tx + size, ty + size, fill=col, radius=9)
                    cv.text(tx + size / 2, ty + size / 2, str(r["n"]), 19, c["on_glimmer"] if key == "glimmer" else c["bg"],
                            "bold", "mm")
                else:
                    cv.rect(tx, ty, tx + size, ty + size, fill=c["bg"], outline=c["red"], radius=9, width=3)
                    cv.text(tx + size / 2, ty + size / 2, str(r["n"]), 19, c["red"], "bold", "mm")
            elif r["start"] <= t:
                cv.rect(tx, ty, tx + size, ty + size, fill=c["bg"], outline=col, radius=9, width=3)
                cv.text(tx + size / 2, ty + size / 2, str(r["n"]), 19, col, "bold", "mm")
            else:
                cv.rect(tx, ty, tx + size, ty + size, fill=c["line"], radius=9)
                cv.text(tx + size / 2, ty + size / 2, str(r["n"]), 19, c["muted"], "regular", "mm")
        # (the grid is 2 rows: 34 questions in 17 columns)

        # The last label this lane gave.
        ly = y0 + 262 + 2 * (size + gap) + 26
        cv.text(x0 + 36, ly, "LAST LABELS", 22, c["muted"], "bold")
        if done:
            r = done[-1]
            cv.text(x0 + 36, ly + 38, f"Question {r['n']}", 30, c["ink"], "semibold")
            for j, f in enumerate(("format", "topic", "credit")):
                if f not in r["answers"]:
                    continue
                yy = ly + 92 + j * 48
                good = r["right"].get(f)
                if good is None:  # a field Max didn't label for this question
                    cv.rect(x0 + 40, yy + 8, x0 + 52, yy + 20, fill=c["muted"], radius=6)
                else:
                    (draw_check if good else draw_cross)(cv, x0 + 48, yy + 15, 13, col if good else c["red"], 4)
                label = pretty(r["answers"][f]) + (" credit" if f == "credit" else "")
                cv.text(x0 + 76, yy, f"{f}", 24, c["muted"])
                cv.text(x0 + 200, yy, label[:34], 28, c["ink"] if good is not False else c["red"], "semibold")

        # Running tally.
        marks = [v for r in done for v in r["right"].values()]
        ty = y1 - 74
        cv.rect(x0 + 36, ty - 14, x1 - 36, ty - 12, fill=c["line"])
        right = f"{sum(marks)} of {len(marks)} labels match" if marks else "waiting for the first one"
        cv.text(x0 + 36, ty, right, 28, c["ink"], "semibold")
        cv.text(x1 - 36, ty, f"${lane.cost(t):.3f} so far", 28, c["muted"], "regular", "ra")

        if finished and key == "glimmer":
            spark_done = len(lanes["spark"].done(lane.wall))
            cv.text(x0 + 36, y1 - 134, f"Spark had labeled {spark_done} of {n} by this point.", 24, col, "semibold")

    if banner > 0:
        sp, gl = lanes["spark"], lanes["glimmer"]
        y = 1080 - 100 + (1 - banner) * 110
        cv.rect(0, y, W, y + 100, fill=c["glimmer"])
        msg = (f"{sp.wall / gl.wall:.1f}× faster  ·  {span(gl.wall)} vs {span(sp.wall)}  ·  "
               f"{gl.summary['right']} vs {sp.summary['right']} of {gl.summary['labeled_fields']} labels match  ·  "
               f"${gl.summary['cost']:.2f} vs ${sp.summary['cost']:.2f}")
        cv.text(W / 2, y + 50, msg, 40, c["on_glimmer"], "bold", "mm")
    return cv.done()


def find_ffmpeg(arg: str | None) -> str:
    for cand in (arg, os.getenv("FFMPEG"), shutil.which("ffmpeg")):
        if cand and (Path(cand).exists() or shutil.which(cand)):
            return cand
    try:
        import imageio_ffmpeg

        return imageio_ffmpeg.get_ffmpeg_exe()
    except ImportError:
        raise SystemExit("No ffmpeg: pass --ffmpeg PATH, set FFMPEG, or `pip install imageio-ffmpeg`.")


def video(race: dict, lanes: dict[str, Lane], speed: float, ffmpeg: str) -> None:
    theme = DARK
    end = max(lane.wall for lane in lanes.values())
    lead, tail = 1.0, 5.0  # seconds held before the race starts and after it ends
    total = lead + end / speed + tail
    frames = int(total * FPS)
    VIDEO.parent.mkdir(parents=True, exist_ok=True)
    cmd = [ffmpeg, "-y", "-loglevel", "error", "-f", "rawvideo", "-pix_fmt", "rgb24", "-s", f"{W}x{H}", "-r", str(FPS),
           "-i", "-", "-c:v", "libx264", "-pix_fmt", "yuv420p", "-crf", "18", "-preset", "medium",
           "-movflags", "+faststart", str(VIDEO)]
    proc = subprocess.Popen(cmd, stdin=subprocess.PIPE)
    last = None
    try:
        for i in range(frames):
            vt = i / FPS
            t = max(0.0, (vt - lead) * speed)
            banner = min(1.0, max(0.0, (vt - (lead + end / speed + 0.6)) / 0.5))
            last = frame(race, lanes, theme, min(t, end), speed, banner)
            proc.stdin.write(last.tobytes())
            if i % 90 == 0:
                print(f"  frame {i}/{frames}", flush=True)
    finally:
        proc.stdin.close()
        proc.wait()
    if proc.returncode:
        raise SystemExit(f"ffmpeg stopped with code {proc.returncode}")
    if last is not None:
        last.save(FINAL)
    print(f"Wrote {VIDEO.relative_to(ROOT)} ({total:.1f} s, {frames} frames) and {FINAL.relative_to(ROOT)}")


def main() -> None:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    ap = argparse.ArgumentParser(description="The performance diagram and the sped-up video from the race log.")
    ap.add_argument("--speed", type=float, default=8.0, help="how many times faster than real time (default 8)")
    ap.add_argument("--only", choices=("diagram", "video"))
    ap.add_argument("--ffmpeg", help="path to ffmpeg")
    args = ap.parse_args()
    race, lanes = load()
    if args.only != "video":
        diagram(race, lanes, LIGHT, ROOT / "eval" / "performance.png")
        diagram(race, lanes, DARK, ROOT / "eval" / "performance-dark.png")
    if args.only != "diagram":
        video(race, lanes, args.speed, find_ffmpeg(args.ffmpeg))


if __name__ == "__main__":
    main()
