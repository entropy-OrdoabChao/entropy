"""
Memory Manager — creates/overwrites memory.md per run.
Tracks pipeline state for debugging. NOT persistent.
"""
import os
import json
from datetime import datetime, timezone

try:
    from core.paths import MEMORY_FILE
except ImportError:
    MEMORY_FILE = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "memory", "memory.md")

class MemoryManager:
    def __init__(self):
        self.run_id = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        self.stages = []
        self.facts = []
        self.flags = []
        self.violations = []
        self.remedies = []
        self.draft_status = "NOT_STARTED"
        self.defendants = []
        self.exhibits = []

    def add_stage(self, stage: str):
        self.stages.append({"stage": stage, "ts": datetime.now(timezone.utc).isoformat()})

    def add_facts(self, facts: list):
        self.facts.extend(facts)

    def add_flag(self, flag: str):
        self.flags.append(flag)

    def add_violation(self, v: dict):
        self.violations.append(v)

    def add_remedy(self, r: str):
        self.remedies.append(r)

    def set_draft_status(self, status: str):
        self.draft_status = status

    def write(self):
        os.makedirs(os.path.dirname(MEMORY_FILE), exist_ok=True)
        lines = [
            f"# MEMORY.md",
            f"",
            f"## Run ID",
            f"{self.run_id}",
            f"",
            f"## Stages Completed",
        ]
        for s in self.stages:
            lines.append(f"- [{s['ts']}] {s['stage']}")

        lines += ["", "## Facts Extracted"]
        for f in self.facts:
            lines.append(f"- {f}")

        lines += ["", "## Flags Detected"]
        for f in self.flags:
            lines.append(f"- {f}")

        lines += ["", "## Violations"]
        for v in self.violations:
            if isinstance(v, dict):
                lines.append(f"- {v.get('statute', 'unknown')}: {v.get('description', '')}")
            else:
                lines.append(f"- {v}")

        lines += ["", "## Remedies"]
        for r in self.remedies:
            lines.append(f"- {r}")

        lines += ["", "## Defendants"]
        for d in self.defendants:
            if isinstance(d, dict):
                lines.append(f"- {d.get('name', '')}: {d.get('role', '')} ({d.get('capacity', 'official')})")
            else:
                lines.append(f"- {d}")

        lines += ["", "## Exhibits"]
        for e in self.exhibits:
            lines.append(f"- {e}")

        lines += ["", f"## Draft Status", self.draft_status]

        with open(MEMORY_FILE, 'w', encoding='utf-8') as f:
            f.write("\n".join(lines))
        return MEMORY_FILE
