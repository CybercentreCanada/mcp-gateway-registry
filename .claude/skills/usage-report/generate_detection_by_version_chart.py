"""Plot cloud_detection_method outcomes split by registry version.

Reads a single registry_metrics.csv, deduplicates events to one row per
unique registry_id (latest event per id), and produces a stacked
horizontal bar chart where:

- Each row is a registry version (top N by instance count, plus an
  "other" rollup for the long tail).
- Each bar segment is the share of instances on that version with a
  given `cloud_detection_method` value: env, dmi, ecs_meta,
  k8s_heuristic, imds, unknown, or "(field absent)" for pre-1.23.0
  instances that don't emit the field at all.
- Total instance count per version is annotated at the right edge.

This chart answers "did the cloud-detection cascade actually start
working on the versions where the fix shipped?". For the fix from
issue #1093 (PR #1106, released in 1.24.2), the expectation is that the
"unknown" segment should shrink on 1.24.2+ rows compared to 1.23.0/1.24.1.

CSV-only: it reads the latest snapshot, not historical. To track the
trend over time, run this script per dated CSV and compare reports.
"""

from __future__ import annotations

import argparse
import csv
import logging
import os
from collections import Counter, defaultdict

import matplotlib

matplotlib.use("Agg")

import sys as _sys

import matplotlib.pyplot as plt

_sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from tufte_style import apply_tufte_style, tufte_axes  # noqa: E402

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s,p%(process)s,{%(filename)s:%(lineno)d},%(levelname)s,%(message)s",
)
logger = logging.getLogger(__name__)

CHART_TITLE: str = "AI Registry -- Cloud Detection Outcomes by Registry Version"
FIGURE_WIDTH: int = 14
FIGURE_HEIGHT: int = 8

# Order detection methods from "good outcome" to "no outcome" so the
# stacked bar reads left-to-right as success-to-failure.
METHOD_ORDER: list[str] = [
    "env",
    "dmi",
    "ecs_meta",
    "k8s_heuristic",
    "imds",
    "unknown",
    "(field absent)",
]

# Pleasant green-to-red diverging-ish palette: green for working methods,
# red/grey for failures and field-absent. Hand-picked so "(field absent)"
# is the most muted (no judgement, just old version) and "unknown" is the
# loudest red (cascade ran, all paths failed).
METHOD_COLORS: dict[str, str] = {
    "env": "#2ca02c",
    "dmi": "#7fbf7f",
    "ecs_meta": "#1f77b4",
    "k8s_heuristic": "#5a9fd4",
    "imds": "#9467bd",
    "unknown": "#d62728",
    "(field absent)": "#bcbcbc",
}


def _read_csv(
    path: str,
) -> list[dict[str, str]]:
    """Load all rows from a single telemetry CSV."""
    with open(path, newline="") as f:
        reader = csv.DictReader(f)
        rows = list(reader)
    logger.info(f"Read {len(rows)} rows from {path}")
    return rows


def _latest_per_instance(
    rows: list[dict[str, str]],
) -> list[dict[str, str]]:
    """One row per registry_id: the latest event by ts."""
    latest: dict[str, dict[str, str]] = {}
    for r in rows:
        rid = (r.get("registry_id") or "").strip()
        if not rid:
            continue
        ts = r.get("ts", "")
        if rid not in latest or ts > latest[rid].get("ts", ""):
            latest[rid] = r
    logger.info(f"Deduplicated {len(rows)} events to {len(latest)} unique instances")
    return list(latest.values())


def _normalize_method(
    raw: str | None,
) -> str:
    """Normalize the cloud_detection_method field for charting.

    An empty string means the field was never emitted, which only happens
    for pre-1.23.0 schemas. Bucket those into "(field absent)" so they
    are visually distinct from "unknown" (cascade ran, all paths failed).
    """
    value = (raw or "").strip()
    if not value:
        return "(field absent)"
    return value


def _compute_method_counts_per_version(
    instances: list[dict[str, str]],
    top_n: int,
    min_instances_for_other: int,
) -> tuple[list[str], dict[str, Counter]]:
    """Bucket instances into (version, detection_method) pairs.

    Versions ranked by instance count. The top_n versions get their own
    rows; everything beyond that is rolled up into a single "other
    (N versions)" row provided the rollup itself has at least
    min_instances_for_other instances (otherwise it's dropped to keep
    the chart readable).

    Returns (ordered_version_labels, {version_label: Counter(method->count)}).
    """
    version_counter: Counter = Counter()
    for r in instances:
        v = (r.get("v") or "unknown").strip() or "unknown"
        version_counter[v] += 1

    top_versions = [v for v, _ in version_counter.most_common(top_n)]
    top_set = set(top_versions)

    method_by_version: dict[str, Counter] = defaultdict(Counter)
    other_versions: set[str] = set()
    for r in instances:
        v = (r.get("v") or "unknown").strip() or "unknown"
        method = _normalize_method(r.get("cloud_detection_method"))
        if v in top_set:
            method_by_version[v][method] += 1
        else:
            method_by_version["__other__"][method] += 1
            other_versions.add(v)

    ordered_labels: list[str] = list(top_versions)
    other_total = sum(method_by_version["__other__"].values())
    if other_total >= min_instances_for_other and other_versions:
        rollup_label = f"other ({len(other_versions)} versions, {other_total} instances)"
        method_by_version[rollup_label] = method_by_version.pop("__other__")
        ordered_labels.append(rollup_label)
    else:
        method_by_version.pop("__other__", None)

    return ordered_labels, method_by_version


