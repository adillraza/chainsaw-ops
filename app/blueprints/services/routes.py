"""Services landing + NETO Shippings tab.

Scaffold stage: the landing page lists available services and the NETO
Shippings route renders a placeholder. The scraper, BigQuery snapshot, and
visualisations are wired in by subsequent tasks.
"""
from __future__ import annotations

import threading
import time

from flask import jsonify, render_template, request
from flask_login import current_user, login_required

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


# ---------------------------------------------------------------------------
# NETO Advanced Config — background re-scrape state
# ---------------------------------------------------------------------------
_config_refresh_lock = threading.Lock()
_config_refresh_state: dict = {"running": False, "message": "", "started_at": None,
                              "finished_at": None, "result": None, "error": None}


def _run_config_refresh(app):
    from app.services.neto_config_scraper import runner
    from app.services import neto_config_service as nc

    def progress(msg):
        _config_refresh_state["message"] = msg

    try:
        result = runner.run_scrape(progress=progress)
        progress("mirroring to local store…")
        with app.app_context():
            nc.mirror_to_local()  # BQ -> SQLite (busts the BQ cache internally)
        _config_refresh_state.update(result=result, error=None, message="done")
    except Exception as exc:  # noqa: BLE001
        _config_refresh_state.update(error=str(exc), message="failed")
    finally:
        _config_refresh_state.update(running=False, finished_at=time.time())


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
        "title": "Freight Calculator",
        "description": "Look up an item's shipping details, what Neto would quote, and the "
                       "live Startrack / AusPost cost — no more manual carrier calculators.",
        "icon": "fa-calculator",
        "endpoint": "services.st_calculator",
        "capability": "services.calculator.view",
        "available": True,
    },
    {
        "key": "neto_config",
        "title": "NETO Advanced Config",
        "description": "Browse every Neto cPanel configuration variable — value, type, "
                       "module and description — in one searchable place.",
        "icon": "fa-sliders",
        "endpoint": "services.neto_config",
        "capability": "services.config.view",
        "available": True,
    },
    {
        "key": "work_diary",
        "title": "Adil Work Diary",
        "description": "Your personal task tracker — tasks pulled from email, with status, "
                       "comments and history. Most recent on top.",
        "icon": "fa-list-check",
        "endpoint": "services.work_diary",
        "capability": "services.work_diary.view",
        "available": True,
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


@services_bp.route("/st-calculator")
@login_required
@require_capability("services.calculator.view")
def st_calculator():
    """Freight Calculator — incremental build. Panel 1: Item Details."""
    from app.services import st_calculator_service as calc

    sku = (request.args.get("sku") or "").strip()
    product = None
    error = None
    if sku:
        try:
            product = calc.get_product(sku)
            if product is None:
                error = f"No product found for SKU '{sku}'."
        except Exception as exc:  # noqa: BLE001
            error = f"Lookup failed: {exc}"
    postcode = (request.args.get("postcode") or "").strip()
    suburb = (request.args.get("suburb") or "").strip()
    try:
        qty = max(1, int(request.args.get("qty") or 1))
    except (TypeError, ValueError):
        qty = 1
    suburbs = calc.suburbs_for_postcode(postcode) if (product and postcode) else []
    quote = None
    if product and postcode and suburb:
        try:
            quote = calc.neto_quotes(product, qty, postcode, suburb)
        except Exception as exc:  # noqa: BLE001
            error = error or f"Quote failed: {exc}"
    return render_template(
        "services/st_calculator.html",
        sku=sku, product=product, error=error,
        postcode=postcode, suburb=suburb, qty=qty, suburbs=suburbs, quote=quote,
    )


@services_bp.route("/st-calculator/postcode/<pc>")
@login_required
@require_capability("services.calculator.view")
def st_calculator_postcode(pc):
    """JSON suburbs for a postcode (for the dependent dropdown)."""
    from app.services import st_calculator_service as calc
    try:
        return jsonify({"postcode": pc, "suburbs": calc.suburbs_for_postcode(pc)})
    except Exception as exc:  # noqa: BLE001
        return jsonify({"postcode": pc, "suburbs": [], "error": str(exc)}), 200


@services_bp.route("/neto-config")
@login_required
@require_capability("services.config.view")
def neto_config():
    """NETO Advanced Config tab — searchable list of all cPanel config variables."""
    from app.services import neto_config_service as nc

    error = None
    snap = None
    try:
        # Fast local SQLite mirror; on a miss build from BigQuery and seed it.
        snap = nc.get_local()
        if snap is None:
            snap = nc.build_payload()
            if snap.get("snapshot_id"):
                try:
                    nc.mirror_to_local(snap)
                except Exception:  # noqa: BLE001
                    import logging as _l
                    _l.getLogger(__name__).warning("neto_config mirror seed failed", exc_info=True)
    except Exception as exc:  # noqa: BLE001
        error = str(exc)

    return render_template("services/neto_config.html", error=error, snap=snap)


@services_bp.route("/work-diary")
@login_required
@require_capability("services.work_diary.view")
def work_diary():
    """Adil Work Diary — task table (newest first) backed by BigQuery."""
    from app.services import work_diary_service as wd

    error = None
    tasks = []
    try:
        tasks = wd.get_tasks()
    except Exception as exc:  # noqa: BLE001
        error = str(exc)
    return render_template(
        "services/work_diary.html",
        error=error, tasks=tasks, statuses=wd.STATUSES,
    )


@services_bp.route("/work-diary/<task_id>/priority", methods=["POST"])
@login_required
@require_capability("services.work_diary.view")
def work_diary_priority(task_id):
    """Set a task's 1-5 star priority (0 clears it). Returns JSON."""
    from app.services import work_diary_service as wd

    try:
        result = wd.set_priority(task_id, request.form.get("priority"))
    except ValueError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400
    except LookupError:
        return jsonify({"ok": False, "error": "task not found"}), 404
    except Exception as exc:  # noqa: BLE001
        return jsonify({"ok": False, "error": str(exc)}), 500
    return jsonify({"ok": True, **result})


@services_bp.route("/work-diary/<task_id>/status", methods=["POST"])
@login_required
@require_capability("services.work_diary.view")
def work_diary_status(task_id):
    """Change a task's status (logs the transition). Returns JSON."""
    from app.services import work_diary_service as wd

    new_status = (request.form.get("status") or "").strip()
    try:
        result = wd.update_status(task_id, new_status, current_user.username)
    except ValueError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400
    except LookupError:
        return jsonify({"ok": False, "error": "task not found"}), 404
    except Exception as exc:  # noqa: BLE001
        return jsonify({"ok": False, "error": str(exc)}), 500
    return jsonify({"ok": True, **result})


@services_bp.route("/work-diary/<task_id>/comment", methods=["POST"])
@login_required
@require_capability("services.work_diary.view")
def work_diary_comment(task_id):
    """Append a comment to a task. Returns the new comment as JSON."""
    from app.services import work_diary_service as wd

    comment = request.form.get("comment") or ""
    try:
        new_comment = wd.add_comment(task_id, comment, current_user.username)
    except ValueError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400
    except LookupError:
        return jsonify({"ok": False, "error": "task not found"}), 404
    except Exception as exc:  # noqa: BLE001
        return jsonify({"ok": False, "error": str(exc)}), 500
    return jsonify({"ok": True, "comment": new_comment})


@services_bp.route("/neto-config/refresh", methods=["POST"])
@login_required
@require_capability("services.config.refresh")
def neto_config_refresh():
    """Kick a background re-scrape of the Advanced Configuration (idempotent)."""
    with _config_refresh_lock:
        if _config_refresh_state["running"]:
            return jsonify({"status": "already_running", "message": _config_refresh_state["message"]})
        _config_refresh_state.update(running=True, message="starting…", started_at=time.time(),
                                     finished_at=None, result=None, error=None)
        from flask import current_app
        app = current_app._get_current_object()
        threading.Thread(target=_run_config_refresh, args=(app,), daemon=True).start()
    return jsonify({"status": "started"})


@services_bp.route("/neto-config/refresh/status")
@login_required
@require_capability("services.config.view")
def neto_config_refresh_status():
    return jsonify(_config_refresh_state)


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
