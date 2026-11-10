"""Terminal interface for running and resuming workflows."""

import json
import logging
import sys
import textwrap
from pathlib import Path

import pytest

from automation_toolkit.exceptions import WorkflowError
from automation_toolkit.workflow import Workflow
from automation_toolkit.workflow_cli import (
    EXIT_FAILED,
    EXIT_INTERRUPTED,
    EXIT_OK,
    EXIT_PARTIAL,
    describe,
    load_workflow,
    main,
    parse_data,
)

WORKFLOW_SOURCE = '''
from pathlib import Path

from automation_toolkit.workflow import Step, Workflow

CALLS = Path(__file__).with_name("calls.log")


def _record(name):
    def action(ctx):
        with CALLS.open("a", encoding="utf-8") as stream:
            stream.write(name + "\\n")
        if name == ctx.get("fail_at"):
            raise RuntimeError(f"{name} exploded")
        if name == "interrupt" and ctx.get("interrupt"):
            raise KeyboardInterrupt
        return {"step": name, "limit": ctx.get("limit")}

    return action


demo = Workflow("demo")
demo.add_step("load", _record("load"), timeout=5)
demo.add_step("transform", _record("transform"), retries=2, retry_delay=0.5, backoff=2)
demo.add_step("interrupt", _record("interrupt"))
demo.add_step("optional", _record("optional"), continue_on_error=True)
demo.add_step("publish", _record("publish"))


def build():
    """A factory returning a fresh workflow."""
    return Workflow("built", [Step("only", lambda ctx: ctx.get("x", "none"))])


not_a_workflow = {"name": "demo"}


def bad_factory():
    raise ValueError("factory failed")
'''


@pytest.fixture
def workflow_file(tmp_path: Path) -> Path:
    path = tmp_path / "flows" / "demo_flow.py"
    path.parent.mkdir()
    path.write_text(textwrap.dedent(WORKFLOW_SOURCE), encoding="utf-8")
    return path


def calls(workflow_file: Path) -> list[str]:
    log = workflow_file.with_name("calls.log")
    return log.read_text().split() if log.exists() else []


def test_run_from_file_path_succeeds_and_prints_report(workflow_file, capsys):
    code = main(["--workflow", f"{workflow_file}:demo", "--data", "limit=10"])
    out = capsys.readouterr().out
    assert code == EXIT_OK
    assert "Workflow 'demo': 5 steps, 5 succeeded, 0 failed, 0 skipped" in out
    assert "1. load: succeeded after 1 attempt" in out
    assert calls(workflow_file) == ["load", "transform", "interrupt", "optional", "publish"]


def test_run_from_module_path_and_factory(workflow_file, monkeypatch, capsys):
    monkeypatch.syspath_prepend(str(workflow_file.parent))
    assert main(["--workflow", "demo_flow:build", "--data", "x=7"]) == EXIT_OK
    assert "Workflow 'built': 1 steps, 1 succeeded" in capsys.readouterr().out
    assert main(["--workflow", "demo_flow:demo"]) == EXIT_OK
    sys.modules.pop("demo_flow", None)


def test_failed_required_step_exits_1_prints_report_and_keeps_checkpoint(
    workflow_file, tmp_path, capsys, caplog
):
    ckpt = tmp_path / "state" / "demo.json"
    args = ["--workflow", f"{workflow_file}:demo", "--checkpoint", str(ckpt)]
    with caplog.at_level(logging.ERROR):
        code = main(args + ["--data", "fail_at=transform", "--traceback"])
    out, err = capsys.readouterr()
    assert code == EXIT_FAILED
    assert "failed at step 'transform' after 3 attempts: RuntimeError: transform exploded" in (
        caplog.text
    )
    assert "2. transform: failed after 3 attempts" in out
    assert "attempt 1: RuntimeError: transform exploded (waited 0.50s before retrying)" in out
    assert "Traceback (most recent call last):" in out
    assert f"Checkpoint kept: {ckpt.resolve()} (rerun to resume)" in err and "kept" not in out
    assert "4. optional: skipped" in out and "5. publish: skipped" in out
    assert ckpt.exists()
    assert calls(workflow_file) == ["load", "transform", "transform", "transform"]

    # Resume with the input corrected: load is not repeated.
    code = main(args + ["--data", "fail_at=none"])
    out = capsys.readouterr().out
    assert code == EXIT_OK
    assert "1. load: succeeded (restored from checkpoint)" in out
    assert calls(workflow_file)[4:] == ["transform", "interrupt", "optional", "publish"]
    assert not ckpt.exists()


def test_restart_discards_existing_checkpoint(workflow_file, tmp_path, capsys):
    ckpt = tmp_path / "demo.json"
    args = ["--workflow", f"{workflow_file}:demo", "--checkpoint", str(ckpt)]
    assert main(args + ["--data", "fail_at=publish"]) == EXIT_FAILED
    assert ckpt.exists()
    assert main(args + ["--restart"]) == EXIT_OK
    assert "restored from checkpoint" not in capsys.readouterr().out
    assert calls(workflow_file).count("load") == 2


