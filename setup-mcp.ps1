# Setup MCP server for Claude Code (global — available in ALL projects)
# Run once per machine after cloning the repo

$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$runCmd = Join-Path (Join-Path $scriptDir "mcp") "run.cmd"

# 1. Build the Docker image
Write-Host "1. Building MCP server image..." -ForegroundColor Cyan
docker compose -f "$scriptDir\docker-compose.yml" build mcp
if ($LASTEXITCODE -ne 0) {
    Write-Host "ERROR: Docker build failed." -ForegroundColor Red
    exit 1
}

# 2. Register globally via claude mcp add (uses run.cmd wrapper to avoid --rm parsing issues)
Write-Host ""
Write-Host "2. Registering MCP server in Claude Code (user scope)..." -ForegroundColor Cyan
claude mcp add db-schema --scope user --transport stdio -- $runCmd

Write-Host ""
Write-Host "Done!" -ForegroundColor Green
Write-Host ""
Write-Host "  Image:    db-schema-tools-mcp" -ForegroundColor Gray
Write-Host "  Wrapper:  $runCmd" -ForegroundColor Gray
Write-Host ""
Write-Host "  The 'db-schema' MCP server is now available in ALL Claude Code projects." -ForegroundColor Gray
Write-Host "  Restart Claude Code and verify with: claude mcp list" -ForegroundColor Yellow
