"""Tests for the LLM-eval leaderboard metrics (pure stdlib over resolved pairs)."""

from __future__ import annotations

import pytest

from research import calibration


class TestAccuracy:
    def test_empty_is_zero(self) -> None:
        assert calibration.accuracy([]) == 0.0

    def test_perfect_directional(self) -> None:
        assert calibration.accuracy([(0.9, True), (0.1, False)]) == 1.0

    def test_midpoint_is_half_credit(self) -> None:
        assert calibration.accuracy([(0.5, True)]) == 0.5

    def test_wrong_direction_scores_zero(self) -> None:
        assert calibration.accuracy([(0.8, False), (0.2, True)]) == 0.0

    def test_mixed(self) -> None:
        # one right, one wrong, one midpoint -> (1 + 0 + 0.5) / 3
        assert calibration.accuracy([(0.7, True), (0.7, False), (0.5, True)]) == pytest.approx(0.5)


class TestBrierSkill:
    def test_empty_is_none(self) -> None:
        assert calibration.brier_skill_score([]) is None

    def test_degenerate_all_same_outcome_is_none(self) -> None:
        # base rate = 1.0 -> reference Brier is 0 -> skill undefined.
        assert calibration.brier_skill_score([(0.9, True), (0.8, True)]) is None

    def test_confident_correct_beats_base_rate(self) -> None:
        pairs = [(0.95, True), (0.05, False), (0.9, True), (0.1, False)]
        skill = calibration.brier_skill_score(pairs)
        assert skill is not None and skill > 0.0

    def test_base_rate_forecast_scores_zero_skill(self) -> None:
        # Forecasting exactly the base rate every time ties the baseline.
        pairs = [(0.5, True), (0.5, False)]
        assert calibration.brier_skill_score(pairs) == pytest.approx(0.0)


class TestBaseRate:
    def test_empty_is_zero(self) -> None:
        assert calibration.base_rate([]) == 0.0

    def test_counts_yes(self) -> None:
        assert calibration.base_rate([(0.5, True), (0.5, False), (0.5, True)]) == pytest.approx(2 / 3)


class TestModelLeaderboard:
    def test_builds_sorted_entries(self, monkeypatch) -> None:
        # Sharp, accurate model vs a noisy one — sharp model should rank first (lower Brier).
        sharp = [(0.95, True), (0.05, False)] * 5
        noisy = [(0.6, False), (0.4, True)] * 5
        monkeypatch.setattr(
            calibration.db, "get_resolved_pairs_by_model",
            lambda: {"sharp-model": sharp, "noisy-model": noisy},
        )
        board = calibration.model_leaderboard()
        assert [e["model"] for e in board] == ["sharp-model", "noisy-model"]
        assert board[0]["brier"] < board[1]["brier"]
        assert board[0]["accuracy"] == 1.0
        assert board[0]["n"] == 10
        assert set(board[0]) >= {"model", "n", "brier", "log_loss", "temperature", "accuracy", "brier_skill"}

    def test_skips_models_with_no_pairs(self, monkeypatch) -> None:
        monkeypatch.setattr(
            calibration.db, "get_resolved_pairs_by_model",
            lambda: {"empty": [], "has-data": [(0.8, True), (0.2, False)]},
        )
        board = calibration.model_leaderboard()
        assert [e["model"] for e in board] == ["has-data"]

    def test_empty_dataset_is_empty_list(self, monkeypatch) -> None:
        monkeypatch.setattr(calibration.db, "get_resolved_pairs_by_model", lambda: {})
        assert calibration.model_leaderboard() == []
