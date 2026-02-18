# Get Azure AD access token from host's az login session
# and pass it to the Docker container
Write-Host "Getting Azure AD token for SQL Database..." -ForegroundColor Cyan
$token = az account get-access-token --resource https://database.windows.net/ --query accessToken -o tsv

if (-not $token) {
    Write-Host "ERROR: Could not get Azure AD token. Run 'az login' first." -ForegroundColor Red
    exit 1
}

Write-Host "Token acquired. Starting extraction..." -ForegroundColor Green

$env:AZURE_SQL_TOKEN = $token
docker compose up --build extractor
$env:AZURE_SQL_TOKEN = $null
