from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402

from automation_toolkit.config import Config  # noqa: E402
from automation_toolkit.processing.analytics import Analytics  # noqa: E402


def generate_charts(metrics: Analytics, folder: Path, config: Config) -> list[Path]:
    folder.mkdir(parents=True, exist_ok=True)
    paths = []
    panels = [
        ("categories", "Revenue by category", metrics.categories.head(20), "category"),
        ("monthly", "Monthly revenue", metrics.monthly, "date"),
        (
            "products",
            f"Top {config.top_n} products by units sold",
            metrics.products.sort_values(["units", "product"], ascending=[False, True]).head(
                config.top_n
            ),
            "product",
        ),
        ("comparison", "Monthly revenue change (%)", metrics.comparison, "date"),
    ]
    with plt.rc_context(
        {
            "font.size": 10,
            "axes.spines.top": False,
            "axes.spines.right": False,
            "figure.facecolor": "white",
        }
    ):
        for name, title, frame, column in panels:
            fig, ax = plt.subplots(figsize=(9, 4.4), layout="constrained")
            labels = (
                frame[column].dt.strftime("%Y-%m")
                if column == "date"
                else frame[column].astype(str).str.slice(0, 42)
            )
            if name in {"categories", "products"}:
                measure = "units" if name == "products" else "revenue"
                ax.barh(labels.iloc[::-1], frame[measure].iloc[::-1], color="#147d92")
                ax.set_xlabel(
                    "Units sold" if measure == "units" else f"Revenue ({config.currency})"
                )
            elif name == "monthly":
                ax.plot(labels, frame["revenue"], color="#147d92", marker="o", linewidth=2.5)
                ax.set_ylabel(config.currency)
            else:
                changes = frame["change_percent"]
                if changes.notna().any():
                    ax.bar(labels, changes.fillna(0), color="#147d92")
                else:
                    ax.text(0.5, 0.5, "No comparable months", ha="center", transform=ax.transAxes)
                ax.set_ylabel("Change (%)")
            if column == "date":
                ax.tick_params(axis="x", rotation=45)
                if len(labels) > 12:
                    for i, label in enumerate(ax.get_xticklabels()):
                        label.set_visible(i % max(1, len(labels) // 12) == 0)
            ax.set_title(title, loc="left", fontweight="bold", pad=15)
            ax.grid(axis="x" if column != "date" else "y", alpha=0.15)
            target = folder / f"{name}.png"
            try:
                fig.savefig(target, dpi=150)
            finally:
                plt.close(fig)
            paths.append(target)
    return paths
