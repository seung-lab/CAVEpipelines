"""GKE Autopilot per-region rate table, read offline from rates.csv.

rates.csv is the source of truth; the committed values are a us-central1 baseline bootstrap.
Regenerate the real per-region rates from the public Cloud Billing Catalog API via CI
(.github/workflows/update-rates.yml) or `python -m pipeline.rates` (needs GCP_BILLING_API_KEY).
Rates cover the pod-based compute classes (default / Balanced / Scale-Out share one
general-purpose rate); node-based classes (Performance / GPU) bill per VM and are not priced here.
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
    """{region: {cpu_on_demand, cpu_spot, mem_on_demand, mem_spot, cluster_fee_hr}} (floats)."""
    with open(_CSV) as stream:
        return {
            row["region"]: {k: float(row[k]) for k in _RATE_FIELDS}
            for row in csv.DictReader(stream)
        }


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


def fetch(api_key: str) -> dict:
    """Per-region table from the Catalog API: Autopilot Pod {mCPU,Memory} Requests SKUs."""
    table = {}
    for sku in _skus(api_key):
        desc = sku.get("description", "")
        if "Autopilot" not in desc or "Pod" not in desc or "Requests" not in desc:
            continue
        spot = sku.get("category", {}).get("usageType") == "Preemptible"
        if "mCPU" in desc:
            field = "cpu_spot" if spot else "cpu_on_demand"
            price = _unit_price(sku) * 1000  # per mCPU-hour -> per vCPU-hour
        elif "Memory" in desc:
            field = "mem_spot" if spot else "mem_on_demand"
            price = _unit_price(sku)  # per GiB-hour
        else:
            continue
        for region in sku.get("serviceRegions", []):
            row = table.setdefault(region, {"cluster_fee_hr": _CLUSTER_FEE_HR})
            row[field] = round(price, 6)
    return {r: v for r, v in table.items() if len(v) == len(_RATE_FIELDS)}


def main():
    key = os.environ.get("GCP_BILLING_API_KEY")
    if not key:
        sys.exit("set GCP_BILLING_API_KEY (a Cloud Billing Catalog API key)")
    table = fetch(key)
    if not table:
        sys.exit("no Autopilot pod SKUs returned")
    with open(_CSV, "w", newline="") as stream:
        writer = csv.writer(stream)
        writer.writerow(["region"] + _RATE_FIELDS)
        for region in sorted(table):
            writer.writerow([region] + [table[region][k] for k in _RATE_FIELDS])
    print(f"wrote {len(table)} regions to {_CSV}")


if __name__ == "__main__":
    main()
