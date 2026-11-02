# TawzeefAI: integration status and delivery plan

This document records the review made on 29 October 2026. It is a historical snapshot, not proof of current delivery readiness or test results. Recheck evidence after each change. LangGraph review paths, résumé section extraction, weighted human assessments, saved conversations, optional OCR, and other capabilities were added after the original review. Consult the [README](../README.md), tests, and [requirements review](pdf-requirements-gap.md) for current evidence and limits.

## Requirements and repository

- `Agentic_AI_Capstone_Project_Idea_Booklet_261029_011440.pdf` describes the recruitment assistant, its knowledge interfaces and permissions, and **suggested** success measures.
- `4_5987536898589990745_261029_072737.pdf` is the accompanying presentation. It adds detailed architecture and workflow, including LangGraph, specialized agents, PostgreSQL and vector storage, Streamlit, screening weights, approval and scheduling, and evaluation. See the [requirements review](pdf-requirements-gap.md) for slide references.
- TawzeefAI is an independent repository, separate from `DEPI_projects` and ShopAgent. Verify the current Git status and remote state before delivery.

## Running the local demo

On Windows, with Python 3.12 or 3.13 and `uv`, run from the project directory:

```powershell
& .\start-demo.ps1
```

Open `http://127.0.0.1:8000/`. The script uses `uv run --locked`, enables demo accounts, and stores its database in `backend/tawzeefai.sqlite3`. Stop it with `Ctrl+C`. See the [README](../README.md) and [setup guide](../SETUP.md) for credentials, optional integrations, tests, and current limitations. Demo credentials are public; keep the server local and use synthetic data.

If present, a Groq key protected for the current Windows account is loaded from `.tooling/groq-key.dpapi`; otherwise the demo uses extractive answers. The protection does not automatically transfer to another device. Do not commit keys, databases, or real résumés, or expose secrets in logs. With Groq enabled, authorized questions and policy excerpts are sent to the provider; this does not make the demo suitable for real data.

From `TawzeefAI/backend`, the baseline checks include:

```powershell
uv run --locked pytest -q
uv run --locked ruff check .
uv run --locked python evaluate.py
```

The evaluation uses synthetic examples with draft labels; it is not an independent benchmark or human timing study. Record fresh command results after changes. A working local setup does not establish that all presentation requirements are met.

## Historical gap categories

The original review identified gaps in architecture and deployment, structured résumé analysis and weighted screening, role-filtered RAG, the candidate lifecycle and external integrations, role management and dashboards, independent evaluation data, and reproducible delivery. Several subsequent changes address parts of these areas, but the historical matrix in the [requirements review](pdf-requirements-gap.md) must not be mistaken for current status. Each requirement needs current evidence before it is considered complete.

The dashboard figures in the presentation are illustrations, not measurements of this project. The PDFs themselves do not authorize sending email, processing real people's data, or publishing a service.

## Proposed work sequence

1. **Establish a baseline:** Review both documents and the current repository, run its checks, and confirm synthetic data is tracked while secrets, databases, and environments remain excluded.
2. **Contracts and data:** Define roles, ownership, candidate states, and safe migrations; prepare synthetic samples and an independently reviewed evaluation reference. Do not describe draft labels as human approval.
3. **Backend and retrieval:** Configure and test PostgreSQL, vector storage, migrations, ingestion, access-filtered retrieval, and accurate citations. Exercise recovery and invalid data before connecting a model.
4. **Agents and screening:** Implement and test LangGraph agents, tools, resumable memory, the rubric, and source evidence. Keep hiring decisions with a reviewer; test injection, identity changes, and counterfactual examples.
5. **Experience and scheduling:** Complete the candidate, employee, HR, and admin journeys, comparison, and dashboards. Test ownership boundaries, duplication, provider failure, and authorized calendar and email integration.
6. **Evaluation and delivery:** Verify end-to-end flows and assess retrieval, answers, and ranking on data held out from tuning. Document bias limitations and perform an actual human baseline study if available. Verify clean Docker setup and record evidence and unmeasured outcomes.

Work that does not depend on Calendar or Gmail can proceed independently. For a real integration test, identify the account, permissions, data, and potential costs before connecting.
