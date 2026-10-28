"""obsidian_sync.feedback_loop — Phase 3F Step 6e stub.

Real Obsidian feedback integration not yet built. This stub matches the
real call signatures (intent= and case_id= kwargs) so reasoning_orchestrator
stops throwing TypeError on every run.

When the real feedback loop is implemented, replace this file. Public API:

    get_pre_draft_insights(intent: str = None, case_id: str = None,
                           **kwargs) -> dict
        Returns {prior_chains: [], related_cases: [], lessons_learned: []}
"""
from typing import Dict, Any


def get_pre_draft_insights(intent: str = None, case_id: str = None,
                            facts=None, **kwargs) -> Dict[str, Any]:
    """Return pre-draft insights from prior chains for this case.

    STUB — returns empty insights. Accepts any kwargs to match real
    callers (reasoning_orchestrator passes intent=, case_id=, facts=).
    """
    return {
        "prior_chains": [],
        "related_cases": [],
        "lessons_learned": [],
        "stub": True,
    }


def record_post_draft_outcome(case_id: str = None, draft_summary: dict = None,
                                **kwargs) -> bool:
    """Record outcome of a draft for future learning.

    STUB — returns True without recording. Accepts any kwargs.
    """
    return True
