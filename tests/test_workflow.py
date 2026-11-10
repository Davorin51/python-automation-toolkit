"""Sequential workflow engine: ordering, shared context, failure handling and retries."""

import json
import logging
import os
import threading
import time
from pathlib import Path

import pytest

from automation_toolkit.config import Config
from automation_toolkit.exceptions import StepTimeoutError, ToolkitError, WorkflowError
from automation_toolkit.ingestion import load_files
from automation_toolkit.pipeline import run_pipeline
from automation_toolkit.processing.analytics import analyze
from automation_toolkit.processing.validation import validate
from automation_toolkit.workflow import (
    Step,
    StepStatus,
    Workflow,
    WorkflowCheckpoint,
    WorkflowContext,
)


def test_steps_run_in_order_and_share_context():
    calls: list[str] = []

    def first(ctx: WorkflowContext) -> int:
        calls.append("first")
        ctx["seen"] = ["first"]
        return ctx["start"] + 1

    def second(ctx: WorkflowContext) -> int:
        calls.append("second")
        ctx["seen"].append("second")
        return ctx.outputs["first"] * 10

    def third(ctx: WorkflowContext) -> str:
        calls.append("third")
        return f"{ctx.outputs['second']}-{ctx.get('missing', 'default')}"

    workflow = Workflow("demo").add_step("first", first).add_step("second", second)
    workflow.add_step("third", third)
    result = workflow.run({"start": 1})

    assert calls == ["first", "second", "third"]
    assert result.succeeded
    assert [s.status for s in result.steps] == [StepStatus.SUCCEEDED] * 3
    assert dict(result.outputs) == {"first": 2, "second": 20, "third": "20-default"}
    assert result.step("second").output == 20 and result.step("second").attempts == 1
    assert result.context["seen"] == ["first", "second"]
    assert "missing" not in result.context
    assert result.summary() == "Workflow 'demo': 3 steps, 3 succeeded, 0 failed, 0 skipped"
    assert all(s.duration_seconds >= 0 for s in result.steps)


def test_required_step_failure_stops_run_and_skips_the_rest(caplog):
    ran: list[str] = []

    def boom(ctx):
        raise ValueError("bad input")

    workflow = Workflow("fail-fast")
    workflow.add_step("prepare", lambda ctx: ran.append("prepare"))
    workflow.add_step("explode", boom)
    workflow.add_step("never", lambda ctx: ran.append("never"))

    with caplog.at_level(logging.INFO), pytest.raises(WorkflowError) as info:
        workflow.run()

    assert ran == ["prepare"]
    message = str(info.value)
    assert "failed at step 'explode' after 1 attempt: ValueError: bad input" in message
    assert isinstance(info.value.__cause__, ValueError)
    result = info.value.result
    assert not result.succeeded
    assert [s.status for s in result.steps] == [
        StepStatus.SUCCEEDED,
        StepStatus.FAILED,
        StepStatus.SKIPPED,
    ]
    assert result.failed == ["explode"] and result.skipped == ["never"]
    assert isinstance(result.step("explode").error, ValueError)
    assert "explode" not in result.outputs and "prepare" in result.outputs
    assert "stopped at step 'explode'" in caplog.text


def test_continue_on_error_records_failure_and_proceeds(caplog):
    def flaky(ctx):
        raise RuntimeError("optional thing broke")

    workflow = Workflow("lenient")
    workflow.add_step("optional", flaky, continue_on_error=True)
    workflow.add_step("after", lambda ctx: sorted(ctx.outputs))

    with caplog.at_level(logging.WARNING):
        result = workflow.run()

    assert not result.succeeded
    assert result.failed == ["optional"] and result.skipped == []
    assert result.step("after").succeeded and result.step("after").output == []
    assert isinstance(result.step("optional").error, RuntimeError)
    assert (
        "Step 'optional' failed after 1 attempt (RuntimeError: optional thing broke); continuing"
        in (caplog.text)
    )
    assert result.summary() == "Workflow 'lenient': 2 steps, 1 succeeded, 1 failed, 0 skipped"


def test_retries_until_success(caplog):
    attempts = 0

    def eventually(ctx):
        nonlocal attempts
        attempts += 1
        if attempts < 3:
            raise ConnectionError(f"try {attempts}")
        return "ok"

    with caplog.at_level(logging.WARNING):
        result = Workflow("retry").add_step("fetch", eventually, retries=2).run()

    assert result.succeeded and attempts == 3
    assert result.step("fetch").attempts == 3
    assert result.step("fetch").error is None
    assert result.outputs["fetch"] == "ok"
    assert caplog.text.count("retrying") == 2


def test_retries_exhausted_reports_attempt_count():
    def always(ctx):
        raise OSError("still down")

    with pytest.raises(WorkflowError, match="after 3 attempts: OSError: still down") as info:
        Workflow("retry").add_step("fetch", always, retries=2).run()
    assert info.value.result.step("fetch").attempts == 3


def test_interrupts_are_not_swallowed():
    def interrupt(ctx):
        raise KeyboardInterrupt

    workflow = Workflow("ctrl-c").add_step("wait", interrupt, retries=5, continue_on_error=True)
    with pytest.raises(KeyboardInterrupt):
        workflow.run()


@pytest.mark.parametrize(
    "kwargs, match",
    [
        ({"name": "", "action": print}, "non-empty"),
        ({"name": "   ", "action": print}, "non-empty"),
        ({"name": 3, "action": print}, "non-empty"),
        ({"name": "x", "action": "not callable"}, "callable"),
        ({"name": "x", "action": print, "retries": -1}, "retries"),
        ({"name": "x", "action": print, "retries": 1.5}, "retries"),
        ({"name": "x", "action": print, "retries": True}, "retries"),
    ],
)
def test_invalid_step_definitions(kwargs, match):
    with pytest.raises(WorkflowError, match=match):
        Step(**kwargs)


def test_invalid_workflow_definitions():
    with pytest.raises(WorkflowError, match="Workflow name"):
        Workflow("")
    workflow = Workflow("dupes").add_step("load", print)
    with pytest.raises(WorkflowError, match="already has a step named 'load'"):
        workflow.add_step("load", print)
    with pytest.raises(WorkflowError, match="Expected a Step"):
        workflow.add(("load", print))  # type: ignore[arg-type]
    with pytest.raises(WorkflowError, match="has no steps"):
        Workflow("empty").run()
    with pytest.raises(KeyError):
        workflow.add_step("other", lambda ctx: None).run().step("unknown")
    assert len(workflow) == 2 and [s.name for s in workflow.steps] == ["load", "other"]


def test_workflow_errors_are_toolkit_errors():
    assert issubclass(WorkflowError, ToolkitError)


def test_each_run_gets_a_fresh_context():
    workflow = Workflow("rerun", [Step("count", lambda ctx: ctx.data.setdefault("n", 0) + 1)])
    seed = {"n": 5}
    first = workflow.run(seed)
    second = workflow.run()
    assert first.outputs["count"] == 6 and second.outputs["count"] == 1
    assert seed == {"n": 5}  # the caller's mapping is copied, not mutated
    assert first.context is not second.context


