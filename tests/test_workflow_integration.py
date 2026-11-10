"""End-to-end scenarios for the workflow engine, its checkpoints and the terminal interface.

These tests exercise the real toolkit functions (ingestion, validation, analytics), real
file-system checkpoints, real retry delays and timeouts, and the CLI both in-process and
as a separate interpreter process that is killed mid-step.
"""

from __future__ import annotations

import json
import logging
import os
import signal
import subprocess
import sys
import textwrap
import time
from pathlib import Path

import pytest

from automation_toolkit.config import Config
from automation_toolkit.exceptions import StepTimeoutError, WorkflowError
from automation_toolkit.ingestion import load_files
from automation_toolkit.processing.analytics import analyze
from automation_toolkit.processing.validation import validate
from automation_toolkit.workflow import Workflow
from automation_toolkit.workflow_cli import (
    EXIT_FAILED,
    EXIT_INTERRUPTED,
    EXIT_OK,
    EXIT_PARTIAL,
    main,
)

ROOT = Path(__file__).resolve().parents[1]


def sales_workflow(publish_failures: dict[str, int], **kwargs) -> Workflow:
    """A realistic toolkit workflow passing only JSON-friendly values through the context.

    ``publish_failures["left"]`` counts how many times the publish step still has to fail.
    """
    config = Config()

    def load(ctx):
        frame = load_files([Path(p) for p in ctx["inputs"]], config)
        path = Path(ctx["workdir"]) / "loaded.json"
        frame.to_json(path, orient="records", date_format="iso")
        return {"rows": len(frame), "path": str(path)}

    def validate_step(ctx):
        frame = load_files([Path(p) for p in ctx["inputs"]], config)
        result = validate(frame, config)
        ctx["accepted_rows"] = len(result.transactions)
        return {"invalid_rows": result.invalid_rows, "accepted_rows": len(result.transactions)}

    def analyze_step(ctx):
        frame = load_files([Path(p) for p in ctx["inputs"]], config)
        metrics = analyze(validate(frame, config).transactions)
        return {"revenue_cents": metrics.total_revenue_cents, "orders": metrics.orders}

    def publish(ctx):
        if publish_failures.get("left", 0) > 0:
            publish_failures["left"] -= 1
            raise OSError("report share unavailable")
        target = Path(ctx["workdir"]) / "summary.json"
        target.write_text(json.dumps({**ctx.outputs["analyze"], **ctx.outputs["validate"]}))
        return str(target)

    workflow = Workflow("sales", **kwargs)
    workflow.add_step("load", load, timeout=30)
    workflow.add_step("validate", validate_step)
    workflow.add_step("analyze", analyze_step, retries=1)
    workflow.add_step("publish", publish, retries=2, retry_delay=0.01, backoff=2, timeout=30)
    return workflow


def test_toolkit_workflow_fails_checkpoints_resumes_and_reports(sales_file, tmp_path, caplog):
    workdir = tmp_path / "work"
    workdir.mkdir()
    checkpoint = tmp_path / "state" / "sales.json"
    data = {"inputs": [str(sales_file)], "workdir": str(workdir)}
    failures = {"left": 5}  # more than the retries allow: the first run fails
    with caplog.at_level(logging.INFO):
        with pytest.raises(WorkflowError, match="publish.*after 3 attempts") as info:
            sales_workflow(failures).run(data, checkpoint=checkpoint)
    first = info.value.result
    assert first.step("publish").delays == pytest.approx([0.01, 0.02])
    assert first.step("publish").duration_seconds >= 0.03
    assert [s.status.value for s in first.steps] == ["succeeded"] * 3 + ["failed"]
    assert checkpoint.exists() and failures["left"] == 2
    report = first.to_dict()
    assert report["steps"][3]["errors"][0]["delay_before_retry"] == 0.01
    assert report["steps"][2]["output"]["revenue_cents"] == 295000
    assert "retrying in 0.01s" in caplog.text and "retrying in 0.02s" in caplog.text

    # The share comes back: the resumed run only executes publish.
    executed: list[str] = []
    resumed_workflow = sales_workflow({"left": 0})
    for step in resumed_workflow.steps:
        original = step.action
        object.__setattr__(
            step, "action", lambda ctx, o=original, n=step.name: (executed.append(n), o(ctx))[1]
        )
    result = resumed_workflow.run(data, checkpoint=checkpoint)
    assert executed == ["publish"]
    assert result.restored == ["load", "validate", "analyze"] and result.succeeded
    assert result.context["accepted_rows"] == 3  # written by validate, restored from the checkpoint
    summary = json.loads(Path(result.outputs["publish"]).read_text())
    assert summary == {"revenue_cents": 295000, "orders": 2, "invalid_rows": 0, "accepted_rows": 3}
    assert not checkpoint.exists()
    json_report = json.loads(result.to_json())
    assert json_report["resumed"] and json_report["restored"] == ["load", "validate", "analyze"]
    assert json_report["steps"][0]["restored"] and json_report["steps"][3]["attempts"] == 1