def test_optional_failure_exits_2(workflow_file, capsys):
    code = main(["--workflow", f"{workflow_file}:demo", "--data", "fail_at=optional"])
    assert code == EXIT_PARTIAL
    out = capsys.readouterr().out
    assert "4 succeeded, 1 failed, 0 skipped" in out and "5. publish: succeeded" in out


def test_interrupt_exits_130_and_keeps_checkpoint(workflow_file, tmp_path, capsys, caplog):
    ckpt = tmp_path / "demo.json"
    args = ["--workflow", f"{workflow_file}:demo", "--checkpoint", str(ckpt)]
    with caplog.at_level(logging.ERROR):
        code = main(args + ["--data", "interrupt=true"])
    assert code == EXIT_INTERRUPTED and "Interrupted" in caplog.text
    assert f"Checkpoint kept: {ckpt.resolve()}" in capsys.readouterr().err
    assert ckpt.exists()
    assert main(args + ["--data", "interrupt=false"]) == EXIT_OK
    assert calls(workflow_file) == [
        "load",
        "transform",
        "interrupt",
        "interrupt",
        "optional",
        "publish",
    ]


def test_interrupt_without_checkpoint_mentions_no_checkpoint(workflow_file, capsys):
    code = main(["--workflow", f"{workflow_file}:demo", "--data", "interrupt=true"])
    captured = capsys.readouterr()
    assert code == EXIT_INTERRUPTED and "Checkpoint kept" not in captured.out + captured.err


def test_json_report_to_file_and_to_stdout(workflow_file, tmp_path, capsys):
    report = tmp_path / "reports" / "run.json"
    code = main(
        [
            "--workflow",
            f"{workflow_file}:demo",
            "--data",
            "fail_at=optional",
            "--json-report",
            str(report),
        ]
    )
    out = capsys.readouterr().out
    assert code == EXIT_PARTIAL and f"JSON report: {report.resolve()}" in out
    data = json.loads(report.read_text(encoding="utf-8"))
    assert data["report_version"] == 1 and data["workflow"] == "demo"
    assert data["failed"] == ["optional"] and data["succeeded"] is False
    assert data["steps"][3]["error"] == {"type": "RuntimeError", "message": "optional exploded"}
    assert data["steps"][0]["output"] == {"step": "load", "limit": None}

    code = main(["--workflow", f"{workflow_file}:demo", "--json-report", "-"])
    out = capsys.readouterr().out
    assert code == EXIT_OK
    parsed = json.loads(out)  # stdout is pure JSON
    assert (
        parsed["succeeded"] is True and [s["status"] for s in parsed["steps"]] == ["succeeded"] * 5
    )


def test_json_report_is_written_for_failed_runs_too(workflow_file, tmp_path, capsys):
    report = tmp_path / "run.json"
    args = ["--workflow", f"{workflow_file}:demo", "--data", "fail_at=load"]
    assert main(args + ["--json-report", str(report)]) == EXIT_FAILED
    data = json.loads(report.read_text())
    assert data["failed"] == ["load"] and data["skipped"] == [
        "transform",
        "interrupt",
        "optional",
        "publish",
    ]
    capsys.readouterr()
    assert main(args + ["--json-report", "-"]) == EXIT_FAILED
    assert json.loads(capsys.readouterr().out)["counts"]["skipped"] == 4


def test_describe_lists_steps_without_running(workflow_file, capsys):
    code = main(["--workflow", f"{workflow_file}:demo", "--describe"])
    out = capsys.readouterr().out
    assert code == EXIT_OK and calls(workflow_file) == []
    assert out.splitlines() == [
        "Workflow 'demo' (5 steps):",
        "  1. load (retries=0, timeout=5s)",
        "  2. transform (retries=2, delays=0.5s, 1s)",
        "  3. interrupt (retries=0)",
        "  4. optional (retries=0, optional)",
        "  5. publish (retries=0)",
    ]
    assert describe(Workflow("w").add_step("s", print, retries=1)) == (
        "Workflow 'w' (1 steps):\n  1. s (retries=1)"
    )


def test_data_parsing_rules(tmp_path):
    json_file = tmp_path / "data.json"
    json_file.write_text('{"a": 1, "b": "file", "nested": {"k": [1, 2]}}')
    data = parse_data(
        ["b=cli", "c=3", "d=true", "e=[1, 2]", "f=plain text", "g=", "h=x=y"], json_file
    )
    assert data == {
        "a": 1,
        "b": "cli",
        "c": 3,
        "d": True,
        "e": [1, 2],
        "f": "plain text",
        "g": "",
        "h": "x=y",
        "nested": {"k": [1, 2]},
    }
    assert parse_data([], None) == {}
    for bad in ("novalue", "=1", "  =1"):
        with pytest.raises(WorkflowError, match="KEY=VALUE"):
            parse_data([bad], None)
    json_file.write_text("[1, 2]")
    with pytest.raises(WorkflowError, match="JSON object"):
        parse_data([], json_file)
    json_file.write_text("{broken")
    with pytest.raises(WorkflowError, match="not valid JSON"):
        parse_data([], json_file)
    with pytest.raises(WorkflowError, match="Cannot read"):
        parse_data([], tmp_path / "missing.json")


