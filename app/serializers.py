"""把数据库行组装成对外 JSON（页面差异/标签清单/导出共用同一版本视图）。"""
from __future__ import annotations

import json
import sqlite3
from typing import Any

from . import service


def _batch_eff(conn: sqlite3.Connection, batch_id: int) -> dict[str, Any]:
    b = conn.execute("SELECT * FROM batches WHERE id=?", (batch_id,)).fetchone()
    recall = conn.execute(
        "SELECT reason FROM batch_recalls WHERE batch_id=? ORDER BY id DESC LIMIT 1", (batch_id,)
    ).fetchone()
    return {
        "id": b["id"],
        "material_id": b["material_id"],
        "code": b["code"],
        "received_on": b["received_on"],
        "expires_on": b["expires_on"],
        "status": service.effective_batch_status(b),
        "recorded_status": b["status"],
        "recalled_reason": recall["reason"] if recall else None,
    }


def serialize_component(conn: sqlite3.Connection, row: sqlite3.Row) -> dict[str, Any]:
    material_code = material_name = batch_code = None
    child_label = None
    material_id = row["material_id"]
    if material_id:
        m = conn.execute("SELECT code,name FROM materials WHERE id=?", (material_id,)).fetchone()
        if m:
            material_code, material_name = m["code"], m["name"]
    if row["batch_id"]:
        b = conn.execute("SELECT code,status,expires_on FROM batches WHERE id=?", (row["batch_id"],)).fetchone()
        if b:
            batch_code = b["code"]
    if row["child_version_id"]:
        child_label = service._version_label(conn, row["child_version_id"])
    return {
        "id": row["id"],
        "position": row["position"],
        "kind": row["kind"],
        "material_id": material_id,
        "material_code": material_code,
        "material_name": material_name,
        "batch_id": row["batch_id"],
        "batch_code": batch_code,
        "child_version_id": row["child_version_id"],
        "child_version_label": child_label,
        "qty": row["qty"],
        "material_labels": json.loads(row["material_labels"] or "[]"),
    }


def serialize_version(conn: sqlite3.Connection, version_id: int) -> dict[str, Any]:
    v = conn.execute("SELECT * FROM recipe_versions WHERE id=?", (version_id,)).fetchone()
    recipe = conn.execute("SELECT * FROM recipes WHERE id=?", (v["recipe_id"],)).fetchone()
    comps = [
        serialize_component(conn, c)
        for c in conn.execute(
            "SELECT * FROM components WHERE recipe_version_id=? ORDER BY position", (version_id,)
        )
    ]
    approvals = []
    for a in conn.execute(
        """SELECT a.*, rv.name AS reviewer_name FROM approvals a
             JOIN reviewers rv ON rv.id=a.reviewer_id
            WHERE a.recipe_version_id=? ORDER BY a.id""",
        (version_id,),
    ):
        approvals.append({
            "id": a["id"],
            "reviewer_id": a["reviewer_id"],
            "reviewer_name": a["reviewer_name"],
            "decided_on": a["decided_on"],
            "stale": bool(a["stale"]),
            "stale_reason": a["stale_reason"],
        })
    violations = service.assess_version(conn, version_id)
    active_approvals = [a for a in approvals if not a["stale"]]
    distinct_reviewers = {a["reviewer_id"] for a in active_approvals}
    is_current = recipe["current_version_id"] == version_id
    blockers: list[str] = []
    if not is_current:
        blockers.append("不是当前版本（配方已变化，审批失效）")
    if v["status"] != "in_approval":
        blockers.append("快照已放行")
    blockers.extend(violations)
    return {
        "id": v["id"],
        "recipe_id": v["recipe_id"],
        "version_no": v["version_no"],
        "created_on": v["created_on"],
        "created_by": v["created_by"],
        "content_hash": v["content_hash"],
        "status": v["status"],
        "needs_review": bool(v["needs_review"]),
        "released_on": v["released_on"],
        "note": v["note"],
        "is_current": is_current,
        "components": comps,
        "approvals": approvals,
        "violations": violations,
        "can_release": not blockers,
        "release_blockers": blockers,
        "active_approval_count": len(active_approvals),
        "distinct_approver_count": len(distinct_reviewers),
    }


def serialize_graph(conn: sqlite3.Connection, version_id: int) -> dict[str, Any]:
    graph = service.resolve_graph(conn, version_id)
    v = conn.execute("SELECT content_hash FROM recipe_versions WHERE id=?", (version_id,)).fetchone()
    violations = service.assess_version(conn, version_id)

    enriched_batches = []
    for item in graph["batches"]:
        bid = item["batch_id"]
        b = conn.execute(
            """SELECT b.id, b.code AS batch_code, m.code AS material_code, m.name AS material_name,
                      b.status, b.expires_on
                 FROM batches b JOIN materials m ON m.id=b.material_id WHERE b.id=?""",
            (bid,),
        ).fetchone()
        enriched_batches.append({
            "batch_id": bid,
            "batch_code": b["batch_code"],
            "material_code": b["material_code"],
            "material_name": b["material_name"],
            "status": service.effective_batch_status(b),
            "paths": item["paths"],
        })
    return {
        "version_id": version_id,
        "content_hash": v["content_hash"],
        "labels": graph["labels"],
        "batches": enriched_batches,
        "violations": violations,
        "cycles": graph["cycles"],
    }


def serialize_recipe(conn: sqlite3.Connection, row: sqlite3.Row) -> dict[str, Any]:
    cur_no = cur_status = None
    needs_review = False
    if row["current_version_id"]:
        cv = conn.execute(
            "SELECT version_no,status,needs_review FROM recipe_versions WHERE id=?",
            (row["current_version_id"],),
        ).fetchone()
        cur_no, cur_status = cv["version_no"], cv["status"]
        needs_review = bool(cv["needs_review"])
    return {
        "id": row["id"],
        "code": row["code"],
        "name": row["name"],
        "revision": row["revision"],
        "current_version_id": row["current_version_id"],
        "current_version_no": cur_no,
        "current_status": cur_status,
        "current_needs_review": needs_review,
    }


def serialize_proposal(conn: sqlite3.Connection, row: sqlite3.Row) -> dict[str, Any]:
    m = conn.execute("SELECT name FROM materials WHERE id=?", (row["new_material_id"],)).fetchone()
    b = conn.execute("SELECT code FROM batches WHERE id=?", (row["new_batch_id"],)).fetchone()
    rv = conn.execute("SELECT name FROM reviewers WHERE id=?", (row["proposed_by"],)).fetchone()
    return {
        "id": row["id"],
        "recipe_id": row["recipe_id"],
        "base_version_id": row["base_version_id"],
        "component_id": row["component_id"],
        "new_material_id": row["new_material_id"],
        "new_material_name": m["name"],
        "new_batch_id": row["new_batch_id"],
        "new_batch_code": b["code"],
        "proposed_by": row["proposed_by"],
        "proposer_name": rv["name"],
        "reason": row["reason"],
        "created_on": row["created_on"],
        "status": row["status"],
        "resulting_version_id": row["resulting_version_id"],
    }
