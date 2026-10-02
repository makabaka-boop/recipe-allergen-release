"""双人放行、同一审核者、快照变化 / 批次召回导致审批失效，以及并发竞争。"""
import threading

import pytest

from app import service
from app.db import new_conn, transaction
from tests.conftest import ids_by_code


REVIEWER = {}


def _setup():
    """招牌蛋糕 v2 已有审核者A 的一次确认（种子内置）。返回 (version_id, 审核者id表)。"""
    conn = new_conn()
    v = conn.execute(
        "SELECT current_version_id FROM recipes WHERE code='RC-SIGN'"
    ).fetchone()["current_version_id"]
    rows = conn.execute("SELECT id,name FROM reviewers").fetchall()
    rids = {r["name"]: r["id"] for r in rows}
    conn.close()
    return v, rids


def test_seed_state_one_approval_waiting_second(fresh_db):
    conn = fresh_db
    v, rids = _setup()
    row = conn.execute(
        "SELECT COUNT(*) n FROM approvals WHERE recipe_version_id=? AND stale=0", (v,)
    ).fetchone()
    assert row["n"] == 1
    vrow = conn.execute("SELECT status FROM recipe_versions WHERE id=?", (v,)).fetchone()
    assert vrow["status"] == "in_approval"


def test_two_distinct_reviewers_release_same_snapshot(fresh_db):
    conn = fresh_db
    v, rids = _setup()
    with transaction(conn):
        result = service.approve(conn, v, rids["审核者B-李强"])
    assert result["released"] is True
    assert conn.execute("SELECT status FROM recipe_versions WHERE id=?", (v,)).fetchone()["status"] == "released"
    approvers = {r["reviewer_id"] for r in conn.execute(
        "SELECT reviewer_id FROM approvals WHERE recipe_version_id=? AND stale=0", (v,))}
    assert len(approvers) == 2


def test_same_reviewer_cannot_confirm_twice(fresh_db):
    conn = fresh_db
    v, rids = _setup()
    from app.service import ServiceError
    with transaction(conn):
        with pytest.raises(ServiceError) as ei:
            service.approve(conn, v, rids["审核者A-王敏"])
    assert ei.value.status == 409
    assert conn.execute("SELECT status FROM recipe_versions WHERE id=?", (v,)).fetchone()["status"] == "in_approval"


def test_approval_invalidated_when_recipe_changes_before_second(fresh_db):
    """第一次确认后、第二次确认前配方产生新版本 -> 旧快照审批失效，且不能放行旧快照。"""
    conn = fresh_db
    v, rids = _setup()
    ids = ids_by_code(conn, "SOYLEC", "ALMOND")
    cake_v1 = conn.execute(
        """SELECT v.id FROM recipe_versions v JOIN recipes r ON r.id=v.recipe_id
            WHERE r.code='RC-CAKE' AND v.version_no=1"""
    ).fetchone()["id"]
    soy_b = conn.execute("SELECT id FROM batches WHERE code='B-SOY-2607'").fetchone()["id"]
    almond_b = conn.execute("SELECT id FROM batches WHERE code='B-ALM-2609'").fetchone()["id"]

    # 起草人又提了 v3（杏仁增量变化），把 v2 变成旧版本
    with transaction(conn):
        v3 = service.create_version(conn, 3, "起草人-赵配方", [
            {"kind": "subrecipe", "child_version_id": cake_v1, "qty": "1 个蛋糕胚"},
            {"kind": "material", "material_id": ids["SOYLEC"], "batch_id": soy_b, "qty": "5g"},
            {"kind": "material", "material_id": ids["ALMOND"], "batch_id": almond_b, "qty": "40g"},
        ], note="杏仁加量")

    # 旧版本上审核者A 的那次确认被标 stale
    stale = conn.execute(
        "SELECT stale,stale_reason FROM approvals WHERE recipe_version_id=? AND reviewer_id=?",
        (v, rids["审核者A-王敏"]),
    ).fetchone()
    assert stale["stale"] == 1 and stale["stale_reason"]

    from app.service import ServiceError
    with transaction(conn):
        with pytest.raises(ServiceError) as ei:
            service.approve(conn, v, rids["审核者B-李强"])  # 试图放行旧快照
    assert ei.value.status == 409

    # 新快照需要两位审核者重新确认
    assert conn.execute("SELECT COUNT(*) n FROM approvals WHERE recipe_version_id=? AND stale=0",
                        (v3,)).fetchone()["n"] == 0
    with transaction(conn):
        service.approve(conn, v3, rids["审核者A-王敏"])
        service.approve(conn, v3, rids["审核者B-李强"])
    assert conn.execute("SELECT status FROM recipe_versions WHERE id=?", (v3,)).fetchone()["status"] == "released"


