"""
core/obsidian_parser.py — Parse structured counts from Obsidian markdown templates.

Reads foia_case_type.md (or any *_case_type.md) and returns claims_data JSON
compatible with complaint_drafter.build_complaint_from_facts().
"""

import re
from pathlib import Path
from typing import List, Dict


def parse_obsidian_counts(filepath: str) -> List[Dict]:
    """Parse structured counts from Obsidian markdown into claims_data JSON.
    
    Returns list of dicts, each with:
        title, statute, controlling_law, duties, elements,
        facts, breach_duty, breach_conduct, decision_trigger, relief
    """
    text = Path(filepath).read_text(encoding="utf-8")
    counts = []
    
    # Split by ## COUNT_N
    sections = re.split(r'## COUNT_\d+', text)
    
    for section in sections[1:]:  # Skip header
        count = {}
        
        # Title
        m = re.search(r'title:\s*(.+)', section)
        count["title"] = m.group(1).strip() if m else ""
        
        # Statute
        m = re.search(r'statute:\s*(.+)', section)
        count["statute"] = m.group(1).strip() if m else ""
        
        # Controlling law
        count["controlling_law"] = re.findall(
            r'- (.+(?:SHALL|shall|MUST|must).+)',
            section.split("duties:")[0] if "duties:" in section else section
        )
        
        # Duties
        duties = []
        duties_section = ""
        if "duties:" in section:
            start = section.index("duties:")
            end_markers = ["elements:", "relief:", "breach_duty:"]
            end = len(section)
            for em in end_markers:
                if em in section[start:]:
                    end = min(end, start + section[start:].index(em))
            duties_section = section[start:end]
        
        for m in re.finditer(r'- text:\s*(.+)\n\s*citation:\s*(.+)', duties_section):
            duties.append({"text": m.group(1).strip(), "citation": m.group(2).strip()})
        count["duties"] = duties
        
        # Elements
        elements = []
        elem_section = ""
        if "elements:" in section:
            start = section.index("elements:")
            end_markers = ["relief:", "breach_duty:", "decision_trigger:"]
            end = len(section)
            for em in end_markers:
                if em in section[start:]:
                    end = min(end, start + section[start:].index(em))
            elem_section = section[start:end]
        
        for m in re.finditer(r'- desc:\s*(.+)\n\s*if:\s*(.+)\n\s*and:\s*(.+)', elem_section):
            elements.append({
                "desc": m.group(1).strip(),
                "if": m.group(2).strip(),
                "and": m.group(3).strip(),
            })
        count["elements"] = elements
        
        # Relief
        relief_section = ""
        if "relief:" in section:
            start = section.index("relief:")
            end_markers = ["breach_duty:", "breach_conduct:", "decision_trigger:"]
            end = len(section)
            for em in end_markers:
                if em in section[start:]:
                    end = min(end, start + section[start:].index(em))
            relief_section = section[start:end]
        count["relief"] = [
            m.strip().lstrip("- ")
            for m in relief_section.split("\n")
            if m.strip().startswith("-")
        ]
        
        # Breach
        m = re.search(r'breach_duty:\s*(.+)', section)
        count["breach_duty"] = m.group(1).strip() if m else ""
        m = re.search(r'breach_conduct:\s*(.+)', section)
        count["breach_conduct"] = m.group(1).strip() if m else ""
        
        # Decision trigger
        dt = {}
        m = re.search(
            r'decision_trigger:\n\s*if:\s*(.+)\n\s*and:\s*(.+)\n\s*then:\s*(.+)',
            section
        )
        if m:
            dt = {
                "if": m.group(1).strip(),
                "and": m.group(2).strip(),
                "then": m.group(3).strip(),
            }
        count["decision_trigger"] = dt
        
        # Facts placeholder (injected by pipeline)
        count["facts"] = []
        
        if count["title"]:
            counts.append(count)
    
    return counts