def test_toolkit_functions_compose_into_a_workflow(sales_file, tmp_path):
    """The engine wraps the existing ingestion, validation and analytics unchanged."""
    config = Config()
    workflow = (
        Workflow("sales")
        .add_step("load", lambda ctx: load_files(ctx["inputs"], config))
        .add_step("validate", lambda ctx: validate(ctx.outputs["load"], config))
        .add_step("analyze", lambda ctx: analyze(ctx.outputs["validate"].transactions))
    )
    result = workflow.run({"inputs": [sales_file]})
    expected = run_pipeline([sales_file], tmp_path / "reports", config, "excel")
    assert result.succeeded
    assert result.outputs["analyze"].total_revenue_cents == expected.analytics.total_revenue_cents
    assert result.outputs["analyze"].orders == expected.analytics.orders == 2
    assert result.outputs["validate"].invalid_rows == 0


# --- Retry delays with exponential backoff -------------------------------------------------


class FakeClock:
    """Records requested sleeps instead of waiting."""

    def __init__(self) -> None:
        self.sleeps: list[float] = []

    def __call__(self, seconds: float) -> None:
        self.sleeps.append(seconds)


def failing_times(count: int, error=ConnectionError):
    """An action that fails ``count`` times and then returns the attempt number."""
    attempts = 0

    def action(ctx):
        nonlocal attempts
        attempts += 1
        if attempts <= count:
            raise error(f"try {attempts}")
        return attempts

    return action


def test_exponential_backoff_schedule_is_waited_between_retries(caplog):
    clock = FakeClock()
    workflow = Workflow("backoff", sleep=clock)
    workflow.add_step("fetch", failing_times(3), retries=3, retry_delay=0.1, backoff=2.0)

    with caplog.at_level(logging.WARNING):
        result = workflow.run()

    assert result.succeeded and result.outputs["fetch"] == 4
    assert clock.sleeps == pytest.approx([0.1, 0.2, 0.4])
    assert result.step("fetch").delays == pytest.approx([0.1, 0.2, 0.4])
    assert len(result.step("fetch").errors) == 3
    assert result.step("fetch").error is None  # cleared by the successful attempt
    assert "retrying in 0.10s" in caplog.text and "retrying in 0.40s" in caplog.text


def test_max_delay_caps_the_backoff():
    step = Step("s", print, retries=5, retry_delay=1, backoff=3, max_delay=10)
    assert step.retry_schedule() == pytest.approx([1, 3, 9, 10, 10])
    assert step.delay_before_retry(1) == 1.0 and isinstance(step.delay_before_retry(1), float)
    with pytest.raises(ValueError, match="at least 1"):
        step.delay_before_retry(0)


def test_constant_delay_when_backoff_is_one():
    clock = FakeClock()
    workflow = Workflow("constant", sleep=clock)
    workflow.add_step("s", failing_times(2), retries=2, retry_delay=0.5, backoff=1)
    assert workflow.run().succeeded
    assert clock.sleeps == [0.5, 0.5]


def test_zero_delay_never_calls_sleep_and_logs_immediate_retry(caplog):
    clock = FakeClock()
    workflow = Workflow("fast", sleep=clock).add_step("s", failing_times(2), retries=2)
    with caplog.at_level(logging.WARNING):
        assert workflow.run().succeeded
    assert clock.sleeps == []
    assert caplog.text.count("retrying immediately") == 2
    assert workflow.steps[0].retry_schedule() == [0.0, 0.0]


def test_no_delay_after_the_final_failed_attempt():
    clock = FakeClock()
    workflow = Workflow("exhausted", sleep=clock)
    workflow.add_step("s", failing_times(99), retries=2, retry_delay=1, continue_on_error=True)
    result = workflow.run()
    assert result.failed == ["s"]
    assert clock.sleeps == [1, 2]  # two retries, no wait after the last failure
    assert result.step("s").delays == [1, 2]
    assert [str(e) for e in result.step("s").errors] == ["try 1", "try 2", "try 3"]


def test_delay_is_irrelevant_without_retries():
    clock = FakeClock()
    workflow = Workflow("single", sleep=clock)
    workflow.add_step("s", failing_times(1), retry_delay=5, continue_on_error=True)
    assert workflow.run().failed == ["s"]
    assert clock.sleeps == [] and workflow.steps[0].retry_schedule() == []


def test_real_sleep_is_used_by_default():
    assert Workflow.__init__.__kwdefaults__["sleep"] is time.sleep
    workflow = Workflow("default-sleep").add_step(
        "s", failing_times(1), retries=1, retry_delay=0.01
    )
    before = time.perf_counter()
    result = workflow.run()
    assert result.succeeded and result.step("s").delays == [0.01]
    assert time.perf_counter() - before >= 0.01
    assert result.step("s").duration_seconds >= 0.01  # includes the retry wait


def test_interrupt_during_sleep_propagates():
    def interrupting_sleep(seconds):
        raise KeyboardInterrupt

    workflow = Workflow("sleepy", sleep=interrupting_sleep)
    workflow.add_step("s", failing_times(1), retries=1, retry_delay=1, continue_on_error=True)
    with pytest.raises(KeyboardInterrupt):
        workflow.run()


@pytest.mark.parametrize(
    "kwargs, match",
    [
        ({"retry_delay": -0.1}, "retry_delay must be a finite number at least 0"),
        ({"retry_delay": True}, "retry_delay"),
        ({"retry_delay": "1"}, "retry_delay"),
        ({"retry_delay": float("nan")}, "retry_delay"),
        ({"retry_delay": float("inf")}, "retry_delay"),
        ({"backoff": 0.5}, "backoff must be a finite number at least 1"),
        ({"backoff": 0}, "backoff"),
        ({"backoff": None}, "backoff"),
        ({"max_delay": -1}, "max_delay must be a finite number at least 0"),
        ({"max_delay": False}, "max_delay"),
        ({"timeout": 0}, "timeout must be a finite number greater than 0"),
        ({"timeout": -2}, "timeout"),
        ({"timeout": True}, "timeout"),
        ({"timeout": "10"}, "timeout"),
        ({"timeout": float("inf")}, "timeout"),
    ],
)
def test_invalid_retry_and_timeout_settings(kwargs, match):
    with pytest.raises(WorkflowError, match=match):
        Step("s", print, **kwargs)
    with pytest.raises(WorkflowError, match=match):
        Workflow("w").add_step("s", print, **kwargs)


def test_boundary_settings_are_accepted():
    step = Step("s", print, retry_delay=0, backoff=1, max_delay=0, timeout=0.001)
    assert step.retry_schedule() == []
    assert Step("s", print, retries=2, retry_delay=3, max_delay=0).retry_schedule() == [0, 0]
    with pytest.raises(WorkflowError, match="sleep must be callable"):
        Workflow("w", sleep="nap")  # type: ignore[arg-type]


# --- Per-step timeouts -----------------------------------------------------------------------


