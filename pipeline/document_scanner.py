"""
document_scanner.py — Read PDFs, DOCX, and TXT from real source folders.

This is the input layer for Beezle Path 2. Instead of scanning
<home>\\cases\\ (a derivative folder produced by a broken earlier
extractor), this module reads directly from the user's actual source
folders like C:\\Users\\malic\\Documents\\pdf\\misc\\.

For each document it:
  1. Extracts text (pdfplumber for .pdf, python-docx for .docx, direct read
     for .txt)
  2. Caches the extracted text by file SHA1 so we don't re-extract on
     subsequent runs
  3. Detects case docket numbers from the content
  4. Detects parties, author, document type
  5. Emits a Document dataclass

Incremental: only re-processes files whose (path, mtime, size) tuple has
changed since last run. A run manifest is written to text_cache/_manifest.json.

Output is a list of Document records consumed by case_assembler.py.
"""

from __future__ import annotations
from core import identity as _ident
from pathlib import Path
from core.entropy_paths import entropy_root as _eroot, package_root as _epkg, hermes_home as _ehermes
import hashlib
import json
import logging
import re
from dataclasses import dataclass, field, asdict
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple, Iterable

log = logging.getLogger("beezle.scanner")


# =============================================================================
# CONSTANTS AND PATTERNS
# =============================================================================

# Accepted file extensions — lowercase
ACCEPTED_EXTENSIONS = {".pdf", ".docx", ".txt"}

# Filer gate — file must mention Filer or Filer to be a candidate for
# case assignment. Reference material (treatises, statutes) passes a
# separate gate (below).
FILER_GATE = re.compile(
    r"\b(?:Filer|Filer[-\s]?Fardy|Fardy\s*Babah|Filer\b[^@])",
    re.IGNORECASE,
)

# Reference material markers — these are legal texts/treatises/statutes
# that belong in the reference index, NOT in a specific case.
REFERENCE_MARKERS = [
    re.compile(r"\bA\s+Treatise\s+on\b", re.I),
    re.compile(r"\bTreatise\s+on\b", re.I),
    re.compile(r"\bUnderstanding\s+Federal\s+Legislation\b", re.I),
    re.compile(r"\bLaw\s+Merchant\b", re.I),
    re.compile(r"\bEn[-\s]Law\b", re.I),
    re.compile(r"\bBlack'?s\s+Law\s+Dictionary\b", re.I),
    re.compile(r"\bCorpus\s+Juris\s+Secundum\b", re.I),
    re.compile(r"\bRestatement\s+\(?(?:Second|Third)\)?\s+of\b", re.I),
    re.compile(r"\bAm\.?\s*Jur\.?\b", re.I),
]

# Personal-life markers — drop these entirely. Not litigation, not reference.
PERSONAL_LIFE_MARKERS = [
    re.compile(r"\bRental\s+Application\b", re.I),
    re.compile(r"\bLease\s+Agreement\b(?!.*Plaintiff)", re.I),
    re.compile(r"\bBenefit\s+Summary\b", re.I),
    re.compile(r"\bCigna\b.*\bPPO\b", re.I),
    re.compile(r"\bCardholder\s+Agreement\b", re.I),
    re.compile(r"\bMeeting[Gg]ate\b", re.I),
    re.compile(r"Brave[\s_]Passwords", re.I),
]

