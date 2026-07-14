# Hull Tactical Pattern — Notes for `trip_ema` / `WeeklyStockSignals`

**Source:** Analysis of `minglv`'s leading public submission to the Hull Tactical Market Prediction competition (Kaggle, Sep 2025 – Jun 2026). Public LB score 4.571 (modified Sharpe), submitted ~8 months before competition end and never updated.

**Why this matters for `trip_ema`:** Your current pipeline generates **signals** from a triple-EMA Pine port. minglv's notebook is a clean blueprint for the layer *above* signal generation — turning a raw directional signal into a **risk-managed allocation** scored on Sharpe, not on prediction accuracy. That's the gap between a "signal generator" and an "allocation engine," and it's directly transferable.

---

## 1. The headline insight

> **The model is not scored on how accurately it predicts returns. It is scored on the Sharpe ratio of a strategy that uses its predictions.**

Most ML practitioners optimize MSE on `forward_returns` and then bolt allocation logic on top as an afterthought. minglv optimizes the actual scoring objective end-to-end via a custom `SharpeLoss`. This is the single most important transferable idea.

For `trip_ema`: today the signal generation and the (eventual) allocation logic are separate concerns. They shouldn't be. The training objective for any learned component should be a function of *backtested Sharpe of the resulting strategy*, not of signal-prediction accuracy.

---

## 2. Architecture decomposition

minglv's pipeline has **three orthogonal layers**, each addressing a different failure mode:

### Layer 1 — Feature engineering (deterministic, Polars)

- Rolling mean and std at windows `[5, 10, 20]` for every base feature
- EWM mean at spans `[5, 10, 20]`
- A small set of hand-crafted derived features (e.g. `U1 = I2 - I1` differences, ratios like `M11 / mean(I2, I7, I9)`)
- A few interaction terms (`V1 * S1`, `M11 * V1`, `I9 * S1`)
- Null handling: forward-fill then backward-fill

**Stack:** Polars, not pandas. Faster, lazier, fewer footguns on financial time series.

**Transfer to `trip_ema`:**
- You already compute EMA bands (the "triple" part). Add rolling std at the same windows.
- Build interaction features between regime indicators (e.g. EMA-slope × volume, RSI × ATR-percentile).
- Use Polars in the feature layer. Your PostgreSQL → DataFrame path currently uses pandas; Polars is a drop-in for read-heavy pipelines and handles your millions of NSE daily bars better.

### Layer 2 — CNN feature extraction (learned temporal patterns)

- **Three separate 1D-CNNs** for sequence lengths 5, 10, 20 (short / medium / long temporal patterns)
- Per CNN: 1×1 conv feature fusion → two conv blocks (64→128→256 channels, kernel 3, BatchNorm, LeakyReLU, MaxPool) → adaptive avg pool → 3-layer FC head emitting **10 features each**
- Total: 30 learned features added to the deterministic feature set
- Trained with `SharpeLoss`, not MSE — this is the critical detail
- Device-aware (CUDA if available, otherwise CPU)

**Why three sequence lengths:** different market regimes operate on different timescales. Single-window CNNs overfit to whichever scale dominated training data.

**Transfer to `trip_ema`:**
- For a triple-EMA system, the natural sequence lengths are the EMA periods themselves (whatever your three are — likely something like 9/21/55 or similar). Train one small CNN per period on the price/volume sequence ending at the signal candle.
- Output: a small number (5–10) of learned features per sequence length, concatenated to your deterministic signal feature set.
- **Crucial:** train these CNNs against backtested strategy Sharpe, not against next-bar return.

### Layer 3 — Heterogeneous ensemble

Three models, deliberately uncorrelated:

| Model | Weight | Feature set | Role |
|---|---|---|---|
| LightGBM | 0.45 | Full (base + engineered + CNN) | Primary tree learner |
| XGBoost | 0.45 | Full (base + engineered + CNN) | Diversifier vs LightGBM |
| ElasticNet | 0.10 | Base features only | Linear anchor, prevents tree overfitting |

The ElasticNet weight is small (10%) but its job isn't to compete — it's to provide a low-correlation third leg that doesn't share the tree models' failure modes. In market regimes where both LGB and XGB overfit to the same spurious tree splits, the linear model holds the line.

Hyperparameters used: both tree models — `n_estimators=500`, `max_depth=8`, `lr=0.05`. ElasticNet — `alpha=0.01`, `l1_ratio=0.5`. Standard, nothing exotic.

