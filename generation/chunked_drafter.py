"""

generation/chunked_drafter.py — Chunked Document Drafter (v2 - grounded).



Key fix from v1:

  Every section now includes a CASE OVERVIEW block that pins down:

    - what motion is being addressed

    - real party names

    - the actual filing type

    - the procedural posture

    - the conflict map summary



This prevents the LLM from hallucinating "Motion to Dismiss" when the

actual motion is something else, or inventing causes of action.



Plus stricter section prompts that say "USE ONLY this data."

"""



import re

import time

from typing import Optional



from core.logger import get_logger

from generation.section_builder import build_sections, merge_sections, DraftSection



log = get_logger("DRAFTER")





BASE_DRAFTER_PROMPT = """You are a legal advocate writing one section of a court filing at a time.



You operate in ADVERSARIAL LITIGATION MODE on behalf of YOUR CLIENT

(identified in CASE OVERVIEW under "We represent").



═══════════════════════════════════════════════════════════════════

ROLE

═══════════════════════════════════════════════════════════════════

You are NOT a neutral analyst. You are an advocate.

Your job is to use the provided facts and rules to argue FOR your client.

You take a position. You do not hedge. You do not concede.



═══════════════════════════════════════════════════════════════════

ABSOLUTE RULES (NEVER VIOLATE)

═══════════════════════════════════════════════════════════════════

- NEVER invent facts not in the provided data

- NEVER invent rules, citations, or case names not in the provided data

- NEVER misquote authority

- NEVER adopt the opposing party's framing

- NEVER repeat opposing arguments without immediately rebutting them

- Use ONLY the data in the user message



═══════════════════════════════════════════════════════════════════

ADVOCACY DIRECTIVES (REQUIRED BEHAVIOR)

═══════════════════════════════════════════════════════════════════

For every issue presented:

  1. Frame the opposing position as theirs ("Garnishee argues...", "Defendant contends...")

  2. Identify the controlling rule from the inputs

  3. Apply the rule to the facts deterministically

  4. Conclude in your client's favor whenever the law supports it



When a rule contains MANDATORY language (shall, must, is required to)

AND the facts show non-compliance:

  → State the rule was violated. PERIOD.

  → Do NOT say "indeterminate"

  → Do NOT say "requires further citation"

  → Do NOT defer to a later filing



When opposing authority is cited against you:

  → Explain why it does not control

  → Distinguish on facts or procedural posture

  → Reaffirm your rule



When multiple legal interpretations exist:

  → Adopt the one that supports your client

  → Explain why it is legally correct



═══════════════════════════════════════════════════════════════════

FORBIDDEN LANGUAGE

═══════════════════════════════════════════════════════════════════

NEVER use these phrases — they are surrender:

  - "indeterminate"

  - "may", "might", "possibly", "arguably", "perhaps"

  - "Plaintiff reserves the right to supplement"

  - "requires further citation"

  - "is not clear"

  - "could be argued"



REQUIRED phrasing for conclusions:

  - "fails as a matter of law"

  - "violates the rule"

  - "is procedurally defective"

  - "must be stricken"

  - "the record establishes"

  - "Accordingly, [outcome]"



═══════════════════════════════════════════════════════════════════

OUTPUT FORMAT

═══════════════════════════════════════════════════════════════════

- Plain prose only — no markdown, no bullets, no headers

- Do NOT repeat the section title (added programmatically)

- Do NOT include a preamble like "Here is the section:"

- Use real party names from CASE OVERVIEW



═══════════════════════════════════════════════════════════════════

THE ONLY RESTRICTION ON ASSERTION

═══════════════════════════════════════════════════════════════════

You may only assert what the provided facts and rules support.

You may NOT invent law to win an argument.

But within those bounds, you MUST argue aggressively and take a position."""