def blocking_action(release: threading.Event, finished: threading.Event, value="late"):
    """Waits until ``release`` is set, then returns ``value`` and signals ``finished``."""

    def action(ctx):
        release.wait(30)
        finished.set()
        return value

    return action


def test_timed_out_step_fails_and_late_result_is_discarded():
    release, finished = threading.Event(), threading.Event()
    workflow = Workflow("slow").add_step("hang", blocking_action(release, finished), timeout=0.05)
    workflow.add_step("after", lambda ctx: "ran")

    with pytest.raises(WorkflowError) as info:
        workflow.run()

    result = info.value.result
    hang = result.step("hang")
    assert hang.status is StepStatus.FAILED and hang.timed_out
    assert isinstance(hang.error, StepTimeoutError) and isinstance(hang.error, TimeoutError)
    assert hang.error_message == (
        "StepTimeoutError: Step 'hang' timed out after 0.05s; the attempt was abandoned."
    )
    assert "timed out after 0.05s" in str(info.value) and info.value.step == "hang"
    assert result.skipped == ["after"]
    # The abandoned attempt finishes later; its value never reaches the outputs.
    release.set()
    assert finished.wait(5)
    assert hang.output is None and "hang" not in result.outputs


def test_timed_out_step_is_retried_with_backoff_and_can_succeed(caplog):
    clock = FakeClock()
    attempts = 0
    release = threading.Event()

    def sometimes_slow(ctx):
        nonlocal attempts
        attempts += 1
        if attempts < 3:
            release.wait(30)
        return attempts

    workflow = Workflow("retry-timeout", sleep=clock)
    workflow.add_step("io", sometimes_slow, retries=2, retry_delay=0.1, timeout=0.05)
    with caplog.at_level(logging.WARNING):
        result = workflow.run()
    release.set()

    assert result.succeeded and result.outputs["io"] == 3
    assert result.step("io").attempts == 3 and clock.sleeps == pytest.approx([0.1, 0.2])
    assert [type(e) for e in result.step("io").errors] == [StepTimeoutError, StepTimeoutError]
    assert not result.step("io").timed_out  # the final attempt succeeded
    assert caplog.text.count("timed out after 0.05s") == 2


def test_timed_out_optional_step_lets_the_workflow_continue():
    release = threading.Event()
    workflow = Workflow("optional-timeout")
    workflow.add_step("slow", lambda ctx: release.wait(30), timeout=0.05, continue_on_error=True)
    workflow.add_step("after", lambda ctx: "ran")
    result = workflow.run()
    release.set()
    assert not result.succeeded and result.failed == ["slow"]
    assert result.outputs == {"after": "ran"} and result.step("slow").timed_out


def test_fast_step_with_timeout_returns_its_value_and_sees_the_context():
    workflow = Workflow("fast-timeout")
    workflow.add_step("a", lambda ctx: ctx["x"] * 2, timeout=5)
    workflow.add_step("b", lambda ctx: ctx.outputs["a"] + 1, timeout=5)
    result = workflow.run({"x": 20})
    assert result.succeeded and dict(result.outputs) == {"a": 40, "b": 41}
    assert all(not s.timed_out and s.errors == [] for s in result.steps)


def test_exception_inside_timed_step_keeps_its_type_and_traceback():
    def broken(ctx):
        raise KeyError("missing-key")

    workflow = Workflow("typed").add_step("broken", broken, timeout=5)
    with pytest.raises(WorkflowError, match="KeyError: 'missing-key'") as info:
        workflow.run()
    step = info.value.result.step("broken")
    assert isinstance(step.error, KeyError) and not step.timed_out
    assert isinstance(info.value.__cause__, KeyError)
    assert "in broken" in step.traceback and "KeyError" in step.traceback


def test_interrupt_inside_timed_step_propagates():
    def interrupt(ctx):
        raise KeyboardInterrupt

    workflow = Workflow("ctrl-c-thread")
    workflow.add_step("wait", interrupt, timeout=5, retries=3, continue_on_error=True)
    with pytest.raises(KeyboardInterrupt):
        workflow.run()


def test_timed_step_runs_in_a_daemon_helper_thread():
    seen: dict[str, object] = {}

    def record(ctx):
        current = threading.current_thread()
        seen["name"], seen["daemon"], seen["main"] = (
            current.name,
            current.daemon,
            current is threading.main_thread(),
        )

    Workflow("thread").add_step("record", record, timeout=5).run()
    assert seen == {"name": "workflow-step-record", "daemon": True, "main": False}
    Workflow("inline").add_step("record", record).run()
    assert seen["main"] is True  # no timeout: the step runs in the caller's thread


# --- Error reporting -------------------------------------------------------------------------


def test_failure_message_names_position_duration_earlier_attempts_and_skipped_steps():
    workflow = Workflow("report", sleep=FakeClock())
    workflow.add_step("ok", lambda ctx: 1)
    workflow.add_step("bad", failing_times(99, error=OSError), retries=2, retry_delay=1)
    workflow.add_step("later", lambda ctx: 2)
    workflow.add_step("last", lambda ctx: 3)

    with pytest.raises(WorkflowError) as info:
        workflow.run()

    message = str(info.value)
    assert message.startswith(
        "Workflow 'report' failed at step 'bad' after 3 attempts: OSError: try 3 [step 2 of 4, "
    )
    assert "earlier attempts: OSError: try 1; OSError: try 2" in message
    assert message.endswith("; skipped: later, last]")
    assert info.value.step == "bad" and info.value.result.skipped == ["later", "last"]


def test_step_result_records_errors_traceback_and_timing():
    def explode(ctx):
        raise ValueError("bad input")

    workflow = Workflow("details").add_step("explode", explode, continue_on_error=True)
    result = workflow.run()
    step = result.step("explode")
    assert step.errors == [step.error] and isinstance(step.error, ValueError)
    assert step.error_message == "ValueError: bad input"
    assert step.traceback.startswith("Traceback (most recent call last):")
    assert 'raise ValueError("bad input")' in step.traceback and step.traceback.endswith(
        "ValueError: bad input\n"
    )
    assert step.started_at is not None and step.finished_at is not None
    assert step.started_at.tzinfo is not None and step.finished_at >= step.started_at
    assert step.describe() == "explode: failed after 1 attempt in 0.00s (ValueError: bad input)"
    assert result.duration_seconds == pytest.approx(step.duration_seconds)


def test_error_message_for_exception_without_text():
    def bare(ctx):
        raise RuntimeError

    step = Workflow("bare").add_step("bare", bare, continue_on_error=True).run().step("bare")
    assert step.error_message == "RuntimeError"
    assert step.describe().endswith("(RuntimeError)")


