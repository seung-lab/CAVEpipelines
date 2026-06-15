from types import SimpleNamespace

from cave_pipeline import costs, rates

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
HOUR = 3600.0


def _job_row(cpu=1.0, mem=2.0, succeeded=0, active=0, start=0.0, end=None, workers=0):
    return SimpleNamespace(
        cpu_req=cpu,
        mem_req=mem,
        succeeded=succeeded,
        active=active,
        started_at=start,
        finished_at=end,
        compute_class="",
        parallelism=workers,
    )


def _pod_row(start, end, phase="Succeeded", cpu=1.0, mem=2.0):
    return SimpleNamespace(
        started_at=start,
        finished_at=end,
        phase=phase,
        cpu_req=cpu,
        mem_req=mem,
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


def test_observed_usage_prices_to_formula():
    usage = costs.usage_from_rows(
        _job_row(succeeded=2), [_pod_row(0, HOUR), _pod_row(0, HOUR)], now=2 * HOUR
    )
    assert usage["basis"] == "observed"
    priced = costs.price_usage(usage, GP)
    assert abs(priced["total"] - 2 * (1 * GP["cpu_spot"] + 2 * GP["mem_spot"])) < 1e-9


def test_live_pods_accrue_between_reads():
    job = _job_row(active=1)
    pods = [_pod_row(0, None, phase="Running")]
    early = costs.usage_from_rows(job, pods, now=HOUR)["pod_hours"]
    later = costs.usage_from_rows(job, pods, now=2 * HOUR)["pod_hours"]
    assert later > early  # a re-read while pods run keeps growing


def test_backfill_covers_pods_lost_before_first_sample():
    usage = costs.usage_from_rows(
        _job_row(succeeded=5), [_pod_row(0, HOUR), _pod_row(0, HOUR)], now=2 * HOUR
    )
    assert usage["basis"] == "observed+backfill"
    assert abs(usage["pod_hours"] - 5.0) < 1e-9  # 2 seen + 3 x mean(1h)


def test_wall_fallback_when_nothing_recorded():
    usage = costs.usage_from_rows(
        _job_row(succeeded=3, start=0.0, end=2 * HOUR), [], now=3 * HOUR
    )
    assert usage["basis"] == "wall"
    assert abs(usage["pod_hours"] - 6.0) < 1e-9


def test_wall_fallback_caps_workers_at_parallelism():
    # 10 tasks through 2 workers run back to back, not 10-wide: wall x 2, not x 10
    usage = costs.usage_from_rows(
        _job_row(succeeded=10, start=0.0, end=2 * HOUR, workers=2), [], now=3 * HOUR
    )
    assert abs(usage["pod_hours"] - 4.0) < 1e-9


def test_gone_pods_consume_completions_like_succeeded():
    # one pod GC'd after finishing ('Gone'): it already accrued observed hours,
    # so the backfill must not bill its completion a second time
    pods = [_pod_row(0, HOUR), _pod_row(0, HOUR, phase="Gone")]
    usage = costs.usage_from_rows(_job_row(succeeded=2), pods, now=2 * HOUR)
    assert usage["basis"] == "observed"
    assert abs(usage["pod_hours"] - 2.0) < 1e-9


def test_fee_charged_over_union_not_per_layer():
    rows = [_job_row(start=0.0, end=2 * HOUR), _job_row(start=HOUR, end=3 * HOUR)]
    fee = costs.fee(TABLE, REGION, rows, now=4 * HOUR)
    assert abs(fee - 3 * GP["cluster_fee_hr"]) < 1e-9  # 3h union, overlap not double


def test_balanced_class_costs_more_than_default():
    usage = {"cpu_hours": 1.0, "mem_gib_hours": 2.0, "pod_hours": 1.0, "basis": "x"}
    assert (
        costs.price_usage(usage, BALANCED)["total"]
        > costs.price_usage(usage, GP)["total"]
    )


def test_normalize_requests_snaps_to_billing_grid():
    cpu, mem, warns, errs = costs.normalize_requests(4, 2)  # below 1 GiB/vCPU floor
    assert (cpu, mem) == (4, 4) and warns and not errs
    cpu, _, warns, _ = costs.normalize_requests(1, 13)  # above 6.5 GiB/vCPU ceiling
    assert cpu == 2.0 and any("ceiling" in w for w in warns)
    _, _, warns, errs = costs.normalize_requests(1.3, 2)  # off the 0.25-vCPU step
    assert any("step" in w for w in warns) and not errs
    *_, errs = costs.normalize_requests(40, 40)
    assert errs and "compute_class" in errs[0]  # past the general-purpose ceiling
    assert costs.normalize_requests(40, 40, "Balanced") == (40, 40, [], [])


def test_fmt_dollars_keeps_subcent_accrual_visible():
    assert costs.fmt_dollars(6.634) == "$6.63"
    assert costs.fmt_dollars(0.0163) == "$0.016"  # 2dp would sit frozen for minutes


def test_committed_rates_load():
    table = rates.load()
    assert "us-east1" in table and "general-purpose" in table["us-east1"]
    assert table["us-east1"]["general-purpose"]["cpu_spot"] > 0
