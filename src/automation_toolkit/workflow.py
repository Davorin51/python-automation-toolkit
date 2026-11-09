"""Minimal workflow engine: named steps run in order with explicit failure handling.

A :class:`Workflow` is an ordered list of :class:`Step` objects. Each step is a callable
that receives the shared :class:`WorkflowContext` and may return a value, which is stored
under the step name for later steps. Steps run strictly one after another.

Error handling is deliberately simple and explicit:

* a step that raises is retried ``retries`` more times; the wait before each retry starts
  at ``retry_delay`` seconds and is multiplied by ``backoff`` after every failure
  (exponential by default), capped by ``max_delay``;
* an attempt that runs longer than ``timeout`` seconds is abandoned and counts as a
  failure (:class:`~automation_toolkit.exceptions.StepTimeoutError`), so it is retried
  like any other error;
* when the attempts are exhausted and the step is required, the remaining steps are
  marked as skipped and :class:`~automation_toolkit.exceptions.WorkflowError` is raised
  with the failing step name as ``error.step`` and the partial :class:`WorkflowResult`
  attached as ``error.result``;
* a step declared with ``continue_on_error=True`` records its failure and lets the
  workflow proceed; the final result then reports ``succeeded == False``.

Every :class:`StepResult` keeps the error of each attempt, the formatted traceback of
the last one, the delays that were waited and timing information;
:meth:`WorkflowResult.report` renders all of it as text for logs and bug reports.

``KeyboardInterrupt`` and ``SystemExit`` are never swallowed, so an interrupted workflow
behaves like any other interrupted toolkit command.
"""

from __future__ import annotations

import logging
import math
import threading
import time
import traceback
from collections.abc import Callable, Iterator, Mapping
from dataclasses import KW_ONLY, dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from automation_toolkit.exceptions import StepTimeoutError, WorkflowError

logger = logging.getLogger(__name__)

StepAction = Callable[["WorkflowContext"], Any]
SleepFunction = Callable[[float], Any]


class StepStatus(StrEnum):
    PENDING = "pending"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    SKIPPED = "skipped"


@dataclass
class WorkflowContext:
    """Mutable state shared by the steps of one run.

    ``data`` holds free-form values (seeded from ``Workflow.run(initial_data)``) and
    ``outputs`` maps the name of every succeeded step to its return value.
    """

    workflow: str
    data: dict[str, Any] = field(default_factory=dict)
    outputs: dict[str, Any] = field(default_factory=dict)

    def __getitem__(self, key: str) -> Any:
        return self.data[key]

    def __setitem__(self, key: str, value: Any) -> None:
        self.data[key] = value

    def __contains__(self, key: object) -> bool:
        return key in self.data

    def get(self, key: str, default: Any = None) -> Any:
        return self.data.get(key, default)


def _check_number(step: str, option: str, value: Any, *, minimum: float, exclusive: bool) -> None:
    """Reject booleans, non-numbers, NaN/inf and values below ``minimum``."""
    valid = (
        isinstance(value, int | float)
        and not isinstance(value, bool)
        and math.isfinite(value)
        and (value > minimum if exclusive else value >= minimum)
    )
    if not valid:
        bound = f"greater than {minimum:g}" if exclusive else f"at least {minimum:g}"
        raise WorkflowError(f"Step '{step}' {option} must be a finite number {bound}.")