def draft_document_chunked(

    doc_type: str,

    case_context: dict = None,

    facts: list = None,

    authority: str = "",

    conflicts: list = None,

    procedural: dict = None,

    progress_callback=None,

) -> dict:

    """Generate a complete legal document using chunked LLM calls."""

    case_context = case_context or {}

    facts = facts or []

    conflicts = conflicts or []

    procedural = procedural or {}

    t0 = time.time()



    case_overview = _build_case_overview(

        doc_type, case_context, procedural, conflicts, facts

    )

    log.stage("draft_chunked", f"Case overview built ({len(case_overview)} chars)")



    plan = build_sections(

        doc_type=doc_type,

        case_context=case_context,

        facts=facts,

        authority=authority,

        conflicts=conflicts,

        procedural=procedural,

    )

    log.stage("draft_chunked", f"Plan: {len(plan)} sections for {doc_type}")



    if not plan:

        return {

            "text": "", "sections_completed": 0, "sections_failed": 0,

            "total_time_ms": 0, "plan": [],

            "error": "No section plan generated",

        }



    try:

        from pipeline.llm_gate import get_gate

        gate = get_gate()

        if not gate.is_available():

            log.warning("draft_chunked", "LLM gate offline, falling back to template")

            return _template_only_fallback(plan, doc_type)

    except Exception as e:

        log.error("draft_chunked", "Cannot get LLM gate", error=str(e))

        return _template_only_fallback(plan, doc_type)



    rendered = {}

    completed = 0

    failed = 0



    for i, section in enumerate(plan):

        section_t0 = time.time()

        log.stage("draft_chunked", f"[{i+1}/{len(plan)}] Generating: {section.name}")



        if progress_callback:

            try:

                progress_callback(i, len(plan), section.name)

            except Exception:

                pass



        try:

            # PHASE 3.5h: For argument sections, assemble deterministically.

            # The LLM is only used for the small "their position + rebuttal"

            # connector text. Definitions and IF/THEN go in verbatim — they are

            # NEVER touched by the LLM, so they cannot be corrupted.

            if section.section_type == "argument":

                text = _assemble_argument_section(gate, section, case_overview)

            else:

                text = _generate_section(gate, section, case_overview)



            section_elapsed = int((time.time() - section_t0) * 1000)



            if text and len(text.strip()) > 20:

                if "[INSUFFICIENT DATA]" in text and len(text) < 100:

                    log.warning("draft_chunked",

                             f"[{i+1}/{len(plan)}] {section.name} returned only INSUFFICIENT DATA")

                    rendered[section.name] = f"[Section {section.name}: insufficient data to draft]"

                    failed += 1

                else:

                    rendered[section.name] = text

                    completed += 1

                    log.stage("draft_chunked",

                              f"[{i+1}/{len(plan)}] {section.name} OK "

                              f"({len(text)} chars, {section_elapsed}ms)")

            else:

                failed += 1

                log.warning("draft_chunked",

                         f"[{i+1}/{len(plan)}] {section.name} returned empty")

                rendered[section.name] = f"[Section {section.name} not generated]"

        except Exception as e:

            failed += 1

            log.error("draft_chunked",

                      f"[{i+1}/{len(plan)}] {section.name} failed",

                      error=str(e))

            rendered[section.name] = f"[Section {section.name} failed: {str(e)[:80]}]"



    # PATCH AUTHORITY_ATTACK: deterministic non-binding citation attack section.

    # Runs the authority_attack_agent against audit data (or in-memory conflicts)

    # and injects a "Section III. DEFENDANT RELIES ON NON-BINDING AUTHORITY"

    # block before the conclusion. NO LLM — pure template fill from scorecard.

    try:

        from pipeline.authority_attack_agent import run_authority_attack, load_violations_from_audit

        from core.path_manager import BASE_PATH



        # Try to find audit data for this case

        target_court = "UNKNOWN"

        violations = []

        cid = (case_context or {}).get("canonical_id") or (case_context or {}).get("registry_id")

        if not cid:

            # Derive from case_no + court

            court_str = ((case_context or {}).get("court", "") or "").lower()

            case_no = (case_context or {}).get("case_no", "")

            if case_no:

                if "common pleas" in court_str or "phila" in court_str:

                    cid = f"phila_ccp:{case_no}"

                elif "district" in court_str or "edpa" in court_str:

                    clean = case_no.replace(" ", "")

                    if not clean.startswith("2:") and "cv" in clean:

                        clean = f"2:{clean}"

                    cid = f"fed:{clean}"



        if cid:

            safe = cid.replace(":", "_").replace("/", "_")

            audit_path = BASE_PATH / "output" / "audit" / f"data_{safe}.json"

            violations, target_court = load_violations_from_audit(audit_path)



        # Fallback: use in-memory conflicts if no audit data

        if not violations and conflicts:

            for c in conflicts:

                if isinstance(c, dict):

                    pos = c.get("their_position", "") or c.get("position", "")

                    if pos:

                        violations.append({

                            "citation": pos[:200],

                            "cited_court": "UNKNOWN",

                            "severity": "fatal" if c.get("status") == "violation" else "serious",

                            "rule_violated": c.get("rule_authority", ""),

                        })

            # Derive target court from case_context

            court_str = ((case_context or {}).get("court", "") or "").lower()

            if "common pleas" in court_str or "phila" in court_str:

                target_court = "PA_CP_PHILA"

            elif "district" in court_str or "edpa" in court_str:

                target_court = "EDPA"



        if violations and target_court != "UNKNOWN":

            # Pick a section number that comes after the last argument section

            arg_count = sum(1 for s in plan if s.section_type == "argument")

            section_num_int = 3 + max(0, arg_count - 1)

            roman_map = {1: "I", 2: "II", 3: "III", 4: "IV", 5: "V", 6: "VI", 7: "VII", 8: "VIII"}

            section_num = roman_map.get(section_num_int, str(section_num_int))



            attack = run_authority_attack(target_court, violations, section_num)

            if attack.get("drafted_section"):

                # Inject the section into rendered dict under a synthetic key

                rendered["__authority_attack__"] = attack["drafted_section"]

                # Append conclusion addendum to the existing conclusion text

                if attack.get("drafted_conclusion_addendum") and "conclusion" in rendered:

                    if rendered["conclusion"] and not rendered["conclusion"].startswith("[Section"):

                        rendered["conclusion"] = (

                            rendered["conclusion"].rstrip() + "\n\n" +

                            attack["drafted_conclusion_addendum"]

                        )

                log.stage("authority_attack",

                          f"Injected attack section: {attack['non_binding_count']}/{attack['total_count']} non-binding ({attack['non_binding_percentage']}%)")

    except Exception as e:

        log.warning("authority_attack", f"Could not generate authority attack section: {e}")



    final_text = merge_sections(rendered, plan)



    # PATCH AUTHORITY_ATTACK: if we injected an attack section, splice it in

    # before the conclusion in the final merged text.

    if "__authority_attack__" in rendered:

        attack_text = rendered["__authority_attack__"]

        # Find the conclusion section header and insert before it

        conclusion_markers = ["CONCLUSION", "Conclusion", "WHEREFORE"]

        inserted = False

        for marker in conclusion_markers:

            idx = final_text.rfind(marker)

            if idx > 0:

                # Walk back to find the start of the line / section header

                line_start = final_text.rfind("\n", 0, idx)

                if line_start > 0:

                    final_text = final_text[:line_start + 1] + "\n" + attack_text + "\n" + final_text[line_start + 1:]

                    inserted = True

                    break

        if not inserted:

            # Fallback: append to end

            final_text = final_text.rstrip() + "\n\n" + attack_text + "\n"



    total_elapsed = int((time.time() - t0) * 1000)



    log.stage("draft_chunked",

              f"COMPLETE: {completed}/{len(plan)} sections, "

              f"{len(final_text)} chars total, {total_elapsed}ms")



    return {

        "text": final_text,

        "sections_completed": completed,

        "sections_failed": failed,

        "sections_total": len(plan),

        "total_time_ms": total_elapsed,

        "plan": [s.to_dict() for s in plan],

    }





