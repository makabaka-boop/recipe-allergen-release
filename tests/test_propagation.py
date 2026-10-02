"""多级引用下的标签传播路径与快照不可变性。"""
from app import serializers, service
from app.db import transaction
from tests.conftest import ids_by_code


def _sign_current(conn):
    return conn.execute(
        "SELECT current_version_id FROM recipes WHERE code='RC-SIGN'"
    ).fetchone()["current_version_id"]


def test_multi_level_propagation_paths(fresh_db):
    """招牌蛋糕当前版经 蛋糕胚v1 -> 卡仕达酱v1 传播 MILK/EGG，GLUTEN 有两条路径。"""
    conn = fresh_db
    g = serializers.serialize_graph(conn, _sign_current(conn))

    by_code = {l["code"]: l for l in g["labels"]}
    assert set(by_code) == {"EGG", "GLUTEN", "MILK", "NUT", "PEANUT", "SOY"}

    milk_path = by_code["MILK"]["paths"][0]
    assert [h["kind"] for h in milk_path] == ["subrecipe", "subrecipe", "material"]
    assert milk_path[0]["name"].startswith("戚风蛋糕胚")
    assert milk_path[1]["name"].startswith("卡仕达酱")
    assert milk_path[2]["code"] == "MILK"
    assert milk_path[2]["batch_code"] == "B-MILK-2609"

    gluten_paths = by_code["GLUTEN"]["paths"]
    assert len(gluten_paths) == 2  # 一条经卡仕达，一条蛋糕胚直接加面粉
    assert {p[-1]["batch_code"] for p in gluten_paths} == {"B-FLOUR-2609"}


def test_version_snapshot_immutable_after_binding_change(fresh_db):
    """原料过敏原绑定后来变化，旧版本快照标签不变（material_labels 是版本快照）。"""
    conn = fresh_db
    vid = _sign_current(conn)
    before = {l["code"] for l in serializers.serialize_graph(conn, vid)["labels"]}

    oat = ids_by_code(conn, "OATMILK")["OATMILK"]
    with transaction(conn):
        conn.execute(
            "INSERT INTO material_allergens(material_id,allergen_code) VALUES(?,?)",
            (oat, "MILK"),
        )

    after = {l["code"] for l in serializers.serialize_graph(conn, vid)["labels"]}
    assert before == after  # 旧版本完全不受影响


def test_released_history_kept_and_new_version_needs_release(fresh_db):
    conn = fresh_db
    v1 = conn.execute(
        """SELECT v.id FROM recipe_versions v JOIN recipes r ON r.id=v.recipe_id
            WHERE r.code='RC-CUSTARD' AND v.version_no=1"""
    ).fetchone()["id"]
    ids = ids_by_code(conn, "EGG", "FLOUR", "MILK")
    milk_batch = conn.execute("SELECT id FROM batches WHERE code='B-MILK-2609'").fetchone()["id"]
    flour_batch = conn.execute("SELECT id FROM batches WHERE code='B-FLOUR-2609'").fetchone()["id"]
    new_egg_batch = conn.execute(
        "INSERT INTO batches(material_id,code,received_on,expires_on,status) VALUES(?,?,?,?, 'active')",
        (ids["EGG"], "B-EGG-2610", "2026-10-01", "2026-11-01"),
    ).lastrowid

    with transaction(conn):
        new_vid = service.create_version(conn, 1, "起草人-赵配方", [
            {"kind": "material", "material_id": ids["MILK"], "batch_id": milk_batch, "qty": "400g"},
            {"kind": "material", "material_id": ids["EGG"], "batch_id": new_egg_batch, "qty": "200g"},
            {"kind": "material", "material_id": ids["FLOUR"], "batch_id": flour_batch, "qty": "60g"},
        ], note="鸡蛋换批次")

    old = conn.execute("SELECT status,released_on FROM recipe_versions WHERE id=?", (v1,)).fetchone()
    assert old["status"] == "released" and old["released_on"]
    new = conn.execute("SELECT status,released_on FROM recipe_versions WHERE id=?", (new_vid,)).fetchone()
    assert new["status"] == "in_approval" and new["released_on"] is None
