"""Generate a customer adoption funnel chart for the usage report.

Reads metrics-YYYY-MM-DD.json and emits a horizontal bar funnel showing
the cohort dropoff from "every customer ever installed" through the
multi-day cohort, the >=3 / 7 / 14 / 30 day survival thresholds, all the
way down to the confirmed-alive (revenue-countable) cohort. Each row is
labeled with absolute count and percentage of the top-of-funnel.

Designed as a LinkedIn-friendly companion chart to the timeseries plots:
single-image story arc, mobile-readable, no axis-decoding required.
"""

from __future__ import annotations

import argparse
import json
import logging
import os

import matplotlib

matplotlib.use("Agg")

import sys as _sys

import matplotlib.pyplot as plt
import seaborn as sns

_sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from tufte_style import apply_tufte_style, tufte_axes  # noqa: E402

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s,p%(process)s,{%(filename)s:%(lineno)d},%(levelname)s,%(message)s",
)
logger = logging.getLogger(__name__)

CHART_TITLE: str = "AI Registry -- Customer Adoption Funnel"
FIGURE_WIDTH: int = 12
FIGURE_HEIGHT: int = 7


def _load_metrics(
    metrics_path: str,
) -> dict:
    """Load metrics JSON."""
    with open(metrics_path) as f:
        return json.load(f)


def _load_liveness_count(
    liveness_path: str | None,
) -> int | None:
    """Load the confirmed-alive count from liveness JSON, or None if unavailable."""
    if not liveness_path or not os.path.exists(liveness_path):
        return None
    with open(liveness_path) as f:
        data = json.load(f)
    return data.get("counts", {}).get("confirmed")


def _build_funnel_rows(
    metrics: dict,
    confirmed_alive: int | None,
) -> list[tuple[str, int]]:
    """Build the ordered (label, count) rows of the funnel.

    Top of funnel is the total customer (non-internal) count. Each
    successive row is a strict subset of the row above it.
    """
    stickiness = metrics["stickiness"]
    total_customer = stickiness["total_non_internal"]
    one_day = stickiness["one_day_wonders"]
    multi_day = total_customer - one_day
    bucket_counts = stickiness["lifetime_bucket_counts"]

    rows: list[tuple[str, int]] = [
        ("Every customer ever installed", total_customer),
        ("Came back at least once", multi_day),
        (">= 3 days lifetime", bucket_counts["3"]),
        (">= 7 days lifetime", bucket_counts["7"]),
        (">= 14 days lifetime", bucket_counts["14"]),
        (">= 30 days lifetime", bucket_counts["30"]),
    ]

    if confirmed_alive is not None:
        rows.append(("Confirmed alive (>=5 hb in 7d)", confirmed_alive))

    return rows


def _generate_chart(
    rows: list[tuple[str, int]],
    output_path: str,
    report_date: str,
) -> None:
    """Render the funnel as a horizontal bar chart, top of funnel at top."""
    apply_tufte_style()

    labels = [r[0] for r in rows]
    counts = [r[1] for r in rows]
    top = counts[0] if counts else 1

    # Reverse so the top of the funnel is at the top of the chart
    labels_rev = labels[::-1]
    counts_rev = counts[::-1]

    fig, ax = plt.subplots(figsize=(FIGURE_WIDTH, FIGURE_HEIGHT))
    fig.suptitle(
        f"{CHART_TITLE}\n(snapshot: {report_date})",
        fontsize=14,
        fontweight="bold",
        y=0.99,
    )

    # Color bars from light (top of funnel, biggest, evaluators) to dark
    # (bottom of funnel, smallest, persistent customers).
    palette = sns.color_palette("Blues_d", len(rows))
    colors = palette[::-1]  # darker at the bottom rows when we plot reversed

    bars = ax.barh(labels_rev, counts_rev, color=colors, edgecolor="white")

    # Annotate each bar with absolute count and % of top of funnel.
    for bar, label, count in zip(bars, labels_rev, counts_rev, strict=False):
        pct = 100.0 * count / top if top else 0
        text = f"  {count} ({pct:.1f}% of top)"
        ax.text(
            bar.get_width() + max(top * 0.005, 0.5),
            bar.get_y() + bar.get_height() / 2,
            text,
            va="center",
            fontsize=10,
            color="black",
        )

    # X-axis padding so annotations fit
    ax.set_xlim(0, top * 1.25)

    ax.set_xlabel("Customer instances", fontsize=11)
    ax.tick_params(axis="y", labelsize=10)
    ax.spines["top"].set_visible(False)
    ax.spines["right"].set_visible(False)

    for _ax in fig.axes:
        tufte_axes(_ax)
    plt.tight_layout(rect=[0, 0, 1, 0.94])
    fig.savefig(output_path, dpi=150, bbox_inches="tight")
    plt.close(fig)
    logger.info(f"Adoption funnel chart saved to {output_path}")


def main() -> None:
    """Parse arguments and emit the funnel chart."""
    parser = argparse.ArgumentParser(
        description="Generate the customer adoption funnel chart from metrics-*.json",
    )
    parser.add_argument(
        "--metrics",
        required=True,
        help="Path to metrics-YYYY-MM-DD.json",
    )
    parser.add_argument(
        "--liveness",
        default=None,
        help=(
            "Optional path to liveness-YYYY-MM-DD.json. When provided, the "
            "confirmed-alive count is appended as the last funnel row."
        ),
    )
    parser.add_argument(
        "--output",
        required=True,
        help="Path to save the output PNG",
    )
    args = parser.parse_args()

    if not os.path.exists(args.metrics):
        logger.error(f"Metrics file not found: {args.metrics}")
        raise SystemExit(1)

    metrics = _load_metrics(args.metrics)
    confirmed = _load_liveness_count(args.liveness)
    rows = _build_funnel_rows(metrics, confirmed)
    report_date = metrics.get("report_date", "unknown")

    _generate_chart(rows, args.output, report_date)


if __name__ == "__main__":
    main()
