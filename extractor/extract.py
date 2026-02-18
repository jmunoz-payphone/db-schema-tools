"""
DB Schema Extractor for SQL Server.
Extracts table schemas to JSON files organized by database schema folders.
Designed to feed AI/DB agents with structured database metadata.
"""

import json
import os
import re
import shutil
import struct
import sys
from pathlib import Path

import pyodbc
import yaml

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

def load_config() -> dict:
    config_path = os.getenv("CONFIG_PATH", "/app/config.yaml")
    with open(config_path, "r", encoding="utf-8") as f:
        cfg = yaml.safe_load(f)

    # Environment variable overrides
    db = cfg.setdefault("database", {})
    db["host"] = os.getenv("DB_HOST", db.get("host", "localhost"))
    db["port"] = int(os.getenv("DB_PORT", db.get("port", 1433)))
    db["database"] = os.getenv("DB_NAME", db.get("database", ""))
    db["driver"] = os.getenv("DB_DRIVER", db.get("driver", "ODBC Driver 18 for SQL Server"))

    ext = cfg.setdefault("extraction", {})
    if os.getenv("EXTRACT_SCHEMAS"):
        ext["schemas"] = [s.strip() for s in os.getenv("EXTRACT_SCHEMAS").split(",") if s.strip()]
    if os.getenv("EXTRACT_TABLES"):
        ext["tables"] = [t.strip() for t in os.getenv("EXTRACT_TABLES").split(",") if t.strip()]

    cfg.setdefault("output", {})
    return cfg


def get_connection_string() -> str:
    """Use DB_CONNECTION_STRING directly if available, otherwise build from parts."""
    conn_str = os.getenv("DB_CONNECTION_STRING", "")
    if conn_str:
        return conn_str
    # Fallback: build from individual vars
    driver = os.getenv("DB_DRIVER", "ODBC Driver 18 for SQL Server")
    host = os.getenv("DB_HOST", "localhost")
    port = os.getenv("DB_PORT", "1433")
    database = os.getenv("DB_NAME", "")
    encrypt = os.getenv("DB_ENCRYPT", "yes")
    trust_cert = os.getenv("DB_TRUST_CERT", "no")
    timeout = os.getenv("DB_TIMEOUT", "30")
    user = os.getenv("DB_USER", "")
    password = os.getenv("DB_PASSWORD", "")

    parts = [
        f"Driver={{{driver}}}",
        f"Server=tcp:{host},{port}",
        f"Database={database}",
        f"Encrypt={encrypt}",
        f"TrustServerCertificate={trust_cert}",
        f"Connection Timeout={timeout}",
    ]
    if user:
        parts.append(f"UID={user}")
        parts.append(f"PWD={password}")

    return ";".join(parts) + ";"


def get_azure_token() -> bytes | None:
    """Get Azure AD access token for SQL Database.

    Priority:
      1. AZURE_SQL_TOKEN env var (injected by run.ps1 from host az login)
      2. DefaultAzureCredential (env vars, managed identity, etc.)
    """
    auth_mode = os.getenv("DB_AUTH", "").lower()
    if auth_mode != "activedirectorydefault":
        return None

    raw_token = os.getenv("AZURE_SQL_TOKEN", "")

    if not raw_token:
        try:
            from azure.identity import DefaultAzureCredential
        except ImportError:
            print("[ERROR] No AZURE_SQL_TOKEN and azure-identity not installed.")
            print("        Run using: ./run.ps1")
            sys.exit(1)

        print("> Acquiring Azure AD token (DefaultAzureCredential) ...")
        credential = DefaultAzureCredential()
        token_obj = credential.get_token("https://database.windows.net/.default")
        raw_token = token_obj.token

    print("> Azure AD token ready.")

    # Pack token for pyodbc: SQL_COPT_SS_ACCESS_TOKEN (1256)
    token_bytes = raw_token.encode("UTF-16-LE")
    return struct.pack(f"<I{len(token_bytes)}s", len(token_bytes), token_bytes)


# ---------------------------------------------------------------------------
# SQL Queries (system views)
# ---------------------------------------------------------------------------

SQL_TABLES = """
SELECT
    s.name  AS schema_name,
    t.name  AS table_name,
    t.type_desc,
    t.create_date,
    t.modify_date
FROM sys.tables t
JOIN sys.schemas s ON t.schema_id = s.schema_id
WHERE t.is_ms_shipped = 0
ORDER BY s.name, t.name
"""

