# Arbitraging the Information Diffusion Lag
## A Prediction-Market-Anchored Framework for Cross-Sectional Equity Mispricings Around Macroeconomic and Geopolitical Events

**Liran Attar**  
*May 27, 2026*

## Abstract

This paper investigates whether prediction markets — specifically Polymarket — can be used to identify exploitable arbitrage windows in individual equities around major macroeconomic, geopolitical, and corporate events. Recent empirical work demonstrates that prediction markets consistently lead consensus analyst forecasts in pricing macro outcomes [3, 4]. We hypothesize that this informational advantage, combined with the well-documented heterogeneity in firm-level sensitivity to specific macro shocks [5], creates a temporary arbitrage gap. To exploit this gap, we propose a decoupled machine learning empirical pipeline whose core execution branches into two distinct strategies: (I) for events with sufficient historical data, a Machine Learning Engine employing Supervised Binary Classification and Ridge Regression with asset-event specific weights to forecast directional bias and drift magnitude; and (II) for novel events with insufficient history, a Sentiment-Gated Momentum Strategy using Rate-of-Change (ROC) momentum with a trailing ATR stop. Both branches are preceded by a Polymarket-anchored signal intake, an LLM semantic routing layer to identify exposed assets, and are governed by a Portfolio Risk Layer that approves, reduces, rejects, or flags all trade suggestions before capital is committed. We deliberately avoid competing with HFT index arbitrageurs at the aggregate level, targeting instead the slower cross-sectional repricing of individual constituents driven by slow-moving capital. Empirical feasibility, system architecture, and execution logic are presented.

## 1 Introduction

### 1.1 Motivation: The Gap Between Knowing and Pricing

Federal Reserve rate cut decisions are among the most anticipated macroeconomic events in financial markets. Yet even when the probability of a rate cut is near-certain — as reflected in prediction market prices — individual equities within the S&P 500 do not immediately reprice to reflect their fundamental sensitivity to the policy change. This delay is not accidental. It is a structural consequence of how information propagates through modern market microstructure.

When a macro shock occurs, high-frequency trading algorithms and ETF arbitrageurs instantly reprice broad market indices. Da and Shive (2018) demonstrate that this ETF-driven activity creates excessive, non-fundamental comovement among constituent stocks: all S&P 500 members move together by virtue of index inclusion, temporarily overriding their specific sensitivities to the underlying shock [2]. As Boguth et al. (2023) show, this aggregate price pressure subsequently reverses as fundamental information diffuses into individual names [6]. The window between the aggregate adjustment and the cross-sectional correction is the opportunity this paper seeks to exploit.

### 1.2 Why Prediction Markets Enable This Strategy

We are not able to compete with HFT algorithms at the index level. Any attempt to trade the aggregate market reaction to a Fed announcement will arrive too late. However, prediction markets such as Polymarket and Kalshi offer a different kind of advantage: they aggregate dispersed information in real-time and produce a continuous probability estimate of macro outcomes that consistently leads both Bloomberg consensus and analyst revisions [3].

Diercks, Katz, and Wright (2026) demonstrate that Kalshi-implied densities over Federal Reserve decisions significantly outperform institutional forecasts, establishing prediction markets as a high-fidelity leading indicator of macro shifts [3]. Bürgi et al. (2025) document the microstructure of these platforms and note that, while liquidity constraints make them unsuitable for direct algorithmic trading, their implied probabilities carry genuine informational content [4]. The core insight of this paper is straightforward: we use prediction markets not as a venue for trading, but as an oracle. The moment Polymarket’s implied probability of a rate cut crosses a calibrated threshold, we interpret this as confirmation that a pricing gap is opening in the asset cross-section — a gap we can capture before it closes.

### 1.3 Research Question

Can prediction-market probability shocks guide profitable trades in the assets an LLM identifies as economically exposed?

The secondary questions are: Which assets exhibit consistent, non-spurious pre-announcement drift attributable to specific macro or geopolitical sensitivities? Can firm-level features predict the expected direction (Long/Short) of a stock’s anticipatory run-up? Furthermore, can a regularized regression model forecast the magnitude of this pre-announcement return to formulate an optimal execution strategy? And for novel, previously unseen events, can a sentiment-gated momentum strategy replicate this edge?

