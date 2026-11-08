"""JSON export of the data-quality report."""

import json
from pathlib import Path

from automation_toolkit.processing.quality import QualityReport

FILENAME = "data_quality.json"


def write_quality_json(path: Path, report: QualityReport) -> None:
    """Write ``report`` as indented UTF-8 JSON with a trailing newline.

    Non-ASCII text (product names, file names) is written as-is rather than escaped so
    the file stays readable; the mapping comes from :meth:`QualityReport.to_dict`, so
    identical validation results produce byte-identical files.
    """
    path.write_text(
        json.dumps(report.to_dict(), indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
