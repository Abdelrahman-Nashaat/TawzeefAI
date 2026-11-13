# Synthetic localhost demo. Set TAWZEEF_POSTGRES_DSN for a disposable database first.
param(
    [switch]$Groq,
    [switch]$Hybrid,
    [ValidateRange(1024, 65535)][int]$Port = 18080
)
$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
if (-not $env:TAWZEEF_POSTGRES_DSN) { throw 'Set TAWZEEF_POSTGRES_DSN privately for a disposable PostgreSQL database.' }
if (-not $env:TAWZEEF_POSTGRES_SCHEMA) { throw 'Set TAWZEEF_POSTGRES_SCHEMA to a new experimental schema name.' }
$modelDir = Join-Path $projectRoot '.tooling\multilingual-e5-small'
if ($Hybrid -and (-not (Test-Path -LiteralPath (Join-Path $modelDir 'model.onnx')) -or
    -not (Test-Path -LiteralPath (Join-Path $modelDir 'tokenizer.json')))) {
    throw 'The local embedding model is missing; see docs/local-semantic-experiment.md.'
}
$names = @('TAWZEEF_DATABASE_ENGINE', 'TAWZEEF_DEMO', 'TAWZEEF_DEMO_GATEWAY_IP',
    'TAWZEEF_POSTGRES_ENABLE_GROQ', 'GROQ_API_KEY', 'TAWZEEF_POSTGRES_KNOWLEDGE_HYBRID',
    'TAWZEEF_EMBEDDING_MODEL_DIR')
$prior = @{}
foreach ($name in $names) { $prior[$name] = [Environment]::GetEnvironmentVariable($name, 'Process') }
try {
    $env:TAWZEEF_DATABASE_ENGINE = 'postgres-experimental'
    $env:TAWZEEF_DEMO = '1'
    Remove-Item Env:TAWZEEF_DEMO_GATEWAY_IP -ErrorAction SilentlyContinue
    $env:TAWZEEF_POSTGRES_ENABLE_GROQ = '0'
    $env:TAWZEEF_POSTGRES_KNOWLEDGE_HYBRID = '0'
    if ($Groq) {
        $credentialPath = Join-Path $projectRoot '.tooling\groq-key.dpapi'
        if (-not (Test-Path -LiteralPath $credentialPath)) { throw 'The protected Groq credential was not found.' }
        try {
            $secureKey = (Get-Content -Raw -LiteralPath $credentialPath).Trim() | ConvertTo-SecureString
            $env:GROQ_API_KEY = [System.Net.NetworkCredential]::new('', $secureKey).Password
        } catch { throw 'The protected Groq credential could not be loaded for this Windows user.' }
        if (-not $env:GROQ_API_KEY) { throw 'The protected Groq credential is empty.' }
        $env:TAWZEEF_POSTGRES_ENABLE_GROQ = '1'
    }
    if ($Hybrid) {
        $env:TAWZEEF_POSTGRES_KNOWLEDGE_HYBRID = '1'
        $env:TAWZEEF_EMBEDDING_MODEL_DIR = $modelDir
    }
    $arguments = @('run', '--locked', '--extra', 'postgres')
    if ($Hybrid) { $arguments += @('--extra', 'semantic') }
    $arguments += @('uvicorn', 'app.main:app', '--host', '127.0.0.1', '--port', "$Port", '--no-proxy-headers')
    Push-Location (Join-Path $projectRoot 'backend')
    try {
        Write-Host "Synthetic PostgreSQL demo: http://127.0.0.1:$Port/; press Ctrl+C to stop."
        & uv @arguments
        if ($LASTEXITCODE -ne 0) { throw 'The local PostgreSQL demo failed.' }
    } finally { Pop-Location }
} finally {
    foreach ($name in $names) { [Environment]::SetEnvironmentVariable($name, $prior[$name], 'Process') }
}
