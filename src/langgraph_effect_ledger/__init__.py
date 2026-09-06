"""Server-owned effect recovery, with optional MCP and legacy LangChain adapters."""

from importlib import import_module

from .operations import EffectExecutor, Operation, OperationConflict
from .store import Claim, OperationStore
from .sqlite import SQLiteOperationStore
from .recovery import RecoveryDecision, RecoveryPolicy

_LEGACY = {
    "VERDICTS": "ledger", "EffectLedger": "ledger", "EffectStore": "ledger",
    "InMemoryEffectStore": "ledger", "Record": "ledger", "StoredResult": "ledger",
    "Finding": "detector", "MixedEffectDetector": "detector",
    "MixedEffectWarning": "detector", "analyze": "detector",
}

__all__ = ["EffectExecutor", "Operation", "OperationConflict", "Claim",
           "OperationStore", "SQLiteOperationStore", "RecoveryDecision", "RecoveryPolicy", *_LEGACY]


def __getattr__(name):
    if name not in _LEGACY:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    value = getattr(import_module(f".{_LEGACY[name]}", __name__), name)
    globals()[name] = value
    return value
