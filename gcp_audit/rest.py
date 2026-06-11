"""Minimal GCP REST plumbing (raw requests + a cloud-platform Bearer).

Credentials are ``{access_token, project_id, organization_id}`` from
``GCP_PROVIDER.resolve_credentials``. ``list_items`` follows ``nextPageToken``.
Network errors propagate so the engine records them per-domain in
``collector_status``.
"""

from __future__ import annotations

import requests

_TIMEOUT = 30

CRM_V1 = "https://cloudresourcemanager.googleapis.com/v1"
COMPUTE_V1 = "https://compute.googleapis.com/compute/v1"
STORAGE_V1 = "https://storage.googleapis.com/storage/v1"
SQLADMIN_V1 = "https://sqladmin.googleapis.com/v1"
IAM_V1 = "https://iam.googleapis.com/v1"
LOGGING_V2 = "https://logging.googleapis.com/v2"


def get(creds, url, params=None) -> dict:
    resp = requests.get(url, headers={"Authorization": f"Bearer {creds['access_token']}"},
                        params=params, timeout=_TIMEOUT)
    resp.raise_for_status()
    return resp.json()


def post(creds, url, body=None) -> dict:
    resp = requests.post(url, headers={"Authorization": f"Bearer {creds['access_token']}",
                                       "Content-Type": "application/json"},
                         json=body or {}, timeout=_TIMEOUT)
    resp.raise_for_status()
    return resp.json() if resp.text else {}


def list_items(creds, url, item_key="items", params=None) -> list:
    out: list = []
    p = dict(params or {})
    while True:
        data = get(creds, url, p)
        out += data.get(item_key, []) or []
        tok = data.get("nextPageToken")
        if not tok:
            break
        p["pageToken"] = tok
    return out
