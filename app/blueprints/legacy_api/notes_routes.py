"""BigQuery-backed notes endpoints used by the legacy PO Cross Check UI."""
from __future__ import annotations

from flask import jsonify, request
from flask_login import current_user, login_required

from app.blueprints.legacy_api import legacy_api_bp
from app.services.cache import refresh_po_cache, update_cache_with_latest_note
from app.services.purchase_orders_service import purchase_orders_service


@legacy_api_bp.route("/notes/save", methods=["POST"])
@login_required
def save_note():
    try:
        data = request.get_json()
        po_item_id = data.get("po_item_id")
        po_id = data.get("po_id")
        sku = data.get("sku")
        comment = data.get("comment")

        if not all([po_item_id, po_id, comment]):
            return jsonify({"success": False, "error": "Missing required fields"}), 400

        success, message = purchase_orders_service.save_item_note(
            po_item_id=po_item_id,
            po_id=po_id,
            sku=sku,
            comment=comment,
            username=current_user.username,
        )

        if success:
            try:
                update_cache_with_latest_note(po_item_id, po_id)
            except Exception as e:
                print(f"Warning: Failed to update cache for po_item_id {po_item_id}: {str(e)}")

            return jsonify({"success": True, "message": message})
        return jsonify({"success": False, "error": message}), 500

    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500


@legacy_api_bp.route("/notes/<po_item_id>", methods=["GET"])
@login_required
def get_notes(po_item_id: str):
    try:
        notes, error = purchase_orders_service.get_item_notes(po_item_id)
        if error:
            return jsonify({"success": False, "error": error}), 500
        return jsonify({"success": True, "notes": notes})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500


@legacy_api_bp.route("/notes/<note_id>/delete", methods=["POST"])
@login_required
def delete_note(note_id: str):
    try:
        data = request.get_json()
        po_item_id = data.get("po_item_id")
        po_id = data.get("po_id")

        if not po_item_id or not po_id:
            return jsonify({"success": False, "error": "Missing po_item_id or po_id"}), 400

        success, message = purchase_orders_service.delete_item_note(
            note_id=note_id,
            username=current_user.username,
        )

        if success:
            try:
                update_cache_with_latest_note(po_item_id, po_id)
                print(f"Cache updated after deleting note {note_id} for po_item_id {po_item_id}")
            except Exception as e:
                print(
                    f"Warning: Failed to update cache after deletion for po_item_id {po_item_id}: {str(e)}"
                )

            return jsonify({"success": True, "message": message})
        return jsonify({"success": False, "error": message}), 500

    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500


@legacy_api_bp.route("/po-notes/save", methods=["POST"])
@login_required
def save_po_note():
    try:
        data = request.get_json()
        po_id = data.get("po_id")
        order_id = data.get("order_id")
        comment = data.get("comment")

        if not all([po_id, comment]):
            return jsonify({"success": False, "error": "Missing required fields"}), 400

        success, message = purchase_orders_service.save_po_note(
            po_id=po_id,
            order_id=order_id,
            comment=comment,
            username=current_user.username,
        )

        if success:
            try:
                refresh_po_cache(po_id)
            except Exception as e:
                print(f"Warning: Failed to refresh cache for PO {po_id}: {str(e)}")

            return jsonify({"success": True, "message": message})
        return jsonify({"success": False, "error": message}), 500

    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500


@legacy_api_bp.route("/po-notes/<po_id>", methods=["GET"])
@login_required
def get_po_notes(po_id: str):
    try:
        notes, error = purchase_orders_service.get_po_notes(po_id)
        if error:
            return jsonify({"success": False, "error": error}), 500
        return jsonify({"success": True, "notes": notes})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500


@legacy_api_bp.route("/po-item-notes/<po_id>", methods=["GET"])
@login_required
def get_all_item_notes_for_po(po_id: str):
    try:
        notes, error = purchase_orders_service.get_all_item_notes_for_po(po_id)
        if error:
            return jsonify({"success": False, "error": error}), 500
        return jsonify({"success": True, "notes": notes})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500
