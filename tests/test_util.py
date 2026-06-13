import json
import socket
import subprocess
import sys
import threading
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from rich.console import Console

from pipeline import cgcache, util


def test_ceil_div_completions():
    assert util.ceil_div(10000, 1000) == 10
    assert util.ceil_div(10001, 1000) == 11  # a partial last batch still needs an index
    assert util.ceil_div(1, 1000) == 1


def _cond(t, s="True"):
    return SimpleNamespace(type=t, status=s)


def _job(conditions=None, **status):
    return SimpleNamespace(status=SimpleNamespace(conditions=conditions, **status))


def test_job_state():
    assert util.job_state(_job([_cond("Complete")])) == "complete"
    assert util.job_state(_job([_cond("Failed")])) == "failed"
    assert util.job_state(_job([_cond("Complete", "False")])) == "running"
    assert util.job_state(_job(None)) == "running"


def test_elapsed():
    start = datetime(2026, 1, 1, tzinfo=timezone.utc)
    end_75 = start + timedelta(minutes=75)
    end_5 = start + timedelta(minutes=5)
    assert util.elapsed(_job(start_time=start, completion_time=end_75)) == "1h15m"
    assert util.elapsed(_job(start_time=start, completion_time=end_5)) == "5m"
    assert util.elapsed(_job(start_time=None)) == "-"


def _render(cfg, job, monkeypatch):
    monkeypatch.setattr(util.kube, "list_jobs", lambda ns, w: [job])
    monkeypatch.setattr(util.kube, "node_summary", lambda: (3, 2, {"e2-standard-4": 3}))
    console = Console(width=160, no_color=True)
    with console.capture() as cap:
        console.print(util.status_table(cfg))
    return cap.get()


def _job_row(succeeded, chunks, batch, conditions=None):
    return SimpleNamespace(
        metadata=SimpleNamespace(
            labels={"layer": "2"},
            annotations={"chunks": str(chunks), "batch_size": str(batch)},
        ),
        status=SimpleNamespace(
            conditions=conditions,
            succeeded=succeeded,
            active=0,
            failed=0,
            ready=0,
            start_time=None,
            completion_time=None,
        ),
    )


def test_status_progress_math(monkeypatch, cfg):
    out = _render(cfg, _job_row(succeeded=4, chunks=1000, batch=100), monkeypatch)
    # 4 succeeded batches * 100 = 400 done of 1000 -> 40%
    assert "400" in out and "1000" in out and "40%" in out
    assert "3 nodes" in out and "2 spot" in out


def test_status_done_caps_at_total(monkeypatch, cfg):
    # last batch is partial: 10*100 = 1000 reported, but only 950 chunks exist.
    job = _job_row(succeeded=10, chunks=950, batch=100, conditions=[_cond("Complete")])
    out = _render(cfg, job, monkeypatch)
    assert "950" in out and "100%" in out  # not 1000, not 105%


def test_query_meta_routes_persistent_to_cache_client(monkeypatch, cfg):
    cfg.persistent_util = True
    seen = {}
    monkeypatch.setattr(util.kube, "util_pod", lambda ns: "util-pod")

    def _exec(ns, pod, argv, **kw):
        seen["argv"] = argv
        return "100 50 1\n"

    monkeypatch.setattr(util.kube, "exec_cmd", _exec)
    monkeypatch.setattr(
        util.kube,
        "run_oneshot",
        lambda *a, **k: pytest.fail("persistent path must not use a one-shot pod"),
    )
    assert util._query_meta(cfg, "counts", "g") == "100 50 1\n"
    assert cgcache.CLIENT_SRC in seen["argv"]  # the warm-server client, not the server


def test_query_meta_routes_oneshot_when_not_persistent(monkeypatch, cfg):
    cfg.persistent_util = False
    seen = {}
    monkeypatch.setattr(
        util.manifest, "oneshot_pod_spec", lambda c, name, argv: ("spec", argv)
    )

    def _oneshot(ns, spec):
        seen["argv"] = spec[1]
        return "yes\n"

    monkeypatch.setattr(util.kube, "run_oneshot", _oneshot)
    monkeypatch.setattr(
        util.kube,
        "exec_cmd",
        lambda *a, **k: pytest.fail("one-shot path must not exec into the util pod"),
    )
    assert util._query_meta(cfg, "mesh", "g") == "yes\n"
    assert cgcache.ONESHOT_SRC in seen["argv"]  # the inline import snippet


def _stub_cache_server(sock_path, reply, ready):
    """Bind a unix socket, accept one connection, reply with `reply` as JSON+newline."""
    srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    srv.settimeout(10)
    srv.bind(sock_path)
    srv.listen(1)
    ready.set()
    try:
        conn, _ = srv.accept()
    except socket.timeout:
        srv.close()
        return
    with conn:
        conn.recv(4096)  # drain the {op, gid} request
        conn.sendall((json.dumps(reply) + "\n").encode())
    srv.close()


def _run_client(sock_path, op, gid, timeout):
    return subprocess.run(
        [sys.executable, "-c", cgcache.CLIENT_SRC, sock_path, op, gid, str(timeout)],
        capture_output=True,
        text=True,
        timeout=15,
    )


def _serve(sock_path, reply):
    ready = threading.Event()
    t = threading.Thread(
        target=_stub_cache_server, args=(sock_path, reply, ready), daemon=True
    )
    t.start()
    ready.wait(5)
    return t


def test_client_relays_server_stdout(tmp_path):
    sock = str(tmp_path / "s.sock")
    t = _serve(sock, {"ok": True, "out": "100 50 1\n"})
    res = _run_client(sock, "counts", "g", 5)
    t.join(5)
    assert res.returncode == 0
    assert res.stdout == "100 50 1\n"  # relayed verbatim -> existing parsers untouched


def test_client_surfaces_server_error(tmp_path):
    sock = str(tmp_path / "s.sock")
    t = _serve(sock, {"ok": False, "err": "Traceback: boom\n"})
    res = _run_client(sock, "counts", "g", 5)
    t.join(5)
    assert res.returncode != 0  # non-zero -> exec_cmd raises, surfacing the traceback
    assert "boom" in res.stderr


def test_client_unreachable_exits_after_timeout(tmp_path):
    sock = str(tmp_path / "nobody.sock")  # nothing ever binds here
    res = _run_client(sock, "counts", "g", 1)  # 1s connect-retry window
    assert res.returncode != 0
    assert "unreachable" in res.stderr
