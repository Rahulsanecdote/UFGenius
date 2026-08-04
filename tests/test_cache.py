"""Cache persistence/consistency tests."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed

from src.data import cache


def test_cache_set_is_atomic_and_leaves_no_temp_files(tmp_path, monkeypatch):
    monkeypatch.setattr(cache, "_CACHE_DIR", tmp_path)

    payload = {"k": "v"}
    cache.set("atomic:test", payload, ttl=60)

    assert cache.get("atomic:test") == payload
    assert len(list(tmp_path.glob("*.pkl"))) == 1
    assert list(tmp_path.glob("*.tmp")) == []


def test_cache_get_with_concurrent_writes_does_not_raise(tmp_path, monkeypatch):
    monkeypatch.setattr(cache, "_CACHE_DIR", tmp_path)

    errors: list[Exception] = []
    key = "atomic:concurrency"

    def _writer():
        for idx in range(200):
            cache.set(key, {"n": idx}, ttl=60)

    def _reader():
        for _ in range(200):
            try:
                cache.get(key)
            except Exception as exc:  # defensive assertion against partial reads
                errors.append(exc)

    with ThreadPoolExecutor(max_workers=4) as executor:
        futures = [executor.submit(_writer), executor.submit(_writer), executor.submit(_reader), executor.submit(_reader)]
        for future in as_completed(futures):
            future.result()

    assert errors == []


# ── eviction races (audit M9) ────────────────────────────────────────────────

class _FakeDir:
    """Stands in for _CACHE_DIR with a fixed glob() result, so we can hand the
    sweep paths that no longer exist — the race another worker's eviction
    creates between glob() and stat()."""

    def __init__(self, paths):
        self._paths = list(paths)

    def glob(self, _pattern):
        return list(self._paths)


def test_cache_size_tolerates_files_vanishing_mid_sweep(tmp_path, monkeypatch):
    ghost = tmp_path / "ghost.pkl"  # never created
    monkeypatch.setattr(cache, "_CACHE_DIR", _FakeDir([ghost]))
    assert cache._cache_size_mb() == 0.0  # must not raise FileNotFoundError


def test_enforce_size_limit_survives_vanished_files(tmp_path, monkeypatch):
    real = tmp_path / "real.pkl"
    real.write_bytes(b"x" * 4096)
    ghost = tmp_path / "ghost.pkl"
    monkeypatch.setattr(cache, "_CACHE_DIR", _FakeDir([real, ghost]))
    monkeypatch.setattr(cache, "MAX_CACHE_SIZE_MB", 0.000001)  # force the sweep

    cache._enforce_size_limit()  # must not raise on the ghost

    assert not real.exists()  # over-limit real file was evicted


def test_concurrent_writes_with_forced_eviction_do_not_raise(tmp_path, monkeypatch):
    monkeypatch.setattr(cache, "_CACHE_DIR", tmp_path)
    # A tiny limit makes every set() run the full eviction sweep, so 4 workers
    # constantly evict each other's files — the audit-M9 scenario.
    monkeypatch.setattr(cache, "MAX_CACHE_SIZE_MB", 0.001)

    errors: list[Exception] = []

    def _worker(worker_id: int):
        for idx in range(50):
            try:
                cache.set(f"evict:{worker_id}:{idx}", {"n": idx, "pad": "y" * 2000}, ttl=60)
                cache.get(f"evict:{worker_id}:{idx}")
                cache.evict_expired()
            except Exception as exc:
                errors.append(exc)

    with ThreadPoolExecutor(max_workers=4) as executor:
        futures = [executor.submit(_worker, i) for i in range(4)]
        for future in as_completed(futures):
            future.result()

    assert errors == []


def test_eviction_trim_target_is_configurable(tmp_path, monkeypatch):
    import src.utils.config as cfg

    monkeypatch.setattr(cache, "_CACHE_DIR", tmp_path)
    monkeypatch.setattr(cache, "MAX_CACHE_SIZE_MB", 0.001)  # ~1 KB limit
    monkeypatch.setattr(cfg, "CACHE_EVICTION_TARGET_RATIO", 1.0)

    for i in range(6):
        cache.set(f"cfg:{i}", {"pad": "x" * 400}, ttl=60)

    # ratio=1.0 → trim stops as soon as size is under the limit itself,
    # so at least one recent entry survives the sweep.
    assert cache._cache_size_mb() <= 0.001
    assert len(list(tmp_path.glob("*.pkl"))) >= 1


def test_nan_eviction_ratio_does_not_wipe_the_cache(tmp_path, monkeypatch):
    # NaN survives min/max clamping (every comparison is False); without the
    # finite guard the trim loop's stop condition never fires and EVERY file
    # is deleted on each sweep.
    import src.utils.config as cfg

    monkeypatch.setattr(cache, "_CACHE_DIR", tmp_path)
    monkeypatch.setattr(cache, "MAX_CACHE_SIZE_MB", 0.001)
    monkeypatch.setattr(cfg, "CACHE_EVICTION_TARGET_RATIO", float("nan"))

    for i in range(6):
        cache.set(f"nan:{i}", {"pad": "x" * 400}, ttl=60)

    assert len(list(tmp_path.glob("*.pkl"))) >= 1


def test_config_rejects_non_finite_ratio_values():
    import src.utils.config as cfg

    assert cfg._finite_or(float("nan"), 0.8) == 0.8
    assert cfg._finite_or(float("inf"), 0.8) == 0.8
    assert cfg._finite_or(0.5, 0.8) == 0.5
