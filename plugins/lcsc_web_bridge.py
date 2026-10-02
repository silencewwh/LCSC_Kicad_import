"""In-page import controls and the narrow WebView-to-KiCad message contract.

The browser owns the document token and rechecks the clicked product identity
before starting an import. Web content never supplies paths or executable code.
"""

from __future__ import annotations

import json
import re
from urllib.parse import urlsplit, urlunsplit

from lcsc_page import normalize_component_id, normalize_navigation

_MESSAGE_KEYS = {"version", "type", "id", "url", "token"}
_STATES = {"idle", "importing", "success", "error"}

# The host page owns its CSS. Keep this small control independent, including its
# pseudo-element icons, so a store theme cannot hide focus or replace its label.
# Icons are simple geometric paths drawn for this project; no font/CDN is needed.
_BUTTON_CSS = r"""
button[data-lcsc-kicad-import][data-lcsc-kicad-import] {
    all: initial !important;
    position: fixed !important;
    right: calc(20px + env(safe-area-inset-right, 0px)) !important;
    bottom: calc(20px + env(safe-area-inset-bottom, 0px)) !important;
    z-index: 2147483647 !important;
    box-sizing: border-box !important;
    display: inline-flex !important;
    align-items: center !important;
    justify-content: center !important;
    gap: 8px !important;
    min-width: 120px !important;
    min-height: 36px !important;
    padding: 7px 14px !important;
    border: 1px solid #1a73e8 !important;
    border-radius: 8px !important;
    background: #1a73e8 !important;
    color: #fff !important;
    box-shadow: none !important;
    font: 500 13px/20px 'Segoe UI', 'Microsoft YaHei UI', system-ui, sans-serif !important;
    letter-spacing: normal !important;
    text-align: center !important;
    white-space: nowrap !important;
    cursor: pointer !important;
    user-select: none !important;
    -webkit-tap-highlight-color: transparent !important;
    transition: background-color .12s ease, border-color .12s ease !important;
}
button[data-lcsc-kicad-import][data-lcsc-kicad-import][hidden] { display: none !important; }
button[data-lcsc-kicad-import][data-lcsc-kicad-import]::before {
    all: initial !important;
    content: '' !important;
    box-sizing: border-box !important;
    display: block !important;
    flex: 0 0 16px !important;
    width: 16px !important;
    height: 16px !important;
    background: currentColor !important;
    color: inherit !important;
    mask: url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 24 24'%3E%3Cpath d='M12 3v12m-4-4 4 4 4-4M5 16v4h14v-4' fill='none' stroke='black' stroke-width='2' stroke-linecap='round' stroke-linejoin='round'/%3E%3C/svg%3E") center / contain no-repeat !important;
}
button[data-lcsc-kicad-import][data-lcsc-kicad-import]::after { display: none !important; }
button[data-lcsc-kicad-import][data-lcsc-kicad-import]:hover:not(:disabled) {
    background: #1765cc !important;
    border-color: #1765cc !important;
}
button[data-lcsc-kicad-import][data-lcsc-kicad-import]:focus-visible {
    outline: 2px solid #1a73e8 !important;
    outline-offset: 2px !important;
}
button[data-lcsc-kicad-import][data-lcsc-kicad-import]:active:not(:disabled) {
    background: #185abc !important;
    border-color: #185abc !important;
}
button[data-lcsc-kicad-import][data-lcsc-kicad-import]:disabled {
    cursor: wait !important;
    background: #e8f0fe !important;
    border-color: #d2e3fc !important;
    color: #1967d2 !important;
}
button[data-lcsc-kicad-import][data-lcsc-kicad-import][data-state='importing']::before {
    mask: none !important;
    background: transparent !important;
    border: 2px solid currentColor !important;
    border-right-color: transparent !important;
    border-radius: 50% !important;
    animation: lcsc-bridge-spinner .8s linear infinite !important;
}
button[data-lcsc-kicad-import][data-lcsc-kicad-import][data-state='success']:not(:hover) {
    background: #f1f3f4 !important;
    border-color: #dadce0 !important;
    color: #3c4043 !important;
}
button[data-lcsc-kicad-import][data-lcsc-kicad-import][data-state='success']::before {
    mask-image: url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 24 24'%3E%3Cpath d='m5 12 4 4L19 6' fill='none' stroke='black' stroke-width='2.5' stroke-linecap='round' stroke-linejoin='round'/%3E%3C/svg%3E") !important;
}
button[data-lcsc-kicad-import][data-lcsc-kicad-import][data-state='error']:not(:hover) {
    background: #fff !important;
    border-color: #f6aea9 !important;
    color: #b3261e !important;
}
button[data-lcsc-kicad-import][data-lcsc-kicad-import][data-state='error']::before {
    mask-image: url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 24 24'%3E%3Cpath d='M19 9a7 7 0 1 0 0 6m0-12v6h-6' fill='none' stroke='black' stroke-width='2' stroke-linecap='round' stroke-linejoin='round'/%3E%3C/svg%3E") !important;
}
button[data-lcsc-kicad-import='row'][data-lcsc-kicad-import] {
    position: static !important;
    width: 46px !important;
    min-width: 46px !important;
    max-width: 46px !important;
    height: 32px !important;
    min-height: 32px !important;
    padding: 0 !important;
    margin: 0 0 6px !important;
    border-radius: 4px !important;
    font-size: 12px !important;
    vertical-align: middle !important;
}
button[data-lcsc-kicad-import='row'][data-lcsc-kicad-import]::before { display: none !important; }
button[data-lcsc-kicad-cart='true'][data-lcsc-kicad-cart] {
    display: inline-block !important;
    box-sizing: border-box !important;
    width: calc(100% - 52px) !important;
    min-width: 0 !important;
    height: 32px !important;
    padding-inline: 0 !important;
    margin-right: 6px !important;
    vertical-align: middle !important;
}
@keyframes lcsc-bridge-spinner { to { transform: rotate(360deg); } }
@media (prefers-reduced-motion: reduce) {
    button[data-lcsc-kicad-import][data-lcsc-kicad-import] { transition: none !important; }
    button[data-lcsc-kicad-import][data-lcsc-kicad-import]::before { animation: none !important; }
}
@media (forced-colors: active) {
    button[data-lcsc-kicad-import][data-lcsc-kicad-import] { border: 2px solid ButtonText !important; }
    button[data-lcsc-kicad-import][data-lcsc-kicad-import]::before { forced-color-adjust: none !important; }
    button[data-lcsc-kicad-import][data-lcsc-kicad-import]:focus-visible { outline-color: Highlight !important; }
}
"""