# Document type markers — scanned against filename AND first 2KB of content
# ORDER matters: order/court_notice detection first (strongest signal), then
# adversary doctypes, then plaintiff doctypes.
DOCTYPE_MARKERS = [
    # Orders (judicial) — these are the strongest identifiers and must come first
    ("order", [
        re.compile(r"\bMEMORANDUM\s+(?:OPINION\s+)?AND\s+ORDER\b", re.I),
        re.compile(r"\bMEMORANDUM\s+OPINION\b", re.I),
        re.compile(r"\bORDER\s+(?:ON|GRANTING|DENYING|DISMISSING)\b", re.I),
        re.compile(r"\bNotice\s+of\s+order\b", re.I),
        re.compile(r"\bIT\s+IS\s+HEREBY\s+ORDERED\b", re.I),
        re.compile(r"\bAND\s+NOW,?\s+this\s+\d+\w*\s+day\s+of\b", re.I),
        re.compile(r"\bBY\s+THE\s+COURT\s*[:\s]*$", re.I | re.MULTILINE),
        re.compile(r"\bJUDGMENT\s+(?:is|shall be|IS)\s+ENTERED\b", re.I),
    ]),
    # Court/PACER notifications (before MTD — these contain "Motion" text in subject lines)
    ("court_notice", [
        re.compile(r"\bGmail\s*-\s*(?:Activity|Notice\s+of\s+order|Rejection\s+of\s+your\s+E-Filing)", re.I),
        re.compile(r"\bNotice\s+of\s+Electronic\s+Filing\b", re.I),
        re.compile(r"\bCM/?ECF\b"),
        re.compile(r"\bautomatic\s+e-mail\s+message\s+generated\s+by\s+the\s+CM/?ECF\b", re.I),
    ]),
    # Opposition / response to motion (adversary) — must come before MTD to catch
    # oppositions that mention "motion to dismiss" in their title
    ("opposition", [
        re.compile(r"\bMemorandum\s+of\s+Law\s+in\s+Opposition\b", re.I),
        re.compile(r"\bResponse\s+in\s+Opposition\b", re.I),
        re.compile(r"\bin\s+Opposition\s+to\s+(?:Plaintiff|Defendant|Motion)", re.I),
        re.compile(r"\bOpposition\s+to\s+(?:Motion|Defendant|Plaintiff)\b", re.I),
        re.compile(r"\bReply\s+(?:Brief|Memorandum)\s+in\s+Support\b", re.I),
        re.compile(r"\bSur[-\s]?[Rr]eply\b"),
        re.compile(r"\bAnswer\s+to\s+Motion\b", re.I),
        re.compile(r"\bRebuttal\s+(?:to|Brief)\b", re.I),
    ]),
    # Motions to dismiss (adversary)
    ("motion_to_dismiss", [
        re.compile(r"\bDefendants?'?\s+Motion\s+to\s+(?:Dismiss|Strike)\b", re.I),
        re.compile(r"\bMOTION\s+TO\s+DISMISS\b(?!\s+is\s+GRANTED|\s+is\s+DENIED)"),
        re.compile(r"\bMotion\s+to\s+Strike\s+(?:Plaintiff|Complaint)\b", re.I),
    ]),
    # Petition (plaintiff) — before "motion" because petitions often mention motions
    ("petition", [
        re.compile(r"\bPETITION\s+TO\s+(?:OPEN|STRIKE|VACATE)\b"),
        re.compile(r"\bPetition\s+to\s+(?:Open|Strike|Vacate)\s+(?:Default|Judgment)\b", re.I),
        re.compile(r"\bPetition\s+for\s+(?:Review|Writ|Reconsideration)\b", re.I),
    ]),
    # Complaint (plaintiff)
    ("complaint", [
        re.compile(r"\bCOMPLAINT\b(?!\s+Submitted)"),
        re.compile(r"\bAmended\s+Complaint\b", re.I),
        re.compile(r"\bSecond\s+Amended\s+Complaint\b", re.I),
        re.compile(r"\bPlaintiff'?s?\s+Complaint\b", re.I),
    ]),
    # Motion (non-MTD, plaintiff-filed)
    ("motion", [
        re.compile(r"\bPlaintiff'?s?\s+Motion\s+to\b", re.I),
        re.compile(r"\bMotion\s+to\s+Compel\b", re.I),
        re.compile(r"\bMotion\s+for\s+(?:Summary\s+Judgment|Reconsideration|Leave\s+to|Default\s+Judgment)\b", re.I),
        re.compile(r"\bMotion\s+for\s+Judicial\s+Notice\b", re.I),
    ]),
    # Memorandum of law (usually plaintiff briefing)
    ("memorandum", [
        re.compile(r"\bMEMORANDUM\s+OF\s+LAW\b(?!\s+IN\s+OPPOSITION)", re.I),
        re.compile(r"\bSupplemental\s+Memorandum\s+of\s+Law\b", re.I),
        re.compile(r"\bBrief\s+in\s+Support\s+of\b", re.I),
    ]),
    # Judicial notice request (plaintiff)
    ("judicial_notice", [
        re.compile(r"\bJudicial\s+Notice\b", re.I),
        re.compile(r"\bRequest\s+for\s+Judicial\s+Notice\b", re.I),
    ]),
    # Exhibit
    ("exhibit", [
        re.compile(r"\bExhibit\s+[A-Z0-9]\b"),
        re.compile(r"\bEx\.\s*[A-Z0-9]\b"),
    ]),
    # Certificate of service
    ("service", [
        re.compile(r"\bCertificate\s+of\s+Service\b", re.I),
        re.compile(r"\bProof\s+of\s+Service\b", re.I),
        re.compile(r"\bService\s+Instructions\b", re.I),
    ]),
    # E-filing rejection notice
    ("efile_rejection", [
        re.compile(r"\bRejection\s+of\s+your\s+E-Filing\b", re.I),
    ]),
]

# Author detection — who wrote this document?
AUTHOR_MARKERS = {
    _ident.party_key(): [
        re.compile(r"\bCOMES\s+NOW\s+Plaintiff\b.*Filer", re.I | re.DOTALL),
        re.compile(r"\bPlaintiff\s+Filer", re.I),
        re.compile(r"\b" + re.escape(_ident.last() or _ident.name()) + r",?\s+Plaintiff\s+(?:pro\s+se|sui\s+juris)\b", re.I),
        re.compile(r"\b" + re.escape(_ident.name()) + r",?\s+Plaintiff\b", re.I),
        re.compile(r"\bPlaintiff,?\s+pro\s+se,?\s+(?:hereby|respectfully|moves|submits)", re.I),
        re.compile(r"\bRespectfully\s+submitted.*(?:\/s\/\s*)?Filer", re.I | re.DOTALL),
        re.compile(r"\bWHEREFORE,?\s+Plaintiff\s+(?:respectfully\s+)?(?:requests|prays|moves)", re.I),
    ],
    "adversary": [
        re.compile(r"\bCOMES\s+NOW\s+Defendant\b", re.I),
        re.compile(r"\bDefendants?'?\s+Motion\b", re.I),
        re.compile(r"\bMemorandum\s+of\s+Law\s+in\s+Opposition.*Defendant", re.I | re.DOTALL),
        re.compile(r"\bNorthern\s+Trust.*respectfully\s+requests", re.I | re.DOTALL),
        re.compile(r"\bDefendant,?\s+by\s+and\s+through\s+(?:its|their)\s+(?:undersigned\s+)?counsel", re.I),
        re.compile(r"\b/s/\s*(?!Filer|Filer)[A-Z][a-z]+\s+[A-Z][a-z]+,?\s+Esq", re.I),
    ],
    "court": [
        re.compile(r"\bBY\s+THE\s+COURT[:\s]", re.I),
        re.compile(r"\bJudge\s+(?:the-judge|Younge)", re.I),
        re.compile(r"^\s*(?:MEMORANDUM|OPINION|ORDER)\s*$", re.I | re.MULTILINE),
        re.compile(r"\bFor\s+the\s+reasons\s+(?:set\s+forth|stated)\s+(?:above|below|herein)", re.I),
        re.compile(r"\bIt\s+is\s+(?:hereby\s+)?ORDERED", re.I),
    ],
}


