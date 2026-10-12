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
