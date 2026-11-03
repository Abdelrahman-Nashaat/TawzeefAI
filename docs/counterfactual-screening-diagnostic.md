# Synthetic counterfactual screening diagnostic

The presentation requests a counterfactual bias test. `data/fixtures/counterfactual-screening-v1.json` freezes four fictional development pairs in which only an unlabelled identity line changes. Three intentionally collide with a required keyword; one contains Arabic identity lines and a common Python requirement. These deliberately narrow adversarial examples are not representative applicants or independently reviewed evidence.

From `backend`, run:

```powershell
uv run --locked python evaluate_counterfactual.py
```

The runner uses a fresh temporary database and local FastAPI endpoints to create fictional jobs, upload DOCX variants as a candidate, and request HR keyword screenings. It then simulates a reviewer choosing the known identity span in the already rule-redacted source, saves the mask through the HR API, and screens again. It stops on endpoint errors, missing or ambiguous identity spans, or malformed pairs. It disables Groq, Google and the optional automatic worker for this process. It reports a fixture version and SHA-256, counts and per-variant keyword scores and required-skill mention flags before and after the selected mask. Save the output along with the commit and environment to reproduce a run; no real CVs belong in the fixture or report.

**Interpretation:** equality is calculated only for scores and required-skill mention flags, not ranking, evidence excerpts, model behavior, or real-world outcomes. The program selecting known spans is not an independent human review. Rule-based and additional masking can miss identity, remove useful evidence, and leave proxies. A result may reveal a difference in these examples but neither equality nor the sample size establishes fairness, anonymity, absence of discrimination, or treatment across populations. This is a development set and must not be used as a held-out accuracy benchmark. Review results manually, including evidence excerpts and cases involving nonidentity skills that should not be removed; the runner does not assess those properties.
