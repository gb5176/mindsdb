@echo off
REM MindsDB Startup Batch Script for DB2 Support
REM Sets environment variables before starting MindsDB

echo === MindsDB Startup with DB2 Support ===
echo.

REM Set SSL Certificate paths (fixes Google Gemini SSL errors in corporate environments)
set SSL_CERT_FILE=C:\Gourav\Workspace\o-workspace\mindsdb\mindsdb-venv\Lib\site-packages\certifi\cacert.pem
set REQUESTS_CA_BUNDLE=C:\Gourav\Workspace\o-workspace\mindsdb\mindsdb-venv\Lib\site-packages\certifi\cacert.pem
set GRPC_DEFAULT_SSL_ROOTS_FILE_PATH=C:\Gourav\Workspace\o-workspace\mindsdb\mindsdb-venv\Lib\site-packages\certifi\cacert.pem
set CURL_CA_BUNDLE=C:\Gourav\Workspace\o-workspace\mindsdb\mindsdb-venv\Lib\site-packages\certifi\cacert.pem

REM Set Google Cloud service account for Vertex AI / Knowledge Base embeddings
set GOOGLE_APPLICATION_CREDENTIALS=C:\Gourav\Company\Document\GCP\Credential\vertex-ai.json

REM Disable ChromaDB telemetry (avoids PostHog version mismatch errors)
set ANONYMIZED_TELEMETRY=False

REM Set DB2 CLI Driver paths
set CLIDRIVER_ROOT=C:\Gourav\Company\Software\v11.5.9_ntx64_odbc_cli\clidriver
set IBM_DB_HOME=%CLIDRIVER_ROOT%
set DB2_HOME=%CLIDRIVER_ROOT%
set DB2DSDRIVER_CFG_PATH=%CLIDRIVER_ROOT%\cfg

REM Prepend ICC paths to PATH (critical for DLL loading)
set PATH=%CLIDRIVER_ROOT%\bin\icc64;%CLIDRIVER_ROOT%\bin\icc;%CLIDRIVER_ROOT%\bin;%PATH%

echo Environment configured:
echo   SSL_CERT_FILE = %SSL_CERT_FILE%
echo   GRPC_DEFAULT_SSL_ROOTS_FILE_PATH = %GRPC_DEFAULT_SSL_ROOTS_FILE_PATH%
echo   GOOGLE_APPLICATION_CREDENTIALS = %GOOGLE_APPLICATION_CREDENTIALS%
echo   IBM_DB_HOME = %IBM_DB_HOME%
echo   DB2_HOME = %DB2_HOME%
echo   DB2DSDRIVER_CFG_PATH = %DB2DSDRIVER_CFG_PATH%
echo   Added to PATH: clidriver\bin\icc64, icc, and bin
echo.

echo Activating virtual environment...
call C:\Gourav\Workspace\o-workspace\mindsdb\mindsdb-venv\Scripts\activate.bat

echo.
echo Starting MindsDB...
echo (Press Ctrl+C to stop)
echo.

python -m mindsdb
