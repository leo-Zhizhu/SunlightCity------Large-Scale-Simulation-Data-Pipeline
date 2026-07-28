#!/usr/bin/env python3
"""
Applies the right SQL files to the right instances.

Six numbered files, two roles, eleven instances. Doing it by hand is eleven psql
invocations in an order that matters, and the failure mode for getting it wrong —
running a shard file on the coordinator, or missing one shard out of ten — is
silent until the fleet has already been running for minutes.

USAGE
    python apply_schema.py --phase load      # before the fleet: 01-04
    python apply_schema.py --phase serve     # after the reduce:  06
    python apply_schema.py --phase all
    python apply_schema.py --phase load --dry-run

WHAT GOES WHERE
    01_cluster_topology.sql    coordinator   grid, sections, shard registry
    02_work_queue.sql          coordinator   lease queue, admission control
    03_shard_schema.sql        every shard   partitioned tables, directional API
    04_bulk_load_tuning.sql    BOTH          storage params (detects its own role)
    05_post_load_indexes.sql   every shard   applied by reduce_finalize.py, not here
    06_serving_federation.sql  coordinator   postgres_fdw over the shards

05 is deliberately absent from --phase load: it belongs to the reduce phase and
reduce_finalize.py runs it per shard, concurrently, after the load drains.

PREREQUISITE, and the one thing this script will not do for you: each shard needs
the static geometry (meo_waypoints, meo_edges, meo_sample_points) before
03_shard_schema.sql will install — it refuses on an instance that cannot answer a
directional query, because failing at install time beats failing per-task later.
Create the empty v1 tables first (docs/DEPLOYMENT.md step 4); plan_tasks.py
--provision then fills them.
"""

from __future__ import annotations

import argparse
import os
import sys

try:
    import psycopg2
except ImportError:
    sys.exit("psycopg2 is required:  pip install psycopg2-binary")

from cluster import ClusterEndpoints


# (filename, role) — role is 'coordinator', 'shard', or 'both'
PHASES = {
    "load": [
        ("01_cluster_topology.sql",  "coordinator"),
        ("02_work_queue.sql",        "coordinator"),
        ("03_shard_schema.sql",      "shard"),
        ("04_bulk_load_tuning.sql",  "both"),
    ],
    "serve": [
        ("06_serving_federation.sql", "coordinator"),
    ],
}
PHASES["all"] = PHASES["load"] + PHASES["serve"]


def strip_psql_meta(sql: str) -> str:
    """
    Removes psql backslash meta-commands, which are not valid over a normal
    connection. Everything else — including DO blocks, dollar quoting and
    transaction control — is passed through untouched.

    Only whole lines starting with a backslash are dropped, so a '\\' inside a
    string literal or a LIKE pattern (03 has several: 'meo\\_exp\\_s%') survives.
    """
    return "\n".join(l for l in sql.splitlines() if not l.lstrip().startswith("\\"))


