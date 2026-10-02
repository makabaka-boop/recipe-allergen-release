"""批次召回传播、既有放行记录不改写、替代原料与并发替换、失效批次、循环引用阻断。"""
import threading

import pytest

from app import service
from app.db import new_conn, transaction
from tests.conftest import ids_by_code


def _version(conn, recipe_code: str, no: int) -> int:
    return conn.execute(
        """SELECT v.id FROM recipe_versions v JOIN recipes r ON r.id=v.recipe_id
            WHERE r.code=? AND v.version_no=?""",
        (recipe_code, no),
    ).fetchone()["id"]


# ---------------- 召回 ----------------
def test_recall_propagates_through_multiple_levels_and_marks_review(fresh_db):
    conn = fresh_db
    milk_batch = conn.execute("SELECT id FROM batches WHERE code='B-MILK-2609'").fetchone()["id"]
    custard = _version(conn, "RC-CUSTARD", 1)
    cake = _version(conn, "RC-CAKE", 1)
    sign1 = _version(conn, "RC-SIGN", 1)
    sign2 = _version(conn, "RC-SIGN", 2)

    with transaction(conn):
        result = service.recall_batch(conn, milk_batch, "牧场抽检不合格")

    assert set(result["affected_versions"]) == {custard, cake, sign1, sign2}  # 多级传递 + 全部历史版本
    for vid in (custard, cake, sign1, sign2):
        row = conn.execute("SELECT needs_review FROM recipe_versions WHERE id=?", (vid,)).fetchone()
        assert row["needs_review"] == 1


def test_recall_does_not_rewrite_release_history(fresh_db):
    """召回偷偷不改写放行：status/released_on/审批行保留，只新增 needs_review 与失效标记。"""
    conn = fresh_db
    sign = _version(conn, "RC-SIGN", 1)
    before = conn.execute(
        "SELECT status,released_on FROM recipe_versions WHERE id=?", (sign,)
    ).fetchone()
    approvals_before = conn.execute(
        "SELECT reviewer_id,decided_on FROM approvals WHERE recipe_version_id=? ORDER BY id", (sign,)
    ).fetchall()

    milk_batch = conn.execute("SELECT id FROM batches WHERE code='B-MILK-2609'").fetchone()["id"]
    with transaction(conn):
        service.recall_batch(conn, milk_batch, "模拟召回")

    after = conn.execute("SELECT status,released_on,needs_review FROM recipe_versions WHERE id=?", (sign,)).fetchone()
    assert after["status"] == before["status"] == "released"
    assert after["released_on"] == before["released_on"]
    assert after["needs_review"] == 1
    approvals_after = conn.execute(
        "SELECT reviewer_id,decided_on FROM approvals WHERE recipe_version_id=? ORDER BY id", (sign,)
    ).fetchall()
    assert [dict(a) for a in approvals_after] == [dict(a) for a in approvals_before]
    # 召回事件是追加的
    assert conn.execute("SELECT COUNT(*) n FROM batch_recalls WHERE batch_id=?",
                        (milk_batch,)).fetchone()["n"] == 1


def test_released_version_hit_by_recall_blocks_downstream_new_release(fresh_db):
    """召回命中后，新版本的放行也会因子配方待复核而被阻止，直至重新放行链路。"""
    conn = fresh_db
    milk_batch = conn.execute("SELECT id FROM batches WHERE code='B-MILK-2609'").fetchone()["id"]
    with transaction(conn):
        service.recall_batch(conn, milk_batch, "模拟召回")

    # 招牌蛋糕 v2（种子待审批）同时引用了受召回链路
    sign2 = _version(conn, "RC-SIGN", 2)
    problems = service.assess_version(conn, sign2)
    assert any("待复核" in p for p in problems)


