#!/bin/zsh
# Concatenate the auditable source into one text file for external review.
# Output is gitignored (it duplicates the repo). Usage: zsh scripts/make_audit_bundle.sh [out]
set -eu
REPO="$(cd "$(dirname "$0")/.." && pwd)"
OUT="${1:-$REPO/AUDIT_BUNDLE.txt}"
cd "$REPO"

FILES=(
  SYSTEM.md
  docs/RESEARCH_PROTOCOL.md
  docs/AI_REVIEW.md
  config/config.yaml
  config/risk_params.yaml
  config/model_params.yaml
  config/events.yaml
  src/common.py
  src/main.py
  src/backtest.py
  src/data/polygon_options.py
  src/data/ibkr_feed.py
  src/data/data_pipeline.py
  src/signals/feature_engineering.py
  src/signals/labeling.py
  src/signals/lightgbm_model.py
  src/signals/range_model.py
  src/signals/regime_classifier.py
  src/signals/signal_generator.py
  src/execution/broker_base.py
  src/execution/ibkr_broker.py
  src/execution/risk.py
  src/execution/position_manager.py
  src/learning/trainer.py
  src/learning/evaluator.py
  src/learning/anomaly_detector.py
  src/learning/self_corrector.py
  src/research/spreads.py
  src/research/walkforward.py
  src/utils/config.py
  src/utils/logger.py
  src/utils/memory.py
  src/utils/trade_log.py
  src/utils/events.py
  src/utils/alerts.py
  scripts/run_paper_day.sh
  deploy/com.pcnslt.odte-spy-bot.plist
  .github/workflows/daily_retrain.yml
  .github/workflows/tests.yml
  tests/conftest.py
  tests/test_data.py
  tests/test_signals.py
  tests/test_execution.py
  tests/test_learning.py
  tests/test_intelligence.py
  tests/test_integration.py
)

{
  echo "# ODTE-SPY-BOT — FULL CODE AUDIT BUNDLE"
  echo "# Generated: $(date '+%Y-%m-%d %H:%M %Z')  Commit: $(git rev-parse --short HEAD)"
  echo "# Files: ${#FILES[@]}   (line numbers per-file, starting at 1)"
  echo
  for f in "${FILES[@]}"; do
    echo "═══════════════════════════════════════════════════════════════════"
    echo "FILE: $f  ($(wc -l < "$f" | tr -d ' ') lines)"
    echo "═══════════════════════════════════════════════════════════════════"
    nl -ba "$f"
    echo
  done
} > "$OUT"

echo "Wrote $OUT ($(wc -l < "$OUT" | tr -d ' ') lines, $(du -h "$OUT" | cut -f1 | tr -d ' '))"
