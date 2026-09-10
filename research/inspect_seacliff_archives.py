#!/usr/bin/env python3
"""List the static input/output contract of pulled Seacliff Bolt archives."""

from __future__ import annotations

import hashlib
import argparse
import json
import zipfile
from pathlib import Path


def inspect(
    path: Path,
    tail_nodes: int = 0,
    node_ids: set[int] | None = None,
) -> dict[str, object]:
    archive_bytes = path.read_bytes()
    with zipfile.ZipFile(path) as archive:
        pickle_bytes = archive.read("model/data.pkl")
    marker = b'{"version": "HEXAGON'
    start = pickle_bytes.find(marker)
    if start < 0:
        raise ValueError(f"No lowered HEXAGON graph found in {path}")
    graph, _ = json.JSONDecoder().raw_decode(
        pickle_bytes[start:].decode("utf-8", errors="ignore")
    )
    result: dict[str, object] = {
        "path": str(path),
        "bytes": len(archive_bytes),
        "sha256": hashlib.sha256(archive_bytes).hexdigest(),
        "input": graph.get("input", []),
        "output": graph.get("output", []),
    }
    if tail_nodes:
        result["tail_nodes"] = [
            {
                key: node.get(key)
                for key in ("id", "name", "op", "input", "output", "comment")
            }
            for node in graph.get("node", [])[-tail_nodes:]
        ]
    if node_ids:
        result["selected_nodes"] = [
            {
                key: node.get(key)
                for key in ("id", "name", "op", "input", "output", "comment")
            }
            for node in graph.get("node", [])
            if node.get("id") in node_ids
        ]
    return result


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("archives", nargs="+")
    parser.add_argument("--tail-nodes", type=int, default=0)
    parser.add_argument("--node-ids", default="")
    arguments = parser.parse_args()
    node_ids = {
        int(value) for value in arguments.node_ids.split(",") if value.strip()
    }
    print(json.dumps([
        inspect(Path(value), arguments.tail_nodes, node_ids)
        for value in arguments.archives
    ], indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
