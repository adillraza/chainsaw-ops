"""Adil Work Diary — BigQuery-backed task store.

Single source of truth lives in BigQuery (``operations.work_diary`` +
``work_diary_comments`` + ``work_diary_status_history``). This service reads the
task list and applies status changes / comments via DML. Volume is tiny (tens of
rows), so DML latency is a non-issue and there is no SQLite mirror.

Reuses the BigQuery client from :class:`purchase_orders_service.BigQueryService`
so service-account credentials live in exactly one place.
"""
from __future__ import annotations

import uuid

from google.cloud import bigquery

from app.template_filters import format_dt

PROJECT = "chainsawspares-385722"
DATASET = "operations"
T_TASKS = f"`{PROJECT}.{DATASET}.work_diary`"
T_COMMENTS = f"`{PROJECT}.{DATASET}.work_diary_comments`"
T_HISTORY = f"`{PROJECT}.{DATASET}.work_diary_status_history`"

# Locked status set (per build decision). Used to validate writes.
STATUSES = ("Backlog", "Inprogress", "Completed")

_bq = None


def _client():
    """Lazy, process-wide BigQuery client (shares creds with the PO service)."""
    global _bq
    if _bq is None:
        from app.services.purchase_orders_service import BigQueryService
        _bq = BigQueryService()
    return _bq.client


def _params(*pairs):
    return bigquery.QueryJobConfig(
        query_parameters=[bigquery.ScalarQueryParameter(n, t, v) for n, t, v in pairs]
    )


def get_tasks() -> list[dict]:
    """All tasks, newest-first, each with its (non-deleted) comments nested."""
    client = _client()
    if client is None:
        raise RuntimeError("BigQuery client not initialised")

    task_rows = list(client.query(f"""
        SELECT task_id, source_message_id, source_subject, source_sender, link_to_email,
               title, clean_description, original_text, received_at, status, priority,
               status_changed_at, completed_at, created_at, updated_at
        FROM {T_TASKS}
        ORDER BY received_at DESC, created_at DESC
    """).result())

    comment_rows = list(client.query(f"""
        SELECT comment_id, task_id, comment, username, created_at
        FROM {T_COMMENTS}
        WHERE deleted_at IS NULL
        ORDER BY created_at ASC
    """).result())

    comments_by_task: dict[str, list] = {}
    for c in comment_rows:
        comments_by_task.setdefault(c["task_id"], []).append({
            "comment_id": c["comment_id"],
            "comment": c["comment"],
            "username": c["username"],
            "created_display": format_dt(c["created_at"], "datetime"),
        })

    tasks = []
    for r in task_rows:
        tasks.append({
            "task_id": r["task_id"],
            "source_subject": r["source_subject"],
            "source_sender": r["source_sender"],
            "link_to_email": r["link_to_email"],
            "title": r["title"],
            "clean_description": r["clean_description"],
            "original_text": r["original_text"],
            "status": r["status"],
            "priority": r["priority"],
            "received_display": format_dt(r["received_at"], "datetime"),
            "status_changed_display": format_dt(r["status_changed_at"], "datetime"),
            "completed_display": format_dt(r["completed_at"], "datetime"),
            "comments": comments_by_task.get(r["task_id"], []),
        })
    return tasks


def update_status(task_id: str, new_status: str, username: str) -> dict:
    """Set a task's status; logs the transition. Returns refreshed display fields."""
    if new_status not in STATUSES:
        raise ValueError(f"invalid status {new_status!r}")
    client = _client()

    cur = list(client.query(
        f"SELECT status FROM {T_TASKS} WHERE task_id=@id",
        job_config=_params(("id", "STRING", task_id)),
    ).result())
    if not cur:
        raise LookupError("task not found")
    from_status = cur[0]["status"]

    client.query(f"""
        UPDATE {T_TASKS}
        SET status=@status,
            status_changed_at=CURRENT_TIMESTAMP(),
            completed_at=CASE WHEN @status='Completed' THEN CURRENT_TIMESTAMP() ELSE NULL END,
            updated_at=CURRENT_TIMESTAMP()
        WHERE task_id=@id
    """, job_config=_params(
        ("status", "STRING", new_status), ("id", "STRING", task_id),
    )).result()

    client.query(f"""
        INSERT INTO {T_HISTORY} (history_id, task_id, from_status, to_status, changed_by, changed_at)
        VALUES (@hid, @id, @from, @to, @by, CURRENT_TIMESTAMP())
    """, job_config=_params(
        ("hid", "STRING", str(uuid.uuid4())), ("id", "STRING", task_id),
        ("from", "STRING", from_status), ("to", "STRING", new_status),
        ("by", "STRING", username),
    )).result()

    row = list(client.query(
        f"SELECT status, status_changed_at, completed_at FROM {T_TASKS} WHERE task_id=@id",
        job_config=_params(("id", "STRING", task_id)),
    ).result())[0]
    return {
        "status": row["status"],
        "status_changed_display": format_dt(row["status_changed_at"], "datetime"),
        "completed_display": format_dt(row["completed_at"], "datetime"),
    }


def set_priority(task_id: str, priority) -> dict:
    """Set a task's priority as a 1-5 star rating, or clear it (0/None → NULL)."""
    if priority in (None, "", "0", 0):
        value = None
    else:
        try:
            n = int(priority)
        except (TypeError, ValueError):
            raise ValueError(f"invalid priority {priority!r}")
        if not 1 <= n <= 5:
            raise ValueError("priority must be 1-5")
        value = str(n)

    client = _client()
    exists = list(client.query(
        f"SELECT 1 FROM {T_TASKS} WHERE task_id=@id",
        job_config=_params(("id", "STRING", task_id)),
    ).result())
    if not exists:
        raise LookupError("task not found")

    client.query(f"""
        UPDATE {T_TASKS}
        SET priority=@priority, updated_at=CURRENT_TIMESTAMP()
        WHERE task_id=@id
    """, job_config=_params(
        ("priority", "STRING", value), ("id", "STRING", task_id),
    )).result()
    return {"priority": value}


def add_comment(task_id: str, comment: str, username: str) -> dict:
    """Append a comment to a task. Returns the new comment for the UI."""
    comment = (comment or "").strip()
    if not comment:
        raise ValueError("empty comment")
    client = _client()

    exists = list(client.query(
        f"SELECT 1 FROM {T_TASKS} WHERE task_id=@id",
        job_config=_params(("id", "STRING", task_id)),
    ).result())
    if not exists:
        raise LookupError("task not found")

    comment_id = str(uuid.uuid4())
    client.query(f"""
        INSERT INTO {T_COMMENTS} (comment_id, task_id, comment, username, created_at, updated_at)
        VALUES (@cid, @id, @comment, @by, CURRENT_TIMESTAMP(), CURRENT_TIMESTAMP())
    """, job_config=_params(
        ("cid", "STRING", comment_id), ("id", "STRING", task_id),
        ("comment", "STRING", comment), ("by", "STRING", username),
    )).result()

    from datetime import datetime, timezone
    return {
        "comment_id": comment_id,
        "comment": comment,
        "username": username,
        "created_display": format_dt(datetime.now(timezone.utc), "datetime"),
    }
