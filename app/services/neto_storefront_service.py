"""Neto storefront shipping quote — the REAL customer-facing quote.

Rather than reconstruct Neto's (very complex) shipping logic, we read the
quote straight from the public chainsawspares.com.au product page's
"Calculate Shipping" widget. That widget fires:

    GET /ajax/ajax_template?proc=load&docid=_jstl__buying_options
        &template=<b64 "buying_options">&type=<b64 "item">
        &fields=<netosd-encoded {sku, qty, ship_zip, ship_city, ship_state, ship_country}>

and the server returns ``^NETO^SUCCESS^<netosd>`` whose ``content`` key holds
the rendered buying-options HTML — including the shipping-method list with
live prices (exactly what the customer sees). No login required.

We replicate Neto's two client-side encodings (``netosd`` payload + JS
``escape()`` of the response content), POST nothing, parse the option rows.
"""
from __future__ import annotations

import base64
import logging
import re
import urllib.parse
import urllib.request

BASE = "https://www.chainsawspares.com.au"
ENDPOINT = BASE + "/ajax/ajax_template"
TIMEOUT = 30

log = logging.getLogger(__name__)


def _b64(s: str) -> str:
    """URL-safe base64 without padding — how Neto encodes the template/type ids."""
    return base64.urlsafe_b64encode(s.encode()).decode().rstrip("=")


# Neto's docid metadata for the product buying-options fragment. These are
# constant across products (they encode the generic "buying_options"/"item"
# template ids, verified against several live SKUs).
_DOCID = "_jstl__buying_options"
_TEMPLATE = _b64("buying_options")
_TYPE = _b64("item")

# JS escape() leaves these unescaped; everything else becomes %XX / %uXXXX.
_JS_SAFE = frozenset(
    "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789@*_+-./"
)


def _js_escape(s: str) -> str:
    out = []
    for ch in s:
        if ch in _JS_SAFE:
            out.append(ch)
        else:
            o = ord(ch)
            out.append("%%%02X" % o if o < 256 else "%%u%04X" % o)
    return "".join(out)


def _js_unescape(s: str) -> str:
    return re.sub(
        r"%u([0-9A-Fa-f]{4})|%([0-9A-Fa-f]{2})",
        lambda m: chr(int(m.group(1) or m.group(2), 16)),
        s,
    )


def _netosd(value, sep: str = "|") -> str:
    """Serialise to Neto's 'NSD1;' wire format (matches create_netosd_data)."""
    return "NSD1;" + _netosd_rc(value, sep)


def _netosd_rc(value, sep: str) -> str:
    if isinstance(value, dict):
        out = "#" + str(len(value)) + sep
        for k, v in value.items():
            out += _netosd_rc(str(k), sep) + _netosd_rc(v, sep)
        return out
    if isinstance(value, (list, tuple)):
        out = "@" + str(len(value)) + sep
        for v in value:
            out += _netosd_rc(v, sep)
        return out
    d = _js_escape(str(value))
    return "$" + str(len(d)) + sep + d


def _carrier_family(method: str) -> str:
    m = (method or "").lower()
    if "startrack" in m or "star track" in m:
        return "startrack"
    if "auspost" in m or "australia post" in m:
        return "auspost"
    return "other"


def _extract_content(raw: str) -> str | None:
    """Pull the unescaped ``content`` HTML out of a ^NETO^SUCCESS^<netosd> body."""
    head = raw.split("^", 3)
    # raw looks like: ^NETO^SUCCESS^NSD1;...   -> ['', 'NETO', 'SUCCESS', 'NSD1;...']
    if len(head) < 4 or head[2].upper() != "SUCCESS":
        return None
    body = head[3]
    m = re.search(r"\$7\|content\$(\d+)\|", body)
    if not m:
        return None
    start = m.end()
    return _js_unescape(body[start:start + int(m.group(1))])


def _parse_options(html: str) -> list[dict]:
    """Parse '<strong>Method</strong> - $Price' rows from the shipping block."""
    block = re.search(
        r'aria-label="Shipping results">(.*?)(?:<div class="mvp_pshare|$)', html, re.S
    )
    scope = block.group(1) if block else html
    out: list[dict] = []
    for grp in re.findall(
        r'aria-label="Shipping method option">(.*?)</div>', scope, re.S
    ):
        m = re.search(r"<strong>(.*?)</strong>\s*-\s*\$?([\d,]+\.\d{2})", grp, re.S)
        if not m:
            continue
        method = re.sub(r"\s+", " ", m.group(1)).strip()
        price = float(m.group(2).replace(",", ""))
        out.append(
            {"method": method, "price": price, "carrier_family": _carrier_family(method)}
        )
    return out


def storefront_quotes(
    sku: str, qty: int, postcode: str, suburb: str, state: str | None
) -> dict:
    """Return the live Neto storefront shipping quote for one item.

    {available, options:[{method, price, carrier_family}], message}
    """
    if not sku:
        return {"available": False, "options": [], "message": "no SKU"}
    fields = {
        "preview": "y",
        "sku": sku,
        "qty": str(max(1, int(qty or 1))),
        "ship_zip": (postcode or "").strip(),
        "ship_city": (suburb or "").strip(),
        "ship_state": (state or "").strip(),
        "ship_country": "AU",
    }
    params = {
        "proc": "load",
        "docid": _DOCID,
        "template": _TEMPLATE,
        "type": _TYPE,
        "fields": _netosd(fields),
    }
    url = ENDPOINT + "?" + urllib.parse.urlencode(params)
    try:
        req = urllib.request.Request(
            url,
            headers={
                "User-Agent": "Mozilla/5.0 (chainsaw-ops freight calculator)",
                "X-Requested-With": "XMLHttpRequest",
                "Accept": "*/*",
            },
        )
        raw = urllib.request.urlopen(req, timeout=TIMEOUT).read().decode("utf-8", "replace")
    except Exception as exc:  # noqa: BLE001
        log.warning("storefront quote fetch failed for %s/%s", sku, postcode, exc_info=True)
        return {"available": False, "options": [], "message": f"couldn't reach storefront ({exc})"}

    content = _extract_content(raw)
    if content is None:
        return {"available": False, "options": [], "message": "storefront returned no quote"}

    options = _parse_options(content)
    if not options:
        return {
            "available": False,
            "options": [],
            "message": "no shipping options for this destination "
                       "(item may be out of stock, oversized, or pickup-only)",
        }
    options.sort(key=lambda o: o["price"])
    return {"available": True, "options": options, "message": None}