# =============================================================================
# DOCUMENT DATACLASS
# =============================================================================

@dataclass
class Document:
    """One scanned document."""
    path: str                          # Absolute path on disk
    filename: str
    extension: str
    size_bytes: int
    mtime: float
    sha1: str

    # Extracted text (cached; not serialized to case_map to save space)
    text_chars: int = 0
    text_path: Optional[str] = None    # Path to cached text file

    # Detected metadata
    doc_type: str = "unknown"          # order/mtd/opposition/complaint/etc
    author: str = "unknown"            # filer/adversary/court/unknown
    dockets: List[str] = field(default_factory=list)  # Detected docket numbers
    parties: List[str] = field(default_factory=list)  # Defendant tags
    case_court: str = ""               # Best-guess court classification
    is_reference: bool = False         # Treatise / statute / scholarly
    is_personal_life: bool = False     # Rental, medical, personal finance
    is_filer_case: bool = False       # filer (runtime identity) mentioned

    def to_dict(self) -> dict:
        return asdict(self)


# =============================================================================
# TEXT EXTRACTION
# =============================================================================

def extract_text_pdf(path: Path) -> str:
    """Extract text from PDF using pdfplumber. Returns empty string on failure."""
    try:
        import pdfplumber
    except ImportError:
        log.error("pdfplumber not installed. pip install pdfplumber")
        return ""

    try:
        text_parts = []
        with pdfplumber.open(path) as pdf:
            for page in pdf.pages:
                try:
                    page_text = page.extract_text() or ""
                    text_parts.append(page_text)
                except Exception as e:
                    log.debug(f"PDF page extract failed in {path.name}: {e}")
                    continue
        return "\n".join(text_parts)
    except Exception as e:
        log.debug(f"PDF extract failed for {path.name}: {e}")
        return ""


def extract_text_docx(path: Path) -> str:
    """Extract text from DOCX using python-docx."""
    try:
        from docx import Document as DocxDocument
    except ImportError:
        log.error("python-docx not installed. pip install python-docx")
        return ""

    try:
        doc = DocxDocument(str(path))
        parts = []
        for para in doc.paragraphs:
            if para.text.strip():
                parts.append(para.text)
        # Also pull table text
        for table in doc.tables:
            for row in table.rows:
                for cell in row.cells:
                    if cell.text.strip():
                        parts.append(cell.text)
        return "\n".join(parts)
    except Exception as e:
        log.debug(f"DOCX extract failed for {path.name}: {e}")
        return ""


def extract_text_txt(path: Path) -> str:
    """Read TXT with permissive encoding."""
    for enc in ("utf-8", "cp1252", "latin-1"):
        try:
            return path.read_text(encoding=enc, errors="ignore")
        except Exception:
            continue
    return ""


def extract_text(path: Path) -> str:
    """Extract text from any supported document type."""
    ext = path.suffix.lower()
    if ext == ".pdf":
        return extract_text_pdf(path)
    if ext == ".docx":
        return extract_text_docx(path)
    if ext == ".txt":
        return extract_text_txt(path)
    return ""


# =============================================================================
# HASHING / CACHING
# =============================================================================

def file_sha1(path: Path, chunk_size: int = 1 << 16) -> str:
    """Compute SHA1 of file contents."""
    h = hashlib.sha1()
    try:
        with path.open("rb") as f:
            while True:
                block = f.read(chunk_size)
                if not block:
                    break
                h.update(block)
    except Exception as e:
        log.debug(f"Hash failed for {path}: {e}")
        return ""
    return h.hexdigest()


# =============================================================================
# METADATA DETECTION
# =============================================================================

