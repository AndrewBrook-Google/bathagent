"""Database initialization utility for BathStuff.
Supports PostgreSQL (default / AlloyDB) as well as SQLite fallback for lightweight local dev.
"""

import os
from pathlib import Path
import sys
from typing import Optional

try:
    import psycopg2
    from psycopg2.extras import execute_values
    PSYCOPG2_AVAILABLE = True
except ImportError:
    PSYCOPG2_AVAILABLE = False

import sqlite3

from bathagent.database.seed import get_seed_data


def get_db_connection_params():
    host = os.getenv("DB_HOST", "localhost")
    port = int(os.getenv("DB_PORT", "5432"))
    user = os.getenv("DB_USER", "postgres")
    password = os.getenv("DB_PASSWORD", "postgres")
    database = os.getenv("DB_NAME", "bathstuff")
    return {"host": host, "port": port, "user": user, "password": password, "dbname": database}


def init_database(use_sqlite: bool = False, sqlite_path: str = "bathstuff.db") -> bool:
    """Initializes the database schema and seeds initial data."""
    schema_path = Path(__file__).parent / "schema.sql"
    with open(schema_path, "r", encoding="utf-8") as f:
        schema_sql = f.read()

    seed_tables = get_seed_data()

    if use_sqlite or not PSYCOPG2_AVAILABLE:
        print(f"🔧 Initializing SQLite database at: {sqlite_path}")
        # Adapt PostgreSQL schema for SQLite
        sqlite_schema = schema_sql.replace("INT PRIMARY KEY", "INTEGER PRIMARY KEY")
        sqlite_schema = sqlite_schema.replace("DECIMAL(5,4)", "REAL")
        sqlite_schema = sqlite_schema.replace("DECIMAL(10,2)", "REAL")
        sqlite_schema = sqlite_schema.replace("CASCADE", "")
        
        conn = sqlite3.connect(sqlite_path)
        cur = conn.cursor()
        cur.executescript(sqlite_schema)
        
        for table_name, rows in seed_tables:
            if not rows:
                continue
            placeholders = ",".join(["?"] * len(rows[0]))
            cur.executemany(f"INSERT INTO {table_name} VALUES ({placeholders})", rows)
        
        conn.commit()
        conn.close()
        print("✅ SQLite database initialized and seeded successfully.")
        return True

    params = get_db_connection_params()
    print(f"🔌 Connecting to PostgreSQL at {params['host']}:{params['port']}/{params['dbname']}...")
    try:
        conn = psycopg2.connect(**params)
        conn.autocommit = True
        cur = conn.cursor()

        print("📋 Applying schema.sql...")
        cur.execute(schema_sql)

        print("🌱 Seeding database tables...")
        for table_name, rows in seed_tables:
            if not rows:
                continue
            placeholders = ",".join(["%s"] * len(rows[0]))
            query = f"INSERT INTO {table_name} VALUES ({placeholders})"
            cur.executemany(query, rows)
            print(f"   ✓ Seeded {len(rows)} rows into {table_name}")

        cur.close()
        conn.close()
        print("✅ PostgreSQL database initialized and seeded successfully.")
        return True
    except Exception as e:
        print(f"❌ Failed to initialize PostgreSQL: {e}")
        print("💡 You can run with SQLite fallback or verify DB_HOST/DB_PORT settings in .env")
        return False


if __name__ == "__main__":
    use_sqlite_flag = "--sqlite" in sys.argv or os.getenv("USE_SQLITE", "false").lower() == "true"
    init_database(use_sqlite=use_sqlite_flag)
