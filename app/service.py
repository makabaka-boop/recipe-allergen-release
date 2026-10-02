"""核心业务规则（纯服务层，FastAPI 之外也可直接调用/测试）。"""
from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import datetime
from typing import Any, Optional

from . import config


# ---------------------------------------------------------------------------
# 小工具
# ---------------------------------------------------------------------------
def now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


def effective_batch_status(row: sqlite3.Row, today_iso: Optional[str] = None) -> str:
    """批次状态：recalled 优先；active 但已过保质期则视为 expired（不偷偷改历史）。"""
    today_iso = today_iso or config.today().isoformat()
    if row["status"] == "recalled":
        return "recalled"
    if row["expires_on"] < today_iso:
        return "expired"
    return "active"


def _material_labels(conn: sqlite3.Connection, material_id: int) -> list[str]:
    rows = conn.execute(
        "SELECT allergen_code FROM material_allergens WHERE material_id=? ORDER BY allergen_code",
        (material_id,),
    ).fetchall()
    return [r["allergen_code"] for r in rows]


def _content_hash(recipe_id: int, payload: list[dict[str, Any]]) -> str:
    canon = json.dumps(
        {"recipe_id": recipe_id, "components": payload},
        ensure_ascii=False,
        sort_keys=True,
    )
    return hashlib.sha256(canon.encode("utf-8")).hexdigest()[:16]


class ServiceError(Exception):
    """业务校验失败。status 为建议的 HTTP 状态码。"""

    def __init__(self, message: str, status: int = 422):
        super().__init__(message)
        self.status = status


# ---------------------------------------------------------------------------
# 标签 / 批次传播图解析（不可变快照 + 当前批次实况）
# ---------------------------------------------------------------------------
def _version_label(conn: sqlite3.Connection, vid: int) -> str:
    row = conn.execute(
        """SELECT r.code AS recipe_code, r.name AS recipe_name, v.version_no
             FROM recipe_versions v JOIN recipes r ON r.id=v.recipe_id
            WHERE v.id=?""",
        (vid,),
    ).fetchone()
    return f"{row['recipe_code']} v{row['version_no']}（{row['recipe_name']}）" if row else f"v{vid}"


def resolve_graph(conn: sqlite3.Connection, version_id: int) -> dict[str, Any]:
    """沿组件引用展开整棵配方树。

    返回：
      labels:  allergen code -> {name, paths[ [hop,...] ]}   每个过敏原由哪些引用路径传播而来
      batches: batch_id -> paths（召回时展示受影响传播路径）
      cycles:  检测到的循环引用描述
    路径中的原料过敏原取自 components.material_labels 快照（版本不可变的一部分）。
    """
    allergen_names = {r["code"]: r["name"] for r in conn.execute("SELECT code,name FROM allergens")}
    labels: dict[str, dict[str, Any]] = {}
    batches: dict[int, list[tuple]] = {}
    cycles: list[str] = []

    def add_path(bucket: dict, key: tuple):
        bucket.setdefault(key[0], [])
        sig = tuple((h["kind"], h["code"], h.get("batch_code") or "", h.get("qty", "")) for h in key[1])
        for existing in bucket[key[0]]:
            ex_sig = tuple((h["kind"], h["code"], h.get("batch_code") or "", h.get("qty", "")) for h in existing)
            if ex_sig == sig:
                return
        bucket[key[0]].append(key[1])

    def walk(vid: int, path: list[dict], stack: set[int]):
        if vid in stack:
            chain = " -> ".join([_version_label(conn, x) for x in [*stack, vid]])
            cycles.append(chain)
            return
        stack = stack | {vid}
        comps = conn.execute(
            "SELECT * FROM components WHERE recipe_version_id=? ORDER BY position", (vid,)
        ).fetchall()
        for c in comps:
            if c["kind"] == "material":
                m = conn.execute("SELECT code,name FROM materials WHERE id=?", (c["material_id"],)).fetchone()
                b_code = None
                if c["batch_id"]:
                    b = conn.execute("SELECT code FROM batches WHERE id=?", (c["batch_id"],)).fetchone()
                    b_code = b["code"] if b else None
                hop = {
                    "depth": len(path),
                    "kind": "material",
                    "code": m["code"] if m else f"material#{c['material_id']}",
                    "name": m["name"] if m else "(已删除原料)",
                    "batch_code": b_code,
                    "qty": c["qty"],
                }
                new_path = [*path, hop]
                for code in json.loads(c["material_labels"] or "[]"):
                    add_path(labels, (code, new_path))
                if c["batch_id"]:
                    add_path(batches, (c["batch_id"], new_path))
            else:
                child = conn.execute(
                    """SELECT v.id, r.code, r.name, v.version_no
                         FROM recipe_versions v JOIN recipes r ON r.id=v.recipe_id
                        WHERE v.id=?""",
                    (c["child_version_id"],),
                ).fetchone()
                hop = {
                    "depth": len(path),
                    "kind": "subrecipe",
                    "code": child["code"] if child else f"version#{c['child_version_id']}",
                    "name": f"{child['name']} v{child['version_no']}" if child else "(缺失子配方)",
                    "batch_code": None,
                    "qty": c["qty"],
                }
                if child:
                    walk(child["id"], [*path, hop], stack)

    walk(version_id, [], set())

    return {
        "labels": [
            {"code": code, "name": allergen_names.get(code, code),
             "paths": labels_for}
            for code, labels_for in sorted(labels.items())
        ],
        "batches": [{"batch_id": bid, "paths": p} for bid, p in sorted(batches.items())],
        "cycles": sorted(set(cycles)),
    }


