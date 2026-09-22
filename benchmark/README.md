# Join Order Benchmark

Three benchmark steps plus optional plotting, one Python script per step. Run them from the repository root with **uv**; dependencies are declared inline (no manual virtualenv or pip setup).

## Prerequisites

- PostgreSQL **18.x**, a dedicated **UTF8** database, and a role allowed to load/configure the extension and import the schema.
- `uv`, `curl`, and enough disk space for the ~1.8 GB Parquet download, PostgreSQL tables/indexes, and temporary conversion/spill files. A full import needs substantially more space than the download.
- Build/install the extension with the target installation's PGXS:

  ```sh
  make PG_CONFIG=/path/to/postgresql-18/bin/pg_config
  make install PG_CONFIG=/path/to/postgresql-18/bin/pg_config
  ```

Choose the PostgreSQL 18 server explicitly. Both database scripts accept standard libpq environment variables (`PGHOST`, `PGPORT`, `PGUSER`, `PGDATABASE`, `PGSERVICE`, `.pgpass`) or `--host`, `--port`, `--user`, `--dbname` overrides. For example:

```sh
export PGHOST=localhost PGPORT=5432 PGUSER=postgres PGDATABASE=job
createdb --encoding=UTF8 --template=template0 "$PGDATABASE"
```

For live JEV, configure `TYPESAFE_API_KEY` in the **PostgreSQL server's environment before it starts**. A client-side environment variable does not update an already-running server. Never put the key in SQL, command arguments, or result files. The scripts do not start, restart, or reconfigure other clusters.

## 1. Download

```sh
uv run benchmark/download.py
```

This downloads into `benchmark/data/`:

- Schema, FK-column indexes and **113 queries** from [gregrahn/join-order-benchmark](https://github.com/gregrahn/join-order-benchmark), pinned to commit `a39603662e023e449cb2121997a5034df9e02ebf`.
- All 21 `job_*.parquet` assets from [DuckDB's v1.0 data release](https://github.com/duckdb/duckdb-data/releases/tag/v1.0), a real JOB/IMDb mirror. This is not a proof of exact equivalence to every [canonical CWI archive](https://event.cwi.nl/da/job/imdb.tgz) snapshot.
- Source URLs, sizes and SHA-256 hashes in `manifest.json`. Interrupted downloads retain resumable `.part` files; existing files are checked against recorded/published hashes where available.

Use `--data-dir PATH` to change the cache, or `--queries-only` to fetch just the small schema/query archive. Review IMDb's terms linked from the downloaded upstream README before redistributing data.

## 2. Import schema and data

```sh
uv run benchmark/import.py
```

The default imports the **full mirror**. It reads cached data and disables JEV; only uv's initial dependency setup may need a package download. It refuses existing public tables/views/sequences rather than dropping or overwriting them. Schema creation, COPY, primary/FK-column indexes, row/reference validation and ANALYZE are one transaction: a failure during that transaction rolls back all database changes. No new FK constraints are added to the upstream schema.

The importer checks Parquet column order and integer integrality/range before losslessly restoring the original PostgreSQL integer types. It converts one table at a time, deletes temporary CSVs, and records row counts and all **27 logical-reference checks** in `data/import.json`.

For an explicitly smaller, real-data experiment:

```sh
uv run benchmark/import.py --sample-modulus 503
```

The sample seeds `title.id % 503 = 0`, retains those movies' facts, referenced dimensions, people's information/aliases, linked titles and recursive episode ancestors. Reference-only titles do not gain all their facts. Sampling changes selectivities and may produce NULL aggregates; **sample timings are not full-JOB benchmark results**.

To reuse already-converted CSVs (one headerless `TABLE.csv` per JOB table, standard CSV quoting, unquoted empty fields representing SQL NULL):

```sh
uv run benchmark/import.py --data-dir /path/to/job-cache --csv-dir /path/to/csv
```

The CSV option is separate from sampling and never downloads missing files. Use trusted, unmodified schema/query files from the downloader.

## 3. Run both planners and compare

First try one query; real JEV requests are opt-in and can incur charges:

```sh
uv run benchmark/run.py --allow-paid --query 1a
# All 113 queries; replace the preceding generated output explicitly:
uv run benchmark/run.py --allow-paid --overwrite
```

The default output is:

```text
benchmark/result/
  plan/jev/1a.txt  1b.txt ...
  plan/pg/1a.txt   1b.txt ...
  exectime.csv
  run.json
```

Query IDs and plan filenames use the **original JOB names** (`1a`, `1b`, ..., `2a`, ...), in natural order. Only successful comparisons are retained. Use `--query` to select queries, and `--data-dir` / `--result-dir` for alternative locations. Existing output is refused unless `--overwrite` is given; unrelated files are preserved. Results under `benchmark/result/` can be tracked in Git; downloaded data and alternate/mock result directories remain ignored.

For each query, the runner:

1. Uses a shared **REPEATABLE READ, READ ONLY** snapshot for both modes, with serial execution and JIT disabled in both. Native GEQO/collapse/method settings otherwise remain unchanged; relevant settings are saved in `run.json`.
2. Prepares a separate, forced-generic plan with `jev.enabled=off` or `on`, records human-readable `EXPLAIN (ANALYZE, VERBOSE, BUFFERS, SETTINGS, SUMMARY, TIMING OFF)` output, then executes **that same cached plan** to collect results. Result retrieval should not trigger a second JEV search.
3. Compares column names/types and exact row multisets, distinguishing SQL NULL from an empty string and retaining duplicates. Row order is ignored; stock JOB queries are deterministic scalar aggregates.
4. Saves both plans and a CSV row **only when both modes succeed and their results match**. The CSV includes `pg_execution_ms`, `jev_execution_ms` and separate planning times. Errors are printed to the console; error/mismatch counts remain in `run.json`, and the final exit status is nonzero if anything failed. Other queries continue, without native fallback or retained failure plans. Fatal connection loss aborts the run, leaving only completed successful comparisons.

Execution times are PostgreSQL's **EXPLAIN ANALYZE Execution Time in milliseconds**, not client wall time or planning/API latency. Each mode executes twice (measurement, then result retrieval); mode order alternates to reduce, not eliminate, cache bias. Keep the database quiescent: concurrent DDL/ANALYZE can invalidate cached plans despite the shared data snapshot. One instrumented run is not a statistically controlled performance comparison or proof of global plan optimality.

Default limits: 300 s per statement, 120 s JEV planning, 10 s per JEV request, 10 s lock wait. Override the first three with `--statement-timeout-ms`, `--planning-timeout-ms`, `--request-timeout-ms`. Extension candidate/decision/message limits still apply, with no native fallback or automatic retries.

**Privacy:** live decisions send full normalized SQL, names, literals, predicates and estimates to JEV. HTTP tracing is disabled by the runner, but SQL transmission still occurs. Saved plans also contain SQL and filter values; protect the result directory.

## 4. Plot execution speedups

```sh
uv run benchmark/plot.py
```

Saves `result/speedup.png` and `result/speedup.svg`. Both axes use equal log scales in milliseconds: **x = JEV**, **y = PostgreSQL**. Above the `y = x` diagonal, JEV is faster; below it, PostgreSQL is faster. The five largest speedups and slowdowns are labeled by JOB name and ratio. Use `--labels N` or `--result-dir PATH` to change the selection/input. No queries or model requests are made.

The plot excludes planning/API latency and shows only retained successful comparisons. Single-run sample timings are not full-JOB or statistically controlled benchmark results.

### Offline validation (same run script)

```sh
# Existing local JOB database: no paid API calls, clearly labeled mock outputs.
uv run benchmark/run.py --mock --result-dir benchmark/result-mock

# No dataset or installation needed: build, then disposable PG + regression checks.
make audit integration PG_CONFIG=/path/to/postgresql-18/bin/pg_config
# Equivalent checks:
uv run benchmark/run.py --audit
uv run benchmark/run.py --self-test --pg-config /path/to/postgresql-18/bin/pg_config
```

`--mock` chooses the cheapest immediate native candidate through a local HTTP oracle. It tests integration/correctness, **not real JEV decision quality or latency**. PostgreSQL must run on the same host and still needs a nonempty `TYPESAFE_API_KEY` in its server environment (a dummy value is sufficient). The mock never logs authentication headers. `--self-test` creates its own disposable cluster with a dummy credential and retains the planner, SDK, tracing, cancellation and failure regressions alongside benchmark-output checks.
