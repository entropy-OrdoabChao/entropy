#!/usr/bin/env python3
"""
phase1_diagnose.py — investigate the usc_t1 surprise

Sample committed 434 chunks as corpus_namespace=usc_t1. We need to know:
  1. Are these real "1 USC § N" citations? (Title 1 USC is "General Provisions"
     — rare but exists. e.g. 1 USC § 8 "Person, human being, child, individual"
     applied in personhood/abortion law)
  2. Are they false positives from metadata that contains "1 USC" string but
     means something else?

Outputs:
  - 20 sample chunks with full metadata + first 300 chars of doc
  - Counts of how often each metadata field is the source of citation text
  - Recommendation: parser patch needed YES/NO
"""
import json
import sys
from collections import Counter
from pathlib import Path

try:
    import chromadb
except ImportError:
    sys.exit("ERROR: chromadb not installed.")

sys.path.insert(0, str(Path(__file__).parent))
from citation_parser import parse


def main():
    client = chromadb.HttpClient(host="localhost", port=8100)
    coll = client.get_collection("legal_docs")

    print("=" * 70)
    print("PHASE 1 DIAGNOSTIC — investigating usc_t1 false positives")
    print("=" * 70)

    # Fetch up to 100 usc_t1 chunks
    r = coll.get(where={"corpus_namespace": "usc_t1"},
                 limit=100, include=["metadatas", "documents"])
    print(f"\nFound {len(r['ids'])} chunks with corpus_namespace=usc_t1")

    # For each, identify which metadata field the parser actually parsed
    METADATA_FIELDS = ["citations_extracted", "citations", "known_statute_names",
                       "known_statutes", "defined_terms", "section"]

    matching_field_counts = Counter()
    matched_text_examples = []
    true_positives = []
    false_positives = []
    doc_only_matches = []

    for cid, m, d in zip(r["ids"], r["metadatas"], r["documents"]):
        # Try parsing each metadata field; find the first that parses to usc_t1
        matched_field = None
        matched_text = ""
        for field in METADATA_FIELDS:
            v = m.get(field)
            if not v or not isinstance(v, str):
                continue
            for part in v.split(","):
                part = part.strip()
                if not part:
                    continue
                c = parse(part)
                if c and c.namespace == "usc_t1":
                    matched_field = field
                    matched_text = part
                    break
            if matched_field:
                break

        if not matched_field and d:
            # Came from doc text fallback
            c = parse(d[:500])
            if c and c.namespace == "usc_t1":
                matched_field = "doc_text"
                matched_text = d[:200]
                doc_only_matches.append({"chunk_id": cid, "doc": d[:300]})

        matching_field_counts[matched_field or "UNKNOWN"] += 1

        # Classify: is this a real "1 USC § N" reference?
        real_usc_t1 = "1 USC" in matched_text or "1 U.S.C." in matched_text
        target = true_positives if real_usc_t1 else false_positives
        if len(target) < 10:
            target.append({
                "chunk_id": cid,
                "source_field": matched_field,
                "matched_text": matched_text[:200],
                "citation_canonical": m.get("citation_canonical"),
                "doc_first_200": (d or "")[:200],
            })
        if len(matched_text_examples) < 30:
            matched_text_examples.append((matched_field, matched_text[:120]))

    print("\n--- Which metadata field produced the parse? ---")
    for field, n in matching_field_counts.most_common():
        print(f"  {n:>4}  {field}")

    print("\n--- First 30 matched texts (field → text) ---")
    for field, txt in matched_text_examples[:30]:
        print(f"  [{field or 'UNKNOWN':>20}]  {txt!r}")

    print(f"\n--- TRUE positives (real '1 USC § N' references) ---")
    print(f"    count: {len(true_positives)}+ (showing first {min(10, len(true_positives))})")
    for tp in true_positives:
        print(f"  chunk={tp['chunk_id']}")
        print(f"    field={tp['source_field']!r}  canonical={tp['citation_canonical']!r}")
        print(f"    matched_text={tp['matched_text']!r}")
        print(f"    doc[:200]={tp['doc_first_200']!r}")
        print()

    print(f"--- FALSE positives (metadata says '1 USC' but doc isn't about it) ---")
    print(f"    count: {len(false_positives)}+ (showing first {min(10, len(false_positives))})")
    for fp in false_positives:
        print(f"  chunk={fp['chunk_id']}")
        print(f"    field={fp['source_field']!r}  canonical={fp['citation_canonical']!r}")
        print(f"    matched_text={fp['matched_text']!r}")
        print(f"    doc[:200]={fp['doc_first_200']!r}")
        print()

    # Verdict
    print("=" * 70)
    print("VERDICT")
    print("=" * 70)
    if len(false_positives) > len(true_positives) * 2:
        print("  → PARSER PATCH NEEDED. False positives dominate.")
        print("  → Likely cause: metadata field contains '1 USC' as a literal")
        print("    string from an upstream extractor, not a real cite.")
    else:
        print("  → No parser patch needed. 'usc_t1' counts are legitimate.")
        print("  → Title 1 USC really does appear N times in the sample.")

    # Save full report
    out = "/tmp/phase1_diagnose_report.json"
    Path(out).write_text(json.dumps({
        "total_usc_t1_chunks": len(r["ids"]),
        "matching_field_counts": dict(matching_field_counts),
        "true_positive_examples": true_positives,
        "false_positive_examples": false_positives,
    }, indent=2, default=str))
    print(f"\nReport → {out}")


if __name__ == "__main__":
    main()