def assess_version(conn: sqlite3.Connection, version_id: int) -> list[str]:
    """活体规则检查（审批/发布闸口）：失效批次、循环引用、子配方未放行/待复核、未绑批次。"""
    today_iso = config.today().isoformat()
    problems: list[str] = []
    graph = resolve_graph(conn, version_id)
    for chain in graph["cycles"]:
        problems.append(f"检测到循环引用：{chain}")

    seen_batches: set[int] = set()

    def walk(vid: int, stack: set[int]):
        if vid in stack:
            return  # 循环已由 resolve_graph 报告
        stack = stack | {vid}
        for c in conn.execute(
            "SELECT * FROM components WHERE recipe_version_id=? ORDER BY position", (vid,)
        ).fetchall():
            if c["kind"] == "material":
                if not c["batch_id"]:
                    m = conn.execute("SELECT name FROM materials WHERE id=?", (c["material_id"],)).fetchone()
                    problems.append(f"原料「{m['name'] if m else c['material_id']}」未绑定供应批次，不能放行")
                    continue
                if c["batch_id"] in seen_batches:
                    continue
                seen_batches.add(c["batch_id"])
                b = conn.execute("SELECT * FROM batches WHERE id=?", (c["batch_id"],)).fetchone()
                if not b:
                    problems.append(f"批次 #{c['batch_id']} 已不存在")
                    continue
                eff = effective_batch_status(b, today_iso)
                if eff == "recalled":
                    problems.append(f"供应批次「{b['code']}」已被召回，不能放行；请替换原料或使用新批次")
                elif eff == "expired":
                    problems.append(f"供应批次「{b['code']}」已于 {b['expires_on']} 过期（失效批次），不能放行")
            else:
                child = conn.execute(
                    "SELECT * FROM recipe_versions WHERE id=?", (c["child_version_id"],)
                ).fetchone()
                if not child:
                    problems.append(f"引用的子配方版本 #{c['child_version_id']} 不存在")
                    continue
                if child["status"] != "released":
                    problems.append(f"子配方「{_version_label(conn, child['id'])}」尚未放行，父配方不能放行")
                if child["needs_review"]:
                    problems.append(f"子配方「{_version_label(conn, child['id'])}」命中召回待复核，父配方不能放行")
                walk(child["id"], stack)

    walk(version_id, set())
    # 去重保序
    return list(dict.fromkeys(problems))


