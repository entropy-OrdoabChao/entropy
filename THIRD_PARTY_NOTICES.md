# Third-party notices and credits

Entropy is built on other people's work. Each component below keeps its own license; those licenses travel with it and are
not changed by Entropy's license (AGPL-3.0). Versions are the ones the package was tested with (2026-09-20).

## Python packages (from requirements.txt)

| Package | Version | License | Home |
|---|---|---|---|
| fastapi | 0.135.1 | MIT | https://github.com/fastapi/fastapi |
| uvicorn | 0.41.0 | BSD-3-Clause | https://uvicorn.dev/ |
| python-multipart | 0.0.22 | Apache-2.0 | https://github.com/Kludex/python-multipart |
| PyYAML | 6.0.3 | MIT | https://pyyaml.org/ |
| python-docx | 1.2.0 | MIT | https://github.com/python-openxml/python-docx |
| fastmcp | 3.2.3 | Apache-2.0 | https://gofastmcp.com |
| httpx | 0.28.1 | BSD-3-Clause | https://github.com/encode/httpx |
| requests | 2.34.2 | Apache-2.0 | https://github.com/psf/requests |
| beautifulsoup4 | 4.14.3 | MIT | https://www.crummy.com/software/BeautifulSoup/bs4/ |
| pdfplumber | 0.11.9 | MIT | https://github.com/jsvine/pdfplumber |
| pypdf | 6.10.2 | BSD-3-Clause | https://github.com/py-pdf/pypdf |
| PyMuPDF (fitz) — OPTIONAL, in requirements-optional.txt, never installed by default | 1.27.2.2 | **AGPL-3.0** (or a commercial license from Artifex) | https://github.com/pymupdf/pymupdf |
| pdf2image | 1.17.0 | MIT | https://github.com/Belval/pdf2image |
| pytesseract | 0.3.13 | Apache-2.0 (Tesseract itself: Apache-2.0) | https://github.com/madmaze/pytesseract |
| Pillow | 12.1.1 | MIT-CMU | https://python-pillow.org |
| reportlab | 4.4.10 | BSD | https://www.reportlab.com/ |
| numpy | 2.4.3 | BSD-3-Clause (and 0BSD, MIT, Zlib, CC0-1.0 parts) | https://numpy.org |
| chromadb | 1.5.5 | Apache-2.0 | https://github.com/chroma-core/chroma |
| sentence-transformers | 5.3.0 | Apache-2.0 | https://www.sbert.net |
| torch | 2.11.0 | BSD-3-Clause | https://pytorch.org |
| google-api-python-client | 2.194.0 | Apache-2.0 | https://github.com/googleapis/google-api-python-client |
| google-auth-oauthlib | 1.3.1 | Apache-2.0 | https://github.com/googleapis/google-cloud-python |
| playwright | 1.58.0 | Apache-2.0 | https://github.com/Microsoft/playwright-python |

**PyMuPDF is AGPL-3.0**, so it is not a dependency of Entropy: it sits in `requirements-optional.txt`, every `import fitz` in
the package is inside a try/except, and text extraction falls back to pdfplumber and pypdf when it is absent. A person who
installs it does so under its own terms.

## Harness, servers and models

| Component | License | Credit |
|---|---|---|
| Hermes Agent (NousResearch) — the optional harness the skills and profiles are written for | MIT | https://github.com/NousResearch/hermes-agent |
| llama.cpp / LM Studio — local model serving | MIT (llama.cpp); LM Studio under its own terms | https://github.com/ggml-org/llama.cpp |
| vLLM — local model serving on larger cards | Apache-2.0 | https://github.com/vllm-project/vllm |
| Qwen3 / Qwen3.5 models (Alibaba Cloud) — base of the 27B and 9B tiers | Apache-2.0 | https://huggingface.co/Qwen |
| IBM Granite 4.2 models — 9B bake-off candidate | Apache-2.0 | https://huggingface.co/ibm-granite |
| Abliterated / fine-tuned derivatives used as bases | inherit the base model's license; the deriving author is credited in models/manifest.json | — |
| BAAI bge-base-en-v1.5 — the embedding model for the library index | MIT | https://huggingface.co/BAAI/bge-base-en-v1.5 |
| Tesseract OCR | Apache-2.0 | https://github.com/tesseract-ocr/tesseract |
| Obsidian (optional vault viewer) | proprietary, free for personal use; Entropy only writes Markdown files it can open | https://obsidian.md |

## Law and data sources (the library packs the person downloads)

| Source | Terms | Notes |
|---|---|---|
| United States Code, CFR, eCFR, Federal Register, public laws, Statutes at Large, court rules, federal opinions (govinfo, uscode.house.gov, ecfr.gov, federalregister.gov, uscourts.gov) | U.S. government works: public domain (17 U.S.C. § 105) | free to download and redistribute |
| Court opinions via CourtListener / Free Law Project | opinions are public domain; the CourtListener database and API are under their terms (CC BY-ND for the bulk data notes; check current terms) | credit: Free Law Project, https://www.courtlistener.com |
| Caselaw Access Project (Harvard) | public domain text; check current access terms | https://case.law |
| State constitutions, statutes, codes, registers, court rules (official state sites in corpus_packs/states/*.yaml) | mostly public records; some states assert copyright or restrict bulk redistribution | the person downloads their own state's text on demand; Entropy ships links only |
| Am Jur, CJS, Wright & Miller, Moore's, MCL and other treatises | copyrighted (Thomson Reuters / LexisNexis / Federal Judicial Center for MCL) | not in the repository; a person who owns copies may add them to their own library. |
| PA Bulletin, PA Code, Pa.C.S. | Commonwealth of Pennsylvania; official texts published for public access | linked in the PA pack |

## People and projects credited in the specs

The state packs, court chains and filing recipes were mapped from official court, legislature and agency sites; those sites are
credited inside each `corpus_packs/states/<ST>.yaml`. The MCP protocol is by Anthropic; the FastMCP framework by Prefect.

If a credit is missing or a license is misstated here, open an issue and it will be corrected.