## 2 Background and Literature Review

### 2.1 Prediction Markets as Leading Macro Indicators

Wolfers and Zitzewitz (2004) established the theoretical foundations for prediction markets as efficient aggregators of dispersed information, demonstrating their superiority over expert surveys and polls [1]. The “wisdom of crowds” mechanism — whereby a large population of bettors with heterogeneous information sets produces a probability estimate more accurate than any individual forecast — is the informational engine this paper leverages. Diercks, Katz, and Wright (2026) extend this framework to modern U.S. macro markets, providing direct evidence that Kalshi-implied probability distributions over CPI and Federal Reserve rate decisions contain significant forecasting power beyond Bloomberg consensus, particularly in the weeks preceding announcements [3].

### 2.2 Index-Level Shock and Non-Fundamental Comovement

Macroeconomic announcements, particularly Federal Reserve rate decisions, initially trigger aggregate market reactions rather than stock-specific price adjustments. Liquidity provision and immediate positioning are primarily executed through index futures and Exchange Traded Funds (ETFs). Da and Shive (2018) document that ETF arbitrage creates significant non-fundamental comovement: when an ETF is traded, its arbitrage mechanism forces constituent stocks to move in lockstep regardless of their individual fundamentals [2].

This aggregate price pressure introduces considerable noise into the cross-section of returns. As Ben-David, Franzoni, and Moussawi (2018) demonstrate, widespread ETF ownership significantly increases the volatility of underlying securities, causing their prices to deviate temporarily from fundamental values during periods of macro-level stress or rapid repricing [7]. In the context of a rate cut, index arbitrage mechanics force cash-rich companies with near-zero rate sensitivity (e.g., Alphabet) and heavily leveraged firms hypersensitive to discount rates to initially move by the identical percentage.

### 2.3 Heterogeneous Rate Sensitivity and the Mispricing Gap

The initial index-level comovement stands in direct contrast to the fundamental realities of individual equities. Ai, Han, Pan, and Xu (2021) provide rigorous empirical evidence that equities possess highly heterogeneous sensitivities to monetary policy announcements, and that portfolios sorted on these “monetary policy announcement premiums” generate significant risk-adjusted returns [5].

Furthermore, a firm’s capital structure inherently dictates its fundamental response to rate cuts. For instance, Ippolito, Ozdagli, and Perez-Orive (2018) show that monetary policy transmission varies significantly across the cross-section of stocks based on factors such as floating-rate debt exposure and financial constraints [8]. The core arbitrage opportunity identified in this framework stems directly from this friction: the divergence between an equity’s uniform, ETF-driven initial return, and its warranted, fundamental-driven return based on its specific rate sensitivity.

### 2.4 Information Diffusion and Slow-Moving Capital

If markets were perfectly frictionless, fundamental arbitrageurs would instantaneously correct this cross-sectional mispricing. However, Boguth, Fisher, Grégoire, and Martineau (2023) show that FOMC announcement returns contain a significant aggregate noise component, and that a meaningful reversal occurs slowly as information is fully digested [6].

The persistence of this arbitrage gap can be explained by the concept of “slow-moving capital” (Duffie, 2010), where institutional frictions, mandate constraints, and periodic rebalancing schedules prevent professional investors from absorbing mispricing instantaneously [9]. Retail investors further contribute to this lag. The time elapsed between the noise-driven aggregate move and the capital-intensive fundamental correction defines the exact holding window our model seeks to exploit.

### 2.5 Persistence of the Gap in Modern Market Microstructure

A natural critique of this proposed arbitrage is whether modern algorithmic and high-frequency trading (HFT) have rendered such inefficiencies obsolete by 2026. However, the prevalence of algorithmic trading primarily accelerates the aggregate index-level response via ETF and futures arbitrage, actively contributing to the non-fundamental comovement. HFTs propagate the initial macro shock uniformly across constituents, exacerbating the cross-sectional mispricing rather than correcting it.

