from __future__ import annotations

import hashlib
import hmac
from datetime import UTC, datetime
from urllib.parse import quote, urlencode

from curl_cffi import requests


class R2Error(RuntimeError):
    """Raised when a Cloudflare R2 request cannot be completed."""


def _clean(value: object) -> str:
    return str(value or "").strip()


def _sha256_hex(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _hmac_sha256(key: bytes, message: str) -> bytes:
    return hmac.new(key, message.encode("utf-8"), hashlib.sha256).digest()


class CloudflareR2Client:
    """Small S3-compatible client shared by image storage and backups."""

    def __init__(self, settings: dict[str, object], *, timeout: float = 60.0) -> None:
        self.account_id = _clean(settings.get("account_id"))
        self.access_key_id = _clean(settings.get("access_key_id"))
        self.secret_access_key = _clean(settings.get("secret_access_key"))
        self.bucket = _clean(settings.get("bucket"))
        self.prefix = _clean(settings.get("prefix"))
        self.timeout = timeout
        self.session = requests.Session(impersonate="chrome", verify=True)

    def validate(self) -> None:
        missing = []
        if not self.account_id:
            missing.append("Account ID")
        if not self.access_key_id:
            missing.append("Access Key ID")
        if not self.secret_access_key:
            missing.append("Secret Access Key")
        if not self.bucket:
            missing.append("Bucket")
        if missing:
            raise R2Error(f"R2 配置不完整：缺少 {'、'.join(missing)}")

    @property
    def endpoint(self) -> str:
        return f"https://{self.account_id}.r2.cloudflarestorage.com"

    def _aws_v4_headers(
        self,
        method: str,
        path: str,
        *,
        query: dict[str, str] | None = None,
        body: bytes = b"",
        extra_headers: dict[str, str] | None = None,
    ) -> tuple[str, dict[str, str]]:
        now = datetime.now(UTC)
        amz_date = now.strftime("%Y%m%dT%H%M%SZ")
        date_stamp = now.strftime("%Y%m%d")
        encoded_query = urlencode(sorted((query or {}).items()))
        payload_hash = _sha256_hex(body)
        headers = {
            "host": f"{self.account_id}.r2.cloudflarestorage.com",
            "x-amz-content-sha256": payload_hash,
            "x-amz-date": amz_date,
        }
        for key, value in (extra_headers or {}).items():
            headers[key.lower()] = str(value).strip()
        sorted_items = sorted(
            (key.lower(), " ".join(str(value).strip().split()))
            for key, value in headers.items()
        )
        canonical_headers = "".join(f"{key}:{value}\n" for key, value in sorted_items)
        signed_headers = ";".join(key for key, _ in sorted_items)
        canonical_request = "\n".join(
            [method.upper(), path, encoded_query, canonical_headers, signed_headers, payload_hash]
        )
        credential_scope = f"{date_stamp}/auto/s3/aws4_request"
        string_to_sign = "\n".join(
            [
                "AWS4-HMAC-SHA256",
                amz_date,
                credential_scope,
                _sha256_hex(canonical_request.encode("utf-8")),
            ]
        )
        k_date = _hmac_sha256(("AWS4" + self.secret_access_key).encode("utf-8"), date_stamp)
        k_region = hmac.new(k_date, b"auto", hashlib.sha256).digest()
        k_service = hmac.new(k_region, b"s3", hashlib.sha256).digest()
        k_signing = hmac.new(k_service, b"aws4_request", hashlib.sha256).digest()
        signature = hmac.new(k_signing, string_to_sign.encode("utf-8"), hashlib.sha256).hexdigest()
        headers["authorization"] = (
            "AWS4-HMAC-SHA256 "
            f"Credential={self.access_key_id}/{credential_scope}, "
            f"SignedHeaders={signed_headers}, Signature={signature}"
        )
        return encoded_query, headers

    def _request(
        self,
        method: str,
        key: str = "",
        *,
        query: dict[str, str] | None = None,
        body: bytes = b"",
        extra_headers: dict[str, str] | None = None,
        timeout: float | None = None,
    ):
        self.validate()
        object_path = f"/{self.bucket}"
        if key:
            object_path += f"/{quote(key.lstrip('/'), safe='/')}"
        encoded_query, headers = self._aws_v4_headers(
            method,
            object_path,
            query=query,
            body=body,
            extra_headers=extra_headers,
        )
        url = f"{self.endpoint}{object_path}"
        if encoded_query:
            url += f"?{encoded_query}"
        return self.session.request(
            method.upper(),
            url,
            headers=headers,
            data=body,
            timeout=self.timeout if timeout is None else timeout,
        )

    def test_connection(self) -> dict[str, object]:
        response = self._request("GET", query={"list-type": "2", "max-keys": "1"}, timeout=30.0)
        if response.status_code >= 400:
            raise R2Error(f"连接 R2 失败：HTTP {response.status_code}")
        return {"ok": True, "status": int(response.status_code)}

    def upload_bytes(
        self,
        key: str,
        payload: bytes,
        *,
        content_type: str = "application/octet-stream",
        metadata: dict[str, str] | None = None,
    ) -> dict[str, object]:
        headers = {"content-type": content_type}
        for item_key, item_value in (metadata or {}).items():
            headers[f"x-amz-meta-{item_key}"] = str(item_value)
        response = self._request("PUT", key, body=payload, extra_headers=headers)
        if response.status_code >= 400:
            raise R2Error(f"上传 R2 对象失败：HTTP {response.status_code}")
        return {"key": key, "etag": str(response.headers.get("etag") or "").strip('"')}

    def get_bytes(self, key: str, *, max_bytes: int = 64 * 1024 * 1024) -> bytes:
        response = self._request("GET", key, timeout=60.0)
        if response.status_code >= 400:
            raise R2Error(f"读取 R2 对象失败：HTTP {response.status_code}")
        payload = bytes(response.content)
        if len(payload) > max_bytes:
            raise R2Error("R2 对象超过允许的大小")
        return payload

    def head_object(self, key: str) -> dict[str, object]:
        response = self._request("HEAD", key, body=b"", timeout=30.0)
        if response.status_code >= 400:
            raise R2Error(f"检查 R2 对象失败：HTTP {response.status_code}")
        return {
            "key": key,
            "size": int(response.headers.get("content-length") or 0),
            "etag": str(response.headers.get("etag") or "").strip('"'),
        }

    def delete_object(self, key: str) -> None:
        response = self._request("DELETE", key, timeout=30.0)
        if response.status_code >= 400 and response.status_code != 404:
            raise R2Error(f"删除 R2 对象失败：HTTP {response.status_code}")

    def list_objects(self, *, prefix: str = "") -> list[dict[str, object]]:
        items: list[dict[str, object]] = []
        continuation = ""
        while True:
            query = {"list-type": "2", "max-keys": "1000"}
            if prefix:
                query["prefix"] = prefix
            if continuation:
                query["continuation-token"] = continuation
            response = self._request("GET", query=query, timeout=30.0)
            if response.status_code >= 400:
                raise R2Error(f"获取 R2 对象列表失败：HTTP {response.status_code}")
            body = response.text
            for block in body.split("<Contents>")[1:]:
                key = _clean(block.split("<Key>", 1)[1].split("</Key>", 1)[0]) if "<Key>" in block else ""
                if not key:
                    continue
                size_text = _clean(block.split("<Size>", 1)[1].split("</Size>", 1)[0]) if "<Size>" in block else "0"
                updated = _clean(block.split("<LastModified>", 1)[1].split("</LastModified>", 1)[0]) if "<LastModified>" in block else ""
                items.append({"key": key, "size": int(size_text or 0), "updated_at": updated})
            if "<IsTruncated>true</IsTruncated>" not in body or "<NextContinuationToken>" not in body:
                break
            continuation = _clean(body.split("<NextContinuationToken>", 1)[1].split("</NextContinuationToken>", 1)[0])
            if not continuation:
                break
        items.sort(key=lambda item: str(item.get("updated_at") or ""), reverse=True)
        return items

    def close(self) -> None:
        self.session.close()