def _build_case_overview(

    doc_type: str,

    case_context: dict,

    procedural: dict,

    conflicts: list,

    facts: list,

) -> str:

    """Build a case overview block prepended to EVERY section.



    This is the LLM's only allowed source of context for the case.

    """

    lines = ["## CASE OVERVIEW (USE ONLY THIS — DO NOT INVENT)"]



    # Parties

    lines.append("")

    lines.append("### PARTIES")

    if case_context.get("plaintiff"):

        lines.append(f"- Plaintiff: {case_context['plaintiff']}")

    if case_context.get("defendant"):

        lines.append(f"- Defendant: {case_context['defendant']}")

    if case_context.get("garnishee"):

        lines.append(f"- Garnishee: {case_context['garnishee']}")

    if case_context.get("opposing_counsel"):

        lines.append(f"- Opposing counsel: {case_context['opposing_counsel']}")



    # ═══ POLARITY DECLARATION (THE MOST IMPORTANT BLOCK) ═══════════════

    your_role = (procedural.get("your_role", "plaintiff") if procedural else "plaintiff")

    your_party_name = case_context.get(your_role, "")

    if not your_party_name:

        your_party_name = case_context.get("plaintiff", "Plaintiff")



    # Identify the opposing party by elimination

    opposing_name = ""

    if your_role == "plaintiff":

        opposing_name = (case_context.get("garnishee", "")

                         or case_context.get("defendant", "")

                         or "the opposing party")

    elif your_role == "defendant":

        opposing_name = case_context.get("plaintiff", "the opposing party")

    elif your_role == "garnishee":

        opposing_name = case_context.get("plaintiff", "the opposing party")



    lines.append("")

    lines.append("### ⚔️ POLARITY (NON-NEGOTIABLE)")

    lines.append(f"- WE represent: {your_party_name} ({your_role})")

    lines.append(f"- OPPOSING party: {opposing_name}")

    lines.append(f"- We are advocating FOR {your_party_name}.")

    lines.append(f"- We are advocating AGAINST {opposing_name}.")

    lines.append(f"- Every conclusion must favor {your_party_name}.")

    lines.append(f"- Every argument must attack {opposing_name}'s position.")

    lines.append(f"- NEVER write a sentence that validates {opposing_name}'s position.")

    lines.append(f"- NEVER conclude that {opposing_name}'s actions were 'proper' or 'appropriate'.")

    lines.append(f"- NEVER write 'judgment is not warranted' — we WANT judgment in our favor.")



    # Court

    lines.append("")

    lines.append("### CASE")

    if case_context.get("court"):

        lines.append(f"- Court: {case_context['court']}")

    if case_context.get("case_no"):

        lines.append(f"- Case No: {case_context['case_no']}")



    # This filing

    lines.append("")

    lines.append("### THIS FILING")

    lines.append(f"- Document type to draft: {doc_type.replace('_', ' ').title()}")



    if procedural:

        posture = procedural.get("posture", "")

        if posture:

            lines.append(f"- Procedural posture: {_explain_posture(posture)}")

        if procedural.get("rule_basis"):

            lines.append(f"- Rule basis: {procedural['rule_basis']}")

        if procedural.get("your_role"):

            lines.append(f"- We represent: {procedural['your_role']}")



    motion_being_addressed = _infer_motion_being_addressed(

        case_context, procedural, conflicts

    )

    if motion_being_addressed:

        lines.append(f"- Motion being addressed: {motion_being_addressed}")



    # Issues from conflict map

    if conflicts:

        lines.append("")

        lines.append("### ISSUES TO ADDRESS (from conflict analysis)")

        for i, c in enumerate(conflicts[:6], 1):

            cd = c if isinstance(c, dict) else c.to_dict()

            issue = cd.get("issue", f"issue_{i}")

            their_pos = cd.get("their_position", "")[:200]

            lines.append(f"{i}. {issue}: {their_pos}")



    # ─── PHASE 3.5j: RECORD FACTS (separate block, used by drafter) ───

    # CRITICAL: this block is what _extract_facts_from_overview reads.

    # ISSUES TO ADDRESS contains opposing positions, not facts. Keeping

    # them separate prevents the polarity engine from rewriting opposing

    # positions as if they were neutral record facts.

    if facts:

        lines.append("")

        lines.append("### RECORD FACTS")

        for i, f in enumerate(facts[:15], 1):

            if f and isinstance(f, str):

                # Clean fact: strip any leading numbering

                clean_fact = re.sub(r"^\d+\.\s*", "", f.strip())

                if clean_fact:

                    lines.append(f"{i}. {clean_fact}")



    # Strict warnings — dynamically built from the actual case data

    lines.append("")

    lines.append("### CRITICAL WARNINGS")



    # Build a positive-space description: what the case IS about

    case_topics = []

    if conflicts:

        for c in conflicts[:6]:

            cd = c if isinstance(c, dict) else c.to_dict()

            issue = cd.get("issue", "")

            if issue and issue != "general" and issue not in case_topics:

                case_topics.append(issue)



    if case_topics:

        topics_str = ", ".join(case_topics)

        lines.append(f"- This case involves ONLY these issues: {topics_str}")

        lines.append(f"- Do NOT introduce any legal theory outside this list.")

    else:

        lines.append("- Use ONLY the issues, parties, and facts listed in CASE OVERVIEW.")



    lines.append("- Do NOT invent motions that are not listed in 'Motion being addressed'.")

    lines.append("- Do NOT invent parties not listed in PARTIES.")

    lines.append("- Do NOT invent rules or citations not provided in the section inputs.")

    lines.append("- If asked to address something not in the data above, "

                 "write '[INSUFFICIENT DATA]'.")



    return "\n".join(lines)





def _explain_posture(posture: str) -> str:

    explanations = {

        "opposition_received": "We received an opposition brief. We are drafting a REPLY.",

        "motion_received": "We received a motion. We are drafting an OPPOSITION.",

        "complaint_received": "We received a complaint. We are drafting preliminary objections or an answer.",

        "garnishee_defective_filing": "Garnishee filed a defective response. We are drafting a motion to strike.",

        "garnishee_default": "Garnishee defaulted. We are drafting a praecipe for judgment.",

        "adverse_order": "We received an adverse order. We are drafting a reconsideration motion.",

        "new_action": "Initiating new action. We are drafting a complaint.",

        "pre_litigation": "Pre-litigation. We are drafting a demand letter or notice.",

    }

    return explanations.get(posture, posture)





