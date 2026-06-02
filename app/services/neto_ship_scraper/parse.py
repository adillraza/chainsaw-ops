"""Pure HTML parsers for Neto cPanel shipping-config pages.

Each function takes raw page HTML and returns plain dicts/lists — no network,
no session — so they're unit-testable against saved fixtures. The orchestrator
(main.py) fetches the pages with an authenticated session and feeds them here.

Pages parsed (all under https://www.chainsawspares.com.au/_cpanel/):
  ship_carrier      -> parse_carriers
  shippingid        -> parse_categories
  shippinggroup     -> parse_options
  shippingcostmgr   -> parse_services         (list)
  shippingcostmgr/view?id=N -> parse_service_detail   (config)
  ship              -> parse_mapping           (the routing matrix)
"""
from __future__ import annotations

import re
from bs4 import BeautifulSoup

_NUM_RE = re.compile(r"-?\d[\d,]*\.?\d*")


def _num(text):
    """Pull the first number out of a string like '$0.00', '22.0000kg', '17 %'."""
    if not text:
        return None
    m = _NUM_RE.search(text.replace(",", ""))
    return float(m.group()) if m else None


def _cells(row):
    return [td.get_text(" ", strip=True) for td in row.find_all("td")]


def _first_table_rows(html):
    soup = BeautifulSoup(html, "html.parser")
    table = soup.find("table")
    if not table:
        return []
    return table.find_all("tr")


def _id_from_link(row):
    """Return the numeric id from a row's view?id=N link, if present."""
    for a in row.find_all("a", href=True):
        m = re.search(r"[?&]id=(\d+)", a["href"])
        if m:
            return m.group(1)
    return None


# ---------------------------------------------------------------------------
# Carriers & Labels  (/_cpanel/ship_carrier)
# cols: [chk, ID, Name, Location, Courier Zone, actions]
# No status column -> active is derived later from service references.
# ---------------------------------------------------------------------------
def parse_carriers(html):
    rows = _first_table_rows(html)
    out = []
    for r in rows[1:]:
        c = _cells(r)
        if len(c) < 5 or not (c[1] or "").strip():
            continue
        out.append({
            "carrier_id": c[1].strip(),
            "name": c[2].strip(),
            "location": c[3].strip(),
            "courier_zone": c[4].strip(),
        })
    return out


# ---------------------------------------------------------------------------
# Shipping Categories  (/_cpanel/shippingid)
# cols: [chk, ID, Name, Description, Default]
# No status column -> active derived from usage (mapping / products).
# ---------------------------------------------------------------------------
def parse_categories(html):
    rows = _first_table_rows(html)
    out = []
    for r in rows[1:]:
        c = _cells(r)
        if len(c) < 4 or not (c[1] or "").strip():
            continue
        out.append({
            "category_id": c[1].strip(),
            "name": c[2].strip(),
            "description": c[3].strip(),
            "is_default": bool((c[4] if len(c) > 4 else "").strip()),
        })
    return out


# ---------------------------------------------------------------------------
# Shipping Options / routing groups  (/_cpanel/shippinggroup)
# cols: [chk, ID, Name, Routing Group, Description, MaximumCharge,
#        MinimumWeight, MaximumWeight, Pick Up, DeliveryDays,
#        DeliveryCutOffTime, Availability, Status, Visibility]
# Has explicit Status (Active/Inactive).
# ---------------------------------------------------------------------------
def parse_options(html):
    rows = _first_table_rows(html)
    out = []
    for r in rows[1:]:
        c = _cells(r)
        if len(c) < 13 or not (c[1] or "").strip():
            continue
        out.append({
            "option_id": c[1].strip(),
            "name": c[2].strip(),
            "routing_group": c[3].strip(),
            "description": c[4].strip(),
            "max_charge": _num(c[5]),
            "min_weight_kg": _num(c[6]),
            "max_weight_kg": _num(c[7]),
            "pickup": c[8].strip(),
            "delivery_days": c[9].strip(),
            "cutoff_time": c[10].strip(),
            "availability": c[11].strip(),
            "status": c[12].strip(),
            "visibility": (c[13].strip() if len(c) > 13 else ""),
            "is_active": c[12].strip().lower() == "active",
        })
    return out


