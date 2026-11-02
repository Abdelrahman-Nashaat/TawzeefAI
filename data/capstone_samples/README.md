# Synthetic educational data for TawzeefAI

All jobs, résumés, policies, hiring patterns, and answers in this directory are **fictional**. They do not represent people, an organization, actual policies, or hiring decisions. Proposed answers and evidence are draft labels that have not been approved by a human reviewer; they do not independently establish accuracy or fairness.

The collection is separate from `data/fixtures/` and is not loaded automatically by the application. It contains:

- `jobs.json`: Six jobs, including similar data roles, with required and preferred skills and responsibilities.
- `cvs.json`: 30 short synthetic résumés, 18 in English and 12 in Arabic, including missing evidence, negation, and untrusted instructions. These are text samples, not PDF/DOCX files or real people's data. `expected_evidence` and the negation and missing-evidence fields are proposed references for review, not final candidate judgments.
- `handbook.json`: 15 bilingual sections about fictional policies, each with an identifier, access level, and **logical** page number. The page number is a test reference for the section; no 15-page PDF was generated or visually checked.
- `policy-qa.json`: 30 proposed question-answer pairs, 15 in each language, referencing section identifiers and logical pages. Human review is required before using the answers as an evaluation reference.
- `negative-qa.json`: Four additional proposed abstention, access-control, and injection cases, separate from the 30 answerable questions.
- `ranking.json`: Ten justified pairwise comparisons of synthetic résumés for the same job. They compare stated evidence and do not judge people. The proposed labels have not been reviewed by a person, and this file has not evaluated the algorithm.
- `hire-patterns.json`: Six short fictional patterns, with no past hiring records. They must not be treated as eligibility rules or evidence of real hiring success.

`generate.py` regenerates the JSON files from its embedded content and overwrites the copies in this directory. `validate.py` checks JSON integrity, counts, unique identifiers, references, languages, and logical page numbers; it does not assess claim accuracy, retrieval quality, or licensing.

```powershell
python data/capstone_samples/validate.py
```

Do not introduce real data into these experiments or compare the examples against an invented human screening time. Scanned documents, OCR, multicolumn files, and human evaluation require separate preparation and testing.