def _infer_motion_being_addressed(case_context, procedural, conflicts) -> str:

    filing_title = case_context.get("current_filing_title", "")

    if filing_title and len(filing_title) > 5:

        return filing_title



    if conflicts:

        issues = []

        for c in conflicts[:3]:

            cd = c if isinstance(c, dict) else c.to_dict()

            issue = cd.get("issue", "")

            if issue and issue != "general":

                issues.append(issue)

        if issues:

            return f"Motion involving: {', '.join(set(issues))}"



    return "(unspecified — see conflict map)"





def _generate_section(gate, section: DraftSection, case_overview: str) -> str:

    user_content = _build_section_input(section, case_overview)



    result = gate.llm.chat(

        messages=[

            {"role": "system", "content": BASE_DRAFTER_PROMPT},

            {"role": "user", "content": user_content},

        ],

        thinking=False,

        n_predict=min(2000, section.expected_length + 800),

    )



    return result.strip() if result else ""





# ══════════════════════════════════════════════════════════════════════

# DETERMINISTIC ARGUMENT ASSEMBLY (PHASE 3.5h)

# ══════════════════════════════════════════════════════════════════════

#

# Argument sections are assembled from deterministic blocks:

#   1. Definitions block (verbatim from definition_engine — never touched)

#   2. Their position + rebuttal (small LLM call, ~200 chars)

#   3. IF/THEN logic chain (verbatim from if_then_engine — never touched)

#   4. Fact application (small LLM call, ~300 chars)

#   5. Conclusion (deterministic template, no LLM)

#

# The LLM only writes the connector prose. The locked legal content

# (definitions, IF/THEN, conclusion) goes in literally so it cannot be

# corrupted by LLM hallucination or auto-completion.

# ══════════════════════════════════════════════════════════════════════



CONNECTOR_REBUTTAL_PROMPT = """You are writing a single short paragraph for a legal brief.



Write 2-3 sentences ONLY. No more.



Format the opposing party's position as theirs:

   "[Opposing party] argues that [paraphrase their position]."



Then immediately rebut:

   "This argument fails as a matter of law."



Use formal legal English. NO markdown. NO preamble. NO explanation.

Output ONLY the paragraph text."""





CONNECTOR_FACT_APPLICATION_PROMPT = """You are writing a single short paragraph for a legal brief.



Write 2-3 sentences ONLY connecting the rule to the specific facts of this case.



Use the EXACT party names, dates, and amounts provided.

Do NOT invent any new facts.

Do NOT cite any rules — those are handled in adjacent paragraphs.



Use formal legal English. NO markdown. NO preamble. NO explanation.

Output ONLY the paragraph text."""





def _assemble_argument_section(gate, section: DraftSection, case_overview: str) -> str:

    """Assemble an argument section deterministically.



    Definitions and IF/THEN blocks go in VERBATIM.

    LLM is used only for two small connector paragraphs (~250 chars each).

    Everything else is template assembly.

    """

    inputs = section.inputs or {}



    plaintiff = inputs.get("plaintiff", "Plaintiff")

    issue = inputs.get("issue", "this issue").replace("_", " ")

    their_position = inputs.get("their_position", "")

    definitions_text = inputs.get("definitions_text", "")

    logic_chain_text = inputs.get("logic_chain_text", "")

    attack_vector = inputs.get("attack_vector", "application")



    # Identify opposing party from case overview (parsed from PARTIES section)

    opposing_party = _extract_opposing_party_from_overview(case_overview)



    blocks = []



    # ─── Block 1: DEFINITIONS (verbatim, no LLM) ──────────────────────

    if definitions_text:

        # Use only the FIRST 3 definitions to keep section tight

        # (your friend pointed out that 6 definitions was overkill)

        trimmed = _trim_definitions(definitions_text, max_count=3)

        blocks.append(trimmed)



    # ─── Block 2: THEIR POSITION + REBUTTAL (deterministic, no LLM) ───

    # PHASE 3.5i: removed LLM call. The rebuttal is templated so the LLM

    # cannot accidentally adopt or "soften" the opposing position.

    rebuttal_para = _build_deterministic_rebuttal(

        opposing_party, their_position, issue

    )

    blocks.append(rebuttal_para)



    # ─── Block 3: IF/THEN LOGIC CHAIN (verbatim, no LLM) ──────────────

    if logic_chain_text:

        blocks.append(logic_chain_text)



    # ─── Block 4: FACT APPLICATION (deterministic — no LLM) ───────────

    # PHASE 3.5i: This block was an LLM call that kept flipping polarity.

    # Now it's deterministically built from plaintiff-framed facts.

    fact_para = _build_deterministic_fact_application(

        case_overview, issue, plaintiff, opposing_party, attack_vector

    )

    blocks.append(fact_para)



    # ─── Block 5: CONCLUSION (deterministic template, no LLM) ─────────

    conclusion = _build_deterministic_conclusion(issue, attack_vector)

    blocks.append(conclusion)



    assembled = "\n\n".join(blocks)



    # ─── PHASE 3.5j: PRE-DRAFT VALIDATION ─────────────────────────────

    # Last line of defense: catch double negatives, defense leaks,

    # plaintiff attacks, contradictions before the section is saved.

    try:

        from pipeline.pre_draft_validator import validate_text

        validation = validate_text(assembled)

        if validation.fixes_applied:

            log.stage("assemble_arg",

                      f"Validator applied {len(validation.fixes_applied)} fixes")

        if validation.issues_found:

            log.warning("assemble_arg",

                     f"Validator flagged {len(validation.issues_found)} issues")

        return validation.cleaned_text

    except Exception as e:

        log.warning("assemble_arg", f"Validator failed, returning unvalidated", error=str(e))

        return assembled





def _llm_connector(gate, system_prompt: str, user_content: str, max_tokens: int = 300) -> str:

    """Make a small focused LLM call for connector prose only.



    Tiny prompt, tiny output, no thinking. Should complete in 10-30 seconds.

    """

    result = gate.llm.chat(

        messages=[

            {"role": "system", "content": system_prompt},

            {"role": "user", "content": user_content},

        ],

        thinking=False,

        n_predict=max_tokens,

    )

    return result.strip() if result else ""





