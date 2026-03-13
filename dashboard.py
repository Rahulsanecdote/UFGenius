"""
Alpaca Signal Bot — Web Dashboard
Run: python dashboard.py
Then open: http://localhost:5001
"""

from __future__ import annotations

import json
import re

import pandas as pd
from flask import Flask, jsonify, render_template_string, request

from src.data.fetcher import clear_data_caches, diagnose, fetch_ohlcv
from src.macro.regime import detect_market_regime
from src.scanner.daily_scan import run_daily_scan, scan_single_ticker
from src.utils import config
from src.utils.logger import get_logger
from src.utils.security import (
    build_rate_limiter,
    has_auth_config,
    is_authorized_request,
    issue_dashboard_ui_token,
    resolve_client_ip,
)

log = get_logger("dashboard")
app = Flask(__name__)

TICKER_RE = re.compile(r"^[A-Z][A-Z0-9.-]{0,9}$")
_rate_limiter = build_rate_limiter()

_CHART_RANGES = {
    "1D": {"period": "1d", "interval": "5m", "max_points": 96},
    "5D": {"period": "5d", "interval": "15m", "max_points": 160},
    "1M": {"period": "1mo", "interval": "1d", "max_points": 32},
    "3M": {"period": "3mo", "interval": "1d", "max_points": 92},
    "1Y": {"period": "1y", "interval": "1d", "max_points": 260},
}

if config.DASHBOARD_ALLOW_REMOTE and not has_auth_config():
    raise RuntimeError("DASHBOARD_ALLOW_REMOTE=true requires DASHBOARD_API_KEY or DASHBOARD_API_KEYS")