def test_report_lists_every_step_attempt_and_optionally_tracebacks():
    workflow = Workflow("report", sleep=FakeClock())
    workflow.add_step("load", lambda ctx: "data")
    workflow.add_step("flaky", failing_times(1, error=TimeoutError), retries=1, retry_delay=0.5)
    workflow.add_step("optional", failing_times(99), continue_on_error=True)
    workflow.add_step("explode", failing_times(99, error=ValueError), retries=1)
    workflow.add_step("never", lambda ctx: None)

    with pytest.raises(WorkflowError) as info:
        workflow.run()
    report = info.value.result.report()
    lines = report.splitlines()

    assert lines[0].startswith("Workflow 'report': 5 steps, 2 succeeded, 2 failed, 1 skipped in ")
    assert lines[1].startswith("  1. load: succeeded after 1 attempt in ")
    assert lines[2].startswith("  2. flaky: succeeded after 2 attempts in ")
    assert lines[3] == "       attempt 1: TimeoutError: try 1 (waited 0.50s before retrying)"
    assert lines[4].startswith("  3. optional: failed after 1 attempt in ")
    assert lines[4].endswith("(ConnectionError: try 1)")
    assert lines[5] == "       attempt 1: ConnectionError: try 1"
    assert lines[6].startswith("  4. explode: failed after 2 attempts in ")
    assert lines[7] == "       attempt 1: ValueError: try 1 (retried immediately)"
    assert lines[8] == "       attempt 2: ValueError: try 2"
    assert lines[9] == "  5. never: skipped"
    assert len(lines) == 10 and "Traceback" not in report

    detailed = info.value.result.report(include_traceback=True)
    assert detailed.count("Traceback (most recent call last):") == 2  # optional and explode
    assert "       ValueError: try 2" in detailed


def test_report_of_a_successful_run_has_no_attempt_lines():
    result = Workflow("clean").add_step("a", lambda ctx: 1).add_step("b", lambda ctx: 2).run()
    lines = result.report(include_traceback=True).splitlines()
    assert len(lines) == 3 and not any(line.startswith("       attempt") for line in lines)
    assert result.step("a").describe().startswith("a: succeeded after 1 attempt in ")


def test_failure_details_are_logged_at_debug_level(caplog):
    workflow = Workflow("logs").add_step("bad", failing_times(99, error=ValueError))
    with caplog.at_level(logging.DEBUG), pytest.raises(WorkflowError):
        workflow.run()
    assert "Failure details:" in caplog.text
    assert "Traceback (most recent call last):" in caplog.text
    assert "Workflow 'logs': 1 steps, 0 succeeded, 1 failed, 0 skipped; stopped at step 'bad'" in (
        caplog.text
    )


def test_definition_errors_carry_no_step_or_result():
    with pytest.raises(WorkflowError) as info:
        Workflow("empty").run()
    assert info.value.step is None and info.value.result is None
    assert issubclass(StepTimeoutError, WorkflowError) and issubclass(
        StepTimeoutError, ToolkitError
    )


# --- Checkpointing and recovery ----------------------------------------------------------


def counting_workflow(calls: list[str], fail_at: str | None = None, **kwargs) -> Workflow:
    """Three steps that record their calls; ``fail_at`` names a step that raises once armed."""

    def make(name):
        def action(ctx):
            calls.append(name)
            if name == fail_at and ctx.get("arm", True):
                raise RuntimeError(f"{name} broke")
            ctx[f"{name}_ran"] = True
            return {"step": name, "upstream": sorted(ctx.outputs)}

        return action

    workflow = Workflow("ckpt", **kwargs)
    for name in ("load", "transform", "publish"):
        workflow.add_step(name, make(name))
    return workflow


def read_checkpoint(path: Path) -> dict:
    return json.loads(path.read_text())


def test_failed_run_leaves_checkpoint_and_resume_skips_completed_steps(tmp_path, caplog):
    path = tmp_path / "state" / "ckpt.json"
    calls: list[str] = []
    workflow = counting_workflow(calls, fail_at="transform")

    with pytest.raises(WorkflowError, match="transform broke"):
        workflow.run({"arm": True}, checkpoint=path)
    assert calls == ["load", "transform"]
    assert path.exists() and not path.with_name("ckpt.json.tmp").exists()
    document = read_checkpoint(path)
    assert set(document) == {"payload", "checksum"}
    payload = document["payload"]
    assert payload["workflow"] == "ckpt" and payload["steps"] == ["load", "transform", "publish"]
    assert [c["name"] for c in payload["completed"]] == ["load"]
    assert payload["completed"][0]["output"] == {"step": "load", "upstream": []}
    assert payload["data"] == {"arm": True, "load_ran": True}

    calls.clear()
    with caplog.at_level(logging.INFO):
        result = workflow.run({"arm": False}, checkpoint=path)

    assert calls == ["transform", "publish"]  # load was not repeated
    assert result.succeeded and result.resumed and result.restored == ["load"]
    assert result.step("load").restored and result.step("load").attempts == 1
    assert result.step("load").output == {"step": "load", "upstream": []}
    assert result.outputs["transform"] == {"step": "transform", "upstream": ["load"]}
    assert result.outputs["publish"]["upstream"] == ["load", "transform"]
    assert result.context["load_ran"] is True and result.context["arm"] is False
    assert "Resuming workflow 'ckpt' from checkpoint: 1 of 3 steps already completed" in (
        caplog.text
    )
    assert result.step("load").describe() == "load: succeeded (restored from checkpoint)"
    assert "1. load: succeeded (restored from checkpoint)" in result.report()
    assert not path.exists()  # a completed run removes its checkpoint


def test_initial_data_of_the_resuming_run_overrides_checkpointed_data(tmp_path):
    path = tmp_path / "ckpt.json"
    workflow = Workflow("precedence")
    workflow.add_step("set", lambda ctx: ctx.update_data(value="from step", produced="kept"))
    workflow.add_step("fail", failing_times(1, error=OSError))
    with pytest.raises(WorkflowError):
        workflow.run({"value": "initial", "other": 1}, checkpoint=path)
    result = workflow.run({"value": "changed", "extra": 3}, checkpoint=path)
    # Step-produced and untouched keys survive; keys passed again are overridden.
    assert result.context.data == {"value": "changed", "produced": "kept", "other": 1, "extra": 3}
    plain = workflow.run({"value": "fresh"})
    assert plain.context.data == {"value": "from step", "produced": "kept"}


def test_interrupted_twice_then_completes_without_repeating_work(tmp_path):
    path = tmp_path / "ckpt.json"
    calls: list[str] = []
    attempts = {"n": 0}

    def flaky_publish(ctx):
        calls.append("publish")
        attempts["n"] += 1
        if attempts["n"] < 3:
            raise KeyboardInterrupt
        return "done"

    workflow = Workflow("twice")
    workflow.add_step("load", lambda ctx: calls.append("load") or "data")
    workflow.add_step("transform", lambda ctx: calls.append("transform") or "shaped")
    workflow.add_step("publish", flaky_publish)
    for _ in range(2):
        with pytest.raises(KeyboardInterrupt):
            workflow.run(checkpoint=path)
    result = workflow.run(checkpoint=path)
    assert calls == ["load", "transform", "publish", "publish", "publish"]
    assert result.restored == ["load", "transform"] and result.outputs["publish"] == "done"


