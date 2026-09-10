#!/usr/bin/env python3
"""Train the first multi-camera Quest Pro expression and eye-pose model."""

from __future__ import annotations

import argparse
import json
import random
from pathlib import Path

import numpy as np
import torch
from torch import nn
from torch.utils.data import DataLoader, Dataset


class CachedFrames(Dataset):
    def __init__(self, cache: Path, indices: np.ndarray, augment: bool) -> None:
        self.images = np.load(cache / "images.npy", mmap_mode="r")
        self.expressions = np.load(cache / "expressions.npy", mmap_mode="r")
        self.eye_orientations = np.load(
            cache / "eye_orientations.npy", mmap_mode="r"
        )
        self.indices = indices
        self.augment = augment

    def __len__(self) -> int:
        return len(self.indices)

    def __getitem__(self, item: int) -> tuple[torch.Tensor, torch.Tensor, torch.Tensor]:
        index = int(self.indices[item])
        images = torch.from_numpy(np.array(self.images[index], copy=True)).to(
            dtype=torch.float32
        ).div_(255)
        if self.augment:
            contrast = random.uniform(0.90, 1.10)
            brightness = random.uniform(-0.04, 0.04)
            images.mul_(contrast).add_(brightness).clamp_(0, 1)
        expressions = torch.from_numpy(
            np.array(self.expressions[index], copy=True)
        )
        orientations = torch.from_numpy(
            np.array(self.eye_orientations[index], copy=True)
        )
        return images, expressions, orientations


class CameraEncoder(nn.Module):
    def __init__(self) -> None:
        super().__init__()
        channels = (1, 16, 32, 48, 64)
        layers: list[nn.Module] = []
        for input_channels, output_channels in zip(channels, channels[1:]):
            layers.extend(
                [
                    nn.Conv2d(
                        input_channels, output_channels, 3, stride=2, padding=1,
                        bias=False,
                    ),
                    nn.BatchNorm2d(output_channels),
                    nn.SiLU(inplace=True),
                ]
            )
        layers.append(nn.AdaptiveAvgPool2d(1))
        self.network = nn.Sequential(*layers)

    def forward(self, image: torch.Tensor) -> torch.Tensor:
        return self.network(image).flatten(1)


class QuestProTrackingModel(nn.Module):
    """Separate symmetric encoders with late feature fusion and two heads."""

    def __init__(self, expression_count: int = 70) -> None:
        super().__init__()
        self.eye_encoder = CameraEncoder()
        self.face_encoder = CameraEncoder()
        self.brow_encoder = CameraEncoder()
        self.fusion = nn.Sequential(
            nn.Linear(64 * 5, 256),
            nn.SiLU(inplace=True),
            nn.Dropout(0.10),
            nn.Linear(256, 160),
            nn.SiLU(inplace=True),
        )
        self.expression_head = nn.Linear(160, expression_count)
        self.eye_orientation_head = nn.Sequential(
            nn.Linear(64 * 2, 96), nn.SiLU(inplace=True), nn.Linear(96, 8)
        )

    def forward(self, cameras: torch.Tensor) -> tuple[torch.Tensor, torch.Tensor]:
        left_eye = self.eye_encoder(cameras[:, 0:1])
        right_eye = self.eye_encoder(cameras[:, 1:2])
        left_face = self.face_encoder(cameras[:, 2:3])
        right_face = self.face_encoder(cameras[:, 3:4])
        brow = self.brow_encoder(cameras[:, 4:5])
        fused = self.fusion(
            torch.cat((left_eye, right_eye, left_face, right_face, brow), dim=1)
        )
        expressions = torch.sigmoid(self.expression_head(fused))
        orientations = self.eye_orientation_head(torch.cat((left_eye, right_eye), dim=1))
        left_orientation = nn.functional.normalize(orientations[:, :4], dim=1)
        right_orientation = nn.functional.normalize(orientations[:, 4:], dim=1)
        return expressions, torch.cat((left_orientation, right_orientation), dim=1)


def blocked_split(
    step_ids: np.ndarray, block_size: int = 45
) -> tuple[np.ndarray, np.ndarray]:
    """Hold out short temporal blocks within every prompted expression step."""
    count = len(step_ids)
    indices = np.arange(count, dtype=np.int32)
    validation = np.zeros(count, dtype=bool)
    for step in np.unique(step_ids):
        step_indices = indices[step_ids == step]
        local_blocks = np.arange(len(step_indices)) // block_size
        validation[step_indices] = local_blocks % 5 == 4
    return indices[~validation], indices[validation]


def quaternion_loss(prediction: torch.Tensor, target: torch.Tensor) -> torch.Tensor:
    losses = []
    for offset in (0, 4):
        predicted = nn.functional.normalize(prediction[:, offset:offset + 4], dim=1)
        expected = nn.functional.normalize(target[:, offset:offset + 4], dim=1)
        dot = torch.sum(predicted * expected, dim=1).abs().clamp(max=1)
        losses.append(1 - dot.square())
    return torch.cat(losses).mean()


