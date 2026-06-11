"""Minimal ARM + Microsoft Graph REST plumbing (raw requests + Bearer).

No azure-mgmt SDK. ``arm_list`` follows ``nextLink`` pagination; ``arm_get``
fetches a single resource. Credentials are the dict produced by
``AZURE_PROVIDER.resolve_credentials`` — ``{arm_token, graph_token, tenant_id}``.
Network errors propagate so the engine records them per-domain in
``collector_status`` (it never aborts the whole run).
"""

from __future__ import annotations

import requests

ARM_BASE = "https://management.azure.com"
GRAPH_BASE = "https://graph.microsoft.com"
_TIMEOUT = 30


def _get(token, url, params=None):
    resp = requests.get(url, headers={"Authorization": f"Bearer {token}"}, params=params, timeout=_TIMEOUT)
    resp.raise_for_status()
    return resp.json()


def arm_get(creds, path, api_version, params=None) -> dict:
    p = dict(params or {})
    p["api-version"] = api_version
    return _get(creds["arm_token"], f"{ARM_BASE}{path}", p)


def arm_list(creds, path, api_version, params=None) -> list:
    """GET a list resource and follow ``nextLink`` (which already carries api-version)."""
    p = dict(params or {})
    p["api-version"] = api_version
    items: list = []
    data = _get(creds["arm_token"], f"{ARM_BASE}{path}", p)
    items += data.get("value", []) or []
    nxt = data.get("nextLink")
    while nxt:
        data = _get(creds["arm_token"], nxt)
        items += data.get("value", []) or []
        nxt = data.get("nextLink")
    return items


def arm_patch(creds, path, api_version, body) -> dict:
    p = {"api-version": api_version}
    resp = requests.patch(f"{ARM_BASE}{path}", params=p, json=body,
                          headers={"Authorization": f"Bearer {creds['arm_token']}",
                                   "Content-Type": "application/json"}, timeout=_TIMEOUT)
    resp.raise_for_status()
    return resp.json() if resp.text else {}


def graph_list(creds, path, params=None) -> list:
    token = creds.get("graph_token")
    if not token:
        return []
    items: list = []
    data = _get(token, f"{GRAPH_BASE}{path}", params)
    items += data.get("value", []) or []
    nxt = data.get("@odata.nextLink")
    while nxt:
        data = _get(token, nxt)
        items += data.get("value", []) or []
        nxt = data.get("@odata.nextLink")
    return items