def test_fully_completed_checkpoint_restores_every_step(tmp_path):
    path = tmp_path / "ckpt.json"
    workflow = Workflow("all").add_step("a", lambda ctx: 1).add_step("b", lambda ctx: 2)
    # Simulate a crash after the last step was checkpointed but before cleanup.
    checkpoint = WorkflowCheckpoint(path)
    first = workflow.run()
    checkpoint.save(workflow, first.steps, first.context)
    calls: list[str] = []
    rerun = Workflow("all")
    rerun.add_step("a", lambda ctx: calls.append("a"))
    rerun.add_step("b", lambda ctx: calls.append("b"))
    result = rerun.run(checkpoint=checkpoint)
    assert (
        calls == [] and result.restored == ["a", "b"] and dict(result.outputs) == {"a": 1, "b": 2}
    )
    assert not path.exists()


def test_failed_optional_step_is_rerun_and_not_checkpointed(tmp_path):
    path = tmp_path / "ckpt.json"
    calls: list[str] = []

    def optional(ctx):
        calls.append("optional")
        raise ValueError("flaky")

    def required(ctx):
        calls.append("required")
        if ctx.get("arm"):
            raise OSError("down")
        return "ok"

    workflow = Workflow("optional")
    workflow.add_step("first", lambda ctx: calls.append("first") or "x")
    workflow.add_step("optional", optional, continue_on_error=True)
    workflow.add_step("required", required)
    with pytest.raises(WorkflowError, match="down"):
        workflow.run({"arm": True}, checkpoint=path)
    assert [c["name"] for c in read_checkpoint(path)["payload"]["completed"]] == ["first"]
    calls.clear()
    result = workflow.run({"arm": False}, checkpoint=path)
    assert calls == ["optional", "required"]
    assert result.restored == ["first"] and result.failed == ["optional"]
    assert not result.succeeded and not path.exists()


def test_run_without_checkpoint_writes_nothing(tmp_path):
    workflow = Workflow("plain").add_step("a", lambda ctx: 1)
    workflow.run()
    assert list(tmp_path.iterdir()) == []


def test_checkpoint_accepts_path_string_and_object(tmp_path):
    targets = (str(tmp_path / "s.json"), tmp_path / "p.json", WorkflowCheckpoint(tmp_path / "o"))
    for target in targets:
        workflow = Workflow("kinds").add_step("a", lambda ctx: 1).add_step("b", failing_times(1))
        with pytest.raises(WorkflowError):
            workflow.run(checkpoint=target)
        assert workflow.run(checkpoint=target).restored == ["a"]
    assert sorted(p.name for p in tmp_path.iterdir()) == []


def test_checkpoint_write_is_atomic(tmp_path, monkeypatch):
    path = tmp_path / "ckpt.json"
    replaced: list[tuple[Path, Path]] = []
    real_replace = os.replace

    def spy(src, dst):
        replaced.append((Path(src), Path(dst)))
        assert Path(src).read_text().strip().endswith("}")  # the temp file is complete
        real_replace(src, dst)

    monkeypatch.setattr("automation_toolkit.workflow.os.replace", spy)
    fsyncs: list[int] = []
    monkeypatch.setattr("automation_toolkit.workflow.os.fsync", lambda fd: fsyncs.append(fd))
    workflow = Workflow("atomic").add_step("a", lambda ctx: 1).add_step("b", failing_times(1))
    with pytest.raises(WorkflowError):
        workflow.run(checkpoint=path)
    assert replaced == [(path.with_name("ckpt.json.tmp"), path)]
    assert len(fsyncs) == 2  # the temporary file, then its directory
    assert not path.with_name("ckpt.json.tmp").exists()


def test_crash_during_checkpoint_write_leaves_previous_checkpoint_intact(tmp_path, monkeypatch):
    path = tmp_path / "ckpt.json"
    workflow = Workflow("crash")
    workflow.add_step("a", lambda ctx: "a")
    workflow.add_step("b", lambda ctx: "b")
    workflow.add_step("c", lambda ctx: "c")
    saved = 0
    real_replace = os.replace

    def crash_on_second(src, dst):
        nonlocal saved
        saved += 1
        if saved == 2:
            raise KeyboardInterrupt  # power loss between writing temp and renaming
        real_replace(src, dst)

    monkeypatch.setattr("automation_toolkit.workflow.os.replace", crash_on_second)
    with pytest.raises(KeyboardInterrupt):
        workflow.run(checkpoint=path)
    monkeypatch.undo()
    assert [c["name"] for c in read_checkpoint(path)["payload"]["completed"]] == ["a"]
    assert path.with_name("ckpt.json.tmp").exists()  # orphan from the interrupted write
    result = workflow.run(checkpoint=path)
    assert result.restored == ["a"] and result.succeeded
    assert not path.exists() and not path.with_name("ckpt.json.tmp").exists()


def _edit_payload(path: Path, edit) -> None:
    document = read_checkpoint(path)
    edit(document["payload"])
    document["checksum"] = WorkflowCheckpoint._checksum(document["payload"])
    path.write_text(json.dumps(document))


CORRUPTIONS = {
    "invalid json": lambda path: path.write_text("{not json"),
    "empty file": lambda path: path.write_text(""),
    "list document": lambda path: path.write_text("[]"),
    "empty mapping": lambda path: path.write_text("{}"),
    "extra top-level key": lambda path: path.write_text(
        json.dumps({**read_checkpoint(path), "note": 1})
    ),
    "payload not a mapping": lambda path: path.write_text(
        json.dumps({"payload": [], "checksum": "x"})
    ),
    "checksum tampered": lambda path: path.write_text(
        json.dumps({**read_checkpoint(path), "checksum": "0" * 64})
    ),
    "output tampered without rechecksum": lambda path: path.write_text(
        path.read_text().replace('"step": "load"', '"step": "evil"')
    ),
    "wrong version": lambda path: _edit_payload(path, lambda p: p.update(version=99)),
    "missing version": lambda path: _edit_payload(path, lambda p: p.pop("version")),
    "other workflow": lambda path: _edit_payload(path, lambda p: p.update(workflow="other")),
    "steps renamed": lambda path: _edit_payload(path, lambda p: p["steps"].__setitem__(1, "x")),
    "step added": lambda path: _edit_payload(path, lambda p: p["steps"].append("extra")),
    "step removed": lambda path: _edit_payload(path, lambda p: p["steps"].pop()),
    "completed not a list": lambda path: _edit_payload(path, lambda p: p.update(completed={})),
    "completed entry not a mapping": lambda path: _edit_payload(
        path, lambda p: p["completed"].__setitem__(0, "load")
    ),
    "completed not a prefix": lambda path: _edit_payload(
        path, lambda p: p["completed"].__setitem__(0, {"name": "publish", "output": 1})
    ),
    "completed out of order": lambda path: _edit_payload(
        path, lambda p: p["completed"].insert(0, {"name": "transform", "output": 1})
    ),
    "completed longer than workflow": lambda path: _edit_payload(
        path, lambda p: p["completed"].extend({"name": n, "output": 1} for n in "tpq")
    ),
    "completed entry without output": lambda path: _edit_payload(
        path, lambda p: p["completed"][0].pop("output")
    ),
    "data not a mapping": lambda path: _edit_payload(path, lambda p: p.update(data=[])),
    "missing completed": lambda path: _edit_payload(path, lambda p: p.pop("completed")),
    "missing data": lambda path: _edit_payload(path, lambda p: p.pop("data")),
    "truncated file": lambda path: path.write_text(path.read_text()[:-15]),
    "binary garbage": lambda path: path.write_bytes(b"\x00\xff\xfe garbage"),
    "path is a directory": lambda path: (path.unlink(), path.mkdir()),
}


