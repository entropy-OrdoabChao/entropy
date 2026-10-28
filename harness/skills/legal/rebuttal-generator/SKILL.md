---
name: rebuttal-generator
description: Take defense arguments from defense-simulator and defeat them using higher authority, proven elements, and logic chain. Produces preemptive rebuttals to include in filings. Use after defense-simulator identifies MODERATE or STRONG attacks.
version: 1.0.0
metadata:
  hermes:
    tags: [legal, rebuttal, counter-argument, defense, adversarial, strengthen]
    requires_toolsets: [terminal]
---

# Rebuttal Generator — Counter-Argument Builder

Takes defense arguments identified by `defense-simulator` and systematically defeats them using controlling authority, proven facts, and the established logic chain.

**Every MODERATE and STRONG defense attack must have a rebuttal before filing.**

## When to Use

- Immediately after `defense-simulator` identifies attacks rated MODERATE or STRONG
- When preparing for opposition's response
- When strengthening a filing before submission
- When opponent has already raised these arguments

## How It Works

```
Defense attack (from defense-simulator)
    ↓
Identify which element/authority/definition is targeted
    ↓
Search for higher authority that defeats the attack
    ↓
Build counter-argument using proven chain
    ↓
Output: ready-to-insert rebuttal paragraph
```

## Rebuttal Methods (in priority order)

### Method 1: Higher Authority Controls
```
DEFENSE: "Definition of person includes corporations under state law"
REBUTTAL: Federal statute controls in this action. 15 U.S.C. § 1692a(6) defines
"debt collector" exclusively for FDCPA purposes. State law definitions do not
override federal statutory definitions within federal claims. [cite hierarchy]
```

### Method 2: Element Already Proven
```
DEFENSE: "Plaintiff had insurable interest through beneficial use"
REBUTTAL: The record establishes vehicle was sold on [date] (Exhibit A — bill of
sale). Ownership terminated upon transfer of title. Beneficial use ceased upon
sale. Element tested and proven FALSE in analysis. [cite record]
```

### Method 3: Definition Misapplied
```
DEFENSE: "Insurable interest is broader than ownership"
REBUTTAL: Under [controlling statute], insurable interest requires ownership or
financial interest in the insured property. Defendant cites no authority for
extending the definition beyond the statutory scope. The statutory definition
controls within this action. [cite definition-lookup result]
```

### Method 4: Procedural Bar
```
DEFENSE: "Claim is time-barred"
REBUTTAL: The applicable statute of limitations is [X years] under [citation].
The cause of action accrued on [date]. This filing is within the limitations
period. Defense argument fails on the merits.
```

### Method 5: Factual Record Defeats
```
DEFENSE: "Refund was adequate"
REBUTTAL: The record shows [total premiums collected] from [date] to [date].
Defendant's refund of $158 represents [X%] of the amount owed. A partial
refund does not satisfy the duty to return ALL premiums collected without
legal basis. [cite mandatory duty language]
```

### Method 6: Chain Logic Unbroken
```
DEFENSE: "Even without insurable interest, policy cancellation required notice"
REBUTTAL: The absence of insurable interest renders the policy void — not
voidable. A void instrument cannot create obligations, including notice
requirements. The defense confuses voidable (requiring action to cancel)
with void (never valid). [cite void vs voidable distinction]
```

## Output Format

```
REBUTTAL ANALYSIS — [Case/Issue]

DEFENSE ARGUMENT 1: [statement]
  STRENGTH: [from defense-simulator]
  REBUTTAL METHOD: [Higher Authority / Element Proven / Definition / Procedural / Factual / Chain Logic]
  REBUTTAL:
    [Full counter-argument with citations]
  RESULT: DEFEATED / PARTIALLY ADDRESSED / UNRESOLVED

DEFENSE ARGUMENT 2: [statement]
  ...

SUMMARY:
  Total defense arguments: [N]
  Defeated: [N]
  Partially addressed: [N]
  Unresolved: [N] — ⚠️ THESE MUST BE FIXED BEFORE FILING

DRAFT-READY REBUTTAL PARAGRAPHS:
  [Ready-to-insert paragraphs for legal-draft]
```

## Decision Rules

```
IF all defenses DEFEATED:
  → proceed to draft with preemptive rebuttals included

IF any defense PARTIALLY ADDRESSED:
  → include rebuttal but flag for user review
  → note the weakness in the draft

IF any defense UNRESOLVED:
  → STOP
  → advise user: "This argument cannot be fully rebutted with current authority"
  → suggest: additional research, factual development, or claim modification
  → DO NOT file with unresolved strong defense arguments
```

## Integration with Draft

The rebuttal paragraphs are inserted into `legal-draft` in Section III (Application to Facts) as preemptive counter-arguments:

```
III. APPLICATION TO FACTS

[Element analysis]

Defendant may argue [defense]. This argument fails because [rebuttal].
[Citation to controlling authority].
```

This prevents the opposition from raising arguments you haven't already addressed.

## Integration in Pipeline

```
legal-scorecard (Step 11)
    ↓
defense-simulator (Step 11.5) — identifies attacks
    ↓
rebuttal-generator (Step 11.6) — defeats attacks  ← YOU ARE HERE
    ↓
baseline-memorandum (Step 12)
    ↓
legal-draft (Step 13) — includes preemptive rebuttals
```

## 7. JUDICIAL IMMUNITY & CONSTITUTIONAL VIOLATION REBUTTALS (NEW - MAY 23, 2026)

### 7.1 Qualified Immunity Rebuttals

**Defense Argument:** "Judge is entitled to absolute judicial immunity"

