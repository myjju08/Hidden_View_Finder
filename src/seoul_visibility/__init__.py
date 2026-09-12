"""Seoul point visibility, using explicit prepared 2.5D data.

Public engine exports load lazily so read-only acquisition planning and storage
checks work before the optional native geospatial environment is provisioned.
"""
from importlib import import_module
from .errors import ConfigurationError, IncompleteCoverageError, ResourceBudgetError, UnsupportedTargetError

__all__ = ['VisibilityEngine', 'TargetPoint', 'VisibilityResult', 'CandidateMask',
           'SparseVisibilityResult', 'State', 'ConfigurationError',
           'IncompleteCoverageError', 'ResourceBudgetError', 'UnsupportedTargetError']


def __getattr__(name):
    if name == 'VisibilityEngine':
        value = getattr(import_module('.engine', __name__), name)
    elif name in {'TargetPoint', 'VisibilityResult', 'CandidateMask', 'SparseVisibilityResult', 'State'}:
        value = getattr(import_module('.types', __name__), name)
    else:
        raise AttributeError(name)
    globals()[name] = value
    return value