@pytest.mark.parametrize("corruption", sorted(CORRUPTIONS))
def test_corrupted_checkpoint_is_discarded_and_workflow_starts_fresh(tmp_path, caplog, corruption):
    path = tmp_path / "ckpt.json"
    calls: list[str] = []
    workflow = counting_workflow(calls, fail_at="transform")
    with pytest.raises(WorkflowError):
        workflow.run({"arm": True}, checkpoint=path)
    CORRUPTIONS[corruption](path)
    calls.clear()
    with caplog.at_level(logging.WARNING):
        if corruption == "path is a directory":
            # Unusable *and* unwritable: the run restarts and fails to save the first step.
            with pytest.raises(WorkflowError, match="Cannot write workflow checkpoint") as info:
                workflow.run({"arm": False}, checkpoint=path)
            assert calls == ["load"] and info.value.step == "load"
            assert info.value.result.skipped == ["transform", "publish"]
            return
        result = workflow.run({"arm": False}, checkpoint=path)
    assert "Ignoring unusable workflow checkpoint" in caplog.text
    assert "for 'ckpt'" in caplog.text
    assert calls == ["load", "transform", "publish"]  # everything re-ran
    assert result.succeeded and not result.resumed and result.restored == []
    assert not path.exists()


def test_corruption_messages_are_specific(tmp_path, caplog):
    path = tmp_path / "ckpt.json"
    workflow = counting_workflow([], fail_at="transform")
    with pytest.raises(WorkflowError):
        workflow.run(checkpoint=path)
    original = path.read_text()
    expectations = {
        "checksum tampered": "checksum does not match",
        "wrong version": "unsupported checkpoint version 99",
        "other workflow": "written for workflow 'other'",
        "step added": "steps have changed",
        "completed not a prefix": "not a prefix",
        "completed entry without output": "has no output",
        "invalid json": "Expecting",
        "empty mapping": "not a workflow checkpoint document",
    }
    for name, fragment in expectations.items():
        path.write_text(original)
        CORRUPTIONS[name](path)
        caplog.clear()
        with caplog.at_level(logging.WARNING):
            assert WorkflowCheckpoint(path).load(workflow) is None
        assert fragment in caplog.text, name
        assert not path.exists()


def test_checkpoint_for_different_step_order_is_rejected(tmp_path, caplog):
    path = tmp_path / "ckpt.json"
    first = Workflow("w").add_step("a", lambda ctx: 1).add_step("b", failing_times(5))
    with pytest.raises(WorkflowError):
        first.run(checkpoint=path)
    reordered = Workflow("w").add_step("b", lambda ctx: 1).add_step("a", lambda ctx: 2)
    with caplog.at_level(logging.WARNING):
        result = reordered.run(checkpoint=path)
    assert "steps have changed" in caplog.text and not result.resumed


def test_non_serializable_output_fails_explicitly_and_keeps_earlier_checkpoint(tmp_path):
    path = tmp_path / "ckpt.json"
    workflow = Workflow("json")
    workflow.add_step("a", lambda ctx: {"ok": [1, 2.5, None, "x"]})
    workflow.add_step("b", lambda ctx: object())
    workflow.add_step("c", lambda ctx: 3)
    with pytest.raises(WorkflowError, match="must be JSON serializable") as info:
        workflow.run(checkpoint=path)
    assert "after step 'b'" in str(info.value) and info.value.step == "b"
    assert info.value.result.step("b").succeeded  # the step itself did not fail
    assert info.value.result.skipped == ["c"]
    assert isinstance(info.value.__cause__, TypeError)
    assert [c["name"] for c in read_checkpoint(path)["payload"]["completed"]] == ["a"]


def test_non_serializable_shared_data_fails_explicitly(tmp_path):
    path = tmp_path / "ckpt.json"
    workflow = Workflow("data").add_step("a", lambda ctx: ctx.__setitem__("obj", set()))
    with pytest.raises(WorkflowError, match="shared data must be JSON serializable"):
        workflow.run(checkpoint=path)
    assert not path.exists()


def test_unwritable_checkpoint_location_is_a_workflow_error(tmp_path):
    blocker = tmp_path / "blocker"
    blocker.write_text("not a directory")
    workflow = Workflow("w").add_step("a", lambda ctx: 1)
    with pytest.raises(WorkflowError, match="Cannot write workflow checkpoint") as info:
        workflow.run(checkpoint=blocker / "ckpt.json")
    assert isinstance(info.value.__cause__, OSError) and info.value.step == "a"


def test_clear_tolerates_missing_files_and_warns_on_os_errors(tmp_path, caplog, monkeypatch):
    checkpoint = WorkflowCheckpoint(tmp_path / "missing.json")
    checkpoint.clear()  # nothing to do, no error
    assert checkpoint.load(Workflow("w").add_step("a", print)) is None
    target = tmp_path / "ckpt.json"
    target.write_text("{}")

    def deny(self):
        raise PermissionError("read-only")

    monkeypatch.setattr(Path, "unlink", deny)
    with caplog.at_level(logging.WARNING):
        WorkflowCheckpoint(target).clear()
    assert "Cannot remove workflow checkpoint" in caplog.text and "read-only" in caplog.text


def test_save_records_only_the_succeeded_prefix(tmp_path):
    workflow = Workflow("prefix")
    workflow.add_step("a", lambda ctx: 1)
    workflow.add_step("b", failing_times(9), continue_on_error=True)
    workflow.add_step("c", lambda ctx: 3)
    result = workflow.run()
    checkpoint = WorkflowCheckpoint(tmp_path / "c.json")
    checkpoint.save(workflow, result.steps, result.context)
    payload = read_checkpoint(checkpoint.path)["payload"]
    assert [c["name"] for c in payload["completed"]] == ["a"]  # c is not a prefix member
    assert payload["version"] == 1 and payload["saved_at"].endswith("+00:00")
    assert payload["completed"][0]["finished_at"].endswith("+00:00")
    restored = checkpoint.load(workflow)
    assert restored is not None and restored.data == {}


def test_checkpoint_with_timeouts_and_retries_resumes_after_timeout(tmp_path):
    path = tmp_path / "ckpt.json"
    release = threading.Event()
    calls: list[str] = []
    workflow = Workflow("timed-ckpt", sleep=FakeClock())
    workflow.add_step("quick", lambda ctx: calls.append("quick") or "q", timeout=5)
    workflow.add_step("slow", lambda ctx: release.wait(30), timeout=0.05, retries=1)
    with pytest.raises(WorkflowError, match="timed out"):
        workflow.run(checkpoint=path)
    release.set()
    rerun = Workflow("timed-ckpt")
    rerun.add_step("quick", lambda ctx: calls.append("quick") or "q", timeout=5)
    rerun.add_step("slow", lambda ctx: "fast now", timeout=5)
    result = rerun.run(checkpoint=path)
    assert calls == ["quick"] and result.restored == ["quick"]
    assert result.outputs["slow"] == "fast now"


