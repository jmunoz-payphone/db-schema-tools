"""
MCP Server for DB Schema — serves extracted SQL Server schema metadata on-demand.

Loads JSON files produced by the extractor into memory indices,
then exposes 7 tools via MCP stdio transport so Claude Code can query
only the relevant tables/columns/relationships without loading 45K lines.
"""

import json
import os
import sys
from collections import defaultdict
from pathlib import Path

from mcp.server.fastmcp import FastMCP

# ---------------------------------------------------------------------------
# Data loading & indexing
# ---------------------------------------------------------------------------

DATA_DIR = Path(os.getenv("DATA_DIR", "/data"))

# Primary indices (populated at startup)
tables: dict[str, dict[str, dict]] = defaultdict(dict)       # tables[schema][table] → full data
column_index: dict[str, list[str]] = defaultdict(list)        # column_index[col_lower] → ["schema.table", ...]
incoming_fks: dict[str, list[dict]] = defaultdict(list)       # incoming_fks["schema.table"] → [{from, fk_name, columns, ref_columns}]
table_name_lookup: list[str] = []                             # ["schema.table", ...] for search


def load_data():
    """Load all per-table JSON files from DATA_DIR into memory indices."""
    if not DATA_DIR.exists():
        print(f"[ERROR] Data directory not found: {DATA_DIR}", file=sys.stderr)
        sys.exit(1)

    loaded = 0
    for schema_dir in sorted(DATA_DIR.iterdir()):
        if not schema_dir.is_dir():
            continue
        schema_name = schema_dir.name
        for table_file in sorted(schema_dir.glob("*.json")):
            if table_file.name.startswith("_"):
                continue
            try:
                data = json.loads(table_file.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError) as e:
                print(f"[WARN] Skipping {table_file}: {e}", file=sys.stderr)
                continue

            table_name = data.get("table", table_file.stem)
            qualified = f"{schema_name}.{table_name}"

            # Primary store
            tables[schema_name][table_name] = data

            # Column index
            for col in data.get("columns", []):
                col_lower = col["name"].lower()
                column_index[col_lower].append(qualified)

            # Incoming FK index (reverse lookup)
            for fk in data.get("foreign_keys", []):
                ref = fk.get("references", {})
                ref_qualified = f"{ref.get('schema', '')}.{ref.get('table', '')}"
                incoming_fks[ref_qualified].append({
                    "from_table": qualified,
                    "fk_name": fk["name"],
                    "from_columns": fk["columns"],
                    "to_columns": ref.get("columns", []),
                    "on_delete": fk.get("on_delete", "NO_ACTION"),
                    "on_update": fk.get("on_update", "NO_ACTION"),
                })

            # Name lookup
            table_name_lookup.append(qualified)
            loaded += 1

    print(f"[INFO] Loaded {loaded} tables from {len(tables)} schemas.", file=sys.stderr)


# ---------------------------------------------------------------------------
# MCP Server
# ---------------------------------------------------------------------------

mcp = FastMCP(
    "db-schema",
    instructions=(
        "Database schema server for SQL Server. "
        "Use these tools to explore table structures, columns, relationships, "
        "and indexes without loading the full 45K-line schema into context."
    ),
)


@mcp.tool()
def list_schemas() -> list[dict]:
    """List all database schemas with table count and total row count."""
    result = []
    for schema_name in sorted(tables.keys()):
        schema_tables = tables[schema_name]
        total_rows = sum(t.get("row_count", 0) for t in schema_tables.values())
        result.append({
            "schema": schema_name,
            "table_count": len(schema_tables),
            "total_rows": total_rows,
        })
    return result


@mcp.tool()
def list_tables(schema: str) -> list[dict]:
    """List all tables in a schema with basic stats (columns, rows, keys, indexes).

    Args:
        schema: Database schema name (e.g. "dbo", "Services")
    """
    if schema not in tables:
        return [{"error": f"Schema '{schema}' not found. Use list_schemas() to see available schemas."}]

    result = []
    for table_name in sorted(tables[schema].keys()):
        t = tables[schema][table_name]
        result.append({
            "table": table_name,
            "columns_count": len(t.get("columns", [])),
            "row_count": t.get("row_count", 0),
            "has_primary_key": t.get("primary_key") is not None,
            "foreign_keys_count": len(t.get("foreign_keys", [])),
            "indexes_count": len(t.get("indexes", [])),
        })
    return result


