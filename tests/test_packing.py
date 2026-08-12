from cave_pipeline import packing


def test_reserved_cpu_matches_observed_allocatable():
    # measured on the cluster: e2-standard-4 3920m, ek-standard-8 7910m, e2-standard-32 31850m
    for vcpu, allocatable in ((2, 1.93), (4, 3.92), (8, 7.91), (16, 15.89), (32, 31.85)):
        assert round(vcpu - packing.reserved_cpu(vcpu), 3) == allocatable


def test_fill_node_grows_into_the_rung_already_forced():
    # 16 vCPU forces a 32-vCPU rung and uses half of it; grow into that same rung
    cpu, mem = packing.fill_node(16, 32)
    assert cpu == 30.75 and mem == 61.5  # memory scales with cpu: GiB/worker unchanged
    assert packing.fill_node(4, 8)[0] == 6.75  # forces an 8, fills it
    assert packing.fill_node(2, 4)[0] == 2.75  # forces a 4, fills it


def test_fill_node_never_crosses_into_a_larger_rung():
    # the grown value must still fit the rung the original forced, or availability shrinks
    for cpu in (0.5, 1, 2, 3, 4, 8, 12, 16, 24, 30):
        grown, _ = packing.fill_node(cpu, cpu * 2)
        rung = next(n for n in packing.NODE_SIZES if n - packing.reserved_cpu(n) >= cpu)
        assert cpu <= grown <= rung - packing.reserved_cpu(rung)


def test_fill_node_stops_at_the_class_ceiling():
    """Filling past the ceiling is undone by normalize_requests, which then warns about a
    value nobody wrote — and the pod lands under the rung anyway."""
    assert packing.fill_node(16, 32, max_cpu=30.0)[0] == 30.0  # 30.75 without the cap
    assert packing.fill_node(8, 16, max_cpu=30.0)[0] == 14.75  # below it: unaffected
    # a curve already over the ceiling is left for normalize_requests to clamp and report
    assert packing.fill_node(40, 80, max_cpu=30.0) == (40, 80)


def test_fill_node_ceiling_keeps_memory_proportional():
    cpu, mem = packing.fill_node(16, 32, max_cpu=30.0)
    assert cpu == 30.0 and mem == 60.0  # GiB per worker unchanged by the cap


def test_fill_node_leaves_memory_bound_requests_alone():
    # above 3.5 GiB/vCPU the machine is picked by memory and may be a highmem shape,
    # whose vCPU ladder this module does not model
    assert packing.fill_node(2, 96) == (2, 96)
    assert packing.fill_node(30, 110) == (30, 110)


def test_fill_node_leaves_small_and_oversized_asks_alone():
    # a conservative NODE_OVERHEAD must never push a 1-vCPU pod up onto a 4-vCPU rung
    assert packing.fill_node(1, 2) == (1, 2)
    assert packing.fill_node(0, 2) == (0, 2)  # no division by zero
    assert packing.fill_node(90, 180) == (90, 180)  # beyond every modelled rung
