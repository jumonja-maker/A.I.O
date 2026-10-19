"""Opt-in Guard v0.4 library; importing it never connects or starts the UI."""
from .adapter import LibreOfficeAdapter
from .core import ConverterGuard, GuardTrace
from .errors import GuardError
from .policy import GuardLimits

__all__ = ['ConverterGuard', 'GuardLimits', 'GuardTrace', 'GuardError', 'LibreOfficeAdapter']