def test_timeouts_retries_backoff_and_recovery_with_real_waits(tmp_path):
    checkpoint = tmp_path / "ckpt.json"
    state = {"calls": 0}

    def flaky_slow(ctx):
        state["calls"] += 1
        if state["calls"] <= 2:
            time.sleep(0.5)  # exceeds the timeout
        return state["calls"]

    workflow = Workflow("timed")
    workflow.add_step("first", lambda ctx: "ok")
    workflow.add_step("slow", flaky_slow, timeout=0.05, retries=1, retry_delay=0.02)
    workflow.add_step("last", lambda ctx: ctx.outputs["slow"] * 10)

    started = time.perf_counter()
    with pytest.raises(WorkflowError, match="timed out after 0.05s") as info:
        workflow.run(checkpoint=checkpoint)
    elapsed = time.perf_counter() - started
    assert 0.12 <= elapsed < 2
    slow = info.value.result.step("slow")
    assert slow.timed_out and slow.attempts == 2 and slow.delays == [0.02]
    assert all(isinstance(e, StepTimeoutError) for e in slow.errors)
    assert info.value.result.skipped == ["last"]
    assert [c["name"] for c in json.loads(checkpoint.read_text())["payload"]["completed"]] == [
        "first"
    ]

    result = workflow.run(checkpoint=checkpoint)  # third call is fast
    assert result.restored == ["first"] and result.outputs == {"first": "ok", "slow": 3, "last": 30}
    assert result.step("slow").attempts == 1 and not result.step("slow").timed_out
    assert not checkpoint.exists()


def test_abandoned_attempt_cannot_poison_a_later_resume(tmp_path):
    checkpoint = tmp_path / "ckpt.json"

    def slow_writer(ctx):
        time.sleep(0.3)
        ctx["poison"] = True
        ctx.outputs["forged"] = 1

    workflow = Workflow("poison")
    workflow.add_step("slow", slow_writer, timeout=0.05, continue_on_error=True)
    workflow.add_step("mark", lambda ctx: ctx.update_data(marked=True))
    workflow.add_step("fail", lambda ctx: (_ for _ in ()).throw(RuntimeError("stop")))
    with pytest.raises(WorkflowError):
        workflow.run(checkpoint=checkpoint)
    time.sleep(0.4)  # let the abandoned attempt finish writing into its private copy
    payload = json.loads(checkpoint.read_text())["payload"]
    assert payload["data"] == {"marked": True} and [c["name"] for c in payload["completed"]] == []
    assert "forged" not in {c["name"] for c in payload["completed"]}


WORKFLOW_FILE = """
import os
import time
from pathlib import Path

from automation_toolkit.workflow import Workflow

STATE = Path(os.environ["WF_STATE"])


def record(name):
    (STATE / "calls.log").open("a").write(name + "\\n")


def load(ctx):
    record("load")
    return {"rows": ctx["rows"]}


def transform(ctx):
    record("transform")
    if (STATE / "fail_transform").exists():
        (STATE / "fail_transform").unlink()
        raise RuntimeError("transform failed once")
    return ctx.outputs["load"]["rows"] * 2


def slow(ctx):
    record("slow")
    if (STATE / "hang").exists():
        (STATE / "hanging").touch()
        time.sleep(60)
    return "fast"


def publish(ctx):
    record("publish")
    if ctx.get("interrupt"):
        raise KeyboardInterrupt
    out = STATE / "published.json"
    out.write_text(str(ctx.outputs["transform"]))
    return str(out)


nightly = Workflow("nightly")
nightly.add_step("load", load)
nightly.add_step("transform", transform, retries=1, retry_delay=0.01)
nightly.add_step("slow", slow, timeout=2)
nightly.add_step("publish", publish)
"""


