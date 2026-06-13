import dataclasses
from types import SimpleNamespace

import pytest
from click.testing import CliRunner

from pipeline import cli, ops


def _invoke(args, cfg, **kw):
    return CliRunner().invoke(cli.deploy, args, obj=cfg, catch_exceptions=False, **kw)


def _mock_helm(monkeypatch, calls):
    monkeypatch.setattr(ops.kube, "secret_data", lambda d, m: {})
    monkeypatch.setattr(ops.kube, "util_pod", lambda ns, wait_create=False: "util-pod")
    monkeypatch.setattr(
        ops.subprocess,
        "run",
        lambda argv, **kw: (
            calls.append("helm"),
            SimpleNamespace(returncode=0, stdout="", stderr=""),
        )[1],
    )


def _fake_load(cfg):
    return lambda name, workload=None: dataclasses.replace(cfg, workload=workload)


def test_oneshot_conflicts_with_setup_flags(cfg):
    res = _invoke(["--oneshot", "--setup"], cfg)  # CliRunner absorbs the SystemExit
    assert res.exit_code != 0
    assert "supersede" in res.output


def test_oneshot_and_all_layers_are_exclusive(cfg):
    res = _invoke(["--oneshot", "--all-layers"], cfg)
    assert res.exit_code != 0
    assert "mutually exclusive" in res.output


def test_oneshot_refuses_non_ingest_workload(cfg):
    cfg.workload = "meshing"  # --oneshot builds from ingest; honor the yaml
    res = _invoke(["--oneshot"], cfg)
    assert res.exit_code != 0
    assert "--all-layers" in res.output


def test_oneshot_aborts_before_any_mutation(monkeypatch, cfg):
    cfg.config_dir = "nonexistent"  # no counts cache -> plan prints the setup note
    touched = []
    monkeypatch.setattr(
        ops.kube, "secret_data", lambda d, m: touched.append("secret") or {}
    )
    res = CliRunner().invoke(cli.deploy, ["--oneshot"], obj=cfg, input="n\n")
    assert res.exit_code != 0
    assert not touched  # confirmation comes before helm/secret work


def test_oneshot_sequences_phases(monkeypatch, cfg):
    cfg.dataset["mesh_config"] = {"max_layer": 3}
    cfg.config_dir = "nonexistent"
    calls = []
    _mock_helm(monkeypatch, calls)
    monkeypatch.setattr(cli.config, "load", _fake_load(cfg))
    monkeypatch.setattr(ops.util, "read_layer_counts", lambda c: {2: 100, 3: 10, 4: 1})
    monkeypatch.setattr(
        ops,
        "setup",
        lambda c, exist_ok=False: calls.append(
            f"setup({c.workload},exist_ok={exist_ok})"
        ),
    )
    monkeypatch.setattr(
        ops, "run_layer", lambda c, layer: calls.append(f"{c.workload}-l{layer}")
    )
    _invoke(["--oneshot", "--yes"], cfg)
    assert calls == [
        "helm",
        "setup(ingest,exist_ok=True)",  # resume-safe (skips a created table)
        "ingest-l2",
        "ingest-l3",
        "ingest-l4",
        "setup(meshing,exist_ok=False)",  # mesh-meta, via the per-workload setup dispatch
        "meshing-l2",
        "meshing-l3",  # capped by mesh_config.max_layer, not the root
    ]


def test_oneshot_setup_is_resume_safe_and_skips_meshing(monkeypatch, cfg):
    cfg.config_dir = "nonexistent"
    calls = []
    _mock_helm(monkeypatch, calls)
    monkeypatch.setattr(cli.config, "load", _fake_load(cfg))
    monkeypatch.setattr(ops.util, "read_layer_counts", lambda c: {2: 5})
    monkeypatch.setattr(
        ops,
        "setup",
        lambda c, exist_ok=False: calls.append(f"setup(exist_ok={exist_ok})"),
    )
    monkeypatch.setattr(ops, "run_layer", lambda c, layer: calls.append(f"l{layer}"))
    _invoke(["--oneshot", "--yes"], cfg)
    assert calls == ["helm", "setup(exist_ok=True)", "l2"]  # no mesh_config -> no meshing


def _mock_all_layers(monkeypatch, cfg, counts, calls):
    cfg.config_dir = "nonexistent"
    _mock_helm(monkeypatch, calls)
    monkeypatch.setattr(ops.util, "read_layer_counts", lambda c: counts)
    monkeypatch.setattr(
        ops,
        "setup",
        lambda c, exist_ok=False: calls.append(
            f"setup({c.workload},exist_ok={exist_ok})"
        ),
    )
    monkeypatch.setattr(
        ops, "run_layer", lambda c, layer: calls.append(f"{c.workload}-l{layer}")
    )


def test_all_layers_meshing_runs_only_meshing(monkeypatch, cfg):
    cfg.workload = "meshing"
    cfg.dataset["mesh_config"] = {"max_layer": 3}
    calls = []
    _mock_all_layers(monkeypatch, cfg, {2: 100, 3: 10, 4: 1}, calls)
    _invoke(["--all-layers", "--yes"], cfg)
    # mesh-meta (meshing setup) + meshing layers capped at max_layer; never ingest
    assert calls == ["helm", "setup(meshing,exist_ok=True)", "meshing-l2", "meshing-l3"]


def test_all_layers_ingest_runs_setup_then_ingest_layers(monkeypatch, cfg):
    cfg.workload = "ingest"
    calls = []
    _mock_all_layers(monkeypatch, cfg, {2: 100, 3: 10}, calls)
    _invoke(["--all-layers", "--yes"], cfg)
    assert calls == ["helm", "setup(ingest,exist_ok=True)", "ingest-l2", "ingest-l3"]


_CONDS = {
    "complete": [SimpleNamespace(type="Complete", status="True")],
    "failed": [SimpleNamespace(type="Failed", status="True")],
    "running": [],
}


def test_run_layer_skips_complete_layers(monkeypatch, cfg, make_job):
    job = make_job(conditions=_CONDS["complete"], succeeded=5)
    monkeypatch.setattr(ops, "_read_job", lambda c, layer: job)
    submitted = []
    monkeypatch.setattr(ops, "submit", lambda c, layer: submitted.append(True))
    ops.run_layer(cfg, 2)
    assert not submitted


def test_run_layer_attaches_and_stops_on_dead_tasks(monkeypatch, cfg, make_job):
    job = make_job(conditions=_CONDS["running"], succeeded=5, failed_indexes="0-3")
    monkeypatch.setattr(ops.costdb, "sample", lambda c: None)
    monkeypatch.setattr(ops, "_read_job", lambda c, layer: job)
    submitted = []
    monkeypatch.setattr(ops, "submit", lambda c, layer: submitted.append(True))
    with pytest.raises(SystemExit, match="inspect 2"):
        ops.run_layer(cfg, 2)
    assert not submitted  # a running layer is attached, never recreated


def test_run_layer_stops_cleanly_when_job_vanishes(monkeypatch, cfg, make_job):
    job = make_job(conditions=_CONDS["running"])
    reads = iter([job, None])  # present at attach, deleted before the first poll
    monkeypatch.setattr(ops.costdb, "sample", lambda c: None)
    monkeypatch.setattr(ops, "_read_job", lambda c, layer: next(reads))
    with pytest.raises(SystemExit, match="disappeared"):
        ops.run_layer(cfg, 2)
