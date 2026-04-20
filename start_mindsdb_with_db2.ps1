# MindsDB Startup Script with DB2 Support
# This script sets up the required environment variables and DLL paths for DB2 before starting MindsDB

Write-Host "=== MindsDB Startup with DB2 Support ===" -ForegroundColor Cyan

# Set SSL Certificate paths (fixes Google Gemini SSL errors in corporate environments)
Write-Host "`nConfiguring SSL certificates..." -ForegroundColor Yellow
$certifiPath = "C:\Gourav\Workspace\o-workspace\mindsdb\mindsdb-venv\Lib\site-packages\certifi\cacert.pem"
$env:SSL_CERT_FILE = $certifiPath
$env:REQUESTS_CA_BUNDLE = $certifiPath
$env:GRPC_DEFAULT_SSL_ROOTS_FILE_PATH = $certifiPath
$env:CURL_CA_BUNDLE = $certifiPath

# Set Google Cloud service account for Vertex AI / Knowledge Base embeddings
$env:GOOGLE_APPLICATION_CREDENTIALS = "C:\Gourav\Company\Document\GCP\Credential\vertex-ai.json"

# Disable ChromaDB telemetry (avoids PostHog version mismatch errors)
$env:ANONYMIZED_TELEMETRY = "False"

Write-Host "  SSL_CERT_FILE = $env:SSL_CERT_FILE" -ForegroundColor Green
Write-Host "  GRPC_DEFAULT_SSL_ROOTS_FILE_PATH = $env:GRPC_DEFAULT_SSL_ROOTS_FILE_PATH" -ForegroundColor Green
Write-Host "  GOOGLE_APPLICATION_CREDENTIALS = $env:GOOGLE_APPLICATION_CREDENTIALS" -ForegroundColor Green

# Set DB2 CLI Driver paths
Write-Host "`nConfiguring DB2 CLI driver..." -ForegroundColor Yellow
$clidriverRoot = "C:\Gourav\Company\Software\v11.5.9_ntx64_odbc_cli\clidriver"
$clidriverBin = Join-Path $clidriverRoot "bin"
$icc64Path = Join-Path $clidriverBin "icc64"
$iccPath = Join-Path $clidriverBin "icc"

# Verify paths exist
if (-not (Test-Path $clidriverBin)) {
    Write-Host "ERROR: DB2 CLI driver not found at: $clidriverRoot" -ForegroundColor Red
    exit 1
}

# Set environment variables for the current session
$env:IBM_DB_HOME = $clidriverRoot
$env:DB2_HOME = $clidriverRoot
$env:DB2DSDRIVER_CFG_PATH = Join-Path $clidriverRoot "cfg"

# Prepend ICC paths to PATH for this session (critical for DLL loading)
$env:PATH = "$icc64Path;$iccPath;$clidriverBin;" + $env:PATH

Write-Host "  IBM_DB_HOME = $env:IBM_DB_HOME" -ForegroundColor Green
Write-Host "  DB2_HOME = $env:DB2_HOME" -ForegroundColor Green
Write-Host "  DB2DSDRIVER_CFG_PATH = $env:DB2DSDRIVER_CFG_PATH" -ForegroundColor Green
Write-Host "  Added to PATH:" -ForegroundColor Green
Write-Host "    - $icc64Path" -ForegroundColor Green
Write-Host "    - $iccPath" -ForegroundColor Green
Write-Host "    - $clidriverBin" -ForegroundColor Green

Write-Host "`nActivating virtual environment..." -ForegroundColor Yellow
& "C:\Gourav\Workspace\o-workspace\mindsdb\mindsdb-venv\Scripts\Activate.ps1"

# Set up log file with timestamp
$logsDir = "C:\Gourav\Workspace\o-workspace\mindsdb\logs"
if (-not (Test-Path $logsDir)) {
    New-Item -ItemType Directory -Path $logsDir -Force | Out-Null
    Write-Host "  Created logs directory: $logsDir" -ForegroundColor Green
}
$timestamp = Get-Date -Format "yyyy-MM-dd_HH-mm-ss"
$logFile = Join-Path $logsDir "mindsdb_$timestamp.log"

Write-Host "`nStarting MindsDB..." -ForegroundColor Yellow
Write-Host "  Log file: $logFile" -ForegroundColor Green
Write-Host "(Press Ctrl+C to stop)`n" -ForegroundColor Gray

# Start MindsDB - output goes to both console and log file
python -m mindsdb 2>&1 | Tee-Object -FilePath $logFile
