"""FastAPI 入口：过敏原标签复核台。

所有写操作在 BEGIN IMMEDIATE 事务内执行（见 db.transaction / service 层），
因此并发的双人确认、并发替换在 SQLite 写锁下被串行化裁决。
"""
from __future__ import annotations

import csv
import io
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException
from fastapi.responses import JSONResponse, Response
from fastapi.staticfiles import StaticFiles

from . import config, schemas, serializers, service
from .db import get_conn as _get_conn, init_db, transaction

app = FastAPI(title="过敏原标签复核台", version="1.0")


def get_conn():
    """FastAPI 依赖：把 contextmanager 包成生成器依赖。"""
    with _get_conn() as conn:
        yield conn


def svc_error_handler(exc: service.ServiceError):
    raise HTTPException(status_code=exc.status, detail=str(exc))


# ---------------- 基础资料 ----------------
@app.get("/api/meta")
def meta(conn=Depends(get_conn)):
    allergens = [dict(r) for r in conn.execute("SELECT code,name FROM allergens ORDER BY code")]
    reviewers = [dict(r) for r in conn.execute("SELECT id,name FROM reviewers ORDER BY id")]
    return {"allergens": allergens, "reviewers": reviewers, "today": config.today().isoformat()}


@app.get("/api/materials")
def list_materials(conn=Depends(get_conn)):
    out = []
    for m in conn.execute("SELECT * FROM materials ORDER BY code"):
        codes = [r["allergen_code"] for r in conn.execute(
            "SELECT allergen_code FROM material_allergens WHERE material_id=? ORDER BY allergen_code",
            (m["id"],))]
        out.append({"id": m["id"], "code": m["code"], "name": m["name"], "allergen_codes": codes})
    return out


@app.get("/api/batches")
def list_batches(conn=Depends(get_conn)):
    return [serializers._batch_eff(conn, r["id"]) for r in conn.execute(
        "SELECT id FROM batches ORDER BY id")]


# ---------------- 配方 ----------------
@app.get("/api/recipes")
def list_recipes(conn=Depends(get_conn)):
    return [serializers.serialize_recipe(conn, r)
            for r in conn.execute("SELECT * FROM recipes ORDER BY code")]


@app.get("/api/recipes/{recipe_id}")
def get_recipe(recipe_id: int, conn=Depends(get_conn)):
    row = conn.execute("SELECT * FROM recipes WHERE id=?", (recipe_id,)).fetchone()
    if not row:
        raise HTTPException(404, "配方不存在")
    summary = serializers.serialize_recipe(conn, row)
    versions = [
        serializers.serialize_version(conn, r["id"])
        for r in conn.execute(
            "SELECT id FROM recipe_versions WHERE recipe_id=? ORDER BY version_no", (recipe_id,))
    ]
    proposals = [
        serializers.serialize_proposal(conn, p)
        for p in conn.execute(
            "SELECT * FROM proposals WHERE recipe_id=? ORDER BY id DESC", (recipe_id,))
    ]
    return {**summary, "versions": versions, "proposals": proposals}


@app.post("/api/versions", response_model=schemas.VersionOut)
def post_version(body: schemas.VersionCreate, conn=Depends(get_conn)):
    payload = [c.model_dump() for c in body.components]
    try:
        with transaction(conn):
            vid = service.create_version(
                conn,
                recipe_id=body.recipe_id,
                created_by=body.created_by,
                components=payload,
                note=body.note,
                expected_current_version_id=body.expected_current_version_id,
            )
    except service.ServiceError as exc:
        svc_error_handler(exc)
    return serializers.serialize_version(conn, vid)


@app.get("/api/versions/{version_id}", response_model=schemas.VersionOut)
def get_version(version_id: int, conn=Depends(get_conn)):
    if not conn.execute("SELECT 1 FROM recipe_versions WHERE id=?", (version_id,)).fetchone():
        raise HTTPException(404, "配方版本不存在")
    return serializers.serialize_version(conn, version_id)


# ---------------- 同一份服务端版本视图：传播图 / 差异 / 导出 ----------------
@app.get("/api/versions/{version_id}/graph")
def version_graph(version_id: int, conn=Depends(get_conn)):
    if not conn.execute("SELECT 1 FROM recipe_versions WHERE id=?", (version_id,)).fetchone():
        raise HTTPException(404, "配方版本不存在")
    return serializers.serialize_graph(conn, version_id)


@app.get("/api/diff/{base_id}/{target_id}")
def version_diff(base_id: int, target_id: int, conn=Depends(get_conn)):
    for vid in (base_id, target_id):
        if not conn.execute("SELECT 1 FROM recipe_versions WHERE id=?", (vid,)).fetchone():
            raise HTTPException(404, f"配方版本 #{vid} 不存在")
    return service.diff_versions(conn, base_id, target_id)