# ---------------------------------------------------------------------------
# 版本创建（不可变快照）
# ---------------------------------------------------------------------------
def _canonical_components(conn: sqlite3.Connection, comps: list[dict[str, Any]]) -> list[dict[str, Any]]:
    payload: list[dict[str, Any]] = []
    for pos, c in enumerate(comps):
        if c["kind"] == "material":
            if not c.get("material_id"):
                raise ServiceError("原料组件必须指定 material_id", 400)
            m = conn.execute("SELECT * FROM materials WHERE id=?", (c["material_id"],)).fetchone()
            if not m:
                raise ServiceError(f"原料 #{c['material_id']} 不存在", 404)
            if not c.get("batch_id"):
                raise ServiceError(f"原料「{m['name']}」必须绑定供应批次后才能进入配方版本", 422)
            b = conn.execute("SELECT * FROM batches WHERE id=?", (c["batch_id"],)).fetchone()
            if not b:
                raise ServiceError(f"批次 #{c['batch_id']} 不存在", 404)
            if b["material_id"] != c["material_id"]:
                raise ServiceError(f"批次「{b['code']}」不属于原料「{m['name']}」", 422)
            eff = effective_batch_status(b)
            if eff == "recalled":
                raise ServiceError(f"批次「{b['code']}」已被召回，不得用于新配方版本", 422)
            if eff == "expired":
                raise ServiceError(f"批次「{b['code']}」已过期（失效批次），不得用于新配方版本", 422)
            payload.append({
                "kind": "material",
                "material_id": c["material_id"],
                "batch_id": c["batch_id"],
                "qty": c.get("qty", ""),
                "labels": _material_labels(conn, c["material_id"]),
            })
        elif c["kind"] == "subrecipe":
            if not c.get("child_version_id"):
                raise ServiceError("子配方组件必须指定 child_version_id", 400)
            child = conn.execute("SELECT * FROM recipe_versions WHERE id=?", (c["child_version_id"],)).fetchone()
            if not child:
                raise ServiceError(f"子配方版本 #{c['child_version_id']} 不存在", 404)
            payload.append({
                "kind": "subrecipe",
                "child_version_id": c["child_version_id"],
                "qty": c.get("qty", ""),
            })
        else:
            raise ServiceError(f"未知组件类型 {c['kind']}", 400)
    return payload


