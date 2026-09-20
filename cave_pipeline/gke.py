"""Every fact that is true of GKE rather than of Kubernetes.

Google's domain strings, Autopilot's billing grid, its billing-catalog ids and its credentials
all live here, so the agnostic layers spell none of them. `kube` talks to a cluster, `costs`
does arithmetic, `manifest` builds objects; each imports a name from here rather than carrying
a vendor literal of its own.

Names other modules already export stay exported there: this owns the definition, not the
spelling every caller uses.
"""

from collections.abc import Callable

# --------------------------------------------------------------------------- domain strings

#: Node label marking preemptible capacity, and the taint that comes with it.
SPOT_LABEL = "cloud.google.com/gke-spot"

#: Node label selecting a compute class. Absent means the default class.
COMPUTE_CLASS_LABEL = "cloud.google.com/compute-class"

#: Annotation binding a Kubernetes service account to a Google one, which is Workload Identity.
GSA_ANNOTATION = "iam.gke.io/gcp-service-account"

SPOT_SELECTOR = {SPOT_LABEL: "true"}

SPOT_TOLERATION = {
    "key": SPOT_LABEL,
    "operator": "Equal",
    "value": "true",
    "effect": "NoSchedule",
}

#: Selectable built-ins; they take the spot label alongside the class, while a custom class
#: carries spot in its own priorities and GKE rejects a pod pinning both. The default class is
#: not here — it is requested by omitting the selector.
BUILTIN_COMPUTE_CLASSES = frozenset(
    {"Balanced", "Scale-Out", "Performance", "Accelerator"}
)

#: The default compute class, which an empty `compute_class` maps to.
GENERAL_CLASS = "general-purpose"

# --------------------------------------------------------------------------- autopilot billing

# The billing grid for the default class, per
# cloud.google.com/kubernetes-engine/docs/concepts/autopilot-resource-requests
CPU_STEP = 0.25  # non-bursting clusters round CPU requests UP to this
MEM_PER_CPU = (1.0, 6.5)  # billable memory:cpu window, GiB per vCPU
GP_MIN = (0.25, 0.5)  # smallest billable pod (vCPU, GiB)
GP_MAX = (30.0, 110.0)  # class ceiling; above needs a different compute class

#: Kubernetes Engine in the Cloud Billing Catalog API, and the published flat cluster fee.
BILLING_SERVICE = "CCD8-9BF1-090E"
CLUSTER_FEE_HR = 0.10

# --------------------------------------------------------------------------- credentials

#: What a token is minted for. GKE accepts no narrower scope.
SCOPES = ("https://www.googleapis.com/auth/cloud-platform",)


def token_source(key_path: str) -> Callable[[], str] | None:
    """A callable handing out a live bearer token for the service account in `key_path`.

    `None` when no key is configured, so a caller with nothing to offer changes nothing. The
    credential is built once and refreshed only once expired, since a token lasts about an hour
    and a run lasts longer.

    A kubeconfig exec plugin authenticates as whoever last ran `gcloud auth login`, and that is
    subject to reauth, which needs a TTY and a person to answer a challenge.
    """
    if not key_path:
        return None
    # Imported here rather than at module scope: `google-auth` arrives transitively with the
    # kubernetes client and is not declared, and `costs` imports this module for arithmetic that
    # must not depend on it.
    import google.auth.transport.requests as google_requests
    from google.oauth2 import service_account

    creds = service_account.Credentials.from_service_account_file(
        key_path, scopes=list(SCOPES)
    )
    request = google_requests.Request()

    def token() -> str:
        if not creds.valid:
            creds.refresh(request)
        return creds.token

    return token
