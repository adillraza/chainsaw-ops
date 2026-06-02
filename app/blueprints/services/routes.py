"""Services landing + NETO Shippings tab.

Scaffold stage: the landing page lists available services and the NETO
Shippings route renders a placeholder. The scraper, BigQuery snapshot, and
visualisations are wired in by subsequent tasks.
"""
from __future__ import annotations

import threading
import time

from flask import jsonify, render_template
from flask_login import login_required

from app.auth.abilities import require_capability
from app.blueprints.services import services_bp


# ---------------------------------------------------------------------------
# NETO Shippings — background re-scrape state
# ---------------------------------------------------------------------------
_refresh_lock = threading.Lock()
_refresh_state: dict = {"running": False, "message": "", "started_at": None,
                        "finished_at": None, "result": None, "error": None}


def _run_refresh(app):
    from app.services.neto_ship_scraper import runner
    from app.services import neto_shipping_service as ns

    def progress(msg):
        _refresh_state["message"] = msg

    try:
        result = runner.run_scrape(progress=progress)
        progress("mirroring to local store…")
        with app.app_context():
            ns.mirror_to_local()  # BQ -> SQLite (busts the BQ cache internally)
        _refresh_state.update(result=result, error=None, message="done")
    except Exception as exc:  # noqa: BLE001
        _refresh_state.update(error=str(exc), message="failed")
    finally:
        _refresh_state.update(running=False, finished_at=time.time())


# Catalogue of services shown on the landing page. ``capability`` controls
# whether the card (and its nav entry) is visible to the current user.
SERVICES = [
    {
        "key": "neto_shipping",
        "title": "NETO Shippings",
        "description": "Visualise the live Neto shipping configuration — carriers, "
                       "categories, options, rate tables and how they're wired together.",
        "icon": "fa-truck-fast",
        "endpoint": "services.neto_shipping",
        "capability": "services.shipping.view",
        "available": True,
    },
    {
        "key": "st_calculator",
        "title": "Startrack Freight Calculator",
        "description": "Quote a SKU to a postcode against the live Startrack API and "
                       "compare to how Neto would charge. (Coming soon.)",
        "icon": "fa-calculator",
        "endpoint": "services.st_calculator",
        "capability": "services.calculator.view",
        "available": False,
    },
]


@services_bp.route("/")
@login_required
@require_capability("services.shipping.view")
def index():
    """Services landing page — card grid of available tools."""
    return render_template("services/index.html", services=SERVICES)


@services_bp.route("/neto-shipping")
@login_required
@require_capability("services.shipping.view")
def neto_shipping():
    """NETO Shippings tab — dashboard + reference tables + service drill-down."""
    from app.services import neto_shipping_service as ns

    error = None
    try:
        # Read from the fast local SQLite mirror; on a miss (first load or
        # cleared mirror), build from BigQuery and seed the mirror.
        snap = ns.get_local()
        if snap is None:
            snap = ns.build_payload()
            try:
                ns.mirror_to_local(snap)
            except Exception:  # noqa: BLE001
                import logging as _l
                _l.getLogger(__name__).warning("neto_shipping mirror seed failed", exc_info=True)
    except Exception as exc:  # noqa: BLE001
        return render_template("services/neto_shipping.html", error=str(exc), snap=None)

    # snap is already enriched (product_count / order stats / cost_band) in build_payload.

    # Which services each category routes to (from the mapping)
    cat_services: dict[str, set] = {}
    for m in snap["mapping"]:
        cat_services.setdefault(m.get("category"), set()).add(m.get("service"))

    # Anomaly flags for the dashboard
    anomalies = []
    fuel_pcts = [sv.get("fuel_pct") for sv in snap["services"] if sv.get("fuel_pct") is not None]
    common_fuel = max(set(fuel_pcts), key=fuel_pcts.count) if fuel_pcts else None
    for sv in snap["services"]:
        if sv.get("fuel_pct") is not None and common_fuel is not None and sv["fuel_pct"] != common_fuel:
            anomalies.append({
                "kind": "fuel", "service": sv["name"],
                "detail": f"Fuel levy {sv['fuel_pct']}% (most services are {common_fuel}%)",
            })
    handling_services = [sv["name"] for sv in snap["services"] if (sv.get("handling_amt") or 0) > 0]

    summary = {
        "carriers": len(snap["carriers"]),
        "categories": len(snap["categories"]),
        "options": len(snap["options"]),
        "services": len(snap["services"]),
        "handling_services": handling_services,
        "common_fuel": common_fuel,
    }

    matrix = _build_matrix(snap)
    flow = _build_flow(snap)

    return render_template(
        "services/neto_shipping.html",
        error=error, snap=snap, summary=summary,
        anomalies=anomalies, cat_services=cat_services, matrix=matrix, flow=flow,
    )