@dataclass(frozen=True)
class Step:
    """One unit of work.

    ``retries`` is the number of *additional* attempts after a failure. Before retry
    number ``k`` (1-based) the engine waits ``retry_delay * backoff ** (k - 1)`` seconds,
    never more than ``max_delay``; the defaults (``retry_delay=0``) retry immediately, and
    ``backoff=1`` gives a constant delay. ``timeout`` bounds a single attempt in seconds.

    A timed-out attempt runs in a helper thread that cannot be killed: the engine stops
    waiting, discards whatever the attempt produces later and moves on, but the callable
    itself keeps running until it returns. Use timeouts for actions that are safe to
    abandon or that observe their own deadline.
    """

    name: str
    action: StepAction
    continue_on_error: bool = False
    retries: int = 0
    _: KW_ONLY
    retry_delay: float = 0.0
    backoff: float = 2.0
    max_delay: float | None = None
    timeout: float | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.name, str) or not self.name.strip():
            raise WorkflowError("Step name must be a non-empty string.")
        if not callable(self.action):
            raise WorkflowError(f"Step '{self.name}' action must be callable.")
        if isinstance(self.retries, bool) or not isinstance(self.retries, int) or self.retries < 0:
            raise WorkflowError(f"Step '{self.name}' retries must be a non-negative integer.")
        _check_number(self.name, "retry_delay", self.retry_delay, minimum=0, exclusive=False)
        _check_number(self.name, "backoff", self.backoff, minimum=1, exclusive=False)
        if self.max_delay is not None:
            _check_number(self.name, "max_delay", self.max_delay, minimum=0, exclusive=False)
        if self.timeout is not None:
            _check_number(self.name, "timeout", self.timeout, minimum=0, exclusive=True)

    def delay_before_retry(self, failed_attempts: int) -> float:
        """Seconds to wait after ``failed_attempts`` consecutive failures (1-based)."""
        if failed_attempts < 1:
            raise ValueError("failed_attempts must be at least 1")
        delay = self.retry_delay * self.backoff ** (failed_attempts - 1)
        if self.max_delay is not None:
            delay = min(delay, self.max_delay)
        return float(delay)

    def retry_schedule(self) -> list[float]:
        """The delays the engine would wait before each retry, in order."""
        return [self.delay_before_retry(k) for k in range(1, self.retries + 1)]


@dataclass
class StepResult:
    name: str
    status: StepStatus = StepStatus.PENDING
    output: Any = None
    #: Error of the last failed attempt; ``None`` once an attempt succeeds.
    error: BaseException | None = None
    attempts: int = 0
    #: Wall-clock time of all attempts including retry delays.
    duration_seconds: float = 0.0
    #: One entry per failed attempt, in order, including attempts that were later retried.
    errors: list[BaseException] = field(default_factory=list)
    #: Formatted traceback of the last failed attempt, for logs and bug reports.
    traceback: str | None = None
    #: Delays (seconds) actually waited before each retry.
    delays: list[float] = field(default_factory=list)
    started_at: datetime | None = None
    finished_at: datetime | None = None

    @property
    def succeeded(self) -> bool:
        return self.status is StepStatus.SUCCEEDED

    @property
    def timed_out(self) -> bool:
        """``True`` when the last attempt was abandoned because of the step timeout."""
        return isinstance(self.error, StepTimeoutError)

    @property
    def error_message(self) -> str | None:
        return _describe_error(self.error) if self.error is not None else None

    def describe(self) -> str:
        """One line: name, status, attempts, duration and the last error, if any."""
        plural = "s" if self.attempts != 1 else ""
        text = f"{self.name}: {self.status.value}"
        if self.status is not StepStatus.SKIPPED and self.attempts:
            text += f" after {self.attempts} attempt{plural} in {self.duration_seconds:.2f}s"
        if self.error is not None:
            text += f" ({self.error_message})"
        return text


def _describe_error(error: BaseException) -> str:
    text = str(error)
    return f"{type(error).__name__}: {text}" if text else type(error).__name__


