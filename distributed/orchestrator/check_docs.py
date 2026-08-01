#!/usr/bin/env python3
"""
Checks that the documentation still agrees with model.py.

    python distributed/orchestrator/check_docs.py          # exits non-zero on failure
    python distributed/orchestrator/check_docs.py -v       # list every check

WHY THIS EXISTS
---------------
The deployment shape has been re-derived twice, and each time a handful of figures
survived the sweep in some spelling nobody grepped for. "11m 38s" was found and fixed;
"11 min 38 s" — the same number, three spaces — sat in the README's first sentence, in
DEPLOYMENT's opening line and in V1_PIPELINE's comparison table for another two
commits. A `git grep` for the value you already know is wrong is not a check; it is a
memory test that the codebase keeps failing.

So the rules below are written against the CONCEPT, not the spelling:

  * every quantity is matched with flexible whitespace, so "11m 38s", "11 min 38 s"
    and "11 m 38 s" are all one pattern;
  * superseded values are listed explicitly, because a stale number is far more
    damaging than a missing one — it reads as authoritative;
  * the canonical value is recomputed from model.py on every run, so this file cannot
    drift either.

TWO THINGS THIS DELIBERATELY DOES NOT DO
----------------------------------------
It does not check verbatim tool output. `reduce_finalize.py` prints "11m 08.7s" via
model.fmt() while the prose says "11m 09s" via the figures' fmt_t(); both are correct
and a transcript must match the tool, not the prose. Blocks that are quoted tool output
are skipped by filename+content heuristics below.

It does not attempt to parse prose. Anything subtler than "this exact quantity appears
with the wrong value" belongs in review, not here.
"""

from __future__ import annotations

import os
import re
import sys

import model

ROOT = os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                     "..", ".."))
SCAN_EXT = (".md", ".py", ".sql", ".yaml", ".yml", ".sh", ".conf", ".cs")


def flex(text: str) -> str:
    """A regex matching `text` with any run of whitespace where it has spaces —
    so one rule covers '11m 38s', '11 min 38 s' and '11  m  38  s'."""
    parts = [re.escape(p) for p in text.split()]
    return r"\s*".join(parts)


