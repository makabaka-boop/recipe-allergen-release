import threading

import pytest
from fastapi.testclient import TestClient

from app import create_app


@pytest.fixture()
def client():
    app = create_app(":memory:")
    with TestClient(app) as c:
        yield c


# ---------- 建数辅助 ----------

def mk_ingredient(c, name, allergens):
    r = c.post("/api/ingredients", json={"name": name, "allergens": allergens})
    assert r.status_code == 200, r.text
    return r.json()["id"]


def mk_batch(c, ingredient_id, code):
    r = c.post("/api/batches", json={"ingredient_id": ingredient_id, "code": code})
    assert r.status_code == 200, r.text
    return r.json()["id"]


def mk_recipe(c, name):
    r = c.post("/api/recipes", json={"name": name})
    assert r.status_code == 200, r.text
    return r.json()["id"]


def mk_version(c, recipe_id):
    r = c.post(f"/api/recipes/{recipe_id}/versions")
    assert r.status_code == 200, r.text
    return r.json()["id"]


def add_ing(c, vid, ing, batch=None, qty=1):
    r = c.post(f"/api/versions/{vid}/items",
               json={"kind": "ingredient", "ingredient_id": ing, "batch_id": batch, "qty": qty})
    assert r.status_code == 200, r.text
    return r.json()["id"]


def add_sub(c, vid, sub_vid, qty=1):
    r = c.post(f"/api/versions/{vid}/items",
               json={"kind": "recipe", "sub_version_id": sub_vid, "qty": qty})
    assert r.status_code == 200, r.text
    return r.json()["id"]


def submit(c, vid):
    assert c.post(f"/api/versions/{vid}/submit").status_code == 200


def confirm(c, vid, reviewer):
    return c.post(f"/api/versions/{vid}/confirm", json={"reviewer": reviewer})


def release(c, vid, by="manager"):
    return c.post(f"/api/versions/{vid}/release", json={"released_by": by})


def full_release(c, vid):
    submit(c, vid)
    assert confirm(c, vid, "alice").status_code == 200
    assert confirm(c, vid, "bob").status_code == 200
    r = release(c, vid)
    assert r.status_code == 200, r.text
    return r.json()


# ---------- 多级引用与标签传播 ----------

def test_multilevel_label_propagation_paths(client):
    peanut = mk_ingredient(client, "花生粉", ["花生"])
    milk = mk_ingredient(client, "奶粉", ["乳"])
    b1 = mk_batch(client, peanut, "P-001")

    r1 = mk_recipe(client, "底料")
    v1 = mk_version(client, r1)
    add_ing(client, v1, peanut, b1)
    full_release(client, v1)

    r2 = mk_recipe(client, "酱体")
    v2 = mk_version(client, r2)
    add_ing(client, v2, milk)
    add_sub(client, v2, v1)
    full_release(client, v2)

    r3 = mk_recipe(client, "成品")
    v3 = mk_version(client, r3)
    add_sub(client, v3, v2)
    add_sub(client, v3, v1)

    data = client.get(f"/api/versions/{v3}/labels").json()
    labels = data["labels"]
    assert set(labels) == {"花生", "乳"}
    # 花生经两条路径传播: 成品→酱体→底料, 成品→底料
    assert len(labels["花生"]) == 2
    depths = sorted(len(p) for p in labels["花生"])
    assert depths == [2, 3]
    # 路径包含配方名与条目描述
    deep = [p for p in labels["花生"] if len(p) == 3][0]
    assert deep[0]["recipe"].startswith("成品")
    assert "酱体" in deep[1]["recipe"]
    assert "花生粉" in deep[2]["desc"] and "P-001" in deep[2]["desc"]
    assert len(labels["乳"]) == 1


def test_cycle_reference_rejected(client):
    ing = mk_ingredient(client, "糖", [])
    r1 = mk_recipe(client, "A")
    v1 = mk_version(client, r1)
    add_ing(client, v1, ing)
    r2 = mk_recipe(client, "B")
    v2 = mk_version(client, r2)
    add_sub(client, v2, v1)  # B 引用更早的 A: 允许
    # A 引用更晚创建的 B: 拒绝(否则成环)
    r = client.post(f"/api/versions/{v1}/items",
                    json={"kind": "recipe", "sub_version_id": v2, "qty": 1})
    assert r.status_code == 400
    assert "更早" in r.json()["detail"]
    # 自引用: 拒绝
    r = client.post(f"/api/versions/{v2}/items",
                    json={"kind": "recipe", "sub_version_id": v2, "qty": 1})
    assert r.status_code == 400