def _plot_chart(
    ordered_labels: list[str],
    method_by_version: dict[str, Counter],
    output_path: str,
    snapshot_date: str | None = None,
) -> None:
    """Render the stacked horizontal bar chart and save to PNG."""
    apply_tufte_style()

    fig, ax = plt.subplots(figsize=(FIGURE_WIDTH, FIGURE_HEIGHT))

    # Sort versions descending in count so the largest cohort is at the
    # top of the horizontal layout (matplotlib draws barh bottom-up).
    plot_labels = list(reversed(ordered_labels))
    plot_totals = [sum(method_by_version[label].values()) for label in plot_labels]

    left = [0.0] * len(plot_labels)
    for method in METHOD_ORDER:
        widths = [method_by_version[label].get(method, 0) for label in plot_labels]
        if not any(widths):
            continue
        ax.barh(
            plot_labels,
            widths,
            left=left,
            label=method,
            color=METHOD_COLORS[method],
            edgecolor="white",
            linewidth=0.5,
        )
        left = [a + b for a, b in zip(left, widths, strict=False)]

    # Annotate each row with total instance count at the right edge.
    max_total = max(plot_totals) if plot_totals else 1
    for label, total in zip(plot_labels, plot_totals, strict=False):
        ax.text(
            total + max_total * 0.01,
            label,
            f" n={total}",
            va="center",
            fontsize=9,
            color="#333333",
        )

    title = CHART_TITLE
    if snapshot_date:
        title += f"\nSnapshot {snapshot_date}: {sum(plot_totals)} customer instances"
    fig.suptitle(title, fontsize=14, fontweight="bold", y=0.99)

    ax.set_xlabel("Instances")
    ax.set_xlim(0, max_total * 1.18)
    ax.legend(
        title="cloud_detection_method",
        loc="lower right",
        ncols=1,
        fontsize=8,
    )

    tufte_axes(ax)
    plt.tight_layout(rect=[0, 0, 1, 0.95])
    fig.savefig(output_path, dpi=150, bbox_inches="tight")
    plt.close(fig)
    logger.info(f"Detection-by-version chart saved to {output_path}")


def _write_csv_sidecar(
    ordered_labels: list[str],
    method_by_version: dict[str, Counter],
    csv_out: str,
) -> None:
    """Write the per-version method breakdown as a CSV for downstream diffing."""
    with open(csv_out, "w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(["version", *METHOD_ORDER, "total"])
        for label in ordered_labels:
            counter = method_by_version[label]
            row = [label]
            total = 0
            for method in METHOD_ORDER:
                count = counter.get(method, 0)
                row.append(count)
                total += count
            row.append(total)
            writer.writerow(row)
    logger.info(f"Detection-by-version CSV written to {csv_out}")


def main() -> None:
    """CLI entry: generate the detection-by-version chart."""
    parser = argparse.ArgumentParser(
        description="Plot cloud_detection_method outcomes split by registry version",
    )
    parser.add_argument(
        "--csv",
        required=True,
        help="Path to a single registry_metrics.csv (dated snapshot)",
    )
    parser.add_argument(
        "--output",
        required=True,
        help="Path to save the output PNG",
    )
    parser.add_argument(
        "--csv-out",
        default=None,
        help="Optional path to write the per-version method breakdown as CSV",
    )
    parser.add_argument(
        "--top-n-versions",
        type=int,
        default=12,
        help="How many top versions get their own row (default: 12)",
    )
    parser.add_argument(
        "--min-instances-for-other",
        type=int,
        default=3,
        help=(
            "Minimum instance count for the rolled-up 'other' row to be "
            "drawn at all (default: 3). Below this threshold the long tail "
            "is dropped from the chart entirely."
        ),
    )
    parser.add_argument(
        "--snapshot-date",
        default=None,
        help="Optional YYYY-MM-DD to label the chart with the snapshot date",
    )
    args = parser.parse_args()

    if not os.path.exists(args.csv):
        logger.error(f"CSV not found: {args.csv}")
        raise SystemExit(1)

    rows = _read_csv(args.csv)
    instances = _latest_per_instance(rows)
    if not instances:
        logger.error("No identified instances in CSV")
        raise SystemExit(1)

    ordered_labels, method_by_version = _compute_method_counts_per_version(
        instances=instances,
        top_n=args.top_n_versions,
        min_instances_for_other=args.min_instances_for_other,
    )

    _plot_chart(
        ordered_labels=ordered_labels,
        method_by_version=method_by_version,
        output_path=args.output,
        snapshot_date=args.snapshot_date,
    )

    if args.csv_out:
        _write_csv_sidecar(ordered_labels, method_by_version, args.csv_out)


if __name__ == "__main__":
    main()
