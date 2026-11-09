"""Sequential workflow engine: ordering, shared context, failure handling and retries."""

import logging
import threading
import time

import pytest

from automation_toolkit.config import Config
from automation_toolkit.exceptions import StepTimeoutError, ToolkitError, WorkflowError
from automation_toolkit.ingestion import load_files
from automation_toolkit.pipeline import run_pipeline
from automation_toolkit.processing.analytics import analyze
from automation_toolkit.processing.validation import validate
from automation_toolkit.workflow import Step, StepStatus, Workflow, WorkflowContext


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
