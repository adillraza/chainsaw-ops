"""Generic annotation thread endpoints + reusable Jinja partial.

Routes:

* ``GET  /annotations/<entity_type>/<entity_id>``   - list (HTML or JSON)
* ``POST /annotations/<entity_type>/<entity_id>``   - create new annotation
* ``POST /annotations/<id>/delete``                 - soft delete an annotation

The HTML responses render :file:`partials/annotation_thread.html` so any
section can mount a thread with one HTMX attribute::

    <div hx-get="/annotations/po_item/123" hx-trigger="load"></div>
"""
from __future__ import annotations

from datetime import datetime

from flask import jsonify, render_template, request
from flask_login import current_user, login_required

from app.blueprints.annotations import annotations_bp
from app.extensions import db
from app.models.annotations import Annotation


def _wants_json() -> bool:
    accept = request.headers.get("Accept", "")
    return "application/json" in accept and "text/html" not in accept


def _serialise_thread(entity_type: str, entity_id: str) -> list[dict]:
    rows = (
        Annotation.query.filter(
            Annotation.entity_type == entity_type,
            Annotation.entity_id == str(entity_id),
            Annotation.deleted_at.is_(None),
        )
        .order_by(Annotation.created_at.asc())
        .all()
    )
    return [row.to_dict() for row in rows]


@annotations_bp.route("/<entity_type>/<entity_id>", methods=["GET"])
@login_required
def list_thread(entity_type: str, entity_id: str):
    annotations = _serialise_thread(entity_type, entity_id)
    if _wants_json():
        return jsonify({"success": True, "annotations": annotations})
    return render_template(
        "partials/annotation_thread.html",
        annotations=annotations,
        entity_type=entity_type,
        entity_id=entity_id,
    )


@annotations_bp.route("/<entity_type>/<entity_id>", methods=["POST"])
@login_required
def create_annotation(entity_type: str, entity_id: str):
    payload = request.get_json(silent=True) or request.form
    comment = (payload.get("comment") or "").strip()
    if not comment:
        if _wants_json():
            return jsonify({"success": False, "error": "Comment is required"}), 400
        return ("Comment is required", 400)

    parent_id = payload.get("parent_id") or None

    annotation = Annotation(
        entity_type=entity_type,
        entity_id=str(entity_id),
        parent_id=parent_id,
        comment=comment,
        author_id=current_user.id,
        author_username=current_user.username,
    )
    db.session.add(annotation)
    db.session.commit()

    if _wants_json():
        return jsonify({"success": True, "annotation": annotation.to_dict()})
    annotations = _serialise_thread(entity_type, entity_id)
    return render_template(
        "partials/annotation_thread.html",
        annotations=annotations,
        entity_type=entity_type,
        entity_id=entity_id,
    )


@annotations_bp.route("/<annotation_id>/delete", methods=["POST"])
@login_required
def delete_annotation(annotation_id: str):
    annotation = Annotation.query.get_or_404(annotation_id)
    if annotation.author_id != current_user.id and not current_user.can("notes.delete_any"):
        return jsonify({"success": False, "error": "Access denied"}), 403

    annotation.deleted_at = datetime.utcnow()
    db.session.commit()

    if _wants_json():
        return jsonify({"success": True})
    annotations = _serialise_thread(annotation.entity_type, annotation.entity_id)
    return render_template(
        "partials/annotation_thread.html",
        annotations=annotations,
        entity_type=annotation.entity_type,
        entity_id=annotation.entity_id,
    )