def run_epoch(
    model: QuestProTrackingModel,
    loader: DataLoader,
    device: torch.device,
    optimizer: torch.optim.Optimizer | None,
    scaler: torch.amp.GradScaler | None,
) -> tuple[float, float, float, float]:
    training = optimizer is not None
    model.train(training)
    total_loss = 0.0
    total_expression_mae = 0.0
    total_active_error = 0.0
    total_active_values = 0
    total_quaternion_loss = 0.0
    samples = 0
    for cameras, expressions, orientations in loader:
        cameras = cameras.to(device, non_blocking=True)
        expressions = expressions.to(device, non_blocking=True)
        orientations = orientations.to(device, non_blocking=True)
        if training:
            optimizer.zero_grad(set_to_none=True)
        with torch.set_grad_enabled(training), torch.amp.autocast(
            device_type=device.type, enabled=device.type == "cuda"
        ):
            predicted_expressions, predicted_orientations = model(cameras)
            expression_elements = nn.functional.smooth_l1_loss(
                predicted_expressions, expressions, beta=0.05, reduction="none"
            )
            expression_loss = torch.mean(expression_elements * (1 + 5 * expressions))
            orientation_loss = quaternion_loss(predicted_orientations, orientations)
            loss = expression_loss + 0.20 * orientation_loss
        if training and optimizer is not None:
            if scaler is not None:
                scaler.scale(loss).backward()
                scaler.step(optimizer)
                scaler.update()
            else:
                loss.backward()
                optimizer.step()
        batch = cameras.shape[0]
        total_loss += float(loss.detach()) * batch
        total_expression_mae += float(
            torch.mean(torch.abs(predicted_expressions - expressions)).detach()
        ) * batch
        active = expressions > 0.10
        total_active_error += float(
            torch.sum(torch.abs(predicted_expressions - expressions)[active]).detach()
        )
        total_active_values += int(torch.sum(active))
        total_quaternion_loss += float(orientation_loss.detach()) * batch
        samples += batch
    return (
        total_loss / samples,
        total_expression_mae / samples,
        total_active_error / max(1, total_active_values),
        total_quaternion_loss / samples,
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="Train a Qpro five-camera model")
    parser.add_argument("cache")
    parser.add_argument("--epochs", type=int, default=30)
    parser.add_argument("--batch-size", type=int, default=128)
    parser.add_argument("--learning-rate", type=float, default=3e-4)
    parser.add_argument("--device", default="cuda:0")
    parser.add_argument("--output", default="models/qpro-five-camera.pt")
    arguments = parser.parse_args()
    if arguments.epochs < 1 or arguments.batch_size < 1:
        parser.error("epochs and batch size must be positive")

    torch.manual_seed(20260805)
    np.random.seed(20260805)
    random.seed(20260805)
    cache = Path(arguments.cache).resolve()
    metadata = json.loads((cache / "metadata.json").read_text(encoding="utf-8"))
    if not metadata.get("complete"):
        raise ValueError("Training cache is incomplete")
    step_ids = np.load(cache / "step_ids.npy", mmap_mode="r")
    train_indices, validation_indices = blocked_split(step_ids)
    train_data = CachedFrames(cache, train_indices, augment=True)
    validation_data = CachedFrames(cache, validation_indices, augment=False)
    device = torch.device(arguments.device)
    if device.type == "cuda" and not torch.cuda.is_available():
        raise RuntimeError("CUDA was requested but PyTorch cannot access an NVIDIA GPU")
    train_loader = DataLoader(
        train_data, batch_size=arguments.batch_size, shuffle=True,
        num_workers=0, pin_memory=device.type == "cuda",
    )
    validation_loader = DataLoader(
        validation_data, batch_size=arguments.batch_size, shuffle=False,
        num_workers=0, pin_memory=device.type == "cuda",
    )
    model = QuestProTrackingModel(len(metadata["expressionNames"])).to(device)
    optimizer = torch.optim.AdamW(
        model.parameters(), lr=arguments.learning_rate, weight_decay=1e-4
    )
    scaler = torch.amp.GradScaler("cuda") if device.type == "cuda" else None
    output = Path(arguments.output).resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    best_mae = float("inf")
    validation_targets = np.asarray(
        np.load(cache / "expressions.npy", mmap_mode="r")[validation_indices]
    )
    zero_mae = float(np.mean(np.abs(validation_targets)))
    active_mask = validation_targets > 0.10
    zero_active_mae = float(np.mean(np.abs(validation_targets[active_mask])))
    print(
        f"Device: {device}; train frames: {len(train_data)}; "
        f"validation frames: {len(validation_data)}\n"
        f"Zero baseline: overall MAE={zero_mae:.5f}; "
        f"active-target MAE={zero_active_mae:.5f}"
    )
    for epoch in range(1, arguments.epochs + 1):
        train_metrics = run_epoch(model, train_loader, device, optimizer, scaler)
        with torch.no_grad():
            validation_metrics = run_epoch(
                model, validation_loader, device, None, None
            )
        print(
            f"Epoch {epoch:02d}: train loss={train_metrics[0]:.5f} "
            f"expr_mae={train_metrics[1]:.5f} active={train_metrics[2]:.5f}; "
            f"val loss={validation_metrics[0]:.5f} expr_mae={validation_metrics[1]:.5f} "
            f"active={validation_metrics[2]:.5f} eye_q={validation_metrics[3]:.5f}"
        )
        if validation_metrics[1] < best_mae:
            best_mae = validation_metrics[1]
            torch.save(
                {
                    "version": 1,
                    "modelState": model.state_dict(),
                    "expressionNames": metadata["expressionNames"],
                    "imageSize": metadata["imageSize"],
                    "validationExpressionMae": best_mae,
                    "validationActiveExpressionMae": validation_metrics[2],
                    "pilotSplit": "every fifth 45-frame block within each step",
                },
                output,
            )
            print(f"  Saved best checkpoint: {output}")
    print(
        f"Training complete. Best pilot expression MAE: {best_mae:.5f}. "
        "This split validates the pipeline, not headset-reseat generalization."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
