# Board predbat-memory

A shared-goal board that cuts Predbat's memory use, with plans unchanged. It runs on the board runner and the
blackboard skill from agent-memory-supabase (`runner/` and `skills/blackboard/` there). The board's memories live in the
`predbat-devs` scope, tagged `board:predbat-memory`.

| File               | What it is                                                                          |
| ------------------ | ----------------------------------------------------------------------------------- |
| `board.toml`       | The board file: goal, done-when, rules, intent limit, models and permissions        |
| `measure.py`       | The runner's `[measure]` command, which confirms a candidate before the board ends  |
| `test_measure.py`  | Tests for `measure.py`: `python3 -m unittest discover -s boards/predbat-memory`     |

## Before the first run: the benchmark

The board needs `coverage/memory_benchmark`, which does not exist yet. Workers use it to find and check savings, and
`measure.py` uses it to judge the lead's candidate. Until it exists, `measure.py` exits 2 with `HARNESS ERROR`.

What it must do:

- **Command line:** `coverage/memory_benchmark <tree> <out dir>`, run from the main checkout. `<tree>` is any batpred
  checkout (a worktree for main or for a board branch). The exit code is 0 when the run completed.
- **The run:** Predbat standalone from `<tree>`'s `apps/predbat/`, with `db_primary: true`, the dummy inverter
  (`dummy_inverter:` block, see `apps/predbat/dummy_inverter.py`) and no Home Assistant. It runs a fixed number of plan
  cycles, with the clock and every input fixed, so two runs of the same tree give the same plans.
- **Where:** inside a Linux container, on the same image every time, since macOS RSS is not usable for memory work.
  The kernel is built for the container's architecture from `<tree>`, so a board branch never runs a stale binary.
- **Output:** `<out dir>/result.json`, plus the run's `predbat.log` in `<out dir>`:

  ```json
  { "ok": true, "steady_rss_mb": 120.5, "peak_rss_mb": 150.2, "plan_digest": "<hex>" }
  ```

    - `steady_rss_mb`: RSS once the run has settled, after the first cycles. The benchmark chooses how, and keeps it fixed.
    - `peak_rss_mb`: VmHWM for the whole run.
    - `plan_digest`: a hash of every cycle's plan, which must not change when memory use does.
    - `ok`: false when the log shows an error, or the kernel fell back to Python ("stale binary").

Two runs of main must give the same `plan_digest`: `measure.py` treats a base that disagrees with itself as a harness
error.

Once it exists, measure main a few times and check that the 25% target in `board.toml` (goal, done-when) and
`measure.py` (`STEADY_CUT`) is still the right one.

## Running it

```sh
cd ~/predbat/batpred
board-runner run boards/predbat-memory/board.toml
```

The runner runs `measure.py` from the main checkout, so this directory and the benchmark must be on the checked-out
branch there. `board-runner status boards/predbat-memory/board.toml` shows where it got to. To continue after the
board stops at its intent limit, use the skill's `resume predbat-memory max_intents=<n>`.
