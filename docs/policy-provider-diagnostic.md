# Policy provider availability diagnostic

On 1 November 2026, a separate diagnostic investigated call failures observed in
the broader synthetic policy run. It used the configured Groq model and fictional
questions and passages only. The probe records HTTP status codes and exception
types, not API keys, request or response bodies, exception messages, or headers.

Two sequential probes produced the following observations:

- Repeating one synthetic question and passage 10 times produced 20 HTTP 200
  responses: ten search calls and ten quote-selection calls. All ten search
  responses passed the local tool-call check and all ten quote responses passed
  the local exact-line check. This is not a measure of policy-answer accuracy.
- Running the 30 bundled draft-labelled English and Arabic questions with their
  fictional passages produced 60 requests: 30 HTTP 200, 29 HTTP 429, and one
  HTTP 400. The 429 responses began after earlier requests had succeeded. HTTP
  429 indicates that the provider rejected requests due to rate limiting. The
  cause of the HTTP 400 was not established; no provider response body was read
  for this diagnostic. Some HTTP 200 responses also failed local validation.

These results establish a rate-limit failure mode in this probe; they do not
identify the cause of each failure in the earlier 34-case run or characterize
provider reliability in general. The varied probe supplies each question's
draft-expected passage directly: it does not exercise retrieval or the full
conversation endpoint. The application currently falls back or abstains when
the provider is unavailable. It does not automatically retry, and no throughput
or answer-correctness claim follows from this probe.

To reproduce from `backend`, set `GROQ_API_KEY` securely in the process
environment and run `python diagnose_policy_provider.py` for the repeated
example, or add `--varied` for the bundled synthetic questions. These commands
make real network requests and can encounter provider limits or costs. The
script does not print the key, provider response bodies, or exception messages.
Do not substitute real employee or candidate data into this development probe.
