import requests
from urllib.parse import urlsplit

from src.config import settings


class APIError(Exception):
    def __init__(self, code, message, *, details=None, uncertain=False):
        super().__init__(message)
        self.code, self.message = code, message
        self.details, self.uncertain = details or {}, uncertain


class APIClient:
    def __init__(self, base_url=None, transport=None):
        self.base_url = (base_url or settings.api_base_url or f"http://127.0.0.1:{settings.api_port}/api/v1").rstrip("/")
        self.transport = transport if transport is not None else requests.Session()
        if transport is None and urlsplit(self.base_url).hostname in ("127.0.0.1", "localhost", "::1", "api"):
            self.transport.trust_env = False

    def request(self, method, path, *, data=None, params=None):
        try:
            response = self.transport.request(method, self.base_url + path, json=data, params=params,
                                              headers={"X-Operator-Token": settings.operator_token.get_secret_value()},
                                              timeout=(3, settings.analysis_timeout_seconds + 15))
        except requests.RequestException:
            raise APIError("REQUEST_UNKNOWN" if method == "POST" else "API_UNAVAILABLE",
                           "请求响应未收到，请核验原记录。" if method == "POST" else "API 尚未就绪或不可达。",
                           uncertain=method == "POST") from None
        try:
            body = response.json()
        except ValueError:
            raise APIError("RESPONSE_INVALID", "API 响应无法核验。", uncertain=method == "POST") from None
        if response.status_code >= 400:
            error = body.get("error", {})
            raise APIError(error.get("code", "API_ERROR"), error.get("message", "请求失败。"), details=error.get("details"))
        return body

    def get(self, path, **kwargs):
        return self.request("GET", path, **kwargs)

    def post(self, path, data):
        return self.request("POST", path, data=data)


def get_api_client():
    return APIClient()