def _trim_definitions(definitions_text: str, max_count: int = 3) -> str:

    """Keep only the first N definitions to avoid bloat.



    Definitions are formatted as: "X" means Y. "X" means Y. ...

    """

    if not definitions_text:

        return ""

    # Split on the pattern '. "' which separates definitions

    sentences = re.split(r'(?<=\.) (?=")', definitions_text)

    return " ".join(sentences[:max_count])





def _extract_opposing_party_from_overview(case_overview: str) -> str:

    """Pull opposing party name from the case overview text."""

    # Look for the polarity declaration

    m = re.search(r"OPPOSING party:\s*([^\n]+)", case_overview)

    if m:

        return m.group(1).strip()

    # Fallback: garnishee or defendant

    m = re.search(r"Garnishee:\s*([^\n]+)", case_overview)

    if m:

        return m.group(1).strip()

    m = re.search(r"Defendant:\s*([^\n]+)", case_overview)

    if m:

        return m.group(1).strip()

    return "The opposing party"





def _extract_facts_from_overview(case_overview: str) -> list:

    """Pull facts from the RECORD FACTS block of the case overview.



    PHASE 3.5j: This used to read from ISSUES TO ADDRESS, which contained

    opposing party positions formatted with "issue:" prefixes. That caused

    the drafter to treat opposing positions as record facts, leading to

    polarity leaks like "Plaintiff has not did not comply".



    Now reads ONLY from RECORD FACTS, which contains actual extracted facts.

    """

    facts = []

    in_record = False

    for line in case_overview.split("\n"):

        if "RECORD FACTS" in line:

            in_record = True

            continue

        if in_record:

            if line.startswith("###"):

                break

            stripped = line.strip()

            if stripped and (stripped[0].isdigit() or stripped.startswith("-")):

                # Strip leading number/dash and any trailing junk

                clean = re.sub(r"^\d+\.\s*", "", stripped)

                clean = clean.lstrip("- ")

                # CRITICAL: strip "issue:" prefix if present (defense-in-depth)

                clean = re.sub(r"^[a-z_]+:\s*", "", clean)

                if clean:

                    facts.append(clean)

    return facts





def _build_deterministic_rebuttal(opposing_party: str, their_position: str, issue: str) -> str:

    """Build the rebuttal paragraph deterministically.



    Frames opposing party's position as theirs and immediately rebuts.

    No LLM call — pure template assembly.

    """

    issue_label = issue.replace("_", " ")



    if their_position:

        # Trim and clean the position

        pos = their_position.strip()

        # Remove leading "They say" / "Their argument" type prefixes

        pos = re.sub(r"^(?:they\s+(?:say|argue|contend)\s+that\s*)", "", pos, flags=re.IGNORECASE)

        pos = re.sub(r"^(?:argues?\s+that\s*)", "", pos, flags=re.IGNORECASE)

        # Cap length

        pos = pos[:280]

        # Lowercase first letter for grammar flow

        if pos and pos[0].isupper():

            pos = pos[0].lower() + pos[1:]

        return (

            f"{opposing_party} argues that {pos}. "

            f"This argument fails as a matter of law."

        )



    return (

        f"{opposing_party} contends that the {issue_label} requirement does not "

        f"control this dispute. This argument fails as a matter of law."

    )





def _build_deterministic_fact_application(

    case_overview: str,

    issue: str,

    plaintiff: str,

    opposing_party: str,

    attack_vector: str,

) -> str:

    """Build the fact application paragraph deterministically.



    PHASE 3.5k: Now applies a relevance filter before polarity reframing.

    Each argument section sees only facts relevant to its issue, not the

    full noisy fact list (which contains case caption, dates, judge names).



    NO LLM CALL — pure assembly.

    """

    from pipeline.fact_polarity_engine import reframe_facts



    # Get raw facts from case overview

    raw_facts = _extract_facts_from_overview(case_overview)



    # PHASE 3.5k: filter facts for relevance to THIS issue

    relevant_facts = _filter_facts_by_issue(raw_facts, issue)



    # Reframe through polarity engine

    framed = reframe_facts(relevant_facts, issue=issue)

    plaintiff_facts = [f.plaintiff_framing.rstrip(".") for f in framed if f.plaintiff_framing][:3]



    issue_label = issue.replace("_", " ")



    if plaintiff_facts:

        # Build a 2-3 sentence paragraph from the framed facts

        if len(plaintiff_facts) == 1:

            return (

                f"The record establishes the following with respect to the "

                f"{issue_label} issue: {plaintiff_facts[0]}. "

                f"These facts trigger the rule above and compel the conclusion that follows."

            )

        elif len(plaintiff_facts) == 2:

            return (

                f"The record establishes the following with respect to the "

                f"{issue_label} issue. First, {plaintiff_facts[0]}. "

                f"Second, {plaintiff_facts[1]}. "

                f"These facts trigger the rule above and compel the conclusion that follows."

            )

        else:

            return (

                f"The record establishes the following with respect to the "

                f"{issue_label} issue. {plaintiff_facts[0]}. "

                f"{plaintiff_facts[1]}. {plaintiff_facts[2]}. "

                f"These facts trigger the rule above and compel the conclusion that follows."

            )



    # Truly no relevant facts — fall back to a generic but pro-plaintiff statement

    return (

        f"The record establishes the operative facts on the {issue_label} "

        f"issue. These facts trigger the rule above and compel the conclusion "

        f"that follows in {plaintiff}'s favor."

    )





# ══════════════════════════════════════════════════════════════════════

# FACT RELEVANCE FILTER (PHASE 3.5k)

# ══════════════════════════════════════════════════════════════════════