# ---------------------------------------------------------------------------
# Services & Rates list  (/_cpanel/shippingcostmgr)
# cols: [chk, ID, Name, Type, Description, PO Box, Status, Clone]
# Has explicit Status (Active/Inactive). Detail at view?id=N.
# ---------------------------------------------------------------------------
def parse_services(html, base_url="https://www.chainsawspares.com.au/_cpanel"):
    rows = _first_table_rows(html)
    out = []
    for r in rows[1:]:
        c = _cells(r)
        if len(c) < 7 or not (c[1] or "").strip():
            continue
        sid = c[1].strip()
        out.append({
            "service_id": sid,
            "name": c[2].strip(),
            "type": c[3].strip(),
            "description": c[4].strip(),
            "po_box": c[5].strip().lower() == "yes",
            "status": c[6].strip(),
            "is_active": c[6].strip().lower() == "active",
            "detail_url": f"{base_url}/shippingcostmgr/view?id={sid}",
        })
    return out


# ---------------------------------------------------------------------------
# Service detail / rate-table config  (/_cpanel/shippingcostmgr/view?id=N)
# Parsed by input/select name + nearby label text. Field names confirmed
# live during build.
# ---------------------------------------------------------------------------
def _input_val(soup, name):
    el = soup.find(["input", "select", "textarea"], attrs={"name": name})
    if not el:
        return None
    if el.name == "select":
        opt = el.find("option", selected=True)
        return opt.get_text(strip=True) if opt else None
    if el.get("type") in ("checkbox", "radio"):
        return el.has_attr("checked")
    return el.get("value")


def parse_service_detail(html):
    """Extract rate-table config from a /_cpanel/shippingcostmgr/view?id=N page.
    Input names confirmed live 2026-06-01 against id=82 (MHF)."""
    soup = BeautifulSoup(html, "html.parser")

    def v(name):
        return _input_val(soup, name)

    return {
        "name": v("method_name"),
        "charge_type": v("chtype"),                       # 'Third Party Shipping Rate' / 'Fixed Rate By Product'
        "cubic_modifier": _num(str(v("cubic_modifier") or "")),
        "tax_inclusive": bool(v("tax_inc")),
        "po_box": bool(v("ship_pobox")),
        "max_length_m": _num(str(v("max_length") or "")),
        "min_charge": _num(str(v("min_cost") or "")),
        "max_charge": _num(str(v("max_cost") or "")),     # blank when uncapped
        "fuel_amt": _num(str(v("shipping_levy_fix") or "")),
        "fuel_pct": _num(str(v("shipping_levy") or "")),
        "handling_amt": _num(str(v("item_handling") or "")),
        "handling_unit": "per item",                      # cPanel handling is always per-item
    }


# ---------------------------------------------------------------------------
# Routing matrix  (/_cpanel/ship)
# Many blocks, each preceded by "<p>Routing group: X</p>" + status spans
# ("- Active"/"- Inactive", "- Visible to customer"/"- Not visible...").
# Each block table: header [Category, , Service / Rates, , Carrier / Labelling]
# rows map Category -> Service/Rates -> Carrier/Labelling.
# ---------------------------------------------------------------------------
def _is_mapping_table(table):
    rows = table.find_all("tr")
    if not rows:
        return False
    hdr = [h.get_text(strip=True) for h in rows[0].find_all(["th", "td"])]
    return "Category" in hdr and "Carrier / Labelling" in hdr


def parse_mapping(html):
    """Forward single pass: track the current routing-group / status context as
    we encounter the "Routing group: X" markers and status spans, then emit
    rows when we reach each mapping table. Blocks are also numbered in order
    (block_index) since a routing group can label more than one block."""
    soup = BeautifulSoup(html, "html.parser")
    out = []
    cur_rg, cur_active, cur_visible = None, None, None
    block_index = 0

    # Visit candidate elements in document order.
    for el in soup.find_all(["p", "span", "b", "table"]):
        if el.name == "table":
            if not _is_mapping_table(el):
                continue
            block_index += 1
            for r in el.find_all("tr")[1:]:
                c = _cells(r)
                if len(c) < 5 or not c[0].strip():
                    continue
                out.append({
                    "block_index": block_index,
                    "routing_group": cur_rg,
                    "block_active": cur_active,
                    "block_visible": cur_visible,
                    "category": c[0].strip(),
                    "service": c[2].strip(),
                    "carrier": c[4].strip(),
                })
            # reset context so a block without its own markers doesn't inherit
            cur_rg, cur_active, cur_visible = None, None, None
            continue

        txt = el.get_text(" ", strip=True)
        if not txt:
            continue
        low = txt.lower()
        if low.startswith("routing group:"):
            cur_rg = txt.split(":", 1)[1].strip() or cur_rg
        elif txt in ("- Active", "- Inactive"):
            cur_active = txt == "- Active"
        elif txt in ("- Visible to customer", "- Not visible to customer"):
            cur_visible = txt == "- Visible to customer"
    return out