def detect_doc_type(filename: str, text_sample: str) -> str:
    """Pick the best-matching document type from filename + first 2KB of text."""
    lower_name = filename.lower()
    sample = (text_sample or "")[:3000]
    sample_lower = sample.lower()

    # Exclude obvious non-filings
    if lower_name.endswith(".json"):
        return "manifest"

    # ========================================================================
    # FJD RANDOM-FILENAME BODY DETECTION (highest priority for image PDFs
    # downloaded from Philadelphia eFile system that have meaningless names
    # like 'jb5CH9NcF.pdf'). These get classified by what's in the body.
    # ========================================================================
    is_fjd_random = bool(re.match(r"^[a-zA-Z0-9]{8,12}\.pdf$", filename))

    # FJD cover sheet — non-substantive, route to filings
    if "PETITION/MOTION COVER SHEET" in sample.upper():
        return "motion_cover_sheet"

    # Court order body markers (catch FJD orders by content)
    if re.search(r"\bAND NOW,?\s*this\b", sample, re.I) or \
       re.search(r"\bIT IS\s+(?:HEREBY\s+)?ORDERED\b", sample, re.I) or \
       re.search(r"\bBY\s+THE\s+COURT\b\s*[,:.]?", sample, re.I):
        if "Plaintiff" in sample[:1500] or "Defendant" in sample[:1500]:
            return "order"

    # Body-text "MOTION TO STRIKE" detection (for FJD random-name briefs)
    if re.search(r"\bMOTION\s+TO\s+STRIKE\b", sample[:2000], re.I):
        if re.search(r"\bAttorneys?\s+for\s+(?:Defendant|Garnishee|Respondent)", sample, re.I) or \
           re.search(r"\bATTORNEY\s+I\.?D\.?\b", sample.upper()[:2000]):
            return "motion_to_strike"
        return "motion_to_strike"

    # Body-text MTD detection
    if re.search(r"\bMOTION\s+TO\s+DISMISS\b", sample[:2000], re.I):
        if re.search(r"\bAttorneys?\s+for\s+(?:Defendant|Respondent)", sample, re.I):
            return "motion_to_dismiss"
        return "motion_to_dismiss"

    # Body-text Memorandum-in-Support detection (defense briefs)
    if re.search(r"\bMEMORANDUM\s+(?:OF\s+LAW\s+)?IN\s+SUPPORT\b", sample[:2000], re.I):
        if re.search(r"\bAttorneys?\s+for\s+(?:Defendant|Garnishee|Respondent)", sample, re.I):
            return "memorandum"  # adversary memo - author detector tags it
        return "memorandum"

    # Filename-first heuristics (cheap)
    if "proposed order" in lower_name:
        return "proposed_order"
    if "certificate of service" in lower_name or "proof of service" in lower_name:
        return "service"
    if "gmail - activity in case" in lower_name or "gmail - notice of" in lower_name:
        return "court_notice"
    if "gmail - rejection" in lower_name:
        return "efile_rejection"

    # NEW: casual filename heuristics for the most common patterns
    # Order matters: more specific first
    if "binding precedent" in lower_name or "authority pack" in lower_name:
        return "memorandum"
    if "supplemental memorandum" in lower_name or "supplemental memo" in lower_name:
        return "memorandum"
    if "judicial notice" in lower_name:
        return "judicial_notice"
    if "petition to open" in lower_name or "petition for" in lower_name:
        return "petition"
    if "amended complaint" in lower_name or "amended_complaint" in lower_name:
        return "complaint"
    if lower_name.startswith("complaint") or "complaint.pdf" in lower_name or "complaint.docx" in lower_name:
        return "complaint"
    if "motion to strike" in lower_name or "motion_to_strike" in lower_name:
        return "motion_to_strike"
    if "motion to dismiss" in lower_name or "motion_to_dismiss" in lower_name or "mtd" in lower_name:
        return "motion_to_dismiss"
    if "motion to compel" in lower_name or "motion_to_compel" in lower_name:
        return "motion"
    if "motion for" in lower_name or "motion_for" in lower_name:
        return "motion"
    if "opposition" in lower_name or "oppose" in lower_name or "opp_" in lower_name:
        return "opposition"
    if "reply" in lower_name and "replyy" not in lower_name[:10]:
        return "opposition"
    if "replyy" in lower_name:  # Catches typos like "avant cfpb replyy.pdf"
        return "opposition"
    if "sur-reply" in lower_name or "surreply" in lower_name or "sur reply" in lower_name:
        return "opposition"
    if "rebuttal" in lower_name:
        return "opposition"
    if "answer to" in lower_name or "answer_to" in lower_name:
        return "opposition"
    if "memorandum of law" in lower_name or "memo of law" in lower_name:
        return "memorandum"
    if "memorandum" in lower_name and "court" not in lower_name and "opinion" not in lower_name:
        return "memorandum"
    if lower_name.startswith("ex ") or lower_name.startswith("exhibit") or "_exhibit_" in lower_name:
        return "exhibit"
    if "affidavit" in lower_name:
        return "memorandum"  # Affidavits are sworn supporting documents
    if "brief" in lower_name and "support" in lower_name:
        return "memorandum"

    # Content-based matching — first match wins, order matters (orders before motions)
    for doctype, patterns in DOCTYPE_MARKERS:
        for p in patterns:
            if p.search(sample) or p.search(filename):
                return doctype

    return "unknown"