def _document_url(value: str) -> str:
    """Canonicalize a real store URL without accepting the address-bar shortcuts."""
    if not isinstance(value, str) or not re.match(r"^https?://", value, re.IGNORECASE):
        raise ValueError("导入消息必须包含完整的立创网页地址。")
    parts = urlsplit(normalize_navigation(value))
    # WebView and the JavaScript URL object may disagree about default ports.
    default_port = 443 if parts.scheme == "https" else 80
    host = parts.hostname
    netloc = host if parts.port in (None, default_port) else f"{host}:{parts.port}"
    return urlunsplit((parts.scheme, netloc, parts.path, parts.query, parts.fragment))


def parse_import_message(raw: str, current_url: str, token: str) -> str:
    """Validate one click against the active document, returning its component ID.

    This validates the message envelope, not the page's identity claim. Callers
    must compare the result with ``current_product_script(component_id)`` in the
    current main frame before importing, and reject it while an import is busy.
    """
    if not isinstance(raw, str) or len(raw) > 8192:
        raise ValueError("网页导入消息无效或过长。")
    try:
        payload = json.loads(raw)
    except (ValueError, RecursionError) as exc:
        raise ValueError("网页导入消息不是有效 JSON。") from exc
    if not isinstance(payload, dict) or set(payload) != _MESSAGE_KEYS:
        raise ValueError("网页导入消息字段无效。")
    if (
        type(payload["version"]) is not int
        or payload["version"] != 1
        or payload["type"] != "import"
    ):
        raise ValueError("网页导入消息版本或操作无效。")
    if not isinstance(token, str) or not token or payload["token"] != token:
        raise ValueError("网页已切换，请在当前商品页重新点击导入。")
    component_id = payload["id"]
    if (
        not isinstance(component_id, str)
        or len(component_id) > 32
        or normalize_component_id(component_id) != component_id
    ):
        raise ValueError("网页导入消息中的商品编号无效。")
    if _document_url(payload["url"]) != _document_url(current_url):
        raise ValueError("导入请求与当前商品页面不一致。")
    return component_id