@pytest.fixture
def flow(tmp_path, monkeypatch):
    state = tmp_path / "state"
    state.mkdir()
    path = tmp_path / "nightly.py"
    path.write_text(textwrap.dedent(WORKFLOW_FILE))
    monkeypatch.setenv("WF_STATE", str(state))
    return path, state


def calls(state: Path) -> list[str]:
    log = state / "calls.log"
    return log.read_text().split() if log.exists() else []


def test_cli_in_process_full_cycle(flow, tmp_path, capsys):
    path, state = flow
    ckpt = tmp_path / "ckpt" / "nightly.json"
    report = tmp_path / "reports" / "nightly.json"
    base = ["--workflow", f"{path}:nightly", "--checkpoint", str(ckpt), "--data", "rows=21"]

    (state / "fail_transform").touch()
    code = main(base + ["--data", "interrupt=true", "--json-report", str(report)])
    captured = capsys.readouterr()
    assert code == EXIT_INTERRUPTED
    assert calls(state) == ["load", "transform", "transform", "slow", "publish"]
    assert "Checkpoint kept" in captured.err and captured.out == ""
    assert not report.exists()  # an interrupt produces no result to report
    completed = json.loads(ckpt.read_text())["payload"]["completed"]
    assert [c["name"] for c in completed] == ["load", "transform", "slow"]
    assert completed[1]["attempts"] == 2 and completed[1]["output"] == 42

    # The interrupt flag was checkpointed with the run's data; override it to resume.
    code = main(base + ["--data", "interrupt=false", "--json-report", str(report)])
    out = capsys.readouterr().out
    assert code == EXIT_OK
    assert calls(state)[5:] == ["publish"]
    assert "1. load: succeeded (restored from checkpoint)" in out
    assert "3. slow: succeeded (restored from checkpoint)" in out
    assert (state / "published.json").read_text() == "42"
    data = json.loads(report.read_text())
    assert data["restored"] == ["load", "transform", "slow"] and data["succeeded"]
    assert data["steps"][1]["retries"] == 1  # attempt history is carried over from the checkpoint
    assert not ckpt.exists()


def test_cli_in_process_timeout_then_resume_and_restart(flow, tmp_path, capsys, caplog):
    path, state = flow
    ckpt = tmp_path / "nightly.json"
    base = ["--workflow", f"{path}:nightly", "--checkpoint", str(ckpt), "--data", "rows=1"]
    (state / "hang").touch()
    with caplog.at_level(logging.ERROR):
        code = main(base + ["--json-report", "-"])
    out, err = capsys.readouterr()
    assert code == EXIT_FAILED and "timed out after 2s" in caplog.text
    assert "Checkpoint kept" in err
    data = json.loads(out)  # stdout is pure JSON even on failure
    assert data["steps"][2]["timed_out"] and data["steps"][2]["status"] == "failed"
    assert data["skipped"] == ["publish"]
    (state / "hang").unlink()
    assert main(base) == EXIT_OK
    assert calls(state) == ["load", "transform", "slow", "slow", "publish"]
    assert main(base + ["--restart"]) == EXIT_OK  # nothing to discard, full fresh run
    assert calls(state)[5:] == ["load", "transform", "slow", "publish"]


def test_cli_recovers_from_a_corrupted_checkpoint(flow, tmp_path, capsys, caplog):
    path, state = flow
    ckpt = tmp_path / "nightly.json"
    base = ["--workflow", f"{path}:nightly", "--checkpoint", str(ckpt), "--data", "rows=2"]
    assert main(base + ["--data", "interrupt=true"]) == EXIT_INTERRUPTED
    ckpt.write_text(ckpt.read_text().replace('"rows": 2', '"rows": 999'))  # tampered
    capsys.readouterr()
    with caplog.at_level(logging.WARNING):
        assert main(base) == EXIT_OK
    assert "checksum does not match" in caplog.text
    assert "restored from checkpoint" not in capsys.readouterr().out
    assert calls(state) == ["load", "transform", "slow", "publish"] * 2
    assert (state / "published.json").read_text() == "4"


def run_cli(args: list[str], env: dict[str, str], **kwargs) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-m", "automation_toolkit.workflow_cli", *args],
        capture_output=True,
        text=True,
        env=env,
        cwd=ROOT,
        timeout=60,
        **kwargs,
    )


