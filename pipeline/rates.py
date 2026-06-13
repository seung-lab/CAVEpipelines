"""GKE Autopilot per-(region, compute-class) rate table, read offline from rates.csv.

rates.csv is the source of truth, generated from the public Cloud Billing Catalog API —
refreshed by CI (.github/workflows/update-rates.yml) or `python -m pipeline.rates`
(needs GCP_BILLING_API_KEY). Pod-based classes (general-purpose / Balanced / Scale-Out)
each have their own rate; node-based classes (Performance / GPU) bill per VM and are absent here.
"""

import csv
import json
import os
import sys
import urllib.request

_CSV = os.path.join(os.path.dirname(__file__), "rates.csv")
_RATE_FIELDS = [
    "cpu_on_demand",
    "cpu_spot",
    "mem_on_demand",
    "mem_spot",
    "cluster_fee_hr",
]
_GKE_SERVICE = "CCD8-9BF1-090E"  # Kubernetes Engine (Cloud Billing Catalog API)
_CLUSTER_FEE_HR = 0.10  # flat Autopilot cluster-management fee (published constant)


def load() -> dict:
    """{region: {compute_class: {cpu_on_demand, cpu_spot, mem_on_demand, mem_spot, cluster_fee_hr}}}."""
    table = {}
    with open(_CSV) as stream:
        for row in csv.DictReader(stream):
            rates = {k: float(row[k]) for k in _RATE_FIELDS}
            table.setdefault(row["region"], {})[row["compute_class"]] = rates
    return table


def _skus(api_key: str):
    base = f"https://cloudbilling.googleapis.com/v1/services/{_GKE_SERVICE}/skus?key={api_key}"
    url = base
    while url:
        with urllib.request.urlopen(url) as resp:
            page = json.load(resp)
        yield from page.get("skus", [])
        token = page.get("nextPageToken")
        url = f"{base}&pageToken={token}" if token else None


def _unit_price(sku) -> float:
    tier = sku["pricingInfo"][0]["pricingExpression"]["tieredRates"][-1]["unitPrice"]
    return int(tier.get("units", 0)) + tier.get("nanos", 0) / 1e9


def _compute_class(desc: str):
    """Map an Autopilot Pod SKU description to its compute class (None = not modeled)."""
    if "Arm" in desc:  # 'Scale-Out Arm' SKUs must not pollute the x86 Scale-Out rows
        return None
    if "Balanced" in desc:
        return "Balanced"
    if "Scale-Out" in desc:
        return "Scale-Out"
    return "general-purpose"


def fetch(api_key: str) -> dict:
    """Per-(region, class) table from the 'Autopilot [Class ][Spot ]Pod {mCPU,Memory} Requests' SKUs."""
    raw = {}  # {region: {class: {field: price}}}
    for sku in _skus(api_key):
        desc = sku.get("description", "")
        if "Autopilot" not in desc or "Pod" not in desc or "Requests" not in desc:
            continue
        if "mCPU" in desc:
            dim, price = "cpu", _unit_price(sku) * 1000  # per mCPU-hour -> per vCPU-hour
        elif "Memory" in desc:
            dim, price = "mem", _unit_price(sku)  # per GiB-hour
        else:
            continue  # ephemeral storage etc.
        cls = _compute_class(desc)
        if cls is None:
            continue
        spot = sku.get("category", {}).get("usageType") == "Preemptible"
        field = f"{dim}_{'spot' if spot else 'on_demand'}"
        for region in sku.get("serviceRegions", []):
            row = raw.setdefault(region, {}).setdefault(
                cls, {"cluster_fee_hr": _CLUSTER_FEE_HR}
            )
            row[field] = round(price, 8)
    table = {}
    for region, classes in raw.items():
        for cls, row in classes.items():
            if all(k in row for k in _RATE_FIELDS):
                table.setdefault(region, {})[cls] = row
    return table


def main():
    key = os.environ.get("GCP_BILLING_API_KEY")
    if not key:
        sys.exit("set GCP_BILLING_API_KEY (a Cloud Billing Catalog API key)")
    table = fetch(key)
    if not table:
        sys.exit("no Autopilot pod SKUs returned")
    with open(_CSV, "w", newline="") as stream:
        writer = csv.writer(stream)
        writer.writerow(["region", "compute_class"] + _RATE_FIELDS)
        for region in sorted(table):
            for cls in sorted(table[region]):
                writer.writerow(
                    [region, cls] + [table[region][cls][k] for k in _RATE_FIELDS]
                )
    print(f"wrote {sum(len(v) for v in table.values())} rows ({len(table)} regions)")


if __name__ == "__main__":
    main()