@dataclass
class WorkflowResult:
    """Outcome of one :meth:`Workflow.run`. ``steps`` preserves the declared order."""

    name: str
    steps: list[StepResult]
    context: WorkflowContext

    @property
    def succeeded(self) -> bool:
        return all(step.status is StepStatus.SUCCEEDED for step in self.steps)

    @property
    def outputs(self) -> Mapping[str, Any]:
        return self.context.outputs

    @property
    def failed(self) -> list[str]:
        return [s.name for s in self.steps if s.status is StepStatus.FAILED]

    @property
    def skipped(self) -> list[str]:
        return [s.name for s in self.steps if s.status is StepStatus.SKIPPED]

    @property
    def duration_seconds(self) -> float:
        return sum(s.duration_seconds for s in self.steps)

    def step(self, name: str) -> StepResult:
        for result in self.steps:
            if result.name == name:
                return result
        raise KeyError(name)

    def summary(self) -> str:
        counts = {status: 0 for status in StepStatus}
        for result in self.steps:
            counts[result.status] += 1
        return (
            f"Workflow '{self.name}': {len(self.steps)} steps, "
            f"{counts[StepStatus.SUCCEEDED]} succeeded, {counts[StepStatus.FAILED]} failed, "
            f"{counts[StepStatus.SKIPPED]} skipped"
        )

    def report(self, *, include_traceback: bool = False) -> str:
        """Multi-line text: the summary, one line per step and the errors of every attempt."""
        lines = [f"{self.summary()} in {self.duration_seconds:.2f}s"]
        for index, result in enumerate(self.steps, start=1):
            lines.append(f"  {index}. {result.describe()}")
            if result.status is StepStatus.FAILED or result.errors:
                for attempt, error in enumerate(result.errors, start=1):
                    waited = ""
                    if attempt <= len(result.delays):
                        delay = result.delays[attempt - 1]
                        waited = (
                            f" (waited {delay:.2f}s before retrying)"
                            if delay > 0
                            else " (retried immediately)"
                        )
                    lines.append(f"       attempt {attempt}: {_describe_error(error)}{waited}")
            if include_traceback and result.traceback:
                lines.extend("       " + line for line in result.traceback.rstrip().splitlines())
        return "\n".join(lines)


