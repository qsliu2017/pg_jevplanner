#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = ["duckdb==1.5.5", "psycopg[binary]==3.3.6"]
# ///
"""Import cached JOB schema/data into an empty PostgreSQL 18 database; no dataset downloads or JEV calls."""
import argparse
import json
from pathlib import Path
import re
import tempfile

import duckdb
import psycopg
from psycopg import sql

from download import DEFAULT_DATA, REFERENCES, LOOKUPS, MOVIE_FACTS, TABLES, sha256


def literal(value):
    return "'" + str(value).replace("'", "''") + "'"


def source_views(db, base, definitions, manifest):
    """Check the mirror's schema and losslessly restore nullable integer columns."""
    for table in TABLES:
        path = base / "parquet" / f"job_{table}.parquet"
        expected = manifest.get("sources", {}).get(table, {})
        if not path.is_file():
            raise ValueError(f"Missing {path}; run download.py first")
        if expected.get("sha256") and sha256(path) != expected["sha256"]:
            raise ValueError(f"Checksum mismatch: {path}")
        source = f"read_parquet({literal(path)})"
        columns = [line.strip().rstrip(',').split() for line in definitions[table].strip().splitlines()]
        actual = [row[0] for row in db.execute(f"DESCRIBE SELECT * FROM {source}").fetchall()]
        if actual != [column[0] for column in columns]:
            raise ValueError(f"Mirror column names/order differ from JOB schema: {table}")
        integers = [column[0] for column in columns if column[1] == "integer"]
        bad = " OR ".join(f"({column} IS NOT NULL AND (NOT isfinite({column}) OR {column} != trunc({column}) "
                          f"OR {column} NOT BETWEEN -2147483648 AND 2147483647))" for column in integers)
        if bad and db.execute(f"SELECT count(*) FROM {source} WHERE {bad}").fetchone()[0]:
            raise ValueError(f"Mirror values cannot be represented exactly as PostgreSQL integers: {table}")
        projection = ", ".join(f"CAST({column[0]} AS INTEGER) AS {column[0]}" if column[0] in integers else column[0]
                               for column in columns)
        db.execute(f"CREATE VIEW src_{table} AS SELECT {projection} FROM {source}")