def detect_author(text_sample: str, doc_type: str, filename: str = "") -> str:
    """Detect who authored the document.

    NEGATIVE-SIGNAL-FIRST: explicit adversary signals (defense counsel signature
    blocks, attorney bar IDs, defense firm names) are checked BEFORE any
    positive Filer signal. This prevents the bug where every adversary brief
    was tagged as 'filer' because the case caption contains 'FILER-FARDY
    FILER' in the body.
    """
    sample = (text_sample or "")[:5000]
    sample_lower = sample.lower()
    fn_lower = (filename or "").lower()

    # ========================================================================
    # TIER 1 — Court orders (always 'court')
    # ========================================================================
    if doc_type in ("order", "court_notice", "efile_rejection"):
        return "court"

    # Court order body markers (catch random-filename FJD orders)
    if re.search(r"\bIT IS (?:HEREBY )?ORDERED\b", sample, re.I):
        if re.search(r"\bBY THE COURT\b", sample, re.I) or re.search(r"\bAND NOW,\s*this\b", sample, re.I):
            return "court"

    # ========================================================================
    # TIER 2 — Explicit adversary signals (HIGHEST PRIORITY)
    # These run BEFORE any Filer-positive signal because every adversary
    # brief contains "FILER NAME" in the case caption.
    # ========================================================================

    # 2a — Defense firms (confirmed in your corpus + common Philly defense firms)
    DEFENSE_FIRMS = [
        # Confirmed in your Lippi corpus (State Farm motions to strike)
        r"forry\s+ullman",
        r"gary\s+a\.?\s+opposing counsel",
        # Common Philly / EDPA / federal defense firms
        r"ballard\s+spahr", r"reed\s+smith", r"morgan,?\s+lewis",
        r"troutman\s+pepper", r"troutman\s+sanders",
        r"klehr\s+harrison", r"marshall\s+dennehey",
        r"wilson\s+elser", r"cipriani\s+(?:&\s+)?werner",
        r"cozen\s+o'connor", r"cozen", r"blank\s+rome",
        r"duane\s+morris", r"eckert\s+seamans",
        r"post\s+&\s+schell", r"post\s+and\s+schell",
        r"gordon\s+(?:&\s+)?rees", r"goldberg\s+segalla",
        r"mcelroy\s+deutsch", r"mcelroy,?\s+deutsch",
        r"jones\s+day", r"dechert", r"kirkland\s+&\s+ellis",
        r"sidley\s+austin", r"hogan\s+lovells", r"k&l\s+gates",
        r"saul\s+ewing", r"stradley\s+ronon", r"fox\s+rothschild",
        r"dilworth\s+paxson", r"obermayer\s+rebmann",
        r"buchanan\s+ingersoll", r"stevens\s+&\s+lee",
        r"weber\s+gallagher", r"rawle\s+&\s+henderson",
    ]
    for pat in DEFENSE_FIRMS:
        if re.search(pat, sample_lower):
            return "adversary"

    # 2b — Attorney bar ID (Filer is pro se with NO PA Bar number)
    # PA Bar IDs are typically 4-6 digits. Filer's FJD pro se filer ID is P75736
    # which is alphanumeric and won't match this pattern.
    if re.search(r"\battorney\s+i\.?\s*d\.?\s*no\.?\s*\d{4,6}\b", sample_lower):
        return "adversary"
    if re.search(r"\bpa\s*(?:bar|attorney|i\.?d\.?)\s*(?:no\.?|#)\s*\d{4,6}\b", sample_lower):
        return "adversary"
    # ", ESQUIRE" or ", ESQ." in the first 1000 chars (signature block area)
    if re.search(r",\s*esquire\b|,\s*esq\.?\b", sample_lower[:1500]):
        # Make sure it's not just a citation referencing an attorney by name
        # Check if it looks like a signature block (followed by address pattern)
        if re.search(r"esquire.{0,200}(?:suite|p\.o\.\s*box|attorneys?\s+for|tel|phone|\(\d{3}\))",
                     sample_lower[:2000], re.S):
            return "adversary"

    # 2c — Counsel-for / Attorneys-for blocks
    counsel_patterns = [
        r"attorneys?\s+for\s+(?:defendants?|respondents?|garnishees?|movant)",
        r"counsel\s+for\s+(?:defendants?|respondents?|garnishees?|movant)",
        r"on\s+behalf\s+of\s+(?:defendants?|respondents?|garnishees?)",
        r"for\s+(?:the\s+)?(?:defendants?|garnishees?)\b.{0,100}\bsubmits?\b",
    ]
    for pat in counsel_patterns:
        if re.search(pat, sample_lower):
            return "adversary"

    # ========================================================================
    # TIER 3 — Incoming mail (NOT a court filing at all)
    # These should ideally be dropped by detect_personal_life upstream, but
    # if they slip through, don't tag them as Filer-authored.
    # ========================================================================
    incoming_mail_patterns = [
        r"^\s*(?:dear\s+(?:mr\.?|ms\.?|valued))",
        r"re:\s*account\s+(?:ending|number)",
        r"this\s+letter\s+is\s+to\s+inform\s+you",
        r"we\s+(?:are\s+writing\s+to\s+)?(?:notify|inform)\s+you",
    ]
    for pat in incoming_mail_patterns:
        if re.search(pat, sample_lower[:600], re.M):
            return "incoming_mail"

    # ========================================================================
    # TIER 4 — Filename signals for Filer drafts
    # ========================================================================
    filer_filename_signals = [*_ident.name_tokens(),
        "binding precedent", "authority pack",
        "petition for", "petition to",
        "supplemental memorandum", "supplemental memo",
        "judicial notice",
        "proposed order",
    ]
    if any(s in fn_lower for s in filer_filename_signals):
        # Filename hint, but still verify no adversary content snuck in
        if not re.search(r"attorneys?\s+for\s+defendant", sample_lower):
            return "filer"

    # ========================================================================
    # TIER 5 — Filename signals for adversary
    # ========================================================================
    adversary_filename_signals = [
        "defendants motion", "defendant's motion",
        "in opposition to plaintiff",
        "garnishee", "garnishees",
    ]
    if any(s in fn_lower for s in adversary_filename_signals):
        return "adversary"

    # ========================================================================
    # TIER 6 — Author markers from AUTHOR_MARKERS dict (legacy)
    # ========================================================================
    for author, patterns in AUTHOR_MARKERS.items():
        for p in patterns:
            if p.search(sample):
                return author

    # ========================================================================
    # TIER 7 — Strong Filer positive signals (signature blocks, pro se)
    # NOTE: We only get here if no adversary signal was found above.
    # ========================================================================
    strong_filer = [
        re.compile(r"/s/\s*Filer[-\s]?Fardy", re.I),
        re.compile(r"\bRespectfully\s+submitted.{0,80}Filer[-\s]?Fardy", re.I | re.S),
        re.compile(r"\bPlaintiff,?\s+pro\s+se\b", re.I),
        re.compile(r"\bPlaintiff\s+respectfully\s+(?:submits|moves|requests|replies)", re.I),
        re.compile(r"\bI,?\s+Filer[-\s]?Fardy.{0,30}declare\b", re.I),
    ]
    for p in strong_filer:
        if p.search(sample):
            return "filer"

    # ========================================================================
    # TIER 7b — Filename-based fallback for reply/rebuttal/opposition
    # If the filename clearly says it's a reply/rebuttal/opposition and we
    # got here without finding an adversary signal, it's a Filer draft.
    # (Adversary signals are checked in Tier 2 — defense firms, bar IDs,
    # "Counsel for Defendant" — which would have already returned.)
    # ========================================================================
    filer_reply_filenames = [
        "reply to ", "reply_to_", "rebuttal", "sur-reply", "sur reply", "surreply",
        "answer to denial", "opposition to defendant",
    ]
    if any(s in fn_lower for s in filer_reply_filenames):
        return "filer"

    # ========================================================================
    # TIER 8 — Doctype-based fallback (much narrower than before)
    # The "Filer name in body = Filer-authored" rule is GONE.
    # ========================================================================
    if doc_type == "motion_to_dismiss":
        # Defendants file MTDs against Filer. Filer doesn't file MTDs.
        return "adversary"
    if doc_type == "complaint" and "plaintiff" in sample_lower[:1000]:
        # Complaints are usually plaintiff-authored unless they're answers
        return "filer"
    if doc_type == "petition":
        return "filer"

    return "unknown"


