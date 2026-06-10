from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from rich.console import Console

from pipeline import util


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