The correction of this mispricing requires fundamental, firm-specific repricing, which is executed by institutional actors constrained by what Duffie termed “slow-moving capital” [9]. Even in highly automated contemporary markets, institutional fundamental execution is bottlenecked by mandate constraints, risk-model recalculations, and the necessity to minimize market impact through TWAP or VWAP execution schedules over several hours. This asynchronous processing preserves the temporal window required for our strategy.

## 3 Empirical Feasibility Study: Proof of Concept

Prior to developing the full machine learning pipeline, a preliminary empirical feasibility study was conducted to assess whether structural macro signals contain an observable relationship with cross-sectional equity returns prior to an event. This study analyzed the unrefined pre-announcement drift of a raw, unfiltered set of S&P 500 equities across multiple macro event types.

Despite the introduction of significant noise — resulting from a mixed event universe (not exclusively rate cuts) and the absence of sector controls — a clear directional relationship was observed. In cases where the entry signal indicated high macro conviction, the subsequent positions demonstrated a measurable alignment between the anticipated direction and the actual equity trajectory in the days preceding the announcement.

While the limitations of this preliminary test are explicitly acknowledged (specifically the small sample of confirmed signal-to-return pairs preventing broad statistical inference), the findings empirically validate the core hypothesis: the pre-announcement run-up is an actionable phenomenon. These results serve as the direct empirical motivation for developing the highly structured, machine-learning-driven pipeline detailed in the following sections.

## 4 Trading Universe

A key design principle of this framework is that the traded instrument is not fixed in advance. The system is asset-agnostic by design: the LLM mapping layer determines which tradable asset is economically relevant to each specific Polymarket event. This creates a dynamic, event-driven universe that may span:

- Equities and equity ETFs
- Bonds and interest rate instruments
- Cryptocurrency
- Foreign Exchange (FX)
- Commodities
- Sector Baskets

The core premise is that Polymarket signals a probability change in a macro or geopolitical outcome, and the LLM determines which asset class should fundamentally care about that outcome. This design decouples signal generation from asset class selection, enabling the strategy to operate across any liquid market.

## 5 System Architecture and Decision Flow

The system operates as a sequential pipeline. At a high level, the flow is:

1. Signal Intake: Polymarket probabilities are ingested per-minute and filtered by tag ontology and duration bounds $D_{\mathrm{lo}} \leq D \leq D_{\mathrm{hi}}$.
2. Threshold Gate: If the implied probability has not crossed the calibrated threshold θ, we wait until it does, and if it doesn’t we discard it. If it has, the event is forwarded to the LLM.
3. LLM Semantic Router: A locally-hosted LLM maps the triggered event to a set of economically exposed assets.
4. Execution Branch: Depending on the availability of historical data for the event– asset pair, the system routes to either the ML Engine (known events) or the Sentiment-Gated Momentum Strategy (novel events). Reinforcement learning is used exclusively at this layer to improve the LLM router over time.
5. Portfolio Risk Layer: All trade suggestions pass through a risk engine that approves, reduces, rejects, paper-trades, or flags for human review before any capital is committed.

```text
Stage I: Polymarket Signal Intake
  └── Has the threshold θ been crossed?
      ├── No  → Discard
      └── Yes → Stage II: LLM Router f_LLM(M) → {T₁, …, Tₙ}
                   └── Sufficient historical data?
                       ├── Yes → Stage III-A: ML Engine
                       └── No  → Stage III-B: Momentum Strategy
                                      └── Stage IV: Portfolio Risk Layer
                                             └── Execution via IB API

```

## 6 Temporal Framework and Polymarket Anchoring

To ensure complete synchronization between historical training and live execution, our temporal framework is anchored entirely to prediction market dynamics rather than static calendar days. We define the following native event timeline:

- $T_0$: The exact timestamp of the event’s inception/announcement on Polymarket.
- $T_e$: The official closure or resolution time of the event on the platform.
- $T_s$: The observation time window for analyzing the asset’s price trajectory. In the backtesting phase, $T_s$ serves as an optimizable parameter.
- $T_\theta$: The precise timestamp when the Polymarket implied probability of an outcome crosses the predefined confidence threshold θ.

