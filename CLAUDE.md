# db-schema-tools — Contexto del proyecto

## Qué es
Monorepo con dos componentes:
1. **extractor/** — Python + pyodbc, se conecta a SQL Server (tech-01-srvdb.database.windows.net / tech-01-db), extrae metadata de 561 tablas a JSONs organizados por schema
2. **mcp/** — Servidor MCP (Model Context Protocol) que carga esos JSONs en memoria y expone 7 tools via stdio para que Claude Code consulte el schema on-demand

## Arquitectura

```
setup-mcp.ps1  →  docker compose build mcp  →  claude mcp add (user scope)
run.ps1        →  az token + docker compose up extractor  →  output/*.json
mcp/run.cmd    →  docker run --rm -i -v output:/data:ro  →  stdio MCP server
```

## Decisiones técnicas tomadas

### MCP Server (mcp/server.py)
- 4 índices en memoria al startup: `tables[schema][table]`, `column_index`, `incoming_fks`, `table_name_lookup`
- 7 tools: `list_schemas`, `list_tables`, `get_table_schema`, `search_tables`, `search_columns`, `get_relationships`, `get_schema_overview`
- Usa `FastMCP` de `mcp>=1.26.0` con `transport="stdio"`
- Docker image: `python:3.12-slim` sin ODBC (no necesita BD, solo lee JSONs)

### Registro en Claude Code
- Se registra a nivel **usuario** (global) via `claude mcp add --scope user` → queda en `~/.claude.json`
- NO en `~/.claude/settings.json` (ese es para settings generales, no MCP)
- `mcp/run.cmd` es un wrapper que usa `%~dp0` para resolver paths relativamente → evita hardcodear paths
- `setup-mcp.ps1` llama a `claude mcp add` que resuelve el path absoluto de cada máquina al momento del setup

### Lecciones aprendidas (bugs encontrados)
- `claude mcp add ... -- docker run --rm -i ...` falla porque `--rm` se parsea como flag de `claude mcp add` → solución: wrapper `run.cmd`
- PowerShell 5 `ConvertTo-Json` genera indentación aberrante y doble espacio después de `:` → no confiar en él para archivos que otros tools consumen
- PowerShell 5 `-Encoding UTF8` escribe con BOM (EF BB BF) → rompe parsers JSON → usar `[System.IO.File]::WriteAllText()` con `UTF8Encoding($false)`
- PowerShell 5 `Join-Path` no acepta 3+ argumentos → usar `Join-Path (Join-Path ...)` anidado

## Archivos clave
- `extractor/extract.py` — Lógica de extracción (557 líneas), queries a sys.tables/columns/indexes/foreign_keys
- `mcp/server.py` — Servidor MCP (~200 líneas)
- `mcp/run.cmd` — Wrapper para docker run (evita problemas de parsing)
- `setup-mcp.ps1` — Setup automático: build + registro global
- `run.ps1` — Obtiene Azure AD token + ejecuta extractor
- `config.yaml` — Filtros de extracción (schemas, tablas, metadata toggles)
- `.env` — Conexión a BD (gitignored)
- `docker-compose.yml` — Servicios extractor + mcp

## Estado actual
- Extractor: funcional, 561 tablas extraídas en output/
- MCP Server: funcional, probado con smoke tests (list_schemas, search_columns)
- Registro global: configurado via setup-mcp.ps1
- Pendiente: verificar que funciona desde otro proyecto tras reiniciar Claude Code

## Comunicación
- Preferencia del usuario: español