HTML = '''
<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>UFGenius Dashboard</title>
  <link rel="preconnect" href="https://fonts.googleapis.com">
  <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
  <link href="https://fonts.googleapis.com/css2?family=DM+Sans:wght@400;500;600;700&family=JetBrains+Mono:wght@400;500;600;700&display=swap" rel="stylesheet">
  <style>
    :root {
      --bg-canvas: #07111b;
      --bg-surface: #0e1726;
      --bg-surface-elevated: #142033;
      --bg-surface-hover: #182842;
      --bg-surface-soft: rgba(20, 32, 51, 0.78);
      --border-default: #26364d;
      --border-strong: #3a4d69;
      --text-primary: #f5f7fa;
      --text-secondary: #b8c4d6;
      --text-muted: #8a99ad;
      --text-inverse: #07111b;
      --action-primary: #4f8cff;
      --action-primary-hover: #3d79ea;
      --action-primary-pressed: #2e67d9;
      --action-secondary-bg: #142846;
      --action-secondary-text: #9dc2ff;
      --status-success: #1fbf75;
      --status-warning: #f2b84b;
      --status-error: #e85d5d;
      --status-info: #5aa7ff;
      --status-neutral: #9aa9bd;
      --surface-success: #0f2a1f;
      --surface-warning: #31240f;
      --surface-error: #331517;
      --surface-info: #10233a;
      --surface-neutral: #16202d;
      --shadow-rest: 0 18px 45px rgba(4, 9, 17, 0.28);
      --shadow-elevated: 0 22px 60px rgba(4, 9, 17, 0.38);
      --radius-sm: 10px;
      --radius-md: 14px;
      --radius-lg: 18px;
      --radius-pill: 999px;
      --transition-fast: 120ms ease;
      --transition-base: 160ms ease;
      --font-sans: "DM Sans", "Avenir Next", "Segoe UI", sans-serif;
      --font-mono: "JetBrains Mono", "SF Mono", "SFMono-Regular", ui-monospace, monospace;
    }

    * { box-sizing: border-box; }

    [hidden] {
      display: none !important;
    }

    html { scroll-behavior: smooth; }

    body {
      margin: 0;
      min-height: 100vh;
      font-family: var(--font-sans);
      background:
        linear-gradient(rgba(90, 167, 255, 0.028) 1px, transparent 1px),
        linear-gradient(90deg, rgba(90, 167, 255, 0.028) 1px, transparent 1px),
        radial-gradient(circle at top left, rgba(79, 140, 255, 0.18), transparent 32%),
        radial-gradient(circle at top right, rgba(31, 191, 117, 0.10), transparent 28%),
        linear-gradient(180deg, #081321 0%, #07111b 48%, #061019 100%);
      background-size: 44px 44px, 44px 44px, auto, auto, auto;
      color: var(--text-primary);
    }

    button,
    input,
    select {
      font: inherit;
    }

    button {
      cursor: pointer;
      border: none;
      background: none;
      color: inherit;
    }

    button:disabled {
      cursor: not-allowed;
      opacity: 0.58;
    }

    a {
      color: inherit;
      text-decoration: none;
    }

    :focus-visible {
      outline: 2px solid var(--action-primary);
      outline-offset: 2px;
    }

    .sr-only {
      position: absolute;
      width: 1px;
      height: 1px;
      padding: 0;
      margin: -1px;
      overflow: hidden;
      clip: rect(0, 0, 0, 0);
      white-space: nowrap;
      border: 0;
    }

    .app-shell {
      min-height: 100vh;
      display: flex;
      flex-direction: column;
    }

    .topbar {
      position: sticky;
      top: 0;
      z-index: 20;
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 18px;
      padding: 14px 24px;
      border-bottom: 1px solid rgba(58, 77, 105, 0.55);
      background: rgba(7, 17, 27, 0.88);
      backdrop-filter: blur(18px);
    }

    .brand-lockup {
      display: flex;
      align-items: center;
      gap: 14px;
      min-width: 0;
    }

    .brand-mark {
      width: 42px;
      height: 42px;
      display: grid;
      place-items: center;
      border-radius: 14px;
      background: linear-gradient(145deg, rgba(79, 140, 255, 0.2), rgba(20, 40, 70, 0.95));
      border: 1px solid rgba(79, 140, 255, 0.24);
      box-shadow: inset 0 1px 0 rgba(255, 255, 255, 0.08);
    }

    .eyebrow {
      margin: 0;
      font-family: var(--font-mono);
      font-size: 12px;
      line-height: 18px;
      letter-spacing: 0.08em;
      text-transform: uppercase;
      color: var(--text-muted);
    }

    .brand-lockup h1 {
      margin: 0;
      font-size: 22px;
      line-height: 30px;
      font-weight: 700;
      letter-spacing: -0.03em;
      color: var(--text-primary);
    }

    .topbar-subtitle {
      color: var(--text-secondary);
      font-size: 13px;
      line-height: 18px;
    }

    .topbar-actions {
      display: flex;
      align-items: center;
      gap: 12px;
      flex-wrap: wrap;
      justify-content: flex-end;
    }

    .status-pill,
    .icon-button,
    .chip-button,
    .toolbar-select,
    .toolbar-input,
    .field-input,
    .field-select,
    .action-button,
    .ghost-button,
    .metric-card,
    .range-button,
    .table-action,
    .summary-action {
      min-height: 44px;
      border-radius: var(--radius-md);
      transition: transform var(--transition-fast), background var(--transition-fast), border-color var(--transition-fast), box-shadow var(--transition-fast);
    }

    .status-pill {
      display: inline-flex;
      align-items: center;
      gap: 10px;
      padding: 0 16px;
      border: 1px solid var(--border-default);
      background: rgba(16, 35, 58, 0.78);
      color: var(--text-primary);
      box-shadow: var(--shadow-rest);
    }

    .status-pill:hover,
    .icon-button:hover,
    .metric-card:hover,
    .chip-button:hover,
    .summary-action:hover,
    .table-action:hover,
    .action-button:hover,
    .ghost-button:hover,
    .range-button:hover {
      transform: translateY(-1px);
      background: var(--bg-surface-hover);
      border-color: var(--border-strong);
    }

    .status-dot {
      width: 10px;
      height: 10px;
      border-radius: 50%;
      background: var(--status-info);
      box-shadow: 0 0 0 4px rgba(90, 167, 255, 0.18);
    }

    .status-healthy {
      background: rgba(15, 42, 31, 0.85);
      border-color: rgba(31, 191, 117, 0.45);
    }

    .status-healthy .status-dot {
      background: var(--status-success);
      box-shadow: 0 0 0 4px rgba(31, 191, 117, 0.16);
    }

    .status-degraded {
      background: rgba(49, 36, 15, 0.92);
      border-color: rgba(242, 184, 75, 0.45);
    }

    .status-degraded .status-dot,
    .status-stale .status-dot {
      background: var(--status-warning);
      box-shadow: 0 0 0 4px rgba(242, 184, 75, 0.14);
    }

    .status-down {
      background: rgba(51, 21, 23, 0.92);
      border-color: rgba(232, 93, 93, 0.45);
    }

    .status-down .status-dot {
      background: var(--status-error);
      box-shadow: 0 0 0 4px rgba(232, 93, 93, 0.16);
    }

    .status-stale {
      background: rgba(22, 32, 45, 0.94);
      border-color: rgba(154, 169, 189, 0.38);
    }

    .sync-chip {
      display: grid;
      gap: 2px;
      padding: 0 12px;
      min-height: 44px;
      align-content: center;
      border-radius: var(--radius-md);
      border: 1px solid rgba(38, 54, 77, 0.68);
      background: rgba(14, 23, 38, 0.76);
      min-width: 120px;
    }

    .sync-chip strong {
      font-family: var(--font-mono);
      font-size: 13px;
      line-height: 20px;
      color: var(--text-primary);
    }

    .icon-button,
    .ghost-button,
    .summary-action,
    .table-action,
    .range-button {
      display: inline-flex;
      align-items: center;
      justify-content: center;
      gap: 8px;
      padding: 0 14px;
      border: 1px solid var(--border-default);
      background: rgba(14, 23, 38, 0.86);
      color: var(--text-secondary);
    }

    .action-button {
      display: inline-flex;
      align-items: center;
      justify-content: center;
      gap: 10px;
      padding: 0 18px;
      font-weight: 600;
      border: 1px solid rgba(79, 140, 255, 0.4);
      background: linear-gradient(180deg, var(--action-primary), var(--action-primary-hover));
      color: white;
      box-shadow: 0 16px 32px rgba(61, 121, 234, 0.24);
    }

    .action-button:hover {
      background: linear-gradient(180deg, var(--action-primary-hover), var(--action-primary-pressed));
    }

    .action-button.secondary {
      border-color: rgba(31, 191, 117, 0.35);
      background: linear-gradient(180deg, #1b8a57, #157047);
      box-shadow: 0 16px 32px rgba(21, 112, 71, 0.22);
    }

    .ghost-button {
      background: rgba(20, 40, 70, 0.62);
      color: var(--action-secondary-text);
      border-color: rgba(79, 140, 255, 0.26);
    }

    .page-shell {
      width: min(1380px, calc(100% - 32px));
      margin: 0 auto;
      padding: 18px 0 40px;
      display: grid;
      gap: 20px;
    }

    .panel {
      position: relative;
      border-radius: var(--radius-lg);
      border: 1px solid rgba(38, 54, 77, 0.82);
      background: linear-gradient(180deg, rgba(14, 23, 38, 0.96), rgba(9, 18, 30, 0.96));
      box-shadow: var(--shadow-rest);
      overflow: hidden;
    }

    .panel::before {
      content: "";
      position: absolute;
      inset: 0;
      background:
        linear-gradient(180deg, rgba(255, 255, 255, 0.03), transparent 32%),
        linear-gradient(90deg, transparent 0%, rgba(90, 167, 255, 0.06) 50%, transparent 100%);
      pointer-events: none;
    }

    .panel-body,
    .overview-grid,
    .support-grid,
    .scan-body {
      position: relative;
      z-index: 1;
    }

    .panel-heading {
      display: flex;
      align-items: flex-start;
      justify-content: space-between;
      gap: 16px;
      padding: 20px 20px 0;
    }

    .panel-heading h2,
    .panel-heading h3 {
      margin: 0;
      font-size: 18px;
      line-height: 24px;
      font-weight: 700;
      letter-spacing: -0.02em;
    }

    .panel-heading p {
      margin: 6px 0 0;
      font-size: 14px;
      line-height: 20px;
      color: var(--text-secondary);
    }

    .overview-grid {
      display: grid;
      gap: 1px;
      padding: 1px;
      grid-template-columns: repeat(4, minmax(0, 1fr));
      border-top: 1px solid rgba(58, 77, 105, 0.5);
      background: rgba(38, 54, 77, 0.52);
    }

    .metric-card {
      display: grid;
      gap: 10px;
      padding: 16px 18px;
      text-align: left;
      border: none;
      min-height: 142px;
      background: linear-gradient(180deg, rgba(14, 23, 38, 0.98), rgba(8, 17, 30, 0.98));
      color: var(--text-primary);
      box-shadow: inset 0 1px 0 rgba(255, 255, 255, 0.04);
    }

    .metric-value {
      font-family: var(--font-mono);
      font-size: 28px;
      line-height: 34px;
      font-weight: 700;
      letter-spacing: -0.04em;
    }

    .metric-note {
      font-size: 13px;
      line-height: 18px;
      color: var(--text-secondary);
      min-height: 36px;
    }

    .workspace-shell {
      padding: 20px;
      display: grid;
      gap: 18px;
    }

    .analysis-form {
      display: grid;
      gap: 16px;
      grid-template-columns: repeat(3, minmax(0, 1fr));
    }

    .field {
      display: grid;
      gap: 8px;
      align-content: start;
    }

    .field label {
      font-size: 12px;
      line-height: 18px;
      font-weight: 500;
      color: var(--text-secondary);
      text-transform: uppercase;
      letter-spacing: 0.06em;
    }

    .field-input,
    .field-select,
    .toolbar-input,
    .toolbar-select {
      width: 100%;
      border: 1px solid var(--border-default);
      background: rgba(7, 17, 27, 0.82);
      color: var(--text-primary);
      padding: 0 14px;
      box-shadow: inset 0 1px 0 rgba(255, 255, 255, 0.03);
    }

    .field-input,
    .toolbar-input,
    .scan-pill,
    .table-action,
    .summary-action,
    .status-chip,
    .status-pill {
      font-family: var(--font-mono);
    }

    .field-input::placeholder,
    .toolbar-input::placeholder {
      color: var(--text-muted);
    }

    .field-help,
    .field-error,
    .panel-note,
    .summary-copy,
    .scan-feedback,
    .empty-copy,
    .health-note,
    .drawer-body p {
      margin: 0;
      font-size: 14px;
      line-height: 20px;
    }

    .field-help,
    .panel-note,
    .summary-copy,
    .health-note {
      color: var(--text-secondary);
    }

    .field-error {
      color: var(--status-error);
    }

    .field-input[aria-invalid="true"],
    .toolbar-input[aria-invalid="true"] {
      border-color: rgba(232, 93, 93, 0.75);
      background: rgba(51, 21, 23, 0.58);
    }

    .workspace-actions,
    .summary-actions,
    .chart-controls,
    .health-actions,
    .toolbar-row,
    .suggestion-row {
      display: flex;
      gap: 10px;
      flex-wrap: wrap;
      align-items: center;
    }

    .progress-strip {
      display: grid;
      grid-template-columns: auto 1fr;
      gap: 14px;
      align-items: center;
      padding: 14px 16px;
      border-radius: var(--radius-md);
      border: 1px solid rgba(58, 77, 105, 0.62);
      background: rgba(16, 35, 58, 0.54);
    }

    .spinner {
      width: 18px;
      height: 18px;
      border-radius: 50%;
      border: 2px solid rgba(255, 255, 255, 0.16);
      border-top-color: var(--action-primary);
      animation: spin 0.7s linear infinite;
      opacity: 0;
    }

    .spinner.active {
      opacity: 1;
    }

    @keyframes spin {
      to { transform: rotate(360deg); }
    }

    .chip-button {
      display: inline-flex;
      align-items: center;
      justify-content: center;
      min-height: 36px;
      padding: 0 12px;
      border: 1px solid rgba(58, 77, 105, 0.72);
      background: rgba(14, 23, 38, 0.82);
      color: var(--text-secondary);
      font-family: var(--font-mono);
      font-size: 12px;
      letter-spacing: 0.04em;
    }

    .chip-button.active {
      border-color: rgba(79, 140, 255, 0.48);
      color: var(--action-secondary-text);
      background: rgba(20, 40, 70, 0.88);
    }

    .analysis-grid {
      display: grid;
      grid-template-columns: minmax(0, 1.08fr) minmax(0, 0.92fr);
      gap: 24px;
    }

    .result-shell,
    .factor-shell,
    .chart-shell,
    .recent-shell,
    .health-shell,
    .scan-shell {
      padding-bottom: 24px;
    }

    .summary-status-row {
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 16px;
      padding: 0 20px;
    }

    .status-chip {
      display: inline-flex;
      align-items: center;
      gap: 8px;
      padding: 8px 12px;
      border-radius: var(--radius-pill);
      border: 1px solid rgba(58, 77, 105, 0.8);
      background: rgba(20, 32, 51, 0.84);
      font-size: 13px;
      line-height: 18px;
      font-weight: 600;
      color: var(--text-primary);
    }

    .chip-success {
      background: rgba(15, 42, 31, 0.92);
      border-color: rgba(31, 191, 117, 0.36);
    }

    .chip-warning {
      background: rgba(49, 36, 15, 0.92);
      border-color: rgba(242, 184, 75, 0.42);
    }

    .chip-error {
      background: rgba(51, 21, 23, 0.92);
      border-color: rgba(232, 93, 93, 0.42);
    }

    .chip-neutral {
      background: rgba(22, 32, 45, 0.92);
      border-color: rgba(154, 169, 189, 0.28);
    }

    .result-content {
      display: grid;
      gap: 18px;
      padding: 18px 20px 0;
    }

    .result-headline {
      margin: 0;
      font-size: 26px;
      line-height: 32px;
      font-weight: 700;
      letter-spacing: -0.03em;
    }

    .summary-copy {
      font-size: 16px;
      line-height: 24px;
    }

    .stat-grid,
    .support-stat-grid {
      display: grid;
      gap: 12px;
      grid-template-columns: repeat(3, minmax(0, 1fr));
    }

    .stat-card,
    .support-stat {
      padding: 14px 16px;
      border-radius: var(--radius-md);
      border: 1px solid rgba(58, 77, 105, 0.72);
      background: rgba(7, 17, 27, 0.66);
    }

    .stat-card span,
    .support-stat span {
      display: block;
      font-size: 12px;
      line-height: 18px;
      color: var(--text-muted);
      text-transform: uppercase;
      letter-spacing: 0.04em;
    }

    .stat-card strong,
    .support-stat strong {
      display: block;
      margin-top: 4px;
      font-size: 18px;
      line-height: 24px;
    }

    .confidence-block {
      display: grid;
      gap: 10px;
      padding: 16px;
      border-radius: var(--radius-md);
      border: 1px solid rgba(58, 77, 105, 0.7);
      background: rgba(16, 35, 58, 0.46);
    }

    .confidence-row {
      display: flex;
      justify-content: space-between;
      gap: 12px;
      align-items: center;
    }

    .confidence-row strong {
      font-size: 18px;
      line-height: 24px;
    }

    .confidence-track {
      height: 10px;
      border-radius: var(--radius-pill);
      background: rgba(255, 255, 255, 0.08);
      overflow: hidden;
    }

    .confidence-fill {
      height: 100%;
      width: 0;
      border-radius: var(--radius-pill);
      background: linear-gradient(90deg, var(--status-neutral), var(--status-info));
      transition: width var(--transition-base);
    }

    .summary-detail-grid {
      display: grid;
      gap: 12px;
      grid-template-columns: repeat(2, minmax(0, 1fr));
    }

    .summary-detail {
      padding: 16px;
      border-radius: var(--radius-md);
      border: 1px solid rgba(58, 77, 105, 0.7);
      background: rgba(7, 17, 27, 0.66);
    }

    .summary-detail .eyebrow {
      margin-bottom: 6px;
    }

    .summary-detail strong {
      font-size: 16px;
      line-height: 24px;
    }

    .summary-actions {
      padding: 0 20px;
    }

    .summary-action {
      color: var(--text-secondary);
    }

    .text-button {
      width: 100%;
      display: flex;
      justify-content: space-between;
      align-items: center;
      gap: 12px;
      padding: 16px 0 0;
      color: var(--action-secondary-text);
      font-size: 14px;
      line-height: 20px;
      font-weight: 500;
      border-top: 1px solid rgba(58, 77, 105, 0.52);
    }

    .explain-panel {
      display: grid;
      gap: 12px;
      padding: 0 20px;
    }

    .explain-card {
      padding: 14px 16px;
      border-radius: var(--radius-md);
      border: 1px solid rgba(58, 77, 105, 0.62);
      background: rgba(7, 17, 27, 0.66);
    }

    .explain-card strong {
      display: block;
      margin-bottom: 6px;
      font-size: 14px;
      line-height: 20px;
    }

    .factor-list {
      display: grid;
      gap: 12px;
      padding: 18px 20px 0;
    }

    .factor-row {
      border-radius: var(--radius-md);
      border: 1px solid rgba(58, 77, 105, 0.7);
      background: rgba(7, 17, 27, 0.7);
      overflow: hidden;
    }

    .factor-toggle {
      width: 100%;
      display: grid;
      grid-template-columns: minmax(0, 1fr) auto;
      gap: 12px;
      padding: 16px;
      text-align: left;
      color: inherit;
    }

    .factor-main {
      display: grid;
      gap: 4px;
    }

    .factor-main strong {
      font-size: 16px;
      line-height: 24px;
    }

    .factor-main span {
      font-size: 14px;
      line-height: 20px;
      color: var(--text-secondary);
    }

    .factor-meta {
      display: flex;
      align-items: center;
      gap: 10px;
      flex-wrap: wrap;
      justify-content: flex-end;
    }

    .factor-status {
      padding: 7px 10px;
      border-radius: var(--radius-pill);
      font-size: 12px;
      line-height: 18px;
      font-weight: 600;
      border: 1px solid rgba(58, 77, 105, 0.74);
      background: rgba(22, 32, 45, 0.86);
    }

    .factor-status.pass {
      background: rgba(15, 42, 31, 0.92);
      border-color: rgba(31, 191, 117, 0.35);
    }

    .factor-status.fail {
      background: rgba(51, 21, 23, 0.92);
      border-color: rgba(232, 93, 93, 0.35);
    }

    .factor-status.neutral {
      background: rgba(49, 36, 15, 0.92);
      border-color: rgba(242, 184, 75, 0.35);
    }

    .factor-status.unknown {
      background: rgba(22, 32, 45, 0.92);
      border-color: rgba(154, 169, 189, 0.28);
    }

    .factor-detail {
      display: grid;
      gap: 8px;
      padding: 0 16px 16px;
      color: var(--text-secondary);
      font-size: 14px;
      line-height: 20px;
      border-top: 1px solid rgba(58, 77, 105, 0.46);
    }

    .support-grid {
      display: grid;
      gap: 24px;
      grid-template-columns: minmax(0, 1.35fr) minmax(320px, 0.65fr);
    }

    .support-side {
      display: grid;
      gap: 24px;
    }

    .chart-body {
      display: grid;
      gap: 18px;
      padding: 18px 20px 0;
    }

    .chart-frame {
      min-height: 300px;
      border-radius: var(--radius-md);
      border: 1px solid rgba(58, 77, 105, 0.72);
      background:
        linear-gradient(180deg, rgba(20, 32, 51, 0.82), rgba(7, 17, 27, 0.82)),
        repeating-linear-gradient(
          0deg,
          transparent,
          transparent 46px,
          rgba(255, 255, 255, 0.04) 46px,
          rgba(255, 255, 255, 0.04) 47px
        );
      display: grid;
      place-items: center;
      overflow: hidden;
    }

    .chart-empty {
      padding: 24px;
      text-align: center;
      color: var(--text-secondary);
      max-width: 360px;
    }

    .chart-svg {
      width: 100%;
      height: 100%;
      min-height: 300px;
      display: block;
    }

    .chart-caption {
      display: flex;
      justify-content: space-between;
      gap: 16px;
      flex-wrap: wrap;
      color: var(--text-secondary);
      font-size: 14px;
      line-height: 20px;
    }

    .range-group {
      display: inline-flex;
      flex-wrap: wrap;
      gap: 8px;
    }

    .range-button.active {
      background: rgba(79, 140, 255, 0.16);
      border-color: rgba(79, 140, 255, 0.48);
      color: var(--action-secondary-text);
    }

    .recent-list,
    .health-list,
    .scan-mobile-list {
      display: grid;
      gap: 12px;
      padding: 18px 20px 0;
    }

    .recent-item,
    .health-item,
    .scan-mobile-card {
      padding: 14px 16px;
      border-radius: var(--radius-md);
      border: 1px solid rgba(58, 77, 105, 0.72);
      background: rgba(7, 17, 27, 0.68);
    }

    .recent-button {
      width: 100%;
      display: flex;
      justify-content: space-between;
      gap: 12px;
      text-align: left;
      color: inherit;
    }

    .recent-meta,
    .health-item,
    .scan-mobile-card {
      display: grid;
      gap: 6px;
    }

    .recent-title,
    .scan-mobile-card strong {
      font-size: 16px;
      line-height: 24px;
      font-weight: 600;
    }

    .recent-subtle,
    .health-item span,
    .scan-mobile-card span {
      color: var(--text-secondary);
      font-size: 14px;
      line-height: 20px;
    }

    .scan-shell .panel-heading {
      padding-bottom: 18px;
    }

    .toolbar-row {
      padding: 0 20px;
    }

    .toolbar-input,
    .toolbar-select {
      flex: 1 1 140px;
      min-width: 0;
    }

    .toolbar-select[disabled] {
      opacity: 0.5;
    }

    .scan-feedback {
      padding: 14px 20px 0;
      color: var(--text-secondary);
    }

    .scan-spotlights {
      display: grid;
      gap: 18px;
      padding: 18px 20px 0;
    }

    .spotlight-empty {
      padding: 18px;
      border-radius: var(--radius-md);
      border: 1px dashed rgba(58, 77, 105, 0.62);
      background: rgba(7, 17, 27, 0.54);
      color: var(--text-secondary);
    }

    .spotlight-bucket {
      display: grid;
      gap: 12px;
    }

    .spotlight-bucket-header {
      display: flex;
      justify-content: space-between;
      align-items: center;
      gap: 12px;
    }

    .spotlight-bucket-title {
      display: inline-flex;
      align-items: center;
      gap: 10px;
      font-size: 15px;
      line-height: 20px;
      font-weight: 700;
    }

    .spotlight-bucket-title::before {
      content: "";
      width: 10px;
      height: 10px;
      border-radius: 999px;
      background: currentColor;
      box-shadow: 0 0 0 4px rgba(255, 255, 255, 0.08);
    }

    .spotlight-bucket-title.strong {
      color: var(--status-success);
    }

    .spotlight-bucket-title.buy {
      color: var(--status-info);
    }

    .spotlight-bucket-title.watch {
      color: var(--status-warning);
    }

    .spotlight-bucket-count {
      font-family: var(--font-mono);
      font-size: 12px;
      line-height: 18px;
      color: var(--text-muted);
      text-transform: uppercase;
      letter-spacing: 0.08em;
    }

    .spotlight-grid {
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(280px, 1fr));
      gap: 12px;
    }

    .spotlight-card {
      display: grid;
      gap: 14px;
      padding: 16px;
      border-radius: var(--radius-md);
      border: 1px solid rgba(58, 77, 105, 0.7);
      background:
        linear-gradient(180deg, rgba(14, 23, 38, 0.95), rgba(7, 17, 27, 0.95)),
        linear-gradient(135deg, rgba(90, 167, 255, 0.08), transparent 58%);
      box-shadow: inset 0 1px 0 rgba(255, 255, 255, 0.03);
    }

    .spotlight-card.strong {
      border-color: rgba(31, 191, 117, 0.32);
    }

    .spotlight-card.buy {
      border-color: rgba(90, 167, 255, 0.34);
    }

    .spotlight-card.watch {
      border-color: rgba(242, 184, 75, 0.32);
    }

    .spotlight-head {
      display: flex;
      justify-content: space-between;
      align-items: flex-start;
      gap: 12px;
    }

    .spotlight-ticker {
      display: grid;
      gap: 4px;
    }

    .spotlight-ticker strong {
      font-family: var(--font-mono);
      font-size: 24px;
      line-height: 28px;
      letter-spacing: -0.04em;
    }

    .spotlight-price {
      font-family: var(--font-mono);
      font-size: 13px;
      line-height: 18px;
      color: var(--text-muted);
    }

    .spotlight-signal {
      font-family: var(--font-mono);
      font-size: 11px;
      line-height: 16px;
      font-weight: 700;
      letter-spacing: 0.08em;
      text-transform: uppercase;
      padding: 6px 10px;
      border-radius: var(--radius-pill);
      border: 1px solid rgba(58, 77, 105, 0.72);
      background: rgba(20, 32, 51, 0.88);
    }

    .spotlight-score-row {
      display: grid;
      gap: 8px;
    }

    .spotlight-score-meta {
      display: flex;
      justify-content: space-between;
      align-items: center;
      gap: 12px;
      font-size: 12px;
      line-height: 18px;
      color: var(--text-muted);
      text-transform: uppercase;
      letter-spacing: 0.08em;
    }

    .spotlight-score {
      font-family: var(--font-mono);
      font-size: 24px;
      line-height: 28px;
      font-weight: 700;
      color: var(--status-success);
    }

    .spotlight-score.cautious {
      color: var(--status-warning);
    }

    .spotlight-score.low {
      color: var(--status-error);
    }

    .spotlight-track {
      height: 6px;
      border-radius: var(--radius-pill);
      background: rgba(255, 255, 255, 0.08);
      overflow: hidden;
    }

    .spotlight-fill {
      height: 100%;
      border-radius: inherit;
      background: linear-gradient(90deg, var(--status-success), #00e2b2);
    }

    .spotlight-fill.cautious {
      background: linear-gradient(90deg, var(--status-warning), #ffd166);
    }

    .spotlight-fill.low {
      background: linear-gradient(90deg, var(--status-error), #ff7b90);
    }

    .spotlight-stats {
      display: grid;
      grid-template-columns: repeat(3, minmax(0, 1fr));
      gap: 8px;
    }

    .spotlight-stat {
      padding: 10px;
      border-radius: var(--radius-sm);
      border: 1px solid rgba(58, 77, 105, 0.56);
      background: rgba(7, 17, 27, 0.78);
    }

    .spotlight-stat span {
      display: block;
      font-size: 11px;
      line-height: 16px;
      color: var(--text-muted);
      text-transform: uppercase;
      letter-spacing: 0.08em;
    }

    .spotlight-stat strong {
      display: block;
      margin-top: 4px;
      font-family: var(--font-mono);
      font-size: 14px;
      line-height: 20px;
    }

    .spotlight-levels {
      display: grid;
      gap: 8px;
      grid-template-columns: repeat(2, minmax(0, 1fr));
    }

    .spotlight-level {
      padding: 10px 12px;
      border-radius: var(--radius-sm);
      background: rgba(7, 17, 27, 0.78);
      border: 1px solid rgba(58, 77, 105, 0.54);
    }

    .spotlight-level span {
      display: block;
      font-size: 11px;
      line-height: 16px;
      color: var(--text-muted);
      text-transform: uppercase;
      letter-spacing: 0.08em;
    }

    .spotlight-level strong {
      display: block;
      margin-top: 4px;
      font-family: var(--font-mono);
      font-size: 14px;
      line-height: 20px;
    }

    .spotlight-reasons {
      margin: 0;
      padding-left: 16px;
      display: grid;
      gap: 6px;
      color: var(--text-secondary);
      font-size: 13px;
      line-height: 18px;
    }

    .scan-table-wrap {
      padding: 18px 20px 0;
      overflow-x: auto;
    }

    .scan-table {
      width: 100%;
      border-collapse: collapse;
      min-width: 880px;
    }

    .scan-table th,
    .scan-table td {
      padding: 14px 12px;
      border-bottom: 1px solid rgba(58, 77, 105, 0.38);
      text-align: left;
      font-size: 14px;
      line-height: 20px;
    }

    .scan-table th {
      color: var(--text-muted);
      font-size: 12px;
      text-transform: uppercase;
      letter-spacing: 0.05em;
      font-weight: 500;
    }

    .scan-row {
      cursor: pointer;
      transition: background var(--transition-fast);
    }

    .scan-row:hover,
    .scan-row:focus-visible {
      background: rgba(20, 32, 51, 0.72);
    }

    .scan-pill {
      display: inline-flex;
      padding: 5px 10px;
      border-radius: var(--radius-pill);
      font-size: 12px;
      line-height: 18px;
      font-weight: 600;
      border: 1px solid rgba(58, 77, 105, 0.72);
      background: rgba(20, 32, 51, 0.82);
    }

    .scan-empty {
      padding: 18px 20px 0;
    }

    .empty-box {
      padding: 18px;
      border-radius: var(--radius-md);
      border: 1px dashed rgba(58, 77, 105, 0.68);
      background: rgba(7, 17, 27, 0.48);
      color: var(--text-secondary);
      display: grid;
      gap: 10px;
    }

    .drawer-backdrop {
      position: fixed;
      inset: 0;
      background: rgba(3, 7, 12, 0.6);
      backdrop-filter: blur(8px);
      z-index: 40;
    }

    .drawer {
      position: fixed;
      top: 0;
      right: 0;
      width: min(460px, 100%);
      height: 100vh;
      z-index: 50;
      background: linear-gradient(180deg, rgba(14, 23, 38, 0.99), rgba(7, 17, 27, 0.99));
      border-left: 1px solid rgba(58, 77, 105, 0.62);
      box-shadow: var(--shadow-elevated);
      display: grid;
      grid-template-rows: auto 1fr;
    }

    .drawer-header {
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 12px;
      padding: 20px 20px 16px;
      border-bottom: 1px solid rgba(58, 77, 105, 0.4);
    }

    .drawer-header h2 {
      margin: 0;
      font-size: 20px;
      line-height: 28px;
    }

    .drawer-body {
      overflow: auto;
      padding: 20px;
      display: grid;
      gap: 16px;
    }

    .drawer-card {
      padding: 16px;
      border-radius: var(--radius-md);
      border: 1px solid rgba(58, 77, 105, 0.68);
      background: rgba(7, 17, 27, 0.68);
    }

    .drawer-card pre {
      margin: 0;
      white-space: pre-wrap;
      word-break: break-word;
      font-family: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, monospace;
      font-size: 13px;
      line-height: 18px;
      color: var(--text-secondary);
    }

    .toast-region {
      position: fixed;
      right: 20px;
      bottom: 20px;
      z-index: 60;
      display: grid;
      gap: 12px;
      justify-items: end;
    }

    .toast {
      min-width: 260px;
      max-width: min(380px, calc(100vw - 32px));
      padding: 14px 16px;
      border-radius: var(--radius-md);
      border: 1px solid rgba(58, 77, 105, 0.74);
      background: rgba(14, 23, 38, 0.96);
      color: var(--text-primary);
      box-shadow: var(--shadow-elevated);
    }

    .toast.success {
      border-color: rgba(31, 191, 117, 0.42);
      background: rgba(15, 42, 31, 0.96);
    }

    .toast.error {
      border-color: rgba(232, 93, 93, 0.42);
      background: rgba(51, 21, 23, 0.97);
    }

    .toast.info {
      border-color: rgba(79, 140, 255, 0.42);
      background: rgba(16, 35, 58, 0.98);
    }

    footer {
      padding: 24px 24px 40px;
      color: var(--text-muted);
      text-align: center;
      font-size: 13px;
      line-height: 18px;
      font-family: var(--font-mono);
    }

    @media (max-width: 1023px) {
      .overview-grid {
        grid-template-columns: repeat(2, minmax(0, 1fr));
      }

      .analysis-form {
        grid-template-columns: repeat(2, minmax(0, 1fr));
      }

      .analysis-grid,
      .support-grid {
        grid-template-columns: 1fr;
      }

      .support-side {
        grid-template-columns: 1fr;
      }
    }

    @media (max-width: 767px) {
      .topbar {
        padding: 16px;
      }

      .topbar-actions {
        width: 100%;
        justify-content: flex-start;
      }

      .page-shell {
        width: min(100% - 32px, 100%);
        padding-top: 20px;
      }

      .overview-grid {
        grid-template-columns: repeat(2, minmax(0, 1fr));
      }

      .metric-value {
        font-size: 20px;
        line-height: 28px;
      }

      .analysis-form,
      .stat-grid,
      .support-stat-grid,
      .summary-detail-grid {
        grid-template-columns: 1fr;
      }

      .panel-heading,
      .summary-status-row,
      .summary-actions,
      .factor-list,
      .chart-body,
      .recent-list,
      .health-list,
      .scan-table-wrap,
      .scan-spotlights,
      .toolbar-row,
      .scan-feedback,
      .scan-empty {
        padding-left: 18px;
        padding-right: 18px;
      }

      .workspace-shell {
        padding: 18px;
      }

      .spotlight-stats,
      .spotlight-levels {
        grid-template-columns: 1fr 1fr;
      }

      .scan-table-wrap {
        display: none;
      }

      .scan-mobile-list {
        display: grid;
      }

      .toast-region {
        right: 16px;
        left: 16px;
        justify-items: stretch;
      }
    }

    @media (min-width: 768px) {
      .scan-mobile-list {
        display: none;
      }
    }

    @media (prefers-reduced-motion: reduce) {
      html { scroll-behavior: auto; }
      *,
      *::before,
      *::after {
        animation-duration: 0.01ms !important;
        animation-iteration-count: 1 !important;
        transition-duration: 0.01ms !important;
        scroll-behavior: auto !important;
      }
    }
  </style>
</head>
<body>
  <div class="app-shell">
    <header class="topbar" role="banner">
      <div class="brand-lockup">
        <div class="brand-mark" aria-hidden="true">
          <svg width="22" height="22" viewBox="0 0 24 24" fill="none" stroke="#5AA7FF" stroke-width="2">
            <polyline points="2 17 8.5 10.5 13.5 15.5 22 7"></polyline>
            <polyline points="16 7 22 7 22 13"></polyline>
          </svg>
        </div>
        <div>
          <p class="eyebrow">Decision Workspace</p>
          <h1>UFGenius Signal Bot</h1>
          <div class="topbar-subtitle">Interactive market context, ticker analysis, and scan review.</div>
        </div>
      </div>
      <div class="topbar-actions">
        <button id="statusBadge" class="status-pill status-info" type="button" aria-label="Open provider health panel">
          <span class="status-dot" aria-hidden="true"></span>
          <span id="statusBadgeText">Syncing</span>
        </button>
        <div class="sync-chip">
          <span class="eyebrow">Last sync</span>
          <strong id="topSyncLabel">--</strong>
        </div>
        <button id="helpButton" class="icon-button" type="button" aria-label="Open help drawer">Help</button>
        <button id="settingsButton" class="icon-button" type="button" aria-label="Open settings drawer">Settings</button>
      </div>
      <div id="statusLive" class="sr-only" aria-live="polite"></div>
    </header>

    <main class="page-shell" role="main">
      <section class="panel" aria-labelledby="overviewTitle">
        <div class="panel-heading">
          <div>
            <h2 id="overviewTitle">Market Overview</h2>
            <p>Fast-glance context before you commit time to a ticker.</p>
          </div>
          <div class="summary-copy">Click any card for metric context and interpretation.</div>
        </div>
        <div class="overview-grid">
          <button id="metricRegime" class="metric-card" type="button" data-metric="regime">
            <span class="eyebrow">Market Regime</span>
            <strong id="regimeLabel" class="metric-value">--</strong>
            <p id="regimeNote" class="metric-note">Awaiting regime data.</p>
          </button>
          <button id="metricVix" class="metric-card" type="button" data-metric="vix">
            <span class="eyebrow">VIX</span>
            <strong id="vixLabel" class="metric-value">--</strong>
            <p id="vixNote" class="metric-note">Fear gauge unavailable.</p>
          </button>
          <button id="metricSpy" class="metric-card" type="button" data-metric="spy">
            <span class="eyebrow">SPY vs 200SMA</span>
            <strong id="spyLabel" class="metric-value">--</strong>
            <p id="spyNote" class="metric-note">Trend context unavailable.</p>
          </button>
          <button id="metricPosture" class="metric-card" type="button" data-metric="strategy">
            <span class="eyebrow">Strategy Posture</span>
            <strong id="biasLabel" class="metric-value">--</strong>
            <p id="biasNote" class="metric-note">Position sizing guidance pending.</p>
          </button>
        </div>
      </section>

      <section class="panel" aria-labelledby="workspaceTitle">
        <div class="workspace-shell">
          <div class="panel-heading" style="padding:0">
            <div>
              <h2 id="workspaceTitle">Analysis Workspace</h2>
              <p>Enter a ticker, choose account context, and run a guided analysis.</p>
            </div>
            <div class="summary-copy">Keyboard-first: press Enter in the ticker field to analyze.</div>
          </div>

          <form id="analysisForm" class="analysis-form" novalidate>
            <div class="field">
              <label for="tickerInput">Ticker</label>
              <input id="tickerInput" class="field-input" type="text" placeholder="AAPL" autocomplete="off" aria-describedby="tickerHelp tickerError">
              <p id="tickerHelp" class="field-help">US equities only</p>
              <p id="tickerError" class="field-error" hidden></p>
            </div>
            <div class="field">
              <label for="accountInput">Account Size</label>
              <input id="accountInput" class="field-input" type="number" inputmode="decimal" placeholder="10000" value="10000" aria-describedby="accountHelp accountError">
              <p id="accountHelp" class="field-help">Used for position sizing</p>
              <p id="accountError" class="field-error" hidden></p>
            </div>
            <div class="field">
              <label for="riskProfile">Risk Profile</label>
              <select id="riskProfile" class="field-select" aria-describedby="riskHelp">
                <option value="CONSERVATIVE">Conservative</option>
                <option value="STANDARD" selected>Standard</option>
                <option value="AGGRESSIVE">Aggressive</option>
              </select>
              <p id="riskHelp" class="field-help">Shapes guidance language and follow-up actions.</p>
            </div>
          </form>

          <div class="workspace-actions">
            <button id="analyzeButton" class="action-button" type="submit" form="analysisForm">Analyze Ticker</button>
            <button id="scanButton" class="action-button secondary" type="button">Full Market Scan</button>
            <button id="compareButton" class="ghost-button" type="button">Compare</button>
          </div>

          <div class="progress-strip" aria-live="polite">
            <div id="progressSpinner" class="spinner" aria-hidden="true"></div>
            <div>
              <p class="eyebrow">Analysis status</p>
              <p id="progressLabel" class="summary-copy">Enter a ticker to begin analysis. Try AAPL, MSFT, NVDA, or SPY.</p>
            </div>
          </div>

          <div>
            <p class="eyebrow" style="margin-bottom:10px">Suggestions</p>
            <div id="suggestionRow" class="suggestion-row"></div>
          </div>
        </div>
      </section>

      <section class="analysis-grid">
        <section class="panel result-shell" aria-labelledby="resultTitle">
          <div class="panel-heading">
            <div>
              <h2 id="resultTitle">Result Summary</h2>
              <p>Plain-English outcome, confidence, and next best action.</p>
            </div>
          </div>
          <div class="summary-status-row">
            <div id="resultBadge" class="status-chip chip-neutral">Idle</div>
            <div class="summary-copy" id="resultTimestamp">No analysis yet</div>
          </div>
          <div class="result-content" id="resultRegion">
            <h3 id="resultHeadline" class="result-headline">Enter a ticker to begin analysis.</h3>
            <p id="resultSummary" class="summary-copy">This workspace will explain what happened, why it happened, and what to do next.</p>

            <div class="stat-grid">
              <div class="stat-card">
                <span>Signal</span>
                <strong id="resultSignalStat">--</strong>
              </div>
              <div class="stat-card">
                <span>Score</span>
                <strong id="resultScoreStat">--</strong>
              </div>
              <div class="stat-card">
                <span>Current Price</span>
                <strong id="resultPriceStat">--</strong>
              </div>
            </div>

            <div class="confidence-block" aria-describedby="confidenceNote">
              <div class="confidence-row">
                <div>
                  <p class="eyebrow">Confidence</p>
                  <strong id="confidenceLabel">Unknown</strong>
                </div>
                <div id="confidencePercent" class="summary-copy">0%</div>
              </div>
              <div class="confidence-track" aria-hidden="true">
                <div id="confidenceFill" class="confidence-fill"></div>
              </div>
              <p id="confidenceNote" class="panel-note">Confidence reflects data completeness and consistency, not expected returns.</p>
            </div>

            <div class="summary-detail-grid">
              <div class="summary-detail">
                <p class="eyebrow">Primary reason</p>
                <strong id="primaryReason">Awaiting analysis.</strong>
              </div>
              <div class="summary-detail">
                <p class="eyebrow">Next best action</p>
                <strong id="nextAction">Analyze a ticker or run a market scan.</strong>
              </div>
            </div>
          </div>

          <div class="summary-actions">
            <button id="retryButton" class="summary-action" type="button" disabled>Retry</button>
            <button id="rawButton" class="summary-action" type="button" disabled>View raw data</button>
            <button id="saveButton" class="summary-action" type="button" disabled>Save result</button>
            <button id="summaryCompareButton" class="summary-action" type="button" disabled>Compare</button>
          </div>

          <div class="explain-panel">
            <button id="explainToggle" class="text-button" type="button" aria-expanded="false" aria-controls="explainContent">
              <span>Explain result</span>
              <span id="explainChevron" aria-hidden="true">+</span>
            </button>
            <div id="explainContent" hidden></div>
          </div>
          <div id="resultsLive" class="sr-only" aria-live="polite"></div>
        </section>

        <section class="panel factor-shell" aria-labelledby="factorTitle">
          <div class="panel-heading">
            <div>
              <h2 id="factorTitle">Factor Breakdown</h2>
              <p>See what passed, failed, or remained unknown.</p>
            </div>
            <button id="expandFactorsButton" class="ghost-button" type="button">View all factors</button>
          </div>
          <div id="factorList" class="factor-list"></div>
        </section>
      </section>

      <section class="support-grid">
        <section class="panel chart-shell" aria-labelledby="chartTitle">
          <div class="panel-heading">
            <div>
              <h2 id="chartTitle">Chart Context</h2>
              <p>Price trend with moving-average context for the active ticker.</p>
            </div>
            <div class="chart-controls">
              <div id="rangeGroup" class="range-group" role="group" aria-label="Chart range selector"></div>
              <button id="maToggle" class="range-button active" type="button" aria-pressed="true">MA Overlay</button>
            </div>
          </div>
          <div class="chart-body">
            <div id="chartFrame" class="chart-frame">
              <div class="chart-empty" id="chartEmpty">Analyze a ticker to view chart context.</div>
            </div>
            <div class="chart-caption">
              <span id="chartSummary">Price and trend summary will appear here.</span>
              <span id="chartUpdated">No chart loaded</span>
            </div>
          </div>
        </section>

        <div class="support-side">
          <section class="panel recent-shell" aria-labelledby="recentTitle">
            <div class="panel-heading">
              <div>
                <h3 id="recentTitle">Recent Analyses</h3>
                <p>Reopen prior work and compare outcomes quickly.</p>
              </div>
            </div>
            <div id="recentList" class="recent-list"></div>
          </section>

          <section class="panel health-shell" id="providerHealthPanel" aria-labelledby="healthTitle">
            <div class="panel-heading">
              <div>
                <h3 id="healthTitle">Provider Health</h3>
                <p>Separate bad setups from bad data pipes.</p>
              </div>
            </div>
            <div class="health-list" id="healthList">
              <div class="health-item">
                <strong>Diagnostics pending</strong>
                <span>Run provider checks to inspect price, fundamentals, and cache freshness.</span>
              </div>
            </div>
            <div class="health-actions" style="padding: 0 24px;">
              <button id="refreshHealthButton" class="summary-action" type="button">Retry diagnostics</button>
              <button id="clearCacheButton" class="summary-action" type="button">Clear cache</button>
            </div>
            <div style="padding: 18px 24px 0;">
              <p id="healthNarrative" class="health-note">The dashboard will tell you whether missing data came from provider issues or market rules.</p>
            </div>
          </section>
        </div>
      </section>

      <section class="panel scan-shell" aria-labelledby="scanTitle">
        <div class="panel-heading">
          <div>
            <h2 id="scanTitle">Full Market Scan</h2>
            <p>Filter, sort, and inspect the strongest opportunities from the latest scan.</p>
          </div>
        </div>

        <div id="scanSpotlights" class="scan-spotlights">
          <div class="spotlight-empty">Run a full market scan to surface ranked candidates as compact trading cards.</div>
        </div>

        <div class="toolbar-row">
          <input id="scanSearch" class="toolbar-input" type="search" placeholder="Search ticker">
          <select id="scanMinScore" class="toolbar-select" aria-label="Minimum score">
            <option value="0">Score: Any</option>
            <option value="50">Score: 50+</option>
            <option value="65">Score: 65+</option>
            <option value="80">Score: 80+</option>
          </select>
          <select id="scanConfidence" class="toolbar-select" aria-label="Confidence filter">
            <option value="ALL">Confidence: Any</option>
            <option value="VERY_HIGH">Very high</option>
            <option value="HIGH">High</option>
            <option value="MODERATE">Moderate</option>
            <option value="LOW">Low</option>
          </select>
          <select id="scanRegimeFit" class="toolbar-select" aria-label="Regime fit filter">
            <option value="ALL">Regime fit: Any</option>
            <option value="STRONG">Regime fit: Strong</option>
            <option value="ALIGNED">Regime fit: Aligned</option>
            <option value="CAUTIOUS">Regime fit: Cautious</option>
          </select>
          <select id="scanVolumeFilter" class="toolbar-select" aria-label="Volume threshold filter">
            <option value="0">Volume: Any</option>
            <option value="50">Volume: 50+</option>
            <option value="70">Volume: 70+</option>
          </select>
          <select id="scanSector" class="toolbar-select" aria-label="Sector filter" disabled>
            <option>Sector data unavailable</option>
          </select>
          <select id="scanSort" class="toolbar-select" aria-label="Sort scan results">
            <option value="score-desc">Sort: Highest score</option>
            <option value="score-asc">Sort: Lowest score</option>
            <option value="ticker-asc">Sort: Ticker A-Z</option>
            <option value="confidence-desc">Sort: Highest confidence</option>
          </select>
          <button id="clearFiltersButton" class="ghost-button" type="button">Clear filters</button>
        </div>

        <p id="scanFeedback" class="scan-feedback">Run a full market scan to populate candidates.</p>

        <div class="scan-table-wrap">
          <table class="scan-table">
            <thead>
              <tr>
                <th scope="col">Ticker</th>
                <th scope="col">Score</th>
                <th scope="col">Setup</th>
                <th scope="col">Confidence</th>
                <th scope="col">Regime Fit</th>
                <th scope="col">Volume</th>
                <th scope="col">Sentiment</th>
                <th scope="col">Updated</th>
                <th scope="col">Actions</th>
              </tr>
            </thead>
            <tbody id="scanTableBody"></tbody>
          </table>
        </div>

        <div id="scanMobileList" class="scan-mobile-list"></div>

        <div id="scanEmptyState" class="scan-empty" hidden>
          <div class="empty-box">
            <span>No tickers match your current filters.</span>
            <button id="clearFiltersInlineButton" class="ghost-button" type="button">Clear filters</button>
          </div>
        </div>
      </section>
    </main>

    <footer>
      UFGenius is for educational use only. Every major state should answer what happened, why it happened, and what to do next.
    </footer>
  </div>

  <div id="drawerBackdrop" class="drawer-backdrop" hidden></div>
  <aside id="drawer" class="drawer" hidden aria-hidden="true" role="dialog" aria-modal="true" aria-labelledby="drawerTitle">
    <div class="drawer-header">
      <h2 id="drawerTitle">Details</h2>
      <button id="drawerClose" class="icon-button" type="button" aria-label="Close details drawer">Close</button>
    </div>
    <div id="drawerBody" class="drawer-body"></div>
  </aside>

  <div id="toastRegion" class="toast-region" aria-live="polite" aria-atomic="true"></div>

  <script>
    const $ = id => document.getElementById(id);
    const API_TOKEN = {{ ui_token | tojson }};
    const AUTH_RECOVERY_STORAGE_KEY = 'ufgenius.authRecoveryTs';
    const AUTH_RECOVERY_COOLDOWN_MS = 15000;
    let authRecoveryTriggered = false;
    const STORAGE_KEYS = {
      recent: 'ufgenius.recentAnalyses',
      saved: 'ufgenius.savedResults'
    };
    const SUGGESTIONS = ['AAPL', 'MSFT', 'NVDA', 'SPY'];
    const CHART_RANGES = ['1D', '5D', '1M', '3M', '1Y'];
    const PROGRESS_STEPS = {
      analyze: [
        'Validating symbol',
        'Fetching price and volume',
        'Fetching fundamentals',
        'Applying regime rules',
        'Calculating score',
        'Generating explanation'
      ],
      scan: [
        'Loading market universe',
        'Filtering candidates',
        'Scoring opportunities',
        'Preparing ranked results'
      ]
    };

    const state = {
      currentResult: null,
      currentScan: null,
      currentChart: null,
      currentRegime: null,
      providerHealth: null,
      providerCheckedAt: null,
      chartRange: '3M',
      chartTicker: null,
      showMa: true,
      progressTimer: null,
      progressKind: null,
      factorOpen: new Set(),
      expandAllFactors: false,
      recentAnalyses: loadStoredArray(STORAGE_KEYS.recent),
      savedResults: loadStoredArray(STORAGE_KEYS.saved),
      lastSyncLabel: null
    };

    function loadStoredArray(key) {
      try {
        const raw = localStorage.getItem(key);
        const parsed = raw ? JSON.parse(raw) : [];
        return Array.isArray(parsed) ? parsed : [];
      } catch (_error) {
        return [];
      }
    }

    function persistStoredArray(key, value) {
      localStorage.setItem(key, JSON.stringify(value));
    }

    function escapeHtml(value) {
      return String(value ?? '')
        .replaceAll('&', '&amp;')
        .replaceAll('<', '&lt;')
        .replaceAll('>', '&gt;')
        .replaceAll('"', '&quot;')
        .replaceAll("'", '&#39;');
    }

    function clearAuthRecoveryMarkerIfStale() {
      const raw = sessionStorage.getItem(AUTH_RECOVERY_STORAGE_KEY);
      if (!raw) return;
      const last = Number(raw);
      if (!Number.isFinite(last)) {
        sessionStorage.removeItem(AUTH_RECOVERY_STORAGE_KEY);
        return;
      }
      if (Date.now() - last >= AUTH_RECOVERY_COOLDOWN_MS) {
        sessionStorage.removeItem(AUTH_RECOVERY_STORAGE_KEY);
      }
    }

    function handleUnauthorizedResponse(errorMessage) {
      if (authRecoveryTriggered) return;
      authRecoveryTriggered = true;

      const last = Number(sessionStorage.getItem(AUTH_RECOVERY_STORAGE_KEY) || 0);
      const recentlyRetried = Number.isFinite(last) && (Date.now() - last) < AUTH_RECOVERY_COOLDOWN_MS;
      if (recentlyRetried) {
        const message = errorMessage || 'Authorization failed. Verify dashboard API key configuration.';
        showToast(message, 'error', true);
        announce(message);
        return;
      }

      sessionStorage.setItem(AUTH_RECOVERY_STORAGE_KEY, String(Date.now()));
      const message = 'Authorization expired. Refreshing dashboard session...';
      showToast(message, 'warning', true);
      announce(message);
      window.setTimeout(() => {
        window.location.reload();
      }, 150);
    }

    function apiFetch(url, options = {}) {
      const headers = new Headers(options.headers || {});
      if (API_TOKEN) headers.set('X-Dashboard-Token', API_TOKEN);
      return fetch(url, { ...options, headers });
    }

    async function apiFetchJson(url, options = {}) {
      const response = await apiFetch(url, options);
      let payload = {};
      try {
        payload = await response.json();
      } catch (_error) {
        payload = {};
      }
      if (!response.ok) {
        if (response.status === 401) {
          const errorMessage = payload.error || 'Unauthorized';
          handleUnauthorizedResponse(errorMessage);
          throw new Error('Authorization failed. Refreshing dashboard session.');
        }
        throw new Error(payload.error || `Request failed (${response.status})`);
      }
      return payload;
    }

    function announce(message) {
      $('statusLive').textContent = message;
    }

    function announceResult(message) {
      $('resultsLive').textContent = message;
    }

    function showToast(message, variant = 'info', persist = false) {
      const toast = document.createElement('div');
      toast.className = `toast ${variant}`;
      toast.textContent = message;
      $('toastRegion').appendChild(toast);
      if (!persist) {
        window.setTimeout(() => {
          toast.remove();
        }, 3000);
      }
      return toast;
    }

    function formatTime(value) {
      if (!value) return '--';
      const date = value instanceof Date ? value : new Date(value);
      if (Number.isNaN(date.getTime())) return '--';
      return date.toLocaleString([], { month: 'short', day: 'numeric', hour: 'numeric', minute: '2-digit' });
    }

    function formatCurrency(value) {
      const amount = Number(value);
      if (!Number.isFinite(amount)) return 'Unknown';
      if (Math.abs(amount) >= 1_000_000_000_000) return `$${(amount / 1_000_000_000_000).toFixed(2)}T`;
      if (Math.abs(amount) >= 1_000_000_000) return `$${(amount / 1_000_000_000).toFixed(2)}B`;
      if (Math.abs(amount) >= 1_000_000) return `$${(amount / 1_000_000).toFixed(2)}M`;
      return `$${amount.toLocaleString(undefined, { maximumFractionDigits: 2 })}`;
    }

    function formatPrice(value) {
      const amount = Number(value);
      if (!Number.isFinite(amount)) return '--';
      return `$${amount.toFixed(2)}`;
    }

    function formatPercent(value, digits = 1) {
      const number = Number(value);
      if (!Number.isFinite(number)) return '--';
      return `${number > 0 ? '+' : ''}${number.toFixed(digits)}%`;
    }

    function formatScore(value) {
      const score = Number(value);
      return Number.isFinite(score) ? score.toFixed(1) : '0.0';
    }

    function normalizeSignalLabel(signal) {
      return String(signal || 'Unknown').replaceAll('_', ' ');
    }

    function validateTicker(value) {
      const ticker = String(value || '').trim().toUpperCase();
      if (!ticker) return { value: '', error: 'Enter a valid ticker symbol.' };
      const ok = /^[A-Z][A-Z0-9.-]{0,9}$/.test(ticker);
      return { value: ticker, error: ok ? '' : 'Enter a valid ticker symbol.' };
    }

    function validateAccount(value) {
      if (value == null || value === '') return { value: null, error: 'Enter a positive account size.' };
      const numeric = Number(value);
      if (!Number.isFinite(numeric) || numeric <= 0) {
        return { value: null, error: 'Enter a positive account size.' };
      }
      return { value: numeric, error: '' };
    }

    function updateFieldState(inputId, errorId, error) {
      const input = $(inputId);
      const errorEl = $(errorId);
      const hasError = Boolean(error);
      input.setAttribute('aria-invalid', hasError ? 'true' : 'false');
      errorEl.hidden = !hasError;
      errorEl.textContent = error;
    }

    function updateFormState() {
      $('tickerInput').value = $('tickerInput').value.toUpperCase();
      const ticker = validateTicker($('tickerInput').value);
      const account = validateAccount($('accountInput').value);

      updateFieldState('tickerInput', 'tickerError', $('tickerInput').value ? ticker.error : '');
      updateFieldState('accountInput', 'accountError', account.error);

      $('analyzeButton').disabled = Boolean(ticker.error || account.error);
      $('scanButton').disabled = Boolean(account.error);
      const compareDisabled = !state.currentResult && state.recentAnalyses.length < 2;
      $('compareButton').disabled = compareDisabled;
      $('summaryCompareButton').disabled = compareDisabled;
      return { ticker, account };
    }

    function startProgress(kind) {
      stopProgress();
      state.progressKind = kind;
      const steps = PROGRESS_STEPS[kind] || [];
      const spinner = $('progressSpinner');
      spinner.classList.add('active');
      let index = 0;
      $('progressLabel').textContent = steps[0] || 'Working...';
      announce(steps[0] || 'Working...');
      state.progressTimer = window.setInterval(() => {
        index = Math.min(index + 1, steps.length - 1);
        $('progressLabel').textContent = steps[index] || 'Working...';
      }, 700);
    }

    function stopProgress(message = null) {
      if (state.progressTimer) {
        window.clearInterval(state.progressTimer);
        state.progressTimer = null;
      }
      state.progressKind = null;
      $('progressSpinner').classList.remove('active');
      if (message) {
        $('progressLabel').textContent = message;
      }
    }

    function collectReasons(result) {
      const items = [
        ...((result && result.disqualifiers) || []),
        ...((result && result.reasons) || []),
        ...((result && result.reasoning) || []),
        ...((result && result.risk_factors) || [])
      ];
      return [...new Set(items.filter(Boolean))];
    }

    function cleanReason(reason) {
      const text = String(reason || '');
      if (!text) return 'No primary reason provided.';
      const parts = text.split(':');
      if (parts.length > 1) return parts.slice(1).join(':').trim();
      return text.replaceAll('_', ' ');
    }

    function hasDataQualityIssue(result) {
      return collectReasons(result).some(reason => /unknown|unable|insufficient|no data|missing|rate limited|unavailable/i.test(reason));
    }

    function confidenceModel(rawConfidence, status) {
      const map = {
        VERY_HIGH: { label: 'High', percent: 92, color: 'linear-gradient(90deg, #1fbf75, #5aa7ff)' },
        HIGH: { label: 'High', percent: 78, color: 'linear-gradient(90deg, #1fbf75, #5aa7ff)' },
        MODERATE: { label: 'Medium', percent: 58, color: 'linear-gradient(90deg, #f2b84b, #5aa7ff)' },
        LOW: { label: 'Low', percent: 34, color: 'linear-gradient(90deg, #f2b84b, #e85d5d)' },
        N_A: { label: 'Unknown', percent: 18, color: 'linear-gradient(90deg, #9aa9bd, #5aa7ff)' }
      };
      const key = String(rawConfidence || 'N_A').replaceAll('/', '_');
      const model = map[key] || map.N_A;
      if (status === 'filtered' || status === 'error') {
        return { label: 'Low', percent: 28, color: 'linear-gradient(90deg, #f2b84b, #e85d5d)' };
      }
      return model;
    }

    function resultPresentation(result) {
      if (!result) {
        return {
          status: 'idle',
          badge: 'Idle',
          headline: 'Enter a ticker to begin analysis.',
          summary: 'Try AAPL, MSFT, NVDA, or SPY to explore the new workspace.',
          primaryReason: 'No analysis has been run yet.',
          nextAction: 'Enter a valid ticker and choose Analyze Ticker.',
          signal: '--',
          score: '--',
          price: '--',
          confidence: confidenceModel(null, 'idle')
        };
      }

      const signal = String(result.signal || 'UNKNOWN');
      const score = result.composite_score ?? result.score ?? 0;
      const primaryReason = cleanReason(collectReasons(result)[0]);
      const dataIssue = hasDataQualityIssue(result);
      let status = 'success';
      if (signal === 'ERROR') status = 'error';
      else if (signal === 'FILTERED_OUT') status = 'filtered';
      else if (dataIssue) status = 'partial';

      const ticker = result.ticker || 'This ticker';
      const confidence = confidenceModel(result.confidence, status);

      if (status === 'filtered') {
        return {
          status,
          badge: 'Filtered Out',
          headline: `${ticker} was excluded from scoring.`,
          summary: primaryReason || 'A hard filter blocked this setup before it could be scored.',
          primaryReason: primaryReason || 'A filter rule excluded the setup.',
          nextAction: filteredNextAction(result),
          signal: normalizeSignalLabel(signal),
          score: formatScore(score),
          price: formatPrice(result.current_price),
          confidence
        };
      }

      if (status === 'error') {
        return {
          status,
          badge: 'Error',
          headline: `We could not complete the analysis for ${ticker}.`,
          summary: primaryReason || 'A data dependency failed before scoring completed.',
          primaryReason: primaryReason || 'Analysis did not return enough data.',
          nextAction: 'Retry analysis or inspect provider health for outages and stale data.',
          signal: normalizeSignalLabel(signal),
          score: formatScore(score),
          price: formatPrice(result.current_price),
          confidence
        };
      }

      if (status === 'partial') {
        return {
          status,
          badge: 'Partial Data',
          headline: `${ticker} was analyzed with incomplete data.`,
          summary: 'The app completed its checks, but one or more factors were partially unavailable.',
          primaryReason: primaryReason || 'Some factors could not be fully verified.',
          nextAction: riskProfileNextStep('partial'),
          signal: normalizeSignalLabel(signal),
          score: formatScore(score),
          price: formatPrice(result.current_price ?? result.entry?.price),
          confidence
        };
      }

      return {
        status,
        badge: normalizeSignalLabel(signal),
        headline: `${ticker} is ${normalizeSignalLabel(signal).toLowerCase()} under the current regime.`,
        summary: `Composite score ${formatScore(score)} with ${confidence.label.toLowerCase()} confidence across price, regime, and factor checks.`,
        primaryReason: primaryReason || 'The strongest factor signals aligned with the current market regime.',
        nextAction: riskProfileNextStep('success'),
        signal: normalizeSignalLabel(signal),
        score: formatScore(score),
        price: formatPrice(result.current_price ?? result.entry?.price),
        confidence
      };
    }

    function filteredNextAction(result) {
      const allReasons = collectReasons(result).join(' ');
      if (/market cap/i.test(allReasons)) {
        return 'Retry analysis or inspect provider health. This looks more like missing fundamentals than a directional market signal.';
      }
      if (/volume/i.test(allReasons)) {
        return 'Inspect liquidity conditions before spending more time on the setup.';
      }
      return 'Retry later or inspect provider health to confirm the block was not caused by bad data.';
    }

    function riskProfileNextStep(outcome) {
      const profile = $('riskProfile').value;
      if (outcome === 'partial') {
        if (profile === 'CONSERVATIVE') return 'Wait for cleaner data before acting on this setup.';
        if (profile === 'AGGRESSIVE') return 'Review the missing factors, then decide whether the setup still justifies further work.';
        return 'Inspect the missing factors before treating this as actionable.';
      }
      if (profile === 'CONSERVATIVE') return 'Review the factor panel and require cleaner confirmation before acting.';
      if (profile === 'AGGRESSIVE') return 'Review price levels and risk sizing before moving to a paper trade plan.';
      return 'Review factor details, trade levels, and regime fit before taking the next step.';
    }

    function statusChipClass(status) {
      if (status === 'success') return 'chip-success';
      if (status === 'partial') return 'chip-warning';
      if (status === 'filtered' || status === 'error') return 'chip-error';
      return 'chip-neutral';
    }

    function factorStatusFromScore(score) {
      const value = Number(score);
      if (!Number.isFinite(value)) return 'unknown';
      if (value >= 65) return 'pass';
      if (value >= 45) return 'neutral';
      return 'fail';
    }

    function factorSummaryFromReasons(reasons, patterns, fallback) {
      const match = reasons.find(reason => patterns.some(pattern => pattern.test(reason)));
      return match ? cleanReason(match) : fallback;
    }

    function buildFactors(result) {
      const reasons = collectReasons(result);
      const scores = (result && result.scores) || {};
      const regime = (result && result.regime_context) || {};
      const marketCap = result && result.market_cap;
      const marketCapState = reasons.some(reason => /UNKNOWN_MARKET_CAP/i.test(reason))
        ? 'unknown'
        : reasons.some(reason => /MICRO_CAP/i.test(reason))
          ? 'fail'
          : Number.isFinite(Number(marketCap))
            ? 'pass'
            : 'unknown';

      const volumeState = reasons.some(reason => /ILLIQUID/i.test(reason))
        ? 'fail'
        : factorStatusFromScore(scores.volume);

      const technicalState = factorStatusFromScore(scores.technical);
      const momentumState = factorStatusFromScore(scores.momentum);
      const sentimentState = factorStatusFromScore(scores.sentiment);
      const regimeState = factorStatusFromScore(scores.macro);
      const riskState = result && result.stop_loss ? 'neutral' : 'unknown';

      return [
        {
          key: 'market-cap',
          label: 'Market Cap',
          status: marketCapState,
          value: Number.isFinite(Number(marketCap)) ? formatCurrency(marketCap) : 'Unknown',
          summary: marketCapState === 'pass'
            ? 'Verified against provider fundamentals.'
            : marketCapState === 'fail'
              ? 'Below the minimum market-cap threshold.'
              : 'No verified market cap returned from the provider.',
          detail: cleanReason(reasons.find(reason => /market cap/i.test(reason)) || 'Threshold: minimum $100M market cap.')
        },
        {
          key: 'volume',
          label: 'Average Volume',
          status: volumeState,
          value: Number.isFinite(Number(scores.volume)) ? `${formatScore(scores.volume)}/100` : 'Unknown',
          summary: factorSummaryFromReasons(reasons, [/RVOL/i, /Volume/i, /ILLIQUID/i], 'Volume evidence was limited in this result.'),
          detail: 'Volume blends relative volume, accumulation, and pressure signals.'
        },
        {
          key: 'trend',
          label: 'Trend',
          status: technicalState,
          value: Number.isFinite(Number(scores.technical)) ? `${formatScore(scores.technical)}/100` : 'Unknown',
          summary: factorSummaryFromReasons(reasons, [/SMA/i, /VWAP/i, /MACD/i, /Parabolic/i, /Golden Cross/i], 'Trend checks combine moving averages, VWAP, and crossover structure.'),
          detail: 'Trend strength is used as the primary technical anchor in the composite score.'
        },
        {
          key: 'momentum',
          label: 'Momentum',
          status: momentumState,
          value: Number.isFinite(Number(scores.momentum)) ? `${formatScore(scores.momentum)}/100` : 'Unknown',
          summary: factorSummaryFromReasons(reasons, [/RSI/i, /Stochastic/i, /ROC/i, /CCI/i, /Divergence/i], 'Momentum checks did not surface a dominant signal.'),
          detail: 'Momentum blends RSI, stochastic, ROC, CCI, and divergence checks.'
        },
        {
          key: 'sentiment',
          label: 'Sentiment',
          status: sentimentState,
          value: Number.isFinite(Number(scores.sentiment)) ? `${formatScore(scores.sentiment)}/100` : 'Unknown',
          summary: factorSummaryFromReasons(reasons, [/News Sentiment/i, /Social Sentiment/i, /Insider/i], 'Sentiment data was neutral or unavailable.'),
          detail: 'Sentiment combines news, social, and insider activity into a single score.'
        },
        {
          key: 'regime',
          label: 'Regime Fit',
          status: regimeState,
          value: regime.regime ? `${normalizeSignalLabel(regime.regime)} (${formatScore(scores.macro)}/100)` : 'Unknown',
          summary: regime.regime
            ? `Current regime is ${normalizeSignalLabel(regime.regime)} with ${normalizeSignalLabel((regime.strategy || {}).bias || 'neutral')} posture.`
            : 'Regime data was unavailable.',
          detail: 'Macro score is the normalized regime score after position-size adjustments.'
        },
        {
          key: 'risk',
          label: 'Volatility / Risk',
          status: riskState,
          value: result && result.position ? `${formatPercent(result.position.risk_percent, 2)} risk` : 'Unknown',
          summary: cleanReason(((result && result.risk_factors) || [])[0] || ((result && result.stop_loss) || {}).method || 'Risk context will appear after a full plan is available.'),
          detail: result && result.stop_loss ? `Stop methodology: ${result.stop_loss.method}` : 'ATR-based risk data was not available for this result.'
        }
      ];
    }

    function renderFactors(result) {
      const list = $('factorList');
      const factors = buildFactors(result);
      if (state.expandAllFactors) {
        state.factorOpen = new Set(factors.map(factor => factor.key));
      }
      list.innerHTML = factors.map(factor => {
        const open = state.factorOpen.has(factor.key);
        return `
          <div class="factor-row">
            <button class="factor-toggle" type="button" data-factor-key="${factor.key}" aria-expanded="${open}" aria-controls="factor-panel-${factor.key}">
              <div class="factor-main">
                <strong>${escapeHtml(factor.label)}</strong>
                <span>${escapeHtml(factor.summary)}</span>
              </div>
              <div class="factor-meta">
                <span>${escapeHtml(factor.value)}</span>
                <span class="factor-status ${factor.status}">${escapeHtml(factor.status.toUpperCase())}</span>
              </div>
            </button>
            <div id="factor-panel-${factor.key}" class="factor-detail" ${open ? '' : 'hidden'}>
              <div>${escapeHtml(factor.detail)}</div>
            </div>
          </div>
        `;
      }).join('');
    }

    function explanationCards(result, presentation) {
      const factors = buildFactors(result);
      const passed = factors.filter(factor => factor.status === 'pass').map(factor => factor.label);
      const failed = factors.filter(factor => factor.status === 'fail').map(factor => factor.label);
      const unknown = factors.filter(factor => factor.status === 'unknown').map(factor => factor.label);

      return [
        {
          title: 'Why this result happened',
          body: presentation.summary
        },
        {
          title: 'What passed or failed',
          body: `Passed: ${passed.length ? passed.join(', ') : 'None clearly passed'}. Failed: ${failed.length ? failed.join(', ') : 'No hard failures beyond general caution'}.`
        },
        {
          title: 'What reduced confidence',
          body: unknown.length ? `Unknown factors: ${unknown.join(', ')}.` : 'Confidence was not reduced by missing factors.'
        },
        {
          title: 'What to do next',
          body: presentation.nextAction
        }
      ];
    }

    function renderResult(result) {
      state.currentResult = result;
      const presentation = resultPresentation(result);
      $('resultBadge').className = `status-chip ${statusChipClass(presentation.status)}`;
      $('resultBadge').textContent = presentation.badge;
      $('resultHeadline').textContent = presentation.headline;
      $('resultSummary').textContent = presentation.summary;
      $('primaryReason').textContent = presentation.primaryReason;
      $('nextAction').textContent = presentation.nextAction;
      $('resultSignalStat').textContent = presentation.signal;
      $('resultScoreStat').textContent = presentation.score;
      $('resultPriceStat').textContent = presentation.price;
      $('resultTimestamp').textContent = state.lastSyncLabel || 'Updated just now';
      $('confidenceLabel').textContent = presentation.confidence.label;
      $('confidencePercent').textContent = `${presentation.confidence.percent}%`;
      $('confidenceFill').style.width = `${presentation.confidence.percent}%`;
      $('confidenceFill').style.background = presentation.confidence.color;

      const cards = explanationCards(result, presentation);
      $('explainContent').innerHTML = cards.map(card => `
        <div class="explain-card">
          <strong>${escapeHtml(card.title)}</strong>
          <p>${escapeHtml(card.body)}</p>
        </div>
      `).join('');

      renderFactors(result);

      const actionDisabled = !result;
      $('retryButton').disabled = actionDisabled;
      $('rawButton').disabled = actionDisabled;
      $('saveButton').disabled = actionDisabled;
      const compareDisabled = !result && state.recentAnalyses.length < 2;
      $('summaryCompareButton').disabled = compareDisabled;
      $('compareButton').disabled = compareDisabled;

      announceResult(`${presentation.badge}. ${presentation.headline} ${presentation.nextAction}`);
    }

    function openExplain(force = null) {
      const content = $('explainContent');
      const toggle = $('explainToggle');
      const open = force == null ? content.hidden : !force;
      content.hidden = open;
      toggle.setAttribute('aria-expanded', String(!open));
      $('explainChevron').textContent = open ? '+' : '-';
    }

    function renderMarketOverview(regime) {
      state.currentRegime = regime;
      const regimeLabel = normalizeSignalLabel(regime && regime.regime);
      $('regimeLabel').textContent = regime && regime.regime ? regimeLabel : '--';
      $('regimeNote').textContent = regime && regime.flags && regime.flags.length
        ? cleanReason(regime.flags[0])
        : 'Regime explanation unavailable.';

      $('vixLabel').textContent = regime && Number.isFinite(Number(regime.vix))
        ? Number(regime.vix).toFixed(1)
        : '--';
      $('vixNote').textContent = regime && Number.isFinite(Number(regime.vix))
        ? (Number(regime.vix) < 20 ? 'Risk appetite is relatively stable.' : 'Volatility is elevated enough to matter.')
        : 'Fear gauge unavailable.';

      $('spyLabel').textContent = regime && Number.isFinite(Number(regime.spy_vs_200))
        ? formatPercent(regime.spy_vs_200, 1)
        : '--';
      $('spyNote').textContent = regime && Number.isFinite(Number(regime.spy_vs_200))
        ? (Number(regime.spy_vs_200) >= 0 ? 'SPY is trading above its long-term trend.' : 'SPY is below its long-term trend.')
        : 'Trend context unavailable.';

      const posture = normalizeSignalLabel((regime && regime.strategy && regime.strategy.bias) || '--');
      $('biasLabel').textContent = posture;
      $('biasNote').textContent = regime && regime.strategy
        ? `Position size multiplier ${(Number(regime.strategy.position_size_multiplier || 0) * 100).toFixed(0)}%.`
        : 'Position sizing guidance pending.';

      updateTopSync(new Date());
    }

    function updateTopSync(value) {
      state.lastSyncLabel = formatTime(value);
      $('topSyncLabel').textContent = state.lastSyncLabel;
    }

    function providerStatusModel() {
      const payload = state.providerHealth;
      if (!payload) return { className: 'status-info', label: 'Syncing', narrative: 'Provider diagnostics are still loading.' };

      const ageMs = state.providerCheckedAt ? Date.now() - new Date(state.providerCheckedAt).getTime() : null;
      if (ageMs != null && ageMs > 5 * 60 * 1000) {
        return { className: 'status-stale', label: 'Stale', narrative: 'Provider diagnostics are older than five minutes.' };
      }

      if (payload.overall === 'HEALTHY') {
        return { className: 'status-healthy', label: 'Connected', narrative: 'Price and fundamentals look healthy.' };
      }

      const fundamentalsError = payload.fundamentals && payload.fundamentals.status !== 'OK';
      const testStatuses = Object.values(payload.tests || {}).map(item => item.status);
      const anyPriceSuccess = testStatuses.some(status => status === 'OK');
      if (!anyPriceSuccess && fundamentalsError) {
        return { className: 'status-down', label: 'Down', narrative: 'Both price history and fundamentals are failing.' };
      }
      return { className: 'status-degraded', label: 'Degraded', narrative: 'Some provider checks are failing or incomplete.' };
    }

    function renderProviderHealth() {
      const healthList = $('healthList');
      const payload = state.providerHealth;
      const statusModel = providerStatusModel();
      $('statusBadge').className = `status-pill ${statusModel.className}`;
      $('statusBadgeText').textContent = statusModel.label;

      if (!payload) {
        healthList.innerHTML = `
          <div class="health-item">
            <strong>Diagnostics pending</strong>
            <span>Provider checks have not completed yet.</span>
          </div>
        `;
        $('healthNarrative').textContent = statusModel.narrative;
        return;
      }

      const tests = payload.tests || {};
      const fundamentals = payload.fundamentals || {};
      const priceRows = Object.entries(tests).map(([symbol, item]) => `
        <div class="health-item">
          <strong>${escapeHtml(symbol)} price feed: ${escapeHtml(item.status || 'UNKNOWN')}</strong>
          <span>${escapeHtml(item.error || `${item.rows || 0} rows checked in ${item.elapsed_sec || '--'}s`)}</span>
        </div>
      `).join('');

      const fundamentalsSummary = fundamentals.status === 'OK'
        ? `Market cap ${formatCurrency(fundamentals.market_cap)}`
        : (fundamentals.error || 'Fundamental payload unavailable.');

      healthList.innerHTML = `
        ${priceRows}
        <div class="health-item">
          <strong>Fundamentals: ${escapeHtml(fundamentals.status || 'UNKNOWN')}</strong>
          <span>${escapeHtml(fundamentalsSummary)}</span>
        </div>
        <div class="health-item">
          <strong>Sentiment services: External</strong>
          <span>News, social, and insider checks depend on upstream APIs that are not included in the yfinance diagnostic.</span>
        </div>
      `;

      const checked = state.providerCheckedAt ? formatTime(state.providerCheckedAt) : '--';
      $('healthNarrative').textContent = `${statusModel.narrative} Last checked ${checked}.`;
      announce(`Provider health ${statusModel.label}. ${statusModel.narrative}`);
    }

    function persistRecentAnalysis(result) {
      if (!result || !result.ticker) return;
      const entry = {
        ticker: result.ticker,
        signal: result.signal,
        score: result.composite_score ?? result.score ?? 0,
        confidence: result.confidence || 'N/A',
        timestamp: new Date().toISOString(),
        result
      };
      state.recentAnalyses = [entry, ...state.recentAnalyses.filter(item => item.ticker !== entry.ticker)].slice(0, 8);
      persistStoredArray(STORAGE_KEYS.recent, state.recentAnalyses);
      renderRecentAnalyses();
    }

    function saveCurrentResult() {
      if (!state.currentResult) return;
      const entry = {
        ticker: state.currentResult.ticker,
        signal: state.currentResult.signal,
        score: state.currentResult.composite_score ?? state.currentResult.score ?? 0,
        confidence: state.currentResult.confidence || 'N/A',
        timestamp: new Date().toISOString(),
        result: state.currentResult
      };
      state.savedResults = [entry, ...state.savedResults.filter(item => item.timestamp !== entry.timestamp)].slice(0, 12);
      persistStoredArray(STORAGE_KEYS.saved, state.savedResults);
      showToast(`Saved ${entry.ticker} for later review.`, 'success');
    }

    function renderRecentAnalyses() {
      const list = $('recentList');
      if (!state.recentAnalyses.length) {
        list.innerHTML = `
          <div class="recent-item">
            <div class="recent-meta">
              <div class="recent-title">No recent analyses</div>
              <div class="recent-subtle">Run a ticker analysis to build a quick-return history.</div>
            </div>
          </div>
        `;
        return;
      }

      list.innerHTML = state.recentAnalyses.map(item => `
        <div class="recent-item">
          <button class="recent-button" type="button" data-reopen-ticker="${item.ticker}">
            <div class="recent-meta">
              <div class="recent-title">${escapeHtml(item.ticker)} • ${escapeHtml(normalizeSignalLabel(item.signal))}</div>
              <div class="recent-subtle">Score ${escapeHtml(formatScore(item.score))} • ${escapeHtml(item.confidence || 'N/A')}</div>
            </div>
            <div class="recent-subtle">${escapeHtml(formatTime(item.timestamp))}</div>
          </button>
        </div>
      `).join('');
    }

    function deriveRegimeFit(row) {
      const macro = Number(row.scores && row.scores.macro);
      if (!Number.isFinite(macro)) return 'Unknown';
      if (macro >= 70) return 'Strong';
      if (macro >= 55) return 'Aligned';
      return 'Cautious';
    }

    function spotlightBucketModel(key) {
      if (key === 'strong_buys') {
        return { title: 'Strong Buy', tone: 'strong' };
      }
      if (key === 'buys') {
        return { title: 'Buy', tone: 'buy' };
      }
      return { title: 'Watch List', tone: 'watch' };
    }

    function spotlightScoreClass(score) {
      const value = Number(score);
      if (!Number.isFinite(value) || value < 45) return 'low';
      if (value < 70) return 'cautious';
      return '';
    }

    function summarizeSpotlightReasons(item) {
      return collectReasons(item).slice(0, 3).map(reason => cleanReason(reason)).filter(Boolean);
    }

    function signalRowsForSpotlights(scan) {
      return [
        ['strong_buys', scan && scan.strong_buys ? scan.strong_buys : []],
        ['buys', scan && scan.buys ? scan.buys : []],
        ['watch_list', scan && scan.watch_list ? scan.watch_list : []],
      ];
    }

    function renderScanSpotlights(scan) {
      const root = $('scanSpotlights');
      const groups = signalRowsForSpotlights(scan).filter(([, items]) => items.length);

      if (!groups.length) {
        root.innerHTML = '<div class="spotlight-empty">Run a full market scan to surface ranked candidates as compact trading cards.</div>';
        return;
      }

      root.innerHTML = groups.map(([key, items]) => {
        const bucket = spotlightBucketModel(key);
        return `
          <section class="spotlight-bucket" aria-label="${escapeHtml(bucket.title)} candidates">
            <div class="spotlight-bucket-header">
              <div class="spotlight-bucket-title ${bucket.tone}">${escapeHtml(bucket.title)}</div>
              <div class="spotlight-bucket-count">${items.length} candidate${items.length === 1 ? '' : 's'}</div>
            </div>
            <div class="spotlight-grid">
              ${items.slice(0, 3).map(item => {
                const score = Number(item.composite_score ?? item.score ?? 0);
                const scoreClass = spotlightScoreClass(score);
                const confidence = item.confidence || 'N/A';
                const reasons = summarizeSpotlightReasons(item);
                const entryPrice = item.entry && item.entry.price;
                const stopPrice = item.stop_loss && item.stop_loss.price;
                const positionSize = item.position && item.position.shares;
                return `
                  <article class="spotlight-card ${bucket.tone}">
                    <div class="spotlight-head">
                      <div class="spotlight-ticker">
                        <strong>${escapeHtml(item.ticker || '?')}</strong>
                        <div class="spotlight-price">${escapeHtml(formatPrice(item.current_price ?? entryPrice))}</div>
                      </div>
                      <div class="spotlight-signal">${escapeHtml(normalizeSignalLabel(item.signal || bucket.title))}</div>
                    </div>
                    <div class="spotlight-score-row">
                      <div class="spotlight-score-meta">
                        <span>Composite score</span>
                        <span>${escapeHtml(confidence)}</span>
                      </div>
                      <div class="spotlight-score ${scoreClass}">${escapeHtml(formatScore(score))}<span style="color:var(--text-muted);font-size:12px">/100</span></div>
                      <div class="spotlight-track" aria-hidden="true">
                        <div class="spotlight-fill ${scoreClass}" style="width:${Math.max(0, Math.min(100, score))}%"></div>
                      </div>
                    </div>
                    <div class="spotlight-stats">
                      <div class="spotlight-stat">
                        <span>Regime fit</span>
                        <strong>${escapeHtml(deriveRegimeFit(item))}</strong>
                      </div>
                      <div class="spotlight-stat">
                        <span>Volume</span>
                        <strong>${Number.isFinite(Number((item.scores || {}).volume)) ? escapeHtml(formatScore((item.scores || {}).volume)) : '--'}</strong>
                      </div>
                      <div class="spotlight-stat">
                        <span>Sentiment</span>
                        <strong>${Number.isFinite(Number((item.scores || {}).sentiment)) ? escapeHtml(formatScore((item.scores || {}).sentiment)) : '--'}</strong>
                      </div>
                    </div>
                    <div class="spotlight-levels">
                      <div class="spotlight-level">
                        <span>Entry</span>
                        <strong>${escapeHtml(formatPrice(entryPrice))}</strong>
                      </div>
                      <div class="spotlight-level">
                        <span>Stop</span>
                        <strong>${escapeHtml(formatPrice(stopPrice))}</strong>
                      </div>
                      <div class="spotlight-level">
                        <span>Position</span>
                        <strong>${Number.isFinite(Number(positionSize)) ? escapeHtml(String(positionSize)) + ' sh' : '--'}</strong>
                      </div>
                      <div class="spotlight-level">
                        <span>Risk</span>
                        <strong>${item.position && Number.isFinite(Number(item.position.risk_percent)) ? escapeHtml(formatPercent(item.position.risk_percent, 2)) : '--'}</strong>
                      </div>
                    </div>
                    ${reasons.length ? `<ul class="spotlight-reasons">${reasons.map(reason => `<li>${escapeHtml(reason)}</li>`).join('')}</ul>` : ''}
                    <button class="table-action" type="button" data-view-ticker="${escapeHtml(item.ticker || '')}">Open analysis</button>
                  </article>
                `;
              }).join('')}
            </div>
          </section>
        `;
      }).join('');
    }

    function flattenScanRows(scan) {
      const groups = [
        ...(scan.strong_buys || []),
        ...(scan.buys || []),
        ...(scan.watch_list || [])
      ];

      return groups.map(item => ({
        ticker: item.ticker || '?',
        signal: item.signal || 'UNKNOWN',
        score: Number(item.composite_score ?? item.score ?? 0),
        confidence: item.confidence || 'N/A',
        regimeFit: deriveRegimeFit(item),
        volume: Number((item.scores || {}).volume),
        sentiment: Number((item.scores || {}).sentiment),
        updated: scan.scan_date || new Date().toISOString(),
        row: item
      }));
    }

    function applyScanFilters(rows) {
      const search = $('scanSearch').value.trim().toUpperCase();
      const minScore = Number($('scanMinScore').value);
      const confidence = $('scanConfidence').value;
      const regimeFit = $('scanRegimeFit').value;
      const volumeMin = Number($('scanVolumeFilter').value);
      const sort = $('scanSort').value;

      let filtered = rows.filter(item => {
        if (search && !item.ticker.includes(search)) return false;
        if (item.score < minScore) return false;
        if (confidence !== 'ALL' && item.confidence !== confidence) return false;
        if (regimeFit !== 'ALL' && item.regimeFit.toUpperCase() !== regimeFit) return false;
        if (volumeMin > 0 && (!Number.isFinite(item.volume) || item.volume < volumeMin)) return false;
        return true;
      });

      const confidenceRank = { VERY_HIGH: 4, HIGH: 3, MODERATE: 2, LOW: 1, N_A: 0, 'N/A': 0 };
      filtered = filtered.sort((a, b) => {
        if (sort === 'score-asc') return a.score - b.score;
        if (sort === 'ticker-asc') return a.ticker.localeCompare(b.ticker);
        if (sort === 'confidence-desc') return (confidenceRank[b.confidence] || 0) - (confidenceRank[a.confidence] || 0);
        return b.score - a.score;
      });

      return filtered;
    }

    function renderScanResults(scan) {
      state.currentScan = scan;
      renderScanSpotlights(scan);
      const rows = flattenScanRows(scan);
      const filtered = applyScanFilters(rows);
      $('scanFeedback').textContent = rows.length
        ? `${rows.length} candidates available from the latest scan. Showing ${filtered.length} after filters.`
        : (scan.alert || 'No tickers matched the current market conditions.');

      $('scanEmptyState').hidden = filtered.length > 0;
      $('scanTableBody').innerHTML = filtered.map(item => `
        <tr class="scan-row" tabindex="0" data-scan-ticker="${item.ticker}">
          <td><strong>${escapeHtml(item.ticker)}</strong></td>
          <td>${escapeHtml(formatScore(item.score))}</td>
          <td><span class="scan-pill">${escapeHtml(normalizeSignalLabel(item.signal))}</span></td>
          <td>${escapeHtml(item.confidence)}</td>
          <td>${escapeHtml(item.regimeFit)}</td>
          <td>${Number.isFinite(item.volume) ? escapeHtml(formatScore(item.volume)) : '--'}</td>
          <td>${Number.isFinite(item.sentiment) ? escapeHtml(formatScore(item.sentiment)) : '--'}</td>
          <td>${escapeHtml(item.updated)}</td>
          <td><button class="table-action" type="button" data-view-ticker="${item.ticker}">View</button></td>
        </tr>
      `).join('');

      $('scanMobileList').innerHTML = filtered.map(item => `
        <div class="scan-mobile-card">
          <strong>${escapeHtml(item.ticker)} • ${escapeHtml(normalizeSignalLabel(item.signal))}</strong>
          <span>Score ${escapeHtml(formatScore(item.score))} • ${escapeHtml(item.confidence)}</span>
          <span>Regime fit ${escapeHtml(item.regimeFit)} • Volume ${Number.isFinite(item.volume) ? escapeHtml(formatScore(item.volume)) : '--'}</span>
          <button class="table-action" type="button" data-view-ticker="${item.ticker}">View</button>
        </div>
      `).join('');
    }

    function polylinePoints(points, key, width, height, padding, min, max) {
      if (!points.length || min === max) return '';
      return points.map((point, index) => {
        const x = padding + (index / (points.length - 1 || 1)) * (width - padding * 2);
        const y = height - padding - ((point[key] - min) / (max - min)) * (height - padding * 2);
        return `${x},${y}`;
      }).join(' ');
    }

    function renderChart(payload) {
      state.currentChart = payload;
      const frame = $('chartFrame');
      if (!payload || !payload.points || !payload.points.length) {
        frame.innerHTML = '<div class="chart-empty" id="chartEmpty">Analyze a ticker to view chart context.</div>';
        $('chartSummary').textContent = payload && payload.summary && payload.summary.note
          ? payload.summary.note
          : 'Price and trend summary will appear here.';
        $('chartUpdated').textContent = 'No chart loaded';
        return;
      }

      const points = payload.points;
      const width = 720;
      const height = 300;
      const padding = 24;
      const prices = points.map(point => Number(point.close)).filter(Number.isFinite);
      const ma20 = points.map(point => Number(point.sma20)).filter(Number.isFinite);
      const ma50 = points.map(point => Number(point.sma50)).filter(Number.isFinite);
      const min = Math.min(...prices, ...(state.showMa ? ma20 : []), ...(state.showMa ? ma50 : []));
      const max = Math.max(...prices, ...(state.showMa ? ma20 : []), ...(state.showMa ? ma50 : []));
      const pricePoints = polylinePoints(points, 'close', width, height, padding, min, max);
      const areaPath = `${pricePoints} ${width - padding},${height - padding} ${padding},${height - padding}`;
      const ma20Points = polylinePoints(points.filter(point => Number.isFinite(Number(point.sma20))), 'sma20', width, height, padding, min, max);
      const ma50Points = polylinePoints(points.filter(point => Number.isFinite(Number(point.sma50))), 'sma50', width, height, padding, min, max);

      frame.innerHTML = `
        <svg class="chart-svg" viewBox="0 0 ${width} ${height}" role="img" aria-label="${escapeHtml(payload.summary.accessible_summary || 'Ticker chart')}">
          <defs>
            <linearGradient id="priceArea" x1="0" y1="0" x2="0" y2="1">
              <stop offset="0%" stop-color="rgba(79,140,255,0.42)"></stop>
              <stop offset="100%" stop-color="rgba(79,140,255,0.02)"></stop>
            </linearGradient>
          </defs>
          <polygon fill="url(#priceArea)" points="${areaPath}"></polygon>
          <polyline fill="none" stroke="#5AA7FF" stroke-width="3" stroke-linecap="round" stroke-linejoin="round" points="${pricePoints}"></polyline>
          ${state.showMa && ma20Points ? `<polyline fill="none" stroke="#F2B84B" stroke-width="2" stroke-linecap="round" points="${ma20Points}"></polyline>` : ''}
          ${state.showMa && ma50Points ? `<polyline fill="none" stroke="#1FBF75" stroke-width="2" stroke-linecap="round" points="${ma50Points}"></polyline>` : ''}
        </svg>
      `;

      $('chartSummary').textContent = payload.summary && payload.summary.accessible_summary
        ? payload.summary.accessible_summary
        : `${payload.ticker} chart loaded.`;
      $('chartUpdated').textContent = `Updated ${formatTime(new Date())}`;
    }

    function openDrawer(title, html) {
      $('drawerTitle').textContent = title;
      $('drawerBody').innerHTML = html;
      $('drawerBackdrop').hidden = false;
      $('drawer').hidden = false;
      $('drawer').setAttribute('aria-hidden', 'false');
      $('drawerClose').focus();
    }

    function closeDrawer() {
      $('drawerBackdrop').hidden = true;
      $('drawer').hidden = true;
      $('drawer').setAttribute('aria-hidden', 'true');
    }

    function openMetricDetail(metric) {
      const regime = state.currentRegime || {};
      const details = {
        regime: {
          title: 'Market Regime',
          body: `Current value: ${escapeHtml(normalizeSignalLabel(regime.regime || 'Unknown'))}. This metric summarizes whether the market backdrop is favorable, neutral, or defensive.`
        },
        vix: {
          title: 'VIX',
          body: `Current value: ${escapeHtml(Number.isFinite(Number(regime.vix)) ? Number(regime.vix).toFixed(1) : 'Unavailable')}. Lower readings usually support risk-on participation; higher readings demand tighter caution.`
        },
        spy: {
          title: 'SPY vs 200SMA',
          body: `Current value: ${escapeHtml(Number.isFinite(Number(regime.spy_vs_200)) ? formatPercent(regime.spy_vs_200, 1) : 'Unavailable')}. This shows whether the broad market is above or below its long-term trend anchor.`
        },
        strategy: {
          title: 'Strategy Posture',
          body: `Current value: ${escapeHtml(normalizeSignalLabel((regime.strategy || {}).bias || 'Unknown'))}. The posture tells you how aggressive position sizing should be under current conditions.`
        }
      };
      const selected = details[metric];
      if (!selected) return;
      openDrawer(selected.title, `<div class="drawer-card"><p>${escapeHtml(selected.body)}</p></div>`);
    }

    function openHelpDrawer() {
      openDrawer('Help', `
        <div class="drawer-card">
          <p>Use this workspace in three passes:</p>
          <p>1. Check Market Overview for regime context.</p>
          <p>2. Analyze a ticker to get a plain-English result, factor breakdown, and chart context.</p>
          <p>3. Run a full market scan to rank opportunities, then reopen any row with keyboard or mouse.</p>
        </div>
        <div class="drawer-card">
          <p>Keyboard shortcuts:</p>
          <p>Enter in the ticker field runs analysis when the form is valid.</p>
          <p>Escape closes this drawer.</p>
          <p>Arrow keys move through scan rows after you focus the table.</p>
        </div>
      `);
    }

    function openSettingsDrawer() {
      openDrawer('Settings', `
        <div class="drawer-card">
          <p>Use these local actions to reset dashboard state or refresh provider health.</p>
        </div>
        <div class="drawer-card">
          <button class="action-button" type="button" data-drawer-action="refresh-health">Retry diagnostics</button>
        </div>
        <div class="drawer-card">
          <button class="ghost-button" type="button" data-drawer-action="clear-cache">Clear cache</button>
        </div>
        <div class="drawer-card">
          <button class="ghost-button" type="button" data-drawer-action="clear-recent">Clear recent analyses</button>
        </div>
      `);
    }

    function openRawDataDrawer() {
      if (!state.currentResult) return;
      openDrawer('Raw Result Payload', `
        <div class="drawer-card">
          <pre>${escapeHtml(JSON.stringify(state.currentResult, null, 2))}</pre>
        </div>
      `);
    }

    function compareCurrentResult() {
      const candidates = state.recentAnalyses.filter(item => state.currentResult ? item.ticker !== state.currentResult.ticker : true);
      const comparison = candidates[0] || state.savedResults[0];
      if (!state.currentResult || !comparison) {
        showToast('Run at least two analyses before comparing outcomes.', 'info');
        return;
      }

      const currentScore = Number(state.currentResult.composite_score ?? state.currentResult.score ?? 0);
      const previousScore = Number(comparison.score ?? comparison.result?.score ?? 0);
      const delta = currentScore - previousScore;
      openDrawer('Compare Results', `
        <div class="drawer-card">
          <p><strong>Current:</strong> ${escapeHtml(state.currentResult.ticker)} • ${escapeHtml(normalizeSignalLabel(state.currentResult.signal))} • Score ${escapeHtml(formatScore(currentScore))}</p>
          <p><strong>Previous:</strong> ${escapeHtml(comparison.ticker)} • ${escapeHtml(normalizeSignalLabel(comparison.signal || comparison.result?.signal))} • Score ${escapeHtml(formatScore(previousScore))}</p>
          <p><strong>Delta:</strong> ${escapeHtml(delta >= 0 ? '+' : '')}${escapeHtml(formatScore(delta))} points.</p>
        </div>
      `);
    }

    async function loadProviderHealth() {
      try {
        const payload = await apiFetchJson('/api/diagnose');
        state.providerHealth = payload;
        state.providerCheckedAt = new Date().toISOString();
        renderProviderHealth();
      } catch (error) {
        state.providerHealth = {
          overall: 'DEGRADED',
          tests: {},
          fundamentals: { status: 'ERROR', error: error.message }
        };
        state.providerCheckedAt = new Date().toISOString();
        renderProviderHealth();
      }
    }

    async function loadRegime() {
      try {
        const payload = await apiFetchJson('/api/regime');
        renderMarketOverview(payload);
      } catch (error) {
        showToast(`Regime load failed: ${error.message}`, 'error');
      }
    }

    async function loadChart(ticker, range = state.chartRange) {
      if (!ticker) {
        renderChart(null);
        return;
      }
      state.chartTicker = ticker;
      state.chartRange = range;
      try {
        const payload = await apiFetchJson(`/api/price-history?ticker=${encodeURIComponent(ticker)}&range=${encodeURIComponent(range)}`);
        renderChart(payload);
      } catch (error) {
        renderChart({ ticker, summary: { note: error.message }, points: [] });
      }
      renderRangeButtons();
    }

    function renderRangeButtons() {
      $('rangeGroup').innerHTML = CHART_RANGES.map(range => `
        <button class="range-button ${state.chartRange === range ? 'active' : ''}" type="button" data-range="${range}">
          ${range}
        </button>
      `).join('');
    }

    async function analyzeTicker() {
      const { ticker, account } = updateFormState();
      if (ticker.error || account.error) {
        stopProgress('Fix the highlighted fields and try again.');
        return;
      }

      $('tickerInput').value = ticker.value;
      startProgress('analyze');
      try {
        const payload = await apiFetchJson(`/api/scan-ticker?ticker=${encodeURIComponent(ticker.value)}&account_size=${encodeURIComponent(account.value)}`);
        updateTopSync(new Date());
        renderResult(payload);
        persistRecentAnalysis(payload);
        await loadChart(payload.ticker || ticker.value, state.chartRange);
        stopProgress(`Analysis ready for ${ticker.value}.`);
        showToast(`Analysis ready for ${ticker.value}.`, 'success');
      } catch (error) {
        renderResult({
          ticker: ticker.value,
          signal: 'ERROR',
          confidence: 'N/A',
          score: 0,
          reasons: [error.message],
          disqualifiers: [error.message]
        });
        stopProgress('Analysis failed. Check provider health or retry.');
        showToast(error.message, 'error', true);
      }
    }

    async function runFullMarketScan() {
      const { account } = updateFormState();
      if (account.error) {
        stopProgress('Enter a positive account size before scanning.');
        return;
      }

      startProgress('scan');
      try {
        const payload = await apiFetchJson(`/api/scan?account_size=${encodeURIComponent(account.value)}`);
        updateTopSync(new Date());
        renderScanResults(payload);
        stopProgress('Full market scan complete.');
        const total = (payload.strong_buys || []).length + (payload.buys || []).length + (payload.watch_list || []).length;
        showToast(`Scan complete. ${total} actionable candidates found.`, 'success');
      } catch (error) {
        $('scanFeedback').textContent = `Scan failed: ${error.message}`;
        renderScanSpotlights(null);
        $('scanTableBody').innerHTML = '';
        $('scanMobileList').innerHTML = '';
        $('scanEmptyState').hidden = false;
        stopProgress('Full market scan failed.');
        showToast(error.message, 'error', true);
      }
    }

    async function retryCurrentAnalysis() {
      if (!state.currentResult || !state.currentResult.ticker) return;
      $('tickerInput').value = state.currentResult.ticker;
      updateFormState();
      await analyzeTicker();
    }

    async function clearCacheAndRefresh() {
      try {
        await apiFetchJson('/api/clear-cache');
        showToast('Cache cleared. Refreshing diagnostics.', 'success');
        await loadProviderHealth();
        if (state.currentResult && state.currentResult.ticker) {
          await loadChart(state.currentResult.ticker, state.chartRange);
        }
      } catch (error) {
        showToast(error.message, 'error', true);
      }
    }

    function viewTickerFromScan(ticker) {
      $('tickerInput').value = ticker;
      updateFormState();
      analyzeTicker();
      window.scrollTo({ top: 0, behavior: 'smooth' });
    }

    function resetFilters() {
      $('scanSearch').value = '';
      $('scanMinScore').value = '0';
      $('scanConfidence').value = 'ALL';
      $('scanRegimeFit').value = 'ALL';
      $('scanVolumeFilter').value = '0';
      $('scanSort').value = 'score-desc';
      if (state.currentScan) {
        renderScanResults(state.currentScan);
      }
    }

    function handleScanRowKeys(event) {
      const rows = [...document.querySelectorAll('.scan-row')];
      const index = rows.indexOf(document.activeElement);
      if (index === -1) return;
      if (event.key === 'ArrowDown' && rows[index + 1]) {
        rows[index + 1].focus();
        event.preventDefault();
      }
      if (event.key === 'ArrowUp' && rows[index - 1]) {
        rows[index - 1].focus();
        event.preventDefault();
      }
      if (event.key === 'Enter') {
        const ticker = document.activeElement.getAttribute('data-scan-ticker');
        if (ticker) {
          viewTickerFromScan(ticker);
          event.preventDefault();
        }
      }
    }

    function seedSuggestionChips() {
      const recentTickers = state.recentAnalyses.map(item => item.ticker).slice(0, 4);
      const combined = [...new Set([...SUGGESTIONS, ...recentTickers])];
      $('suggestionRow').innerHTML = combined.map(ticker => `
        <button class="chip-button" type="button" data-suggest="${ticker}">${ticker}</button>
      `).join('');
    }

    function initializeResultState() {
      renderResult(null);
      renderRecentAnalyses();
      renderProviderHealth();
      renderRangeButtons();
      renderChart(null);
      openExplain(false);
      seedSuggestionChips();
      updateFormState();
    }

    $('analysisForm').addEventListener('submit', event => {
      event.preventDefault();
      analyzeTicker();
    });

    $('scanButton').addEventListener('click', runFullMarketScan);
    $('compareButton').addEventListener('click', compareCurrentResult);
    $('summaryCompareButton').addEventListener('click', compareCurrentResult);
    $('retryButton').addEventListener('click', retryCurrentAnalysis);
    $('rawButton').addEventListener('click', openRawDataDrawer);
    $('saveButton').addEventListener('click', saveCurrentResult);
    $('helpButton').addEventListener('click', openHelpDrawer);
    $('settingsButton').addEventListener('click', openSettingsDrawer);
    $('statusBadge').addEventListener('click', () => {
      document.getElementById('providerHealthPanel').scrollIntoView({ behavior: 'smooth', block: 'start' });
      showToast('Scrolled to provider health.', 'info');
    });
    $('refreshHealthButton').addEventListener('click', loadProviderHealth);
    $('clearCacheButton').addEventListener('click', clearCacheAndRefresh);
    $('clearFiltersButton').addEventListener('click', resetFilters);
    $('clearFiltersInlineButton').addEventListener('click', resetFilters);
    $('expandFactorsButton').addEventListener('click', () => {
      state.expandAllFactors = !state.expandAllFactors;
      if (!state.expandAllFactors) state.factorOpen = new Set();
      renderFactors(state.currentResult);
      $('expandFactorsButton').textContent = state.expandAllFactors ? 'Collapse factors' : 'View all factors';
    });
    $('explainToggle').addEventListener('click', () => openExplain($('explainContent').hidden));
    $('maToggle').addEventListener('click', () => {
      state.showMa = !state.showMa;
      $('maToggle').classList.toggle('active', state.showMa);
      $('maToggle').setAttribute('aria-pressed', String(state.showMa));
      renderChart(state.currentChart);
    });
    $('drawerClose').addEventListener('click', closeDrawer);
    $('drawerBackdrop').addEventListener('click', closeDrawer);

    ['tickerInput', 'accountInput'].forEach(id => {
      $(id).addEventListener('input', updateFormState);
    });

    ['scanSearch', 'scanMinScore', 'scanConfidence', 'scanRegimeFit', 'scanVolumeFilter', 'scanSort'].forEach(id => {
      $(id).addEventListener('input', () => {
        if (state.currentScan) renderScanResults(state.currentScan);
      });
      $(id).addEventListener('change', () => {
        if (state.currentScan) renderScanResults(state.currentScan);
      });
    });

    $('tickerInput').addEventListener('keydown', event => {
      if (event.key === 'Enter' && !$('analyzeButton').disabled) {
        event.preventDefault();
        analyzeTicker();
      }
    });

    document.addEventListener('click', event => {
      const target = event.target.closest('[data-suggest], [data-factor-key], [data-metric], [data-range], [data-view-ticker], [data-reopen-ticker], [data-drawer-action]');
      if (!target) return;

      if (target.dataset.suggest) {
        $('tickerInput').value = target.dataset.suggest;
        updateFormState();
        analyzeTicker();
      }

      if (target.dataset.factorKey) {
        const key = target.dataset.factorKey;
        if (state.factorOpen.has(key)) state.factorOpen.delete(key);
        else state.factorOpen.add(key);
        renderFactors(state.currentResult);
      }

      if (target.dataset.metric) {
        openMetricDetail(target.dataset.metric);
      }

      if (target.dataset.range) {
        if (state.chartTicker) loadChart(state.chartTicker, target.dataset.range);
      }

      if (target.dataset.viewTicker) {
        viewTickerFromScan(target.dataset.viewTicker);
      }

      if (target.dataset.reopenTicker) {
        viewTickerFromScan(target.dataset.reopenTicker);
      }

      if (target.dataset.drawerAction === 'refresh-health') {
        closeDrawer();
        loadProviderHealth();
      }

      if (target.dataset.drawerAction === 'clear-cache') {
        closeDrawer();
        clearCacheAndRefresh();
      }

      if (target.dataset.drawerAction === 'clear-recent') {
        state.recentAnalyses = [];
        persistStoredArray(STORAGE_KEYS.recent, []);
        renderRecentAnalyses();
        closeDrawer();
        showToast('Recent analyses cleared.', 'success');
      }
    });

    document.addEventListener('keydown', event => {
      if (event.key === 'Escape' && !$('drawer').hidden) {
        closeDrawer();
      }
      handleScanRowKeys(event);
    });

    clearAuthRecoveryMarkerIfStale();
    initializeResultState();
    loadRegime();
    loadProviderHealth();
  </script>
</body>
</html>
'''


