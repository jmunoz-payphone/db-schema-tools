@echo off
docker run --rm -i -v "%~dp0..\output:/data:ro" db-schema-tools-mcp
