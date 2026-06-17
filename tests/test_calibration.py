"""Tests for the temperature-scaling calibration math (pure stdlib)."""

from __future__ import annotations

import pytest

from research import calibration


class TestApplyTemperature:
    def test_fixed_point_at_half(self) -> None:
        # 0.5 has logit 0, so any temperature leaves it at 0.5.
        assert calibration.apply_temperature(0.5, 5.0) == pytest.approx(0.5)

    def test_temp_above_one_pulls_toward_half(self) -> None:
        # T > 1 softens overconfidence: 0.9 moves toward 0.5.
        out = calibration.apply_temperature(0.9, 2.0)
        assert 0.5 < out < 0.9

    def test_temp_below_one_sharpens(self) -> None:
        out = calibration.apply_temperature(0.9, 0.5)
        assert out > 0.9

    def test_identity_at_temp_one(self) -> None:
        assert calibration.apply_temperature(0.73, 1.0) == pytest.approx(0.73, abs=1e-6)

    def test_extremes_clamped_not_nan(self) -> None:
        # 0.0/1.0 are clamped internally so logit stays finite.
        assert 0.0 <= calibration.apply_temperature(0.0, 2.0) < 0.5
        assert 0.5 < calibration.apply_temperature(1.0, 2.0) <= 1.0


class TestLossMetrics:
    def test_log_loss_empty_is_zero(self) -> None:
        assert calibration.log_loss([]) == 0.0

    def test_brier_empty_is_zero(self) -> None:
        assert calibration.brier_score([]) == 0.0

    def test_brier_perfect_prediction(self) -> None:
        pairs = [(1.0, True), (0.0, False)]
        assert calibration.brier_score(pairs) == pytest.approx(0.0)

    def test_brier_hand_computed(self) -> None:
        # (0.8-1)^2 + (0.3-0)^2 = 0.04 + 0.09 = 0.13, /2 = 0.065
        pairs = [(0.8, True), (0.3, False)]
        assert calibration.brier_score(pairs) == pytest.approx(0.065)

    def test_log_loss_better_for_confident_correct(self) -> None:
        confident = calibration.log_loss([(0.9, True)])
        unsure = calibration.log_loss([(0.6, True)])
        assert confident < unsure


class TestFitTemperature:
    def test_empty_returns_one(self) -> None:
        assert calibration.fit_temperature([]) == 1.0

    def test_overconfident_set_wants_temp_above_one(self) -> None:
        # Predictions of 0.9 that only resolve True half the time are overconfident;
        # the best temperature should soften them (T > 1).
        pairs = [(0.9, True), (0.9, False), (0.1, True), (0.1, False)]
        assert calibration.fit_temperature(pairs) > 1.0

    def test_well_calibrated_set_near_one(self) -> None:
        # A calibrated set with spread: 0.7 predictions resolve True 70% of the time,
        # 0.3 predictions 30%. Temperature scaling can't improve on this, so T stays ~1.
        # (All-0.5 predictions would be degenerate — logit 0, insensitive to T.)
        pairs = (
            [(0.7, True)] * 7 + [(0.7, False)] * 3
            + [(0.3, True)] * 3 + [(0.3, False)] * 7
        )
        assert calibration.fit_temperature(pairs) == pytest.approx(1.0, abs=0.5)


class TestCalibrationCurve:
    def test_last_bin_includes_one(self) -> None:
        curve = calibration.calibration_curve([(1.0, True)])
        last = curve[-1]
        assert last["count"] == 1
        assert last["empirical_rate"] == pytest.approx(1.0)

    def test_empty_bin_has_none_means(self) -> None:
        curve = calibration.calibration_curve([(0.05, True)])
        # The 0.9-1.0 bin is empty.
        assert curve[-1]["count"] == 0
        assert curve[-1]["predicted_mean"] is None
        assert curve[-1]["empirical_rate"] is None

    def test_bins_partition_probabilities(self) -> None:
        curve = calibration.calibration_curve([(0.25, True), (0.75, False)])
        counts = [b["count"] for b in curve]
        assert sum(counts) == 2


class TestRecalibrator:
    def test_uncalibrated_apply_is_identity(self) -> None:
        r = calibration.identity_recalibrator("some-model")
        assert r.calibrated is False
        assert r.apply(0.83) == 0.83

    def test_calibrated_apply_uses_temperature(self) -> None:
        r = calibration.Recalibrator(
            model="m", n=100, calibrated=True, temperature=2.0,
            min_n=50, brier=0.0, log_loss=0.0, curve=[],
        )
        assert r.apply(0.9) == pytest.approx(calibration.apply_temperature(0.9, 2.0))
