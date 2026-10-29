# TawzeefAI: requirements and implementation notes

## Source and scope

The source is **Project 05: TawzeefAI, HR & Recruitment Screening Assistant**, pages 12–13 of `Agentic_AI_Capstone_Project_Idea_Booklet_261029_011440.pdf` supplied by the project owner. This document distinguishes the booklet's requirements and suggested success measures from proposed implementation decisions. Nothing in the booklet specifies a particular programming language, framework, model provider, database, calendar provider, or hosting platform.

## Booklet requirements

### Agent system

- Match résumés to real job requirements and provide explainable scores.
- Provide an interview-scheduling tool.
- Track each candidate's actual status across multiple interactions.
- Plan and execute multistep recruitment workflows.

### Retrieval and knowledge

- Use HR policy and employee-handbook documents, real job descriptions and requirements, and a sample anonymized archive of past successful-hire profile patterns.
- Tune retrieval to distinguish similar but different roles.
- Deliver a working RAG-backed HR policy question-and-answer flow.

### Interfaces and operations

- Provide chat for candidates and current employees.
- Provide an HR dashboard showing actual recruitment metrics, including time to hire and screening volume, and a candidate-pipeline visualization.
- Provide genuinely distinct candidate and HR-staff views with enforced access control.
- Manage secrets and audit every screening decision.
- Handle incomplete and malformed résumé data.

### Expected deliverables

- A working résumé-screening assistant tested against a real, representative role.
- A working RAG-backed HR policy Q&A flow and a dashboard with real, meaningful recruitment metrics.
- Documented access control separating candidate and HR views.
- A short account of how matching logic addresses one specific named bias risk.

## Suggested success measures from the booklet

These are **suggested**, not demonstrated results or stricter requirements added by this document:

- Correctly rank candidates in at least 8 of 10 cases against a real, defined role.
- Correctly answer representative HR policy questions.
- Verify, with an actual test, no data leakage between candidate and HR-only views.
- Measure screening-time reduction against a fully manual baseline.

Evaluation needs a defined dataset, expected results, a repeatable procedure and recorded outcomes. A small test set does not prove universal fairness or absence of all possible leaks. A time-saving claim requires an actual comparable baseline; it cannot be inferred merely from automation.

## Proposed implementation direction

These are engineering options, not requirements imposed by the booklet:

- Use an isolated Python 3.12 backend with a simple browser interface and a local database for a reproducible graduation demo. SQLite with FTS5 is available on the current machine and can support an initial lexical retrieval baseline; assess retrieval quality on similar roles before deciding whether embeddings or a vector database are necessary.
- Persist applications, workflow steps, interview appointments and timestamps. Implement a real local scheduling workflow with conflict checks; present it as an internal calendar unless an external provider is actually connected.
- Make scores traceable to job-relevant evidence and missing information. Let HR review decisions; do not infer suitability from protected characteristics or automatically treat prior hiring patterns as proof of suitability. Test a named bias risk with controlled examples and document limitations.
- Enforce authentication and authorization on the server for every data operation. Restrict candidate records to their owners and HR access to authorized staff; distinguish policy-document visibility by role. Record screening decisions and relevant actions without logging unnecessary personal data.
- Provide a configurable model interface. If no model credentials or local model are available, an extractive, cited fallback can make the demo usable, but must be labeled as such: it does not establish that a generative RAG workflow or model-driven agent has been completed.
- Test PDF and DOCX extraction with representative synthetic examples, including malformed and scanned documents. OCR support and Arabic extraction should be reported only after they are implemented and tested.
- Keep provider secrets outside source control, pin application dependencies, and document first-run setup and restoration. Use only synthetic or appropriately anonymized data for development and demos.

## Current evidence and open decisions

- A local demo implementation now exists. Python, `uv`, Git, Node and development testing tools have been observed working; SQLite 3.53.1 supports FTS5 on this machine. Current capabilities and their limitations are documented in the root README; the existence of code does not establish that every booklet deliverable has been met.
- The supplied booklet was read as a text PDF. This does not establish accurate processing of scanned files, Arabic résumés, multicolumn documents, or DOCX files.
- The actual deployment target, model access, database choice, external calendar integration, operational data and evaluation baseline remain to be decided or supplied during implementation.
- A local demo, seeded data or mock integration must be identified as such; the existence of a tool or an MCP connection does not prove the corresponding product feature works.
