"""JSON export of a dataset profile."""

import json
from pathlib import Path

from automation_toolkit.processing.profiling import DatasetProfile


def write_profile_json(path: Path, profile: DatasetProfile) -> None:
    """Write ``profile`` as indented UTF-8 JSON, creating parent folders as needed."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(profile.to_dict(), indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
