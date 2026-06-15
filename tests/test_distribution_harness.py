"""The injected worker harness: env contract + the outcome -> exit-code matrix."""

import pytest

from cave_pipeline.distribution import FATAL, SUCCESS, TRANSIENT
from cave_pipeline.distribution.harness import run


@pytest.fixture
def env(monkeypatch):
    for k, v in {
        "PCG_GRAPH_ID": "g",
        "PCG_LAYER": "2",
        "PCG_PERM_SEED": "7",
        "PCG_BATCH_SIZE": "4",
        "JOB_COMPLETION_INDEX": "0",
    }.items():
        monkeypatch.setenv(k, v)


def _run(outcomes, finalize=None):
    """Run the harness over a 2x2x1 grid (4 chunks); `outcomes` is the per-chunk result."""
    seen = []

    def make_processor(ctx, layer, env):
        assert ctx == "ctx" and layer == 2  # the injected context reaches the workload
        results = iter(outcomes)
        return lambda coord: (seen.append(coord), next(results))[1]

    code = run(
        make_processor,
        context_factory=lambda env: "ctx",
        bounds_fn=lambda ctx, layer: (2, 2, 1),
        finalize=finalize,
    )
    return code, seen


def test_all_ok_returns_success_and_finalizes(env):
    done = []
    code, seen = _run(["ok"] * 4, finalize=lambda ctx, layer: done.append(layer))
    assert code == SUCCESS and len(seen) == 4 and done == [2]


def test_transient_returns_transient_and_skips_finalize(env):
    done = []
    code, _ = _run(
        ["ok", "transient", "ok", "ok"], finalize=lambda c, lyr: done.append(lyr)
    )
    assert code == TRANSIENT and not done  # batch is retried; finalize must not run


def test_fatal_without_transient_returns_fatal(env):
    code, _ = _run(["ok", "fatal", "ok", "ok"])
    assert code == FATAL


def test_transient_outranks_fatal(env):
    # any transient retries the whole batch (done chunks skip); fatal waits for next round
    code, _ = _run(["fatal", "transient", "ok", "ok"])
    assert code == TRANSIENT


def test_finalize_failure_is_fatal(env):
    def boom(ctx, layer):
        raise RuntimeError("verify failed")

    code, _ = _run(["ok"] * 4, finalize=boom)
    assert code == FATAL  # a fully-ok batch whose finalize fails must not re-open chunks