**Transfer to `trip_ema`:**
- If you add a learned overlay, use exactly this three-leg structure. Two correlated nonlinear models + one regularized linear anchor.
- The ElasticNet leg is your insurance against the trees collectively hallucinating a regime that isn't there.

---

## 3. The Sharpe loss (the core pattern)

```python
class SharpeLoss(nn.Module):
    def __init__(self, epsilon=1e-8):
        super().__init__()
        self.epsilon = epsilon

    def forward(self, y_pred, y_true):
        # Treat predictions as position sizes: long when positive, flat when negative
        positions = torch.where(y_pred > 0, y_pred, torch.tensor(0.0, device=y_pred.device))
        # Strategy returns = position × realized return
        strategy_returns = positions * y_true
        # Sharpe = mean / std
        mean_return = torch.mean(strategy_returns)
        std_return = torch.std(strategy_returns)
        sharpe_ratio = mean_return / (std_return + self.epsilon)
        # Negative because we minimize loss but want to maximize Sharpe
        return -sharpe_ratio
```

**What this does that MSE doesn't:**
- Rewards being directionally right when realized vol is high (big payoff)
- Penalizes being right but with high prediction variance (low Sharpe)
- Treats the prediction as a *position*, not a target

**Limitations:**
- Batch Sharpe is noisy with small batches — use batch size ≥ 256 for stable gradients
- No drawdown penalty — pair with max-drawdown regularization in a v2
- The "long when positive, flat when negative" rule encodes a long-only constraint; for `trip_ema`'s short-permitted version, replace with `positions = torch.tanh(y_pred)` or similar bounded function

**For `trip_ema`:** A weekly-signal variant of this loss is the right training objective for any learned component you add. Define `strategy_returns` as the realized week-ahead return when the triple-EMA signal fires, weighted by the model's confidence output.

---

## 4. Post-processing — turning prediction into allocation

This is the bit most people miss. Raw prediction → final position size is a **deterministic pipeline** with tunable parameters:

```
raw_pred = ensemble.predict(features)
signal = clip(raw_pred * signal_multiplier, 0, 2)          # scale and clip
allocation = clip(signal / (volatility * vol_scaling), 0, 2)  # vol-scale
final = smoothing * allocation + (1 - smoothing) * last     # EMA smooth
```

minglv's parameters:
- `signal_multiplier = 500` — scales tiny return predictions into meaningful position sizes
- `vol_window = 20` — rolling std of recent returns
- `min/max signal = 0/2` — long-only, max 2x leverage
- `smoothing_factor = 0.8` — heavy weight on new allocation, but still anchored to last

**Why this matters:** the model output isn't the answer. The post-processing is doing significant work — vol-scaling alone probably explains a large fraction of the Sharpe edge.

**Transfer to `trip_ema`:**
- Your allocation logic should look exactly like this — deterministic, parameterized, testable.
- Vol-scaling should use either realized 20-day vol or your VIX-equivalent if working on indices.
- Smoothing factor prevents allocation whipsaw — your weekly cadence already gives you some smoothing, but a `0.7 × new + 0.3 × last` blend is a useful additional layer.
- Make every parameter in this block a config field — exactly your existing `@dataclass Config` pattern.

---

## 5. Things minglv does that are *not* novel but are *correct*

- **Polars over pandas** for data loading and feature engineering
- **StandardScaler** fit on train only, applied to test (two separate scalers — one for full feature set, one for base features used by ElasticNet)
- **Forward-fill then backward-fill** for nulls — defensible default for financial time series
- **Single train/val split with `validation_size = 0.1`** held out as the most recent 10%, not random — correct for time series
- **Early stopping** on LightGBM (patience 20)
- **`@dataclass Config`** with all hyperparameters in one place — exactly your pattern
- **Inference server pattern** — train once, serve a `predict(row)` function that maintains rolling history state

The last one is interesting: `predict()` mutates a global `history` DataFrame and a `last_allocation` float on each call. This is the streaming inference pattern — Kaggle's competition harness feeds one row at a time, model must maintain its own state. For `trip_ema`'s production service, this is the right shape for the weekly-update endpoint too.

---

## 6. What's deliberately *missing* from minglv's notebook (and worth adding in v2)

