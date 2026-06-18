param(
    [string]$PythonPath = ""
)

$ErrorActionPreference = "Stop"

$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
$Python = $PythonPath
if (-not $Python) {
    $Python = Join-Path $Root ".venv\Scripts\python.exe"
}
if (($Python -ne "python") -and (-not (Test-Path $Python))) {
    $Python = "python"
}

Push-Location $Root
try {
    $env:DJANGO_ENV = "desktop"
    $env:DJANGO_DEBUG = "false"
    $env:DJANGO_ALLOWED_HOSTS = "127.0.0.1,localhost"
    $env:DJANGO_SECRET_KEY = "desktop-build-only-secret-key-not-used-for-production"
    $env:DJANGO_DB_NAME = (Join-Path $Root ".runtime\desktop-build.sqlite3")
    $env:SOLARCONTROL_DATA_DIR = (Join-Path $Root ".runtime\desktop-build-data")
    $env:DJANGO_MEDIA_ROOT = (Join-Path $Root ".runtime\desktop-build-media")
    $env:DJANGO_STATIC_ROOT = (Join-Path $Root "staticfiles")

    & $Python manage.py collectstatic --no-input
    if ($LASTEXITCODE -ne 0) { throw "collectstatic failed with exit code $LASTEXITCODE" }

    & $Python -m PyInstaller --noconfirm --clean BrazSolarScan.spec
    if ($LASTEXITCODE -ne 0) { throw "PyInstaller failed with exit code $LASTEXITCODE" }

    Copy-Item -LiteralPath (Join-Path $Root "desktop.env.example") -Destination (Join-Path $Root "dist\BrazSolarScan\desktop.env.example") -Force

    $ArtifactDir = Join-Path $Root "artifacts"
    New-Item -ItemType Directory -Path $ArtifactDir -Force | Out-Null
    $ZipPath = Join-Path $ArtifactDir "BrazSolarScan-Windows.zip"
    if (Test-Path $ZipPath) { Remove-Item -LiteralPath $ZipPath -Force }
    Compress-Archive -Path (Join-Path $Root "dist\BrazSolarScan\*") -DestinationPath $ZipPath -CompressionLevel Optimal

    Write-Host "Executavel: $(Join-Path $Root 'dist\BrazSolarScan\BrazSolarScan.exe')"
    Write-Host "Pacote: $ZipPath"
}
finally {
    Pop-Location
}