SQL_COLUMNS = """
SELECT
    c.column_id,
    c.name                          AS column_name,
    tp.name                         AS data_type,
    c.max_length,
    c.precision,
    c.scale,
    c.is_nullable,
    c.is_identity,
    c.is_computed,
    cc.definition                   AS computed_definition,
    dc.name                         AS default_name,
    dc.definition                   AS default_value
FROM sys.columns c
JOIN sys.types tp ON c.user_type_id = tp.user_type_id
LEFT JOIN sys.computed_columns cc
    ON cc.object_id = c.object_id AND cc.column_id = c.column_id
LEFT JOIN sys.default_constraints dc
    ON dc.parent_object_id = c.object_id AND dc.parent_column_id = c.column_id
WHERE c.object_id = OBJECT_ID(?)
ORDER BY c.column_id
"""

SQL_PRIMARY_KEY = """
SELECT
    i.name  AS pk_name,
    COL_NAME(ic.object_id, ic.column_id) AS column_name,
    ic.key_ordinal
FROM sys.indexes i
JOIN sys.index_columns ic ON i.object_id = ic.object_id AND i.index_id = ic.index_id
WHERE i.is_primary_key = 1
  AND i.object_id = OBJECT_ID(?)
ORDER BY ic.key_ordinal
"""

SQL_INDEXES = """
SELECT
    i.name                          AS index_name,
    i.type_desc                     AS index_type,
    i.is_unique,
    i.is_primary_key,
    COL_NAME(ic.object_id, ic.column_id) AS column_name,
    ic.key_ordinal,
    ic.is_included_column
FROM sys.indexes i
JOIN sys.index_columns ic ON i.object_id = ic.object_id AND i.index_id = ic.index_id
WHERE i.object_id = OBJECT_ID(?)
  AND i.is_primary_key = 0
  AND i.type > 0
ORDER BY i.name, ic.key_ordinal
"""

SQL_FOREIGN_KEYS = """
SELECT
    fk.name                         AS fk_name,
    COL_NAME(fkc.parent_object_id, fkc.parent_column_id)     AS column_name,
    OBJECT_SCHEMA_NAME(fkc.referenced_object_id)              AS ref_schema,
    OBJECT_NAME(fkc.referenced_object_id)                     AS ref_table,
    COL_NAME(fkc.referenced_object_id, fkc.referenced_column_id) AS ref_column,
    fk.delete_referential_action_desc AS on_delete,
    fk.update_referential_action_desc AS on_update
FROM sys.foreign_keys fk
JOIN sys.foreign_key_columns fkc ON fk.object_id = fkc.constraint_object_id
WHERE fk.parent_object_id = OBJECT_ID(?)
ORDER BY fk.name, fkc.constraint_column_id
"""

SQL_CHECK_CONSTRAINTS = """
SELECT
    cc.name       AS constraint_name,
    cc.definition AS constraint_definition
FROM sys.check_constraints cc
WHERE cc.parent_object_id = OBJECT_ID(?)
ORDER BY cc.name
"""

SQL_ROW_COUNT = """
SELECT SUM(p.rows) AS row_count
FROM sys.partitions p
WHERE p.object_id = OBJECT_ID(?)
  AND p.index_id IN (0, 1)
"""


# ---------------------------------------------------------------------------
# Extraction logic
# ---------------------------------------------------------------------------

def get_qualified_name(schema: str, table: str) -> str:
    return f"[{schema}].[{table}]"


def extract_columns(cursor, qualified: str, cfg_ext: dict) -> list[dict]:
    cursor.execute(SQL_COLUMNS, [qualified])
    columns = []
    for row in cursor.fetchall():
        col = {
            "position": row.column_id,
            "name": row.column_name,
            "data_type": row.data_type,
            "max_length": row.max_length,
            "precision": row.precision,
            "scale": row.scale,
            "is_nullable": row.is_nullable,
            "is_identity": row.is_identity,
        }
        if cfg_ext.get("include_computed_columns", True) and row.is_computed:
            col["is_computed"] = True
            col["computed_definition"] = row.computed_definition
        if cfg_ext.get("include_default_constraints", True) and row.default_value:
            col["default"] = {
                "name": row.default_name,
                "value": row.default_value,
            }
        col["type_display"] = _friendly_type(row)
        columns.append(col)
    return columns


def _friendly_type(row) -> str:
    t = row.data_type.lower()
    if t in ("nvarchar", "nchar", "varchar", "char", "varbinary", "binary"):
        length = "MAX" if row.max_length == -1 else str(
            row.max_length // 2 if t.startswith("n") else row.max_length
        )
        return f"{row.data_type}({length})"
    if t in ("decimal", "numeric"):
        return f"{row.data_type}({row.precision},{row.scale})"
    return row.data_type


def extract_primary_key(cursor, qualified: str) -> dict | None:
    cursor.execute(SQL_PRIMARY_KEY, [qualified])
    rows = cursor.fetchall()
    if not rows:
        return None
    return {
        "name": rows[0].pk_name,
        "columns": [r.column_name for r in rows],
    }


