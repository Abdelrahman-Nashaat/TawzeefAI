# Local development demo. Do not expose this server to the public network.
param([switch]$Google, [switch]$SemanticScreening, [switch]$Ocr)
$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$modelDir = Join-Path $projectRoot '.tooling\multilingual-e5-small'
if ($SemanticScreening -and (-not (Test-Path -LiteralPath (Join-Path $modelDir 'model.onnx')) -or
    -not (Test-Path -LiteralPath (Join-Path $modelDir 'tokenizer.json')))) {
    throw 'The local embedding model is missing; see docs/local-semantic-experiment.md.'
}
$priorSemantic = [Environment]::GetEnvironmentVariable('TAWZEEF_ENABLE_SEMANTIC_SCREENING', 'Process')
$priorModel = [Environment]::GetEnvironmentVariable('TAWZEEF_EMBEDDING_MODEL_DIR', 'Process')
$priorOcr = [Environment]::GetEnvironmentVariable('TAWZEEF_ENABLE_OCR', 'Process')
$priorTessdata = [Environment]::GetEnvironmentVariable('TESSDATA_PREFIX', 'Process')
$localTessdata = Join-Path $projectRoot '.tooling\tessdata'
if ($Ocr -and -not $priorTessdata -and (Test-Path -LiteralPath (Join-Path $localTessdata 'eng.traineddata')) -and
    (Test-Path -LiteralPath (Join-Path $localTessdata 'ara.traineddata'))) { $env:TESSDATA_PREFIX = $localTessdata }
$env:TAWZEEF_ENABLE_OCR = if ($Ocr) { '1' } else { '0' }
$env:TAWZEEF_ENABLE_SEMANTIC_SCREENING = if ($SemanticScreening) { '1' } else { '0' }
if ($SemanticScreening) { $env:TAWZEEF_EMBEDDING_MODEL_DIR = $modelDir }
$credentialPath = Join-Path $projectRoot '.tooling\groq-key.dpapi'
$env:TAWZEEF_DEMO = '1'
$env:TAWZEEF_ENABLE_GROQ = '0'
$env:TAWZEEF_ENABLE_GOOGLE = if ($Google) { '1' } else { '0' }
if (Test-Path -LiteralPath $credentialPath) {
    try {
        $secureKey = (Get-Content -Raw -LiteralPath $credentialPath).Trim() | ConvertTo-SecureString
        $env:GROQ_API_KEY = [System.Net.NetworkCredential]::new('', $secureKey).Password
        $env:TAWZEEF_ENABLE_GROQ = '1'
        Write-Host 'Groq enabled for this local demo. Use synthetic data only.'
    } catch {
        Write-Warning 'Could not load the Windows-user-protected Groq key. Starting with extractive answers.'
    }
}
Push-Location (Join-Path $projectRoot 'backend')
try {
    Write-Host 'Starting TawzeefAI at http://127.0.0.1:8000; press Ctrl+C to stop.'
    if ($Google) { Write-Host 'Google delivery enabled for explicit HR actions on this Windows account.' }
    if ($SemanticScreening) { Write-Host 'Experimental local semantic screening enabled; HR review is required.' }
    if ($Ocr) { Write-Host 'Local OCR enabled for explicitly selected uploads; OCR text requires review.' }
    $runArgs = @('run', '--locked')
    if ($Google) { $runArgs += @('--extra', 'google') }
    if ($SemanticScreening) { $runArgs += @('--extra', 'semantic') }
    if ($Ocr) { $runArgs += @('--extra', 'ocr') }
    $runArgs += @('uvicorn', 'app.main:app', '--host', '127.0.0.1', '--port', '8000')
    & uv @runArgs
    if ($LASTEXITCODE -ne 0) { throw 'The local server failed.' }
} finally {
    Remove-Item Env:GROQ_API_KEY -ErrorAction SilentlyContinue
    [Environment]::SetEnvironmentVariable('TAWZEEF_ENABLE_SEMANTIC_SCREENING', $priorSemantic, 'Process')
    [Environment]::SetEnvironmentVariable('TAWZEEF_EMBEDDING_MODEL_DIR', $priorModel, 'Process')
    [Environment]::SetEnvironmentVariable('TAWZEEF_ENABLE_OCR', $priorOcr, 'Process')
    [Environment]::SetEnvironmentVariable('TESSDATA_PREFIX', $priorTessdata, 'Process')
    Pop-Location
}