def sample(db, modulus):
    db.execute(f"CREATE TABLE seeds AS SELECT id FROM src_title WHERE id % {modulus} = 0")
    for table in LOOKUPS:
        db.execute(f"CREATE TABLE {table} AS SELECT * FROM src_{table}")
    for table in MOVIE_FACTS:
        db.execute(f"CREATE TABLE {table} AS SELECT * FROM src_{table} WHERE movie_id IN (SELECT id FROM seeds)")
    for table, fact, column in (("name", "cast_info", "person_id"), ("char_name", "cast_info", "person_role_id"),
                                ("company_name", "movie_companies", "company_id"), ("keyword", "movie_keyword", "keyword_id")):
        db.execute(f"CREATE TABLE {table} AS SELECT * FROM src_{table} WHERE id IN (SELECT {column} FROM {fact})")
    for table in ("aka_name", "person_info"):
        db.execute(f"CREATE TABLE {table} AS SELECT * FROM src_{table} WHERE person_id IN (SELECT id FROM name)")
    db.execute("""CREATE TABLE wanted_titles AS
        WITH RECURSIVE wanted(id) AS (
            (SELECT id FROM seeds UNION SELECT linked_movie_id FROM movie_link
             UNION SELECT episode_of_id FROM aka_title WHERE episode_of_id IS NOT NULL)
            UNION
            SELECT t.episode_of_id FROM src_title t JOIN wanted w ON t.id=w.id
            WHERE t.episode_of_id IS NOT NULL
        ) SELECT id FROM wanted""")
    db.execute("CREATE TABLE title AS SELECT * FROM src_title WHERE id IN (SELECT id FROM wanted_titles)")
    return db.execute("SELECT count(*) FROM seeds").fetchone()[0]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", type=Path, default=DEFAULT_DATA)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--sample-modulus", type=int, help="Opt-in reference-closed sample seeded by title.id %% N = 0 (e.g. 503)")
    mode.add_argument("--csv-dir", type=Path, help="Import already-converted JOB table CSVs instead of Parquet")
    parser.add_argument("--host")
    parser.add_argument("--port", type=int)
    parser.add_argument("--user")
    parser.add_argument("--dbname")
    args = parser.parse_args()
    if args.sample_modulus is not None and args.sample_modulus < 2:
        parser.error("--sample-modulus must be >= 2; omit it for the full mirror")
    base = args.data_dir.resolve()
    schema = (base / "upstream/schema.sql").read_text()
    indexes = (base / "upstream/fkindexes.sql").read_text()
    manifest_path = base / "manifest.json"
    manifest = json.loads(manifest_path.read_text()) if manifest_path.exists() else {}
    definitions = dict(re.findall(r"CREATE TABLE (\w+) \((.*?)\);", schema, re.DOTALL))
    if set(definitions) != set(TABLES):
        raise ValueError("Expected the original 21 JOB tables")
    kwargs = {name: getattr(args, name) for name in ("host", "port", "user", "dbname") if getattr(args, name) is not None}
    counts, seed_count = {}, None
    with psycopg.connect(**kwargs, autocommit=True, connect_timeout=10) as connection:
        if connection.info.server_version // 10000 != 18:
            raise ValueError("Import requires PostgreSQL 18.x")
        # Utility SET runs before any SELECT, even if session preload enabled JEV.
        connection.execute("SET jev.enabled=off; SET search_path=public,pg_catalog")
        with connection.transaction(), tempfile.TemporaryDirectory(prefix="job-import-") as temporary:
            with connection.cursor() as cursor:
                existing = cursor.execute("SELECT count(*) FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace "
                                          "WHERE n.nspname='public' AND c.relkind IN ('r','p','v','m','f','S')").fetchone()[0]
                if existing:
                    raise ValueError("Refusing to import into a database with existing public tables/views/sequences")
                with duckdb.connect() as db:
                    db.execute("SET threads=4; SET memory_limit='1GB'")
                    db.execute(f"SET temp_directory={literal(Path(temporary) / 'spill')}")
                    if not args.csv_dir:
                        source_views(db, base, definitions, manifest)
                        if args.sample_modulus:
                            seed_count = sample(db, args.sample_modulus)
                    # DDL, COPY, indexes, validation and statistics are one transaction.
                    cursor.execute(schema)
                    for table in TABLES:
                        if args.csv_dir:
                            csv_path = args.csv_dir.resolve() / (table + ".csv")
                            expected = manifest.get("rows", {}).get(table)
                        else:
                            view = table if args.sample_modulus else "src_" + table
                            expected = db.execute(f"SELECT count(*) FROM {view}").fetchone()[0]
                            csv_path = Path(temporary) / (table + ".csv")
                            db.execute(f"COPY (SELECT * FROM {view}) TO {literal(csv_path)} (FORMAT CSV, HEADER FALSE, NULL '')")
                        with cursor.copy(sql.SQL("COPY {} FROM STDIN WITH (FORMAT csv)").format(sql.Identifier(table))) as copy:
                            with csv_path.open("rb") as stream:
                                while block := stream.read(1024 * 1024):
                                    copy.write(block)
                        count = cursor.execute(sql.SQL("SELECT count(*) FROM {}").format(sql.Identifier(table))).fetchone()[0]
                        if expected is not None and count != expected:
                            raise ValueError(f"Row-count mismatch for {table}: {count} != {expected}")
                        counts[table] = count
                        if not args.csv_dir:
                            csv_path.unlink()
                        print(f"Imported {table}: {count} rows", flush=True)
                    cursor.execute(indexes)
                    checks = 0
                    for table, columns in REFERENCES.items():
                        for column, target in columns.items():
                            missing = cursor.execute(sql.SQL(
                                "SELECT count(*) FROM {} c WHERE c.{} IS NOT NULL AND NOT EXISTS "
                                "(SELECT 1 FROM {} p WHERE p.id=c.{})").format(
                                    sql.Identifier(table), sql.Identifier(column), sql.Identifier(target), sql.Identifier(column))).fetchone()[0]
                            if missing:
                                raise ValueError(f"{table}.{column}: {missing} missing references to {target}")
                            checks += 1
                    cursor.execute(sql.SQL("ANALYZE {}").format(sql.SQL(", ").join(map(sql.Identifier, TABLES))))
        report = {"database": connection.info.dbname, "server_version": connection.info.server_version,
                  "mode": "csv" if args.csv_dir else ("sample" if args.sample_modulus else "full mirror"),
                  "sample_modulus": args.sample_modulus, "seed_titles": seed_count,
                  "rows": counts, "logical_references_checked": checks,
                  "source_manifest": str(manifest_path), "duckdb_version": duckdb.__version__}
        try:
            (base / "import.json").write_text(json.dumps(report, indent=2) + "\n")
        except OSError as exc:
            raise SystemExit(f"Database import committed, but could not save {base / 'import.json'}: {exc}") from exc
    print(f"PASS: 21 tables, {sum(counts.values())} rows, {checks} reference checks; schema, indexes and ANALYZE committed")


if __name__ == "__main__":
    main()
