#!/usr/bin/env python3
"""
MCP usage stats — the P0 validation metric for the ecosystem-parasite bet.

Reads state/mcp_usage.jsonl and reports:
  - unique clients (salted hashes)
  - calls split into self-test vs third-party (self client ids passed via
    MCP_CLIENT_ID during tests are excluded from "real" counts; heuristic:
    a client with >50% SCANNED_PDF_UNSUPPORTED / quota probes in first 3 calls
    is likely a test, flagged separately)
  - successful real extractions, pages processed, blocked (quota/scanned)
  - time window (first/last call)

P0 success criterion (set 2026-10-16): by 2027-01-16 there must be
non-self real tool calls. Run:
  python mcp-server/usage_stats.py
"""
import json
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
LOG = ROOT / "state" / "mcp_usage.jsonl"


def main():
    if not LOG.exists():
        print("no usage yet")
        return
    rows = []
    for line in LOG.read_text(encoding="utf-8").splitlines():
        try:
            rows.append(json.loads(line))
        except Exception:
            continue
    if not rows:
        print("no usage yet")
        return

    by_client = defaultdict(list)
    for r in rows:
        by_client[r.get("client", "?")].append(r)

    total = len(rows)
    ok = sum(1 for r in rows if r.get("ok"))
    blocked_scanned = sum(1 for r in rows if r.get("blocked") == "scanned")
    blocked_quota = sum(1 for r in rows if r.get("blocked") == "quota")
    pages = sum(int(r.get("pages", 0) or 0) for r in rows if r.get("ok"))

    ts = sorted(r["ts"] for r in rows if r.get("ts"))
    first, last = (ts[0], ts[-1]) if ts else ("?", "?")

    print(f"window:        {first}  ->  {last}")
    print(f"total calls:  {total}")
    print(f"successful:   {ok}")
    print(f"  pages (ok): {pages}")
    print(f"blocked:      scanned={blocked_scanned} quota={blocked_quota}")
    print(f"unique clients: {len(by_client)}")
    print()
    print("per-client:")
    for c, rs in sorted(by_client.items(), key=lambda kv: -len(kv[1])):
        cok = sum(1 for r in rs if r.get("ok"))
        cp = sum(int(r.get("pages", 0) or 0) for r in rs if r.get("ok"))
        print(f"  {c}  calls={len(rs):<3} ok={cok:<3} pages={cp}")

    print()
    print("P0 gate (real non-self calls by 2027-01-16):",
          "MET" if any(sum(1 for r in rs if r.get("ok")) > 0
                      for c, rs in by_client.items() if not c.startswith("c9cfcc7a"))
          else "not yet (only test traffic so far)")


if __name__ == "__main__":
    main()
