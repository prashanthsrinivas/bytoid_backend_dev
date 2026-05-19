import json
import time
import requests
import botocore.auth
import botocore.awsrequest
import botocore.credentials
from requests.auth import HTTPBasicAuth


class APIConnector:
    def __init__(self, userid, config, context=None):
        self.config = config
        self.context = context or {}
        self.userid = userid

    # -------------------------
    # Utils
    # -------------------------
    def _render(self, value):
        if isinstance(value, str):
            for k, v in self.context.items():
                value = value.replace(f"{{{{{k}}}}}", str(v))
        return value

    def _validate_auth(self, auth):
        auth_type = auth.get("type")

        if auth_type == "bearer" and "token" not in auth:
            raise ValueError("Bearer token missing")

        if auth_type == "api_key" and not all(
            k in auth for k in ("key_name", "key_value")
        ):
            raise ValueError("API key config invalid")

        if auth_type == "basic" and not all(
            k in auth for k in ("username", "password")
        ):
            raise ValueError("Basic auth config invalid")

        if auth_type == "oauth2" and not all(
            k in auth for k in ("client_id", "client_secret", "token_url")
        ):
            raise ValueError("OAuth2 config invalid")

        if auth_type == "aws_sigv4" and not all(
            k in auth for k in ("access_key_id", "secret_access_key", "region", "service")
        ):
            raise ValueError("AWS SigV4 auth requires: access_key_id, secret_access_key, region, service")

        if auth_type == "azure_oauth" and "access_token" not in auth:
            raise ValueError("Azure OAuth auth requires: access_token")

    # -------------------------
    # OAuth2
    # -------------------------
    def _get_oauth2_token(self, auth):
        response = requests.post(
            auth["token_url"],
            data={
                "grant_type": "client_credentials",
                "client_id": auth["client_id"],
                "client_secret": auth["client_secret"],
            },
            timeout=10,
        )
        response.raise_for_status()
        return response.json()["access_token"]

    # -------------------------
    # Headers
    # -------------------------
    def _build_headers(self):
        headers = {
            "User-Agent": "Mozilla/5.0 (APIConnector)",
            "Accept": "application/json",
            "Content-Type": "application/json",
        }

        req_headers = self.config["request"].get("headers", {})
        for k, v in req_headers.items():
            headers[k] = self._render(v)

        auth = self.config.get("auth", {})
        auth_type = auth.get("type")

        if auth_type == "bearer":
            headers["Authorization"] = f"Bearer {self._render(auth['token'])}"

        elif auth_type == "api_key":
            headers[auth["key_name"]] = self._render(auth["key_value"])

        elif auth_type == "oauth2":
            token = self._get_oauth2_token(auth)
            headers["Authorization"] = f"Bearer {token}"

        elif auth_type == "azure_oauth":
            headers["Authorization"] = f"Bearer {self._render(auth['access_token'])}"

        return headers

    # -------------------------
    # Execute
    # -------------------------
    def execute(self):
        req = self.config["request"]
        auth = self.config.get("auth", {})

        self._validate_auth(auth)

        url = self._render(req["url"])
        method = req.get("method", "GET").upper()
        body = req.get("body")
        params = req.get("query_params")

        headers = self._build_headers()

        timeout = self.config.get("timeout", 10)
        retry_cfg = self.config.get("retry", {})
        retries = retry_cfg.get("count", 1)
        backoff = retry_cfg.get("backoff", 1)

        auth_obj = None
        if auth.get("type") == "basic":
            auth_obj = HTTPBasicAuth(auth["username"], auth["password"])

        if auth.get("type") == "aws_sigv4":
            creds = botocore.credentials.Credentials(
                access_key=auth["access_key_id"],
                secret_key=auth["secret_access_key"],
                token=auth.get("session_token"),
            )
            aws_req = botocore.awsrequest.AWSRequest(
                method=method,
                url=url,
                headers=headers,
                data=json.dumps(body) if body else None,
            )
            botocore.auth.SigV4Auth(creds, auth["service"], auth["region"]).add_auth(aws_req)
            headers = dict(aws_req.headers)

        last_error = None

        for attempt in range(retries):
            try:
                response = requests.request(
                    method=method,
                    url=url,
                    headers=headers,
                    json=body,
                    params=params,
                    auth=auth_obj,
                    timeout=timeout,
                )

                try:
                    response_body = response.json()
                except ValueError:
                    response_body = response.text

                return {
                    "success": response.ok,
                    "status_code": response.status_code,
                    "response": response_body,
                }

            except Exception as e:
                last_error = str(e)
                time.sleep(backoff**attempt)

        return {"success": False, "error": last_error}
