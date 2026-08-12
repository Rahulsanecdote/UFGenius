"""Tests for the persisted equity high-water mark (roadmap Phase 4)."""

from src.risk.peak_equity import PeakEquityTracker


def test_first_observation_sets_peak(tmp_path):
    t = PeakEquityTracker(path=str(tmp_path / "peak.json"))
    assert t.peak() is None
    assert t.observe(10_000) == 10_000


def test_peak_is_monotonic_high_water_mark(tmp_path):
    path = str(tmp_path / "peak.json")
    t = PeakEquityTracker(path=path)
    t.observe(10_000)
    assert t.observe(12_000) == 12_000  # new high
    assert t.observe(9_000) == 12_000   # drawdown does not lower the peak
    assert t.observe(11_000) == 12_000  # still below the mark


def test_peak_persists_across_instances(tmp_path):
    path = str(tmp_path / "peak.json")
    PeakEquityTracker(path=path).observe(15_000)
    reloaded = PeakEquityTracker(path=path).load()
    assert reloaded.peak() == 15_000
    # a later lower reading from a fresh instance keeps the persisted high
    assert PeakEquityTracker(path=path).observe(13_000) == 15_000


def test_non_positive_or_nonfinite_ignored(tmp_path):
    t = PeakEquityTracker(path=str(tmp_path / "peak.json"))
    t.observe(10_000)
    assert t.observe(0) == 10_000
    assert t.observe(-5) == 10_000
    assert t.observe(float("nan")) == 10_000
    # +inf must NOT become the peak — else drawdown would be 1.0 forever and the
    # gate would veto every entry once enabled.
    assert t.observe(float("inf")) == 10_000
    assert t.observe(float("-inf")) == 10_000


def test_missing_store_is_no_peak(tmp_path):
    t = PeakEquityTracker(path=str(tmp_path / "nope.json")).load()
    assert t.peak() is None


def test_malformed_store_degrades_gracefully(tmp_path):
    path = tmp_path / "peak.json"
    path.write_text("not json", encoding="utf-8")
    t = PeakEquityTracker(path=str(path)).load()
    assert t.peak() is None  # unreadable → no peak, fails open (drawdown 0)