def detect_reference(filename: str, text_sample: str) -> bool:
    """Is this a treatise / statute / scholarly reference (not a case filing)?"""
    if FILER_GATE.search((text_sample or "")[:5000]):
        return False  # If Filer is mentioned, it's probably a filing that CITES a treatise
    if FILER_GATE.search(filename):
        return False
    for p in REFERENCE_MARKERS:
        if p.search(filename) or p.search((text_sample or "")[:3000]):
            return True
    return False


def detect_personal_life(filename: str, text_sample: str) -> bool:
    """Is this personal life material to be dropped?"""
    for p in PERSONAL_LIFE_MARKERS:
        if p.search(filename):
            return True
    # Check content only if filename didn't trigger
    sample = (text_sample or "")[:2000]
    for p in PERSONAL_LIFE_MARKERS:
        if p.search(sample):
            # But not if it's an exhibit in a case against the same company
            if FILER_GATE.search(sample) and re.search(r"\b(?:Plaintiff|Defendant|Complaint|Exhibit)\b", sample, re.I):
                return False
            return True
    return False


def detect_filer_mention(text_sample: str, filename: str) -> bool:
    """Does this document mention Filer or Filer?"""
    if FILER_GATE.search(filename):
        return True
    if text_sample and FILER_GATE.search(text_sample):
        return True
    return False


# Docket detection — uses the same anchored patterns as case_consolidator
DOCKET_PATTERNS = [
    # Federal: "2:26-cv-00456", optional -XYZ judge suffix — requires colon between digits
    (re.compile(r"\b(\d:\d{2}-cv-\d{4,5})(?:-[A-Z]{2,4})?\b", re.I),
     "fed",
     lambda m: m.group(1).lower()),
    # Federal underscored: "2_26-cv-00456"
    (re.compile(r"\b(\d)_(\d{2})-cv-(\d{4,5})\b", re.I),
     "fed",
     lambda m: f"{m.group(1)}:{m.group(2)}-cv-{m.group(3)}"),
    # Philadelphia Common Pleas via anchor (strongest)
    (re.compile(r"(?:Case\s*ID|Case\s*No\.?|Docket\s*No\.?|Docket\s*Number)[:\s]+(\d{9})\b", re.I),
     "phila_ccp",
     lambda m: m.group(1)),
    # Bare 9-digit starting with 24 or 25 (very likely Philly CCP docket)
    # Must NOT be immediately preceded/followed by other digits
    (re.compile(r"(?<!\d)(2[45]\d{7})(?!\d)"),
     "phila_ccp",
     lambda m: m.group(1)),
    # PA state civil
    (re.compile(r"\b(\d{4}-CV-\d{4,6})\b", re.I),
     "state_cv",
     lambda m: m.group(1).upper()),
    # Third Circuit appeal: "No. 24-1234" at beginning of line or after "Appeal No."
    # Much stricter to avoid false positives from citation bodies
    (re.compile(r"(?:Appeal\s+No\.?|3d\s+Cir\.?\s+No\.?)\s+(2[0-9]-\d{4,5})\b", re.I),
     "ca3",
     lambda m: m.group(1).lower()),
]


# Patterns that, when found around a candidate docket, indicate it's inside
# a citation body (e.g., "No. 20-1892, 2020 WL ...") and should NOT be
# treated as a case docket for THIS document
CITATION_BODY_EXCLUDERS = [
    re.compile(r"\b(?:20\d{2}\s+WL|F\.\s*Supp\.|F\.\s*3?d|F\.\s*4th|U\.S\.|S\.\s*Ct\.|Cir\.)"),
]


