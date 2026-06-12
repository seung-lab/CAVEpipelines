import dataclasses
from types import SimpleNamespace

import pytest
from click.testing import CliRunner

from pipeline import cli


def _invoke(args, cfg, **kw):
    return CliRunner().invoke(cli.deploy, args, obj=cfg, catch_exceptions=False, **kw)


def _mock_helm(monkeypatch, calls):
    monkeypatch.setattr(cli.kube, "secret_data", lambda d, m: {})
    monkeypatch.setattr(
        cli.subprocess,
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
    assert "supersedes" in res.output


def test_oneshot_aborts_before_any_mutation(monkeypatch, cfg):
    cfg.config_dir = "nonexistent"  # no counts cache -> plan prints the setup note
    touched = []
    monkeypatch.setattr(
        cli.kube, "secret_data", lambda d, m: touched.append("secret") or {}
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
    first_probe = {"fails": True}

    def counts(c):  # graph unreadable once -> setup runs exactly once
        if first_probe.pop("fails", None):
            raise SystemExit("no graph")
        return {2: 100, 3: 10, 4: 1}

    monkeypatch.setattr(cli.util, "read_layer_counts", counts)
    monkeypatch.setattr(cli.setup, "callback", lambda *a, **k: calls.append("setup"))
    monkeypatch.setattr(
        cli.mesh_meta, "callback", lambda *a, **k: calls.append("mesh-meta")
    )
    monkeypatch.setattr(
        cli, "_run_layer", lambda ctx, c, layer: calls.append(f"{c.workload}-l{layer}")
    )
    _invoke(["--oneshot", "--yes"], cfg)
    assert calls == [
        "helm",
        "setup",
        "ingest-l2",
        "ingest-l3",
        "ingest-l4",
        "mesh-meta",
        "meshing-l2",
        "meshing-l3",  # capped by mesh_config.max_layer, not the root
    ]


def test_oneshot_resumes_without_setup_or_meshing(monkeypatch, cfg):
    cfg.config_dir = "nonexistent"
    calls = []
    _mock_helm(monkeypatch, calls)
    monkeypatch.setattr(cli.config, "load", _fake_load(cfg))
    monkeypatch.setattr(cli.util, "read_layer_counts", lambda c: {2: 5})
    monkeypatch.setattr(cli.setup, "callback", lambda *a, **k: calls.append("setup"))
    monkeypatch.setattr(
        cli, "_run_layer", lambda ctx, c, layer: calls.append(f"l{layer}")
    )
    _invoke(["--oneshot", "--yes"], cfg)
    assert calls == ["helm", "l2"]  # graph readable -> no setup; no mesh_config -> done


def _job(state, failed_indexes=None):
    conditions = {
        "complete": [SimpleNamespace(type="Complete", status="True")],
        "failed": [SimpleNamespace(type="Failed", status="True")],
        "running": [],
    }[state]
    return SimpleNamespace(
        metadata=SimpleNamespace(
            name="ingest-l2",
            labels={"graph": "g", "layer": "2"},
            annotations={"chunks": "10", "batch_size": "1"},
        ),
        status=SimpleNamespace(
            conditions=conditions,
            succeeded=5,
            active=1,
            ready=1,
            failed=0,
            failed_indexes=failed_indexes,
            start_time=None,
            completion_time=None,
        ),
    )


def test_run_layer_skips_complete_layers(monkeypatch, cfg):
    monkeypatch.setattr(cli, "_read_job", lambda c, layer: _job("complete"))
    submitted = []
    monkeypatch.setattr(cli.submit, "callback", lambda *a, **k: submitted.append(True))
    cli._run_layer(None, cfg, 2)
    assert not submitted


def test_run_layer_attaches_and_stops_on_dead_tasks(monkeypatch, cfg):
    monkeypatch.setattr(cli.costdb, "sample", lambda c: None)
    monkeypatch.setattr(
        cli, "_read_job", lambda c, layer: _job("running", failed_indexes="0-3")
    )
    submitted = []
    monkeypatch.setattr(cli.submit, "callback", lambda *a, **k: submitted.append(True))
    with pytest.raises(SystemExit, match="inspect 2"):
        cli._run_layer(None, cfg, 2)
    assert not submitted  # a running layer is attached, never recreated
