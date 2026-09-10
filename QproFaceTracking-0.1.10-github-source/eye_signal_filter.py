#!/usr/bin/env python3
"""Independent low-latency filtering for left/right eye signals."""

from __future__ import annotations

import collections
import math

import numpy as np


def _alpha(cutoff_hz: np.ndarray, elapsed_s: float) -> np.ndarray:
    rate = 2.0 * math.pi * cutoff_hz * elapsed_s
    return rate / (rate + 1.0)


class OneEuroVectorFilter:
    """Timestamp-aware One Euro filter for one vector-valued signal."""

    def __init__(
        self,
        dimensions: int,
        min_cutoff_hz: float = 1.5,
        beta: float = 0.08,
        derivative_cutoff_hz: float = 1.0,
    ) -> None:
        if dimensions < 1:
            raise ValueError("dimensions must be positive")
        self.dimensions = dimensions
        self.min_cutoff_hz = float(min_cutoff_hz)
        self.beta = float(beta)
        self.derivative_cutoff_hz = float(derivative_cutoff_hz)
        self._value: np.ndarray | None = None
        self._derivative = np.zeros(dimensions, dtype=np.float64)
        self._time_s: float | None = None

    def reset(self) -> None:
        self._value = None
        self._derivative.fill(0.0)
        self._time_s = None

    def update(self, value: np.ndarray, timestamp_s: float) -> np.ndarray:
        current = np.asarray(value, dtype=np.float64)
        if current.shape != (self.dimensions,):
            raise ValueError(
                f"expected a {self.dimensions}-component vector, got {current.shape}"
            )
        if self._value is None or self._time_s is None:
            self._value = current.copy()
            self._time_s = float(timestamp_s)
            return current.copy()

        elapsed = float(timestamp_s) - self._time_s
        self._time_s = float(timestamp_s)
        if not math.isfinite(elapsed) or elapsed <= 1e-6:
            return self._value.copy()
        elapsed = min(elapsed, 0.25)

        raw_derivative = (current - self._value) / elapsed
        derivative_alpha = _alpha(
            np.full(self.dimensions, self.derivative_cutoff_hz), elapsed
        )
        self._derivative += derivative_alpha * (raw_derivative - self._derivative)
        cutoff = self.min_cutoff_hz + self.beta * np.abs(self._derivative)
        value_alpha = _alpha(cutoff, elapsed)
        self._value += value_alpha * (current - self._value)
        return self._value.copy()


class IndependentEyeFilter:
    """Three-frame median plus distinct adaptive filters for the two eyes."""

    def __init__(
        self,
        dimensions: int,
        median_window: int = 3,
        min_cutoff_hz: float = 1.5,
        beta: float = 0.08,
        derivative_cutoff_hz: float = 1.0,
    ) -> None:
        if median_window < 1 or median_window % 2 == 0:
            raise ValueError("median_window must be a positive odd number")
        self.dimensions = dimensions
        self.median_window = median_window
        self.left_history: collections.deque[np.ndarray] = collections.deque(
            maxlen=median_window
        )
        self.right_history: collections.deque[np.ndarray] = collections.deque(
            maxlen=median_window
        )
        arguments = (dimensions, min_cutoff_hz, beta, derivative_cutoff_hz)
        self.left = OneEuroVectorFilter(*arguments)
        self.right = OneEuroVectorFilter(*arguments)

    def reset(self) -> None:
        self.left_history.clear()
        self.right_history.clear()
        self.left.reset()
        self.right.reset()

    def update(
        self,
        left: np.ndarray,
        right: np.ndarray,
        timestamp_s: float,
    ) -> tuple[np.ndarray, np.ndarray]:
        left_value = np.asarray(left, dtype=np.float64)
        right_value = np.asarray(right, dtype=np.float64)
        expected = (self.dimensions,)
        if left_value.shape != expected or right_value.shape != expected:
            raise ValueError(f"both eye values must have shape {expected}")
        self.left_history.append(left_value.copy())
        self.right_history.append(right_value.copy())
        left_median = np.median(np.stack(self.left_history), axis=0)
        right_median = np.median(np.stack(self.right_history), axis=0)
        return (
            self.left.update(left_median, timestamp_s),
            self.right.update(right_median, timestamp_s),
        )
