# Local development demo. Do not expose this server to the public network.
$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$credentialPath = Join-Path $projectRoot '.tooling\groq-key.dpapi'
$env:TAWZEEF_DEMO = '1'
$env:TAWZEEF_ENABLE_GROQ = '0'
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
    uv run --locked uvicorn app.main:app --host 127.0.0.1 --port 8000
} finally {
    Remove-Item Env:GROQ_API_KEY -ErrorAction SilentlyContinue
    Pop-Location
}
