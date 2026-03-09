"""Provider-path consistency checks (Alpaca canonical, Robinhood removed)."""

from __future__ import annotations

from pathlib import Path


def test_alpaca_module_present_and_robinhood_removed():
    project_root = Path(__file__).resolve().parent.parent
    assert (project_root / "src" / "alpaca" / "portfolio.py").exists()
    assert not (project_root / "src" / "robinhood").exists()


def test_no_runtime_robinhood_imports():
    project_root = Path(__file__).resolve().parent.parent
    offenders = []
    scan_paths = [project_root / "src", project_root / "bot.py", project_root / "dashboard.py"]
    paths = []
    for p in scan_paths:
        if p.is_file():
            paths.append(p)
        elif p.is_dir():
            paths.extend(p.rglob("*.py"))
    for path in paths:
        if "__pycache__" in path.parts:
            continue
        text = path.read_text(encoding="utf-8")
        if "src.robinhood" in text or "from src.robinhood" in text:
            offenders.append(str(path))
    assert not offenders, f"Found stale robinhood imports: {offenders}"
