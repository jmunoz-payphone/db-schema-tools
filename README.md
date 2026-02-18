# db-schema-tools

Monorepo con dos componentes para gestionar el schema de SQL Server:

1. **extractor** — Se conecta a SQL Server, extrae metadata de todas las tablas a archivos JSON
2. **mcp** — Servidor MCP que carga los JSONs y expone 7 tools para consultas on-demand

## Requisitos

- Docker
- Azure CLI (`az login`) si se usa autenticación Azure AD

## Inicio rápido

```powershell
# 1. Configurar conexión
cp .env.example .env
# Editar .env con los datos de tu base de datos

# 2. Extraer schemas
./run.ps1

# 3. Configurar MCP en Claude Code (una vez por máquina)
./setup-mcp.ps1
```

## Estructura del proyecto

```
db-schema-tools/
├── extractor/
│   ├── extract.py          # Lógica de extracción
│   ├── Dockerfile          # Python 3.12 + ODBC Driver 18
│   └── requirements.txt    # pyodbc, PyYAML, azure-identity
├── mcp/
│   ├── server.py           # Servidor MCP con 7 tools
│   ├── run.cmd             # Wrapper para ejecutar el container
│   ├── Dockerfile          # Python 3.12-slim (sin ODBC)
│   └── requirements.txt    # mcp>=1.26.0
├── output/                 # Compartido: extractor escribe, MCP lee
├── setup-mcp.ps1           # Setup: construye imagen + registra en Claude Code
├── docker-compose.yml      # Ambos servicios
├── run.ps1                 # Ejecuta extractor con token Azure AD
├── config.yaml             # Configuración de extracción
├── .env                    # Conexión a BD (gitignored)
└── .env.example            # Template para .env
```

## Extractor

Extrae metadata de SQL Server (tablas, columnas, PKs, FKs, índices, constraints, row counts) a archivos JSON organizados por schema.

### Autenticación

**Azure AD (por defecto):**

```env
DB_AUTH=ActiveDirectoryDefault
```

```powershell
az login
./run.ps1
```

**Autenticación SQL Server:**

```env
DB_AUTH=SqlPassword
DB_USER=sa
DB_PASSWORD=tu-password
```

```powershell
docker compose up --build extractor
```

### Configuración

Ver `config.yaml` para opciones de extracción: filtros de schemas/tablas, toggles de metadata, opciones de output.

### Output

```
output/
├── _agent_summary.json     # Resumen compacto para agentes AI
├── _full_schema.json       # Schema completo (todas las tablas)
├── dbo/
│   ├── _index.json         # Índice del schema con stats
│   ├── Transaction.json    # Detalle por tabla
│   └── ...
└── {schema}/...
```

## Servidor MCP

Sirve datos de schema on-demand via 7 tools. Construye 4 índices en memoria al iniciar a partir de los JSONs en `output/`.

### Tools

| Tool | Descripción |
|---|---|
| `list_schemas()` | Todos los schemas con cantidad de tablas y total de filas |
| `list_tables(schema)` | Tablas de un schema con stats básicas |
| `get_table_schema(schema, table)` | Detalle completo: columnas, PK, FKs, índices |
| `search_tables(pattern)` | Buscar tablas por nombre (substring) |
| `search_columns(column_name)` | Encontrar qué tablas tienen una columna específica |
| `get_relationships(schema, table)` | FKs salientes + entrantes |
| `get_schema_overview(schema)` | Vista compacta: nombre de tabla + PK |

### Integración con Claude Code

Una sola vez por máquina:

```powershell
./setup-mcp.ps1
```

Esto:
1. Construye la imagen Docker `db-schema-tools-mcp`
2. Registra el MCP server a nivel **usuario** via `claude mcp add` — disponible en **todos** los proyectos

No queda nada hardcodeado en el código fuente. El script `run.cmd` usa `%~dp0` para resolver paths relativos al proyecto, y `claude mcp add` resuelve el path absoluto de cada máquina al momento del setup.

### Verificar

```powershell
claude mcp list
```

### Ejemplo de uso (desde cualquier proyecto)

- "¿Qué tablas tienen la columna StoreId?"
- "Muéstrame el schema de dbo.Transaction"
- "¿Cuáles son las relaciones de dbo.Store?"