def detect_dockets(text: str, filename: str) -> List[str]:
    """
    Extract docket numbers from text + filename.
    
    Strategy: look in the caption area (first 3000 chars where case captions
    live) FIRST, since that's where the case's own docket number appears.
    Fall back to filename if caption has nothing.
    """
    results: Set[str] = set()
    text = text or ""

    # Caption area: first ~3000 chars, typically contains the header with
    # "Case ID: XXX" / "Case No. XXX"
    caption = text[:3000]
    caption_and_filename = filename + "\n" + caption

    for pattern, juris, normalizer in DOCKET_PATTERNS:
        for match in pattern.finditer(caption_and_filename):
            try:
                norm = normalizer(match)
                if norm:
                    results.add(f"{juris}:{norm}")
            except Exception:
                continue

    # If nothing in caption, do a second pass over more of the document but
    # require a line-level anchor to avoid false positives inside citation bodies
    if not results and len(text) > 3000:
        for line in text[:20000].splitlines():
            # Only care about lines that look like they're identifying THIS case
            if not re.search(r"\b(?:Case\s*(?:ID|No|Number)|Docket\s*(?:No|Number)|Civil\s*Action\s*No)\b", line, re.I):
                continue
            for pattern, juris, normalizer in DOCKET_PATTERNS:
                for match in pattern.finditer(line):
                    try:
                        norm = normalizer(match)
                        if norm:
                            results.add(f"{juris}:{norm}")
                    except Exception:
                        continue

    return sorted(results)


# Party detection — same tags as case_consolidator
PARTY_PATTERNS = [
    ("amerisave",      re.compile(r"\bAmerisave\b", re.I)),
    ("planet_home",    re.compile(r"\bPlanet\s*Home\b", re.I)),
    ("dovenmuehle",    re.compile(r"\bDovenmuehle\b", re.I)),
    ("transunion",     re.compile(r"\bTransUnion\b", re.I)),
    ("equifax",        re.compile(r"\bEquifax\b", re.I)),
    ("experian",       re.compile(r"\bExperian\b", re.I)),
    ("capital_one",    re.compile(r"\bCapital\s*One\b", re.I)),
    ("greensky",       re.compile(r"\bGreen[Ss]ky\b", re.I)),
    ("avant",          re.compile(r"\bAvant\b", re.I)),
    ("lippi",          re.compile(r"\bLippi\b", re.I)),
    ("northern_trust", re.compile(r"\bNorthern\s*Trust\b", re.I)),
    ("state_farm",     re.compile(r"\bState\s*Farm\b", re.I)),
    ("allstate",       re.compile(r"\bAllstate\b", re.I)),
    ("progressive",    re.compile(r"\bProgressive\s+(?:Insurance|Ins\.|Casualty|Direct)\b", re.I)),
    ("chase",          re.compile(r"\b(?:JP\s*Morgan\s+)?Chase\s+Bank\b", re.I)),
    ("pennymac",       re.compile(r"\bPenny\s*Mac\b", re.I)),
    ("bny_mellon",     re.compile(r"\bBN[YC]\s*Mellon\b", re.I)),
    ("pgw",            re.compile(r"\bPhiladelphia\s+Gas\s+Works\b|\bPGW\b", re.I)),
    ("cfpb",           re.compile(r"\bCFPB\b|\bConsumer\s+Financial\s+Protection\s+Bureau\b", re.I)),
]


def detect_parties(text: str) -> List[str]:
    """
    Detect all defendant parties mentioned in the document caption area.
    
    Only scans the first 5000 chars (the caption block) to prevent
    the false-positive explosion that happens when a brief discusses
    other related cases in passing.
    """
    if not text:
        return []
    results: List[str] = []
    sample = text[:5000]
    for tag, pattern in PARTY_PATTERNS:
        if pattern.search(sample):
            results.append(tag)
    return results


# =============================================================================
# SCANNER
# =============================================================================

