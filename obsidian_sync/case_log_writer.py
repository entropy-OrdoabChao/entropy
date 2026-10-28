"""obsidian_sync.case_log_writer — Phase 3F Step 6f stub.

Real case log writer not yet built. Stub matches real call signature AND
return shape — the real caller does `result, msg = write_case_log(...)`,
so we return a (bool, str) tuple, not a bare bool.
"""
from typing import Dict, Any, Tuple


def write_case_log(canonical_id: str = None, case_id: str = None,
                    entry: Dict[str, Any] = None, **kwargs) -> Tuple[bool, str]:
    """Write a case log entry to Obsidian.

    STUB — returns (True, 'stub'). Real caller does tuple-unpack
    `result, msg = write_case_log(...)` so we must return a tuple.
    """
    return (True, "stub")


def append_draft_event(canonical_id: str = None, case_id: str = None,
                        event: Dict[str, Any] = None, **kwargs) -> Tuple[bool, str]:
    """Append a drafting event to case timeline.

    STUB — returns (True, 'stub').
    """
    return (True, "stub")