ISSUE_KEYWORDS = {

    "verification": [

        "verif", "sworn", "oath", "attest", "praecipe", "signature", "signed",

        "objection", "answer", "filing",

    ],

    "garnishment": [

        "garnish", "writ", "execution", "asset", "property", "possession",

        "interrogator", "judgment debtor", "answer",

    ],

    "service": [

        "serv", "deliver", "mail", "notice", "process",

    ],

    "default": [

        "default", "respond", "answer", "reply", "deadline", "failed to",

    ],

    "discovery": [

        "interrogator", "deposit", "request for production", "discover",

        "request for admiss",

    ],

    "jurisdiction": [

        "jurisdic", "personal", "subject matter", "venue",

    ],

    "due_process": [

        "notice", "opportunity", "hear", "due process", "constitution",

    ],

    "fcra_furnisher": [

        "credit", "furnisher", "report", "trade line", "fcra", "1681",

        "dispute", "investigat",

    ],

    "failure_to_investigate": [

        "investigat", "dispute", "reasonable inquiry", "review",

    ],

    "pleading_sufficiency": [

        "pleading", "complaint", "averment", "allegation", "plausible",

    ],

}





# Patterns that should ALWAYS be excluded as "junk facts" (case metadata,

# not substantive record facts). These are facts the LLM extracted that

# look like "the case is styled X v Y" or "the order was issued on date".

JUNK_FACT_PATTERNS = [

    r"^the\s+case\s+is\s+(?:styled|assigned|pending)",

    r"^the\s+document\s+is\s+(?:dated|titled)",

    r"^the\s+order\s+was\s+(?:issued|signed|entered|denied)",

    r"^the\s+court\s+(?:denied|granted|entered)",

    r"^the\s+case\s+number\s+is",

    r"^control\s+(?:no|number)",

    r"^judge\s+\w+",

    r"^case\s+id\s+\d",

    r"^plaintiff\s+is\s+\w",

    r"^defendant\s+is\s+\w",

]





def _filter_facts_by_issue(facts: list, issue: str) -> list:

    """Filter facts to only those relevant to the given issue.



    Strategy:

      1. Drop junk facts (case metadata, dates without action, etc.)

      2. Keep facts containing keywords for this issue

      3. Always keep facts with specific dates (Feb 10, March 6, etc.)

         IF they also contain action verbs (filed, served, responded)

      4. If filter removes everything, return all non-junk facts as fallback

    """

    # PATCH STATE_FIX: Drop facts that are opposing-counsel characterizations

    # of court rulings (e.g., "the Court denied the motion") which the LLM

    # extracts from briefs but should not be treated as record facts.

    try:

        from pipeline.fact_polarity_engine import is_procedural_ruling_safe

        facts = [f for f in (facts or []) if is_procedural_ruling_safe(f)]

    except ImportError:

        pass

    if not facts:

        return []



    issue_lower = issue.lower()

    keywords = ISSUE_KEYWORDS.get(issue_lower, [])



    # Compile junk patterns

    junk_regexes = [re.compile(p, re.IGNORECASE) for p in JUNK_FACT_PATTERNS]



    # First pass: drop junk

    non_junk = []

    for f in facts:

        if not f or not isinstance(f, str):

            continue

        f_clean = f.strip()

        if any(rx.search(f_clean) for rx in junk_regexes):

            continue

        non_junk.append(f_clean)



    # Second pass: keep facts matching issue keywords or containing action verbs + dates

    relevant = []

    action_verbs = ["filed", "served", "responded", "submitted", "attached",

                    "delivered", "investigated", "objected", "answered"]

    date_pattern = re.compile(

        r"\b(?:january|february|march|april|may|june|july|august|"

        r"september|october|november|december)\s+\d{1,2}",

        re.IGNORECASE

    )



    for f in non_junk:

        f_lower = f.lower()

        # Match by issue keyword

        if any(kw in f_lower for kw in keywords):

            relevant.append(f)

            continue

        # Match by action verb + date (likely substantive)

        if (any(v in f_lower for v in action_verbs) and date_pattern.search(f)):

            relevant.append(f)

            continue



    # Fallback: if filter killed everything, return non-junk facts

    if not relevant and non_junk:

        log.stage("filter_facts",

                  f"issue={issue}: no relevant facts, returning {len(non_junk)} non-junk facts")

        return non_junk[:5]



    log.stage("filter_facts",

              f"issue={issue}: {len(relevant)} relevant of {len(facts)} total")

    return relevant[:5]





def _build_deterministic_conclusion(issue: str, attack_vector: str) -> str:

    """Build the conclusion paragraph from a template — no LLM."""

    issue_label = issue.replace("_", " ")



    if attack_vector == "timing":

        return (

            f"Accordingly, the {issue_label} defect existed at the time of filing "

            f"and was not retroactively cured by any subsequent action. The opposing "

            f"party's filing remains procedurally defective and must be stricken."

        )



    if attack_vector == "application":

        return (

            f"Accordingly, the opposing party has failed to satisfy the rule's "

            f"mandatory requirement on the {issue_label} issue. The defect remains "

            f"in effect and the opposing party's position fails as a matter of law."

        )



    if attack_vector == "scope":

        return (

            f"Accordingly, the opposing party cannot rely on a discretionary rule to "

            f"manufacture an entitlement on the {issue_label} issue. Their position "

            f"is legally unsupported and must be rejected."

        )



    if attack_vector == "burden":

        return (

            f"Accordingly, the opposing party has not carried its burden of "

            f"demonstrating compliance on the {issue_label} issue. The procedural "

            f"defect remains and the opposing party's position fails."

        )



    if attack_vector == "authority":

        return (

            f"Accordingly, the opposing party's cited authority does not control "

            f"the {issue_label} issue. The defect remains and the opposing position fails."

        )



    if attack_vector == "procedure":

        return (

            f"Accordingly, the opposing party's chosen procedure cannot substitute "

            f"for compliance with the rule on the {issue_label} issue. The defect "

            f"remains and the opposing position fails."

        )



    return (

        f"Accordingly, the {issue_label} defect remains and the opposing position "

        f"fails as a matter of law."

    )







