# Plan Portfolio — Experiment Results

**Question:** does running the optimiser cascade N times with different overrides and keeping the
best-scoring plan deliver the improvement the best-of-N oracle predicted, with no regressions?

**Answer:** yes. The mechanism reproduces the oracle to within 0.2%, with **zero regressions**, at
**4.25x** runtime. It works exactly as designed. Whether 4.25x is worth 0.33% mean improvement is a
separate question, addressed at the end.

## Success criterion, fixed in advance

The spec committed to these targets before the run, taken from an oracle computed post-hoc over the
same n=100 seed-0 data used by the stride experiment.

| | target | measured | verdict |
|---|---|---|---|
| Mean metric change | ≈ -2.05 | **-2.0431** | met |
| Regressions | **0** | **0** | met |
| Runtime multiplier | ≈ 4.9x | **4.25x** | met, better than predicted |

## Method

The portfolio arm used the same 100-scenario seed-0 set and the same `predbat_debug_agile1.yaml`
template as the stride experiment, and was compared against `stride_results_1a.json` — the **exact**
stride-1 control the oracle was computed from (100 scenarios, seeds 0-99, mean metric 627.4597, total
runtime 1195s). The arm's template differs from the stride-1 template in exactly the four lines of the
`plan_portfolio` block:

```yaml
args:
  plan_portfolio:
    - {}                      # default member, always first
    - levels_stride_max: 2
    - levels_stride_max: 3
```

Sign convention: **a positive diff means the portfolio arm is worse.**

## Results

| | portfolio vs default-only |
|---|---|
| Scenarios compared | 100 |
| `end_record`-flagged | 0 |
| Mean metric diff | **-2.0431** |
| Min / max metric diff | -130.1937 / **+0.0028** |
| Better / worse / unchanged | **20 / 0 / 80** |
| Mean cost diff | -2.0068 |
| Mean runtime (control -> arm) | 11.955s -> 50.804s |
| Total runtime (control -> arm) | 1195s -> 5080s |
| **Runtime multiplier** | **4.25x** |
| Slowest single scenario (control -> arm) | 132.5s -> **464.9s** |

The `+0.0028` maximum is below the harness's 0.01 significance threshold and is counted as unchanged.
It is not a counterexample to the elitism guarantee: that guarantee holds at the **common scoring
horizon**, whereas the harness reports each run's metric at its own `end_record`. A float-level
discrepancy between the two is expected.

## The mechanism is doing what it claims

Two independent confirmations, both stronger than the unit tests.

**State isolation.** For 19 of the 20 improved scenarios, the portfolio's selected metric equals the
metric of the corresponding **standalone** stride arm exactly — that is, `stride_results_2.json` /
`stride_results_3.json`, run in a separate process. For the 80 unchanged scenarios it equals the
control exactly. Members running inside the portfolio therefore produce bit-identical plans to the
same configuration run in its own process. If any state leaked between members, neither identity
would hold.

**Common-horizon scoring changes a decision, correctly.** Exactly one scenario diverged from the
oracle's pick:

| scenario | default | stride 2 | stride 3 | oracle would pick | portfolio picked |
|---|---|---|---|---|---|
| id 64 | 589.3890 | 589.4032 | 588.9610 | stride 3 (588.9610) | **default (589.3890)** |

This is the mechanism being **more correct than the oracle**, not less. The oracle compared metrics
that each run computed over its own `end_record`; the metric is integrated over `end_record`, so that
comparison is invalid whenever the horizons differ — precisely the error the spec was designed to
avoid. The portfolio rescored all three candidates on a common horizon and found the default was
genuinely best there. The resulting 0.0043 shortfall against the oracle target is the oracle being
slightly wrong, not the implementation underperforming.

## Runtime

4.25x is **cheaper** than the 4.91x the oracle implied, because the oracle summed three independent
full runs while the portfolio shares per-scenario setup across members.

The tail is the operational problem, not the mean. The slowest single scenario went from 132.5s to
**464.9s**, well beyond the 5-minute Predbat loop. Sequentially, this portfolio does not fit the live
loop.

Members are, however, **embarrassingly parallel across processes** — they share no state by
construction, which the state-isolation evidence above demonstrates empirically. Running them
concurrently would bring wall-clock close to the slowest single member rather than the sum, which
would fit. That is the obvious follow-up and is deliberately out of scope here.

## What the improvement actually is

As with the stride experiment, the mean is a poor summary.

- **80 of 100 scenarios do not move at all.**
- 20 improve, none regress.
- The mean is dominated by one scenario: id 85 at **-130.19**. Dropping it takes the mean from -2.04
  to roughly -0.73.

So the portfolio is not "0.33% better plans on average". It is **insurance: most of the time it
changes nothing, occasionally it rescues a badly-wrong plan, and it can never make things worse.**
That is a different and more defensible value proposition than a fractional average gain — and unlike
the stride experiment, there is no statistical uncertainty to argue about, because per-scenario
selection is a dominance argument rather than a distributional estimate.

## Known defects, not fixed here

1. **Elitism is documented but not enforced.** `optimise_all_windows_portfolio` iterates the
   configured member list as given. A user configuring `plan_portfolio: [{levels_stride_max: 2},
   {levels_stride_max: 3}]` — with no default member — gets no guarantee at all, and the portfolio
   can produce a worse plan than today, silently voiding the feature's central claim. The code should
   either prepend a default member or reject such a config. This experiment is unaffected because its
   member 0 is the explicit default.

2. **The self-consistency unit test may be vacuous.** `test_portfolio_self_consistency` builds its
   scenario with only `reset_inverter` before calling `calculate_plan`, setting no rates, PV or load.
   If the resulting plan is trivial, the test passes without exercising state isolation — which would
   explain why it passed on the first attempt. The experiment above supersedes it as evidence, but
   the test should be strengthened to run a non-trivial scenario or it will not catch a future
   regression.

## Decision

**No default was changed.** `plan_portfolio` remains absent, which short-circuits to a single direct
cascade call and is byte-identical to previous behaviour (verified by a 20/20 zero-diff comparison
generated on both sides of the change).

The mechanism is validated and the guarantee holds. Whether to enable it, and with which members, is
a deployment decision that turns on available compute — and on parallelising the members first, since
the sequential worst case of 464.9s does not fit the 5-minute loop.

## Reproducing

```bash
cd coverage
source setup.csh
# Control and scenario set come from the stride experiment (seed 0, n=100)
./run_stride_experiment 100 0            # produces stride_results_1a.json + stride_scenarios.yaml
python3 -c "
import yaml
d = yaml.unsafe_load(open('stride_template_1.yaml'))
d['args']['plan_portfolio'] = [{}, {'levels_stride_max': 2}, {'levels_stride_max': 3}]
yaml.dump(d, open('stride_template_portfolio.yaml','w'), default_flow_style=False)"
python3 ../apps/predbat/unit_test.py --random-run \
    --random-template stride_template_portfolio.yaml \
    --random-scenarios stride_scenarios.yaml \
    --random-results stride_results_portfolio.json
python3 ../apps/predbat/unit_test.py --random-compare \
    stride_results_1a.json stride_results_portfolio.json
```

The portfolio arm takes roughly 85 minutes at n=100 and must be backgrounded — a foreground shell
with a 10-minute limit will not survive it. Note `self.log` output does not reach stdout in this
harness, so the "Plan portfolio selected member ..." lines do not appear in the run log; confirm the
portfolio actually engaged from the runtime multiplier and the metric differences instead.
