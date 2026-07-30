# PMRA — Falsifying a Trading Hypothesis (and Knowing When to Stop)

> **Resume / portfolio blurb.** Built a prediction-market research system to test whether a web-search LLM could out-forecast Kalshi's prices net of fees, then ran four independent falsification passes — forecasting skill, bias inversion, structural arbitrage, and a 4,610-market behavioral study — that each returned a rigorous negative. Caught a look-ahead data leak that was manufacturing a fake positive, killed a nominally-significant (t=2.6) result after recognizing its payoff skew made the statistic invalid, and correctly chose to deploy no capital. Reached a complete, defensible "no edge" conclusion for $0.70 in API spend and $0 at risk.

---

## The hypothesis, stated to be killable

A general-purpose LLM with live web search might estimate the probability of a prediction-market outcome more accurately than the market's own price — often enough, and by a wide enough margin, to be profitable *after* Kalshi's fees and bid-ask spread. That is the whole thesis, and it is worth testing precisely because it is cheap to test and expensive to assume: if true, it scales; if false, most people discover it with real capital.

I wrote it as a falsifiable claim with an explicit kill condition: **the model must show positive Brier skill (beat a 0.25 coin flip) AND positive net-of-fee P&L, in some category, at a sample size large enough to matter (n ≥ 30).** If it couldn't clear that bar, the hypothesis was dead and no money should move. Everything below is the apparatus built to give that claim an honest chance to fail.

## The harness — instrumented to disprove itself

The system (`src/research/`, ~4,600 lines, 29 test suites) is a full research loop, but the part that matters is the measurement layer. Live Kalshi discovery pulls tradeable binary markets; order books are priced at **realistic far-touch fills** — you buy at the ask VWAP and sell into the bids, never at the mid — with Kalshi's `0.07·p·(1−p)` fee netted into every expected value, so no edge is ever credited that the spread would actually eat. An LLM (Anthropic or OpenAI, per-model) produces a probability per market against resolution-grounded prompts.

The reusable asset is not the forecaster; it's the scorekeeping. Every prediction is written to an append-only SQLite layer (`db.py`, the only module allowed raw SQL) and later joined to the realized outcome, so calibration is measured, not asserted: per-model **Brier and log-loss**, temperature-scaled reliability curves, and an **ROI gauge that subtracts real API credit spend from realized P&L** — the system is built to tell me it is losing. Statistics are **event-clustered** (correlated legs of one underlying event count once), which is exactly the discipline that later dismantled two tempting false positives. Engineering hygiene is real and in service of that trust — enforced module boundaries (HTTP confined to the exchange clients, no business logic in the data layer), full type hints under mypy, ruff, and pre-commit — but it is the means, not the point. The point is that the harness can be believed when it delivers bad news.

## Four independent falsification passes

**(a) Forecasting skill (weather/econ/crypto).** Negative and decisive: **1 win in 13 resolved signals, −$585.85 modeled P&L, Brier 0.305 over 22 resolved pairs — worse than a 0.25 coin flip.** The failure was uniform across categories (econ 0/8, weather 1/5), not a bad patch hiding a good one. The spread cost only ~$7 of that loss; being *wrong* caused the rest.

**(b) Confidence-bias inversion.** The model was overconfident and directionally wrong, which invites the reflex "then just fade it." In-sample, inverting every signal returned +$550. I killed it: the 13 signals span only **5 independent events** (eight of them are thresholds on a *single* jobs report), so the effective n is ~5. An "edge" resting on five correlated observations is an artifact, not a strategy.

**(c) Structural / taker arbitrage.** Dead. Across **1,796 live mutually-exclusive events**, buying every outcome's YES costs a median of **$1.07 for a $1 payout — a ~7% overround — and there were zero executable sell-side arbitrages** net of fees. The mispricing inside the spread belongs to whoever *posts* it, not to a taker.

**(d) Mechanical behavioral edge.** The largest pass: **4,610 resolved markets across 1,901 distinct events**, event-clustered, all P&L at far-touch fills. The classic favorite-longshot bias is unmistakable *gross* — a contract priced at 0.55 mid-life resolves YES ~65% of the time. Net of the overround and fees it evaporates: **selling longshots is significantly negative (−0.058/contract, t = −5.35); buying favorites is not distinguishable from zero (+0.012, t = 1.08) and flips sign depending on entry timing.** A real behavioral bias, fully consumed by the transaction cost.

## The two moments that are the actual deliverable

**I caught a look-ahead leak that was fabricating a positive.** Testing near-dated Politics markets — the one category that looked plausibly inefficient — I realized every candidate market had resolved *after* the model's training cutoff, so the analyzer's web search could read the outcome instead of forecasting it. I checked, and it did: **6 of 6 probed markets cited post-resolution facts** ("announced her resignation on May 22," "confirmed as of July 28"). The contaminated Brier of 0.164 was worthless. Rather than bank a fake win, I discarded it and fell back to the one clean measurement available for free — the market's *own* price, which forecasts these at Brier ~0.10. The category was efficient; the "edge" was a leak.

**I killed a statistically significant result.** In the behavioral study, deep favorites (0.90–0.99) showed net **+0.02/contract at t ≈ 2.6–2.9** — nominally exploitable. I didn't take it. The entire positive rested on **7 losing events (2 near resolution)**; with payoffs of +0.03 on a win and −0.95 on a loss, the distribution is extreme-negative-skew and the Gaussian t-statistic is invalid when the loss tail is that under-sampled. A handful more losses — well inside the confidence interval — zeroes it. And I couldn't verify a $10 order even fills at those thin deep-favorite quotes. A number that clears the significance bar but not the scrutiny bar is not an edge.

## The decision

Across four independent sources of edge — forecasting, bias, structure, and behavior — the market is efficient at retail scale, and every apparent exception is either inside the spread or inside the noise. The correct action was to deploy no capital, and that is the action I took. The full falsification campaign cost **$0.70 in API credits**; the entire project, including the earlier paper-trading phase that generated the calibration dataset, cost **~$6.97, with $0 of capital ever at risk.** The paper harness did its job: it converted a $585 modeled loss into a $7 lesson. That is the process working exactly as designed — a fast, cheap, honest "no."

---

> **If an interviewer asks me to walk through this:** "I hypothesized an LLM could beat prediction-market prices net of fees, and I built the harness to *disprove* it — realistic far-touch fills, append-only prediction-vs-outcome tracking, Brier and ROI instrumentation, event-clustered stats. Four independent tests all came back negative: forecasting Brier 0.305, arbitrage killed by a 7% overround, and a 4,610-market behavioral bias that's real gross but net-negative after costs. The two things I'm proudest of are catching a look-ahead leak that was faking a positive result, and killing a t=2.6 result once I saw the payoff skew made the statistic meaningless. The conclusion was 'the market is efficient, don't trade,' and I reached it for under a dollar with zero capital at risk. The reusable asset is the measurement harness — it's built to tell me when I'm wrong."
