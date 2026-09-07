"""Actionable failures; unknown coverage is never interpreted as clear space."""

class VisibilityError(ValueError):
    """Base error for an unsupported query or product."""

class ConfigurationError(VisibilityError):
    pass

class IncompleteCoverageError(VisibilityError):
    pass

class ResourceBudgetError(VisibilityError):
    pass

class UnsupportedTargetError(VisibilityError):
    pass