- **No cross-validation across time** — only single hold-out. A proper expanding-window walk-forward CV would be more defensible but compute-heavier.
- **No regime detection** — model treats all market states the same. A regime classifier (HMM or simpler) gating the ensemble would help.
- **No drawdown control** in the loss — pure Sharpe ignores tail risk shape.
- **No transaction cost penalty** — the smoothing factor partially addresses this but doesn't model it.
- **Fixed ensemble weights** (0.45 / 0.45 / 0.10) — should ideally be learned or regime-conditional.
- **No uncertainty quantification** — model gives a point estimate, not a distribution. Allocation sizing would benefit from prediction intervals.

These are all the right directions for a v2 — and the right things to add to whatever you build for `trip_ema`.

---

## 7. Direct application to `trip_ema` — concrete plan

When you come back to this in a few days, here's the order of operations:

### Phase A — Reproduce the pattern offline
1. Build a Polars-based feature pipeline on your NSE daily bars: rolling mean/std at your three EMA windows + a few interactions
2. Define a weekly-Sharpe loss function (week-ahead returns × position from signal)
3. Train one CNN per EMA window on the price/volume sequence ending at each signal candle
4. Stack with LGB + XGB + ElasticNet using the same 0.45 / 0.45 / 0.10 weights

### Phase B — Allocation engine
5. Build the deterministic post-processing layer: signal scale → vol-scale → smooth
6. Parameterize as a config class — `signal_multiplier`, `vol_window`, `min/max_signal`, `smoothing_factor`, `vol_scaling`
7. Backtest the full pipeline against your existing signal-only baseline. The learned overlay should improve Sharpe at the cost of some prediction accuracy — that's the right tradeoff.

### Phase C — Production integration
8. Replace global `history` mutation with proper state management in your FastAPI service
9. Wire allocation output into your PostgreSQL signals table as a new column
10. Add allocation columns to the PySide6 GUI state panels

### Phase D — Things to skip until later
- Don't bother with regime detection in v1 — get the basic stack working first
- Don't try to learn the ensemble weights — fixed weights are fine and defensible
- Don't add transaction cost modeling unless you're going live

---

## 8. Code skeleton — Sharpe loss adapted for `trip_ema`

```python
class WeeklySharpeLoss(nn.Module):
    """Sharpe loss for weekly-cadence signals.

    y_pred: model confidence in the signal (continuous, can be unbounded)
    y_true: realized week-ahead return
    long_only: if False, allows short positions via tanh
    """
    def __init__(self, long_only: bool = True, epsilon: float = 1e-8):
        super().__init__()
        self.long_only = long_only
        self.epsilon = epsilon

    def forward(self, y_pred, y_true):
        if self.long_only:
            positions = torch.relu(y_pred)
        else:
            positions = torch.tanh(y_pred)  # bounded [-1, 1]

        strategy_returns = positions * y_true
        mean_return = torch.mean(strategy_returns)
        std_return = torch.std(strategy_returns)

        # Annualize: weekly Sharpe × sqrt(52)
        sharpe = (mean_return / (std_return + self.epsilon)) * (52 ** 0.5)
        return -sharpe
```

Note the `sqrt(52)` — annualization factor for weekly data. minglv's daily-cadence loss didn't annualize because the comparison is relative across submissions; for your own backtesting you want comparable numbers.

---

## 9. Open questions for when you resume

- Does your weekly cadence give enough samples for a CNN to learn anything meaningful? NSE has ~20 years × 52 weeks = ~1000 weekly bars per ticker. Per-ticker CNNs probably underfit; cross-ticker training with ticker embedding might work.
- What's the right `signal_multiplier` analog for your context? minglv's 500 is calibrated to S&P daily returns being small. Weekly NSE returns are larger — multiplier should probably be in the 10–50 range.
- Do you train one model across all NSE tickers, or one model per liquidity tier? Cross-sectional training is more data-efficient but assumes regime homogeneity.

---

## 10. References

- minglv's public notebook: `hull-tactical-prediction-with-cnn-ensemble.ipynb` (local copy)
- Public leaderboard (as of May 26, 2026): minglv #1 at 4.571, bc0102 #2 at 4.084, TeamNari #3 at 3.982, FastScramblers #4 at 3.935
- Competition page: https://www.kaggle.com/competitions/hull-tactical-market-prediction
- Final submission deadline was Dec 15, 2025; competition ends Jun 16, 2026 (forward-eval phase)

---

**Bottom line:** the architecture is reusable, the SharpeLoss insight is the keeper, the post-processing layer is where the actual edge lives. Don't try to copy the whole stack — extract the three layered ideas and apply them to your existing triple-EMA signal generator.