def _error_response(message: str, status: int):
    return jsonify({"error": message}), status


def _parse_account_size(raw_value: str | None) -> tuple[float | None, str | None]:
    if raw_value is None or raw_value == "":
        return config.ACCOUNT_SIZE, None
    try:
        value = float(raw_value)
    except (TypeError, ValueError):
        return None, "account_size must be numeric"
    if value <= 0:
        return None, "account_size must be positive"
    if value < config.DASHBOARD_MIN_ACCOUNT_SIZE:
        return None, f"account_size must be >= {config.DASHBOARD_MIN_ACCOUNT_SIZE:.0f}"
    if value > config.DASHBOARD_MAX_ACCOUNT_SIZE:
        return None, f"account_size must be <= {config.DASHBOARD_MAX_ACCOUNT_SIZE:.0f}"
    return value, None


def _parse_ticker(raw_value: str | None) -> tuple[str | None, str | None]:
    ticker = (raw_value or "").upper().strip()
    if not ticker:
        return None, "ticker parameter is required"
    if not TICKER_RE.fullmatch(ticker):
        return None, "ticker format is invalid"
    return ticker, None


def _runtime_host() -> str:
    if config.env("PORT"):
        return "0.0.0.0"
    if config.DASHBOARD_ALLOW_REMOTE:
        return "0.0.0.0"
    return config.DASHBOARD_HOST