def create_version(
    conn: sqlite3.Connection,
    recipe_id: int,
    created_by: str,
    components: list[dict[str, Any]],
    note: str = "",
    expected_current_version_id: Optional[int] = None,
) -> int:
    """写入一个新的不可变配方版本并把配方头指针指向它。返回新版本 id。"""
    if not components:
        raise ServiceError("配方至少包含一个组件", 422)
    canonical = _canonical_components(conn, components)
    h = _content_hash(recipe_id, canonical)

    recipe = conn.execute("SELECT * FROM recipes WHERE id=?", (recipe_id,)).fetchone()
    if not recipe:
        raise ServiceError(f"配方 #{recipe_id} 不存在", 404)

    # 乐观并发：调用方必须基于当前最新版本提出改动（并发替换 / 同时编辑）
    if (
        expected_current_version_id is not None
        and recipe["current_version_id"] is not None
        and expected_current_version_id != recipe["current_version_id"]
    ):
        raise ServiceError(
            "配方已被其他人修改（基础版本已变化），请基于最新版本重新提出", 409
        )

    if recipe["current_version_id"] is not None:
        cur = conn.execute(
            "SELECT content_hash FROM recipe_versions WHERE id=?",
            (recipe["current_version_id"],),
        ).fetchone()
        if cur and cur["content_hash"] == h:
            raise ServiceError("新版本与当前版本内容完全相同，未产生差异", 409)

    next_no = (recipe["revision"] or 0) + 1
    cur = conn.execute(
        "SELECT COALESCE(MAX(version_no),0)+1 AS n FROM recipe_versions WHERE recipe_id=?",
        (recipe_id,),
    ).fetchone()
    version_no = cur["n"]

    cur2 = conn.execute(
        """INSERT INTO recipe_versions
           (recipe_id, version_no, created_on, created_by, content_hash, status, note)
           VALUES (?,?,?,?,?, 'in_approval', ?)""",
        (recipe_id, version_no, now_iso(), created_by, h, note),
    )
    vid = cur2.lastrowid
    for pos, c in enumerate(canonical):
        if c["kind"] == "material":
            conn.execute(
                """INSERT INTO components
                   (recipe_version_id, position, kind, material_id, batch_id, qty, material_labels)
                   VALUES (?,?, 'material', ?,?,?,?)""",
                (vid, pos, c["material_id"], c["batch_id"], c["qty"],
                 json.dumps(c["labels"], ensure_ascii=False)),
            )
        else:
            conn.execute(
                """INSERT INTO components
                   (recipe_version_id, position, kind, child_version_id, qty)
                   VALUES (?,?, 'subrecipe', ?,?)""",
                (vid, pos, c["child_version_id"], c["qty"]),
            )

    # 结构上子配方只能引用已存在的版本（即更早版本），新插入节点不可能被旧节点引用；
    # 仍做一次防御性循环检测。
    if resolve_graph(conn, vid)["cycles"]:
        raise ServiceError("该组件结构构成循环引用，已阻止发布", 422)

    # 旧当前版本上的"半成品"审批（只点过一次）随新版本失效；已放行记录绝不动。
    if recipe["current_version_id"] is not None:
        conn.execute(
            """UPDATE approvals SET stale=1,
                   stale_reason='配方已产生更新的不可变版本，针对旧快照的审批失效'
             WHERE recipe_version_id=? AND stale=0
               AND (SELECT status FROM recipe_versions WHERE id=?)='in_approval'""",
            (recipe["current_version_id"], recipe["current_version_id"]),
        )

    conn.execute(
        "UPDATE recipes SET current_version_id=?, revision=? WHERE id=?",
        (vid, next_no, recipe_id),
    )
    # 针对同一基础版本尚未处理的其他替换提案并发失效
    conn.execute(
        "UPDATE proposals SET status='superseded' WHERE recipe_id=? AND status='open'",
        (recipe_id,),
    )
    return vid


# ---------------------------------------------------------------------------
# 双人审批
# ---------------------------------------------------------------------------
def approve(conn: sqlite3.Connection, version_id: int, reviewer_id: int) -> dict[str, Any]:
    v = conn.execute("SELECT * FROM recipe_versions WHERE id=?", (version_id,)).fetchone()
    if not v:
        raise ServiceError(f"配方版本 #{version_id} 不存在", 404)
    recipe = conn.execute("SELECT * FROM recipes WHERE id=?", (v["recipe_id"],)).fetchone()
    reviewer = conn.execute("SELECT * FROM reviewers WHERE id=?", (reviewer_id,)).fetchone()
    if not reviewer:
        raise ServiceError(f"审核者 #{reviewer_id} 不存在", 404)

    if recipe["current_version_id"] != version_id:
        raise ServiceError("该快照已不是配方当前版本：配方发生过变化，审批失效，请对最新版本重新确认", 409)
    if v["status"] == "released":
        raise ServiceError("该不可变快照已完成放行，无需重复确认", 409)

    existing = conn.execute(
        "SELECT id FROM approvals WHERE recipe_version_id=? AND reviewer_id=?",
        (version_id, reviewer_id),
    ).fetchone()
    if existing:
        raise ServiceError("同一审核者不能对同一快照确认两次（双人放行要求两名不同审核者）", 409)

    problems = assess_version(conn, version_id)
    if problems:
        # 第二次确认前批次被召回 / 已过期：第一次确认随之失效，必须出新版本重新放行
        conn.execute(
            "UPDATE approvals SET stale=1, stale_reason=? WHERE recipe_version_id=? AND stale=0",
            ("第二次确认前活体检发发现问题（批次召回/失效等），审批失效", version_id),
        )
        raise ServiceError("活体检发未通过，本次确认被阻止；既有确认已失效：" + "；".join(problems), 422)

    cur = conn.execute(
        "INSERT INTO approvals (recipe_version_id, reviewer_id, decided_on) VALUES (?,?,?)",
        (version_id, reviewer_id, now_iso()),
    )
    # 同一快照的有效确认（stale 行只可能来自此前被拦下的流程，此时会被上面的 problems 挡住）
    count = conn.execute(
        "SELECT COUNT(*) AS n FROM approvals WHERE recipe_version_id=? AND stale=0",
        (version_id,),
    ).fetchone()["n"]
    released = False
    if count >= 2:
        conn.execute(
            "UPDATE recipe_versions SET status='released', released_on=? WHERE id=? AND status='in_approval'",
            (now_iso(), version_id),
        )
        released = conn.execute("SELECT status FROM recipe_versions WHERE id=?", (version_id,)).fetchone()["status"] == "released"
    return {"approval_id": cur.lastrowid, "approval_count": count, "released": released}


