#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = []
# ///
"""Download pinned JOB schema/queries and the real IMDb Parquet mirror; no database access."""
import argparse
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
from pathlib import Path, PurePosixPath
import re
import subprocess
import tarfile
import urllib.request

DEFAULT_DATA = Path(__file__).resolve().parent / "data"
COMMIT = "a39603662e023e449cb2121997a5034df9e02ebf"
REPOSITORY = "https://github.com/gregrahn/join-order-benchmark"
RELEASE = "https://api.github.com/repos/duckdb/duckdb-data/releases/tags/v1.0"
REFERENCES = {
    "aka_name": {"person_id": "name"},
    "aka_title": {"movie_id": "title", "kind_id": "kind_type", "episode_of_id": "title"},
    "cast_info": {"person_id": "name", "movie_id": "title", "person_role_id": "char_name", "role_id": "role_type"},
    "complete_cast": {"movie_id": "title", "subject_id": "comp_cast_type", "status_id": "comp_cast_type"},
    "movie_companies": {"movie_id": "title", "company_id": "company_name", "company_type_id": "company_type"},
    "movie_info": {"movie_id": "title", "info_type_id": "info_type"},
    "movie_info_idx": {"movie_id": "title", "info_type_id": "info_type"},
    "movie_keyword": {"movie_id": "title", "keyword_id": "keyword"},
    "movie_link": {"movie_id": "title", "linked_movie_id": "title", "link_type_id": "link_type"},
    "person_info": {"person_id": "name", "info_type_id": "info_type"},
    "title": {"kind_id": "kind_type", "episode_of_id": "title"},
}
LOOKUPS = ("comp_cast_type", "company_type", "info_type", "kind_type", "link_type", "role_type")
MOVIE_FACTS = ("aka_title", "cast_info", "complete_cast", "movie_companies", "movie_info",
               "movie_info_idx", "movie_keyword", "movie_link")
TABLES = sorted(set(LOOKUPS) | set(REFERENCES) | {"char_name", "company_name", "keyword", "name"})


def sha256(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def download(url, destination, size=None):
    if destination.exists() and (size is None or destination.stat().st_size == size):
        return
    partial = destination.with_suffix(destination.suffix + ".part")
    if destination.exists():
        destination.replace(partial)
    subprocess.run(["curl", "--fail", "--silent", "--show-error", "--location", "--retry", "3",
                    "--connect-timeout", "30", "--max-time", "1800", "--continue-at", "-",
                    "--proto", "=https", "--proto-redir", "=https", url, "--output", str(partial)], check=True)
    if size is not None and partial.stat().st_size != size:
        raise ValueError(f"Unexpected download size: {destination.name}")
    partial.replace(destination)
    print(f"Downloaded {destination.name}", flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", type=Path, default=DEFAULT_DATA)
    parser.add_argument("--queries-only", action="store_true", help="Download just the small schema/query archive, not 1.8 GB of Parquet")
    parser.add_argument("--workers", type=int, default=4)
    args = parser.parse_args()
    if args.workers < 1:
        parser.error("--workers must be positive")
    base = args.data_dir.resolve()
    upstream, parquet = base / "upstream", base / "parquet"
    upstream.mkdir(parents=True, exist_ok=True)
    parquet.mkdir(exist_ok=True)
    manifest_path = base / "manifest.json"
    previous = json.loads(manifest_path.read_text()) if manifest_path.exists() else {}
    archive_url = f"https://codeload.github.com/gregrahn/join-order-benchmark/tar.gz/{COMMIT}"
    archive_path = upstream / "repository.tgz"
    download(archive_url, archive_path)
    archive_hash = sha256(archive_path)
    if previous.get("schema_archive", {}).get("sha256", archive_hash) != archive_hash:
        raise ValueError("Cached schema archive checksum changed; remove it and download again")
    extracted = set()
    with tarfile.open(archive_path) as archive:
        for member in archive:
            parts = PurePosixPath(member.name).parts
            if not member.isfile() or len(parts) != 2 or parts[0] != "join-order-benchmark-" + COMMIT:
                continue
            name = parts[1]
            if name in ("schema.sql", "fkindexes.sql", "README.md") or re.fullmatch(r"[0-9]+[a-z]\.sql", name):
                if member.size > 2 * 1024 * 1024:
                    raise ValueError(f"Unexpected archive member size: {name}")
                (upstream / name).write_bytes(archive.extractfile(member).read())
                extracted.add(name)
    if not {"schema.sql", "fkindexes.sql", "README.md"} <= extracted or len(extracted) != 116:
        raise ValueError("Expected the pinned schema/indexes/README and 113 JOB queries")
    (upstream / "commit.txt").write_text(COMMIT + "\n")
    sources = previous.get("sources", {})
    if not args.queries_only:
        with urllib.request.urlopen(RELEASE, timeout=30) as response:
            release = json.load(response)
        (upstream / "duckdb-release.json").write_text(json.dumps(release, indent=2) + "\n")
        assets = {asset["name"]: asset for asset in release["assets"]}

        def fetch(table):
            asset = assets[f"job_{table}.parquet"]
            path = parquet / asset["name"]
            download(asset["browser_download_url"], path, asset["size"])
            digest = sha256(path)
            expected = sources.get(table, {}).get("sha256")
            published = asset.get("digest") or ""
            if (expected and digest != expected) or (published.startswith("sha256:") and published[7:] != digest):
                raise ValueError(f"Checksum mismatch: {path.name}; remove it and download again")
            return table, {"url": asset["browser_download_url"], "bytes": asset["size"], "sha256": digest}

        with ThreadPoolExecutor(max_workers=args.workers) as executor:
            sources = dict(executor.map(fetch, TABLES))
    manifest = {"schema_repository": REPOSITORY, "schema_commit": COMMIT,
                "schema_archive": {"url": archive_url, "sha256": archive_hash},
                "data_source": RELEASE, "sources": sources, "queries": 113,
                "warning": "DuckDB JOB Parquet mirror; not a proof of equivalence to every canonical CWI archive snapshot."}
    # Preserve sampling metadata when refreshing queries in an existing sample cache.
    if args.queries_only:
        manifest = {**previous, **manifest}
    manifest_path.write_text(json.dumps(manifest, indent=2) + "\n")
    print(f"Ready: {upstream} (113 queries)" + ("; Parquet download skipped" if args.queries_only else "; 21 Parquet tables"))


if __name__ == "__main__":
    main()
