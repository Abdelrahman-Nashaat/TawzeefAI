# TawzeefAI setup

TawzeefAI is a local recruitment-assistant demo based on the project booklet and presentation. It is not complete against those documents and is not ready for real applicant data or public deployment. See the [README](README.md) for current capabilities and the [requirements review](docs/pdf-requirements-gap.md) for historical gaps.

## Run locally on Windows

Install Python 3.12 or 3.13 and `uv`. From PowerShell in the repository root:

```powershell
cd backend
uv sync --locked
cd ..
& .\start-demo.ps1
```

Open <http://127.0.0.1:8000/> and press `Ctrl+C` to stop the server. The script enables publicly documented demo accounts and binds to localhost. Use synthetic data only. The local SQLite database persists between runs; demo credentials are listed in the [README](README.md).

For optional local semantic résumé screening, prepare the pinned model as described in [local semantic experiment](docs/local-semantic-experiment.md) and run `& .\start-demo.ps1 -SemanticScreening`. For the separately configured Windows Google integration, run `& .\start-demo.ps1 -Google`. The switches can be combined. Semantic suggestions are unverified and require HR review.

## Run the local Docker demo

Start Docker Desktop and wait for its engine, then run:

```powershell
& .\start-docker-demo.ps1
```

Open <http://127.0.0.1:18000/>. The script creates a local random PostgreSQL password in `.tooling/docker-postgres.env`, which is excluded from Git. Keep this file with the Docker data: changing the file does not change the password of an existing database. Containers bind to localhost and use known demo credentials; this is not a production setup.

To enable experimental hybrid retrieval in Docker, prepare the model described in [local semantic experiment](docs/local-semantic-experiment.md) and run `& .\start-docker-demo.ps1 -Hybrid`. The app stores primary data in SQLite; PostgreSQL/pgvector is used by the optional retrieval index. Docker data is separate from direct-run SQLite data. To stop containers while retaining volumes:

```powershell
docker compose --env-file .tooling/docker-postgres.env --project-name tawzeefai-local down
```

Deleting volumes deletes their data. Windows Credential Manager credentials are not available in the Linux container.

## Checks

From `backend`, run the relevant checks after changes:

```powershell
uv run --locked pytest -q
uv run --locked ruff check .
uv run --locked python evaluate.py
uv run --locked python evaluate_capstone.py
```

These commands are instructions, not claims that checks passed. Evaluation examples are synthetic and their draft labels have not been independently approved.

## Model, secrets, and Google integration

If `.tooling/groq-key.dpapi` contains a key protected for the current Windows user, `start-demo.ps1` loads it into the server process and enables Groq; otherwise the demo uses extractive answers. The encrypted key is tied to the Windows user. `.tooling/`, `.env` files, local databases, and virtual environments are excluded from Git. Never commit credentials or real applicant data.

With Groq enabled, questions and authorized policy passages may be sent to the provider; use synthetic data and review the source. A citation or exact quotation does not establish that an answer is relevant or complete. See the [README](README.md) for experimental settings and limits.

See [Google integration](docs/google-integration.md) for OAuth setup and permissions. A synthetic private Calendar event was created and removed through the application. Actual Gmail delivery has not been tested end-to-end. External actions require separate HR review and confirmation; enabling the integration does not send anything by itself.

## Create a local administrator

The demo does not create an administrator with a default password. Stop the server and run from `backend`:

```powershell
uv run --locked python -m app.admin_users --database tawzeefai.sqlite3 --username localadmin --display-name "Local Admin"
```

The command prompts interactively for a new password twice and works only before an administrator exists. The administrator has the employee role plus a separate account-management capability; this does not grant HR access to candidate records. Account changes revoke sessions for subsequent requests, although operations already in progress may finish.

## Remaining work

The local demo implements parts of the specified workflow, but the full specialized-agent architecture, reliable policy answers, independent human evaluation, manual time comparison, and production deployment remain incomplete or unverified. SQLite stores primary application data; the optional experimental policy retrieval index uses PostgreSQL/pgvector. Review the [README](README.md) and the tests for current evidence rather than treating setup or installed tools as proof of project completion.
