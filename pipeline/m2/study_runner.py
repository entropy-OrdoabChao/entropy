"""Compatibility shim — the runner is pipeline.m2.analysis_runner (m0 ANALYSIS divided by axis; "study" was a misnomer that blurred it into the counsel learning fleet)."""
from pipeline.m2.analysis_runner import *  # noqa
from pipeline.m2.analysis_runner import main

# CLASS FIX (2026-10-22, mail-watch run): `from ... import *` NEVER imports names that start with
# "_", so every private module-level object of analysis_runner was invisible through this shim.
# draft_runner.py:309 does `sr._LAST_USAGE.pop(...)` -> AttributeError, which killed every drafting
# cell in the thread pool (the m2 drafting lane produced zero argument text). Re-export the private
# module-level objects BY REFERENCE so `study_runner._X is analysis_runner._X` (mutable state such as
# _LAST_USAGE is mutated in place, never rebound, so the shared identity is the whole point).
import pipeline.m2.analysis_runner as _analysis_runner
for _n in dir(_analysis_runner):
    if _n.startswith("_") and not _n.startswith("__") and _n not in globals():
        globals()[_n] = getattr(_analysis_runner, _n)
del _n
if __name__ == "__main__":
    import sys; sys.exit(main())
