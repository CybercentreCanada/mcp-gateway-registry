"""Shared Tufte-inspired styling for usage-report charts.

Applies Edward Tufte's principles from "The Visual Display of Quantitative
Information" to matplotlib/seaborn charts:

- Data-ink ratio: maximize ink that conveys data, minimize decorative ink
- Chartjunk: remove top/right spines, heavy grids, redundant labels
- Layering: primary data in dark/saturated tones, secondary in muted greys
- Honest scales: integer y-ticks, minimal decoration

Every chart generator should import and call `apply_tufte_style()` once at
module load, then `tufte_axes(ax)` per axes after plotting.

See tufte-viz-guidelines.md for the principles this implements.
"""

from __future__ import annotations

import matplotlib.pyplot as plt
import seaborn as sns

PRIMARY_COLOR: str = "#2b2b2b"
SECONDARY_COLOR: str = "#6b6b6b"
ACCENT_COLOR: str = "#1f77b4"
GRID_COLOR: str = "#e8e8e8"
TEXT_COLOR: str = "#333333"

CATEGORICAL_PALETTE: list[str] = [
    "#1f77b4",
    "#ff7f0e",
    "#2ca02c",
    "#d62728",
    "#9467bd",
    "#8c564b",
    "#e377c2",
    "#7f7f7f",
]

SEQUENTIAL_PALETTE_NAME: str = "Greys"


def apply_tufte_style() -> None:
    """Apply Tufte rcParams once per process.

    Sets a clean base: minimal spines, light grid, muted text, sans-serif font.
    Call once at the top of each chart generator before any plotting.
    """
    sns.set_theme(style="white")
    plt.rcParams.update(
        {
            "axes.spines.top": False,
            "axes.spines.right": False,
            "axes.spines.left": True,
            "axes.spines.bottom": True,
            "axes.edgecolor": SECONDARY_COLOR,
            "axes.linewidth": 0.8,
            "axes.labelcolor": TEXT_COLOR,
            "axes.labelsize": 10,
            "axes.titlesize": 11,
            "axes.titleweight": "normal",
            "axes.titlecolor": PRIMARY_COLOR,
            "axes.titlepad": 8,
            "xtick.color": SECONDARY_COLOR,
            "ytick.color": SECONDARY_COLOR,
            "xtick.labelsize": 9,
            "ytick.labelsize": 9,
            "xtick.major.size": 3,
            "ytick.major.size": 3,
            "xtick.major.width": 0.6,
            "ytick.major.width": 0.6,
            "grid.color": GRID_COLOR,
            "grid.linewidth": 0.5,
            "grid.alpha": 0.7,
            "legend.frameon": False,
            "legend.fontsize": 9,
            "legend.title_fontsize": 9,
            "figure.facecolor": "white",
            "axes.facecolor": "white",
            "savefig.facecolor": "white",
            "savefig.dpi": 150,
            "font.family": "sans-serif",
            "font.size": 10,
        }
    )


def tufte_axes(
    ax: plt.Axes,
    grid: str = "y",
) -> None:
    """Apply per-axes Tufte cleanup.

    Args:
        ax: The matplotlib Axes to clean up.
        grid: "y" (default), "x", "both", or "none" for which gridlines to keep.
              Tufte preferred minimal or no grid; "y" gives readers a quiet
              horizontal reference for value comparisons without competing with
              the data line.
    """
    ax.spines["top"].set_visible(False)
    ax.spines["right"].set_visible(False)

    if grid == "y":
        ax.grid(axis="y", linestyle="-", linewidth=0.5, color=GRID_COLOR, alpha=0.7)
        ax.grid(axis="x", visible=False)
    elif grid == "x":
        ax.grid(axis="x", linestyle="-", linewidth=0.5, color=GRID_COLOR, alpha=0.7)
        ax.grid(axis="y", visible=False)
    elif grid == "both":
        ax.grid(linestyle="-", linewidth=0.5, color=GRID_COLOR, alpha=0.7)
    else:
        ax.grid(visible=False)

    ax.set_axisbelow(True)
    ax.tick_params(colors=SECONDARY_COLOR)


def tufte_title(
    ax: plt.Axes,
    title: str,
    subtitle: str | None = None,
) -> None:
    """Set a Tufte-style title (left-aligned, normal weight, optional subtitle).

    Tufte preferred informative titles integrated with the chart, not the bold
    centered headlines that come by default. Subtitle, when given, sits below
    the title in lighter grey and is best used for units, scope, or methodology.
    """
    ax.set_title(title, loc="left", fontsize=11, fontweight="normal", color=PRIMARY_COLOR)
    if subtitle:
        ax.text(
            0,
            1.02,
            subtitle,
            transform=ax.transAxes,
            ha="left",
            va="bottom",
            fontsize=9,
            color=SECONDARY_COLOR,
        )