def test_recall_before_second_approval_blocks_and_invalidates_first(fresh_db):
    """第二次确认前批次被召回：第一次确认失效，发布被阻止。"""
    conn = fresh_db
    v, rids = _setup()
    soy_b = conn.execute("SELECT id FROM batches WHERE code='B-SOY-2607'").fetchone()["id"]
    with transaction(conn):
        service.recall_batch(conn, soy_b, "供应商通报大豆卵磷脂交叉污染")

    problems = service.assess_version(conn, v)
    assert any("召回" in p for p in problems)

    from app.service import ServiceError
    with transaction(conn):
        with pytest.raises(ServiceError) as ei:
            service.approve(conn, v, rids["审核者B-李强"])
    assert ei.value.status == 422
    assert conn.execute("SELECT status FROM recipe_versions WHERE id=?", (v,)).fetchone()["status"] == "in_approval"
    # 第一次确认被标记失效
    assert conn.execute(
        "SELECT COUNT(*) n FROM approvals WHERE recipe_version_id=? AND stale=0", (v,)
    ).fetchone()["n"] == 0
    assert conn.execute(
        "SELECT COUNT(*) n FROM approvals WHERE recipe_version_id=? AND stale=1", (v,)
    ).fetchone()["n"] == 1


def test_concurrent_two_approvals_only_releases_with_distinct_reviewers(fresh_db):
    """两个连接同时对同一快照确认：串行化后仅不同审核者组合可放行；同审核者竞争被唯一约束挡住。"""
    conn = fresh_db
    v, rids = _setup()
    errors: list[Exception] = []

    def worker(reviewer: str):
        c = new_conn()
        try:
            with transaction(c):
                service.approve(c, v, rids[reviewer])
        except Exception as e:  # noqa: BLE001
            errors.append(e)
        finally:
            c.close()

    t1 = threading.Thread(target=worker, args=("审核者B-李强",))
    t2 = threading.Thread(target=worker, args=("审核者B-李强",))  # 与A已确认 + B并发两次
    t1.start(); t2.start(); t1.join(); t2.join()

    assert len(errors) == 1  # 恰好一个 B 撞唯一约束
    assert conn.execute("SELECT status FROM recipe_versions WHERE id=?", (v,)).fetchone()["status"] == "released"
    assert conn.execute(
        "SELECT COUNT(*) n FROM approvals WHERE recipe_version_id=? AND stale=0", (v,)
    ).fetchone()["n"] == 2


def test_concurrent_distinct_reviewers_release(fresh_db):
    """种子版 v2 上 B/C 几乎同时确认，任一顺序都应恰好放行一次。"""
    conn = fresh_db
    # 先清掉种子里 A 的确认，构造一个零确认起点
    conn.execute("DELETE FROM approvals WHERE recipe_version_id=(SELECT current_version_id FROM recipes WHERE code='RC-SIGN')")
    v, rids = _setup()
    errors: list[Exception] = []

    def worker(reviewer: str):
        c = new_conn()
        try:
            with transaction(c):
                service.approve(c, v, rids[reviewer])
        except Exception as e:  # noqa: BLE001
            errors.append(e)
        finally:
            c.close()

    ts = [threading.Thread(target=worker, args=(name,))
          for name in ("审核者B-李强", "审核者C-周婷")]
    for t in ts: t.start()
    for t in ts: t.join()

    assert errors == []
    assert conn.execute("SELECT status FROM recipe_versions WHERE id=?", (v,)).fetchone()["status"] == "released"
    assert conn.execute("SELECT released_on FROM recipe_versions WHERE id=?", (v,)).fetchone()["released_on"]