def test_new_version_after_recall_requires_full_release(client, fresh_db):
    """召回后产生的新版本 needs_review 不继承，但需重新双人放行。"""
    conn = fresh_db
    ids = ids_by_code(conn, "MILK", "OATMILK", "EGG", "FLOUR")
    milk_batch = conn.execute("SELECT id FROM batches WHERE code='B-MILK-2609'").fetchone()["id"]
    with transaction(conn):
        service.recall_batch(conn, milk_batch, "牛奶召回")
    custard1 = _version(conn, "RC-CUSTARD", 1)

    oat_batch = conn.execute("SELECT id FROM batches WHERE code='B-OAT-2610'").fetchone()["id"]
    egg_batch = conn.execute("SELECT id FROM batches WHERE code='B-EGG-2609'").fetchone()["id"]
    flour_batch = conn.execute("SELECT id FROM batches WHERE code='B-FLOUR-2609'").fetchone()["id"]
    with transaction(conn):
        v2 = service.create_version(conn, 1, "起草人-赵配方", [
            {"kind": "material", "material_id": ids["OATMILK"], "batch_id": oat_batch, "qty": "400g"},
            {"kind": "material", "material_id": ids["EGG"], "batch_id": egg_batch, "qty": "200g"},
            {"kind": "material", "material_id": ids["FLOUR"], "batch_id": flour_batch, "qty": "60g"},
        ], note="牛奶替换为燕麦奶", expected_current_version_id=custard1)

    row = conn.execute("SELECT status,needs_review FROM recipe_versions WHERE id=?", (v2,)).fetchone()
    assert row["status"] == "in_approval" and row["needs_review"] == 0
    assert {l["code"] for l in service.resolve_graph(conn, v2)["labels"]} <= {"EGG", "GLUTEN"}


# ---------------- 替代原料 ----------------
def _custard_components(conn, version_id: int):
    return conn.execute(
        "SELECT id,material_id FROM components WHERE recipe_version_id=? AND kind='material' ORDER BY position",
        (version_id,),
    ).fetchall()


def test_substitution_proposal_apply_creates_version_and_drops_milk_label(client, fresh_db):
    conn = fresh_db
    custard = _version(conn, "RC-CUSTARD", 1)
    milk_comp = _custard_components(conn, custard)[0]
    ids = ids_by_code(conn, "OATMILK")
    oat_batch = conn.execute("SELECT id FROM batches WHERE code='B-OAT-2610'").fetchone()["id"]
    proposer = conn.execute("SELECT id FROM reviewers WHERE name='审核者C-周婷'").fetchone()["id"]

    with transaction(conn):
        pid = service.create_proposal(
            conn, recipe_id=1, base_version_id=custard, component_id=milk_comp["id"],
            new_material_id=ids["OATMILK"], new_batch_id=oat_batch,
            proposed_by=proposer, reason="消费者乳敏反馈，改用燕麦奶")
        new_vid = service.apply_proposal(conn, pid)

    labels = {l["code"] for l in service.resolve_graph(conn, new_vid)["labels"]}
    assert "MILK" not in labels and "EGG" in labels
    # 原版本不动
    assert {l["code"] for l in service.resolve_graph(conn, custard)["labels"]} >= {"MILK", "EGG"}
    p = conn.execute("SELECT status,resulting_version_id FROM proposals WHERE id=?", (pid,)).fetchone()
    assert p["status"] == "applied" and p["resulting_version_id"] == new_vid

    # HTTP 层核对差异：新版本相对旧版本 MILK 标签消失
    resp = client.get(f"/api/diff/{custard}/{new_vid}")
    assert resp.status_code == 200
    body = resp.json()
    assert "MILK" in body["labels_removed"]
    assert any(e["change"] == "modified" and "燕麦奶" in e["name"] for e in body["entries"])


def test_proposal_rejects_recalled_or_foreign_batch(fresh_db):
    conn = fresh_db
    custard = _version(conn, "RC-CUSTARD", 1)
    milk_comp = _custard_components(conn, custard)[0]
    ids = ids_by_code(conn, "OATMILK", "EGG")
    egg_batch = conn.execute("SELECT id FROM batches WHERE code='B-EGG-2609'").fetchone()["id"]
    oat_batch = conn.execute("SELECT id FROM batches WHERE code='B-OAT-2610'").fetchone()["id"]
    proposer = conn.execute("SELECT id FROM reviewers WHERE name='审核者C-周婷'").fetchone()["id"]

    # 批次不属于所选原料
    with pytest.raises(service.ServiceError) as ei:
        with transaction(conn):
            service.create_proposal(conn, 1, custard, milk_comp["id"],
                                    ids["OATMILK"], egg_batch, proposer)
    assert ei.value.status == 422

    # 召回燕麦批次后再提案
    with transaction(conn):
        service.recall_batch(conn, oat_batch, "演练召回")
    with pytest.raises(service.ServiceError) as ei2:
        with transaction(conn):
            service.create_proposal(conn, 1, custard, milk_comp["id"],
                                    ids["OATMILK"], oat_batch, proposer)
    assert ei2.value.status == 422


