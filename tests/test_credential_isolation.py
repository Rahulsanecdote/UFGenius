"""Unit tests must not see real credentials, and the list that ensures it must
not drift from config.py.

The isolation fixture in conftest.py blanks a named list of credentials. A new
`SOME_API_KEY: str = env(...)` added to config.py and not to that list would be
silently exempt — a developer shell exporting it would let unit tests reach the
live service again, which is how the gap was found in the first place.
"""

from __future__ import annotations

import pathlib
import re

import pytest

import src.utils.config as cfg
from tests.conftest import _CREDENTIALS

_CRED_RE = re.compile(
    r"^([A-Z][A-Z0-9_]*(?:KEY|KEYS|SECRET|TOKEN|CHAT_ID|PASSWORD)[A-Z0-9_]*)\s*:\s*str",
    re.MULTILINE,
)


def test_every_credential_in_config_is_isolated():
    src = pathlib.Path("src/utils/config.py").read_text()
    declared = set(_CRED_RE.findall(src))
    assert declared, "the pattern found nothing — it has stopped matching config.py"
    missing = sorted(declared - set(_CREDENTIALS))
    assert not missing, (
        f"credentials declared in config.py but not blanked for unit tests: "
        f"{missing}. Add them to _CREDENTIALS in tests/conftest.py."
    )


@pytest.mark.parametrize("name", _CREDENTIALS)
def test_credentials_are_blank_inside_a_unit_test(name):
    assert getattr(cfg, name, "") == "", (
        f"{name} is visible to a unit test — the isolation fixture did not run"
    )


def test_a_test_can_still_set_its_own_key(monkeypatch):
    """The fixture blanks the ambient value; it must not stop a test from
    supplying the key it is actually testing with."""
    monkeypatch.setattr(cfg, "FMP_KEY", "k")
    assert cfg.FMP_KEY == "k"
