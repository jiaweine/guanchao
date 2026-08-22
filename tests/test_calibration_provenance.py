from guanchao.detection import Calibration
from guanchao.evolution import EvolutionReport
from guanchao.harness import AgentHarness
from guanchao.policy import PolicyProfile
from guanchao.store import Store

import guanchao.harness as harness_module


class _AcceptingEngine:
    seen_base = None

    def evolve(self, current, examples, profile):
        type(self).seen_base = current
        promoted = Calibration.from_dict({**current.to_dict(), "bias": current.bias + 0.25})
        return EvolutionReport(
            True,
            0.1,
            0.2,
            0.0,
            len(examples),
            "accepted",
            promoted,
            profile,
        )


class _RejectingEngine:
    seen_base = None

    def evolve(self, current, examples, profile):
        type(self).seen_base = current
        return EvolutionReport(
            False,
            0.1,
            0.1,
            0.0,
            len(examples),
            "rejected",
            current,
            profile,
        )


def test_withdrawn_review_calibration_returns_to_pre_review_manual_base(tmp_path, monkeypatch):
    store = Store(str(tmp_path / "provenance-reset.sqlite"))
    manual_base = Calibration(bias=-0.21, decision_threshold=0.61, high_threshold=0.86)
    store.save_calibration(manual_base)
    harness = AgentHarness(store)
    profile = PolicyProfile()

    try:
        monkeypatch.setattr(harness_module, "EvolutionEngine", _AcceptingEngine)
        report, promoted, reset = harness._fit_review_dataset([], profile, "dataset-a")
        assert report.accepted is True
        assert promoted is True and reset is False
        learned = store.get_calibration()
        assert learned != manual_base
        assert _AcceptingEngine.seen_base == manual_base

        provenance = store._setting("review_calibration_provenance")
        assert provenance is not None
        assert Calibration.from_dict(provenance["base"]) == manual_base
        assert Calibration.from_dict(provenance["applied"]) == learned

        monkeypatch.setattr(harness_module, "EvolutionEngine", _RejectingEngine)
        report, promoted, reset = harness._fit_review_dataset([], profile, "dataset-withdrawn")
        assert report.accepted is False
        assert promoted is False and reset is True
        assert _RejectingEngine.seen_base == manual_base
        assert store.get_calibration() == manual_base
        assert store._setting("review_calibration_provenance") == {}
    finally:
        harness.close()


def test_external_calibration_override_becomes_new_base_and_is_never_cold_reset(tmp_path, monkeypatch):
    store = Store(str(tmp_path / "provenance-override.sqlite"))
    original_base = Calibration(bias=-0.30, decision_threshold=0.58, high_threshold=0.83)
    store.save_calibration(original_base)
    harness = AgentHarness(store)
    profile = PolicyProfile()

    try:
        monkeypatch.setattr(harness_module, "EvolutionEngine", _AcceptingEngine)
        harness._fit_review_dataset([], profile, "dataset-a")
        review_learned = store.get_calibration()
        assert review_learned != original_base

        manual_override = Calibration(
            bias=0.17,
            decision_threshold=0.67,
            abstain_margin=0.04,
            high_threshold=0.91,
        )
        store.save_calibration(manual_override)

        monkeypatch.setattr(harness_module, "EvolutionEngine", _RejectingEngine)
        report, promoted, reset = harness._fit_review_dataset([], profile, "dataset-b")
        assert report.accepted is False
        assert promoted is False and reset is False
        assert _RejectingEngine.seen_base == manual_override
        assert store.get_calibration() == manual_override
        assert store.get_calibration() != Calibration()
        assert store._setting("review_calibration_provenance") == {}
    finally:
        harness.close()


def test_new_review_fit_after_manual_override_uses_override_as_base(tmp_path, monkeypatch):
    store = Store(str(tmp_path / "provenance-rebase.sqlite"))
    initial = Calibration(bias=-0.25, decision_threshold=0.57, high_threshold=0.82)
    store.save_calibration(initial)
    harness = AgentHarness(store)
    profile = PolicyProfile()

    try:
        monkeypatch.setattr(harness_module, "EvolutionEngine", _AcceptingEngine)
        harness._fit_review_dataset([], profile, "dataset-a")

        override = Calibration(bias=0.08, decision_threshold=0.64, high_threshold=0.89)
        store.save_calibration(override)
        harness._fit_review_dataset([], profile, "dataset-b")
        assert _AcceptingEngine.seen_base == override

        provenance = store._setting("review_calibration_provenance")
        assert provenance is not None
        assert Calibration.from_dict(provenance["base"]) == override
        assert Calibration.from_dict(provenance["applied"]) == store.get_calibration()
    finally:
        harness.close()