def _build_section_input(section: DraftSection, case_overview: str) -> str:

    """Build the user message for a single section. Always starts with case overview."""

    parts = [

        case_overview,

        "",

        "─────────────────────────────────────",

        "",

        f"# SECTION TO WRITE: {section.title}",

        f"# Type: {section.section_type}",

        "",

        f"## INSTRUCTIONS",

        section.instructions,

        "",

    ]



    inputs = section.inputs or {}



    if section.section_type == "preamble":

        parts.append("## REMEMBER:")

        parts.append("- Use the EXACT motion type from CASE OVERVIEW > 'Motion being addressed'")

        parts.append("- Use the EXACT party names from CASE OVERVIEW > PARTIES")

        parts.append("- Do NOT refer to any motion type that is not listed in CASE OVERVIEW")



    elif section.section_type == "rule_framework":

        parts.append("## CONTROLLING AUTHORITY (USE ONLY THIS)")

        auth = inputs.get("authority", "")

        if auth:

            parts.append(auth[:3000])

        else:

            parts.append("[NO AUTHORITY PROVIDED — write '[INSUFFICIENT DATA]']")



    elif section.section_type == "facts":

        parts.append("## FACTS TO INCLUDE (USE ONLY THESE)")

        facts_list = inputs.get("facts", [])

        if facts_list:

            for i, fact in enumerate(facts_list, 1):

                parts.append(f"{i}. {fact}")

            parts.append("")

            parts.append("Number each fact in your output. Use exact dates/amounts.")

            parts.append("Do NOT add facts not listed above.")

        else:

            parts.append("[NO FACTS PROVIDED — write '[INSUFFICIENT DATA]']")



    elif section.section_type == "argument":

        parts.append("## ARGUMENT DATA — USE THIS TO ATTACK")

        parts.append("")

        parts.append(f"### Issue: {inputs.get('issue', 'unknown')}")

        parts.append("")



        # ─── DEFINITIONS (locked, must be used verbatim) ──────────

        definitions_text = inputs.get("definitions_text", "")

        if definitions_text:

            parts.append("### 🧱 LOCKED DEFINITIONS (USE VERBATIM)")

            parts.append("These definitions come from controlling authority.")

            parts.append("You MUST include them in Paragraph 1 of your argument.")

            parts.append("Do NOT paraphrase. Do NOT skip them. Do NOT invent new ones.")

            parts.append("")

            parts.append(definitions_text)

            parts.append("")



        # ─── IF/THEN LOGIC CHAIN (deterministic, must be followed) ──

        logic_chain_text = inputs.get("logic_chain_text", "")

        if logic_chain_text:

            parts.append("### ⚖️ IF/THEN LOGIC CHAIN (DETERMINISTIC)")

            parts.append("This chain has been computed deterministically.")

            parts.append("You MUST include it verbatim as a block in Paragraph 3.")

            parts.append("Then narrate the conclusion in Paragraph 4.")

            parts.append("")

            parts.append(logic_chain_text)

            parts.append("")



        # Surface attack vector — tells the LLM HOW to attack

        attack_vector = inputs.get("attack_vector", "application")

        attack_summary = inputs.get("attack_summary", "")

        status = inputs.get("status", "violation")

        parts.append(f"### ⚔️ ATTACK VECTOR: {attack_vector.upper()}")

        if attack_summary:

            parts.append(f"### Attack summary: {attack_summary}")

        parts.append(f"### Status: {status} (violation = clear win, challenge = attack their interpretation)")

        parts.append("")



        parts.append("### Opposing party's position (state this then REBUT it):")

        their_pos = inputs.get("their_position", "")

        if their_pos:

            parts.append(their_pos)

        else:

            parts.append("(opposing party's specific position not extracted — "

                         "argue from the rule and facts directly)")



        parts.append("")

        parts.append("### Controlling rule:")

        rule = inputs.get("rule_text", "")

        if rule and rule != "(rule text not retrieved)":

            parts.append(rule)

        else:

            parts.append("(specific rule text not in DB cache — use the IF/THEN "

                         "chain above, which already encodes the rule's mandatory "

                         "requirement)")



        parts.append("")

        parts.append("## REQUIRED ARGUMENT STRUCTURE — FOLLOW EXACTLY")

        parts.append("")

        parts.append("Paragraph 1 — DEFINITIONS:")

        parts.append("  State the locked definitions verbatim. These are not optional.")

        parts.append("  This locks the meaning of the terms before any argument.")

        parts.append("")

        parts.append("Paragraph 2 — STATE THEIR POSITION:")

        parts.append("  Frame as: '[Opposing party] argues that [their position].'")

        parts.append("  Then immediately: 'This argument fails as a matter of law.'")

        parts.append("")

        parts.append("Paragraph 3 — IF/THEN LOGIC:")

        parts.append("  Quote the IF/THEN chain provided above as a block.")

        parts.append("  Do NOT rephrase the IF, AND, or THEN clauses.")

        parts.append("")

        parts.append("Paragraph 4 — APPLY TO THE FACTS:")

        parts.append("  State the specific facts from CASE OVERVIEW that trigger the chain.")

        parts.append("  Use exact dates, party names, and amounts.")

        parts.append("")

        parts.append("Paragraph 5 — DETERMINISTIC CONCLUSION:")

        parts.append("  Begin with 'Accordingly,' and state the conclusion in plaintiff's favor.")

        parts.append("  Use language like: 'is procedurally defective', 'must be stricken',")

        parts.append("  'fails as a matter of law'.")

        parts.append("")

        parts.append("ABSOLUTELY FORBIDDEN in this section:")

        parts.append("  - 'reserves the right to supplement'")

        parts.append("  - 'requires further citation'")

        parts.append("  - 'indeterminate'")

        parts.append("  - 'may', 'might', 'arguably', 'possibly'")

        parts.append("  - Any sentence that does not advocate for the client")

        parts.append("  - Skipping the definitions block")

        parts.append("  - Skipping the IF/THEN block")



    # ===PHASE_3F_STEP6D_RECONNECT_V1=== new section_type handlers

    elif section.section_type == "dispositive_anchor":

        parts.append("## DISPOSITIVE ORDER FACTS (TRANSLATE TO PROSE — DO NOT INVENT)")

        parts.append("")

        disp_text = inputs.get("dispositive_text", "")

        if disp_text:

            parts.append(disp_text)

        else:

            parts.append("[NO DISPOSITIVE ORDERS PROVIDED — write '[INSUFFICIENT DATA]']")

        parts.append("")

        parts.append("## RULES:")

        parts.append("- Translate the facts above into a clean legal narrative.")

        parts.append("- Use ONLY the dates, ECF numbers, and defendants listed.")

        parts.append("- Do NOT add facts. Do NOT make legal arguments here.")

        parts.append("- Do NOT use 'may', 'might', 'arguably', 'perhaps'.")

        parts.append("- Close with: 'Each order terminated Plaintiff\'s claims as set forth therein.'")



    # ===PHASE_3F_JUDICIAL_DUTY_V2=== judicial_duty_breach_input_formatter

    elif section.section_type == "judicial_duty_breach":

        parts.append("## COURT-AUTHORED DOCTRINAL DEFECTS - VERBATIM QUOTES TO USE")

        parts.append("")

        defects = inputs.get("judicial_duty_defects", []) or []

        if defects:

            for i, group in enumerate(defects, 1):

                date_str = group.get("date") or "(date unknown)"

                judge_str = group.get("judge") or "this Court"

                ecf_str = group.get("ecf") or ""

                src = group.get("source_doc") or "(source unknown)"

                parts.append(f"--- Defect Group {i}: {date_str}  |  {judge_str}  |  {ecf_str}  |  source: {src} ---")

                for j, d in enumerate(group.get("defects", []) or [], 1):

                    sev = (d.get("severity") or "?").upper()

                    vt = d.get("violation_type") or "?"

                    page = d.get("page")

                    lines = d.get("lines")

                    rule_citation = d.get("rule_citation") or ""

                    rule_text = d.get("rule_text") or ""

                    quote = (d.get("quote") or "").strip()

                    parts.append(f"  {j}. [{sev}] {vt}  (page {page}, lines {lines})")

                    if rule_citation:

                        parts.append(f"     RULE CITATION: {rule_citation}")

                    if rule_text:

                        parts.append(f"     RULE TEXT: {rule_text}")

                    if quote:

                        parts.append(f"     VERBATIM QUOTE FROM ORDER (USE THIS EXACTLY): \"{quote}\"")

                parts.append("")

        else:

            parts.append("[NO DEFECTS PROVIDED - write '[INSUFFICIENT DATA]']")

        parts.append("")

        parts.append("## RULES - STRICT (READ CAREFULLY):")

        parts.append("- For EACH defect group above, write ONE paragraph.")

        parts.append("- Each paragraph MUST contain the VERBATIM QUOTE FROM ORDER, character for character, in quotation marks.")

        parts.append("- Do NOT paraphrase the quote. Do NOT shorten the quote. Do NOT modify the quote.")

        parts.append("- Each paragraph MUST also state: (1) the source order date and ECF, (2) the rule citation that was misapplied, (3) one sentence on why the misapplication renders the order voidable under FRCP 60(b)(4).")

        parts.append("- Conclude with: \"These defects, individually and collectively, render the underlying judgment void under Rule 60(b)(4) and entitle Plaintiff to relief.\"")

        parts.append("- Do NOT invent additional defects. Do NOT cite cases not listed above.")

        parts.append("- Do NOT use hedging words: 'arguably', 'may', 'might', 'perhaps', 'possibly', 'indeterminate'.")

    # ===END PHASE_3F_JUDICIAL_DUTY_V2===

    elif section.section_type == "evidence_ignored":

        parts.append("## RECORD EVIDENCE THE DISPOSITIVE ORDERS FAILED TO ADDRESS")

        parts.append("")

        gaps = inputs.get("record_gaps", []) or []

        if gaps:

            for i, g in enumerate(gaps, 1):

                parts.append(f"  {i}. {g}")

        else:

            parts.append("[NO GAPS PROVIDED — write '[INSUFFICIENT DATA]']")

        parts.append("")

        parts.append("## RULES:")

        parts.append("- For each numbered item above, state in prose that the order(s) failed to address it.")

        parts.append("- Conclude that a ruling entered without considering record evidence deprives the party of meaningful opportunity to be heard under Mullane v. Central Hanover Bank, 339 U.S. 306 (1950).")

        parts.append("- Conclude that this constitutes a due process violation rendering the judgment void under FRCP 60(b)(4).")

        parts.append("- Do NOT invent any evidence not listed.")

    # ===END PHASE_3F_STEP6D_RECONNECT_V1===



    elif section.section_type == "question":

        parts.append("## QUESTION TO PRESENT")

        parts.append("Write a single 'Whether...' question based ONLY on the issues")

        parts.append("listed in CASE OVERVIEW > 'ISSUES TO ADDRESS'.")

        parts.append("Do NOT introduce any legal theory not in the issues list.")





    elif section.section_type == "parties":

        parts.append("## PARTIES TO IDENTIFY")

        parts.append(f"Plaintiff: {inputs.get('plaintiff', '')}")

        parts.append(f"Defendant: {inputs.get('defendant', '')}")

        parts.append("")

        parts.append("One paragraph per party.")



    elif section.section_type == "jurisdiction":

        parts.append("## COURT")

        parts.append(inputs.get("court", ""))



    elif section.section_type == "conclusion":

        parts.append("## CONCLUSION TYPE")

        parts.append(f"Doc type: {inputs.get('doc_type', '')}")

        parts.append("Keep it short. Reference the relief sought based on the")

        parts.append("issues in CASE OVERVIEW. Do NOT mention claims/issues that")

        parts.append("are not in CASE OVERVIEW.")



    parts.append("")

    parts.append("Now write the section text only. No preamble. No section title.")

    return "\n".join(parts)





def _template_only_fallback(plan, doc_type: str) -> dict:

    rendered = {}

    for section in plan:

        rendered[section.name] = (

            f"[Section to be drafted: {section.name}]\n"

            f"Instructions: {section.instructions[:200]}"

        )



    return {

        "text": merge_sections(rendered, plan),

        "sections_completed": 0,

        "sections_failed": len(plan),

        "sections_total": len(plan),

        "total_time_ms": 0,

        "plan": [s.to_dict() for s in plan],

        "error": "LLM offline — template-only fallback used",

    }