# ---------- 双人审批与快照失效 ----------

def test_release_requires_two_distinct_reviewers(client):
    ing = mk_ingredient(client, "芝麻", ["芝麻"])
    rid = mk_recipe(client, "R")
    vid = mk_version(client, rid)
    add_ing(client, vid, ing)
    submit(client, vid)
    assert release(client, vid).status_code == 400  # 无人确认
    assert confirm(client, vid, "alice").status_code == 200
    assert confirm(client, vid, "alice").status_code == 409  # 同人重复
    assert release(client, vid).status_code == 400  # 仍只有一人
    assert confirm(client, vid, "bob").status_code == 200
    r = release(client, vid)
    assert r.status_code == 200
    assert r.json()["status"] == "released"
    assert r.json()["release"]["snapshot_hash"] == r.json()["snapshot_hash"]


def test_recipe_change_between_confirmations_invalidates_approval(client):
    old = mk_ingredient(client, "花生油", ["花生"])
    new = mk_ingredient(client, "葵花油", [])
    rid = mk_recipe(client, "R")
    vid = mk_version(client, rid)
    item = add_ing(client, vid, old)
    submit(client, vid)
    assert confirm(client, vid, "alice").status_code == 200
    # 第二次确认前配方被替换 → alice 的确认失效
    s = client.post(f"/api/versions/{vid}/substitutions",
                    json={"item_id": item, "new_ingredient_id": new, "proposer": "carol"}).json()
    h = client.get(f"/api/versions/{vid}").json()["snapshot_hash"]
    r = client.post(f"/api/substitutions/{s['id']}/apply", json={"expected_snapshot_hash": h})
    assert r.status_code == 200
    detail = client.get(f"/api/versions/{vid}").json()
    assert detail["confirmations"][0]["status"] == "invalidated"
    assert confirm(client, vid, "bob").status_code == 200
    assert release(client, vid).status_code == 400  # 只剩 bob 一人有效
    assert confirm(client, vid, "alice").status_code == 200  # 重新确认
    assert release(client, vid).status_code == 200


def test_concurrent_substitution_conflict(client):
    a = mk_ingredient(client, "A油", [])
    b = mk_ingredient(client, "B油", [])
    c_ = mk_ingredient(client, "C油", [])
    rid = mk_recipe(client, "R")
    vid = mk_version(client, rid)
    item = add_ing(client, vid, a)
    submit(client, vid)
    s1 = client.post(f"/api/versions/{vid}/substitutions",
                     json={"item_id": item, "new_ingredient_id": b, "proposer": "u1"}).json()
    s2 = client.post(f"/api/versions/{vid}/substitutions",
                     json={"item_id": item, "new_ingredient_id": c_, "proposer": "u2"}).json()
    h = client.get(f"/api/versions/{vid}").json()["snapshot_hash"]
    # 两个审核者基于同一快照并发应用替换: 只有一个成功
    r1 = client.post(f"/api/substitutions/{s1['id']}/apply", json={"expected_snapshot_hash": h})
    r2 = client.post(f"/api/substitutions/{s2['id']}/apply", json={"expected_snapshot_hash": h})
    assert r1.status_code == 200
    assert r2.status_code == 409  # 快照已变 → 冲突
    assert "冲突" in r2.json()["detail"]
    detail = client.get(f"/api/versions/{vid}").json()
    assert detail["items"][0]["ingredient_id"] == b
    st = {s["id"]: s["status"] for s in detail["substitutions"]}
    assert st[s1["id"]] == "applied" and st[s2["id"]] == "superseded"


