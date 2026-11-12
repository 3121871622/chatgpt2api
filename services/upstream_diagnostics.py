"""上游连通性自检：DNS、直连 TCP/TLS、当前生效代理。

用于排查「对话/绘画超时」。典型结论：

* ``chatgpt.com`` 只解析到 IPv6 → DNS 被污染，容器直连必然超时；
* 直连失败但已配置代理组 → 账号需要走代理（或把默认代理设为该组）；
* 直连与代理都失败 → 先修代理节点。
"""

from __future__ import annotations

import socket
import ssl
import time
from typing import Any

HOSTS = ("chatgpt.com", "auth.openai.com")


def _resolve(host: str) -> dict[str, Any]:
    began = time.perf_counter()
    try:
        infos = socket.getaddrinfo(host, 443, proto=socket.IPPROTO_TCP)
    except OSError as exc:
        return {
            "host": host,
            "ok": False,
            "error": f"{type(exc).__name__}: {exc}",
            "duration_ms": round((time.perf_counter() - began) * 1000, 1),
        }
    addresses = sorted({info[4][0] for info in infos})
    return {
        "host": host,
        "ok": True,
        "addresses": addresses,
        "ipv4": [item for item in addresses if ":" not in item],
        "ipv6": [item for item in addresses if ":" in item],
        "duration_ms": round((time.perf_counter() - began) * 1000, 1),
    }


def _tcp_tls(host: str, timeout: float = 8.0) -> dict[str, Any]:
    began = time.perf_counter()
    try:
        with socket.create_connection((host, 443), timeout=timeout) as sock:
            tcp_ms = round((time.perf_counter() - began) * 1000, 1)
            context = ssl.create_default_context()
            with context.wrap_socket(sock, server_hostname=host) as tls:
                return {
                    "host": host,
                    "ok": True,
                    "tcp_ms": tcp_ms,
                    "tls_ms": round((time.perf_counter() - began) * 1000, 1),
                    "tls_version": tls.version(),
                }
    except Exception as exc:  # noqa: BLE001 - 诊断结果本身就是错误信息
        return {
            "host": host,
            "ok": False,
            "error": f"{type(exc).__name__}: {exc}",
            "duration_ms": round((time.perf_counter() - began) * 1000, 1),
        }


CLOUDFLARE_IPV4_PREFIXES = (
    "104.",
    "172.64.",
    "172.65.",
    "172.66.",
    "172.67.",
    "188.114.",
)


def _looks_cloudflare(addresses: list[str]) -> bool:
    return any(address.startswith(CLOUDFLARE_IPV4_PREFIXES) for address in addresses)


def _build_advice(
    resolves: list[dict[str, Any]],
    direct: list[dict[str, Any]],
    proxy: dict[str, Any],
) -> list[str]:
    advice: list[str] = []
    suspicious = [
        item["host"]
        for item in resolves
        if item.get("ok") and item.get("ipv4") and not _looks_cloudflare(item["ipv4"])
    ]
    if suspicious:
        advice.append(
            "、".join(suspicious)
            + " 的 IPv4 不属于 Cloudflare 段，疑似 DNS 被污染；"
            "代理（socks5h）走远端解析不受影响，直连会失败"
        )
    direct_failures = [item["host"] for item in direct if not item.get("ok")]
    if direct_failures and proxy.get("has_proxy"):
        advice.append(
            "直连 " + "、".join(direct_failures) + " 失败；已配置代理组，"
            "请确认账号实际走代理（默认出口设为该组，或账号单独指定代理）"
        )
    elif direct_failures:
        advice.append(
            "直连 " + "、".join(direct_failures) + " 失败，且当前默认出口为直连：请配置可用代理组"
        )
    if proxy.get("has_proxy") and not proxy.get("available"):
        advice.append("已配置代理组但被判定不可用：请检查节点健康或重新探测")
    if not advice:
        advice.append("上游连通性正常")
    return advice


def diagnose() -> dict[str, Any]:
    resolves = [_resolve(host) for host in HOSTS]
    direct = [_tcp_tls(host) for host in HOSTS]

    proxy: dict[str, Any] = {}
    try:
        from services.proxy_management_service import proxy_management_service

        view = proxy_management_service.view()
        effective = view.effective_default
        proxy = {
            "source": effective.source,
            "label": effective.label,
            "has_proxy": effective.has_proxy,
            "available": effective.available,
        }
    except Exception as exc:  # noqa: BLE001
        proxy = {"error": f"{type(exc).__name__}: {exc}"}

    advice = _build_advice(resolves, direct, proxy)
    return {
        "checks": {"dns": resolves, "direct": direct, "proxy": proxy},
        "advice": advice,
    }
