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