# ---------------------------------------------------------------------------
# 批次召回（不改写历史，只标待复核 + 传播）
# ---------------------------------------------------------------------------
def versions_referencing_batch(conn: sqlite3.Connection, batch_id: int) -> list[int]:
    """所有（含多级子配方传递）钉住该批次的配方版本。"""
    direct = {r["recipe_version_id"] for r in conn.execute(
        "SELECT DISTINCT recipe_version_id FROM components WHERE batch_id=?", (batch_id,)
    )}
    # 反向邻接：child_version_id -> 引用它的父版本
    parents: dict[int, set[int]] = {}
    for r in conn.execute("SELECT recipe_version_id, child_version_id FROM components WHERE kind='subrecipe'"):
        parents.setdefault(r["child_version_id"], set()).add(r["recipe_version_id"])
    affected, frontier = set(direct), list(direct)
    while frontier:
        node = frontier.pop()
        for p in parents.get(node, ()):
            if p not in affected:
                affected.add(p)
                frontier.append(p)
    return sorted(affected)


def recall_batch(conn: sqlite3.Connection, batch_id: int, reason: str) -> dict[str, Any]:
    b = conn.execute("SELECT * FROM batches WHERE id=?", (batch_id,)).fetchone()
    if not b:
        raise ServiceError(f"批次 #{batch_id} 不存在", 404)
    if b["status"] == "recalled":
        raise ServiceError(f"批次「{b['code']}」已处于召回状态", 409)
    conn.execute("UPDATE batches SET status='recalled' WHERE id=?", (batch_id,))
    conn.execute(
        "INSERT INTO batch_recalls (batch_id, reason, recalled_on) VALUES (?,?,?)",
        (batch_id, reason, now_iso()),
    )
    affected = versions_referencing_batch(conn, batch_id)
    for vid in affected:
        # 注意：只打"待复核"标，不改 status/released_on/approvals —— 既有放行记录原样保留
        conn.execute("UPDATE recipe_versions SET needs_review=1 WHERE id=?", (vid,))
        conn.execute(
            "UPDATE approvals SET stale=1, stale_reason=? WHERE recipe_version_id=? AND stale=0",
            (f"供应批次「{b['code']}」被召回，针对该快照的未完成审批失效", vid),
        )
    return {"batch_id": batch_id, "batch_code": b["code"], "affected_versions": affected}


