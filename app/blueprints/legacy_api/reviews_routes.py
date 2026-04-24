"""Item-review workflow JSON endpoints used by Retail / Warehouse pages."""
from __future__ import annotations

import json
import uuid
from datetime import datetime

from flask import jsonify, request
from flask_login import current_user, login_required

from app.auth.abilities import user_can
from app.blueprints.legacy_api import legacy_api_bp
from app.extensions import db
from app.models.reviews import (
    CLOSED_REVIEW_STATUSES,
    OPEN_REVIEW_STATUSES,
    ItemReview,
)
from app.services.reviews_sync import sync_review_to_bigquery


@legacy_api_bp.route("/reviews/flag", methods=["POST"])
@login_required
def flag_item_for_review():
    if not user_can(current_user, "reviews.flag"):
        return jsonify({"success": False, "error": "Access denied"}), 403
    try:
        data = request.get_json() or {}
        po_id = data.get("po_id")
        po_item_id = data.get("po_item_id")
        if not po_id or not po_item_id:
            return jsonify({"success": False, "error": "Missing po_id or po_item_id"}), 400

        existing = ItemReview.query.filter(
            ItemReview.po_id == po_id,
            ItemReview.po_item_id == po_item_id,
            ItemReview.status.in_(OPEN_REVIEW_STATUSES),
        ).first()
        if existing:
            return jsonify({
                "success": False,
                "error": "Item already flagged for review",
                "review": existing.to_dict(),
            }), 400

        review = ItemReview(
            review_id=uuid.uuid4().hex,
            po_id=po_id,
            order_id=data.get("order_id"),
            po_item_id=po_item_id,
            sku=data.get("sku"),
            flagged_by=current_user.username,
            flag_comment=data.get("comment"),
            comparison_snapshot=json.dumps(data.get("comparison_snapshot") or {}),
        )
        db.session.add(review)
        db.session.commit()

        sync_review_to_bigquery(review)

        return jsonify({
            "success": True,
            "message": "Item flagged for warehouse review",
            "review": review.to_dict(),
        })
    except Exception as e:
        db.session.rollback()
        print(f"Error flagging item for review: {str(e)}")
        return jsonify({"success": False, "error": str(e)}), 500


@legacy_api_bp.route("/reviews", methods=["GET"])
@login_required
def list_reviews():
    if not user_can(current_user, "reviews.warehouse.view"):
        return jsonify({"success": False, "error": "Access denied"}), 403
    open_reviews = (
        ItemReview.query.filter(ItemReview.status.in_(OPEN_REVIEW_STATUSES))
        .order_by(ItemReview.flagged_at.desc().nullslast())
        .all()
    )
    closed_reviews = (
        ItemReview.query.filter(ItemReview.status.in_(CLOSED_REVIEW_STATUSES))
        .order_by(ItemReview.flagged_at.desc().nullslast())
        .limit(100)
        .all()
    )
    return jsonify({
        "success": True,
        "open_reviews": [review.to_dict() for review in open_reviews],
        "closed_reviews": [review.to_dict() for review in closed_reviews],
    })


@legacy_api_bp.route("/reviews/retail", methods=["GET"])
@login_required
def list_retail_reviews():
    if not user_can(current_user, "reviews.retail.view"):
        return jsonify({"success": False, "error": "Access denied"}), 403
    awaiting_retail = (
        ItemReview.query.filter(ItemReview.status == "warehouse_closed")
        .order_by(
            ItemReview.warehouse_closed_at.desc().nullslast(),
            ItemReview.flagged_at.desc().nullslast(),
        )
        .all()
    )
    pending_warehouse = (
        ItemReview.query.filter(ItemReview.status.in_(OPEN_REVIEW_STATUSES))
        .order_by(ItemReview.flagged_at.desc().nullslast())
        .all()
    )
    completed_reviews = (
        ItemReview.query.filter(ItemReview.status == "retail_closed")
        .order_by(
            ItemReview.retail_closed_at.desc().nullslast(),
            ItemReview.flagged_at.desc().nullslast(),
        )
        .limit(150)
        .all()
    )
    return jsonify({
        "success": True,
        "awaiting_retail": [review.to_dict() for review in awaiting_retail],
        "pending_warehouse": [review.to_dict() for review in pending_warehouse],
        "completed_reviews": [review.to_dict() for review in completed_reviews],
    })


@legacy_api_bp.route("/reviews/<review_id>/close", methods=["POST"])
@login_required
def close_review(review_id: str):
    if not user_can(current_user, "reviews.warehouse.close"):
        return jsonify({"success": False, "error": "Access denied"}), 403
    review = ItemReview.query.filter_by(review_id=review_id).first_or_404()
    data = request.get_json() or {}
    comment = (data.get("comment") or "").strip()
    if not comment:
        return jsonify({"success": False, "error": "Comment is required to close a review."}), 400
    review.status = "warehouse_closed"
    review.warehouse_comment = comment
    review.warehouse_assigned_to = current_user.username
    review.warehouse_closed_at = datetime.utcnow()
    review.updated_at = datetime.utcnow()
    db.session.commit()
    sync_review_to_bigquery(review)
    return jsonify({"success": True, "review": review.to_dict()})


@legacy_api_bp.route("/reviews/<review_id>/retail-close", methods=["POST"])
@login_required
def retail_close_review(review_id: str):
    if not user_can(current_user, "reviews.retail.close"):
        return jsonify({"success": False, "error": "Access denied"}), 403
    review = ItemReview.query.filter_by(review_id=review_id).first_or_404()
    if review.status == "retail_closed":
        return jsonify({"success": False, "error": "Review already completed by retail."}), 400
    if review.status != "warehouse_closed":
        return jsonify({
            "success": False,
            "error": "Only warehouse-completed reviews can be resolved by retail.",
        }), 400
    data = request.get_json() or {}
    comment = (data.get("comment") or "").strip()
    review.status = "retail_closed"
    if comment:
        review.retail_comment = comment
    review.retail_closed_by = current_user.username
    review.retail_closed_at = datetime.utcnow()
    review.updated_at = datetime.utcnow()
    db.session.commit()
    sync_review_to_bigquery(review)
    return jsonify({"success": True, "review": review.to_dict()})
