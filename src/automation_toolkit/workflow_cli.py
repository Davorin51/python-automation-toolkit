"""Run or resume a :class:`~automation_toolkit.workflow.Workflow` from the terminal.

The workflow is a Python object, so the CLI loads it from ``module:attribute`` or
``path/to/file.py:attribute``. The attribute may be a ``Workflow`` or a zero-argument
callable returning one. ``--checkpoint`` enables resume: a second invocation with the
same path continues after the last completed step; ``--restart`` discards that state.

Exit status: 0 when every step succeeded, 2 when the run completed but optional
(``continue_on_error``) steps failed, 1 for a failed required step or any usage error,
130 when interrupted with Ctrl-C (the checkpoint, if any, is kept for a later resume).
"""

from __future__ import annotations

import argparse
import importlib
import importlib.util
import json
import logging
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from automation_toolkit import __version__
from automation_toolkit.exceptions import ToolkitError, WorkflowError
from automation_toolkit.workflow import Workflow, WorkflowCheckpoint, WorkflowResult

EXIT_OK = 0
EXIT_FAILED = 1
EXIT_PARTIAL = 2
EXIT_INTERRUPTED = 130

logger = logging.getLogger(__name__)


def load_workflow(spec: str) -> Workflow:
    """Resolve ``module:attr`` or ``file.py:attr`` to a :class:`Workflow` instance."""
    target, separator, attribute = spec.rpartition(":")
    if not separator or not target or not attribute:
        raise WorkflowError(
            f"Workflow {spec!r} must be given as module:attribute or path/to/file.py:attribute."
        )
    if target.endswith(".py") or "/" in target or "\\" in target:
        module = _import_file(Path(target))
    else:
        try:
            module = importlib.import_module(target)
        except Exception as exc:  # noqa: BLE001 - any import-time failure is reported
            raise WorkflowError(f"Cannot import workflow module {target!r}: {exc}") from exc
    try:
        candidate = getattr(module, attribute)
    except AttributeError:
        raise WorkflowError(
            f"Module {target!r} has no attribute {attribute!r}; "
            "expected a Workflow or a function returning one."
        ) from None
    if callable(candidate) and not isinstance(candidate, Workflow):
        try:
            candidate = candidate()
        except Exception as exc:  # noqa: BLE001 - surfaced as a friendly error
            raise WorkflowError(f"Workflow factory {spec!r} raised {exc!r}") from exc
    if not isinstance(candidate, Workflow):
        raise WorkflowError(
            f"{spec!r} is a {type(candidate).__name__}, not a Workflow or a function returning one."
        )
    return candidate


def _import_file(path: Path) -> Any:
    if not path.is_file():
        raise WorkflowError(f"Workflow file {path} does not exist.")
    name = f"_automation_toolkit_workflow_{abs(hash(str(path.resolve())))}"
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise WorkflowError(f"Cannot load workflow file {path}.")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    try:
        spec.loader.exec_module(module)
    except Exception as exc:  # noqa: BLE001 - any import-time failure is reported
        sys.modules.pop(name, None)
        raise WorkflowError(f"Cannot import workflow file {path}: {exc}") from exc
    return module


