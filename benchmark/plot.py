#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = ["matplotlib==3.10.8", "adjustText==1.3.0"]
# ///
"""Plot successful JOB execution-time comparisons; no database or JEV calls."""
import argparse
import csv
import json
import math
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.ticker import FuncFormatter
from adjustText import adjust_text
import numpy as np


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--result-dir", type=Path, default=Path(__file__).resolve().parent / "result")
    parser.add_argument("--labels", type=int, default=5, help="Label this many largest speedups and slowdowns each")
    args = parser.parse_args()
    if args.labels < 0:
        parser.error("--labels must be nonnegative")
    with (args.result_dir / "exectime.csv").open(newline="") as stream:
        rows = [row for row in csv.DictReader(stream)
                if row["status"] == "ok" and row["results_match"].lower() == "true"]
    if not rows:
        parser.error("No successful comparisons in exectime.csv")
    modes = {row["jev_mode"] for row in rows}
    if len(modes) != 1:
        parser.error("Do not mix live and mock measurements in one plot")
    mode = next(iter(modes))
    points = []
    for row in rows:
        x, y = float(row["jev_execution_ms"]), float(row["pg_execution_ms"])
        if not all(math.isfinite(value) and value > 0 for value in (x, y)):
            parser.error(f"{row['query_id']}: log axes require positive finite execution times")
        points.append((row["query_id"], x, y, y / x))
    faster = sorted((point for point in points if point[3] > 1), key=lambda p: -p[3])
    slower = sorted((point for point in points if point[3] < 1), key=lambda p: p[3])
    tied = [point for point in points if point[3] == 1]
    blue, orange, ink = "#176c9c", "#bc5829", "#27384b"
    plt.rcParams.update({"font.family": "DejaVu Sans", "font.size": 11,
                         "axes.labelcolor": ink, "text.color": ink,
                         "xtick.color": ink, "ytick.color": ink,
                         "svg.fonttype": "none", "svg.hashsalt": "pg-jev-job-speedup"})
    fig, ax = plt.subplots(figsize=(10.5, 10))
    fig.subplots_adjust(left=0.12, right=0.95, bottom=0.16, top=0.87)
    ax.set_xscale("log")
    ax.set_yscale("log")
    low = min(min(point[1:3]) for point in points) / 2
    high = max(max(point[1:3]) for point in points) * 2
    ax.set_xlim(low, high)
    ax.set_ylim(low, high)
    ax.set_aspect("equal", adjustable="box")
    domain = np.geomspace(low, high, 300)
    ax.fill_between(domain, domain, high, color=blue, alpha=0.035, zorder=0)
    ax.fill_between(domain, low, domain, color=orange, alpha=0.035, zorder=0)
    ax.plot(domain, domain, color="#677789", linestyle="--", linewidth=1.2,
            label="Equal execution time (y = x)", zorder=1)
    ax.grid(which="major", color="#dce1e6", linewidth=0.65, zorder=0)
    ax.tick_params(which="minor", length=2, color="#b5bec7")
    ax.xaxis.set_major_formatter(FuncFormatter(lambda value, _: f"{value:g}"))
    ax.yaxis.set_major_formatter(FuncFormatter(lambda value, _: f"{value:g}"))
    for group, color, label in ((faster, blue, f"JEV faster: {len(faster)} queries"),
                                (slower, orange, f"PostgreSQL faster: {len(slower)} queries"),
                                (tied, "#7d8895", f"Equal: {len(tied)} queries")):
        if group:
            ax.scatter([p[1] for p in group], [p[2] for p in group], s=45, color=color,
                       edgecolors="white", linewidths=0.6, alpha=0.85, label=label, zorder=3)
    ax.set_xlabel("JEV execution time (ms, log scale)", labelpad=12)
    ax.set_ylabel("PostgreSQL execution time (ms, log scale)", labelpad=12)
    ax.text(0.035, 0.69, "JEV faster\nabove the diagonal", transform=ax.transAxes,
            color=blue, fontsize=12, linespacing=1.6)
    ax.text(0.43, 0.055, "PostgreSQL faster\nbelow the diagonal", transform=ax.transAxes,
            color=orange, fontsize=12, linespacing=1.6)
    ax.legend(loc="upper left", frameon=True, facecolor="white", edgecolor="#dce1e6", fontsize=10)
    fig.suptitle("JEV vs PostgreSQL: execution time", x=0.5, y=0.965, fontsize=20, weight="bold")
    fig.text(0.5, 0.925, f"{len(points)} successful JOB comparisons  ·  {mode} JEV  ·  execution only",
             ha="center", color="#637183", fontsize=11)
    texts = []
    annotated = faster[:args.labels] + slower[:args.labels]
    for name, x, y, ratio in annotated:
        gain = ratio > 1
        factor = ratio if gain else 1 / ratio
        label = f"{name} · {factor:.1f}× {'faster' if gain else 'slower'}"
        ax.scatter([x], [y], s=78, facecolors="none", edgecolors=blue if gain else orange,
                   linewidths=1.2, zorder=4)
        label_x = x if gain else x / 1.8
        texts.append(ax.text(label_x, y, label, ha="left" if gain else "right", color=blue if gain else orange, fontsize=9,
                             bbox=dict(boxstyle="round,pad=0.25", facecolor="white", edgecolor="none", alpha=0.94), zorder=5))
    if texts:
        np.random.seed(0)
        adjust_text(texts, x=[p[1] for p in points], y=[p[2] for p in points],
                    target_x=[p[1] for p in annotated], target_y=[p[2] for p in annotated],
                    ax=ax, expand=(1.15, 1.5), force_text=(0.6, 0.8),
                    force_static=(0.2, 0.4), iter_lim=600,
                    arrowprops=dict(arrowstyle="-", color="#7d8895", lw=0.8), min_arrow_len=3)
    # Leave padding inside the axes for label boxes (not just text extents).
    fig.canvas.draw()
    renderer = fig.canvas.get_renderer()
    edge = ax.get_window_extent(renderer)
    for text in texts:
        bounds = text.get_window_extent(renderer)
        dx = max(0, edge.x0 + 12 - bounds.x0) + min(0, edge.x1 - 12 - bounds.x1)
        if dx:
            px, py = ax.transData.transform(text.get_position())
            text.set_position(ax.transData.inverted().transform((px + dx, py)))
    metadata_path = args.result_dir / "run.json"
    metadata = json.loads(metadata_path.read_text()) if metadata_path.exists() else {}
    attempted = metadata.get("attempted_queries", len(points))
    fig.text(0.5, 0.087, f"Labels: largest {args.labels} JEV speedups and {args.labels} slowdowns. Speedup = PG time / JEV time.",
             ha="center", fontsize=10)
    fig.text(0.5, 0.055, f"Real-data sample · {len(points)}/{attempted} queries retained · single-run measurements; cache effects apply.",
             ha="center", fontsize=9, color="#637183")
    fig.text(0.5, 0.034, "Planning and API latency excluded. Timings are PostgreSQL EXPLAIN ANALYZE Execution Time.",
             ha="center", fontsize=9, color="#637183")
    for extension in ("png", "svg"):
        output = args.result_dir / f"speedup.{extension}"
        fig.savefig(output, dpi=180, facecolor="white", metadata={"Date": None} if extension == "svg" else None)
        if extension == "svg":
            output.write_text("\n".join(line.rstrip() for line in output.read_text().splitlines()) + "\n")
        print(output)
    plt.close(fig)
    print(f"JEV faster: {len(faster)}; PostgreSQL faster: {len(slower)}; equal: {len(tied)}")
    for name, _, _, ratio in annotated:
        print(f"{name}: PG/JEV = {ratio:.3f}x")


if __name__ == "__main__":
    main()
