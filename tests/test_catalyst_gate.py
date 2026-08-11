"""Tests for the P1.4 catalyst-tag veto gate."""

from __future__ import annotations

from src.catalysts.catalyst_gate import CatalystGate

_VETO = ["TRADING_HALT", "FRAUD", "SEC_INVESTIGATION"]


def test_veto_on_matching_tag():
    g = CatalystGate(veto_tags=_VETO)
    d = g.evaluate("AAA", ["FRAUD"])
    assert d.vetoed is True and d.action == "veto"
    assert "FRAUD" in d.reasons[0]


def test_case_insensitive():
    g = CatalystGate(veto_tags=_VETO)
    assert g.evaluate("AAA", ["fraud"]).vetoed is True
    assert g.evaluate("AAA", ["Trading_Halt"]).vetoed is True


def test_clear_when_no_matching_tag():
    g = CatalystGate(veto_tags=_VETO)
    assert g.evaluate("AAA", ["EARNINGS_BEAT", "UPGRADE"]).action == "clear"


def test_single_string_tag():
    g = CatalystGate(veto_tags=_VETO)
    assert g.evaluate("AAA", "FRAUD").vetoed is True


def test_none_and_garbage_tags_fail_open():
    g = CatalystGate(veto_tags=_VETO)
    assert g.evaluate("AAA", None).action == "clear"
    assert g.evaluate("AAA", []).action == "clear"
    assert g.evaluate("AAA", [None, 123]).action == "clear"


def test_multiple_hits_are_reported_once():
    g = CatalystGate(veto_tags=_VETO)
    d = g.evaluate("AAA", ["FRAUD", "FRAUD", "TRADING_HALT"])
    assert d.vetoed is True
    assert "FRAUD" in d.reasons[0] and "TRADING_HALT" in d.reasons[0]
