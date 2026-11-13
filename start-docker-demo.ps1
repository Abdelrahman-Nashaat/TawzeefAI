# Local demonstration only. Docker Desktop must already be running.
param([switch]$Hybrid, [switch]$Postgres)
$ErrorActionPreference = 'Stop'
if ($Hybrid -and $Postgres) { throw 'Choose either -Hybrid or -Postgres; the PostgreSQL demo uses lexical search.' }
$projectRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$dockerCommand = Get-Command docker.exe -ErrorAction SilentlyContinue
if ($dockerCommand) {
    $dockerExe = $dockerCommand.Source
} else {
    $dockerExe = Join-Path $env:LOCALAPPDATA 'Programs\DockerDesktop\resources\bin\docker.exe'
    if (-not (Test-Path -LiteralPath $dockerExe)) { throw 'Docker CLI not found. Install or open Docker Desktop.' }
}
$env:PATH = "$(Split-Path -Parent $dockerExe);$env:PATH"
& $dockerExe info --format '{{.ServerVersion}}' | Out-Null
if ($LASTEXITCODE -ne 0) { throw 'Docker engine is unavailable. Open Docker Desktop and wait for it to start.' }
$tooling = Join-Path $projectRoot '.tooling'
New-Item -ItemType Directory -Force -Path $tooling | Out-Null
$secretsFile = Join-Path $tooling 'docker-postgres.env'
if (-not (Test-Path -LiteralPath $secretsFile)) {
    $bytes = New-Object byte[] 32
    $random = [Security.Cryptography.RandomNumberGenerator]::Create()
    try { $random.GetBytes($bytes) } finally { $random.Dispose() }
    $password = [Convert]::ToBase64String($bytes).Replace('+', 'A').Replace('/', 'B').TrimEnd('=')
    [IO.File]::WriteAllText($secretsFile, "TAWZEEF_POSTGRES_PASSWORD=$password`n", [Text.UTF8Encoding]::new($false))
}
Push-Location $projectRoot
try {
    if ($Postgres) {
        $composeArgs = @('compose', '--env-file', $secretsFile, '--project-name', 'tawzeefai-postgres-local', '-f', 'docker/compose.postgres-demo.yaml')
    } else {
        $composeArgs = @('compose', '--env-file', $secretsFile, '--project-name', 'tawzeefai-local', '-f', 'compose.yaml')
    }
    if ($Hybrid) {
        $modelDir = Join-Path $projectRoot '.tooling\multilingual-e5-small'
        if (-not (Test-Path -LiteralPath (Join-Path $modelDir 'model.onnx')) -or
            -not (Test-Path -LiteralPath (Join-Path $modelDir 'tokenizer.json'))) {
            throw 'The local embedding model is missing; see docs/local-semantic-experiment.md.'
        }
        $composeArgs += @('-f', 'docker/compose.hybrid.yaml')
    }
    & $dockerExe @composeArgs up -d --build
    if ($LASTEXITCODE -ne 0) { throw 'Docker Compose failed; check the output above.' }
    if ($Postgres) {
        Write-Host 'Local PostgreSQL demo: http://127.0.0.1:18001/ (synthetic data only).'
    } elseif ($Hybrid) {
        Write-Host 'Local demo: http://127.0.0.1:18000/  (experimental PostgreSQL hybrid search enabled.)'
    } else {
        Write-Host 'Local demo: http://127.0.0.1:18000/  (PostgreSQL is not yet connected to the app.)'
    }
} finally { Pop-Location }
