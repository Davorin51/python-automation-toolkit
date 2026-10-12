# Contributing

Install the development dependencies with `python -m pip install -e '.[dev]'`.
Before submitting a change, run:

```bash
ruff check .
ruff format --check .
pytest --cov --cov-report=term-missing
python -m build
```

Add regression tests for changed validation rules, arithmetic or output behavior.
Keep ingestion, business calculations and output adapters separate. CLI and GUI
must call the shared pipeline. Do not commit customer data, credentials or local
report folders. Synthetic fixture changes must include updated control totals.

Report a bug with a minimal synthetic dataset, configuration, Python version and
the observed/expected behavior. Avoid uploading confidential business records.