# Require a schema.org Product naming this exact page, including on explicit
# C-number URLs: a verification page can retain the original product address.
# Search text, offers, recommendation cards and query strings cannot select a
# component. Conflicting main-product metadata fails closed.
_CURRENT_PRODUCT_SCRIPT = r"""
(function () {
    var current;
    try { current = new URL(window.location.href); } catch (error) { return ''; }
    var host = current.hostname.toLowerCase();
    if (!/^(?:[a-z0-9](?:[a-z0-9-]*[a-z0-9])?\.)*(?:lcsc\.com|lcsc\.cn|szlcsc\.com)$/.test(host)) return '';
    if (!/^https?:$/.test(current.protocol) || current.username || current.password) return '';
    if (current.port && current.port !== '80' && current.port !== '443') return '';
    if (/%(?:2f|3f|23|25)/i.test(current.pathname)) return '';
    var path;
    try { path = decodeURIComponent(current.pathname); } catch (error) { return ''; }
    if (/[\x00-\x1f\x7f\\]/.test(path)) return '';
    var international = host === 'lcsc.com' || host.endsWith('.lcsc.com');
    var explicit = /^\/product-detail\/(?:[^/]+\/)*(?:[^/]*_)?(C[1-9][0-9]*)\.html$/i.exec(path);
    if (!explicit && !international) {
        explicit = /^\/item\/(?:[^/]+\/)*(?:[^/]*_)?(C[1-9][0-9]*)\.html$/i.exec(path);
    }
    var productPath = /^\/(?:product-detail|item)\/.+\.html$/i.test(path);
    var chineseItem = host === 'item.szlcsc.com' && /^\/[0-9]+\.html$/.test(path);
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
        if (typeof value.sku !== 'string' || !/^C[1-9][0-9]{0,30}$/i.test(value.sku.trim())) return;
        var id = value.sku.trim().toUpperCase();
        if (ids.indexOf(id) < 0) ids.push(id);
    }
    document.querySelectorAll('script[type="application/ld+json"]').forEach(function (script) {
        try { inspect(JSON.parse(script.textContent)); } catch (error) {}
    });
    if (ids.length !== 1) return '';
    if (explicit) {
        var urlId = explicit[1].toUpperCase();
        if (urlId.length > 32 || ids[0] !== urlId) return '';
        return urlId;
    }
    return ids.length === 1 ? ids[0] : '';
})()
"""