def test_cli_subprocess_killed_mid_step_resumes_from_durable_checkpoint(flow, tmp_path):
    path, state = flow
    env = {**os.environ, "WF_STATE": str(state), "PYTHONPATH": str(ROOT / "src")}
    ckpt = tmp_path / "nightly.json"
    base = ["--workflow", f"{path}:nightly", "--checkpoint", str(ckpt), "--data", "rows=5"]

    (state / "hang").touch()
    process = subprocess.Popen(
        [sys.executable, "-m", "automation_toolkit.workflow_cli", *base],
        env=env,
        cwd=ROOT,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    deadline = time.time() + 30
    while not (state / "hanging").exists():
        assert process.poll() is None, process.communicate()
        assert time.time() < deadline, "the slow step never started"
        time.sleep(0.02)
    process.send_signal(signal.SIGKILL)  # simulated power loss during the third step
    process.wait(timeout=30)
    assert calls(state) == ["load", "transform", "slow"]
    assert ckpt.exists() and not ckpt.with_name("nightly.json.tmp").exists()
    assert [c["name"] for c in json.loads(ckpt.read_text())["payload"]["completed"]] == [
        "load",
        "transform",
    ]

    (state / "hang").unlink()
    completed = run_cli(base + ["--json-report", "-"], env)
    assert completed.returncode == EXIT_OK, completed.stderr
    assert "Resuming workflow 'nightly' from checkpoint: 2 of 4 steps" in completed.stderr
    report = json.loads(completed.stdout)
    assert report["restored"] == ["load", "transform"] and report["succeeded"]
    assert calls(state)[3:] == ["slow", "publish"]
    assert (state / "published.json").read_text() == "10" and not ckpt.exists()


def test_cli_subprocess_exit_codes_and_describe(flow, tmp_path):
    path, state = flow
    env = {**os.environ, "WF_STATE": str(state), "PYTHONPATH": str(ROOT / "src")}
    spec = ["--workflow", f"{path}:nightly"]

    described = run_cli(spec + ["--describe"], env)
    assert described.returncode == EXIT_OK and calls(state) == []
    assert described.stdout.splitlines()[0] == "Workflow 'nightly' (4 steps):"
    assert "2. transform (retries=1, delays=0.01s)" in described.stdout
    assert "3. slow (retries=0, timeout=2s)" in described.stdout

    failed = run_cli(spec + ["--data", "rows=1", "--data", "interrupt=true"], env)
    assert failed.returncode == EXIT_INTERRUPTED and "Interrupted" in failed.stderr

    bad = run_cli(["--workflow", f"{path}:missing"], env)
    assert bad.returncode == EXIT_FAILED and "has no attribute 'missing'" in bad.stderr

    ok = run_cli(spec + ["--data", "rows=3", "--json-report", "-"], env)
    assert ok.returncode == EXIT_OK and json.loads(ok.stdout)["succeeded"] is True

    usage = run_cli(spec + ["--restart"], env)
    assert usage.returncode == 2 and "--restart requires --checkpoint" in usage.stderr


def test_console_script_entry_point_resolves():
    """The installed metadata (not a stale build folder) registers the new console script."""
    from importlib.metadata import distributions

    values = {
        ep.value
        for dist in distributions()
        if dist.metadata["Name"] == "python-automation-toolkit"
        for ep in dist.entry_points
        if ep.group == "console_scripts" and ep.name == "automation-toolkit-workflow"
    }
    if not values:  # pragma: no cover - needs `pip install -e .` after this change
        pytest.skip("package metadata predates the workflow CLI; reinstall with pip install -e .")
    assert values == {"automation_toolkit.workflow_cli:main"}


def test_optional_failure_exit_code_from_a_python_defined_workflow(tmp_path, capsys):
    module = tmp_path / "opt.py"
    module.write_text(
        textwrap.dedent(
            """
            from automation_toolkit.workflow import Workflow

            def build():
                wf = Workflow("opt")
                wf.add_step("ok", lambda ctx: 1)
                wf.add_step("maybe", lambda ctx: 1 / 0, continue_on_error=True, retries=1)
                wf.add_step("after", lambda ctx: sorted(ctx.outputs))
                return wf
            """
        )
    )
    assert main(["--workflow", f"{module}:build", "--json-report", "-"]) == EXIT_PARTIAL
    report = json.loads(capsys.readouterr().out)
    assert report["failed"] == ["maybe"] and report["steps"][1]["attempts"] == 2
    assert report["steps"][1]["error"]["type"] == "ZeroDivisionError"
    assert report["steps"][2]["output"] == ["ok"]
