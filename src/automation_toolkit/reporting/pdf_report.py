from datetime import datetime
from html import escape
from pathlib import Path

import reportlab
from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import getSampleStyleSheet
from reportlab.lib.units import inch
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import (
    Image,
    PageBreak,
    Paragraph,
    SimpleDocTemplate,
    Spacer,
    Table,
    TableStyle,
)

from automation_toolkit.config import Config
from automation_toolkit.processing.analytics import Analytics
from automation_toolkit.processing.validation import ValidationResult

_FONTS = Path(reportlab.__file__).parent / "fonts"
pdfmetrics.registerFont(TTFont("ToolkitSans", str(_FONTS / "Vera.ttf")))
pdfmetrics.registerFont(TTFont("ToolkitSans-Bold", str(_FONTS / "VeraBd.ttf")))
pdfmetrics.registerFontFamily("ToolkitSans", normal="ToolkitSans", bold="ToolkitSans-Bold")


def write_pdf(
    path: Path,
    result: ValidationResult,
    metrics: Analytics,
    charts: list[Path],
    config: Config,
    generated_at: datetime,
) -> None:
    styles = getSampleStyleSheet()
    for style in styles.byName.values():
        style.fontName = (
            "ToolkitSans-Bold" if style.name.startswith(("Title", "Heading")) else "ToolkitSans"
        )
    styles["Title"].textColor = colors.HexColor("#15354B")
    styles["Heading2"].textColor = colors.HexColor("#147d92")
    story = []

    def paragraph(text: str, style: str = "BodyText") -> None:
        story.append(Paragraph(escape(text), styles[style]))

    paragraph("SALES PERFORMANCE", "Heading2")
    paragraph(config.company_name, "Title")
    paragraph("Automated business report", "Heading1")
    dates = result.transactions["date"]
    paragraph(f"Reporting period: {dates.min():%d %b %Y} - {dates.max():%d %b %Y}")
    paragraph(f"Generated: {generated_at:%d %b %Y %H:%M} UTC")
    story.append(Spacer(1, 24))
    kpis = [
        ["REVENUE", "ORDERS", "AVERAGE ORDER"],
        [
            f"{metrics.total_revenue_cents / 100:,.2f} {config.currency}",
            str(metrics.orders),
            f"{metrics.average_order_value:,.2f} {config.currency}",
        ],
    ]
    kpis = [[Paragraph(escape(value), styles["BodyText"]) for value in row] for row in kpis]
    table = Table(kpis, colWidths=[170, 160, 170])
    table.setStyle(
        TableStyle(
            [
                ("BACKGROUND", (0, 0), (-1, -1), colors.HexColor("#eaf2f5")),
                ("TOPPADDING", (0, 0), (-1, -1), 12),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 12),
            ]
        )
    )
    story.append(table)
    story.append(Spacer(1, 24))
    paragraph("Results at a glance", "Heading2")
    leader = str(metrics.categories.iloc[0]["category"])
    paragraph(
        f"{metrics.units_sold:,} units sold across {len(metrics.categories)} categories. "
        f"The leading category by revenue is {leader}."
    )
    paragraph(f"Order calculation: {metrics.order_basis}.")
    paragraph("Data quality", "Heading2")
    paragraph(
        f"Input: {result.input_rows:,} rows. Accepted: {len(result.transactions):,}. "
        f"Invalid: {result.invalid_rows:,}. Excluded: {result.excluded_rows:,}. "
        f"Validation policy: {config.validation_policy}."
    )
    if result.rejected_files:
        paragraph("Rejected files: " + ", ".join(Path(x).name for x in result.rejected_files))
    paragraph(
        "Period comparisons use calendar months. The first and last months may be "
        "partial. A percentage change is unavailable when the prior revenue is zero."
    )
    story.append(PageBreak())
    paragraph("Sales breakdown", "Heading1")
    for chart in charts[:2]:
        story.append(Image(str(chart), width=6.9 * inch, height=3.373 * inch))
        story.append(Spacer(1, 12))
    story.append(PageBreak())
    paragraph("Products and period comparison", "Heading1")
    for chart in charts[2:]:
        story.append(Image(str(chart), width=6.9 * inch, height=3.373 * inch))
        story.append(Spacer(1, 12))

    def footer(canvas, doc) -> None:
        canvas.setFont("ToolkitSans", 8)
        canvas.setFillColor(colors.HexColor("#536575"))
        canvas.drawString(48, 24, "Python Automation Toolkit | Sales analytics")
        canvas.drawRightString(A4[0] - 48, 24, f"Page {doc.page}")

    SimpleDocTemplate(
        str(path), pagesize=A4, rightMargin=48, leftMargin=48, topMargin=45, bottomMargin=42
    ).build(story, onFirstPage=footer, onLaterPages=footer)