# --- Timed-out attempts cannot alter the live context -----------------------------------


def test_abandoned_attempt_writes_do_not_reach_the_live_context():
    release, wrote = threading.Event(), threading.Event()

    def late_writer(ctx):
        release.wait(30)
        ctx["late"] = "written after timeout"
        ctx.outputs["forged"] = "injected"
        del ctx.data["keep"]
        wrote.set()
        return "late value"

    workflow = Workflow("isolation")
    workflow.add_step("slow", late_writer, timeout=0.05, continue_on_error=True)
    workflow.add_step("after", lambda ctx: dict(ctx.data))
    result = workflow.run({"keep": 1})
    release.set()
    assert wrote.wait(5)
    assert result.context.data == {"keep": 1}
    assert dict(result.outputs) == {"after": {"keep": 1}}
    assert "forged" not in result.outputs and "late" not in result.context
    assert result.step("slow").output is None and result.step("slow").timed_out


def test_abandoned_attempt_cannot_alter_what_a_checkpoint_records(tmp_path):
    path = tmp_path / "ckpt.json"
    release, wrote = threading.Event(), threading.Event()

    def late_writer(ctx):
        release.wait(30)
        ctx["poison"] = True
        wrote.set()

    workflow = Workflow("ckpt-isolation")
    workflow.add_step("slow", late_writer, timeout=0.05, continue_on_error=True)
    workflow.add_step("ok", lambda ctx: "ok")
    workflow.add_step("fail", failing_times(9))
    with pytest.raises(WorkflowError):
        workflow.run(checkpoint=path)
    release.set()
    assert wrote.wait(5)
    assert read_checkpoint(path)["payload"]["data"] == {}
    assert [c["name"] for c in read_checkpoint(path)["payload"]["completed"]] == []


def test_in_time_attempt_changes_are_merged_into_the_live_context():
    def writer(ctx):
        ctx["added"] = ctx["seed"] + 1
        del ctx.data["seed"]
        return "ok"

    result = Workflow("merge").add_step("w", writer, timeout=5).run({"seed": 41})
    assert result.context.data == {"added": 42}
    assert result.outputs["w"] == "ok"


def test_timed_attempt_sees_earlier_outputs_but_cannot_rewrite_them():
    def tamper(ctx):
        assert ctx.outputs["first"] == 1
        ctx.outputs["first"] = "tampered"
        return 2

    result = (
        Workflow("ro-outputs")
        .add_step("first", lambda ctx: 1)
        .add_step("second", tamper, timeout=5)
        .run()
    )
    assert dict(result.outputs) == {"first": 1, "second": 2}


def test_failed_timed_attempt_discards_its_partial_writes_before_retry():
    attempts = {"n": 0}

    def half_done(ctx):
        attempts["n"] += 1
        ctx["partial"] = attempts["n"]
        if attempts["n"] == 1:
            raise OSError("midway")
        return ctx.get("partial")

    result = Workflow("partial").add_step("s", half_done, retries=1, timeout=5).run()
    assert result.context.data == {"partial": 2} and result.outputs["s"] == 2


def test_timed_out_step_followed_by_a_step_that_relies_on_untouched_data():
    release = threading.Event()

    def slow_mutator(ctx):
        ctx["counter"] = ctx["counter"] + 100
        release.wait(30)

    workflow = Workflow("sequence")
    workflow.add_step("slow", slow_mutator, timeout=0.05, retries=2, continue_on_error=True)
    workflow.add_step("sum", lambda ctx: ctx["counter"])
    result = workflow.run({"counter": 1})
    release.set()
    assert result.outputs["sum"] == 1 and result.step("slow").attempts == 3


# --- JSON reporting ----------------------------------------------------------------------


def test_json_report_of_a_failed_resumed_run_covers_statuses_retries_errors_and_restored(
    tmp_path,
):
    path = tmp_path / "ckpt.json"
    workflow = Workflow("json", sleep=FakeClock())
    workflow.add_step("load", lambda ctx: {"rows": 3})
    workflow.add_step("fail", failing_times(9, error=OSError), retries=1, retry_delay=0.5)
    with pytest.raises(WorkflowError):
        workflow.run(checkpoint=path)

    rerun = Workflow("json", sleep=FakeClock())
    rerun.add_step("load", lambda ctx: {"rows": 3})
    rerun.add_step("fail", failing_times(1, error=OSError), retries=2, retry_delay=0.5)
    rerun.add_step("optional", failing_times(9, error=ValueError), continue_on_error=True)
    rerun.add_step("break", failing_times(9, error=RuntimeError))
    rerun.add_step("never", lambda ctx: None)
    # The checkpoint was written for ["load", "fail"]; the step list changed, so it is
    # discarded and the run starts fresh. Build the matching workflow instead.
    workflow = Workflow("json", sleep=FakeClock())
    workflow.add_step("load", lambda ctx: {"rows": 3})
    workflow.add_step("fail", failing_times(1, error=OSError), retries=2, retry_delay=0.5)
    with pytest.raises(WorkflowError) as info:
        rerun.run(checkpoint=path)
    report = json.loads(info.value.result.to_json())

    assert report["report_version"] == 1 and report["workflow"] == "json"
    assert report["succeeded"] is False and report["resumed"] is False
    assert report["generated_at"].endswith("+00:00")
    assert report["summary"] == "Workflow 'json': 5 steps, 2 succeeded, 2 failed, 1 skipped"
    assert report["counts"] == {"pending": 0, "succeeded": 2, "failed": 2, "skipped": 1}
    assert report["failed"] == ["optional", "break"] and report["skipped"] == ["never"]
    assert report["restored"] == []
    assert [s["name"] for s in report["steps"]] == ["load", "fail", "optional", "break", "never"]

    load, fail, optional, broke, never = report["steps"]
    assert load["status"] == "succeeded" and load["output"] == {"rows": 3}
    assert load["attempts"] == 1 and load["retries"] == 0 and load["errors"] == []
    assert load["error"] is None and load["traceback"] is None and load["timed_out"] is False
    assert load["output_serializable"] is True
    assert load["started_at"].endswith("+00:00") and load["finished_at"] >= load["started_at"]

    assert fail["status"] == "succeeded" and fail["attempts"] == 2 and fail["retries"] == 1
    assert fail["errors"] == [
        {"attempt": 1, "type": "OSError", "message": "try 1", "delay_before_retry": 0.5}
    ]
    assert fail["error"] is None and fail["output"] == 2

    assert optional["status"] == "failed" and optional["retries"] == 0
    assert optional["error"] == {"type": "ValueError", "message": "try 1"}
    assert optional["errors"][0]["delay_before_retry"] is None
    assert optional["traceback"].startswith("Traceback") and optional["output"] is None

    assert broke["status"] == "failed" and broke["error"]["type"] == "RuntimeError"
    assert never == {
        "name": "never",
        "status": "skipped",
        "restored": False,
        "attempts": 0,
        "retries": 0,
        "duration_seconds": 0.0,
        "started_at": None,
        "finished_at": None,
        "timed_out": False,
        "error": None,
        "errors": [],
        "traceback": None,
        "output": None,
        "output_serializable": True,
    }
    assert report["duration_seconds"] == pytest.approx(
        sum(s["duration_seconds"] for s in report["steps"])
    )


