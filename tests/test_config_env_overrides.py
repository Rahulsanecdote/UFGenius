"""Environment overrides for config accessors (src/utils/config.py).

A knob that only exists in ``config.yaml`` costs a commit and a manual deploy to
turn on a managed host, which is the wrong shape for anything you tune while the
market is open. These tests pin the two halves of the fix: the ``env_list``
parsing contract, and the fact that every ``CATALYST_ALERTS_*`` accessor really
does read the environment (a doc claiming so is not evidence).

The reload fixture restores the module afterwards, so nothing leaks into other
tests: it removes the keys it set and reloads once more from a clean environment.
"""

from __future__ import annotations

import importlib
import os

import pytest

from src.utils import config as cfg


@pytest.fixture
def reload_config():
    """Reload ``src.utils.config`` with extra env vars set, then restore."""
    keys: list[str] = []

    def _reload(**env: str):
        keys.extend(env)
        os.environ.update(env)
        return importlib.reload(cfg)

    yield _reload
    for key in keys:
        os.environ.pop(key, None)
    importlib.reload(cfg)


class TestEnvList:
    def test_splits_and_strips(self, monkeypatch):
        monkeypatch.setenv("UFG_TEST_LIST", " strong , dilution ")
        assert cfg.env_list("UFG_TEST_LIST", ["moderate"]) == ["strong", "dilution"]

    def test_unset_keeps_the_default(self):
        assert cfg.env_list("UFG_TEST_ABSENT", ["strong"]) == ["strong"]

    def test_blank_keeps_the_default(self, monkeypatch):
        # A hosting dashboard's field cleared to "" is an accident far more
        # often than a deliberate empty list, and an empty list here means
        # "silently do nothing" — the failure mode worth refusing.
        monkeypatch.setenv("UFG_TEST_BLANK", "   ")
        assert cfg.env_list("UFG_TEST_BLANK", ["strong"]) == ["strong"]

    def test_empty_items_are_dropped(self, monkeypatch):
        monkeypatch.setenv("UFG_TEST_HOLES", "strong,,dilution,")
        assert cfg.env_list("UFG_TEST_HOLES", []) == ["strong", "dilution"]

    def test_default_is_copied_not_aliased(self):
        default = ["strong"]
        out = cfg.env_list("UFG_TEST_ABSENT", default)
        out.append("weak")
        assert default == ["strong"]


class TestCatalystAlertEnvOverrides:
    """Every catalyst-alert knob is env-settable, not just the master switch."""

    def test_all_keys_are_read_from_the_environment(self, reload_config):
        mod = reload_config(
            CATALYST_ALERTS_ENABLED="true",
            CATALYST_ALERTS_UNIVERSE="all",
            CATALYST_ALERTS_TIERS="strong,dilution",
            CATALYST_ALERTS_LOOKBACK_SEC="120",
            CATALYST_ALERTS_DEDUP_TTL_SEC="900",
            CATALYST_ALERTS_MAX_PER_RUN="9",
            CATALYST_ALERTS_SUPPRESS_HALTED="false",
        )
        assert mod.CATALYST_ALERTS_ENABLED is True
        assert mod.CATALYST_ALERTS_UNIVERSE == "all"
        assert mod.CATALYST_ALERTS_TIERS == ["strong", "dilution"]
        assert mod.CATALYST_ALERTS_LOOKBACK_SEC == 120.0
        assert mod.CATALYST_ALERTS_DEDUP_TTL_SEC == 900.0
        assert mod.CATALYST_ALERTS_MAX_PER_RUN == 9
        assert mod.CATALYST_ALERTS_SUPPRESS_HALTED is False

    def test_defaults_survive_an_unset_environment(self, reload_config):
        # Nothing set: the yaml defaults must still win, and they are the
        # conservative ones — off, watchlist-scoped, strong-only.
        mod = reload_config()
        assert mod.CATALYST_ALERTS_ENABLED is False
        assert mod.CATALYST_ALERTS_UNIVERSE == "watchlist"
        assert mod.CATALYST_ALERTS_TIERS == ["strong"]
        assert mod.CATALYST_ALERTS_SUPPRESS_HALTED is True

    def test_garbage_numbers_fall_back_to_the_default(self, reload_config):
        mod = reload_config(CATALYST_ALERTS_MAX_PER_RUN="lots",
                            CATALYST_ALERTS_LOOKBACK_SEC="soon")
        assert mod.CATALYST_ALERTS_MAX_PER_RUN == 5
        assert mod.CATALYST_ALERTS_LOOKBACK_SEC == 300.0