## 7 Stage I: Signal Intake and Ontological Filtering

Not all Polymarket events carry actionable financial signals. The raw universe of events E is systematically filtered using an empirically derived tag ontology and a bounded duration window. We define a strict subset of event tags S (e.g., macro-indicators, equities, geopolitics) designed to isolate events with a high probability of generating cross-sectional repricing.

Additionally, we define the event’s duration $D_e = T_e - T_0$. The event is only retained if its duration falls within an optimizable bounded window $D_{\mathrm{lo}} \leq D_e \leq D_{\mathrm{hi}}$. Events that are too short do not allow sufficient time for the slow-moving capital drift to materialize, while events that are too long introduce excessive idiosyncratic noise. An event e ∈ E passes this gate if and only if:

$$
\bigl(\mathrm{Tags}(e) \cap S \neq \varnothing\bigr)
\;\land\;
D_{\mathrm{lo}} \leq D_e \leq D_{\mathrm{hi}}
\tag{1}
$$

The event is subsequently monitored on a per-minute basis. When the Polymarket-implied probability crosses the calibrated threshold θ, the event is forwarded to the LLM Semantic Router (Stage II).

## 8 Stage II: LLM Semantic Router

Once a threshold-crossing event is identified, the system must determine which specific assets possess fundamental exposure to the shock. We formalize a locally-hosted Large Language Model (LLM) as a deterministic semantic mapping function. Given a market descriptor M (title, category, tags), the LLM outputs a closed-set vector of relevant standard tickers or asset identifiers:

$$
f_{\mathrm{LLM}}(M) \rightarrow \{T_1, T_2, \ldots, T_n\}
\tag{2}
$$

The LLM generates zero conversational text and makes zero trading decisions. It functions purely as a clustering engine, eliminating the manual overhead of mapping qualitative geopolitical or macro events to quantitative assets. A Reinforcement Learning (RL) feedback loop is used exclusively at this layer to improve router quality over time: the LLM receives a positive reward if the identified assets subsequently exhibit elevated realized volatility and event-driven correlation around $T_s$, confirming that the mapped asset did in fact move due to the event.

Following LLM mapping, the system evaluates whether sufficient historical event– asset pairs exist for the current event category and asset. If sufficient data is available, the trade is routed to the ML Engine (Stage III-A). If the event is novel or historical data is sparse, the trade is routed to the Sentiment-Gated Momentum Strategy (Stage III-B).

## 9 Stage III-A: ML Execution Engine — Known Events

### 9.1 Feature Construction

For each asset i mapped by the LLM under event category c, the ML engine constructs the following feature vector $X_{i,c}$:

- Year-to-Date (YTD) price change of asset i
- Sector trend over the past 1 month
- S&P 500 trend over the past 2 weeks
- Asset i trend over the past 2 weeks

### 9.2 Asset-Event Specific Weights

To prevent cross-contamination of signals, each model learns Asset-Event Specific Weights ($W_{i,c}$). The feature mapping for asset i is completely isolated per event category c. For instance, the learned weights for Alphabet Inc. (GOOGL) reacting to an “Earnings” event are entirely distinct and independent from GOOGL’s weights reacting to a “Federal Reserve Rate Cut” or a “Geopolitical” event.

### 9.3 Model I: Supervised Binary Classification (SBC)

The first model predicts the directional bias of the anticipated drift:

- Target Y : Did asset i spend the majority of the interval [$T_0$, $T_e$] above its opening value at $T_0$?
- Output: $\hat{Y}$ ∈ {−1, +1}, where −1 denotes a SHORT bias and +1 denotes a LONG bias.

### 9.4 Model II: Ridge Regression

The second model forecasts the magnitude of the anticipated drift:

- Feature set: Identical to the classification model, using asset-event specific weights $W_{i,c}$.
- Target Y : Maximum percentage change during [$T_0$, $T_e$], signed consistent with the majority direction from Model I.
- Output: $\hat{y}_{\mathrm{mag}}$, the predicted peak drift magnitude.

