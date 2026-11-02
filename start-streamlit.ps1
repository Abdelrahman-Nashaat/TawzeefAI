# Optional local UI. Start the FastAPI demo separately; never expose demo accounts.
param([ValidateRange(1,65535)][int]$ApiPort = 8000)
$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$priorPort = [Environment]::GetEnvironmentVariable('TAWZEEF_STREAMLIT_API_PORT', 'Process')
$priorProjectEnvironment = [Environment]::GetEnvironmentVariable('UV_PROJECT_ENVIRONMENT', 'Process')
$env:TAWZEEF_STREAMLIT_API_PORT = [string]$ApiPort
$env:UV_PROJECT_ENVIRONMENT = Join-Path $projectRoot '.tooling\streamlit-venv'
Push-Location (Join-Path $projectRoot 'backend')
try {
    Write-Host 'Streamlit local demo: http://127.0.0.1:8501. Start the FastAPI demo separately.'
    & uv run --locked --extra streamlit streamlit run streamlit_app.py --server.address=127.0.0.1 --server.port=8501 --server.enableCORS=true --server.enableXsrfProtection=true --browser.gatherUsageStats=false --server.headless=true
    if ($LASTEXITCODE -ne 0) { throw 'The Streamlit server failed.' }
} finally {
    [Environment]::SetEnvironmentVariable('TAWZEEF_STREAMLIT_API_PORT', $priorPort, 'Process')
    [Environment]::SetEnvironmentVariable('UV_PROJECT_ENVIRONMENT', $priorProjectEnvironment, 'Process')
    Pop-Location
}
