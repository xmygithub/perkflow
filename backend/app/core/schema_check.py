"""只读核对当前 ORM，或用 --initial 对照冻结的 0001 基线。"""

import argparse
import asyncio
import re
import runpy
from pathlib import Path

from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from sqlalchemy import CheckConstraint, Connection, Index, MetaData, Table, inspect, text
from sqlalchemy.exc import SQLAlchemyError

from backend.app.core.config import Settings
from backend.app.core.database import Database
from backend.app.models import Base


def normalize_check(sql: str) -> str:
    sql = re.sub(r"_\w+(?=')", "", sql)
    parts = re.split(r"('(?:''|[^'])*')", sql)
    # MySQL adds quoting/parentheses/charset introducers to reflected CHECK expressions.
    return "".join(
        part if i % 2 else re.sub(r"[\s`()]+", "", part.lower()) for i, part in enumerate(parts)
    )


def initial_metadata() -> MetaData:
    """只在内存收集冻结 0001 的建表定义，供旧 SQL 库只读核对；不执行 DDL。"""
    metadata = MetaData()

    class Collector:
        def create_table(self, name, *elements, **options):
            return Table(name, metadata, *elements, **options)

        def create_index(self, name, table_name, columns, unique=False):
            table = metadata.tables[table_name]
            Index(name, *(table.c[name] for name in columns), unique=unique)

    path = Path(__file__).resolve().parents[2] / "alembic/versions/0001_initial_core_schema.py"
    upgrade = runpy.run_path(str(path))["upgrade"]
    upgrade.__globals__["op"] = Collector()
    upgrade()
    return metadata


def compare_schema(connection: Connection, metadata: MetaData | None = None) -> list[str]:
    metadata = Base.metadata if metadata is None else metadata
    context = MigrationContext.configure(
        connection, opts={"compare_type": True, "compare_server_default": True}
    )
    differences = compare_metadata(context, metadata)
    problems = [f"Alembic difference: {difference}" for difference in differences]
    inspector = inspect(connection)
    existing = set(inspector.get_table_names())
    for table in metadata.sorted_tables:
        if table.name not in existing:
            continue  # Already reported by compare_metadata.
        expected_pk = [column.name for column in table.primary_key.columns]
        if inspector.get_pk_constraint(table.name)["constrained_columns"] != expected_pk:
            problems.append(f"{table.name}: primary key differs")
        reflected_columns = {column["name"]: column for column in inspector.get_columns(table.name)}
        for column in table.columns:
            reflected = reflected_columns.get(column.name)
            if reflected is None:
                continue
            if column.computed is not None:
                computed = reflected.get("computed", {})
                if normalize_check(str(column.computed.sqltext)) != normalize_check(
                    computed.get("sqltext", "")
                ) or column.computed.persisted != computed.get("persisted"):
                    problems.append(f"{table.name}.{column.name}: generated expression differs")
            # Alembic's standard type comparison can omit MySQL unsigned/precision flags.
            for flag in ("unsigned", "fsp", "charset", "collation"):
                if getattr(column.type, flag, None) != getattr(reflected["type"], flag, None):
                    problems.append(f"{table.name}.{column.name}: {flag} differs")
        expected_checks = {
            c.name: normalize_check(str(c.sqltext))
            for c in table.constraints
            if isinstance(c, CheckConstraint)
        }
        actual_checks = {
            c["name"]: normalize_check(c["sqltext"])
            for c in inspector.get_check_constraints(table.name)
        }
        if actual_checks != expected_checks:
            problems.append(f"{table.name}: CHECK constraints differ")
        options = inspector.get_table_options(table.name)
        if options.get("mysql_engine") != "InnoDB":
            problems.append(f"{table.name}: expected InnoDB")
        if options.get("mysql_collate") != "utf8mb4_0900_ai_ci":
            problems.append(f"{table.name}: expected utf8mb4_0900_ai_ci")
    disabled = connection.execute(
        text(
            "SELECT TABLE_NAME, CONSTRAINT_NAME FROM information_schema.TABLE_CONSTRAINTS "
            "WHERE CONSTRAINT_SCHEMA = DATABASE() "
            "AND CONSTRAINT_TYPE = 'CHECK' AND ENFORCED <> 'YES'"
        )
    ).all()
    problems.extend(f"{row[0]}.{row[1]}: CHECK is not enforced" for row in disabled)
    return problems


async def main() -> int:
    parser = argparse.ArgumentParser(description="只读核对当前结构；旧 SQL 导入库用 --initial")
    parser.add_argument("--initial", action="store_true", help="核对冻结 0001，以便安全 stamp 0001")
    args = parser.parse_args()
    metadata = initial_metadata() if args.initial else Base.metadata
    database = Database(Settings())
    try:
        async with database.engine.connect() as connection:
            problems = await connection.run_sync(lambda sync: compare_schema(sync, metadata))
        if problems:
            print("Schema mismatch; do not stamp the migration:")
            for problem in problems:
                print(f"- {problem}")
            return 1
        label = "frozen 0001 baseline" if args.initial else "current ORM baseline"
        print(f"Schema matches the {label}. No database writes performed.")
        return 0
    except SQLAlchemyError as exc:
        print(f"Database check failed ({type(exc).__name__}); verify backend/.env and schema.")
        return 1
    finally:
        await database.close()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