def _build_flow(snap: dict) -> dict:
    """Sankey nodes/links: Category -> Service -> Carrier-family, with flow
    weighted by each category's active-product count (split evenly across the
    services/carriers it routes to, so total flow is conserved)."""
    active = [m for m in snap["mapping"] if m.get("block_active")]
    cat_count = {c["name"]: c.get("product_count", 0) for c in snap["categories"]}

    # distinct (category, service) and (service, carrier-family) from active mappings
    cat_services: dict[str, set] = {}
    svc_families: dict[str, set] = {}
    for m in active:
        cat, svc = m.get("category"), m.get("service")
        if not cat or not svc:
            continue
        cat_services.setdefault(cat, set()).add(svc)
        svc_families.setdefault(svc, set()).add(_carrier_family(m.get("carrier")))

    cat_node = lambda n: f"cat:{n}"
    svc_node = lambda n: f"svc:{n}"
    fam_node = lambda n: f"car:{n}"

    links = []
    svc_inflow: dict[str, float] = {}
    for cat, svcs in cat_services.items():
        pc = cat_count.get(cat, 0)
        if pc <= 0 or not svcs:
            continue
        share = pc / len(svcs)
        for svc in svcs:
            links.append({"source": cat_node(cat), "target": svc_node(svc), "value": round(share, 1)})
            svc_inflow[svc] = svc_inflow.get(svc, 0) + share

    for svc, fams in svc_families.items():
        inflow = svc_inflow.get(svc, 0)
        if inflow <= 0 or not fams:
            continue
        share = inflow / len(fams)
        for fam in fams:
            links.append({"source": svc_node(svc), "target": fam_node(fam.title()), "value": round(share, 1)})

    used = set()
    for l in links:
        used.add(l["source"]); used.add(l["target"])
    nodes = [{"name": n} for n in sorted(used)]
    return {"nodes": nodes, "links": links}


@services_bp.route("/neto-shipping/refresh", methods=["POST"])
@login_required
@require_capability("services.shipping.refresh")
def neto_shipping_refresh():
    """Kick a background re-scrape (idempotent — ignores if already running)."""
    with _refresh_lock:
        if _refresh_state["running"]:
            return jsonify({"status": "already_running", "message": _refresh_state["message"]})
        _refresh_state.update(running=True, message="starting…", started_at=time.time(),
                              finished_at=None, result=None, error=None)
        from flask import current_app
        app = current_app._get_current_object()
        threading.Thread(target=_run_refresh, args=(app,), daemon=True).start()
    return jsonify({"status": "started"})


@services_bp.route("/neto-shipping/refresh/status")
@login_required
@require_capability("services.shipping.view")
def neto_shipping_refresh_status():
    return jsonify(_refresh_state)


def _carrier_family(carrier: str) -> str:
    """Bucket a mapping 'carrier/labelling' string into a colour family."""
    c = (carrier or "").lower()
    if "startrack" in c or "star track" in c:
        return "startrack"
    if "auspost" in c or "eparcel" in c or "australia post" in c:
        return "auspost"
    if "stamp" in c:
        return "stamp"
    if "generic" in c:
        return "generic"
    return "other"


def _build_matrix(snap: dict) -> dict:
    """Category (rows) x active routing-block (cols) grid; cell = service+carrier.

    Mirrors the cPanel `ship` page (routing-group blocks x categories), so staff
    can scan how each category is handled in each routing context."""
    active = [m for m in snap["mapping"] if m.get("block_active")]

    # Columns: active blocks, ordered by routing_group then block_index.
    blocks = {}
    for m in active:
        bi = m["block_index"]
        if bi not in blocks:
            blocks[bi] = {
                "block_index": bi,
                "routing_group": m.get("routing_group") or "—",
                "visible": m.get("block_visible"),
                "services": set(),
            }
        blocks[bi]["services"].add(m.get("service"))
    block_list = sorted(blocks.values(), key=lambda b: (b["routing_group"], b["block_index"]))
    for b in block_list:
        b["discriminates"] = len(b["services"]) > 1  # routes categories to different services

    # Rows: categories appearing in active mappings, ordered by product count desc.
    cat_count = {c["name"]: c.get("product_count", 0) for c in snap["categories"]}
    cat_names = sorted({m["category"] for m in active if m.get("category")},
                       key=lambda n: (-cat_count.get(n, 0), n))

    # Grid: (category, block_index) -> {service, carrier, family}
    grid = {}
    for m in active:
        grid[(m["category"], m["block_index"])] = {
            "service": m.get("service"),
            "carrier": m.get("carrier"),
            "family": _carrier_family(m.get("carrier")),
        }

    # Service config lookup (by name) for cell drill-down.
    svc_cfg = {}
    for sv in snap["services"]:
        svc_cfg[sv["name"]] = {
            "service_id": sv.get("service_id"), "type": sv.get("type"),
            "charge_type": sv.get("charge_type"), "fuel_pct": sv.get("fuel_pct"),
            "handling_amt": sv.get("handling_amt"), "tax_inclusive": sv.get("tax_inclusive"),
            "cubic_modifier": sv.get("cubic_modifier"), "max_length_m": sv.get("max_length_m"),
            "cost_band": sv.get("cost_band"),
        }

    return {
        "blocks": block_list,
        "categories": [{"name": n, "product_count": cat_count.get(n, 0)} for n in cat_names],
        "grid": {f"{c}||{b}": v for (c, b), v in grid.items()},
        "svc_cfg": svc_cfg,
    }