**Rebuttal Method 1: Qualified Immunity Standard**
```
REBUTTAL: Judicial immunity is qualified, not absolute. Under Stump v. Sparkman,
judges are immune only for acts taken within their judicial capacity and in
judicial discretion. This immunity does not extend to acts that violate
established constitutional rights or exceed judicial authority. [cite Stump,
Forrester v. White]
```

**Rebuttal Method 2: Clear Violation Exception**
```
REBUTTAL: Qualified immunity does not protect violations of clearly established
constitutional rights. The 14th Amendment Due Process right to a fair trial
was clearly established under Mathews v. Eldridge. The judge's [specific
action] violated this right by [violation details]. [cite 14th Amendment,
Mathews v. Eldridge]
```

**Rebuttal Method 3: Exceeding Judicial Authority**
```
REBUTTAL: The judge exceeded judicial authority by acting as an appellate
court (trial courts may not present themselves as appellate authority).
This is a clear violation of procedural due process. Judicial immunity does
not protect acts beyond the judge's jurisdiction. [cite relevant precedent]
```

### 7.2 14th Amendment Due Process Rebuttals

**Defense Argument:** "Procedural errors were harmless"

**Rebuttal Method 1: Structural Violation**
```
REBUTTAL: The error was structural, not harmless. Under Mathews v. Eldridge,
procedural due process violations that affect the fundamental fairness of
the proceeding cannot be treated as harmless. The judge's [specific error]
undermined the entire adjudication. [cite Mathews v. Eldridge]
```

**Rebuttal Method 2: Clear Record of Violation**
```
REBUTTAL: The record demonstrates a clear pattern of [violation type]. The
judge denied leave to amend without element-by-element analysis, dismissed
the complaint with conclusory language, and used punitive language against
the pro se plaintiff. These are not harmless errors but intentional
discriminatory actions. [cite record evidence]
```

**Rebuttal Method 3: Substantial Injustice Standard**
```
REBUTTAL: Under Caperton v. A.T. Massey Coal Co., judicial bias that creates
a reasonable appearance of partiality violates due process. The judge's
language was [cited excerpt], demonstrating [bias pattern]. This creates a
substantial likelihood of unjust outcome. [cite Caperton v. A.T. Massey]
```

### 7.3 Equal Protection Rebuttals

**Defense Argument:** "Pro se status alone justifies additional scrutiny"

**Rebuttal Method 1: Discriminatory Application**
```
REBUTTAL: While pro se plaintiffs receive liberal construction of pleadings,
this does not justify unequal treatment. Under Alexander v. Sandoval,
procedural accommodations must be applied uniformly. The judge applied
stricter scrutiny to this pro se plaintiff than to represented litigants
in similar circumstances. This disparate treatment violates equal protection.
[cite Alexander v. Sandoval]
```

**Rebuttal Method 2: No Statutory Basis**
```
REBUTTAL: There is no statutory or rule basis for denying leave to amend
based solely on pro se status. Pa.R.C.P. 235 and FRCP 15(a) require
element-by-element analysis, not discretionary dismissal. The judge's
action had no legal basis and constituted arbitrary discrimination.
[cite Pa.R.C.P. 235, FRCP 15(a)]
```

### 7.4 28 U.S.C. § 1983 Rebuttals

**Defense Argument:** "Judge cannot be sued under § 1983"

**Rebuttal Method 1: Qualified Immunity Exception**
```
REBUTTAL: While judges are generally immune from § 1983 suits, immunity is
qualified, not absolute. Under 28 U.S.C. § 1983, qualified immunity protects
only violations of law that were not clearly established. The constitutional
rights at issue here were clearly established under controlling precedent.
[cite 28 U.S.C. § 1983, Stump v. Sparkman]
```

**Rebuttal Method 2: Civil Rights Remedy**
```
REBUTTAL: Section 1983 provides the exclusive civil remedy for constitutional
violations by government actors, including judges acting in their official
capacity. The plaintiff seeks only declaratory and injunctive relief, not
damages against the judge personally. [cite 42 U.S.C. § 1983, § 1988]
```

### 7.5 Recusal Motion Rebuttals

**Defense Argument:** "No showing of actual bias"

**Rebuttal Method 1: Apparent Bias Standard**
```
REBUTTAL: Under 28 U.S.C. § 455, recusal is required when a reasonable person
would question the judge's impartiality. The judge's language [cited] and
procedural actions [cited] create a reasonable appearance of bias. The
standard is not actual bias but appearance of bias. [cite 28 U.S.C. § 455,
Caperton v. A.T. Massey]
```

**Rebuttal Method 2: Presumption of Fairness Rebutted**
```
REBUTTAL: While courts presume judicial fairness, this presumption is rebutted
by clear evidence of bias or discriminatory treatment. The judge's
conclusory dismissal, denial of leave to amend without proper analysis,
and punitive language against the plaintiff rebut this presumption.
[cite relevant precedent]
```

### 7.6 Evidence Requirements for Rebuttals

| Claim Type | Evidence Needed |
|------------|-----------------|
| 14th Amendment Due Process | Transcript excerpts, dismissal orders, docket history showing delay |
| 14th Amendment Equal Protection | Comparative treatment evidence, pro se status documentation |
| Qualified Immunity Exception | Clear violation of established law, precedent showing similar conduct unlawful |
| Recusal | Transcript excerpts showing bias, punitive language patterns |

**Related Reference:** `references/judicial-immunity-analysis-20260523.md`

## Anti-Hallucination

- Every rebuttal MUST cite authority from the database
- Do not fabricate counter-arguments without legal basis
- If no rebuttal exists for a strong defense → mark UNRESOLVED, do not fake a counter
- The strength of a rebuttal is only as strong as the authority behind it