_CANDIDATES_SCRIPT = r"""
(function () {
    var main = MAIN_PRODUCT_SCRIPT;
    if (main) return [{id: main, cart: null}];
    var current;
    try { current = new URL(window.location.href); } catch (error) { return []; }
    // This is the verified self-operated search layout. A cart in a marketplace
    // offer, recommendation, or verification document cannot select a part.
    if (current.hostname !== 'so.szlcsc.com' || current.pathname !== '/global.html' ||
        !/^https?:$/.test(current.protocol) || current.username || current.password ||
        (current.port && current.port !== '80' && current.port !== '443')) return [];
    function metadata(node) {
        try {
            var value = JSON.parse(node.getAttribute('data-custom-data'));
            return value && typeof value === 'object' && !Array.isArray(value) ? value : null;
        } catch (error) { return null; }
    }
    function code(value) {
        return typeof value === 'string' && /^C[1-9][0-9]{0,30}$/i.test(value.trim()) ?
            value.trim().toUpperCase() : '';
    }
    var candidates = [];
    document.querySelectorAll('button[data-custom-data][data-spm="iad"]').forEach(function (cart) {
        if (!cart.isConnected || cart.textContent.replace(/\s/g, '') !== '加入购物车') return;
        var data = metadata(cart);
        var id = data && data.productType === 'electronic' ? code(data.productCode) : '';
        var row = cart.closest('section[data-spm="zy"]');
        if (!id || !row || !row.isConnected) return;
        var numbers = [];
        row.querySelectorAll('dl').forEach(function (entry) {
            if (entry.closest('section[data-spm="zy"]') !== row) return;
            var term = entry.querySelector('dt');
            var value = entry.querySelector('dd');
            if (term && value && term.textContent.replace(/[\s:：]/g, '') === '编号') {
                numbers.push(code(value.textContent));
            }
        });
        if (!numbers.length || numbers.some(function (value) { return value !== id; })) return;
        var links = [];
        row.querySelectorAll('a[data-custom-data]').forEach(function (link) {
            if (link.closest('section[data-spm="zy"]') !== row) return;
            var info = metadata(link);
            if (!info || info.productType !== 'electronic') return;
            var url;
            try { url = new URL(link.href, current.href); } catch (error) { return; }
            if (url.hostname !== 'item.szlcsc.com' || !/^\/[1-9][0-9]*\.html$/.test(url.pathname) ||
                !/^https?:$/.test(url.protocol) || url.username || url.password ||
                (url.port && url.port !== '80' && url.port !== '443')) return;
            links.push(code(info.productCode));
        });
        if (!links.length || links.some(function (value) { return value !== id; })) return;
        candidates.push({id: id, cart: cart});
    });
    return candidates;
})()
""".replace("MAIN_PRODUCT_SCRIPT", _CURRENT_PRODUCT_SCRIPT.strip()).strip()


def current_product_script(component_id: str | None = None) -> str:
    """Read the main product, or verify one explicitly selected main/list product."""
    if component_id is None:
        return _CURRENT_PRODUCT_SCRIPT.strip()
    if (
        not isinstance(component_id, str)
        or normalize_component_id(component_id) != component_id
        or len(component_id) > 32
    ):
        raise ValueError("Invalid component ID")
    return (
        "(function(){var id="
        + json.dumps(component_id)
        + ";var candidates="
        + _CANDIDATES_SCRIPT
        + ";return candidates.some(function(candidate){return candidate.id===id;})?id:'';})()"
    )


