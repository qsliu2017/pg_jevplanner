#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = ["psycopg[binary]==3.3.6"]
# ///
"""Compare JOB results, plans and execution times with JEV and native PostgreSQL.
Use --allow-paid for JEV, --mock for an offline oracle, or --self-test for regressions.
"""
import argparse
from collections import Counter
import csv
import http.server
import ipaddress
import json
import os
import pathlib
import re
import socket
import subprocess
import sys
import tempfile
import threading
import time

ROOT = pathlib.Path(__file__).resolve().parents[1]


def literal(value):
    return "'" + str(value).replace("'", "''") + "'"


def unused_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def join_sets(plan):
    """Intermediate logical groups, ignoring method, orientation and wrappers."""
    leaves = set()
    groups = set()
    for child in plan.get("Plans", []):
        child_leaves, child_groups = join_sets(child)
        leaves |= child_leaves
        groups |= child_groups
    if "Relation Name" in plan:
        leaves.add(plan["Alias"])
    if "Join" in plan["Node Type"] or plan["Node Type"] == "Nested Loop":
        groups.add(frozenset(leaves))
    return leaves, groups


class Mock(http.server.BaseHTTPRequestHandler):
    mode = "first"
    calls = []
    choices = []
    expected_auth = None
    record_calls = False

    def log_message(self, *args):
        pass

    def do_POST(self):
        assert self.path == "/v1/systemone"
        if type(self).expected_auth is not None:
            assert self.headers["Authorization"] == type(self).expected_auth
        request = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        question = request["questions"]["select_join"]
        assert question["type"] == "choice"
        options = question["criteria"]
        state = request["state"]
        forest = {frozenset(c["relids"]) for c in state["current_components"]}
        assert 2 <= len(options) <= 255
        assert len(options) == len(forest) * (len(forest) - 1) // 2
        assert all(isinstance(v, str) for v in options.values())
        assert state["query_sql"].strip().startswith(("SELECT", "WITH"))
        assert state["objective"] and state["query_block"]["id"] >= 1
        assert set().union(*forest) == set(state["query_block"]["relids"])
        candidates = {k: json.loads(v) for k, v in options.items()}
        for candidate in candidates.values():
            left, right = (frozenset(candidate[k]) for k in ("left_relids", "right_relids"))
            assert left in forest and right in forest and not left & right
            assert left | right == set(candidate["joined_relids"])
            assert candidate["native_plan"]["total_cost"] >= 0
        if type(self).record_calls:
            type(self).calls.append(request)
        mode = type(self).mode
        if mode == "invalid_once":
            type(self).mode = "first"
            mode = "invalid"
        if mode == "slow":
            time.sleep(0.4)
        if mode == "rate_limit":
            self.send_response(429)
            self.end_headers()
            return
        keys = list(options)
        selected = keys[-1] if mode == "last" else keys[0]
        if mode in ("max_cost", "min_cost"):
            selector = max if mode == "max_cost" else min
            selected = selector(keys, key=lambda k: candidates[k]["native_plan"]["total_cost"])
        if mode in ("de_fg", "df_eg"):
            aliases = {r["relid"]: r["alias"] for r in state["relations"]}
            desired = ({"d", "e"}, {"f", "g"}) if mode == "de_fg" else ({"d", "f"}, {"e", "g"})
            for group in desired:
                found = next((k for k, c in candidates.items()
                              if {aliases[i] for i in c["joined_relids"]} == group), None)
                if found:
                    selected = found
                    break
        if type(self).record_calls:
            type(self).choices.append((request, selected))
        answer = {
            "model": request["model"],
            "answers": {"select_join": {
                "type": "choice", "choice": "not-a-candidate" if mode == "invalid" else selected,
                "confidence": 1.0, "probabilities": {k: float(k == selected) for k in keys},
            }},
            "usage": {"input_tokens": 1, "output_tokens": 1},
        }
        if mode == "wrong_type":
            answer["answers"]["select_join"]["type"] = "score"
        if mode == "confidence":
            answer["answers"]["select_join"]["confidence"] = 2
        body = json.dumps(answer).encode()
        if mode == "malformed":
            body = b"not-json-and-never-an-error-message-secret"
        elif mode == "oversize":
            body = b" " * (300 * 1024)
        elif mode in ("echo_key", "escaped_key"):
            answer["diagnostic"] = "offline-test-credential"
            body = json.dumps(answer).encode()
            if mode == "escaped_key":
                body = body.replace(b"offline-test-credential", b"\\u006fffline-test-credential")
        elif mode == "malformed_echo":
            body = b"not-json offline-test-credential\n\x1b[31m"
        elif mode == "binary":
            body = b"invalid\x00body"
        elif mode == "invalid_utf8":
            body = b"invalid\xffbody"
        elif mode == "http_error_body":
            body = b'{"error":"mock rate limit"}'
        self.send_response(429 if mode == "http_error_body" else 200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass


def self_test(args):
    """The original offline regression suite, kept with the benchmark tooling."""
    Mock.expected_auth = "Bearer offline-test-credential"
    Mock.record_calls = True
    Mock.calls, Mock.choices = [], []
    bindir = pathlib.Path(subprocess.check_output([args.pg_config, "--bindir"], text=True).strip())
    library = next((p for p in (ROOT / "pg_jevplanner.so", ROOT / "pg_jevplanner.dylib") if p.exists()), None)
    if not library:
        raise SystemExit("Build the extension first")
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Mock)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    endpoint = f"http://127.0.0.1:{server.server_port}/v1/systemone"
    env = dict(os.environ, TYPESAFE_API_KEY="offline-test-credential")
    for key in ("PGHOST", "PGPORT", "PGDATABASE", "PGUSER", "PGOPTIONS", "PGSERVICE"):
        env.pop(key, None)
    passed = 0

    with tempfile.TemporaryDirectory(prefix="pgjev-", dir="/tmp") as directory:
        base = pathlib.Path(directory)
        data = base / "data"
        port = unused_port()
        subprocess.run([str(bindir / "initdb"), "-D", str(data), "-U", "pgjev", "--no-locale",
                        "--encoding=UTF8", "--auth=trust"], env=env, check=True, stdout=subprocess.DEVNULL)
        ctl = [str(bindir / "pg_ctl"), "-D", str(data)]
        server_options = (f"-F -p {port} -k {base} -c listen_addresses='' "
                          "-c max_parallel_workers_per_gather=0 -c jit=off")
        subprocess.run(ctl + ["-w", "-l", str(base / "postgres.log"), "-o", server_options, "start"],
                       env=env, check=True, stdout=subprocess.DEVNULL)
        prefix = (f"LOAD {literal(library)}; SET jev.endpoint={literal(endpoint)}; SET geqo=off; ")

        def sql(query, enabled=False, settings="", check=True, stop=True, batch=False):
            command = [str(bindir / "psql"), "-XAtq", "-h", str(base), "-p", str(port),
                       "-U", "pgjev", "-d", "postgres", "-v", f"ON_ERROR_STOP={int(stop)}"]
            text = prefix + settings + f" SET jev.enabled={'on' if enabled else 'off'}; " + query
            # -c submits one protocol message; stdin psql normally splits batches.
            if batch:
                command += ["-c", text]
            result = subprocess.run(command, input=None if batch else text, text=True,
                                    capture_output=True, env=env, timeout=90)
            if check and result.returncode:
                raise AssertionError(f"SQL failed: {query}\n{result.stderr}\n{result.stdout}")
            return result

        def ok(label):
            nonlocal passed
            passed += 1
            print(f"ok {passed} - {label}", flush=True)

        def failure(query, message, *, settings="", mode="first"):
            Mock.mode = mode
            result = sql(query, True, settings, check=False)
            assert result.returncode and message in result.stderr, (query, result.stderr)
            return result

        def explain(query, settings=""):
            return json.loads(sql("EXPLAIN (FORMAT JSON) " + query, True, settings).stdout)[0]["Plan"]

        def trace_json(stderr):
            entries = []
            pattern = r"^NOTICE:  JEV HTTP (request|response) (\d+) at ([^\n]+)\nDETAIL:  "
            for match in re.finditer(pattern, stderr, re.MULTILINE):
                body, _ = json.JSONDecoder().raw_decode(stderr[match.end():])
                entries.append((match[1], int(match[2]), match[3], body))
            return entries

        query3 = "SELECT d.id FROM d,e,f WHERE d.id=e.id AND e.id=f.id ORDER BY d.id;"
        query4 = "SELECT d.id FROM d,e,f,g WHERE d.id=e.id AND e.id=f.id AND f.id=g.id ORDER BY d.id;"
        try:
            sql("""
                CREATE TABLE a(id int PRIMARY KEY, bucket int, payload text);
                INSERT INTO a SELECT i,i%5,repeat('private-payload',20) FROM generate_series(1,1000) i;
                CREATE INDEX a_bucket_idx ON a(bucket);
                CREATE TABLE b(id int PRIMARY KEY, value int);
                INSERT INTO b SELECT i,i*2 FROM generate_series(1,1000) i;
                CREATE TABLE d AS SELECT id FROM a WHERE id<10;
                CREATE TABLE e AS SELECT * FROM d;
                CREATE TABLE f AS SELECT * FROM d;
                CREATE TABLE g AS SELECT * FROM d;
                CREATE TABLE h AS SELECT * FROM d;
                ANALYZE;
            """)
            sql(f"CREATE FUNCTION jevplanner_version() RETURNS text AS {literal(library)}, 'jevplanner_version' LANGUAGE C STRICT;")
            assert sql("SELECT jevplanner_version();").stdout.strip() == "pg_jevplanner 0.1.0 / PostgreSQL 18 / join-order-only"
            assert sql(query3).stdout and not Mock.calls
            ok("module uses the native planner unchanged when disabled")

            singles = {
                "indexed lookup": "SELECT id,payload FROM a WHERE id=3;",
                "grouping": "SELECT bucket,count(*) FROM a GROUP BY bucket ORDER BY bucket;",
                "distinct": "SELECT DISTINCT bucket FROM a ORDER BY bucket;",
                "ordering/limit": "SELECT id FROM a ORDER BY bucket,id LIMIT 7;",
                "window": "SELECT id,row_number() OVER (ORDER BY id) FROM a WHERE id<8 ORDER BY id;",
                "min/max": "SELECT min(id),max(id) FROM a WHERE id>5;",
                "grouping sets": "SELECT bucket,count(*) FROM a GROUP BY GROUPING SETS ((bucket),()) ORDER BY bucket;",
                "target SRF": "SELECT generate_series(1,3);",
                "union": "SELECT id FROM a WHERE id<4 UNION SELECT id FROM b WHERE id<6 ORDER BY id;",
            }
            for label, query in singles.items():
                before = len(Mock.calls)
                assert sql(query, True).stdout == sql(query).stdout
                assert len(Mock.calls) == before
                ok(f"{label}: zero HTTP calls; native physical/upper planning")
            assert explain(singles["indexed lookup"]) == json.loads(sql("EXPLAIN (FORMAT JSON) " + singles["indexed lookup"]).stdout)[0]["Plan"]
            query2 = "SELECT d.id FROM d JOIN e USING(id) ORDER BY d.id;"
            before = len(Mock.calls)
            assert sql(query2, True).stdout == sql(query2).stdout
            assert len(Mock.calls) == before
            ok("two-table join is a forced merge, not an API decision")

            expected = sql(query4).stdout
            native_groups = join_sets(json.loads(sql("EXPLAIN (FORMAT JSON) " + query4).stdout)[0]["Plan"])[1]
            groups = []
            for mode, wanted in (("de_fg", ({"d", "e"}, {"f", "g"})), ("df_eg", ({"d", "f"}, {"e", "g"}))):
                Mock.mode = mode
                before = len(Mock.calls)
                plan = explain(query4)
                actual_groups = join_sets(plan)[1]
                assert all(frozenset(group) in actual_groups for group in wanted), (mode, plan)
                assert len(Mock.calls) - before == 2, "four leaves need two model choices and a forced final merge"
                assert sql(query4, True).stdout == expected
                groups.append(actual_groups)
                ok(f"{mode}: model selects the actual bushy join tree, with identical results")
            assert groups[0] != groups[1] and any(g != native_groups for g in groups)
            ok("model-owned join order differs from native and cannot be replaced by cached unselected trees")

            Mock.mode = "max_cost"
            cross = "SELECT count(*) FROM d JOIN e ON d.id=e.id JOIN f ON e.id+1=f.id;"
            before = len(Mock.calls)
            plan = explain(cross)
            request, selected = Mock.choices[-1]
            candidates = [json.loads(c) for c in request["questions"]["select_join"]["criteria"].values()]
            assert len(Mock.calls) - before == 1 and len(candidates) == 3
            assert frozenset({"d", "f"}) in join_sets(plan)[1], plan
            assert max(c["native_plan"]["total_cost"] for c in candidates) > min(c["native_plan"]["total_cost"] for c in candidates)
            assert sql(cross, True).stdout == sql(cross).stdout
            ok("costlier Cartesian intermediate is offered and selectable; pairs are not cost/edge shortlisted")

            for settings in ("SET enable_hashjoin=off; SET enable_mergejoin=off;",
                             "SET enable_nestloop=off; SET enable_mergejoin=off;"):
                Mock.mode = "de_fg"
                plan = explain(query4, settings)
                assert {frozenset({"d", "e"}), frozenset({"f", "g"})} <= join_sets(plan)[1]
                assert sql(query4, True, settings).stdout == expected
            ok("native join-method settings change physical planning without changing the chosen grouping")

            Mock.mode = "first"
            contexts = {
                "explicit joins": "SELECT d.id FROM d JOIN e USING(id) JOIN f USING(id) ORDER BY d.id;",
                "quoted aliases": 'SELECT "a b".id FROM d "a b",e "c d",f "e f" WHERE "a b".id="c d".id AND "c d".id="e f".id ORDER BY 1;',
                "self-join aliases": "SELECT x.id FROM d x,d y,d z WHERE x.id=y.id AND y.id=z.id ORDER BY 1;",
                "flattened subquery": "SELECT s.id FROM (SELECT d.id FROM d,e WHERE d.id=e.id) s,f WHERE s.id=f.id ORDER BY 1;",
                "nonflattened subquery": "SELECT s.id FROM (SELECT d.id FROM d,e,f WHERE d.id=e.id AND e.id=f.id LIMIT 5) s,g,h WHERE s.id=g.id AND g.id=h.id ORDER BY 1;",
                "materialized CTE": "WITH q AS MATERIALIZED (SELECT d.id FROM d,e,f WHERE d.id=e.id AND e.id=f.id) SELECT q.id FROM q,g,h WHERE q.id=g.id AND g.id=h.id ORDER BY 1;",
                "grouping over joins": "SELECT d.id%2,count(*) FROM d,e,f WHERE d.id=e.id AND e.id=f.id GROUP BY d.id%2 ORDER BY 1;",
                "window over joins": "SELECT d.id,row_number() OVER (ORDER BY d.id) FROM d,e,f WHERE d.id=e.id AND e.id=f.id ORDER BY 1;",
            }
            for label, query in contexts.items():
                before = len(Mock.calls)
                assert sql(query, True).stdout == sql(query).stdout
                calls = Mock.calls[before:]
                assert calls and all(r["state"]["query_sql"] == calls[0]["state"]["query_sql"] for r in calls)
                if label in ("nonflattened subquery", "materialized CTE"):
                    assert len(calls) == 2 and len({r["state"]["query_block"]["id"] for r in calls}) == 2
                ok(f"{label}: query/alias context and result correctness")
            state = Mock.calls[-1]["state"]
            assert len(state["join_predicates"]) >= 2, "equality predicates moved to ECs must remain visible"
            assert all("filtered_rows" in r and "filters" in r for r in state["relations"])
            assert {r["alias"] for r in state["relations"]} >= {"d", "e", "f"}
            ok("state includes alias maps, predicates and filtered cardinalities, not only decision metadata")

            marker_query = query3.replace("ORDER BY", "AND 'explicit; SQL literal' <> '' ORDER BY")
            batch = "SET application_name='unrelated-é-secret'; " + marker_query + " SELECT 'trailing-secret';"
            before = len(Mock.calls)
            sql(batch, True, batch=True)
            body = json.dumps(Mock.calls[before:])
            assert "explicit; SQL literal" in body and "SELECT" in body
            assert "unrelated-" not in body and "trailing-secret" not in body and "LOAD" not in body
            assert "private-payload" not in body and "offline-test-credential" not in body
            ok("full normalized statement includes literals but excludes sibling statements in one protocol batch")
            prepared = "PREPARE q(int) AS " + query3.replace("ORDER BY", "AND d.id>$1 ORDER BY") + " EXECUTE q(2); DEALLOCATE q;"
            before = len(Mock.calls)
            assert sql(prepared, True).stdout == sql(prepared).stdout
            assert "$1" in Mock.calls[before]["state"]["query_sql"]
            assert "PREPARE" not in Mock.calls[before]["state"]["query_sql"]
            ok("prepared query context is the statement, not PREPARE/EXECUTE wrapper text")

            Mock.mode = "df_eg"
            low_limits = "SET join_collapse_limit=1; SET from_collapse_limit=1; SET geqo=on; SET geqo_threshold=2;"
            explicit4 = "SELECT d.id FROM d JOIN e USING(id) JOIN f USING(id) JOIN g USING(id) ORDER BY 1;"
            plan = explain(explicit4, low_limits)
            assert {frozenset({"d", "f"}), frozenset({"e", "g"})} <= join_sets(plan)[1]
            before = len(Mock.calls)
            assert sql(query4, False, low_limits).stdout == expected and len(Mock.calls) == before
            result = sql(query3 + " SHOW join_collapse_limit; SHOW from_collapse_limit;", True, low_limits)
            assert result.stdout.endswith("1\n1\n")
            ok("collapse/GEQO heuristics cannot preselect enabled join order; disabled mode and GUCs are preserved")

            Mock.mode = "first"
            assert sql("SHOW jev.log_http;").stdout.strip() == "off"
            quiet = sql(query3, True)
            assert "JEV HTTP" not in quiet.stderr
            before = len(Mock.calls)
            traced = sql(query3, True, "SET jev.log_http=on;")
            entries = trace_json(traced.stderr)
            assert len(Mock.calls) - before == 1 and len(entries) == 2
            assert entries[0][:2] == ("request", 1) and entries[1][:2] == ("response", 1)
            assert entries[0][3] == Mock.calls[-1] and "HTTP 200" in entries[1][2]
            assert entries[1][3]["answers"]["select_join"]["choice"] == "p0"
            assert traced.stdout == quiet.stdout
            assert "Authorization" not in traced.stderr and "offline-test-credential" not in traced.stderr
            toggled = sql("SET jev.log_http=on; " + query3 + " SET jev.log_http=off; " + query3, True)
            assert len(trace_json(toggled.stderr)) == 2 and toggled.stdout == quiet.stdout * 2
            ok("HTTP trace is opt-in, accurate, credential-free and can be disabled in the same backend")
            for mode in ("rate_limit", "http_error_body"):
                result = failure(query3, "HTTP status 429", settings="SET jev.log_http=on;", mode=mode)
                assert "HTTP 429" in result.stderr
                assert ("(empty body)" if mode == "rate_limit" else '"error": "mock rate limit"') in result.stderr
            result = failure(query3, "not valid JSON", settings="SET jev.log_http=on;", mode="malformed")
            assert trace_json(result.stderr)[1][3] == "not-json-and-never-an-error-message-secret"
            for mode, message in (("binary", "invalid response body"), ("invalid_utf8", "not valid JSON")):
                assert "(non-text body omitted)" in failure(query3, message, settings="SET jev.log_http=on;", mode=mode).stderr
            ok("opt-in trace safely exposes text error bodies and omits binary/invalid encodings")
            for mode in ("echo_key", "escaped_key", "malformed_echo"):
                Mock.mode = mode
                result = sql(query3, True, "SET jev.log_http=on;", check=mode != "malformed_echo")
                assert "offline-test-credential" not in result.stderr and "\\u006fffline-test-credential" not in result.stderr
                assert "[REDACTED]" in result.stderr and "\x1b" not in result.stderr
                if mode == "malformed_echo":
                    assert result.returncode and "not valid JSON" in result.stderr
            Mock.mode = "first"
            secret_sql = query3.replace("ORDER BY", "AND 'offline-test-credential' <> '' ORDER BY")
            assert "offline-test-credential" not in sql(secret_sql, True, "SET jev.log_http=on;").stderr
            ok("trace redacts echoed credentials and credential occurrences inside the query")

            for mode, message in (("invalid", "unknown alternative"), ("wrong_type", "not a Choice"),
                                  ("confidence", "outside [0,1]"), ("malformed", "not valid JSON"),
                                  ("rate_limit", "HTTP status 429"), ("oversize", "response exceeds")):
                before = len(Mock.calls)
                result = failure(query3, message, mode=mode)
                assert len(Mock.calls) == before + 1 and "not-json-and-never-an-error-message-secret" not in result.stderr
                ok(f"{mode}: failed join-order decision errors without retry or native fallback")
            failure(query3, "request failed", settings="SET jev.timeout_ms=30;", mode="slow")
            failure(query3, "total planning timeout exceeded", settings="SET jev.planning_timeout_ms=100; SET jev.timeout_ms=1000;", mode="slow")
            failure(query3, "canceling statement due to statement timeout", settings="SET statement_timeout=30; SET jev.timeout_ms=1000;", mode="slow")
            ok("request/global deadlines and PostgreSQL cancellation are enforced")
            before = len(Mock.calls)
            failure(query4, "decision budget exceeded", settings="SET jev.max_decisions=1;")
            assert len(Mock.calls) == before + 1
            before = len(Mock.calls)
            failure(query3, "join candidate budget exceeded", settings="SET jev.max_candidates=2;")
            assert len(Mock.calls) == before
            many = "SELECT count(*) FROM " + ",".join(f"d x{i}" for i in range(24)) + ";"
            failure(many, "Candidate pair count is 276")
            assert len(Mock.calls) == before
            ok("decision/candidate limits error explicitly; native paths themselves are no longer capped")
            huge = query3.replace("ORDER BY", "AND " + literal("x" * 50000) + " <> '' ORDER BY")
            failure(huge, "encoding budget")
            assert len(Mock.calls) == before
            ok("oversized full-query context errors instead of truncating SQL or using native join search")

            unsupported = [
                ("SELECT a.id FROM a LEFT JOIN b USING(id);", "non-inner"),
                ("SELECT * FROM d RIGHT JOIN e USING(id);", "non-inner"),
                ("SELECT * FROM d FULL JOIN e USING(id);", "non-inner"),
                ("SELECT d.id FROM d WHERE EXISTS(SELECT 1 FROM e WHERE e.id=d.id);", "subquery expressions"),
                ("SELECT * FROM d WHERE id IN (SELECT id FROM e);", "subquery expressions"),
                ("SELECT (SELECT max(id) FROM d);", "subquery expressions"),
                ("SELECT * FROM d,LATERAL (SELECT d.id) s;", "LATERAL"),
                ("WITH RECURSIVE t(n) AS (VALUES(1) UNION ALL SELECT n+1 FROM t WHERE n<3) SELECT * FROM t;", "recursive CTE"),
                ("UPDATE a SET bucket=1 WHERE id=1;", "data-modifying"),
            ]
            for query, message in unsupported:
                before = len(Mock.calls)
                failure(query, message)
                assert len(Mock.calls) == before
            failure(query3, "parallel planning", settings="SET max_parallel_workers_per_gather=1;")
            sql("""CREATE FOREIGN DATA WRAPPER no_handler; CREATE SERVER dummy FOREIGN DATA WRAPPER no_handler;
                   CREATE FOREIGN TABLE remote_table(id int) SERVER dummy;
                   CREATE TABLE partition_parent(id int) PARTITION BY RANGE(id);
                   CREATE TABLE partition_child PARTITION OF partition_parent FOR VALUES FROM (0) TO (10);
                   CREATE TABLE inherited(id int); CREATE TABLE inheritor() INHERITS(inherited);
                   CREATE VIEW guarded WITH (security_barrier=true) AS SELECT * FROM d;
                   CREATE TABLE secret_rows(id int); ALTER TABLE secret_rows ENABLE ROW LEVEL SECURITY;
                   CREATE ROLE unprivileged; GRANT SELECT ON secret_rows TO unprivileged;
                   CREATE POLICY visible ON secret_rows FOR SELECT USING (true);""")
            for query, message in (("SELECT * FROM remote_table;", "foreign tables"),
                                   ("SELECT * FROM partition_parent WHERE id=1;", "partitioned"),
                                   ("SELECT * FROM inherited;", "inherited"),
                                   ("SELECT * FROM guarded;", "security-barrier"),
                                   ("SET ROLE unprivileged; SELECT * FROM secret_rows;", "row-security")):
                before = len(Mock.calls)
                failure(query, message)
                assert len(Mock.calls) == before
            denied = sql("SET ROLE unprivileged; SET jev.log_http=on;", check=False)
            assert denied.returncode and 'permission denied to set parameter "jev.log_http"' in denied.stderr
            ok("unsupported semantics/security boundaries reject before HTTP; trace remains superuser-only")
            sql("CREATE DATABASE ascii_context TEMPLATE template0 ENCODING 'SQL_ASCII';")
            ascii_result = subprocess.run([str(bindir / "psql"), "-XqAt", "-h", str(base), "-p", str(port),
                                           "-U", "pgjev", "-d", "ascii_context", "-v", "ON_ERROR_STOP=1"],
                                          input=prefix + " SET jev.enabled=on; SELECT 1;", text=True,
                                          capture_output=True, env=env, timeout=10)
            assert ascii_result.returncode and "non-UTF8" in ascii_result.stderr
            ok("SQL context is only serialized from supported UTF8 databases")

            Mock.mode = "invalid_once"
            result = sql(query3 + " SHOW join_collapse_limit; SHOW from_collapse_limit; " + query3,
                         True, "SET join_collapse_limit=1; SET from_collapse_limit=1;", stop=False)
            assert "unknown alternative" in result.stderr and result.stdout == "1\n1\n" + sql(query3).stdout
            ok("error unwinding restores collapse settings and join-search lifecycle in the same backend")

            import psycopg
            fixture = base / "job-fixture"
            (fixture / "upstream").mkdir(parents=True)
            for name, query in (("1a", query3), ("2a", query4),
                                ("10a", "SELECT v FROM (VALUES(NULL::text),(''),('x'),('x')) s(v);")):
                (fixture / "upstream" / (name + ".sql")).write_text(query)
            settings = argparse.Namespace(data_dir=fixture, result_dir=base / "result", query=None,
                overwrite=False, library=str(library), statement_timeout_ms=15000,
                request_timeout_ms=1000, planning_timeout_ms=15000, mock=True)
            with psycopg.connect(host=str(base), port=port, user="pgjev", dbname="postgres",
                                 autocommit=True, prepare_threshold=None) as connection:
                Mock.mode = "min_cost"
                before = len(Mock.calls)
                assert benchmark(settings, connection, endpoint) == 0
                assert len(Mock.calls) - before == 3, "results must reuse measured plans without second JEV planning"
                with (settings.result_dir / "exectime.csv").open() as stream:
                    records = list(csv.DictReader(stream))
                assert [r["job_query"] for r in records] == ["1a.sql", "2a.sql", "10a.sql"]
                assert [r["query_id"] for r in records] == ["1a", "2a", "10a"]
                assert all(r["results_match"] == "true" and r["jev_mode"] == "mock" for r in records)
                for mode in ("pg", "jev"):
                    for name in ("1a", "2a", "10a"):
                        assert "Execution Time:" in (settings.result_dir / "plan" / mode / f"{name}.txt").read_text()
                with connection.transaction():
                    null = execute_query(connection, "SELECT NULL::text AS v", "pg")
                    empty = execute_query(connection, "SELECT ''::text AS v", "jev")
                    assert not null["error"] and not empty["error"] and null["rows"] != empty["rows"]
                    multiple = execute_query(connection, "SELECT 1; SET application_name='hidden-tail'", "pg")
                    assert multiple["error"] and connection.execute("SHOW application_name").fetchone()[0] != "hidden-tail"
                assert Counter([("x",), ("x",)]) != Counter([("x",)])
                ok("benchmark writes ordered plan/CSV outputs, preserves NULL/duplicates, and reuses measured plans")
                try:
                    benchmark(settings, connection, endpoint)
                except ValueError as exc:
                    assert "not empty" in str(exc)
                else:
                    raise AssertionError("Existing benchmark output must not be silently overwritten")
                notes = settings.result_dir / "notes.txt"
                notes.write_text("keep")
                settings.overwrite, settings.query = True, ["1a", "2a"]
                Mock.mode = "invalid_once"
                assert benchmark(settings, connection, endpoint) == 1
                with (settings.result_dir / "exectime.csv").open() as stream:
                    records = list(csv.DictReader(stream))
                assert len(records) == 1 and records[0]["query_id"] == "2a" and records[0]["status"] == "ok"
                metadata = json.loads((settings.result_dir / "run.json").read_text())
                assert metadata["completed_queries"] == 2 and metadata["omitted_queries"] == 1
                for mode in ("jev", "pg"):
                    assert not (settings.result_dir / "plan" / mode / "1a.txt").exists()
                    assert not (settings.result_dir / "plan" / mode / "10a.txt").exists()
                assert notes.read_text() == "keep"
                ok("benchmark retains only matching results, reports failures, continues, and safely replaces generated outputs")
                (fixture / "upstream/1a.sql").write_text("SELECT pg_sleep(0.2);")
                (fixture / "upstream/2a.sql").write_text("SELECT 1;")
                settings.statement_timeout_ms = 50
                assert benchmark(settings, connection, endpoint) == 1
                with (settings.result_dir / "exectime.csv").open() as stream:
                    records = list(csv.DictReader(stream))
                assert len(records) == 1 and records[0]["query_id"] == "2a"
                assert json.loads((settings.result_dir / "run.json").read_text())["omitted_queries"] == 1
                for mode in ("pg", "jev"):
                    with connection.transaction():
                        timed_out = execute_query(connection, "SELECT pg_sleep(0.2)", mode)
                        assert "statement timeout" in timed_out["error"] and timed_out["execution_ms"] == ""
                ok("benchmark enforces execution timeouts in both modes and never records failures as zero timings")

            subprocess.run(ctl + ["-m", "fast", "-w", "stop"], check=True, stdout=subprocess.DEVNULL)
            no_key = dict(env)
            no_key.pop("TYPESAFE_API_KEY")
            subprocess.run(ctl + ["-w", "-l", str(base / "postgres.log"), "-o", server_options, "start"],
                           env=no_key, check=True, stdout=subprocess.DEVNULL)
            before = len(Mock.calls)
            assert sql(singles["indexed lookup"], True).stdout == sql(singles["indexed lookup"]).stdout
            assert sql(query2, True).stdout == sql(query2).stdout
            failure(query3, "requires server environment TYPESAFE_API_KEY")
            assert len(Mock.calls) == before
            ok("credentials are required only for real model choices, not single-table or forced merges")
            print(f"PASS: {passed} checks; {len(Mock.calls)} mock JEV requests; no live API calls")
        except BaseException:
            print((base / "postgres.log").read_text()[-12000:])
            raise
        finally:
            subprocess.run(ctl + ["-m", "immediate", "-w", "stop"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            server.shutdown()
            server.server_close()


def audit_build():
    """Keep the native-linkage/SDK checks with the other offline tests."""
    native = {"standard_planner", "standard_join_search", "make_join_rel", "set_cheapest",
              "generate_partitionwise_join_paths", "join_search_hook"}
    references, definitions = set(), set()
    for name in ("pg_jevplanner", "jev_planner", "sdk"):
        output = subprocess.check_output(["nm", "-g", str(ROOT / "src" / (name + ".o"))], text=True)
        for line in output.splitlines():
            fields = line.split()
            if len(fields) not in (2, 3) or len(fields[-2]) != 1:
                continue
            kind, symbol = fields[-2:]
            if sys.platform == "darwin":
                symbol = symbol.removeprefix("_")
            assert not symbol.startswith("jev_pg_"), symbol
            if kind in ("U", "w", "v"):
                references.add(symbol)
                if name == "sdk":
                    assert not symbol.startswith("jev_") and symbol not in native, symbol
            else:
                definitions.add(symbol)
    assert native <= references and not native & definitions
    assert not {"jev_choose_path", "jev_compare_paths", "jev_choose_options"} & (references | definitions)
    assert not (ROOT / "vendor").exists()
    subprocess.run(["cc", "-std=c99", "-x", "c", "-fsyntax-only", "-I" + str(ROOT / "src"), "-"],
                   input='#include "sdk.h"\n', text=True, check=True)
    print("audit: native optimizer linkage and independent SDK; no private optimizer")


def set_option(connection, name, value, local=False):
    from psycopg import sql
    command = sql.SQL("SET " + ("LOCAL " if local else "") + "{} TO {}")
    connection.execute(command.format(sql.Identifier(name), sql.Literal(str(value))))


def execute_query(connection, query, mode):
    """Measure and fetch the same cached plan; JEV is consulted only once."""
    from psycopg import sql
    name = sql.Identifier("benchmark_" + mode)
    result = {"plan": "", "rows": None, "columns": None,
              "planning_ms": "", "execution_ms": "", "error": ""}
    phase = "prepare"
    try:
        # A savepoint lets the other planner run even if this mode fails.
        with connection.transaction():
            set_option(connection, "jev.enabled", "on" if mode == "jev" else "off", local=True)
            with connection.cursor() as cursor:
                # Binary mode uses the extended protocol, rejecting multiple SQL
                # statements in a query file rather than executing a hidden tail.
                cursor.execute(sql.SQL("PREPARE {} AS\n").format(name) + sql.SQL(query), binary=True)
                phase = "explain"
                cursor.execute(sql.SQL("EXPLAIN (ANALYZE, VERBOSE, BUFFERS, SETTINGS, SUMMARY, TIMING OFF) EXECUTE {}").format(name))
                result["plan"] = "\n".join(row[0] for row in cursor.fetchall()) + "\n"
                for label, field in (("Planning", "planning_ms"), ("Execution", "execution_ms")):
                    match = re.search(r"^" + label + r" Time: ([0-9]+(?:\.[0-9]+)?) ms$", result["plan"], re.MULTILINE)
                    if not match:
                        raise ValueError(f"Missing {label} Time in EXPLAIN output")
                    result[field] = float(match[1])
                phase = "results"
                cursor.execute(sql.SQL("EXECUTE {}").format(name))
                result["columns"] = [(column.name, column.type_code) for column in cursor.description]
                # JOB returns scalar aggregates. Counter also preserves duplicates
                # for row-producing fixtures, while ignoring unspecified row order.
                result["rows"] = Counter(cursor.fetchall())
    except Exception as exc:
        result["error"] = phase + ": " + str(exc)
    finally:
        # PREPARE isn't transactional. Disable psycopg auto-prepare and clean
        # session statements even after a rolled-back savepoint.
        connection.execute("DEALLOCATE ALL")
    return result


def query_files(directory, selected):
    files = []
    for path in directory.glob("*.sql"):
        match = re.fullmatch(r"([0-9]+)([a-z])\.sql", path.name)
        if match:
            files.append(((int(match[1]), match[2]), path))
    ordered = list(enumerate((path for _, path in sorted(files)), 1))
    if selected:
        wanted = {name.removesuffix(".sql") for name in selected}
        missing = wanted - {path.stem for _, path in ordered}
        if missing:
            raise ValueError("Unknown JOB queries: " + ", ".join(sorted(missing)))
        ordered = [(number, path) for number, path in ordered if path.stem in wanted]
    if not ordered:
        raise ValueError(f"No JOB queries in {directory}; run download.py first")
    return ordered


def prepare_output(directory, overwrite):
    if directory.exists() and any(directory.iterdir()) and not overwrite:
        raise ValueError(f"Result directory is not empty: {directory}; use --overwrite or a new --result-dir")
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    for mode in ("jev", "pg"):
        folder = directory / "plan" / mode
        if not folder.resolve().is_relative_to(directory.resolve()):
            raise ValueError(f"Plan directory points outside the result directory: {folder}")
        folder.mkdir(mode=0o700, parents=True, exist_ok=True)
        if overwrite:
            for path in folder.glob("*.txt"):
                if re.fullmatch(r"(?:q[0-9]+|[0-9]+[a-z])\.txt", path.name):
                    path.unlink()
    # Never recursively delete the output directory or unrelated user files.


def benchmark(args, connection, endpoint=None):
    files = query_files(args.data_dir / "upstream", args.query)
    prepare_output(args.result_dir, args.overwrite)
    from psycopg import sql
    connection.execute("SET jev.enabled=off")
    connection.execute(sql.SQL("LOAD {}").format(sql.Literal(args.library)))
    options = {
        "max_parallel_workers_per_gather": "0", "jit": "off",
        "plan_cache_mode": "force_generic_plan", "search_path": "public, pg_catalog",
        "statement_timeout": args.statement_timeout_ms, "lock_timeout": 10000,
        "jev.log_http": "off", "jev.debug": "off", "jev.timeout_ms": args.request_timeout_ms,
        "jev.planning_timeout_ms": args.planning_timeout_ms,
    }
    for name, value in options.items():
        if name == "search_path":
            connection.execute("SET search_path=public,pg_catalog")
        else:
            set_option(connection, name, value)
    if endpoint:
        set_option(connection, "jev.endpoint", endpoint)
    settings = {name: value for name, value in connection.execute(
        "SELECT name,setting FROM pg_settings WHERE name LIKE 'jev.%' AND name NOT IN ('jev.endpoint','jev.enabled') "
        "OR name IN ('server_version','geqo','geqo_threshold','join_collapse_limit','from_collapse_limit',"
        "'max_parallel_workers_per_gather','jit','work_mem','plan_cache_mode','statement_timeout','lock_timeout',"
        "'enable_hashjoin','enable_mergejoin','enable_nestloop','enable_seqscan','enable_indexscan')")}
    source_manifest = args.data_dir / "manifest.json"
    metadata = {"jev_mode": "mock" if args.mock else "live", "settings": settings,
                "mode_settings": {"pg": {"jev.enabled": "off"}, "jev": {"jev.enabled": "on"}},
                "queries": {}, "attempted_queries": len(files), "completed_queries": 0, "omitted_queries": 0,
                "results_policy": "Only successful, matching comparisons are retained; failures still return a nonzero exit status.",
                "timing": "One EXPLAIN ANALYZE per mode; cached plan executed again for correctness. No cold-cache guarantee.",
                "source_manifest": json.loads(source_manifest.read_text()) if source_manifest.exists() else None,
                "import_report": json.loads((args.data_dir / "import.json").read_text()) if (args.data_dir / "import.json").exists() else None}
    (args.result_dir / "run.json").write_text(json.dumps(metadata, indent=2) + "\n")
    fields = ["query_id", "job_query", "jev_mode", "pg_execution_ms", "jev_execution_ms",
              "pg_planning_ms", "jev_planning_ms", "results_match", "status", "pg_error", "jev_error"]
    failures = 0
    with (args.result_dir / "exectime.csv").open("w", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=fields, lineterminator="\n")
        writer.writeheader()
        stream.flush()
        for number, path in files:
            query = path.read_text(encoding="utf-8")
            results = {}
            # Both modes see the same MVCC snapshot. Keep the benchmark DB quiet:
            # concurrent DDL/ANALYZE may still invalidate cached plans/statistics.
            with connection.transaction():
                connection.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY")
                connection.execute("SELECT pg_current_snapshot()")
                for mode in (("pg", "jev") if number % 2 else ("jev", "pg")):
                    results[mode] = execute_query(connection, query, mode)
            valid = all(not result["error"] for result in results.values())
            same = valid and results["pg"]["columns"] == results["jev"]["columns"] and results["pg"]["rows"] == results["jev"]["rows"]
            status = "ok" if same else ("mismatch" if valid else "error")
            failures += status != "ok"
            if same:
                record = {"query_id": path.stem, "job_query": path.name,
                          "jev_mode": metadata["jev_mode"], "results_match": "true", "status": "ok"}
                for mode, result in results.items():
                    label = f"jev ({metadata['jev_mode']}; jev.enabled=on)" if mode == "jev" else "pg (jev.enabled=off)"
                    header = f"Query {path.stem}: {path.name}\nPlanner: {label}\n\n{query.strip()}\n\n"
                    (args.result_dir / "plan" / mode / f"{path.stem}.txt").write_text(header + result["plan"], encoding="utf-8")
                    for field in ("execution_ms", "planning_ms", "error"):
                        record[f"{mode}_{field}"] = result[field]
                writer.writerow(record)
                stream.flush()
                metadata["queries"][path.stem] = path.name
            else:
                for mode, result in results.items():
                    if result["error"]:
                        print(f"{path.name} {mode}: {result['error']}", flush=True)
            metadata["completed_queries"] += 1
            metadata["omitted_queries"] = failures
            (args.result_dir / "run.json").write_text(json.dumps(metadata, indent=2) + "\n")
            print(f"{path.name}: {status}; pg={results['pg']['execution_ms']} ms, jev={results['jev']['execution_ms']} ms", flush=True)
    print(f"{len(files) - failures}/{len(files)} queries match; results: {args.result_dir}")
    return int(failures != 0)


def positive(value):
    number = int(value)
    if number <= 0:
        raise argparse.ArgumentTypeError("must be positive")
    return number


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--allow-paid", action="store_true", help="Permit real JEV requests; full SQL is sent to the provider")
    mode.add_argument("--mock", action="store_true", help="Local min-immediate-cost oracle; not a live-JEV performance test")
    mode.add_argument("--self-test", action="store_true", help="Disposable PostgreSQL + offline regression suite")
    mode.add_argument("--audit", action="store_true", help="Check native linkage and SDK isolation; no database needed")
    parser.add_argument("--pg-config", default="pg_config", help="For --self-test")
    parser.add_argument("--data-dir", type=pathlib.Path, default=ROOT / "benchmark/data")
    parser.add_argument("--result-dir", type=pathlib.Path, default=ROOT / "benchmark/result")
    parser.add_argument("--query", action="append", help="Only this JOB stem, e.g. 1a (repeatable); default all queries")
    parser.add_argument("--overwrite", action="store_true", help="Replace generated plans/CSV/metadata, not unrelated files")
    parser.add_argument("--host")
    parser.add_argument("--port", type=int)
    parser.add_argument("--user")
    parser.add_argument("--dbname")
    parser.add_argument("--library", default="pg_jevplanner", help="Installed extension name, or a built library path for testing")
    parser.add_argument("--statement-timeout-ms", type=positive, default=300000)
    parser.add_argument("--planning-timeout-ms", type=positive, default=120000)
    parser.add_argument("--request-timeout-ms", type=positive, default=10000)
    args = parser.parse_args()
    if args.audit or args.self_test:
        audit_build()
        if args.self_test:
            self_test(args)
        return 0
    try:
        import psycopg
    except ImportError:
        parser.error("Run with uv: uv run benchmark/run.py [options] (dependencies are declared inline)")
    args.data_dir, args.result_dir = args.data_dir.resolve(), args.result_dir.resolve()
    # Validate before connecting or making any model requests.
    query_files(args.data_dir / "upstream", args.query)
    kwargs = {name: getattr(args, name) for name in ("host", "port", "user", "dbname") if getattr(args, name) is not None}
    kwargs.update(autocommit=True, prepare_threshold=None, connect_timeout=10)
    server = None
    try:
        with psycopg.connect(**kwargs) as connection:
            if connection.info.server_version // 10000 != 18:
                raise ValueError("Benchmark requires PostgreSQL 18.x")
            endpoint = None
            if args.mock:
                host, address = connection.info.host or "", connection.info.hostaddr or ""
                local = ipaddress.ip_address(address).is_loopback if address else host.startswith(("/", "@"))
                if not local:
                    raise ValueError("--mock requires a PostgreSQL server on this same host")
                Mock.mode, Mock.expected_auth, Mock.record_calls = "min_cost", None, False
                server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Mock)
                threading.Thread(target=server.serve_forever, daemon=True).start()
                endpoint = f"http://127.0.0.1:{server.server_port}/v1/systemone"
            return benchmark(args, connection, endpoint)
    finally:
        if server:
            server.shutdown()
            server.server_close()


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (ValueError, OSError) as exc:
        sys.exit(str(exc))
