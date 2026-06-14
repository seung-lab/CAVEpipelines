import json
import socket
import subprocess
import sys
import threading

from pipeline import cgcache


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
