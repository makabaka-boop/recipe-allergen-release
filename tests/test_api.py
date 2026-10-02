"""HTTP 层：页面差异/标签清单/导出同源、乐观锁、审批与召回接口。"""
import csv
import io

from tests.conftest import ids_by_code


def _sign_v1(client):
    recipes = {r["code"]: r for r in client.get("/api/recipes").json()}
    rid = recipes["RC-SIGN"]["id"]
    detail = client.get(f"/api/recipes/{rid}").json()
    v1 = next(v for v in detail["versions"] if v["version_no"] == 1)
    return rid, v1["id"]


def test_graph_diff_export_share_same_server_version(client, fresh_db):
    _, vid = _sign_v1(client)
    g = client.get(f"/api/versions/{vid}/graph").json()
    v = client.get(f"/api/versions/{vid}").json()
    export = client.get(f"/api/versions/{vid}/export?format=json").json()

    assert g["version_id"] == vid
    assert g["content_hash"] == v["content_hash"] == export["content_hash"]
    # 导出里的标签清单与 graph 完全同源
    assert [l["code"] for l in export["graph"]["labels"]] == [l["code"] for l in g["labels"]]
    # 页面清单：MILK 必须经过两级子配方到达
    milk = next(l for l in g["labels"] if l["code"] == "MILK")
    kinds = [h["kind"] for h in milk["paths"][0]]
    assert kinds == ["subrecipe", "subrecipe", "material"]


def test_csv_export_rows_match_graph_paths(client, fresh_db):
    _, vid = _sign_v1(client)
    g = client.get(f"/api/versions/{vid}/graph").json()
    raw = client.get(f"/api/versions/{vid}/export?format=csv").content.decode("utf-8-sig")
    rows = list(csv.reader(io.StringIO(raw)))
    header, data = rows[0], rows[1:]
    assert header == ["版本", "内容指纹", "过敏原", "过敏原名称", "传播路径"]
    expected = sum(len(l["paths"]) for l in g["labels"])
    assert len(data) == expected
    milk_rows = [r for r in data if r[2] == "MILK"]
    assert milk_rows and "卡仕达酱" in milk_rows[0][4]


def test_optimistic_lock_blocks_stale_edit(client, fresh_db):
    conn = fresh_db
    recipes = {r["code"]: r for r in client.get("/api/recipes").json()}
    rid = recipes["RC-SIGN"]["id"]
    detail = client.get(f"/api/recipes/{rid}").json()
    cur = next(v for v in detail["versions"] if v["is_current"])  # 种子当前版为 v2
    v_cur = cur["id"]
    ids = ids_by_code(conn, "SOYLEC")
    soy_b = conn.execute("SELECT id FROM batches WHERE code='B-SOY-2607'").fetchone()["id"]
    cake_v1 = conn.execute(
        """SELECT v.id FROM recipe_versions v JOIN recipes r ON r.id=v.recipe_id
            WHERE r.code='RC-CAKE' AND v.version_no=1"""
    ).fetchone()["id"]

    payload = {
        "recipe_id": rid, "created_by": "起草人-赵配方", "note": "并发测试A",
        "expected_current_version_id": v_cur,
        "components": [
            {"kind": "subrecipe", "child_version_id": cake_v1, "qty": "1"},
            {"kind": "material", "material_id": ids["SOYLEC"], "batch_id": soy_b, "qty": "6g"},
        ],
    }
    r1 = client.post("/api/versions", json=payload)
    assert r1.status_code == 200
    # 同一调用方拿着旧的 expected 再提一次 -> 409
    r2 = client.post("/api/versions", json=payload)
    assert r2.status_code == 409
    assert "最新版本" in r2.json()["detail"]


def test_approve_api_flow_and_duplicate_reviewer(client, fresh_db):
    conn = fresh_db
    recipes = {r["code"]: r for r in client.get("/api/recipes").json()}
    rid = recipes["RC-SIGN"]["id"]
    detail = client.get(f"/api/recipes/{rid}").json()
    cur = next(v for v in detail["versions"] if v["is_current"])
    reviewers = {r["name"]: r["id"] for r in client.get("/api/meta").json()["reviewers"]}

    # A 已确认（种子），重复 A -> 409
    dup = client.post(f"/api/versions/{cur['id']}/approve", json={"reviewer_id": reviewers["审核者A-王敏"]})
    assert dup.status_code == 409
    # B 第二人确认 -> 放行
    ok = client.post(f"/api/versions/{cur['id']}/approve", json={"reviewer_id": reviewers["审核者B-李强"]})
    assert ok.status_code == 200 and ok.json()["released"] is True
    assert ok.json()["version"]["status"] == "released"


def test_recall_api_returns_propagation_and_flags(client, fresh_db):
    _, vid = _sign_v1(client)
    milk_batch = fresh_db.execute("SELECT id FROM batches WHERE code='B-MILK-2609'").fetchone()["id"]
    resp = client.post(f"/api/batches/{milk_batch}/recall", json={"reason": "API 召回演练"})
    assert resp.status_code == 200
    body = resp.json()
    assert vid in body["affected_versions"]
    hit = next(p for p in body["propagation"] if p["version_id"] == vid)
    # 传播路径：牛奶经由 招牌->蛋糕胚->卡仕达 到达
    assert hit["paths"], "召回必须展示引用传播路径"
    leaf = hit["paths"][0][-1]
    assert leaf["batch_code"] == "B-MILK-2609"

    g = client.get(f"/api/versions/{vid}/graph").json()
    assert any("召回" in x for x in g["violations"])
    detail = client.get(f"/api/versions/{vid}").json()
    assert detail["status"] == "released" and detail["needs_review"] is True  # 放行记录原样保留


def test_proposal_api_end_to_end(client, fresh_db):
    recipes = {r["code"]: r for r in client.get("/api/recipes").json()}
    rid = recipes["RC-CUSTARD"]["id"]
    detail = client.get(f"/api/recipes/{rid}").json()
    cur = next(v for v in detail["versions"] if v["is_current"])
    milk_comp = next(c for c in cur["components"] if c["material_code"] == "MILK")
    oat = next(m for m in client.get("/api/materials").json() if m["code"] == "OATMILK")
    oat_batch = next(b for b in client.get("/api/batches").json()
                     if b["material_id"] == oat["id"] and b["status"] == "active")
    reviewer = next(r for r in client.get("/api/meta").json()["reviewers"] if r["name"] == "审核者C-周婷")

    resp = client.post("/api/proposals", json={
        "recipe_id": rid, "base_version_id": cur["id"], "component_id": milk_comp["id"],
        "new_material_id": oat["id"], "new_batch_id": oat_batch["id"],
        "proposed_by": reviewer["id"], "reason": "页面替代"})
    assert resp.status_code == 200
    pid = resp.json()["id"]

    applied = client.post(f"/api/proposals/{pid}/apply")
    assert applied.status_code == 200
    new_vid = applied.json()["resulting_version_id"]
    g = client.get(f"/api/versions/{new_vid}/graph").json()
    assert "MILK" not in [l["code"] for l in g["labels"]]