def test_data_json_file_via_cli(workflow_file, tmp_path, capsys):
    json_file = tmp_path / "data.json"
    json_file.write_text('{"limit": 5}')
    args = ["--workflow", f"{workflow_file}:demo", "--data-json", str(json_file)]
    assert main(args + ["--json-report", "-"]) == EXIT_OK
    assert json.loads(capsys.readouterr().out)["steps"][0]["output"]["limit"] == 5
    assert main(args + ["--data", "limit=9", "--json-report", "-"]) == EXIT_OK
    assert json.loads(capsys.readouterr().out)["steps"][0]["output"]["limit"] == 9
    json_file.write_text("[]")
    assert main(args) == EXIT_FAILED


@pytest.mark.parametrize(
    "spec, match",
    [
        ("no_colon", "module:attribute"),
        (":attr", "module:attribute"),
        ("mod:", "module:attribute"),
        ("definitely_missing_module_xyz:wf", "Cannot import workflow module"),
        ("{file}:missing", "has no attribute 'missing'"),
        ("{file}:not_a_workflow", "is a dict, not a Workflow"),
        ("{file}:bad_factory", "raised ValueError"),
        ("{file}:CALLS", "is a PosixPath, not a Workflow"),
        ("{dir}/absent.py:demo", "does not exist"),
    ],
)
def test_invalid_workflow_specs(workflow_file, spec, match, caplog):
    spec = spec.format(file=workflow_file, dir=workflow_file.parent)
    with pytest.raises(WorkflowError, match=match):
        load_workflow(spec)
    with caplog.at_level(logging.ERROR):
        assert main(["--workflow", spec]) == EXIT_FAILED
    assert match.split("'")[0].strip() in caplog.text


def test_broken_workflow_file_is_reported(tmp_path, caplog):
    broken = tmp_path / "broken.py"
    broken.write_text("raise ImportError('boom at import')\n")
    with pytest.raises(WorkflowError, match="Cannot import workflow file .*boom at import"):
        load_workflow(f"{broken}:wf")
    registered = [m for m in sys.modules.values() if getattr(m, "__file__", None) == str(broken)]
    assert registered == []  # the half-imported module is not left behind
    with caplog.at_level(logging.ERROR):
        assert main(["--workflow", f"{broken}:wf"]) == EXIT_FAILED
    assert "boom at import" in caplog.text


def test_loading_the_same_file_twice_returns_fresh_modules(workflow_file):
    first = load_workflow(f"{workflow_file}:build")
    second = load_workflow(f"{workflow_file}:build")
    assert first is not second and first.name == second.name == "built"


def test_version_and_missing_workflow_argument(capsys):
    with pytest.raises(SystemExit) as info:
        main(["--version"])
    assert info.value.code == 0 and capsys.readouterr().out.strip()
    with pytest.raises(SystemExit) as info:
        main([])
    assert info.value.code == 2


def test_unwritable_json_report_is_a_failure(workflow_file, tmp_path, caplog, capsys):
    blocker = tmp_path / "blocker"
    blocker.write_text("file")
    with caplog.at_level(logging.ERROR):
        code = main(
            ["--workflow", f"{workflow_file}:demo", "--json-report", str(blocker / "r.json")]
        )
    assert code == EXIT_FAILED and "Cannot write workflow report" in caplog.text
    assert "5 succeeded" in capsys.readouterr().out  # the text report was still printed


def test_console_script_is_registered():
    import tomllib

    pyproject = tomllib.loads((Path(__file__).resolve().parents[1] / "pyproject.toml").read_text())
    scripts = pyproject["project"]["scripts"]
    assert scripts["automation-toolkit-workflow"] == "automation_toolkit.workflow_cli:main"


def test_other_toolkit_errors_are_reported_as_failures(monkeypatch, caplog):
    from automation_toolkit.exceptions import ConfigurationError

    def broken(spec):
        raise ConfigurationError("bad setting")

    monkeypatch.setattr("automation_toolkit.workflow_cli.load_workflow", broken)
    with caplog.at_level(logging.ERROR):
        assert main(["--workflow", "x:y"]) == EXIT_FAILED
    assert "bad setting" in caplog.text


def test_unloadable_file_spec_is_reported(workflow_file, monkeypatch):
    monkeypatch.setattr(
        "automation_toolkit.workflow_cli.importlib.util.spec_from_file_location",
        lambda name, path: None,
    )
    with pytest.raises(WorkflowError, match="Cannot load workflow file"):
        load_workflow(f"{workflow_file}:demo")


def test_restart_requires_checkpoint(workflow_file, capsys):
    with pytest.raises(SystemExit) as info:
        main(["--workflow", f"{workflow_file}:demo", "--restart"])
    assert info.value.code == 2 and "--restart requires --checkpoint" in capsys.readouterr().err
    assert calls(workflow_file) == []