def apply_file(conn, path: str, label: str, dry_run: bool) -> None:
    if not os.path.exists(path):
        raise FileNotFoundError(f"SQL file not found: {path}")

    sql = strip_psql_meta(open(path, encoding="utf-8").read())
    name = os.path.basename(path)

    if dry_run:
        print(f"    would apply {name} to {label} ({len(sql.splitlines())} lines)")
        return

    # autocommit, because several of these files manage their own BEGIN/COMMIT and
    # a wrapping transaction would make CREATE EXTENSION and ALTER DATABASE fail.
    conn.autocommit = True
    cur = conn.cursor()
    cur.execute(sql)

    # Surface the NOTICEs. They are not noise here: 03 announces which
    # v1-compatibility views it created, and 04 announces which ROLE it decided it
    # was — the single most useful line for confirming the file landed where intended.
    for n in conn.notices[-12:]:
        print(f"      {n.strip()}")
    del conn.notices[:]
    print(f"    applied {name} to {label}")


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--phase", choices=sorted(PHASES), default="load")
    p.add_argument("--shards", type=int, default=None,
                   help="shard count (default: read from the coordinator's meo_shards, "
                        "falling back to SUNLIT_SHARD_COUNT)")
    p.add_argument("--only-shard", type=int, default=None,
                   help="apply shard files to just this instance (for replacing one)")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--coord-host"); p.add_argument("--coord-port")
    p.add_argument("--coord-db"); p.add_argument("--db-user"); p.add_argument("--db-password")
    p.add_argument("--sql-dir",
                   default=os.environ.get("SUNLIT_SQL_DIR") or
                           os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "db"))
    args = p.parse_args()

    sql_dir = os.path.normpath(args.sql_dir)
    endpoints = ClusterEndpoints.from_environment()

    coord = dict(
        host=args.coord_host or os.environ.get("SUNLIT_COORD_HOST", "localhost"),
        port=int(args.coord_port or os.environ.get("SUNLIT_COORD_PORT", 5432)),
        dbname=args.coord_db or os.environ.get("SUNLIT_COORD_DB", "sunlit_coord"),
        user=args.db_user or os.environ.get("SUNLIT_DB_USER", "admin"),
        password=args.db_password or os.environ.get("SUNLIT_DB_PASSWORD", "password"),
    )

    print("=" * 78)
    print(f"  Applying schema — phase '{args.phase}'")
    print("=" * 78)
    print(f"  coordinator : {coord['user']}@{coord['host']}:{coord['port']}/{coord['dbname']}")

    # ---- How many shards -------------------------------------------------
    if args.only_shard is not None:
        shard_ids = [args.only_shard]
    elif args.shards is not None:
        shard_ids = list(range(args.shards))
    else:
        # Prefer the coordinator's own registry over an environment variable: it is
        # the authority, and reading it means this cannot disagree with what
        # plan_tasks.py registered.
        shard_ids = None

    files = PHASES[args.phase]
    need_coord = any(r in ("coordinator", "both") for _, r in files)
    need_shards = any(r in ("shard", "both") for _, r in files)

    try:
        coord_conn = psycopg2.connect(**coord)
    except psycopg2.Error as e:
        print(f"\nERROR: coordinator unreachable: {str(e).splitlines()[0]}", file=sys.stderr)
        return 1

    try:
        if shard_ids is None:
            try:
                coord_conn.autocommit = True
                c = coord_conn.cursor()
                c.execute("SELECT shard_index FROM meo_shards ORDER BY shard_index")
                shard_ids = [r[0] for r in c.fetchall()]
            except psycopg2.Error:
                # meo_shards does not exist yet — expected on a first run, since
                # 01_cluster_topology.sql is what creates it.
                coord_conn.rollback()
                shard_ids = list(range(int(os.environ.get("SUNLIT_SHARD_COUNT", "10"))))
                print(f"  (meo_shards not present yet; using SUNLIT_SHARD_COUNT="
                      f"{len(shard_ids)})")

        print(f"  shards      : {len(shard_ids)}  "
              f"{endpoints.shard_host_template.format(i='N')}:{endpoints.shard_port}")
        print()

        # ---- Coordinator files, in order ---------------------------------
        if need_coord:
            print("  COORDINATOR")
            for name, role in files:
                if role in ("coordinator", "both"):
                    apply_file(coord_conn, os.path.join(sql_dir, name),
                               "coordinator", args.dry_run)
            print()

        # ---- Shard files -------------------------------------------------
        if need_shards:
            for i in shard_ids:
                sh = endpoints.shard(i)
                label = f"shard {i} ({sh['host']}:{sh['port']}/{sh['dbname']})"
                print(f"  {label}")

                if args.dry_run:
                    for name, role in files:
                        if role in ("shard", "both"):
                            apply_file(None, os.path.join(sql_dir, name),
                                       f"shard {i}", True)
                    print()
                    continue

                try:
                    conn = psycopg2.connect(
                        host=sh["host"], port=sh["port"], dbname=sh["dbname"],
                        user=sh["user"], password=coord["password"])
                except psycopg2.Error as e:
                    print(f"    ERROR unreachable: {str(e).splitlines()[0]}",
                          file=sys.stderr)
                    return 1

                try:
                    conn.autocommit = True
                    conn.cursor().execute("CREATE EXTENSION IF NOT EXISTS postgis")
                    for name, role in files:
                        if role in ("shard", "both"):
                            apply_file(conn, os.path.join(sql_dir, name),
                                       f"shard {i}", False)
                finally:
                    conn.close()
                print()

    except psycopg2.Error as e:
        print(f"\nERROR: {e}", file=sys.stderr)
        return 1
    except FileNotFoundError as e:
        print(f"\nERROR: {e}", file=sys.stderr)
        return 1
    finally:
        coord_conn.close()

    print("=" * 78)
    if args.dry_run:
        print("  DRY RUN — nothing applied.")
    elif args.phase in ("load", "all"):
        print("  Schema in place. Next:")
        print("    python plan_tasks.py --run-id <id> --shards "
              f"{len(shard_ids)} --provision")
    else:
        print("  Federation SQL in place. Bring it up with reduce_finalize.py.")
    print("=" * 78)
    return 0


if __name__ == "__main__":
    sys.exit(main())
