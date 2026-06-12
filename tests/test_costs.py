from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from pipeline import costs, rates

REGION = "us-east1"
GP = {
    "cpu_on_demand": 0.0445,
    "cpu_spot": 0.0133,
    "mem_on_demand": 0.0049225,
    "mem_spot": 0.0014767,
    "cluster_fee_hr": 0.10,
}
BALANCED = {**GP, "cpu_spot": 0.0194, "mem_spot": 0.0021406}
TABLE = {REGION: {"general-purpose": GP, "Balanced": BALANCED}}


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
    assert costs.parse_cpu("8913484669n") == 8.913484669  # metrics-API nanocores
    assert costs.parse_cpu("1500000u") == 1.5
    assert costs.parse_mem("2Gi") == 2.0
    assert costs.parse_mem("512Mi") == 0.5


def test_rate_for_maps_default_class_and_misses():
    assert costs.rate_for(TABLE, REGION, "") == GP  # default class -> general-purpose
    assert costs.rate_for(TABLE, REGION, "Balanced")["cpu_spot"] == 0.0194
    assert costs.rate_for(TABLE, REGION, "Performance") is None  # node-based -> absent
    assert costs.rate_for(TABLE, "no-region", "") is None


def test_per_pod_estimate_matches_formula():
    t0 = datetime(2026, 1, 1, tzinfo=timezone.utc)
    t1 = t0 + timedelta(hours=1)
    job = _job("1", "2Gi", start=t0, end=t1, succeeded=2)
    pods = [_pod(t0, t1), _pod(t0, t1)]  # 2 pods x 1h = 2 pod-hr
    est = costs.estimate_job_cost(job, pods, TABLE, REGION)
    expected = 2 * (1 * GP["cpu_spot"] + 2 * GP["mem_spot"]) + GP["cluster_fee_hr"] * 1
    assert abs(est["total"] - expected) < 1e-9
    assert est["basis"] == "per-pod"


def test_balanced_class_costs_more_than_default():
    t0 = datetime(2026, 1, 1, tzinfo=timezone.utc)
    t1 = t0 + timedelta(hours=1)
    gp = costs.estimate_job_cost(_job(start=t0, end=t1, succeeded=1), [], TABLE, REGION)
    bal = costs.estimate_job_cost(
        _job(cls="Balanced", start=t0, end=t1, succeeded=1), [], TABLE, REGION
    )
    assert bal["total"] > gp["total"]


def test_wall_clock_fallback_when_no_pods():
    t0 = datetime(2026, 1, 1, tzinfo=timezone.utc)
    t1 = t0 + timedelta(hours=2)
    job = _job("1", "2Gi", start=t0, end=t1, succeeded=3)
    est = costs.estimate_job_cost(job, [], TABLE, REGION)  # wall 2h x 3 pods = 6 pod-hr
    expected = 6 * (1 * GP["cpu_spot"] + 2 * GP["mem_spot"]) + GP["cluster_fee_hr"] * 2
    assert abs(est["total"] - expected) < 1e-9
    assert est["basis"] == "wall x pods"


def test_node_based_class_is_refused_not_priced():
    est = costs.estimate_job_cost(_job(cls="Performance"), [], TABLE, REGION)
    assert "error" in est and "node-based" in est["error"]


def test_estimate_never_raises_on_bad_input():
    assert "error" in costs.estimate_job_cost(None, [], TABLE, REGION)


def test_fmt_dollars_keeps_subcent_accrual_visible():
    assert costs.fmt_dollars(6.634) == "$6.63"
    assert costs.fmt_dollars(0.0163) == "$0.016"  # 2dp would sit frozen for minutes


def test_committed_rates_load():
    table = rates.load()
    assert "us-east1" in table and "general-purpose" in table["us-east1"]
    assert table["us-east1"]["general-purpose"]["cpu_spot"] > 0