def test_release_race_only_one_wins(client):
    ing = mk_ingredient(client, "盐", [])
    rid = mk_recipe(client, "R")
    vid = mk_version(client, rid)
    add_ing(client, vid, ing)
    submit(client, vid)
    confirm(client, vid, "alice")
    confirm(client, vid, "bob")
    results = []

    def do_release():
        results.append(release(client, vid, by="racer").status_code)

    threads = [threading.Thread(target=do_release) for _ in range(5)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert results.count(200) == 1
    assert results.count(409) == 4


# ---------- 批次约束与召回 ----------

def test_invalid_batch_binding_and_release_blocked(client):
    ing = mk_ingredient(client, "核桃", ["坚果"])
    bid = mk_batch(client, ing, "W-1")
    rid = mk_recipe(client, "R")
    vid = mk_version(client, rid)
    add_ing(client, vid, ing, bid)
    client.post(f"/api/batches/{bid}/recall")
    # 失效批次不能绑定到新条目
    r = client.post(f"/api/versions/{vid}/items",
                    json={"kind": "ingredient", "ingredient_id": ing, "batch_id": bid, "qty": 1})
    assert r.status_code == 400
    # 召回使待审批确认失效且放行被阻止
    submit(client, vid)
    confirm(client, vid, "alice")
    confirm(client, vid, "bob")
    r = release(client, vid)
    assert r.status_code == 400 and "批次" in r.json()["detail"]


def test_recall_marks_re_review_preserves_release_and_paths(client):
    peanut = mk_ingredient(client, "花生", ["花生"])
    bid = mk_batch(client, peanut, "P-9")
    r1 = mk_recipe(client, "底料")
    v1 = mk_version(client, r1)
    add_ing(client, v1, peanut, bid)
    full_release(client, v1)
    r2 = mk_recipe(client, "成品")
    v2 = mk_version(client, r2)
    add_sub(client, v2, v1)
    full_release(client, v2)

    rel_before = client.get(f"/api/versions/{v2}").json()["release"]
    res = client.post(f"/api/batches/{bid}/recall").json()
    affected = {m["version_id"] for m in res["affected"]}
    assert affected == {v1, v2}  # 沿引用链向上传播

    v2_after = client.get(f"/api/versions/{v2}").json()
    assert v2_after["status"] == "re_review"
    # 既有放行记录不被改写
    assert v2_after["release"] == rel_before
    # 待复核列表展示传播路径
    rr = client.get("/api/re-review").json()
    mark = [m for m in rr if m["version_id"] == v2][0]
    assert mark["paths"][0][0]["desc"].startswith("子配方:底料")
    assert "P-9" in mark["paths"][0][1]["desc"]
    # 待复核版本不能直接再放行
    assert release(client, v2).status_code == 409
    # 新版本继承条目, 换批次后重新走审批放行
    new_bid = mk_batch(client, peanut, "P-10")
    v1b = mk_version(client, r1)  # 继承旧条目(含召回批次)
    old_item = v1b and client.get(f"/api/versions/{v1b}").json()["items"][0]
    s = client.post(f"/api/versions/{v1b}/substitutions",
                    json={"item_id": old_item["id"], "new_ingredient_id": peanut,
                          "new_batch_id": new_bid, "proposer": "qa"}).json()
    h = client.get(f"/api/versions/{v1b}").json()["snapshot_hash"]
    assert client.post(f"/api/substitutions/{s['id']}/apply",
                       json={"expected_snapshot_hash": h}).status_code == 200
    full_release(client, v1b)
    v2b = mk_version(client, r2)  # 继承引用 v1... 需改为引用 v1b
    # 新版本中旧引用仍指向 v1(re_review), 放行应被拒
    submit(client, v2b)
    confirm(client, v2b, "alice")
    confirm(client, v2b, "bob")
    assert release(client, v2b).status_code == 400


def test_recall_between_confirmations_invalidates(client):
    ing = mk_ingredient(client, "奶", ["乳"])
    bid = mk_batch(client, ing, "M-1")
    rid = mk_recipe(client, "R")
    vid = mk_version(client, rid)
    add_ing(client, vid, ing, bid)
    submit(client, vid)
    confirm(client, vid, "alice")
    client.post(f"/api/batches/{bid}/recall")  # 第二次确认前批次变化
    detail = client.get(f"/api/versions/{vid}").json()
    assert detail["confirmations"][0]["status"] == "invalidated"
    confirm(client, vid, "bob")
    assert release(client, vid).status_code == 400


# ---------- 差异 / 导出一致性 ----------

def test_diff_and_export_share_server_version(client):
    a = mk_ingredient(client, "花生", ["花生"])
    b = mk_ingredient(client, "奶", ["乳"])
    rid = mk_recipe(client, "R")
    v1 = mk_version(client, rid)
    add_ing(client, v1, a, qty=2)
    v2 = mk_version(client, rid)  # 继承 v1 条目
    item_b = add_ing(client, v2, b, qty=1)

    diff = client.get(f"/api/versions/{v2}/diff?against={v1}").json()
    assert [x["desc"] for x in diff["added"]] == ["原料:奶"]
    assert diff["removed"] == []

    labels = client.get(f"/api/versions/{v2}/labels").json()
    export = client.get(f"/api/versions/{v2}/export").json()
    assert export["snapshot_hash"] == labels["snapshot_hash"]
    assert export["labels"] == labels["labels"]
    assert set(export["labels"]) == {"花生", "乳"}
    assert "attachment" in client.get(f"/api/versions/{v2}/export").headers["content-disposition"]