# ---------------------------------------------------------------------------
# 替换提案
# ---------------------------------------------------------------------------
def create_proposal(
    conn: sqlite3.Connection,
    recipe_id: int,
    base_version_id: int,
    component_id: int,
    new_material_id: int,
    new_batch_id: int,
    proposed_by: int,
    reason: str = "",
) -> int:
    recipe = conn.execute("SELECT * FROM recipes WHERE id=?", (recipe_id,)).fetchone()
    if not recipe:
        raise ServiceError(f"配方 #{recipe_id} 不存在", 404)
    if recipe["current_version_id"] != base_version_id:
        raise ServiceError("基础版本不是当前版本，请刷新后基于最新版本提出替代", 409)
    comp = conn.execute("SELECT * FROM components WHERE id=? AND kind='material'", (component_id,)).fetchone()
    if not comp or comp["recipe_version_id"] != base_version_id:
        raise ServiceError("目标组件不存在或不属于该基础版本", 404)
    m = conn.execute("SELECT * FROM materials WHERE id=?", (new_material_id,)).fetchone()
    if not m:
        raise ServiceError(f"替代原料 #{new_material_id} 不存在", 404)
    b = conn.execute("SELECT * FROM batches WHERE id=?", (new_batch_id,)).fetchone()
    if not b:
        raise ServiceError(f"替代批次 #{new_batch_id} 不存在", 404)
    if b["material_id"] != new_material_id:
        raise ServiceError("替代批次不属于所选择的替代原料", 422)
    eff = effective_batch_status(b)
    if eff != "active":
        raise ServiceError(f"替代批次「{b['code']}」状态为 {eff}，不能用于替代", 422)
    if not conn.execute("SELECT 1 FROM reviewers WHERE id=?", (proposed_by,)).fetchone():
        raise ServiceError(f"审核者 #{proposed_by} 不存在", 404)

    cur = conn.execute(
        """INSERT INTO proposals
           (recipe_id, base_version_id, component_id, new_material_id, new_batch_id,
            proposed_by, reason, created_on, status)
           VALUES (?,?,?,?,?,?,?,?,'open')""",
        (recipe_id, base_version_id, component_id, new_material_id, new_batch_id,
         proposed_by, reason, now_iso()),
    )
    return cur.lastrowid


def apply_proposal(conn: sqlite3.Connection, proposal_id: int) -> int:
    """采纳提案：以基础版本为蓝本生成一个新不可变版本（原快照保持不变）。"""
    p = conn.execute("SELECT * FROM proposals WHERE id=?", (proposal_id,)).fetchone()
    if not p:
        raise ServiceError(f"提案 #{proposal_id} 不存在", 404)
    if p["status"] != "open":
        raise ServiceError(f"提案已处于 {p['status']} 状态，不能采纳", 409)
    recipe = conn.execute("SELECT * FROM recipes WHERE id=?", (p["recipe_id"],)).fetchone()
    if recipe["current_version_id"] != p["base_version_id"]:
        conn.execute("UPDATE proposals SET status='superseded' WHERE id=?", (proposal_id,))
        raise ServiceError("基础版本已变化（并发替换），该提案已失效", 409)

    comps = conn.execute(
        "SELECT * FROM components WHERE recipe_version_id=? ORDER BY position",
        (p["base_version_id"],),
    ).fetchall()
    target = conn.execute("SELECT * FROM components WHERE id=?", (p["component_id"],)).fetchone()
    payload: list[dict[str, Any]] = []
    found = False
    for c in comps:
        if c["id"] == target["id"]:
            found = True
            payload.append({
                "kind": "material",
                "material_id": p["new_material_id"],
                "batch_id": p["new_batch_id"],
                "qty": c["qty"],
            })
        elif c["kind"] == "material":
            payload.append({"kind": "material", "material_id": c["material_id"],
                            "batch_id": c["batch_id"], "qty": c["qty"]})
        else:
            payload.append({"kind": "subrecipe", "child_version_id": c["child_version_id"],
                            "qty": c["qty"]})
    if not found:
        raise ServiceError("目标组件在基础版本中已不存在", 409)

    proposer = conn.execute("SELECT name FROM reviewers WHERE id=?", (p["proposed_by"],)).fetchone()["name"]
    m = conn.execute("SELECT name FROM materials WHERE id=?", (p["new_material_id"],)).fetchone()
    note = f"采纳替代提案 #{proposal_id}：替换为「{m['name']}」。{p['reason']}".strip()
    new_vid = create_version(
        conn,
        recipe_id=p["recipe_id"],
        created_by=proposer,
        components=payload,
        note=note,
        expected_current_version_id=p["base_version_id"],
    )
    conn.execute(
        "UPDATE proposals SET status='applied', resulting_version_id=? WHERE id=?",
        (new_vid, proposal_id),
    )
    return new_vid