def extract_indexes(cursor, qualified: str) -> list[dict]:
    cursor.execute(SQL_INDEXES, [qualified])
    idx_map: dict[str, dict] = {}
    for row in cursor.fetchall():
        key = row.index_name
        if key not in idx_map:
            idx_map[key] = {
                "name": row.index_name,
                "type": row.index_type,
                "is_unique": row.is_unique,
                "columns": [],
                "included_columns": [],
            }
        if row.is_included_column:
            idx_map[key]["included_columns"].append(row.column_name)
        else:
            idx_map[key]["columns"].append(row.column_name)
    result = list(idx_map.values())
    for idx in result:
        if not idx["included_columns"]:
            del idx["included_columns"]
    return result


def extract_foreign_keys(cursor, qualified: str) -> list[dict]:
    cursor.execute(SQL_FOREIGN_KEYS, [qualified])
    fk_map: dict[str, dict] = {}
    for row in cursor.fetchall():
        key = row.fk_name
        if key not in fk_map:
            fk_map[key] = {
                "name": row.fk_name,
                "columns": [],
                "references": {
                    "schema": row.ref_schema,
                    "table": row.ref_table,
                    "columns": [],
                },
                "on_delete": row.on_delete,
                "on_update": row.on_update,
            }
        fk_map[key]["columns"].append(row.column_name)
        fk_map[key]["references"]["columns"].append(row.ref_column)
    return list(fk_map.values())


def extract_check_constraints(cursor, qualified: str) -> list[dict]:
    cursor.execute(SQL_CHECK_CONSTRAINTS, [qualified])
    return [{"name": r.constraint_name, "definition": r.constraint_definition} for r in cursor.fetchall()]


def extract_row_count(cursor, qualified: str) -> int:
    cursor.execute(SQL_ROW_COUNT, [qualified])
    row = cursor.fetchone()
    return int(row.row_count) if row and row.row_count else 0


def extract_table(cursor, schema: str, table: str, cfg_ext: dict) -> dict:
    qualified = get_qualified_name(schema, table)
    result: dict = {
        "schema": schema,
        "table": table,
        "qualified_name": f"{schema}.{table}",
        "columns": extract_columns(cursor, qualified, cfg_ext),
        "primary_key": extract_primary_key(cursor, qualified),
        "row_count": extract_row_count(cursor, qualified),
    }

    if cfg_ext.get("include_foreign_keys", True):
        fks = extract_foreign_keys(cursor, qualified)
        if fks:
            result["foreign_keys"] = fks

    if cfg_ext.get("include_indexes", True):
        idxs = extract_indexes(cursor, qualified)
        if idxs:
            result["indexes"] = idxs

    if cfg_ext.get("include_check_constraints", True):
        ccs = extract_check_constraints(cursor, qualified)
        if ccs:
            result["check_constraints"] = ccs

    return result


# ---------------------------------------------------------------------------
# Filtering
# ---------------------------------------------------------------------------

def should_extract(schema: str, table: str, cfg_ext: dict) -> bool:
    schemas_filter = cfg_ext.get("schemas", [])
    tables_filter = cfg_ext.get("tables", [])
    exclude = cfg_ext.get("exclude_tables", [])
    qualified = f"{schema}.{table}"

    if qualified in exclude:
        return False
    if tables_filter:
        return qualified in tables_filter
    if schemas_filter:
        return schema in schemas_filter
    return True


# ---------------------------------------------------------------------------
# Output
# ---------------------------------------------------------------------------

def write_json(path: Path, data, pretty: bool):
    path.parent.mkdir(parents=True, exist_ok=True)
    indent = 2 if pretty else None
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=indent, default=str, ensure_ascii=False)