def injection_script(token: str) -> str:
    """Create or refresh controls for the main product or verified search rows.

    No DOM or global runtime is added unless a product is confirmed. Repeated
    calls reuse the runtime and preserve pending-click state.
    Use ``refresh_script`` for polling; only install when it reports a confirmed
    component without an installed runtime. No import occurs before a click.
    """
    if not isinstance(token, str) or not token or len(token) > 256:
        raise ValueError("Document token must contain 1–256 characters")
    return (
        r"""
(function () {
    if (window.top !== window || !document.body) return '';
    var token = TOKEN_JSON;
    var key = '__lcscKiCadImport';
    var runtime = window[key];
    if (runtime && runtime.token === token && typeof runtime.refresh === 'function') {
        return runtime.refresh();
    }
    var readProduct = function () { return PRODUCT_SCRIPT; };
    var readCandidates = function () { return CANDIDATES_SCRIPT; };
    var initial = readCandidates();
    if (runtime && typeof runtime.dispose === 'function') runtime.dispose();
    if (!initial.length) return '';
    var styleId = 'lcsc-kicad-import-style';
    var cartAttribute = 'data-lcsc-kicad-cart';
    runtime = {
        token: token, button: null, controls: [], id: '', url: '', busy: false,
        busyId: '', states: Object.create(null), disposed: false
    };
    window[key] = runtime;
    runtime.readProduct = readProduct;
    runtime.readCandidates = readCandidates;
    runtime.removeControl = function (control) {
        if (control.button.isConnected) control.button.remove();
        if (control.cart && control.cart.getAttribute(cartAttribute) === 'true') {
            if (control.previousCartAttribute === null) control.cart.removeAttribute(cartAttribute);
            else control.cart.setAttribute(cartAttribute, control.previousCartAttribute);
        }
    };
    runtime.dispose = function () {
        if (runtime.disposed) return;
        runtime.disposed = true;
        runtime.id = '';
        runtime.controls.forEach(runtime.removeControl);
        runtime.controls = [];
        runtime.button = null;
        var style = document.getElementById(styleId);
        if (style) style.remove();
        if (window[key] === runtime) delete window[key];
    };
    runtime.paint = function () {
        runtime.controls.forEach(function (control) {
            var button = control.button;
            var result = runtime.states[control.id] || {state: 'idle', message: ''};
            var importing = runtime.busy && runtime.busyId === control.id;
            var visualState = importing ? 'importing' : result.state === 'importing' ? 'idle' : result.state;
            var text = importing ? (control.cart ? '导入中' : '导入中…') :
                visualState === 'success' ? '已导入' : visualState === 'error' ? '重试' :
                control.cart ? '导入' : '导入 KiCad';
            var title = '导入 KiCad · ' + control.id + (result.message ? ' · ' + result.message : '');
            var paintKey = JSON.stringify([control.id, runtime.busy, visualState, text, title]);
            if (control.paintKey === paintKey) return;
            control.paintKey = paintKey;
            button.hidden = false;
            button.disabled = runtime.busy;
            button.textContent = text;
            button.title = title;
            button.setAttribute('data-lcsc-component', control.id);
            button.setAttribute('aria-label', title + ' · ' + text);
            button.setAttribute('aria-busy', importing ? 'true' : 'false');
            button.setAttribute('data-state', visualState);
        });
    };
    runtime.createControl = function (candidate) {
        var button = document.createElement('button');
        if (!candidate.cart) button.id = 'lcsc-kicad-import';
        button.type = 'button';
        button.setAttribute('data-lcsc-kicad-import', candidate.cart ? 'row' : 'detail');
        button.setAttribute('aria-live', 'polite');
        button.setAttribute('aria-atomic', 'true');
        var control = {
            id: candidate.id, cart: candidate.cart, button: button, paintKey: null,
            previousCartAttribute: candidate.cart ? candidate.cart.getAttribute(cartAttribute) : null
        };
        button.addEventListener('click', function (event) {
            event.preventDefault();
            event.stopPropagation();
            if (runtime.disposed || window[key] !== runtime) return;
            var clickedId = control.id;
            // Refresh reconciles each concrete cart node. Recycled/removed row
            // buttons never inherit the identity of the newly rendered product.
            runtime.refresh();
            if (runtime.disposed || runtime.busy || !button.isConnected ||
                runtime.controls.indexOf(control) < 0 ||
                (control.cart && clickedId !== control.id)) return;
            var id = control.id;
            var bridge = window.lcscImport;
            if (!bridge || typeof bridge.postMessage !== 'function') {
                runtime.status(id, 'error', '连接未就绪，请重新打开浏览器。');
                return;
            }
            runtime.status(id, 'importing', '导入中…');
            try {
                bridge.postMessage(JSON.stringify({
                    version: 1, type: 'import', id: id,
                    url: window.location.href, token: runtime.token
                }));
            } catch (error) {
                runtime.status(id, 'error', '发送失败，请重试。');
            }
        });
        if (candidate.cart) {
            candidate.cart.setAttribute(cartAttribute, 'true');
            candidate.cart.insertAdjacentElement('afterend', button);
        } else {
            var existing = document.getElementById('lcsc-kicad-import');
            if (existing) existing.remove();
            document.body.appendChild(button);
        }
        return control;
    };
    runtime.refresh = function () {
        if (runtime.disposed) return '';
        var candidates = runtime.readCandidates();
        if (!candidates.length) {
            runtime.dispose();
            return '';
        }
        var url = window.location.href;
        if (runtime.url !== url) {
            runtime.url = url;
            runtime.states = Object.create(null);
        }
        runtime.id = candidates[0].id;
        if (!document.getElementById(styleId)) {
            var style = document.createElement('style');
            style.id = styleId;
            style.textContent = BUTTON_CSS;
            document.body.appendChild(style);
        }
        var remaining = runtime.controls.slice();
        var next = [];
        candidates.forEach(function (candidate) {
            var control = remaining.find(function (item) {
                return item.cart === candidate.cart && (!candidate.cart || item.id === candidate.id) &&
                    item.button.isConnected;
            });
            if (control) {
                remaining.splice(remaining.indexOf(control), 1);
                control.id = candidate.id;
                if (control.cart) {
                    if (control.cart.getAttribute(cartAttribute) !== 'true') {
                        control.cart.setAttribute(cartAttribute, 'true');
                    }
                    if (control.cart.nextElementSibling !== control.button) {
                        control.cart.insertAdjacentElement('afterend', control.button);
                    }
                }
            } else {
                // Remove stale controls for this cart before taking ownership of
                // its marker again, preserving any previous attribute value.
                remaining.filter(function (item) { return item.cart === candidate.cart; }).forEach(function (item) {
                    runtime.removeControl(item);
                    remaining.splice(remaining.indexOf(item), 1);
                });
                control = runtime.createControl(candidate);
            }
            next.push(control);
        });
        remaining.forEach(runtime.removeControl);
        runtime.controls = next;
        runtime.button = next.find(function (control) { return !control.cart; })?.button || null;
        runtime.paint();
        return runtime.id;
    };
    runtime.status = function (id, state, message) {
        if (!runtime.refresh()) return;
        if (state === 'importing') {
            runtime.busy = true;
            runtime.busyId = id;
        } else if (!runtime.busyId || runtime.busyId === id) {
            runtime.busy = false;
            runtime.busyId = '';
        }
        runtime.states[id] = {state: state, message: message || ''};
        runtime.paint();
    };
    return runtime.refresh();
})()
""".replace("BUTTON_CSS", json.dumps(_BUTTON_CSS))
        .replace("CANDIDATES_SCRIPT", _CANDIDATES_SCRIPT)
        .replace("PRODUCT_SCRIPT", current_product_script())
        .replace("TOKEN_JSON", json.dumps(token))
    )


