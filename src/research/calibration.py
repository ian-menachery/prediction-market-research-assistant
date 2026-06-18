"""Calibration: measure how miscalibrated Claude is, and correct for it.

Pure math (stdlib only) — reads resolved ``(claude_prob, resolution)`` pairs via
``db`` and never writes. Recalibration is **temperature scaling** (one parameter
``T``): ``p_cal = sigmoid(logit(p) / T)``. ``T > 1`` softens overconfidence (pulls
estimates toward 0.5); ``T < 1`` sharpens. ``T`` is fit by minimizing log-loss over
resolved pairs.

The correction is applied only once at least ``CALIBRATION_MIN_N`` markets have
resolved; below that the recalibrator is the identity and reports ``calibrated=False``.
Temperature scaling corrects *confidence* miscalibration, not a *directional* bias
(that would need a second/Platt parameter) — an upgrade noted for later.
"""

from __future__ import annotations

import math
import os
from dataclasses import dataclass

from research import db

_EPS = 1e-6
MIN_N = int(os.getenv("CALIBRATION_MIN_N", "50"))
_BINS = 10


def _clamp(p: float) -> float:
    return min(1.0 - _EPS, max(_EPS, p))


def _sigmoid(x: float) -> float:
    # Numerically stable both directions.
    if x >= 0:
        return 1.0 / (1.0 + math.exp(-x))
    z = math.exp(x)
    return z / (1.0 + z)


def _logit(p: float) -> float:
    p = _clamp(p)
    return math.log(p / (1.0 - p))


def _apply_temp(p: float, temperature: float) -> float:
    return _sigmoid(_logit(p) / temperature)


def apply_temperature(p: float, temperature: float) -> float:
    """Public: apply a fitted temperature to a probability (p_cal = sigmoid(logit(p)/T))."""
    return _apply_temp(p, temperature)


def log_loss(pairs: list[tuple[float, bool]]) -> float:
    if not pairs:
        return 0.0
    total = 0.0
    for p, y in pairs:
        p = _clamp(p)
        total += -(math.log(p) if y else math.log(1.0 - p))
    return total / len(pairs)


def brier_score(pairs: list[tuple[float, bool]]) -> float:
    if not pairs:
        return 0.0
    return sum((p - (1.0 if y else 0.0)) ** 2 for p, y in pairs) / len(pairs)


def fit_temperature(pairs: list[tuple[float, bool]]) -> float:
    """Temperature minimizing log-loss over T in [0.05, 20], via ternary search."""
    if not pairs:
        return 1.0

    def loss(t: float) -> float:
        return log_loss([(_apply_temp(p, t), y) for p, y in pairs])

    lo, hi = 0.05, 20.0
    for _ in range(100):
        if hi - lo < 1e-4:
            break
        m1 = lo + (hi - lo) / 3.0
        m2 = hi - (hi - lo) / 3.0
        if loss(m1) < loss(m2):
            hi = m2
        else:
            lo = m1
    return (lo + hi) / 2.0


def accuracy(pairs: list[tuple[float, bool]]) -> float:
    """Directional hit rate: fraction where the side past 0.5 matched the outcome.

    A prediction of exactly 0.5 makes no directional call, so it scores half-credit.
    """
    if not pairs:
        return 0.0
    hits = 0.0
    for p, y in pairs:
        if p == 0.5:
            hits += 0.5
        elif (p > 0.5) == y:
            hits += 1.0
    return hits / len(pairs)


def base_rate(pairs: list[tuple[float, bool]]) -> float:
    """Empirical YES rate over the resolved set (the naive climatology forecast)."""
    if not pairs:
        return 0.0
    return sum(1 for _, y in pairs if y) / len(pairs)


def brier_skill_score(pairs: list[tuple[float, bool]]) -> float | None:
    """Brier skill vs always forecasting the base rate: 1 - brier / brier_baserate.

    > 0 means the model beats the naive climatology; 0 ties it; < 0 is worse. ``None`` when
    undefined — no pairs, or a degenerate all-one-outcome set (baseline Brier is 0).
    """
    if not pairs:
        return None
    ref = base_rate(pairs)
    ref_brier = brier_score([(ref, y) for _, y in pairs])
    if ref_brier == 0.0:
        return None
    return 1.0 - brier_score(pairs) / ref_brier


def calibration_curve(pairs: list[tuple[float, bool]], bins: int = _BINS) -> list[dict]:
    """Reliability bins: predicted mean vs empirical resolve-rate per probability bin."""
    out: list[dict] = []
    for i in range(bins):
        lo, hi = i / bins, (i + 1) / bins
        # Last bin is inclusive of 1.0; others are [lo, hi).
        sel = [
            (p, y) for p, y in pairs
            if p >= lo and (p < hi or (i == bins - 1 and p <= hi))
        ]
        if sel:
            predicted_mean: float | None = sum(p for p, _ in sel) / len(sel)
            empirical_rate: float | None = sum(1 for _, y in sel if y) / len(sel)
        else:
            predicted_mean = empirical_rate = None
        out.append({
            "bin_lo": lo, "bin_hi": hi,
            "predicted_mean": predicted_mean, "empirical_rate": empirical_rate,
            "count": len(sel),
        })
    return out


@dataclass(frozen=True)
class Recalibrator:
    model: str
    n: int
    calibrated: bool
    temperature: float
    min_n: int
    brier: float
    log_loss: float
    curve: list[dict]

    def apply(self, p: float) -> float:
        """Calibrated probability, or the raw value if not yet calibrated."""
        if not self.calibrated:
            return p
        return _apply_temp(p, self.temperature)


def _build_one(model: str, pairs: list[tuple[float, bool]]) -> Recalibrator:
    n = len(pairs)
    calibrated = n >= MIN_N
    return Recalibrator(
        model=model,
        n=n,
        calibrated=calibrated,
        temperature=fit_temperature(pairs) if calibrated else 1.0,
        min_n=MIN_N,
        brier=brier_score(pairs),
        log_loss=log_loss(pairs),
        curve=calibration_curve(pairs),
    )


def identity_recalibrator(model: str | None) -> Recalibrator:
    """A no-op recalibrator for a model with no (or too few) resolved pairs."""
    return _build_one(model or "unknown", [])


def build_recalibrators() -> dict[str, Recalibrator]:
    """One recalibrator per model from the resolved set (each gated by MIN_N).

    Build once per scan; apply the one matching each analysis's model.
    """
    return {
        model: _build_one(model, pairs)
        for model, pairs in db.get_resolved_pairs_by_model().items()
    }


def model_leaderboard() -> list[dict]:
    """Per-model forecasting scorecard from the resolved set — the LLM-eval leaderboard.

    Reuses ``_build_one`` for N / Brier / log-loss / temperature, and adds directional
    accuracy and Brier skill score (vs the base-rate forecast). Sorted best-first by Brier
    (ties broken by larger N). Models with no resolved pairs are omitted — nothing to score.
    """
    out: list[dict] = []
    for model, pairs in db.get_resolved_pairs_by_model().items():
        if not pairs:
            continue
        recal = _build_one(model, pairs)
        out.append({
            "model": model,
            "n": recal.n,
            "calibrated": recal.calibrated,
            "brier": recal.brier,
            "log_loss": recal.log_loss,
            "temperature": recal.temperature,
            "accuracy": accuracy(pairs),
            "base_rate": base_rate(pairs),
            "brier_skill": brier_skill_score(pairs),
        })
    out.sort(key=lambda e: (e["brier"], -e["n"]))
    return out