def generate_agent_summary(all_tables: list[dict]) -> dict:
    """Create a compact summary optimized for an AI agent to understand the DB."""
    schemas: dict[str, list] = {}
    relationships: list[dict] = []

    for t in all_tables:
        s = t["schema"]
        schemas.setdefault(s, [])
        table_summary = {
            "table": t["table"],
            "columns": [
                {
                    "name": c["name"],
                    "type": c["type_display"],
                    "nullable": c["is_nullable"],
                    "identity": c["is_identity"],
                }
                for c in t["columns"]
            ],
            "primary_key": [c for c in (t["primary_key"]["columns"] if t["primary_key"] else [])],
            "row_count": t.get("row_count", 0),
        }
        schemas[s].append(table_summary)

        for fk in t.get("foreign_keys", []):
            relationships.append({
                "from": f"{t['schema']}.{t['table']}",
                "from_columns": fk["columns"],
                "to": f"{fk['references']['schema']}.{fk['references']['table']}",
                "to_columns": fk["references"]["columns"],
                "on_delete": fk["on_delete"],
            })

    return {
        "_description": (
            "Database schema summary for AI agent consumption. "
            "Use this to understand table structures, column types, "
            "and relationships before writing or optimizing SQL queries."
        ),
        "schemas": schemas,
        "relationships": relationships,
        "total_tables": len(all_tables),
        "total_relationships": len(relationships),
    }


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    print("=" * 60)
    print("  DB Schema Extractor for SQL Server")
    print("=" * 60)

    cfg = load_config()
    ext_cfg = cfg.get("extraction", {})
    out_cfg = cfg.get("output", {})

    conn_str = get_connection_string()
    token_struct = get_azure_token()
    pretty = out_cfg.get("pretty_print", True)
    output_dir = Path(out_cfg.get("directory", "/app/output"))

    # Show connection info (mask sensitive parts)
    db_host = os.getenv("DB_HOST", cfg["database"]["host"])
    db_name = os.getenv("DB_NAME", cfg["database"]["database"])
    db_port = os.getenv("DB_PORT", cfg["database"]["port"])
    db_auth = os.getenv("DB_AUTH", "SqlPassword")
    print(f"\n> Connecting to {db_host}:{db_port}/{db_name} ...")
    print(f"> Auth mode: {db_auth}")
    print(f"> Connection string: {_mask_connection_string(conn_str)}")

    try:
        if token_struct:
            # Azure AD: pass token via SQL_COPT_SS_ACCESS_TOKEN (1256)
            conn = pyodbc.connect(conn_str, attrs_before={1256: token_struct})
        else:
            conn = pyodbc.connect(conn_str, timeout=30)
    except pyodbc.Error as e:
        print(f"\n[ERROR] Failed to connect to database:\n{e}")
        sys.exit(1)

    cursor = conn.cursor()
    print("> Connected successfully.\n")

    # Clean output dir contents (can't rmtree a Docker mount point)
    if output_dir.exists():
        for item in output_dir.iterdir():
            if item.is_dir():
                shutil.rmtree(item)
            else:
                item.unlink()
    output_dir.mkdir(parents=True, exist_ok=True)

    # Discover tables
    cursor.execute(SQL_TABLES)
    tables_raw = cursor.fetchall()

    tables_to_process = [
        (row.schema_name, row.table_name)
        for row in tables_raw
        if should_extract(row.schema_name, row.table_name, ext_cfg)
    ]

    if not tables_to_process:
        print("[WARN] No tables matched your filters. Check config.yaml.")
        sys.exit(0)

    print(f"> Found {len(tables_to_process)} tables to extract.\n")

    all_tables: list[dict] = []
    schemas_seen: set[str] = set()

    for i, (schema, table) in enumerate(tables_to_process, 1):
        print(f"  [{i}/{len(tables_to_process)}] {schema}.{table}")
        table_data = extract_table(cursor, schema, table, ext_cfg)
        all_tables.append(table_data)
        schemas_seen.add(schema)

        # Write individual table file: output/{schema}/{table}.json
        table_path = output_dir / schema / f"{table}.json"
        write_json(table_path, table_data, pretty)

    # Per-schema index files
    for schema in schemas_seen:
        schema_tables = [t for t in all_tables if t["schema"] == schema]
        index_data = {
            "schema": schema,
            "table_count": len(schema_tables),
            "tables": [
                {
                    "table": t["table"],
                    "columns_count": len(t["columns"]),
                    "row_count": t.get("row_count", 0),
                    "has_primary_key": t["primary_key"] is not None,
                    "foreign_keys_count": len(t.get("foreign_keys", [])),
                    "indexes_count": len(t.get("indexes", [])),
                }
                for t in schema_tables
            ],
        }
        write_json(output_dir / schema / "_index.json", index_data, pretty)

    # Full consolidated schema
    if out_cfg.get("generate_full_schema", True):
        write_json(output_dir / "_full_schema.json", all_tables, pretty)
        print(f"\n> Generated _full_schema.json")

    # Agent summary
    if out_cfg.get("generate_agent_summary", True):
        summary = generate_agent_summary(all_tables)
        write_json(output_dir / "_agent_summary.json", summary, pretty)
        print(f"> Generated _agent_summary.json")

    conn.close()

    print(f"\n{'=' * 60}")
    print(f"  Extraction complete!")
    print(f"  Schemas: {', '.join(sorted(schemas_seen))}")
    print(f"  Tables:  {len(all_tables)}")
    print(f"  Output:  {output_dir}")
    print(f"{'=' * 60}")


def _mask_connection_string(conn_str: str) -> str:
    """Mask passwords and secrets in the connection string for logging."""
    masked = re.sub(r'(PWD|Password)=[^;]+', r'\1=***', conn_str, flags=re.IGNORECASE)
    return masked


if __name__ == "__main__":
    main()