def _runtime_port() -> int:
    return config.env_int("PORT", config.DASHBOARD_PORT)


def _downsample_frame(df: pd.DataFrame, max_points: int) -> pd.DataFrame:
    if df.empty or len(df) <= max_points:
        return df
    step = max(len(df) // max_points, 1)
    sampled = df.iloc[::step].copy()
    if sampled.index[-1] != df.index[-1]:
        sampled = pd.concat([sampled, df.iloc[[-1]]])
    return sampled


def _fallback_chart_frame(ticker: str, range_key: str) -> pd.DataFrame:
    fallback_cfg = {
        "1D": {"period": "1mo", "interval": "1d", "tail": 1},
        "5D": {"period": "1mo", "interval": "1d", "tail": 5},
        "1M": {"period": "1y", "interval": "1d", "tail": 22},
        "3M": {"period": "1y", "interval": "1d", "tail": 66},
        "1Y": {"period": "1y", "interval": "1d", "tail": 252},
    }[range_key]
    fallback = fetch_ohlcv(
        ticker,
        period=fallback_cfg["period"],
        interval=fallback_cfg["interval"],
        use_cache=True,
    )
    if fallback is None or fallback.empty:
        return pd.DataFrame()
    return fallback.tail(fallback_cfg["tail"]).copy()


def _build_chart_payload(ticker: str, range_key: str) -> dict:
    cfg = _CHART_RANGES[range_key]
    df = fetch_ohlcv(
        ticker,
        period=cfg["period"],
        interval=cfg["interval"],
        use_cache=True,
    )
    if df is None or df.empty:
        df = _fallback_chart_frame(ticker, range_key)
    if df is None or df.empty:
        return {
            "ticker": ticker,
            "range": range_key,
            "status": "EMPTY",
            "points": [],
            "summary": {
                "note": "No chart data returned for this range.",
                "accessible_summary": f"No chart data returned for {ticker}."
            },
        }

    df = _downsample_frame(df, cfg["max_points"]).copy()
    df["SMA20"] = df["Close"].rolling(20).mean()
    df["SMA50"] = df["Close"].rolling(50).mean()

    points: list[dict] = []
    for idx, row in df.iterrows():
        points.append({
            "t": idx.isoformat() if hasattr(idx, "isoformat") else str(idx),
            "close": round(float(row["Close"]), 2),
            "volume": int(row["Volume"]) if pd.notna(row["Volume"]) else None,
            "sma20": round(float(row["SMA20"]), 2) if pd.notna(row["SMA20"]) else None,
            "sma50": round(float(row["SMA50"]), 2) if pd.notna(row["SMA50"]) else None,
        })

    first_close = float(df["Close"].iloc[0])
    last_close = float(df["Close"].iloc[-1])
    change_pct = ((last_close / first_close) - 1) * 100 if first_close else 0.0
    high = float(df["High"].max())
    low = float(df["Low"].min())

    return {
        "ticker": ticker,
        "range": range_key,
        "status": "READY",
        "points": points,
        "summary": {
            "last_close": round(last_close, 2),
            "change_pct": round(change_pct, 2),
            "high": round(high, 2),
            "low": round(low, 2),
            "point_count": len(points),
            "accessible_summary": (
                f"{ticker} over {range_key}: last close {last_close:.2f}, "
                f"change {change_pct:+.2f} percent, high {high:.2f}, low {low:.2f}."
            ),
        },
    }


@app.before_request
def _api_security_guards():
    if not request.path.startswith("/api/"):
        return None

    client_key = resolve_client_ip(request)
    if not _rate_limiter.allow(client_key):
        return _error_response("Too many requests", 429)

    if config.DASHBOARD_ALLOW_REMOTE:
        if not has_auth_config():
            log.error("Remote dashboard enabled without configured dashboard API keys")
            return _error_response("Remote mode misconfigured", 503)
        if not is_authorized_request(request):
            return _error_response("Unauthorized", 401)
    return None


@app.route("/")
def index():
    ui_token = issue_dashboard_ui_token() if config.DASHBOARD_ALLOW_REMOTE else ""
    return render_template_string(HTML, ui_token=ui_token)


@app.route("/healthz")
def healthz():
    return jsonify({"status": "ok"}), 200


@app.route("/api/diagnose")
def api_diagnose():
    """Health check — test yfinance connectivity."""
    try:
        return jsonify(diagnose())
    except Exception:
        log.exception("Diagnose endpoint error")
        return jsonify({"error": "Diagnosis failed"}), 500


@app.route("/api/clear-cache")
def api_clear_cache():
    """Clear all cached data to force fresh fetches."""
    try:
        clear_data_caches()
        return jsonify({"status": "ok", "message": "All caches cleared"})
    except Exception:
        log.exception("Clear cache error")
        return jsonify({"error": "Failed to clear cache"}), 500


@app.route("/api/price-history")
def api_price_history():
    ticker, ticker_err = _parse_ticker(request.args.get("ticker"))
    if ticker_err:
        return _error_response(ticker_err, 400)

    range_key = (request.args.get("range") or "3M").upper()
    if range_key not in _CHART_RANGES:
        return _error_response("range must be one of 1D, 5D, 1M, 3M, 1Y", 400)

    try:
        return jsonify(_build_chart_payload(ticker, range_key))
    except Exception:
        log.exception("Price history endpoint error")
        return _error_response("Internal server error", 500)


@app.route("/api/regime")
def api_regime():
    try:
        regime = detect_market_regime()
        regime.pop("_df", None)
        return jsonify(regime)
    except Exception:
        log.exception("Regime endpoint error")
        return jsonify({"error": "Internal server error", "regime": "NEUTRAL_CHOPPY", "strategy": {"bias": "NEUTRAL"}}), 500


@app.route("/api/scan-ticker")
def api_scan_ticker():
    ticker, ticker_err = _parse_ticker(request.args.get("ticker"))
    if ticker_err:
        return _error_response(ticker_err, 400)
    account_size, account_err = _parse_account_size(request.args.get("account_size"))
    if account_err:
        return _error_response(account_err, 400)

    try:
        plan = scan_single_ticker(ticker, account_size=float(account_size))
        return jsonify(_clean(plan))
    except Exception:
        log.exception("Scan ticker endpoint error")
        return _error_response("Internal server error", 500)


@app.route("/api/scan")
def api_scan():
    account_size, account_err = _parse_account_size(request.args.get("account_size"))
    if account_err:
        return _error_response(account_err, 400)

    try:
        result = run_daily_scan(account_size=float(account_size))
        return jsonify(_clean(result))
    except Exception:
        log.exception("Full scan endpoint error")
        return _error_response("Internal server error", 500)


def _clean(obj):
    """Recursively strip non-JSON-serialisable objects (DataFrames, Series)."""
    if isinstance(obj, dict):
        return {k: _clean(v) for k, v in obj.items() if k != "_df"}
    if isinstance(obj, list):
        return [_clean(i) for i in obj]
    if isinstance(obj, pd.Series):
        return None
    if isinstance(obj, pd.DataFrame):
        return None
    try:
        json.dumps(obj)
        return obj
    except (TypeError, ValueError):
        return str(obj)


if __name__ == "__main__":
    host = _runtime_host()
    port = _runtime_port()

    print("""
╔══════════════════════════════════════════════════════╗
║         UFGenius — Alpaca Signal Bot                 ║
║         Dashboard running at http://localhost:{port:<5} ║
║                                                      ║
║  NOT FINANCIAL ADVICE — Educational only            ║
╚══════════════════════════════════════════════════════╝
""".format(port=port))
    app.run(host=host, port=port, debug=False)