@app.get("/api/versions/{version_id}/export")
def export_version(version_id: int, conn=Depends(get_conn), format: str = "json"):
    """页面标签清单与导出文件来自同一服务端版本视图。"""
    if not conn.execute("SELECT 1 FROM recipe_versions WHERE id=?", (version_id,)).fetchone():
        raise HTTPException(404, "配方版本不存在")
    version = serializers.serialize_version(conn, version_id)
    graph = serializers.serialize_graph(conn, version_id)
    bundle = {
        "exported_from_version_id": version_id,
        "content_hash": version["content_hash"],
        "version": version,
        "graph": graph,
    }
    if format == "json":
        return JSONResponse(
            bundle,
            headers={"Content-Disposition": f'attachment; filename="version-{version_id}.json"'},
        )
    if format == "csv":
        buf = io.StringIO()
        w = csv.writer(buf)
        w.writerow(["版本", "内容指纹", "过敏原", "过敏原名称", "传播路径"])
        for lab in graph["labels"]:
            for path in lab["paths"]:
                w.writerow([
                    f"v{version['version_no']}", version["content_hash"], lab["code"], lab["name"],
                    " -> ".join(f"{h['name']}({h['batch_code'] or '-'})" for h in path),
                ])
        return Response(
            buf.getvalue(), media_type="text/csv; charset=utf-8",
            headers={"Content-Disposition": f'attachment; filename="labels-{version_id}.csv"'},
        )
    raise HTTPException(400, "format 仅支持 json / csv")


# ---------------- 审批 ----------------
@app.post("/api/versions/{version_id}/approve")
def post_approve(version_id: int, body: schemas.ApprovalIn, conn=Depends(get_conn)):
    try:
        with transaction(conn):
            result = service.approve(conn, version_id, body.reviewer_id)
    except service.ServiceError as exc:
        svc_error_handler(exc)
    return {**result, "version": serializers.serialize_version(conn, version_id)}


# ---------------- 批次召回 ----------------
@app.post("/api/batches/{batch_id}/recall")
def post_recall(batch_id: int, body: schemas.RecallCreate, conn=Depends(get_conn)):
    try:
        with transaction(conn):
            result = service.recall_batch(conn, batch_id, body.reason)
    except service.ServiceError as exc:
        svc_error_handler(exc)
    out = []
    for vid in result["affected_versions"]:
        g = serializers.serialize_graph(conn, vid)
        hit = next((b for b in g["batches"] if b["batch_id"] == batch_id), None)
        out.append({"version_id": vid, "paths": hit["paths"] if hit else []})
    return {
        "batch_id": result["batch_id"],
        "batch_code": result["batch_code"],
        "reason": body.reason,
        "recalled_on": service.now_iso(),
        "affected_versions": result["affected_versions"],
        "propagation": out,
    }


# ---------------- 替代原料提案 ----------------
@app.post("/api/proposals", response_model=schemas.ProposalOut)
def post_proposal(body: schemas.ProposalCreate, conn=Depends(get_conn)):
    try:
        with transaction(conn):
            pid = service.create_proposal(
                conn,
                recipe_id=body.recipe_id,
                base_version_id=body.base_version_id,
                component_id=body.component_id,
                new_material_id=body.new_material_id,
                new_batch_id=body.new_batch_id,
                proposed_by=body.proposed_by,
                reason=body.reason,
            )
    except service.ServiceError as exc:
        svc_error_handler(exc)
    return serializers.serialize_proposal(
        conn, conn.execute("SELECT * FROM proposals WHERE id=?", (pid,)).fetchone())


@app.post("/api/proposals/{proposal_id}/apply")
def apply_proposal(proposal_id: int, conn=Depends(get_conn)):
    try:
        with transaction(conn):
            new_vid = service.apply_proposal(conn, proposal_id)
    except service.ServiceError as exc:
        svc_error_handler(exc)
    return {"resulting_version_id": new_vid,
            "version": serializers.serialize_version(conn, new_vid)}


@app.post("/api/proposals/{proposal_id}/reject")
def reject_proposal(proposal_id: int, conn=Depends(get_conn)):
    try:
        with transaction(conn):
            service.reject_proposal(conn, proposal_id)
    except service.ServiceError as exc:
        svc_error_handler(exc)
    return {"ok": True}


init_db()

# 生产环境：挂载 React 构建产物
DIST = Path(__file__).resolve().parent.parent / "frontend" / "dist"
if DIST.exists():
    app.mount("/", StaticFiles(directory=DIST, html=True), name="static")

    @app.exception_handler(404)
    async def spa_fallback(request, exc):  # noqa: ANN001
        if request.url.path.startswith("/api/"):
            return JSONResponse({"detail": "资源不存在"}, status_code=404)
        return JSONResponse((DIST / "index.html").read_text(encoding="utf-8"),
                            status_code=200, media_type="text/html")
