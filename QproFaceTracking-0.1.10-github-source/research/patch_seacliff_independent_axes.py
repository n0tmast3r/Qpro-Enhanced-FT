#!/usr/bin/env python3
"""Create a Seacliff archive exposing its per-eye local gaze branch.

The stock graph blends a local per-eye 2x2 prediction (node 18) with a
binocular/cross-eye 2x2 prediction before reshaping it as the public 1x4 gaze
head.  Both tensors contain four floats.  This research patch redirects only
the existing reshape input from final blend node 50 to local node 18; model
inputs, output count, output shape, and the native decoder contract remain
unchanged.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import zipfile
from pathlib import Path


GRAPH_MEMBER = "model/data.pkl"
GRAPH_MARKER = b'{"version": "HEXAGON'
OLD_NODE = (
    b'"id": 52, "name": "211_reshape", "op": "OP_Reshape", '
    b'"padding": "NN_PAD_NA", "input": [[50, 0], [51, 0]]'
)
NEW_NODE = OLD_NODE.replace(b'[[50, 0]', b'[[18, 0]')


def graph_from_pickle(payload: bytes) -> dict[str, object]:
    start = payload.find(GRAPH_MARKER)
    if start < 0:
        raise ValueError("Lowered HEXAGON graph was not found")
    graph, _ = json.JSONDecoder().raw_decode(
        payload[start:].decode("utf-8", errors="ignore")
    )
    return graph


def validate_contract(graph: dict[str, object], *, patched: bool) -> None:
    nodes = {node["id"]: node for node in graph["node"]}
    if nodes[18]["output"][0]["shape"] != [2, 2]:
        raise ValueError("Unexpected local gaze node contract")
    expected_input = 18 if patched else 50
    if nodes[52]["input"] != [[expected_input, 0], [51, 0]]:
        raise ValueError("Unexpected public gaze reshape input")
    if nodes[52]["output"][0]["shape"] != [1, 4]:
        raise ValueError("Unexpected public gaze output contract")
    if [item["shape"] for item in graph["output"]] != [
        [1, 4], [1, 6], [1, 6], [1, 1], [1, 1]
    ]:
        raise ValueError("Unexpected Seacliff model outputs")


def patch(source: Path, destination: Path) -> dict[str, object]:
    if source.resolve() == destination.resolve():
        raise ValueError("Destination must not overwrite the source archive")
    with zipfile.ZipFile(source, "r") as archive:
        members = [(info, archive.read(info.filename)) for info in archive.infolist()]
    member_map = dict((info.filename, payload) for info, payload in members)
    original = member_map[GRAPH_MEMBER]
    graph = graph_from_pickle(original)
    reshape_input = {node["id"]: node for node in graph["node"]}[52]["input"]
    if reshape_input == [[18, 0], [51, 0]]:
        validate_contract(graph, patched=True)
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, destination)
        with zipfile.ZipFile(destination, "r") as verification:
            verification.testzip()
            validate_contract(
                graph_from_pickle(verification.read(GRAPH_MEMBER)), patched=True
            )
        result = destination.read_bytes()
        return {
            "source": str(source),
            "destination": str(destination),
            "bytes": len(result),
            "sha256": hashlib.sha256(result).hexdigest(),
            "redirect": "public gaze reshape already uses local per-eye node 18",
            "alreadyPatched": True,
        }

    validate_contract(graph, patched=False)
    if original.count(OLD_NODE) != 1:
        raise ValueError("The expected public gaze reshape was not unique")
    modified = original.replace(OLD_NODE, NEW_NODE, 1)
    if len(modified) != len(original):
        raise AssertionError("The graph patch must remain byte-length preserving")
    validate_contract(graph_from_pickle(modified), patched=True)

    destination.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(destination, "w") as output:
        for info, payload in members:
            output.writestr(info, modified if info.filename == GRAPH_MEMBER else payload)
    with zipfile.ZipFile(destination, "r") as verification:
        verification.testzip()
        validate_contract(
            graph_from_pickle(verification.read(GRAPH_MEMBER)), patched=True
        )
    result = destination.read_bytes()
    return {
        "source": str(source),
        "destination": str(destination),
        "bytes": len(result),
        "sha256": hashlib.sha256(result).hexdigest(),
        "redirect": "public gaze reshape: final blend node 50 -> local per-eye node 18",
        "alreadyPatched": False,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("source", type=Path)
    parser.add_argument("destination", type=Path)
    arguments = parser.parse_args()
    print(json.dumps(patch(arguments.source, arguments.destination), indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