# ---------------------------------------------------------------------------
# The canonical facts, recomputed from model.py, and the values they replaced.
#
# `bad` entries are written loosely on purpose: "11 min 38 s" and "11m38s" collapse to
# the same pattern, because the whole point is to catch the spelling nobody thought of.
# ---------------------------------------------------------------------------
def facts() -> list[tuple[str, str, list[str]]]:
    m = model
    total_disp = f"{int(m.total_seconds() // 60)}m {round(m.total_seconds() % 60):02d}s"
    return [
        ("deployment: workers", str(m.WORKERS),
         [r"\b50\s+(?:Kubernetes\s+)?(?:map\s+)?workers?\b",
          r"\bworkers?\s*[:=]\s*50\b", r"\b50\s+pods\b"]),
        ("deployment: data shards", str(m.SHARDS),
         [r"\b10\s+(?:data\s+)?shards?\b", r"\bten\s+shards?\b",
          r"\bshards?\s*[:=]\s*10\b"]),
        ("deployment: db instances", str(m.SHARDS + 1),
         [r"\b11\s+(?:PostgreSQL\s+|database\s+)?instances?\b"]),
        ("wall clock, achieved", total_disp,
         [flex("11 min 38 s"), flex("11m 38s"), flex("3 m 20 s"),
          flex("2 m 35 s")]),
        ("speedup, work-normalised", f"{m.speedup():.1f}x",
         # NB: 164.0x, 176.0x etc. appear legitimately in the shard-sweep table as the
         # speedup a DIFFERENT shard count would give, so only the superseded
         # HEADLINE values are listed here.
         [r"\b154\.7\s*[x×]", r"\b155\s*[x×]\s+total"]),
        ("throughput ceiling", f"{m.WORKERS * 4.05:.1f}x",
         [r"\b202\.5\s*[x×]"]),
        ("total vCPU", str(m.vcpu()), [r"\b572\s+vCPU\b", r"\b400\s+vCPU\b"]),
        ("total RAM (GB)", f"{m.ram_gb():,}", [r"\b2,?114\b"]),
        ("cost (vCPU-hours)", f"{m.vcpu() * m.total_seconds() / 3600:.0f}",
         [r"~?\s*111\s+vCPU-hours"]),
        ("rows per shard", f"{m.EXPOSURE_ROWS // m.SHARDS:,}",
         [r"\b788,687,280\b", r"\b789\s*[Mm](?:illion)?\b"]),
        ("leaves per shard", f"{m.tasks() // m.SHARDS:,}", [r"\b3,024\b"]),
        ("edge rows per shard", f"{m.EDGE_ROWS // m.SHARDS / 1e6:.1f}M",
         [r"\b14\.5\s*M\b"]),
        ("fleet row rate", f"{m.WORKERS * m.WORKER_ROW_RATE:,.0f}",
         [r"\b14,?787,?900\b", r"\b14\.8\s*M\b"]),
        ("cluster worth", f"{m.total_seconds(m.WORKERS, 1) / m.total_seconds():.1f}x",
         [r"\b6\.1\s*[x×]"]),
        ("per-task ray time", "0.88 s", [flex("~1.8 s"), flex("3.1 s per task")]),
        ("socket share", "43%", [r"\b42%\s+of\s+(?:its|the)\b"]),
        ("affinity group size", f"{m.DAYS}",
         [r"twelve\s+dates\s+per\s+group", r"twelve\s+times\s+out\s+of\s+twelve"]),
        ("deadline", "15 minutes",
         [r"\b11[-\s]minute\s+(?:target|deadline|budget)\b",
          r"\b12[-\s]minute\s+(?:runtime|target)\b"]),
    ]


# Files whose fenced blocks are verbatim tool output: their numbers must match the
# TOOL, not the prose convention, so they are exempt from the wall-clock spelling rule.
TRANSCRIPT_HINTS = ("map wall clock", "vs the deadline", "shard load vs cap",
                    "SunlightCity · run", "write imbalance")


def scan() -> list[str]:
    problems = []
    checks = facts()
    for dirpath, dirnames, filenames in os.walk(ROOT):
        dirnames[:] = [d for d in dirnames
                       if d not in (".git", "__pycache__", "node_modules", "Library")]
        for fn in filenames:
            if not (fn.endswith(SCAN_EXT) or fn.startswith("Dockerfile")):
                continue
            path = os.path.join(dirpath, fn)
            rel = os.path.relpath(path, ROOT)
            if rel.startswith("distributed/orchestrator/check_docs.py"):
                continue          # this file lists the stale values on purpose
            try:
                text = open(path, encoding="utf-8", errors="replace").read()
            except OSError:
                continue
            transcript = any(h in text for h in TRANSCRIPT_HINTS)
            for name, good, bads in checks:
                for bad in bads:
                    for mo in re.finditer(bad, text):
                        if transcript and name == "wall clock, achieved":
                            continue
                        line = text[:mo.start()].count("\n") + 1
                        problems.append(
                            f"{rel}:{line}  {name}: found {mo.group(0)!r}, "
                            f"canonical is {good!r}")
    return problems


def main() -> int:
    verbose = "-v" in sys.argv
    checks = facts()
    if verbose:
        print(f"{'quantity':<28}{'canonical':>18}   superseded patterns")
        print("-" * 78)
        for name, good, bads in checks:
            print(f"{name:<28}{good:>18}   {len(bads)}")
        print()
    problems = scan()
    for p in problems:
        print(f"  STALE  {p}")
    print(f"\n  {len(checks)} quantities checked against model.py across the tree "
          f"-> {len(problems)} stale reference(s)")
    return 1 if problems else 0


if __name__ == "__main__":
    raise SystemExit(main())
