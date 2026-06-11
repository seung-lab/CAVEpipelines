from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from pipeline import costs, rates

RATE = {
    "cpu_on_demand": 0.0445,
    "cpu_spot": 0.0100,
    "mem_on_demand": 0.0049,
    "mem_spot": 0.0011,
    "cluster_fee_hr": 0.10,
}


def _job(cpu="1", mem="2Gi", cls="", *, start=None, end=None, succeeded=0, active=0):
    spec = SimpleNamespace(
        template=SimpleNamespace(
            spec=SimpleNamespace(
                containers=[
                    SimpleNamespace(
                        resources=SimpleNamespace(requests={"cpu": cpu, "memory": mem})
                    )
                ],
                node_selector=({"cloud.google.com/compute-class": cls} if cls else {}),
            )
        )
    )
    status = SimpleNamespace(
        start_time=start, completion_time=end, succeeded=succeeded, active=active
    )
    return SimpleNamespace(spec=spec, status=status)


def _pod(start, end):
    term = SimpleNamespace(started_at=start, finished_at=end)
    return SimpleNamespace(
        metadata=SimpleNamespace(creation_timestamp=start),
        status=SimpleNamespace(
            container_statuses=[SimpleNamespace(state=SimpleNamespace(terminated=term))]
        ),
    )


def test_parse_cpu_mem_units():
    assert costs.parse_cpu("500m") == 0.5
    assert costs.parse_cpu("2") == 2.0
    assert costs.parse_mem("2Gi") == 2.0
    assert costs.parse_mem("512Mi") == 0.5


def test_per_pod_estimate_matches_formula():
    t0 = datetime(2026, 1, 1, tzinfo=timezone.utc)
    t1 = t0 + timedelta(hours=1)
    job = _job("1", "2Gi", start=t0, end=t1, succeeded=2)
    pods = [_pod(t0, t1), _pod(t0, t1)]  # 2 pods x 1h = 2 pod-hr
    est = costs.estimate_job_cost(job, pods, RATE)
    expected = (
        2 * (1 * RATE["cpu_spot"] + 2 * RATE["mem_spot"]) + RATE["cluster_fee_hr"] * 1
    )
    assert abs(est["total"] - expected) < 1e-9
    assert est["basis"] == "per-pod"


def test_wall_clock_fallback_when_no_pods():
    t0 = datetime(2026, 1, 1, tzinfo=timezone.utc)
    t1 = t0 + timedelta(hours=2)
    job = _job("1", "2Gi", start=t0, end=t1, succeeded=3)
    est = costs.estimate_job_cost(job, [], RATE)  # wall 2h x 3 pods = 6 pod-hr
    expected = (
        6 * (1 * RATE["cpu_spot"] + 2 * RATE["mem_spot"]) + RATE["cluster_fee_hr"] * 2
    )
    assert abs(est["total"] - expected) < 1e-9
    assert est["basis"] == "wall x pods"


def test_node_based_class_is_refused_not_priced():
    est = costs.estimate_job_cost(_job(cls="Performance"), [], RATE)
    assert "error" in est and "node-based" in est["error"]


def test_estimate_never_raises_on_bad_input():
    assert "error" in costs.estimate_job_cost(None, [], RATE)


def test_rate_for_is_safe():
    assert costs.rate_for("") is None
    assert costs.rate_for("no-such-region") is None


def test_committed_rates_load():
    table = rates.load()
    assert "us-east1" in table and table["us-east1"]["cpu_spot"] > 0
