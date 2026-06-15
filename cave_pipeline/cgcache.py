"""In-pod cg-cache server + client snippets, run as `python -c` source strings.

The persistent util pod runs SERVER_SRC as its container command: it imports
ChunkedGraph once, then answers meta probes over a unix socket. Probes exec the
stdlib-only CLIENT_SRC; the one-shot path (no util pod) runs ONESHOT_SRC inline.

These ship as source strings because the util pod runs the PCG image, which lacks
the `pipeline` package. This module imports nothing from `pipeline`, so both `util`
and `manifest` import it without a cycle. op/gid/socket/timeout pass as argv (never
interpolated), so the snippets are static and injection-free.
"""

CG_SOCK = "/tmp/cg-cache.sock"

# The two meta probes, defined once and embedded in both the server and the one-shot
# snippet. A fresh ChunkedGraph per call re-reads meta, so the mutable mesh block is
# never stale; the output reproduces the standalone snippet's stdout (parsers unchanged).
_OPS_SRC = """
from pychunkedgraph.graph import ChunkedGraph


def run_op(op, gid):
    cg = ChunkedGraph(graph_id=gid)
    if op == "counts":
        return " ".join(str(int(c)) for c in cg.meta.layer_chunk_counts) + "\\n"
    if op == "mesh":
        return ("yes" if cg.meta.custom_data.get("mesh") else "no") + "\\n"
    raise ValueError("unknown op: " + repr(op))
"""

# Util pod command: import cg once, then serve fresh-cg probes forever. Never exits
# (the bigtable channel thread only hangs atexit, which we never reach); k8s restarts
# the container on crash. Serial accept loop — the operator runs one command at a time.
SERVER_SRC = (
    """
import json
import os
import socket
import sys
import traceback
"""
    + _OPS_SRC
    + """
sock = sys.argv[1]
try:
    os.unlink(sock)
except FileNotFoundError:
    pass
srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
srv.bind(sock)
srv.listen(1)
print("cg-cache: ready", flush=True)
while True:
    conn, _ = srv.accept()
    with conn:
        buf = b""
        while b"\\n" not in buf:
            chunk = conn.recv(4096)
            if not chunk:
                break
            buf += chunk
        try:
            req = json.loads(buf.decode().strip())
            reply = {"ok": True, "out": run_op(req["op"], req["gid"])}
        except Exception:
            reply = {"ok": False, "err": traceback.format_exc()}
        conn.sendall((json.dumps(reply) + "\\n").encode())
"""
)

# Thin client: connect-retry until the timeout (covers the cg import window right after
# deploy), send {op, gid}, relay the server's stdout, or exit non-zero with its traceback.
CLIENT_SRC = """
import json
import socket
import sys
import time

sock, op, gid, timeout = sys.argv[1], sys.argv[2], sys.argv[3], float(sys.argv[4])
deadline = time.monotonic() + timeout
while True:
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        s.connect(sock)
        break
    except OSError:
        s.close()
        if time.monotonic() > deadline:
            sys.stderr.write("cg-cache unreachable on " + sock + "; retry\\n")
            sys.exit(1)
        time.sleep(0.25)
s.sendall((json.dumps({"op": op, "gid": gid}) + "\\n").encode())
buf = b""
while b"\\n" not in buf:
    chunk = s.recv(4096)
    if not chunk:
        break
    buf += chunk
resp = json.loads(buf.decode().strip())
if resp.get("ok"):
    sys.stdout.write(resp["out"])
    sys.exit(0)
sys.stderr.write(resp.get("err") or "cg-cache: malformed response\\n")
sys.exit(1)
"""

# One-shot path (no util pod): import cg, run the op inline, os._exit to dodge the
# bigtable atexit hang.
ONESHOT_SRC = (
    """
import os
import sys
import traceback
"""
    + _OPS_SRC
    + """
try:
    sys.stdout.write(run_op(sys.argv[1], sys.argv[2]))
    sys.stdout.flush()
    os._exit(0)
except Exception:
    traceback.print_exc()
    sys.stderr.flush()
    os._exit(1)
"""
)
