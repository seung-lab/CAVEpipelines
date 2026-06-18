import dataclasses
from types import SimpleNamespace

from click.testing import CliRunner

from cave_pipeline import cli, ops


def _invoke(args, cfg, **kw):
    return CliRunner().invoke(cli.deploy, args, obj=cfg, catch_exceptions=False, **kw)


def _mock_helm(monkeypatch, calls):
    # deploy_infra fails fast if helm or the secrets dir is missing; neither exists in CI
    monkeypatch.setattr(ops.shutil, "which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr(ops.os.path, "isdir", lambda p: True)
    monkeypatch.setattr(ops.kube, "secret_data", lambda d, m: {})
    monkeypatch.setattr(ops.kube, "util_pod", lambda ns, wait_create=False: "util-pod")
    monkeypatch.setattr(ops.kube, "list_jobs", lambda ns, w=None: [])  # drive clears suspend on entry
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


def test_oneshot_refuses_migrate_workload(cfg):
    cfg.workload = "migrate"  # migrate is never part of a build DAG
    res = _invoke(["--oneshot"], cfg)
    assert res.exit_code != 0
    assert "not part of a build" in res.output


def test_oneshot_aborts_before_any_mutation(monkeypatch, cfg):
    cfg.config_dir = "nonexistent"  # no counts cache -> plan prints the setup note
    touched = []
    monkeypatch.setattr(
        ops.kube, "secret_data", lambda d, m: touched.append("secret") or {}
    )
    args = [
        "--oneshot",
        "--from",
        "0",
        "--to",
        "0",
    ]  # skip the range prompt; confirm with "n"
    res = CliRunner().invoke(cli.deploy, args, obj=cfg, input="n\n")
    assert res.exit_code != 0
    assert not touched  # confirmation comes before helm/secret work


def test_oneshot_sequences_phases(monkeypatch, cfg, stub_layer_counts):
    cfg.dataset["mesh_config"] = {"max_layer": 3}
    cfg.config_dir = "nonexistent"
    calls = []
    _mock_helm(monkeypatch, calls)
    monkeypatch.setattr(cli.config, "load", _fake_load(cfg))
    stub_layer_counts({2: 100, 3: 10, 4: 1})
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
        "setup(meshing,exist_ok=True)",  # mesh-meta, resume-safe like every stage
        "meshing-l2",
        "meshing-l3",  # capped by mesh_config.max_layer, not the root
    ]


def test_oneshot_requires_mesh_config(cfg):
    cfg.dataset.pop(
        "mesh_config", None
    )  # meshing is mandatory; a build needs mesh_config
    res = _invoke(["--oneshot", "--yes"], cfg)
    assert res.exit_code != 0
    assert "not configured" in res.output


def test_oneshot_ingest_only_range_runs_just_ingest(monkeypatch, cfg, stub_layer_counts):
    cfg.dataset.pop("mesh_config", None)  # ingest-only range -> mesh_config not needed
    cfg.config_dir = "nonexistent"
    calls = []
    _mock_helm(monkeypatch, calls)
    monkeypatch.setattr(cli.config, "load", _fake_load(cfg))
    stub_layer_counts({2: 5})
    monkeypatch.setattr(
        ops,
        "setup",
        lambda c, exist_ok=False: calls.append(f"setup(exist_ok={exist_ok})"),
    )
    monkeypatch.setattr(ops, "run_layer", lambda c, layer: calls.append(f"l{layer}"))
    _invoke(["--oneshot", "--from", "0", "--to", "0", "--yes"], cfg)
    assert calls == ["helm", "setup(exist_ok=True)", "l2"]  # ingest only; no meshing


def _mock_all_layers(monkeypatch, cfg, counts, calls):
    cfg.config_dir = "nonexistent"
    _mock_helm(monkeypatch, calls)
    monkeypatch.setattr(
        ops.config, "load", _fake_load(cfg)
    )  # orchestrate uses _phase_cfg
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
