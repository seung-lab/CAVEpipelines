"""Fill the node a pod request already forces, so quota is not held idle.

Autopilot bills the pod's request, but Compute Engine quota is charged on the whole node
vCPU: a 16-vCPU pod alone on a 32-vCPU node holds 2.00 node-vCPU per vCPU of work. Sizing
here is about that ratio only — pricing lives in costs.py.
"""

import math

from .costs import CPU_STEP

# vCPU rungs the pod-billed classes provision on: E2 (2-32) and T2D (1-60).
NODE_SIZES = (1, 2, 4, 8, 16, 32, 48, 60)
# vCPU of DaemonSets per node, held back when filling one. Deliberately over-stated
# (measured 0.553) and used only to shrink the grown request, never to pick the rung.
NODE_OVERHEAD = 1.0
# GiB per vCPU on the *standard* shapes (4 GB/vCPU nominal, 3.63 measured on
# e2-standard-32). A gate, not a capacity: above this the machine is chosen by memory and
# may be a highmem shape, so the cpu-side rung arithmetic below does not describe it.
STD_MEM_PER_CPU = 3.5


def reserved_cpu(vcpu: float) -> float:
    """GKE's node-allocatable CPU reservation: 6% of the first core, 1% of the second,
    0.5% of cores 3-4, 0.25% above 4."""
    reserved, left = 0.0, vcpu
    for cores, rate in ((1, 0.06), (1, 0.01), (2, 0.005)):
        reserved += min(left, cores) * rate
        left = max(0.0, left - cores)
    return reserved + left * 0.0025


def fill_node(
    cpu: float, mem: float, overhead: float = NODE_OVERHEAD, max_cpu: float = 0.0
) -> tuple:
    """Grow (cpu, mem) to fill the smallest rung that already had to hold them.

    Availability is untouched: every rung that fits the grown value is one that fit the
    original, so the set of shapes GKE may schedule on is unchanged. The rung is picked on
    allocatable, which is exact, and `overhead` only shrinks the grown value — over-stating
    it costs headroom and can never push the pod up a rung.

    `max_cpu` (0 = none) is the class ceiling: filling past it is undone by the caller's
    clamp, which then warns about a value nobody wrote. A curve already above it is left
    alone, so that clamp still reports the operator's own mistake.

    Memory scales by the same factor to hold GiB-per-worker fixed, since layer_processes
    tracks billed cpu. Requests above STD_MEM_PER_CPU are left alone: memory picks the
    machine there, and a highmem rung has a different vCPU ladder than the one modelled.
    """
    if cpu <= 0 or mem > cpu * STD_MEM_PER_CPU:
        return cpu, mem
    for size in NODE_SIZES:
        if size - reserved_cpu(size) >= cpu:
            usable = size - reserved_cpu(size) - overhead
            if max_cpu:
                usable = min(usable, max_cpu)
            grown = math.floor(round(usable / CPU_STEP, 6)) * CPU_STEP
            return (grown, mem * grown / cpu) if grown > cpu else (cpu, mem)
    return cpu, mem  # larger than any rung modelled; the class ceiling handles it