class Workflow:
    """An ordered, sequentially executed list of steps.

    Steps are validated when added: names must be unique and non-empty. The workflow can
    be run any number of times; every run gets a fresh context and fresh results.
    ``sleep`` performs the retry delays and exists so tests (or schedulers) can replace
    :func:`time.sleep`.
    """

    def __init__(
        self,
        name: str,
        steps: Iterator[Step] | list[Step] | None = None,
        *,
        sleep: SleepFunction = time.sleep,
    ) -> None:
        if not isinstance(name, str) or not name.strip():
            raise WorkflowError("Workflow name must be a non-empty string.")
        if not callable(sleep):
            raise WorkflowError(f"Workflow '{name}' sleep must be callable.")
        self.name = name
        self._sleep = sleep
        self._steps: list[Step] = []
        for step in steps or ():
            self.add(step)

    @property
    def steps(self) -> tuple[Step, ...]:
        return tuple(self._steps)

    def __len__(self) -> int:
        return len(self._steps)

    def add(self, step: Step) -> Workflow:
        if not isinstance(step, Step):
            raise WorkflowError(f"Expected a Step, got {type(step).__name__}.")
        if any(existing.name == step.name for existing in self._steps):
            raise WorkflowError(f"Workflow '{self.name}' already has a step named '{step.name}'.")
        self._steps.append(step)
        return self

    def add_step(
        self,
        name: str,
        action: StepAction,
        *,
        continue_on_error: bool = False,
        retries: int = 0,
        retry_delay: float = 0.0,
        backoff: float = 2.0,
        max_delay: float | None = None,
        timeout: float | None = None,
    ) -> Workflow:
        """Convenience wrapper around :meth:`add`; returns ``self`` for chaining."""
        return self.add(
            Step(
                name,
                action,
                continue_on_error=continue_on_error,
                retries=retries,
                retry_delay=retry_delay,
                backoff=backoff,
                max_delay=max_delay,
                timeout=timeout,
            )
        )

    def run(self, initial_data: Mapping[str, Any] | None = None) -> WorkflowResult:
        if not self._steps:
            raise WorkflowError(f"Workflow '{self.name}' has no steps.")
        context = WorkflowContext(self.name, data=dict(initial_data or {}))
        results = [StepResult(step.name) for step in self._steps]
        outcome = WorkflowResult(self.name, results, context)
        total = len(self._steps)
        logger.info("Starting workflow '%s' with %d steps", self.name, total)
        for index, (step, result) in enumerate(zip(self._steps, results, strict=True), start=1):
            self._execute(step, result, context)
            if result.status is StepStatus.FAILED and not step.continue_on_error:
                for remaining in results[index:]:
                    remaining.status = StepStatus.SKIPPED
                logger.error("%s; stopped at step '%s'", outcome.summary(), step.name)
                logger.debug("Failure details:\n%s", outcome.report(include_traceback=True))
                error = result.error
                assert error is not None
                plural = "s" if result.attempts != 1 else ""
                message = (
                    f"Workflow '{self.name}' failed at step '{step.name}' after "
                    f"{result.attempts} attempt{plural}: {_describe_error(error)} "
                    f"[step {index} of {total}, {result.duration_seconds:.2f}s"
                )
                if len(result.errors) > 1:
                    earlier = "; ".join(_describe_error(e) for e in result.errors[:-1])
                    message += f"; earlier attempts: {earlier}"
                if skipped := [r.name for r in results[index:]]:
                    message += f"; skipped: {', '.join(skipped)}"
                message += "]"
                failure = WorkflowError(message)
                failure.result = outcome
                failure.step = step.name
                raise failure from error
        logger.info("%s", outcome.summary())
        return outcome

    def _execute(self, step: Step, result: StepResult, context: WorkflowContext) -> None:
        started = time.perf_counter()
        result.started_at = datetime.now(UTC)
        try:
            for attempt in range(1, step.retries + 2):
                result.attempts = attempt
                try:
                    output = self._call(step, context)
                except Exception as exc:
                    result.error = exc
                    result.errors.append(exc)
                    result.traceback = "".join(traceback.format_exception(exc))
                    if attempt <= step.retries:
                        delay = step.delay_before_retry(attempt)
                        logger.warning(
                            "Step '%s' failed on attempt %d/%d (%s); retrying %s",
                            step.name,
                            attempt,
                            step.retries + 1,
                            _describe_error(exc),
                            f"in {delay:.2f}s" if delay > 0 else "immediately",
                        )
                        result.delays.append(delay)
                        if delay > 0:
                            self._sleep(delay)
                        continue
                    result.status = StepStatus.FAILED
                    level = logging.WARNING if step.continue_on_error else logging.ERROR
                    logger.log(
                        level,
                        "Step '%s' failed after %d attempt%s (%s)%s",
                        step.name,
                        attempt,
                        "s" if attempt != 1 else "",
                        _describe_error(exc),
                        "; continuing" if step.continue_on_error else "",
                    )
                    break
                else:
                    result.status = StepStatus.SUCCEEDED
                    result.output = output
                    result.error = None
                    result.traceback = None
                    context.outputs[step.name] = output
                    logger.info("Step '%s' succeeded", step.name)
                    break
        finally:
            result.duration_seconds = time.perf_counter() - started
            result.finished_at = datetime.now(UTC)

    @staticmethod
    def _call(step: Step, context: WorkflowContext) -> Any:
        """Run one attempt, in a helper thread when the step has a timeout."""
        if step.timeout is None:
            return step.action(context)
        outcome: list[tuple[bool, Any]] = []

        def target() -> None:
            try:
                outcome.append((True, step.action(context)))
            except BaseException as exc:  # noqa: BLE001 - re-raised in the calling thread
                outcome.append((False, exc))

        thread = threading.Thread(target=target, name=f"workflow-step-{step.name}", daemon=True)
        thread.start()
        thread.join(step.timeout)
        if thread.is_alive():
            raise StepTimeoutError(
                f"Step '{step.name}' timed out after {step.timeout:g}s; the attempt was abandoned."
            )
        ok, value = outcome[0]
        if ok:
            return value
        raise value