def refresh_script(token: str) -> str:
    """Return JSON with ``installed`` and confirmed ``componentId`` for polling.

    An installed runtime refreshes itself without redefining functions. Without
    one, this only reads product metadata and verified search rows. Native callers should run the full
    injection only when ``installed`` is false and ``componentId`` is nonempty.
    """
    if not isinstance(token, str) or not token or len(token) > 256:
        raise ValueError("Document token must contain 1–256 characters")
    return r"""
(function () {
    if (window.top !== window || !document.body) {
        return JSON.stringify({installed: false, componentId: ''});
    }
    var token = TOKEN_JSON;
    var runtime = window.__lcscKiCadImport;
    if (runtime && runtime.token === token && typeof runtime.refresh === 'function') {
        var id = runtime.refresh();
        return JSON.stringify({installed: Boolean(id), componentId: id});
    }
    var candidates = CANDIDATES_SCRIPT;
    var id = candidates.length ? candidates[0].id : "";
    if (runtime && typeof runtime.dispose === 'function') runtime.dispose();
    return JSON.stringify({installed: false, componentId: id});
})()
""".replace("CANDIDATES_SCRIPT", _CANDIDATES_SCRIPT).replace("TOKEN_JSON", json.dumps(token))


def status_script(component_id: str, state: str, message: str = "") -> str:
    """Update the existing in-page button with a result from the import worker."""
    if (
        not isinstance(component_id, str)
        or normalize_component_id(component_id) != component_id
        or len(component_id) > 32
    ):
        raise ValueError("Invalid component ID")
    if state not in _STATES:
        raise ValueError("Invalid import state")
    arguments = ",".join(json.dumps(value) for value in (component_id, state, str(message)[:2000]))
    return (
        "(function(){var runtime=window.__lcscKiCadImport;"
        "if(runtime&&typeof runtime.status==='function'){runtime.status("
        + arguments
        + ");}return '';})()"
    )
