# Opt-in, local-only experiment. Start the PostgreSQL Docker service first.
$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$modelDir = Join-Path $projectRoot '.tooling\multilingual-e5-small'
$databaseEnv = Join-Path $projectRoot '.tooling\docker-postgres.env'
if (-not (Test-Path -LiteralPath (Join-Path $modelDir 'model.onnx')) -or
    -not (Test-Path -LiteralPath (Join-Path $modelDir 'tokenizer.json'))) {
    throw 'The local embedding model is missing; see docs/local-semantic-experiment.md.'
}
if (-not (Test-Path -LiteralPath $databaseEnv)) {
    throw 'PostgreSQL credentials are missing; run start-docker-demo.ps1 first.'
}
$entry = (Get-Content -Raw -LiteralPath $databaseEnv).Trim()
if ($entry -notmatch '^TAWZEEF_POSTGRES_PASSWORD=([A-Za-z0-9]+)$') { throw 'Invalid local PostgreSQL environment file.' }
$secret = $Matches[1]
$names = @('PGHOST', 'PGHOSTADDR', 'PGPORT', 'PGDATABASE', 'PGUSER', 'PGPASSWORD', 'PGOPTIONS', 'PGSERVICE', 'PGSERVICEFILE',
           'TAWZEEF_ENABLE_HYBRID', 'TAWZEEF_EMBEDDING_MODEL_DIR', 'TAWZEEF_DEMO',
           'TAWZEEF_ENABLE_GROQ', 'GROQ_API_KEY')
$prior = @{}
foreach ($name in $names) { $prior[$name] = [Environment]::GetEnvironmentVariable($name, 'Process') }
try {
    $env:PGHOST = '127.0.0.1'
    $env:PGPORT = '15432'
    $env:PGDATABASE = 'tawzeefai'
    $env:PGUSER = 'tawzeefai'
    $env:PGPASSWORD = $secret
    Remove-Item Env:PGHOSTADDR, Env:PGOPTIONS, Env:PGSERVICE, Env:PGSERVICEFILE -ErrorAction SilentlyContinue
    $env:TAWZEEF_ENABLE_HYBRID = '1'
    $env:TAWZEEF_EMBEDDING_MODEL_DIR = $modelDir
    $env:TAWZEEF_DEMO = '1'
    $env:TAWZEEF_ENABLE_GROQ = '0'
    $credentialPath = Join-Path $projectRoot '.tooling\groq-key.dpapi'
    if (Test-Path -LiteralPath $credentialPath) {
        try {
            $key = (Get-Content -Raw -LiteralPath $credentialPath).Trim() | ConvertTo-SecureString
            $env:GROQ_API_KEY = [System.Net.NetworkCredential]::new('', $key).Password
            $env:TAWZEEF_ENABLE_GROQ = '1'
        } catch { Write-Warning 'Groq credential unavailable; using extractive answers.' }
    }
    Push-Location (Join-Path $projectRoot 'backend')
    try {
        Write-Host 'Experimental local demo: http://127.0.0.1:8000/; press Ctrl+C to stop.'
        uv run --locked --extra postgres --extra semantic uvicorn app.main:app --host 127.0.0.1 --port 8000
        if ($LASTEXITCODE -ne 0) { throw 'The local server failed.' }
    } finally { Pop-Location }
} finally {
    foreach ($name in $names) { [Environment]::SetEnvironmentVariable($name, $prior[$name], 'Process') }
}