### 9.5 Arbitrage Gap and Entry Logic

At $T_\theta$, the system calculates the already-realized drift ($r_{\mathrm{current}}$). The remaining unpriced edge, or “Arbitrage Gap” ($G_i$), is:

$$
G_i = \hat{y}_{\mathrm{mag}} - \left|r_{\mathrm{current}}\right|
\tag{3}
$$

If $G_i$ ≤ 0, the asset is considered fully priced and the trade is aborted. If $G_i$ > 0, an exploitable gap exists and the suggestion is forwarded to the Portfolio Risk Layer. The combined output passed to the Portfolio Layer is:

$$
\hat{Y} \in \{-1,+1\} + \hat{y}_{\mathrm{mag}}
\;\longrightarrow\; \text{Trade Suggestion}
$$

## 10 Stage III-B: Sentiment-Gated Momentum Strategy — Novel Events

For events where insufficient historical data exists to train asset-event specific models, the system employs a sentiment-gated momentum strategy. This approach captures the pre-announcement drift without requiring a trained ML model, using real-time price dynamics and external sentiment as a filter.

### 10.1 Sentiment Gate

Before entering any position, the system scans fresh external context from a set of pre-agreed, trusted financial news sources (3 sites × 3 recent articles each). Positive sentiment corroborating the Polymarket signal direction is required as a precondition for entry. This gate prevents momentum entries in the absence of fundamental narrative support.

### 10.2 Definitions

Let the following quantities be defined:

- $P_t$: current asset price at time $t$
- $n$: lookback window in bars (e.g., $n = 14$)
- $\mathrm{ATR}_n$: Average True Range over the lookback window $n$
- $H_n$: highest price observed over the lookback window $n$
- $k$: volatility multiplier (tunable hyperparameter)

### 10.3 Formulae

The trailing stop level at time t is defined as:

$$
S_t = H_n - k \cdot \mathrm{ATR}_n
\tag{4}
$$

The Rate-of-Change (ROC) momentum indicator is defined as:

$$
M_t = \frac{P_t - P_{t-n}}{P_{t-n}}
\tag{5}
$$

### 10.4 Exit Conditions

A position entered under this strategy is exited when either of the following conditions is met:

$$
P_t \leq S_t \quad \text{(trailing stop triggered)}
\qquad \text{or} \qquad
M_t \leq 0 \quad \text{(momentum reversal)}
\tag{6}
$$

The trailing ATR stop captures upside drift while protecting against reversals, and the ROC condition provides an emotion-free exit when the momentum signal dissipates.

The resulting trade suggestion is forwarded to the Portfolio Risk Layer.

## 11 Stage IV: Portfolio Risk Layer

The Portfolio Risk Layer is the final intelligence checkpoint before capital reaches the market. It integrates live state from multiple data streams — open positions, historical trades, model outputs, and portfolio exposure summaries stored in a PostgreSQL database — with per-minute Polymarket probability feeds and Interactive Brokers (IB) market data.

### 11.1 Risk Engine

Before any trade is executed, the risk engine evaluates the trade suggestion across four dimensions:

- Capital: Position size is scaled by model confidence, available liquidity, and realized volatility.
- Position Size: Size is reduced when portfolio risk or cross-asset correlation is elevated.
- Exposure Caps: Hard limits are enforced at the asset level, sector level, event-type level, and strategy channel level.
- Kill Switch: The system halts all new position-taking if daily loss, total drawdown, or consecutive loss streaks breach predefined thresholds.

### 11.2 Decision Output

The Portfolio Risk Layer produces one of five outcomes for each trade suggestion:

- Approve: Execute at full model-specified size.
- Reduce: Execute at a scaled-down size due to risk or correlation constraints.
- Reject: Discard the trade; an exposure cap has been breached.
- Paper-Trade: Log the trade for evaluation without committing real capital; used when model confidence is insufficient for live deployment.
- Human Review: Flag the trade for manual inspection when confidence signals are conflicting or ambiguous.