def parse_data(pairs: Sequence[str], json_file: Path | None) -> dict[str, Any]:
    """Build the initial context data from ``--data-json`` and ``--data KEY=VALUE`` options.

    Values given on the command line are parsed as JSON when possible (``3``, ``true``,
    ``[1, 2]``) and kept as text otherwise. Command-line pairs override the JSON file.
    """
    data: dict[str, Any] = {}
    if json_file is not None:
        try:
            loaded = json.loads(json_file.read_text(encoding="utf-8"))
        except OSError as exc:
            raise WorkflowError(f"Cannot read {json_file}: {exc}") from exc
        except ValueError as exc:
            raise WorkflowError(f"{json_file} is not valid JSON: {exc}") from exc
        if not isinstance(loaded, dict):
            raise WorkflowError(f"{json_file} must contain a JSON object at the top level.")
        data.update(loaded)
    for pair in pairs:
        key, separator, raw = pair.partition("=")
        if not separator or not key.strip():
            raise WorkflowError(f"--data expects KEY=VALUE, got {pair!r}.")
        try:
            value: Any = json.loads(raw)
        except ValueError:
            value = raw
        data[key.strip()] = value
    return data


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="automation-toolkit-workflow",
        description="Run or resume a workflow defined in Python.",
        epilog=(
            "Exit status: 0 all steps succeeded; 2 completed with failed optional steps; "
            "1 failed or invalid; 130 interrupted (checkpoint kept)."
        ),
    )
    parser.add_argument("--version", action="version", version=__version__)
    parser.add_argument(
        "--workflow",
        required=True,
        metavar="SPEC",
        help="module:attribute or path/to/file.py:attribute naming a Workflow or a factory",
    )
    parser.add_argument(
        "--checkpoint",
        type=Path,
        metavar="FILE",
        help="Save progress to FILE after each step and resume from it when it exists",
    )
    parser.add_argument(
        "--restart", action="store_true", help="Discard an existing checkpoint before running"
    )
    parser.add_argument(
        "--data",
        action="append",
        default=[],
        metavar="KEY=VALUE",
        help="Initial context value; VALUE is parsed as JSON when possible (repeatable)",
    )
    parser.add_argument(
        "--data-json", type=Path, metavar="FILE", help="JSON object with initial context values"
    )
    parser.add_argument(
        "--json-report",
        metavar="FILE",
        help="Write the JSON result report to FILE ('-' prints it to stdout instead of text)",
    )
    parser.add_argument(
        "--describe", action="store_true", help="List the steps and their settings, do not run"
    )
    parser.add_argument(
        "--traceback", action="store_true", help="Include tracebacks in the text report"
    )
    parser.add_argument("--verbose", action="store_true")
    return parser


def describe(workflow: Workflow) -> str:
    lines = [f"Workflow '{workflow.name}' ({len(workflow)} steps):"]
    for index, step in enumerate(workflow.steps, start=1):
        details = [f"retries={step.retries}"]
        if step.retries and (step.retry_delay or step.max_delay is not None):
            details.append("delays=" + ", ".join(f"{d:g}s" for d in step.retry_schedule()))
        if step.timeout is not None:
            details.append(f"timeout={step.timeout:g}s")
        if step.continue_on_error:
            details.append("optional")
        lines.append(f"  {index}. {step.name} ({', '.join(details)})")
    return "\n".join(lines)


def _emit(result: WorkflowResult, args: argparse.Namespace) -> bool:
    """Print the text or JSON report; return ``False`` when a JSON file could not be written."""
    if args.json_report == "-":
        print(result.to_json())
        return True
    print(result.report(include_traceback=args.traceback))
    if args.json_report:
        try:
            target = result.write_json(args.json_report)
        except WorkflowError as exc:
            logger.error("%s", exc)
            return False
        print(f"JSON report: {target.resolve()}")
    return True


def _notice_checkpoint_kept(checkpoint: WorkflowCheckpoint | None) -> None:
    """Tell the operator how to resume; goes to stderr so ``--json-report -`` stays pure JSON."""
    if checkpoint is not None and checkpoint.path.exists():
        print(f"Checkpoint kept: {checkpoint.path.resolve()} (rerun to resume)", file=sys.stderr)


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.restart and args.checkpoint is None:
        parser.error("--restart requires --checkpoint")
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(levelname)s %(name)s: %(message)s",
    )
    checkpoint = WorkflowCheckpoint(args.checkpoint) if args.checkpoint else None
    try:
        workflow = load_workflow(args.workflow)
        if args.describe:
            print(describe(workflow))
            return EXIT_OK
        data = parse_data(args.data, args.data_json)
        if checkpoint is not None and args.restart:
            checkpoint.clear()
        result = workflow.run(data, checkpoint=checkpoint)
    except WorkflowError as exc:
        logger.error("%s", exc)
        if exc.result is not None:
            _emit(exc.result, args)
            _notice_checkpoint_kept(checkpoint)
        return EXIT_FAILED
    except (ToolkitError, ValueError) as exc:
        logger.error("%s", exc)
        return EXIT_FAILED
    except KeyboardInterrupt:
        logger.error("Interrupted")
        _notice_checkpoint_kept(checkpoint)
        return EXIT_INTERRUPTED
    if not _emit(result, args):
        return EXIT_FAILED
    return EXIT_OK if result.succeeded else EXIT_PARTIAL


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