def test_json_report_marks_restored_steps(tmp_path):
    path = tmp_path / "ckpt.json"
    workflow = (
        Workflow("restored").add_step("a", lambda ctx: [1, 2]).add_step("b", failing_times(1))
    )
    with pytest.raises(WorkflowError):
        workflow.run(checkpoint=path)
    report = workflow.run(checkpoint=path).to_dict()
    assert report["succeeded"] and report["resumed"] and report["restored"] == ["a"]
    a, b = report["steps"]
    assert a["restored"] is True and a["output"] == [1, 2] and a["started_at"] is None
    assert a["attempts"] == 1 and a["retries"] == 0
    assert b["restored"] is False and b["output"] == 2


def test_json_report_records_timeouts_with_backoff(tmp_path):
    release = threading.Event()
    workflow = Workflow("timeouts", sleep=FakeClock())
    workflow.add_step("slow", lambda ctx: release.wait(30), timeout=0.05, retries=1, retry_delay=1)
    with pytest.raises(WorkflowError) as info:
        workflow.run()
    release.set()
    step = info.value.result.to_dict()["steps"][0]
    assert step["timed_out"] is True and step["attempts"] == 2 and step["retries"] == 1
    assert [e["type"] for e in step["errors"]] == ["StepTimeoutError", "StepTimeoutError"]
    assert (
        step["errors"][0]["delay_before_retry"] == 1
        and step["errors"][1]["delay_before_retry"] is None
    )
    assert "timed out after 0.05s" in step["error"]["message"]


def test_json_report_replaces_non_serializable_outputs():
    marker = object()
    result = (
        Workflow("objects").add_step("obj", lambda ctx: marker).add_step("n", lambda ctx: 1).run()
    )
    report = json.loads(result.to_json())  # must not raise
    obj, n = report["steps"]
    assert obj["output"] == repr(marker) and obj["output_serializable"] is False
    assert n["output"] == 1 and n["output_serializable"] is True
    compact = result.to_json(indent=None)
    assert "\n" not in compact and json.loads(compact)["workflow"] == "objects"


def test_write_json_creates_parents_and_reports_write_errors(tmp_path):
    result = Workflow("write").add_step("a", lambda ctx: "x").run()
    target = result.write_json(tmp_path / "out" / "nested" / "report.json")
    assert target == tmp_path / "out" / "nested" / "report.json"
    assert json.loads(target.read_text(encoding="utf-8"))["steps"][0]["output"] == "x"
    blocker = tmp_path / "blocker"
    blocker.write_text("file")
    with pytest.raises(WorkflowError, match="Cannot write workflow report") as info:
        result.write_json(blocker / "report.json")
    assert isinstance(info.value.__cause__, OSError)
    assert result.write_json(str(tmp_path / "s.json")).exists()


def test_step_result_to_dict_matches_object_state():
    step = Workflow("one").add_step("a", failing_times(2), retries=2).run().step("a")
    data = step.to_dict()
    assert data["attempts"] == 3 and data["retries"] == 2 and data["output"] == 3
    assert [e["attempt"] for e in data["errors"]] == [1, 2]
    assert all(e["delay_before_retry"] == 0.0 for e in data["errors"])
    assert data["duration_seconds"] == step.duration_seconds


def test_run_rejects_non_mapping_initial_data_and_bad_checkpoint_types(tmp_path):
    workflow = Workflow("args").add_step("a", lambda ctx: 1)
    with pytest.raises(WorkflowError, match="initial_data must be a mapping, got list"):
        workflow.run([("a", 1)])
    with pytest.raises(WorkflowError, match="initial_data must be a mapping, got str"):
        workflow.run("a=1")
    with pytest.raises(WorkflowError, match="checkpoint must be a path or WorkflowCheckpoint"):
        workflow.run(checkpoint=5)
    assert workflow.run(checkpoint=tmp_path / "ok.json").succeeded  # PathLike accepted
    assert list(tmp_path.iterdir()) == []


def test_checkpoint_with_invalid_attempt_count_is_discarded(tmp_path, caplog):
    path = tmp_path / "ckpt.json"
    workflow = counting_workflow([], fail_at="transform")
    with pytest.raises(WorkflowError):
        workflow.run(checkpoint=path)
    for bad in ("3", -1, True, 2.5):
        _edit_payload(path, lambda p, bad=bad: p["completed"][0].update(attempts=bad))
        caplog.clear()
        with caplog.at_level(logging.WARNING):
            assert WorkflowCheckpoint(path).load(workflow) is None
        assert "invalid attempt count" in caplog.text
        with pytest.raises(WorkflowError):
            workflow.run(checkpoint=path)
    _edit_payload(path, lambda p: p["completed"][0].pop("attempts"))  # older file: default 1
    restored = WorkflowCheckpoint(path).load(workflow)
    assert restored is not None
    result = Workflow("ckpt")
    for name in ("load", "transform", "publish"):
        result.add_step(name, lambda ctx, n=name: n)
    assert result.run(checkpoint=path).step("load").attempts == 1


def test_deeply_nested_checkpoint_is_discarded_not_crashing(tmp_path, caplog):
    path = tmp_path / "ckpt.json"
    path.write_text("[" * 100_000 + "]" * 100_000)
    with caplog.at_level(logging.WARNING):
        assert WorkflowCheckpoint(path).load(Workflow("w").add_step("a", print)) is None
    assert "Ignoring unusable workflow checkpoint" in caplog.text and not path.exists()


def test_json_report_rejects_nan_and_infinity_outputs():
    result = Workflow("nan").add_step("n", lambda ctx: float("nan")).run()
    report = result.to_dict()["steps"][0]
    assert report["output"] == "nan" and report["output_serializable"] is False
    nested = Workflow("inf").add_step("n", lambda ctx: {"v": float("inf")}).run().to_json()
    assert (
        "Infinity" not in nested and json.loads(nested)["steps"][0]["output_serializable"] is False
    )


def test_write_json_is_atomic_and_leaves_no_temporary_file(tmp_path, monkeypatch):
    result = Workflow("w").add_step("a", lambda ctx: 1).run()
    target = tmp_path / "r.json"
    replaced: list[Path] = []
    real = os.replace
    monkeypatch.setattr(
        "automation_toolkit.workflow.os.replace",
        lambda s, d: (replaced.append(Path(s)), real(s, d)),
    )
    result.write_json(target)
    assert replaced == [tmp_path / "r.json.tmp"] and not (tmp_path / "r.json.tmp").exists()
    assert json.loads(target.read_text())["workflow"] == "w"
