# Patterns from Winning Financial Kaggle Solutions — Notes for `trip_ema` / `WeeklyStockSignals`

**Source writeups studied:**
1. Optiver Trading at the Close — `hyd` 1st place
2. Jane Street Real-Time Market Data Forecasting — Evgeniia Grigoreva, 8th place (gold)
3. Ubiquant Market Prediction — `hyd` 3rd place (5-seeds transformer ensemble)
4. Ubiquant Market Prediction — Davide Stenner 2nd place (robust CV + LightGBM)
5. Ubiquant Market Prediction — `k-i-y` 1st place (betting strategy)
6. Ubiquant Market Prediction — Kaggle Doer 8th place
7. Jane Street Market Prediction — Yirun 1st place (Supervised Autoencoder + MLP)

**Why these are relevant:** every one of these competitions is structurally close to what you're building: predict a forward-looking signal from a panel of features for a universe of instruments, scored on something risk-aware, evaluated on time-disjoint test data. The patterns that win across all seven are remarkably consistent — and most of them are absent from the average tutorial pipeline.

---

## The 12 patterns that show up across multiple writeups

These are the cross-cutting ideas. Each section is the pattern, who used it, why it works, and how it maps to `trip_ema`.

---

### Pattern 1 — Purged K-Fold with embargo, not random CV

**Used by:** Davide Stenner (Ubiquant), every Jane Street top-10, Optiver hyd

**The idea:** When your training data and validation data are temporally adjacent, a feature that uses a 20-day rolling stat will leak target information into validation. Random K-fold on time-series produces overoptimistic CV scores that have zero relationship to leaderboard performance.

