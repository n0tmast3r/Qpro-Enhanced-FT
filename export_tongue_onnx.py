#!/usr/bin/env python3
"""Convert tongue checkpoints to ONNX twins for the low-memory live runtime.

Live tongue inference runs each checkpoint's ONNX twin through ONNX Runtime +
DirectML, so the receiver never loads PyTorch/CUDA. Converting needs PyTorch,
so it runs in this short-lived helper process and that memory is released when
it exits. Each twin records the SHA-256 of its checkpoint, so a changed
checkpoint is converted again.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
import warnings
from pathlib import Path

# Bump when the export or its metadata changes so existing twins are rebuilt.
ONNX_FORMAT = "1"


def onnx_path(checkpoint: Path) -> Path:
    return checkpoint.with_suffix(".onnx")


def checkpoint_digest(checkpoint: Path) -> str:
    return hashlib.sha256(checkpoint.read_bytes()).hexdigest()


def export(checkpoint: Path) -> Path:
    import numpy as np
    import onnx
    import onnxruntime as ort
    import torch

    from train_tongue_model import create_model

    state = torch.load(checkpoint, map_location="cpu", weights_only=False)
    architecture = str(state.get("architecture", "legacy-late-fusion-v1"))
    target_names = list(state["targetNames"])
    image_size = int(state["imageSize"])
    model = create_model(architecture, target_names)
    model.load_state_dict(state["modelState"])
    model.eval()

    target = onnx_path(checkpoint)
    temporary = target.with_name(target.name + ".tmp")
    sample = torch.rand(1, 2, image_size, image_size, generator=torch.Generator().manual_seed(0))
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")  # exporter deprecation/tracer notices
        torch.onnx.export(
            model, (sample,), str(temporary), input_names=["cameras"],
            output_names=["values"], opset_version=17, dynamo=False,
        )
    proto = onnx.load(str(temporary))
    for key, value in {
        "qproTongueOnnxFormat": ONNX_FORMAT,
        "sourceSha256": checkpoint_digest(checkpoint),
        "architecture": architecture,
        "targetNames": json.dumps(target_names),
        "imageSize": str(image_size),
        "visibilityGate": json.dumps(state.get("visibilityGate", {}), default=float),
    }.items():
        entry = proto.metadata_props.add()
        entry.key, entry.value = key, value
    onnx.save(proto, str(temporary))

    # Refuse a twin that does not reproduce its checkpoint (for example after a
    # future exporter change); the live runtime then keeps using PyTorch.
    with torch.inference_mode():
        expected = model(sample).numpy()
    session = ort.InferenceSession(str(temporary), providers=["CPUExecutionProvider"])
    actual = session.run(None, {"cameras": sample.numpy()})[0]
    del session
    error = float(np.abs(expected - actual).max())
    if error > 1e-3:
        temporary.unlink(missing_ok=True)
        raise RuntimeError(f"the ONNX twin does not match the checkpoint (max error {error:.2e})")
    temporary.replace(target)
    return target


def main() -> int:
    parser = argparse.ArgumentParser(description="Convert tongue checkpoints to ONNX twins.")
    parser.add_argument("checkpoints", nargs="+", type=Path)
    arguments = parser.parse_args()
    for checkpoint in arguments.checkpoints:
        checkpoint = checkpoint.resolve()
        try:
            target = export(checkpoint)
        except Exception as error:  # noqa: BLE001 - report and let the caller fall back
            print(f"ONNX conversion failed for {checkpoint.name}: {error}", file=sys.stderr)
            return 1
        print(f"ONNX twin ready: {target.name}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
