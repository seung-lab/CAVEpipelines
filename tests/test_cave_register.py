import io
import urllib.error

from cave_pipeline import ops, stages


def _cave_cfg(cfg, tmp_path, token="tok"):
    (tmp_path / "cave-secret.json").write_text('{"token": "%s"}' % token)
    cfg.secret_files = {"cave-secret.json": "cave-secret.json"}
    cfg.dataset["cave_config"] = {"host": "https://h", "dataset": "ds"}  # service defaults
    return cfg


def test_register_cave_noop_without_config(monkeypatch, cfg, tmp_path):
    posted = []
    monkeypatch.setattr(
        stages.urllib.request, "urlopen", lambda *a, **k: posted.append(1)
    )
    cfg.dataset.pop("cave_config", None)
    ops.register_cave(cfg, str(tmp_path))
    assert not posted  # no POST when cave_config is absent


def test_register_cave_posts_bearer_token_to_sticky_auth(monkeypatch, cfg, tmp_path):
    _cave_cfg(cfg, tmp_path)  # cfg.graph_id == "g" from the fixture
    seen = {}

    class _Resp:
        status = 200

        def read(self):
            return b"ok"

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    def fake_urlopen(req, timeout=None):
        seen.update(
            url=req.full_url,
            auth=req.get_header("Authorization"),
            method=req.get_method(),
        )
        return _Resp()

    monkeypatch.setattr(stages.urllib.request, "urlopen", fake_urlopen)
    ops.register_cave(cfg, str(tmp_path))
    assert (
        seen["url"]
        == "https://h/sticky_auth/api/v1/service/pychunkedgraph/table/g/dataset/ds"
    )
    assert seen["auth"] == "Bearer tok"
    assert seen["method"] == "POST"


def test_register_cave_is_best_effort_on_failure(monkeypatch, cfg, tmp_path):
    # a registration failure must never block the deploy
    _cave_cfg(cfg, tmp_path)

    def boom(req, timeout=None):
        raise urllib.error.HTTPError(
            req.full_url, 409, "exists", {}, io.BytesIO(b"already registered")
        )

    monkeypatch.setattr(stages.urllib.request, "urlopen", boom)
    ops.register_cave(cfg, str(tmp_path))  # must not raise
