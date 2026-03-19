from __future__ import annotations

import json
import re
import urllib.parse
from datetime import datetime
from pathlib import Path
from typing import Optional

from mitmproxy import http
from runtime_config import get_runtime_settings

OPENID_JSON_RE = re.compile(r'["\']openid["\']\s*:\s*["\']([^"\']+)["\']', re.IGNORECASE)


class OpenIDInterceptor:
    def __init__(self) -> None:
        runtime_settings = get_runtime_settings(reload=True)
        self.target_domain = runtime_settings.get("mitm.target_domain")
        self.output_path = runtime_settings.get("mitm.output_path")

    def response(self, flow: http.HTTPFlow) -> None:
        if self.target_domain and self.target_domain not in flow.request.pretty_host:
            return

        openid, source = self._extract_openid(flow)
        if not openid:
            return

        self._save_openid(openid, source, flow)

    def _extract_openid(self, flow: http.HTTPFlow) -> tuple[Optional[str], Optional[str]]:
        url_openid = self._extract_openid_from_url(flow.request.url)
        if url_openid:
            return url_openid, "url"

        json_openid = self._extract_openid_from_json_response(flow)
        if json_openid:
            return json_openid, "json"

        cookie_openid = self._extract_openid_from_cookies(flow)
        if cookie_openid:
            return cookie_openid, "set-cookie"

        return None, None

    def _extract_openid_from_url(self, url: str) -> Optional[str]:
        if "openid=" not in url:
            return None
        parsed_url = urllib.parse.urlparse(url)
        query_params = urllib.parse.parse_qs(parsed_url.query)
        values = query_params.get("openid")
        if not values:
            return None
        return values[0]

    def _extract_openid_from_json_response(self, flow: http.HTTPFlow) -> Optional[str]:
        content_type = flow.response.headers.get("Content-Type", "")
        if "application/json" not in content_type:
            return None

        try:
            text = flow.response.get_text()
        except Exception:
            return None

        match = OPENID_JSON_RE.search(text)
        if not match:
            return None
        return match.group(1)

    def _extract_openid_from_cookies(self, flow: http.HTTPFlow) -> Optional[str]:
        for cookie in flow.response.headers.get_all("Set-Cookie"):
            match = re.search(r"openid=([^;]+)", cookie)
            if match:
                return match.group(1)
        return None

    def _save_openid(self, openid: str, source: Optional[str], flow: http.HTTPFlow) -> None:
        payload = {
            "captured_at": datetime.now().astimezone().isoformat(),
            "openid": openid,
            "source": source,
            "url": flow.request.url,
            "host": flow.request.pretty_host,
        }

        self.output_path.parent.mkdir(parents=True, exist_ok=True)
        with self.output_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(payload, ensure_ascii=False) + "\n")

        print(f"[mitmproxy] captured openid from {source}: {openid}")


addons = [OpenIDInterceptor()]