**The fix:**
- Sort by time
- For fold *i*, training = everything outside a contiguous time block
- **Purge** training samples within `gap` periods of the validation block (those rows' features overlap with validation targets)
- **Embargo** an additional `gap` periods after validation before resuming training (validation targets leak into training features)

**For `trip_ema`:** You have ~20 years × 52 weeks of NSE data per ticker. Use Purged K-Fold with `gap = max(rolling_window_size, target_horizon)`. For a 4-week-ahead target with 20-week rolling features, `gap` is at least 20 weeks. Probably more like 25 weeks to be safe.

```python
def purged_kfold_split(n_samples, n_splits, gap):
    """Returns (train_idx, val_idx) tuples with purge+embargo."""
    fold_size = n_samples // n_splits
    for k in range(n_splits):
        val_start = k * fold_size
        val_end = val_start + fold_size
        # Training = everything except [val_start - gap, val_end + gap]
        train_idx = list(range(0, max(0, val_start - gap))) + \
                    list(range(min(n_samples, val_end + gap), n_samples))
        val_idx = list(range(val_start, val_end))
        yield train_idx, val_idx
```

**This single change is probably worth more than any model architecture upgrade.** Stenner explicitly said this is what put him 2nd while many ensemble-heavy submissions tanked on private LB.

---

### Pattern 2 — Cross-sectional ranking, not absolute prediction

**Used by:** k-i-y (Ubiquant 1st), hyd transformer (Ubiquant 3rd), all top-10 Optiver

**The idea:** Predicting "stock X will return 0.3% this week" is hard and noisy. Predicting "stock X will rank in the top 20% of returns this week within the NSE universe" is much more learnable, because the cross-sectional structure is more stable than the absolute level.

**Implementation:**
- For each time bucket, normalize features cross-sectionally across all tickers (z-score within bucket)
- Train with a ranking loss (e.g., pairwise rank, listwise rank) OR with regression but evaluate ranking metrics
- At inference, only the rank within the universe matters for allocation

**For `trip_ema`:** This is a major shift in how you think about the problem. Today you generate per-ticker signals in isolation. Cross-sectional thinking means: at each weekly bar, rank all NSE tickers by signal strength, and allocate to the top quantile. The triple-EMA crossover becomes a *feature*, not the final decision.

```python
# At each weekly bucket, normalize per-ticker features cross-sectionally
df = df.with_columns([
    ((pl.col(c) - pl.col(c).mean().over("week_id")) /
     pl.col(c).std().over("week_id")).alias(f"{c}_xs_z")
    for c in feature_cols
])
```

**Practical payoff:** removes a huge amount of regime drift. Cross-sectional rank is invariant to whether the whole market is up 5% or down 5% that week.

---

### Pattern 3 — Target neutralization against confounders

**Used by:** k-i-y (Ubiquant 1st)

**The idea:** Your target (forward return) is contaminated by factors you don't actually want to predict — market beta, sector exposure, size effect. If your model just learns "small-cap stocks have higher vol → higher absolute return → predict big numbers for small caps," you're not capturing alpha, you're capturing factor exposure.

**The fix:** Before training, regress the target on a few known confounders (market return, sector dummies, log market cap) and use the **residual** as the actual training target.

```python
import statsmodels.api as sm

confounders = df[["market_return", "log_mcap", "sector_dummy_1", ...]]
y_neutral = df["forward_return"] - sm.OLS(df["forward_return"], confounders).fit().predict()
```

**For `trip_ema`:** Build a "Nifty 500 weekly return" series, regress each ticker's weekly forward return against it, and train on the residual. Your model will learn idiosyncratic alpha instead of leveraged beta.

---

### Pattern 4 — Supervised Autoencoder with auxiliary heads

**Used by:** Yirun (Jane Street 1st) — this is the iconic one

**The architecture:**

```
Input features (130 anonymized)
    │
    ▼
Encoder (Linear → BN → SiLU → Dropout)
    │
    ▼
Bottleneck embedding (~64 dims)
    ├──► Decoder → reconstruction loss (MSE)
    ├──► MLP head 1 → primary target loss (BCE for direction)
    └──► MLP head 2 → auxiliary target loss (regression on continuous target)

Final prediction = MLP head fed (bottleneck embedding ⊕ original features)
```

**Why it works:**
- The reconstruction loss forces the bottleneck to capture **what the data actually looks like**, not just what predicts the target. This regularizes against overfitting to label noise.
- The auxiliary heads share gradient signal — even when the primary target is noisy, the secondary target stabilizes learning.
- Concatenating bottleneck embedding with raw features in the final head means the model can use both denoised representations AND raw signal.

**For `trip_ema`:** This is the single most transferable architecture from this set. Your inputs (price/volume sequence ending at the signal candle + computed indicators) go into an encoder. Bottleneck reconstructs the input, predicts week-ahead direction (BCE), and predicts week-ahead return magnitude (Huber). Final head uses bottleneck + raw features.

**Watch out:** Yirun explicitly noted that "balanced noise augmentation" matters — add Gaussian noise to inputs during training. Too much noise hurts; tune via CV.

---

### Pattern 5 — Multi-seed ensemble averaging

**Used by:** hyd (Ubiquant 3rd, literally named "5 seeds ensemble transformer"), Stenner, Yirun

**The idea:** Train the same model 5 times with different random seeds. Average the predictions. That's it.

**Why it works:** Financial data has a high noise floor. A single seed's solution sits at a particular local minimum that happens to favor certain spurious patterns. Averaging across seeds cancels seed-specific overfitting and the variance reduction is substantial — typically 10-20% improvement on CV/LB without any architecture change.

**For `trip_ema`:** Cheapest win in the entire document. After your model works, train it 5 times with seeds [42, 123, 456, 789, 1024] and average. Add this as a `Config.n_seeds` parameter.

```python
preds = []
for seed in config.seeds:
    torch.manual_seed(seed)
    np.random.seed(seed)
    model = train_model(seed=seed)
    preds.append(model.predict(X_test))
final_pred = np.mean(preds, axis=0)
```

---

### Pattern 6 — Online learning / incremental retraining at inference

**Used by:** Optiver hyd (1st), several Jane Street top-10

**The idea:** The world changes between training data and test data. Markets drift. A model frozen at training time degrades on out-of-sample data.

**Optiver hyd's specific trick:** At inference time, every N days, retrain a small "correction model" on the most recent data while keeping the base model frozen. The correction model learns "how is the world different now than during training?" and its output is added (with a small weight) to the base model's output.

**For `trip_ema`:** Less important for weekly cadence than for tick data, but the pattern still applies. Idea: every quarter, retrain a residual-correction LGB on the last 2 quarters of out-of-sample errors. Add `correction_weight * correction_pred` to the base model output. This is exactly what minglv didn't do in the Hull notebook — and probably should have.

This dovetails with your existing `ComputeScheduler` design — if you can get that mystery resolved, the natural extension is "compute scheduler retrains the correction model weekly."

---

### Pattern 7 — Zero-sum / synthetic-index decomposition

**Used by:** Optiver hyd

**The idea:** In Optiver, the target was `stock_return - index_return` over a future window. The index is itself a weighted combination of all stocks' returns. So if you know the weights, you can decompose:

```
target_i = stock_return_i - (w_1 * stock_return_1 + w_2 * stock_return_2 + ... + w_n * stock_return_n)
```

This means **predictions across all stocks must sum to zero** (weighted by index weights). hyd's submission included a final post-processing step that subtracted the weighted mean of predictions, enforcing this constraint.

**For `trip_ema`:** If you ever build a relative-return model ("NSE ticker X vs Nifty"), this constraint is free Bayesian regularization. The math:

```python
# After model predicts raw forward returns for all tickers in universe
raw_preds = model.predict(X_universe)
weights = market_cap_weights  # or equal weights
weighted_mean = np.sum(weights * raw_preds) / np.sum(weights)
adjusted_preds = raw_preds - weighted_mean  # now sums to zero
```

Surprisingly effective — moved hyd from middle-pack to 1st in the public-to-private shakeup.

---

### Pattern 8 — Stock/ticker embeddings

**Used by:** k-i-y (Ubiquant 1st), Ubiquant transformer (3rd), Kaggle Doer (8th)

**The idea:** Don't treat `stock_id` as a categorical feature to one-hot encode. Learn a dense embedding for each ticker (typically 8-32 dims) as part of the model.

**Why it works:** Stocks share latent characteristics (sector, size, liquidity, beta). A learned embedding captures these without you having to engineer them explicitly. Stocks that "behave similarly" end up with similar embeddings, and the model can generalize across them.

```python
class TickerEmbeddingModel(nn.Module):
    def __init__(self, n_tickers, n_features, emb_dim=16):
        super().__init__()
        self.ticker_emb = nn.Embedding(n_tickers, emb_dim)
        self.mlp = nn.Sequential(
            nn.Linear(n_features + emb_dim, 128),
            nn.SiLU(),
            nn.Dropout(0.3),
            nn.Linear(128, 1),
        )
    def forward(self, features, ticker_ids):
        emb = self.ticker_emb(ticker_ids)
        return self.mlp(torch.cat([features, emb], dim=-1))
```

**For `trip_ema`:** You have thousands of NSE tickers. A 16-dim embedding per ticker is 16,000 params for the entire NSE — trivially cheap. Use this *anywhere* you train a cross-sectional model. The embedding will cluster tickers by behavior in ways that surface useful structure (you can later inspect with t-SNE).

---

### Pattern 9 — Transformer over the time dimension, not over tickers

**Used by:** hyd transformer (Ubiquant 3rd)

**The idea:** Standard tabular transformer uses self-attention across features. Financial transformer uses self-attention across time steps for the same ticker — making it a sequence model.

**Architecture (Ubiquant hyd):**
```
[B, T, F]  # batch, time, features
   │
Linear projection to d_model
   │
Add positional encoding
   │
N × Transformer Encoder Blocks (typically N=3-6)
   │
Take last time step's embedding (or pool over time)
   │
MLP head → prediction
```

**Why it works:** Captures long-range temporal dependencies that LSTMs struggle with. Attention learns which past time steps matter for the current prediction — different time steps matter in different regimes.

**For `trip_ema`:** Your weekly cadence and ~1000 weekly bars per ticker is on the small side for transformers, but you could try sequence length 32-52 (i.e., last 8-12 months). Use d_model=64, 3 layers. Train with the SharpeLoss from the Hull doc. Concatenate with ticker embedding (Pattern 8). This is the "v3" upgrade after you have the basic stack working.

---

### Pattern 10 — Heavy regularization beats clever architecture

**Used by:** Stenner (Ubiquant 2nd), explicitly

**Stenner's parameters for LightGBM on Ubiquant:**
- `feature_fraction = 0.4` (use only 40% of features per tree)
- `bagging_fraction = 0.7` with `bagging_freq = 1`
- `min_data_in_leaf = 2000` (very high — forces robust splits)
- `lambda_l1 = 1.0`, `lambda_l2 = 1.0`
- `max_depth = 6` (shallow)
- `num_leaves = 31` (default-ish)
- Early stopping with patience 50

**The principle:** In financial data, the signal-to-noise ratio is so low that any LGB hyperparameter that *increases capacity* will overfit. The right move is the opposite of what works on Kaggle's tabular playground competitions — push every regularization knob aggressively.

**For `trip_ema`:** When you train LGB/XGB on your features, **start with these parameters, not the defaults.** Your default `n_estimators=500, max_depth=8` from the Hull notebook is too aggressive. Stenner's parameters are what survive forward-looking validation.

---

### Pattern 11 — Don't optimize MSE, optimize the scoring metric directly

**Used by:** all top-10 in Optiver (weighted Pearson r), Jane Street (Sharpe-like), Hull (modified Sharpe)

**The idea:** Already covered in your Hull notes — but worth reiterating because **every single one of these competitions has a winner who customized the loss.** Yirun used a weighted BCE; Optiver winners used MAE with per-sample weights matching the scoring weight; hyd's transformer used MSE but only after target neutralization (Pattern 3).

**For `trip_ema`:** Your training objective and your backtest objective must be the same function. If you backtest Sharpe, train on a Sharpe-like loss. If you backtest hit rate, train on BCE.

---

### Pattern 12 — Memory & speed engineering as a first-class concern

**Used by:** Evgeniia Grigoreva (Jane Street 8th — explicitly noted), Optiver hyd, Jane Street top-10

**The patterns:**
- **Polars over pandas** everywhere — Grigoreva noted Pandas training "exceeded 20+ hours, forcing early termination" before switching to Polars
- **`reduce_mem_usage()`** function to downcast int64→int32, float64→float32 — saves 50-70% RAM
- **Parquet** instead of CSV — 5-10x faster I/O
- **Chunked training** for datasets that don't fit in RAM
- **16ms inference budget** in Jane Street — forced careful model size choices

**For `trip_ema`:** You're already on PostgreSQL with millions of NSE daily bars. Your read path likely uses pandas. Switch to Polars-via-`connectorx` or Polars reading from a Parquet dump. Add a `reduce_mem_usage` pass on every DataFrame. These aren't optional optimizations — they determine whether your training loop completes in 30 minutes or 30 hours, which determines whether you can run 5-seed ensembles (Pattern 5).

---

## Patterns that are tempting but didn't work

Worth noting because you'll be tempted to try them:

### LSTMs underperformed feedforward NNs almost everywhere

Grigoreva tried LSTM, abandoned it: *"Model struggled to generalize due to high noise and non-stationarity in financial data. Theoretical insight: LSTMs may underperform in environments with low signal-to-noise ratios."* Multiple Ubiquant top finishers said the same thing. **MLPs with rich engineered features beat LSTMs consistently.**

### Heavy feature engineering with hundreds of features

Beyond ~50 well-chosen features, more features made things worse for tree models. Stenner's winning LGB used `feature_fraction=0.4` for a reason — too many features creates spurious correlations.

### Single-model architectures vs ensembles

No top solution used a single model. Even Yirun's "Supervised Autoencoder + MLP" was a 3-seed average. Single models are useful as components; final submissions are always blends.

### LSTM/GRU with raw price input

Doesn't work. Always pre-process to returns/log-returns before any sequence model. Raw prices are non-stationary and the model never learns anything stable.

---

## Concrete v1 → v2 → v3 plan for `trip_ema`

### v1 — Quick wins (1-2 days when you resume)
Things you can do without changing the architecture meaningfully:

1. **Switch to Polars** in the feature path (Pattern 12)
2. **Add `reduce_mem_usage`** after every DataFrame creation (Pattern 12)
3. **Replace random K-fold with Purged K-Fold + embargo** (Pattern 1) — this alone will change your CV scores meaningfully
4. **Apply Stenner's regularization parameters** to your existing LGB (Pattern 10)
5. **5-seed averaging** of your current model (Pattern 5)

### v2 — Cross-sectional reframing (1-2 weeks)
Restructure the problem statement:

6. **Cross-sectional normalization** within each weekly bucket (Pattern 2)
7. **Target neutralization** against Nifty 500 returns + sector (Pattern 3)
8. **Add ticker embeddings** to whatever model you're using (Pattern 8)
9. **Zero-sum post-processing** if predicting relative returns (Pattern 7)

### v3 — Architecture upgrade (2-4 weeks)
Now the interesting stuff:

10. **Supervised Autoencoder** with multi-head loss (Pattern 4) — the Hull doc's SharpeLoss becomes one of three heads
11. **Custom loss = backtest Sharpe** (Pattern 11, also in Hull doc)
12. **Optional v3.5: Transformer over time dimension** with ticker embedding (Pattern 9)

### v4 — Production hardening
13. **Quarterly online correction model** retraining (Pattern 6) — wire into your `ComputeScheduler`

---

## Architecture sketch combining the best of all writeups

If you were to build the maximum-borrowed model for `trip_ema`, it would look like:

```
For each weekly bar:
    For each ticker:
        Input: 52-week sequence of [price, volume, your 3 EMAs, RSI, ATR]
        Cross-sectionally normalized against universe that week (Pattern 2)

        ↓ Ticker embedding (16-dim, learned) (Pattern 8)
        ↓ 1D-CNN feature extractor at scales [9, 21, 55] (Hull pattern, weighted toward longer)
        ↓ Concatenate: [CNN_features] + [ticker_emb] + [hand-engineered features]

        ↓ Supervised Autoencoder (Pattern 4):
              encoder → bottleneck → decoder (reconstruction loss)
                                  → direction head (BCE on sign of week-ahead return)
                                  → magnitude head (Huber on neutralized return — Pattern 3)
                                  → Sharpe head (custom SharpeLoss from Hull doc, Pattern 11)

        ↓ Final head: bottleneck ⊕ raw → ranking score

Train with:
    - Purged K-Fold, gap=25 weeks (Pattern 1)
    - 5 seeds, average predictions (Pattern 5)
    - Stenner-style regularization on any LGB legs (Pattern 10)
    - Polars throughout (Pattern 12)

At inference:
    - Predict ranking score for all NSE tickers each week
    - Zero-sum adjustment (Pattern 7)
    - Allocate to top 20% quantile
    - Volatility-scale and smooth (Hull doc post-processing)

Quarterly:
    - Train a small LGB correction model on last 2 quarters of OOS errors (Pattern 6)
    - Add correction_weight * correction_pred to base output
```

That's the kitchen-sink version. You don't build this all at once — you build v1, validate, build v2, validate, etc.

---

## Three lessons that don't fit a pattern but matter

1. **Davide Stenner (2nd, Ubiquant) said the difference between him and 1st place was that 1st place "got lucky on regime."** His private LB rank was 1 point behind, his CV was actually better. In low-SNR competitions, the gap between 1st and 10th is mostly noise. **Optimize for being consistently top-20, not for winning.** This applies to your production system: a robust 1.5 Sharpe strategy beats a sometimes-3.0/sometimes-0.5 strategy.

2. **k-i-y (1st, Ubiquant) explicitly framed their solution as "a betting strategy, not a prediction model."** Their writeup is mostly about allocation and Kelly sizing, not features. **The thing that wins is rarely the model — it's what you do with the model's output.** This is the Hull doc's post-processing insight, restated.

3. **Yirun (1st, Jane Street) trained for ~3 weeks on a single GPU.** Not 20 GPUs, not a cluster — one GPU. The advantage was the architecture (supervised AE), not the compute. **Compute is rarely the binding constraint for someone with your skill level. Architecture choice and CV discipline are.**

---

## References

- Optiver Trading at the Close — hyd 1st place: https://www.kaggle.com/competitions/optiver-trading-at-the-close/writeups/hyd-1st-place-solution
- Jane Street Real-Time — Grigoreva 8th: https://www.kaggle.com/competitions/jane-street-real-time-market-data-forecasting/writeups/evgeniia-grigoreva-private-lb-8th-solution
- Ubiquant — hyd 3rd (transformer): https://www.kaggle.com/competitions/ubiquant-market-prediction/writeups/hyd-3rd-place-solution-5-seeds-ensemble-transforme
- Ubiquant — Stenner 2nd (LGB): https://www.kaggle.com/competitions/ubiquant-market-prediction/writeups/davide-stenner-2nd-place-solution-robust-cv-and-lg
- Jane Street Market Prediction — Yirun 1st (Supervised AE): https://www.kaggle.com/competitions/jane-street-market-prediction/writeups/cats-trading-yirun-s-solution-1st-place-training-s
- Ubiquant — k-i-y 1st: https://www.kaggle.com/competitions/ubiquant-market-prediction/writeups/k-i-y-1st-place-solution-our-betting-strategy
- Ubiquant — Kaggle Doer 8th: https://www.kaggle.com/competitions/ubiquant-market-prediction/writeups/kaggle-doer-8th-place-solution

---

**Bottom line:** Pick three patterns from this list. Implement them. Validate they help. Pick three more. Don't try to do all twelve in one go. The v1 list is achievable in a weekend; the v3 list is a multi-month project. The companion Hull Tactical doc gives you the post-processing and SharpeLoss patterns; this doc gives you the training and CV patterns. Together they're a complete blueprint.
