import requests
import time


class APIConnector:
    def __init__(self, config, context=None):
        self.config = config
        self.context = context or {}

    def _render(self, value):
        if isinstance(value, str):
            for k, v in self.context.items():
                value = value.replace(f"{{{{{k}}}}}", str(v))
        return value

    def _build_headers(self):
        headers = {}
        for k, v in self.config["request"].get("headers", {}).items():
            headers[k] = self._render(v)

        auth = self.config.get("auth", {})
        auth_type = auth.get("type")

        if auth_type == "bearer":
            headers["Authorization"] = f"Bearer {self._render(auth['token'])}"

        elif auth_type == "api_key":
            headers[auth["key_name"]] = self._render(auth["key_value"])

        elif auth_type == "basic":
            headers["Authorization"] = requests.auth._basic_auth_str(
                auth["username"], auth["password"]
            )

        return headers

    def execute(self):
        req = self.config["request"]

        url = self._render(req["url"])
        method = req.get("method", "GET").upper()
        body = req.get("body")
        params = req.get("query_params")

        headers = self._build_headers()

        timeout = self.config.get("timeout", 10)
        retry_cfg = self.config.get("retry", {})
        retries = retry_cfg.get("count", 1)
        backoff = retry_cfg.get("backoff", 1)

        last_error = None

        for attempt in range(retries):
            try:
                response = requests.request(
                    method=method,
                    url=url,
                    headers=headers,
                    json=body,
                    params=params,
                    timeout=timeout,
                )

                return {
                    "success": response.ok,
                    "status_code": response.status_code,
                    "response": response.json() if response.content else {},
                    "headers": dict(response.headers),
                }

            except Exception as e:
                last_error = str(e)
                time.sleep(backoff**attempt)

        return {"success": False, "error": last_error}