@mcp.tool()
def get_table_schema(schema: str, table: str) -> dict:
    """Get full schema details for a specific table: columns, PK, FKs, indexes, constraints.

    Args:
        schema: Database schema name (e.g. "dbo")
        table: Table name (e.g. "Transaction")
    """
    if schema not in tables:
        return {"error": f"Schema '{schema}' not found."}
    if table not in tables[schema]:
        return {"error": f"Table '{schema}.{table}' not found."}
    return tables[schema][table]


@mcp.tool()
def search_tables(pattern: str) -> list[dict]:
    """Search for tables by name (case-insensitive substring match).

    Args:
        pattern: Search pattern to match against table names (e.g. "transaction", "store")
    """
    pattern_lower = pattern.lower()
    matches = []
    for qualified in table_name_lookup:
        if pattern_lower in qualified.lower():
            schema, table_name = qualified.split(".", 1)
            t = tables[schema][table_name]
            matches.append({
                "qualified_name": qualified,
                "columns_count": len(t.get("columns", [])),
                "row_count": t.get("row_count", 0),
                "primary_key": (
                    t["primary_key"]["columns"] if t.get("primary_key") else None
                ),
            })
    return matches if matches else [{"message": f"No tables matching '{pattern}'."}]


@mcp.tool()
def search_columns(column_name: str) -> list[dict]:
    """Find all tables that contain a specific column (case-insensitive exact match).

    Args:
        column_name: Column name to search for (e.g. "StoreId", "CreatedDate")
    """
    col_lower = column_name.lower()

    # Exact match first
    if col_lower in column_index:
        return [{"column": column_name, "tables": column_index[col_lower]}]

    # Fuzzy: substring match across column names
    matches: dict[str, list[str]] = defaultdict(list)
    for col_key, table_list in column_index.items():
        if col_lower in col_key:
            for qualified in table_list:
                matches[col_key].append(qualified)

    if matches:
        return [
            {"column": col, "tables": tbls}
            for col, tbls in sorted(matches.items())
        ]

    return [{"message": f"No columns matching '{column_name}'."}]


@mcp.tool()
def get_relationships(schema: str, table: str) -> dict:
    """Get all foreign key relationships for a table (both outgoing and incoming).

    Args:
        schema: Database schema name (e.g. "dbo")
        table: Table name (e.g. "Transaction")
    """
    if schema not in tables:
        return {"error": f"Schema '{schema}' not found."}
    if table not in tables[schema]:
        return {"error": f"Table '{schema}.{table}' not found."}

    qualified = f"{schema}.{table}"
    t = tables[schema][table]

    outgoing = []
    for fk in t.get("foreign_keys", []):
        ref = fk["references"]
        outgoing.append({
            "fk_name": fk["name"],
            "columns": fk["columns"],
            "references": f"{ref['schema']}.{ref['table']}",
            "ref_columns": ref["columns"],
            "on_delete": fk.get("on_delete", "NO_ACTION"),
            "on_update": fk.get("on_update", "NO_ACTION"),
        })

    incoming = incoming_fks.get(qualified, [])

    return {
        "table": qualified,
        "outgoing_fks": outgoing,
        "incoming_fks": incoming,
        "total_outgoing": len(outgoing),
        "total_incoming": len(incoming),
    }


@mcp.tool()
def get_schema_overview(schema: str) -> list[dict]:
    """Get a compact overview of all tables in a schema (table name + PK columns only).

    Args:
        schema: Database schema name (e.g. "dbo")
    """
    if schema not in tables:
        return [{"error": f"Schema '{schema}' not found."}]

    result = []
    for table_name in sorted(tables[schema].keys()):
        t = tables[schema][table_name]
        pk = t.get("primary_key")
        result.append({
            "table": table_name,
            "primary_key": pk["columns"] if pk else None,
            "row_count": t.get("row_count", 0),
        })
    return result


# ---------------------------------------------------------------------------
# Entrypoint
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    load_data()
    mcp.run(transport="stdio")
