# db-schema-tools

Monorepo with two components for SQL Server schema management:

1. **extractor** — Connects to SQL Server, extracts metadata for all tables to JSON files
2. **mcp** — MCP server that loads those JSONs and exposes 7 tools for on-demand schema queries

## Prerequisites

- Docker
- Azure CLI (`az login`) if using Azure AD authentication

## Quick start

```powershell
# 1. Configure connection
cp .env.example .env
# Edit .env with your database connection details

# 2. Extract schemas
./run.ps1

# 3. Setup MCP for Claude Code (once per machine)
./setup-mcp.ps1
```

## Project structure

```
db-schema-tools/
├── extractor/
│   ├── extract.py          # Schema extraction logic
│   ├── Dockerfile          # Python 3.12 + ODBC Driver 18
│   └── requirements.txt    # pyodbc, PyYAML, azure-identity
├── mcp/
│   ├── server.py           # MCP server with 7 tools
│   ├── Dockerfile          # Python 3.12-slim (no ODBC needed)
│   └── requirements.txt    # mcp>=1.26.0
├── output/                 # Shared: extractor writes, MCP reads
├── .mcp.json               # Claude Code MCP config (uses $DB_SCHEMA_OUTPUT)
├── setup-mcp.ps1           # One-time setup: builds image + sets env var
├── docker-compose.yml      # Both services
├── run.ps1                 # Runs extractor with Azure AD token
├── config.yaml             # Extraction configuration
├── .env                    # Connection config (gitignored)
└── .env.example            # Template for .env
```

## Extractor

Extracts SQL Server metadata (tables, columns, PKs, FKs, indexes, constraints, row counts) to organized JSON files.

### Authentication

**Azure AD (default):**

```env
DB_AUTH=ActiveDirectoryDefault
```

```powershell
az login
./run.ps1
```

**SQL Server authentication:**

```env
DB_AUTH=SqlPassword
DB_USER=sa
DB_PASSWORD=your-password
```

```powershell
docker compose up --build extractor
```

### Configuration

See `config.yaml` for extraction options: schema/table filters, metadata toggles, output settings.

### Output

```
output/
├── _agent_summary.json     # Compact AI-friendly summary
├── _full_schema.json       # Complete schema (all tables)
├── dbo/
│   ├── _index.json         # Schema index with stats
│   ├── Transaction.json    # Per-table details
│   └── ...
└── {schema}/...
```

## MCP Server

Serves schema data on-demand via 7 tools. Builds 4 in-memory indices at startup from the JSON files in `output/`.

### Tools

| Tool | Description |
|---|---|
| `list_schemas()` | All schemas with table count and row totals |
| `list_tables(schema)` | Tables in a schema with basic stats |
| `get_table_schema(schema, table)` | Full detail: columns, PK, FKs, indexes |
| `search_tables(pattern)` | Find tables by name (substring match) |
| `search_columns(column_name)` | Find which tables have a specific column |
| `get_relationships(schema, table)` | Outgoing + incoming foreign keys |
| `get_schema_overview(schema)` | Compact view: table name + PK only |

### Claude Code integration

Una sola vez por máquina:

```powershell
./setup-mcp.ps1
```

Esto:
1. Construye la imagen Docker `db-schema-tools-mcp`
2. Registra el MCP server a nivel **usuario** (`--scope user`) — disponible en **todos** los proyectos

### Verificar

```powershell
claude mcp list
```

### Ejemplo de uso (desde cualquier proyecto)

- "¿Qué tablas tienen la columna StoreId?"
- "Muéstrame el schema de dbo.Transaction"
- "¿Cuáles son las relaciones de dbo.Store?"
