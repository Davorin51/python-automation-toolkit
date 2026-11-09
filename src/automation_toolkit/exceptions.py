"""Errors safe to display in the CLI and desktop application."""


class ToolkitError(Exception):
    """Base class for actionable application errors."""


class ConfigurationError(ToolkitError):
    """Configuration is malformed or unsupported."""


class IngestionError(ToolkitError):
    """An input file cannot be read or does not match the schema."""


class ValidationError(ToolkitError):
    """There are no usable transactions after validation."""


class ReportError(ToolkitError):
    """Reports could not be written; no complete run was published."""


class WorkflowError(ToolkitError):
    """A workflow is malformed or a required step failed; later steps were not run."""

    #: Partial ``WorkflowResult`` when a step failed at run time; ``None`` for definition errors.
    result = None
    #: Name of the step whose failure stopped the run; ``None`` for definition errors.
    step = None


class StepTimeoutError(WorkflowError, TimeoutError):
    """A workflow step attempt exceeded its configured timeout and was abandoned."""