def reject_proposal(conn: sqlite3.Connection, proposal_id: int) -> None:
    p = conn.execute("SELECT * FROM proposals WHERE id=?", (proposal_id,)).fetchone()
    if not p:
        raise ServiceError(f"提案 #{proposal_id} 不存在", 404)
    if p["status"] != "open":
        raise ServiceError(f"提案已处于 {p['status']} 状态", 409)
    conn.execute("UPDATE proposals SET status='rejected' WHERE id=?", (proposal_id,))


# ---------------------------------------------------------------------------
# 版本差异
# ---------------------------------------------------------------------------
def diff_versions(conn: sqlite3.Connection, base_id: int, target_id: int) -> dict[str, Any]:
    def comp_map(vid: int):
        rows = conn.execute(
            """SELECT c.*, m.code AS m_code, m.name AS m_name, b.code AS b_code,
                      r.code AS r_code, r.name AS r_name, cv.version_no AS cv_no
                 FROM components c
                 LEFT JOIN materials m ON m.id=c.material_id
                 LEFT JOIN batches b ON b.id=c.batch_id
                 LEFT JOIN recipe_versions cv ON cv.id=c.child_version_id
                 LEFT JOIN recipes r ON r.id=cv.recipe_id
                WHERE c.recipe_version_id=? ORDER BY c.position""",
            (vid,),
        ).fetchall()
        out = {}
        order = []
        for c in rows:
            if c["kind"] == "material":
                key = c["position"]
                desc = c["m_name"]
                detail = f"批次 {c['b_code'] or '未绑定'}；{c['qty']}"
                labels = json.loads(c["material_labels"] or "[]")
            else:
                key = c["position"]
                desc = f"{c['r_name']} v{c['cv_no']}"
                detail = c["qty"]
                labels = []
            out[key] = {"kind": c["kind"], "name": desc, "detail": detail, "labels": labels}
            order.append(key)
        return out, order

    base, b_order = comp_map(base_id)
    tgt, t_order = comp_map(target_id)
    entries: list[dict[str, Any]] = []
    for key in t_order:
        if key not in base:
            t = tgt[key]
            entries.append({"change": "added", "kind": t["kind"], "name": t["name"],
                            "after": t["detail"], "labels_after": t["labels"]})
        elif base[key]["detail"] != tgt[key]["detail"] or base[key]["labels"] != tgt[key]["labels"]:
            entries.append({"change": "modified", "kind": tgt[key]["kind"], "name": tgt[key]["name"],
                            "before": base[key]["detail"], "after": tgt[key]["detail"],
                            "labels_before": base[key]["labels"], "labels_after": tgt[key]["labels"]})
    for key in b_order:
        if key not in tgt:
            x = base[key]
            entries.append({"change": "removed", "kind": x["kind"], "name": x["name"],
                            "before": x["detail"], "labels_before": x["labels"]})

    g_base = {x["code"] for x in resolve_graph(conn, base_id)["labels"]}
    g_tgt = {x["code"] for x in resolve_graph(conn, target_id)["labels"]}
    return {
        "base_version_id": base_id,
        "target_version_id": target_id,
        "same_version": base_id == target_id,
        "entries": entries,
        "labels_added": sorted(g_tgt - g_base),
        "labels_removed": sorted(g_base - g_tgt),
    }