## 12 Hyperparameter Optimization

All tunable parameters are optimized via walk-forward cross-validation to prevent look-ahead bias:

| Parameter | Description | Function |
|---|---|---|
| $S$ | Target Tag Ontology | Filters events by empirically verified categories |
| $D_{\mathrm{lo}}, D_{\mathrm{hi}}$ | Duration Bounds | Constrains event length to eliminate noise and illiquidity |
| $T_s$ | Observation Window | Time window to evaluate asset price trajectory |
| $\theta$ | Oracle Trigger Threshold | Sets confidence required to forward event to LLM |
| $\sigma_{\min}$ | LLM RL Reward Threshold | Minimum volatility for positive router reward |
| $n$ | Momentum Lookback | Lookback window for ROC and ATR computation |
| $k$ | ATR Multiplier | Scales trailing stop distance from the recent high |

*Table 1. Hyperparameters and their roles in the pipeline.*

## 13 Results and Performance Evaluation

### 13.1 Performance Metrics

Strategy performance will be evaluated against the following metrics:

- Total Return: Net strategy return after transaction costs and rejected trades.
- Sharpe Ratio: Risk-adjusted return compared to simple buy-and-hold and momentum baselines.
- Maximum Drawdown: Worst peak-to-trough loss over the full backtest period.
- Hit Rate: Share of trades with correct direction and positive P&L.
- Classification Precision/Recall: Direction accuracy for the Long/Short prediction from Model I.
- Regression MAE/RMSE: Accuracy of the predicted percentage move from Model II.

### 13.2 Baselines

All results will be benchmarked against: (i) buy-and-hold of the mapped asset; (ii) the relevant asset-class benchmark; (iii) simple momentum without the Polymarket signal; and (iv) a no-Polymarket-signal version of the same ML model.

### 13.3 Expected Challenges

1. Timing Leakage: Event time $T_0$ must be strictly defined before asset moves are observed to avoid look-ahead contamination.
2. Event Mapping Noise: The LLM asset mapping may miss indirect exposure channels or generate spurious tickers.
3. Sparse Samples: Macro shocks are rare events, making overfitting a material risk for the ML engine.
4. Market Frictions: Slippage, transaction costs, and delayed execution can erode or eliminate the predicted edge.
5. Prediction-Market Noise: Thin liquidity or temporary crowd bias can produce false threshold crossings.

This section is reserved for empirical results following the completion of the full backtesting phase.

## References

- [1] Wolfers, J., & Zitzewitz, E. (2004). Prediction Markets. Journal of Economic Perspectives, 18(2), 107–126.
- [2] Da, Z., & Shive, S. (2018). Exchange Traded Funds and Asset Return Comovement. European Financial Management, 24(1), 136–168.
- [3] Diercks, A., Katz, M., & Wright, J. (2026). Kalshi and the Rise of Macro Markets. Federal Reserve Board FEDS Working Paper No. 2026-010.
- [4] Bürgi, C., et al. (2025). Economics of the Kalshi Prediction Market. CEPR Discussion Papers.
- [5] Ai, H., Han, L., Pan, X., & Xu, L. (2021). The Cross Section of the Monetary Policy Announcement Premium. Journal of Financial Economics, 142(2), 705–728.
- [6] Boguth, O., Fisher, M., Grégoire, V., & Martineau, C. (2023). Noisy FOMC Returns? Information, Price Pressure, and Post-Announcement Reversals. SSRN Working Paper 4131740.
- [7] Ben-David, I., Franzoni, F., & Moussawi, R. (2018). Do ETFs Increase Volatility?. The Journal of Finance, 73(6), 2529–2583.
- [8] Ippolito, F., Ozdagli, A. K., & Perez-Orive, A. (2018). Floating Rate Loans and Credit Risk: Monetary Policy Transmission in the Cross-Section of Stocks. Journal of Financial Economics, 129(3), 435–453.
- [9] Duffie, D. (2010). Presidential Address: Asset Price Dynamics with Slow-Moving Capital. The Journal of Finance, 65(4), 1237–1267.
