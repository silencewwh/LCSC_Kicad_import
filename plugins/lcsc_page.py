"""Conservative LCSC address handling, independent of wxPython and KiCad."""

from __future__ import annotations

import re
from urllib.parse import SplitResult, unquote, urlsplit, urlunsplit

LCSC_HOME = "https://www.szlcsc.com/"
_STORE_DOMAINS = ("lcsc.com", "lcsc.cn", "szlcsc.com")
_COMPONENT_ID = re.compile(r"C[1-9][0-9]*", re.IGNORECASE | re.ASCII)
_PRODUCT_PATH = re.compile(
    r"/product-detail/(?:[^/]+/)*(?:[^/]*_)?(C[1-9][0-9]*)\.html", re.IGNORECASE | re.ASCII
)
_ITEM_PATH = re.compile(
    r"/item/(?:[^/]+/)*(?:[^/]*_)?(C[1-9][0-9]*)\.html", re.IGNORECASE | re.ASCII
)


def normalize_component_id(text: str) -> str | None:
    """Return an uppercase ID only for a complete, standalone C-number."""
    value = text.strip()
    return value.upper() if _COMPONENT_ID.fullmatch(value) else None


def _validated_url(value: str) -> SplitResult:
    # urlsplit silently strips some controls; reject those before parsing.
    if (
        not value
        or re.search(r"[\x00-\x20\x7f\\]", value)
        or re.search(r"%(?![0-9a-fA-F]{2})", value)
    ):
        raise ValueError("网址含有空格、控制字符或无效编码，请重新输入。")
    try:
        parts = urlsplit(value)
        host = parts.hostname or ""
        port = parts.port
        # Also check percent-encoded bytes so invalid UTF-8 cannot become an ID.
        decoded = unquote(value, encoding="utf-8", errors="strict")
        decoded.encode("utf-8", errors="strict")
    except (ValueError, UnicodeError) as exc:
        raise ValueError("网址格式或编码无效，请输入立创商城网址。") from exc
    if (
        parts.scheme.lower() not in ("http", "https")
        or parts.username is not None
        or parts.password is not None
        or port not in (None, 80, 443)
        or not re.fullmatch(r"[a-z0-9]+(?:[a-z0-9.-]*[a-z0-9])?", host, re.ASCII)
        or any(
            not label or label.startswith("-") or label.endswith("-") for label in host.split(".")
        )
        or not any(host == domain or host.endswith("." + domain) for domain in _STORE_DOMAINS)
        or re.search(r"[\x00-\x1f\x7f\\]", decoded)
    ):
        raise ValueError("仅支持立创商城的 HTTP/HTTPS 网址，请勿输入其他网站或带账号的网址。")
    return parts


def normalize_navigation(text: str) -> str:
    """Accept a C-number or an LCSC address; reject ambiguous/foreign addresses."""
    value = text.strip()
    component_id = normalize_component_id(value)
    if component_id:
        return f"https://so.szlcsc.com/global.html?k={component_id}"
    if "://" not in value:
        value = "https://" + value
    parts = _validated_url(value)
    return urlunsplit(
        (parts.scheme.lower(), parts.netloc.lower(), parts.path or "/", parts.query, parts.fragment)
    )


def component_id_from_url(url: str) -> str | None:
    """Read explicit IDs from product paths, never search parameters or page IDs.

    Chinese item.szlcsc.com numeric paths use a different internal identifier.
    Those need PRODUCT_ID_SCRIPT or a manually entered C-number.
    """
    try:
        parts = _validated_url(url.strip())
        # Do not let escaped separators change the shape of a product URL.
        if re.search(r"%(?:2f|3f|23|25)", parts.path, re.IGNORECASE):
            return None
        path = unquote(parts.path, encoding="utf-8", errors="strict")
        match = _PRODUCT_PATH.fullmatch(path)
        host = parts.hostname or ""
        international = host == "lcsc.com" or host.endswith(".lcsc.com")
        if not match and not international:
            match = _ITEM_PATH.fullmatch(path)
        return match.group(1).upper() if match else None
    except (ValueError, UnicodeError):
        return None


# A read-only expression for wx.html2.WebView.RunScript. The Chinese site's URL
# uses productId (2392), whereas EasyEDA needs its distinct SKU (C2040). Read only
# a schema.org Product describing THIS page, not arbitrary page text or offers.
# Verified on https://item.szlcsc.com/2392.html (2026-09-29).
PRODUCT_ID_SCRIPT = r"""
(function () {
    var current = new URL(window.location.href);
    var host = current.hostname.toLowerCase();
    if (!/^(?:[a-z0-9-]+\.)*(?:lcsc\.com|lcsc\.cn|szlcsc\.com)$/.test(host)) return '';
    if (!/^https?:$/.test(current.protocol) || current.username || current.password) return '';
    if (current.port && current.port !== '80' && current.port !== '443') return '';
    var productPath = /^\/(?:product-detail|item)\/.+\.html$/i.test(current.pathname);
    var chineseItem = host === 'item.szlcsc.com' && /^\/[0-9]+\.html$/.test(current.pathname);
    if (!productPath && !chineseItem) return '';
    var ids = [];
    function inspect(value) {
        if (!value || typeof value !== 'object') return;
        if (Array.isArray(value)) { value.forEach(inspect); return; }
        if (Array.isArray(value['@graph'])) value['@graph'].forEach(inspect);
        var types = Array.isArray(value['@type']) ? value['@type'] : [value['@type']];
        if (types.indexOf('Product') < 0) return;
        var address = value.url || value['@id'];
        if (typeof address !== 'string') return;
        try {
            var product = new URL(address, current.href);
            if (product.origin !== current.origin || product.pathname !== current.pathname) return;
        } catch (error) { return; }
        if (typeof value.sku !== 'string' || !/^C[1-9][0-9]*$/i.test(value.sku.trim())) return;
        var id = value.sku.trim().toUpperCase();
        if (ids.indexOf(id) < 0) ids.push(id);
    }
    document.querySelectorAll('script[type="application/ld+json"]').forEach(function (script) {
        try { inspect(JSON.parse(script.textContent)); } catch (error) {}
    });
    return ids.length === 1 ? ids[0] : '';
})()
"""
