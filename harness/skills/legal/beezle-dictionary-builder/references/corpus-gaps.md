# Corpus Gaps: Terms Not Found in ChromaDB

## judicial_immunity (federal)
**Status:** NOT_FOUND  
**Cause:** SCOTUS case law not ingested properly into ChromaDB  
**Workaround:** Manual research required; term exists in Black's Law Dictionary and federal case law but not in the ingested corpus  
**Fixed in:** ChromaDB re-ingestion (pending)

## standing (federal)
**Status:** NOT_FOUND (DB Error)  
**Cause:** ChromaDB ingestion pipeline bug: `'int' object has no attribute 'strip'`  
**Error Location:** ChromaDB query/ingestion layer  
**Workaround:** Manual research or ChromaDB re-ingestion required  
**Fixed in:** Ingestion pipeline patch (pending)

## federal_reserve_note (treasury)
**Status:** NOT_FOUND (Scope Mismatch)  
**Cause:** Term defined in Federal Reserve Act (12 USC § 411), not Treasury regulations  
**Scope:** Defined in federal/federal_reserve, not treasury  
**Workaround:** Query with scope `federal` or `federal_reserve` instead of `treasury`  
**Note:** Treasury regulations do not define Federal Reserve Notes; this is a scope mismatch, not a corpus gap

## lawful_money (treasury)
**Status:** FOUND (for reference)  
**Note:** Unlike federal_reserve_note, "lawful money" IS defined in Treasury regulations (31 CFR)  
**Source:** Treasury definitions include "lawful money of the United States" as a defined term