def test_concurrent_substitution_only_one_wins(fresh_db):
    """两个会话基于同一旧版本并发采纳替换：第二个 409 失败并被标 superseded，不产生错误发布。"""
    conn = fresh_db
    custard = _version(conn, "RC-CUSTARD", 1)
    ids = ids_by_code(conn, "OATMILK", "BUTTER")
    oat_batch = conn.execute("SELECT id FROM batches WHERE code='B-OAT-2610'").fetchone()["id"]
    butter_batch = conn.execute("SELECT id FROM batches WHERE code='B-BTR-2609'").fetchone()["id"]
    reviewer = conn.execute("SELECT id FROM reviewers WHERE name='审核者C-周婷'").fetchone()["id"]
    milk_comp = _custard_components(conn, custard)[0]

    p1 = p2 = None
    with transaction(conn):
        p1 = service.create_proposal(conn, 1, custard, milk_comp["id"],
                                     ids["OATMILK"], oat_batch, reviewer, "换燕麦奶")
        p2 = service.create_proposal(conn, 1, custard, milk_comp["id"],
                                     ids["BUTTER"], butter_batch, reviewer, "换黄油")

    outcomes = {}

    def worker(pid: int, key: str):
        c = new_conn()
        try:
            with transaction(c):
                outcomes[key] = ("ok", service.apply_proposal(c, pid))
        except Exception as e:  # noqa: BLE001
            outcomes[key] = ("err", str(e))
        finally:
            c.close()

    t1 = threading.Thread(target=worker, args=(p1, "oat"))
    t2 = threading.Thread(target=worker, args=(p2, "butter"))
    t1.start(); t2.start(); t1.join(); t2.join()

    statuses = {k: v[0] for k, v in outcomes.items()}
    assert sorted(statuses.values()) == ["err", "ok"]
    cur = conn.execute("SELECT current_version_id,revision FROM recipes WHERE id=1").fetchone()
    assert cur["revision"] == 1  # 只前进一个版本
    # 输家提案被标 superseded
    loser = p2 if outcomes["oat"][0] == "ok" else p1
    assert conn.execute("SELECT status FROM proposals WHERE id=?", (loser,)).fetchone()["status"] == "superseded"


# ---------------- 失效批次 / 循环引用 ----------------
def test_expired_batch_blocks_version_creation(fresh_db):
    conn = fresh_db
    ids = ids_by_code(conn, "MILK", "EGG", "FLOUR")
    old_milk = conn.execute("SELECT id FROM batches WHERE code='B-MILK-2608-OLD'").fetchone()["id"]
    egg_batch = conn.execute("SELECT id FROM batches WHERE code='B-EGG-2609'").fetchone()["id"]
    flour_batch = conn.execute("SELECT id FROM batches WHERE code='B-FLOUR-2609'").fetchone()["id"]
    with pytest.raises(service.ServiceError) as ei:
        with transaction(conn):
            service.create_version(conn, 1, "起草人-赵配方", [
                {"kind": "material", "material_id": ids["MILK"], "batch_id": old_milk, "qty": "1"},
                {"kind": "material", "material_id": ids["EGG"], "batch_id": egg_batch, "qty": "1"},
                {"kind": "material", "material_id": ids["FLOUR"], "batch_id": flour_batch, "qty": "1"},
            ])
    assert ei.value.status == 422 and "过期" in str(ei.value)


def test_circular_reference_blocks_publication(fresh_db):
    """直接写库构造环（API 不允许引用未来版本），assess 必须阻止发布并报告路径。"""
    conn = fresh_db
    custard = _version(conn, "RC-CUSTARD", 1)
    cake = _version(conn, "RC-CAKE", 1)
    # 让卡仕达酱反向引用蛋糕胚 -> 环
    pos = conn.execute("SELECT COALESCE(MAX(position),-1)+1 p FROM components WHERE recipe_version_id=?",
                       (custard,)).fetchone()["p"]
    conn.execute(
        "INSERT INTO components(recipe_version_id,position,kind,child_version_id,qty) VALUES(?,?,'subrecipe',?,?)",
        (custard, pos, cake, "构造环"))
    problems = service.assess_version(conn, custard)
    assert any("循环引用" in p for p in problems)
    graph = service.resolve_graph(conn, custard)
    assert graph["cycles"]


def test_unbound_material_blocks(fresh_db):
    conn = fresh_db
    ids = ids_by_code(conn, "MILK")
    with pytest.raises(service.ServiceError) as ei:
        with transaction(conn):
            service.create_version(conn, 1, "起草人-赵配方", [
                {"kind": "material", "material_id": ids["MILK"], "batch_id": None, "qty": "1"},
            ])
    assert ei.value.status == 422