class DocumentScanner:
    """
    Scans source directories and produces Document records.

    Uses mtime+size+path for change detection, SHA1 for dedup across runs.
    Caches extracted text at text_cache/<sha1>.txt.
    """

    def __init__(
        self,
        source_dirs: List[Path],
        cache_dir: Path,
        accepted_exts: Set[str] = ACCEPTED_EXTENSIONS,
    ):
        self.source_dirs = [Path(d) for d in source_dirs]
        self.cache_dir = Path(cache_dir)
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.accepted_exts = accepted_exts

        # Load manifest from previous run
        self.manifest_path = self.cache_dir / "_manifest.json"
        self.prev_manifest: Dict[str, dict] = self._load_manifest()
        self.new_manifest: Dict[str, dict] = {}

        self.documents: List[Document] = []
        self.skipped_personal: int = 0
        self.skipped_reference: int = 0
        self.skipped_no_text: int = 0
        self.scanned_count: int = 0
        self.cache_hits: int = 0

    def _load_manifest(self) -> Dict[str, dict]:
        if not self.manifest_path.exists():
            return {}
        try:
            return json.loads(self.manifest_path.read_text(encoding="utf-8"))
        except Exception:
            return {}

    def _save_manifest(self) -> None:
        try:
            self.manifest_path.write_text(
                json.dumps(self.new_manifest, indent=2),
                encoding="utf-8",
            )
        except Exception as e:
            log.error(f"Manifest save failed: {e}")

    def _cache_text_path(self, sha1: str) -> Path:
        # Shard by first 2 hex chars to avoid one giant directory
        return self.cache_dir / sha1[:2] / f"{sha1}.txt"

    def _read_cached_text(self, sha1: str) -> Optional[str]:
        p = self._cache_text_path(sha1)
        if p.exists():
            try:
                return p.read_text(encoding="utf-8", errors="ignore")
            except Exception:
                return None
        return None

    def _write_cached_text(self, sha1: str, text: str) -> None:
        p = self._cache_text_path(sha1)
        p.parent.mkdir(parents=True, exist_ok=True)
        try:
            p.write_text(text, encoding="utf-8", errors="ignore")
        except Exception as e:
            log.debug(f"Text cache write failed for {sha1}: {e}")

    def _should_reprocess(self, path: Path, stat) -> bool:
        """True if this file has changed since the last run."""
        key = str(path)
        prev = self.prev_manifest.get(key)
        if not prev:
            return True
        return (
            prev.get("mtime") != stat.st_mtime
            or prev.get("size") != stat.st_size
        )

    def _iter_files(self) -> Iterable[Path]:
        """Walk all source directories for acceptable files."""
        for src in self.source_dirs:
            if not src.exists():
                log.warning(f"Source dir does not exist: {src}")
                continue
            for f in src.rglob("*"):
                if not f.is_file():
                    continue
                if f.suffix.lower() not in self.accepted_exts:
                    continue
                # Skip hidden / lock files
                if f.name.startswith(("~", ".")):
                    continue
                yield f

    def scan(self) -> List[Document]:
        """Run the scan. Returns list of Document records."""
        for path in self._iter_files():
            try:
                stat = path.stat()
            except Exception:
                continue

            self.scanned_count += 1

            reprocess = self._should_reprocess(path, stat)

            # Hash once per run (cheap)
            sha1 = file_sha1(path)
            if not sha1:
                continue

            # Try cached text first
            text = None
            if not reprocess:
                text = self._read_cached_text(sha1)
                if text is not None:
                    self.cache_hits += 1

            if text is None:
                text = extract_text(path)
                if text:
                    self._write_cached_text(sha1, text)

            if not text:
                self.skipped_no_text += 1
                # Still record in manifest so we don't rescan every run
                self.new_manifest[str(path)] = {
                    "sha1": sha1, "mtime": stat.st_mtime, "size": stat.st_size,
                    "text_chars": 0, "skipped": "no_text",
                }
                continue

            text_sample = text[:10000]

            # Fast filters
            is_personal = detect_personal_life(path.name, text_sample)
            if is_personal:
                self.skipped_personal += 1
                self.new_manifest[str(path)] = {
                    "sha1": sha1, "mtime": stat.st_mtime, "size": stat.st_size,
                    "text_chars": len(text), "skipped": "personal",
                }
                continue

            is_reference = detect_reference(path.name, text_sample)
            if is_reference:
                self.skipped_reference += 1
                # DO record reference docs so they can be indexed later
                doc = Document(
                    path=str(path),
                    filename=path.name,
                    extension=path.suffix.lower(),
                    size_bytes=stat.st_size,
                    mtime=stat.st_mtime,
                    sha1=sha1,
                    text_chars=len(text),
                    text_path=str(self._cache_text_path(sha1)),
                    doc_type="reference",
                    author="reference",
                    is_reference=True,
                )
                self.documents.append(doc)
                self.new_manifest[str(path)] = {
                    "sha1": sha1, "mtime": stat.st_mtime, "size": stat.st_size,
                    "text_chars": len(text), "doc_type": "reference",
                }
                continue

            # Main detection
            doc_type = detect_doc_type(path.name, text_sample)
            author = detect_author(text_sample, doc_type, filename=path.name)
            dockets = detect_dockets(text, path.name)
            parties = detect_parties(text)
            is_filer = detect_filer_mention(text_sample, path.name)

            doc = Document(
                path=str(path),
                filename=path.name,
                extension=path.suffix.lower(),
                size_bytes=stat.st_size,
                mtime=stat.st_mtime,
                sha1=sha1,
                text_chars=len(text),
                text_path=str(self._cache_text_path(sha1)),
                doc_type=doc_type,
                author=author,
                dockets=dockets,
                parties=parties,
                is_filer_case=is_filer,
            )
            self.documents.append(doc)
            self.new_manifest[str(path)] = {
                "sha1": sha1,
                "mtime": stat.st_mtime,
                "size": stat.st_size,
                "text_chars": len(text),
                "doc_type": doc_type,
                "author": author,
                "dockets": dockets,
                "parties": parties,
                "is_filer": is_filer,
            }

        self._save_manifest()
        return self.documents

    def summary(self) -> dict:
        """Stats for the dry-run report."""
        by_type: Dict[str, int] = {}
        by_author: Dict[str, int] = {}
        filer_docs = 0
        with_dockets = 0

        for doc in self.documents:
            by_type[doc.doc_type] = by_type.get(doc.doc_type, 0) + 1
            by_author[doc.author] = by_author.get(doc.author, 0) + 1
            if doc.is_filer_case:
                filer_docs += 1
            if doc.dockets:
                with_dockets += 1

        return {
            "scanned_count": self.scanned_count,
            "cache_hits": self.cache_hits,
            "kept_documents": len(self.documents),
            "skipped_personal": self.skipped_personal,
            "skipped_reference": self.skipped_reference,
            "skipped_no_text": self.skipped_no_text,
            "filer_documents": filer_docs,
            "documents_with_dockets": with_dockets,
            "by_type": by_type,
            "by_author": by_author,
        }
