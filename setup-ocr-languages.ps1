# Download pinned Tesseract language data into this project's ignored tooling directory.
$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$target = Join-Path $projectRoot '.tooling\tessdata'
$revision = '87416418657359cb625c412a48b6e1d6d41c29bd'
$hashes = [ordered]@{
    'eng' = '7D4322BD2A7749724879683FC3912CB542F19906C83BCC1A52132556427170B2'
    'ara' = 'E3206D3DC87FD50C24A0FB9F01838615911D25168F4E64415244B67D2BB3E729'
}
New-Item -ItemType Directory -Force -Path $target | Out-Null
foreach ($language in $hashes.Keys) {
    $path = Join-Path $target "$language.traineddata"
    if (Test-Path -LiteralPath $path) {
        if ((Get-FileHash -LiteralPath $path -Algorithm SHA256).Hash -ne $hashes[$language]) {
            throw "Existing $language language data differs from the pinned file; it was not overwritten."
        }
        continue
    }
    $temporary = Join-Path $target ([IO.Path]::GetRandomFileName())
    try {
        $uri = "https://raw.githubusercontent.com/tesseract-ocr/tessdata_fast/$revision/$language.traineddata"
        Invoke-WebRequest -Uri $uri -OutFile $temporary
        if ((Get-FileHash -LiteralPath $temporary -Algorithm SHA256).Hash -ne $hashes[$language]) {
            throw "Downloaded $language language data failed its SHA-256 check."
        }
        Move-Item -LiteralPath $temporary -Destination $path -ErrorAction Stop
    } finally {
        if (Test-Path -LiteralPath $temporary) { Remove-Item -LiteralPath $temporary }
    }
}
Write-Host "English and Arabic language data verified at $target. Run start-demo.ps1 -Ocr."
