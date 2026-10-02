"""灌入模拟过敏原标签数据。

引用结构（配方只能引用更早的子配方版本）：
  卡仕达酱 v1 ──┬─ 牛奶（批次）
                ├─ 鸡蛋液（批次）
                └─ 小麦粉（批次）
  蛋糕胚 v1   ──┬─ 卡仕达酱 v1（子配方）
                ├─ 小麦粉（批次）
                └─ 花生碎（批次）
  招牌蛋糕 v1 ──┬─ 蛋糕胚 v1（子配方）
                └─ 大豆卵磷脂（批次）
  招牌蛋糕 v2（待第二次确认）：起草人已提，审核者A 已确认，等审核者B
另含一个已过期的牛奶批次用于"失效批次阻断发布"，一个燕麦奶（无过敏原）替代候选。
"""
from __future__ import annotations

import sys

from . import service
from .db import init_db, new_conn, transaction

ALLERGENS = [
    ("GLUTEN", "含麸质的谷物及其制品"),
    ("CRUSTACEAN", "甲壳纲类动物及其制品"),
    ("EGG", "蛋类及其制品"),
    ("FISH", "鱼类及其制品"),
    ("PEANUT", "花生及其制品"),
    ("SOY", "大豆及其制品"),
    ("MILK", "乳及乳制品（包括乳糖）"),
    ("NUT", "坚果及其果仁类制品"),
]

REVIEWERS = ["审核者A-王敏", "审核者B-李强", "审核者C-周婷", "起草人-赵配方"]

# (code, name, [allergen codes])
MATERIALS = [
    ("MILK", "全脂牛奶", ["MILK"]),
    ("OATMILK", "燕麦奶（无乳制品交叉）", []),
    ("EGG", "巴氏杀菌鸡蛋液", ["EGG"]),
    ("FLOUR", "高筋小麦粉", ["GLUTEN"]),
    ("PEANUT", "烘烤花生碎", ["PEANUT"]),
    ("SOYLEC", "大豆卵磷脂", ["SOY"]),
    ("BUTTER", "无盐黄油", ["MILK"]),
    ("ALMOND", "扁桃仁粉", ["NUT"]),
    ("SHRIMP", "虾仁", ["CRUSTACEAN"]),
]

# (material_code, batch_code, received, expires, status)
BATCHES = [
    ("MILK", "B-MILK-2609", "2026-09-10", "2026-10-10", "active"),
    ("MILK", "B-MILK-2608-OLD", "2026-08-01", "2026-09-01", "active"),  # 已过期（按今天 2026-10-02）
    ("OATMILK", "B-OAT-2610", "2026-09-20", "2026-11-20", "active"),
    ("EGG", "B-EGG-2609", "2026-09-12", "2026-10-12", "active"),
    ("FLOUR", "B-FLOUR-2609", "2026-09-05", "2026-12-05", "active"),
    ("PEANUT", "B-PEA-2608", "2026-08-20", "2026-11-20", "active"),
    ("SOYLEC", "B-SOY-2607", "2026-07-15", "2027-01-15", "active"),
    ("BUTTER", "B-BTR-2609", "2026-09-18", "2026-10-25", "active"),
    ("ALMOND", "B-ALM-2609", "2026-09-02", "2026-12-02", "active"),
    ("SHRIMP", "B-SHR-2609", "2026-09-28", "2026-10-28", "active"),
]


def seed(reset: bool = True) -> None:
    init_db()
    conn = new_conn()
    try:
        if reset:
            conn.execute("PRAGMA foreign_keys=OFF")
            conn.execute("BEGIN IMMEDIATE")
            try:
                for tbl in ("approvals", "components", "recipe_versions", "proposals",
                            "batch_recalls", "batches", "material_allergens", "materials",
                            "recipes", "reviewers", "allergens"):
                    conn.execute(f"DELETE FROM {tbl}")
                conn.execute("COMMIT")
            except Exception:
                conn.execute("ROLLBACK")
                raise
            conn.execute("PRAGMA foreign_keys=ON")
        with transaction(conn):
            for code, name in ALLERGENS:
                conn.execute("INSERT INTO allergens(code,name) VALUES(?,?)", (code, name))
            rids = {}
            for name in REVIEWERS:
                cur = conn.execute("INSERT INTO reviewers(name) VALUES(?)", (name,))
                rids[name] = cur.lastrowid
            mids: dict[str, int] = {}
            for code, name, labels in MATERIALS:
                cur = conn.execute("INSERT INTO materials(code,name) VALUES(?,?)", (code, name))
                mids[code] = cur.lastrowid
                for lab in labels:
                    conn.execute(
                        "INSERT INTO material_allergens(material_id,allergen_code) VALUES(?,?)",
                        (cur.lastrowid, lab))
            bids: dict[str, int] = {}
            for mcode, bcode, recv, exp, status in BATCHES:
                cur = conn.execute(
                    "INSERT INTO batches(material_id,code,received_on,expires_on,status) VALUES(?,?,?,?,?)",
                    (mids[mcode], bcode, recv, exp, status))
                bids[bcode] = cur.lastrowid

            def new_recipe(code: str, name: str) -> int:
                return conn.execute(
                    "INSERT INTO recipes(code,name,revision) VALUES(?,?,-1)", (code, name)
                ).lastrowid

            r_custard = new_recipe("RC-CUSTARD", "卡仕达酱")
            r_cake = new_recipe("RC-CAKE", "戚风蛋糕胚")
            r_sign = new_recipe("RC-SIGN", "招牌复合蛋糕")

            # 1) 卡仕达酱 v1，放行
            v1 = service.create_version(conn, r_custard, "起草人-赵配方", [
                {"kind": "material", "material_id": mids["MILK"], "batch_id": bids["B-MILK-2609"], "qty": "400g"},
                {"kind": "material", "material_id": mids["EGG"], "batch_id": bids["B-EGG-2609"], "qty": "200g"},
                {"kind": "material", "material_id": mids["FLOUR"], "batch_id": bids["B-FLOUR-2609"], "qty": "60g"},
            ], note="初版")
            service.approve(conn, v1, rids["审核者A-王敏"])
            service.approve(conn, v1, rids["审核者B-李强"])

            # 2) 蛋糕胚 v1 引用卡仕达酱，放行
            v2 = service.create_version(conn, r_cake, "起草人-赵配方", [
                {"kind": "subrecipe", "child_version_id": v1, "qty": "660g（卡仕达整批）"},
                {"kind": "material", "material_id": mids["FLOUR"], "batch_id": bids["B-FLOUR-2609"], "qty": "300g"},
                {"kind": "material", "material_id": mids["PEANUT"], "batch_id": bids["B-PEA-2608"], "qty": "30g"},
            ], note="初版，含卡仕达子配方")
            service.approve(conn, v2, rids["审核者A-王敏"])
            service.approve(conn, v2, rids["审核者C-周婷"])

            # 3) 招牌复合蛋糕 v1（二级引用），放行
            v3 = service.create_version(conn, r_sign, "起草人-赵配方", [
                {"kind": "subrecipe", "child_version_id": v2, "qty": "1 个蛋糕胚"},
                {"kind": "material", "material_id": mids["SOYLEC"], "batch_id": bids["B-SOY-2607"], "qty": "5g"},
            ], note="初版")
            service.approve(conn, v3, rids["审核者B-李强"])
            service.approve(conn, v3, rids["审核者C-周婷"])

            # 4) v2 起草：新增扁桃仁粉，仅审核者A确认，等待第二人
            v4 = service.create_version(conn, r_sign, "起草人-赵配方", [
                {"kind": "subrecipe", "child_version_id": v2, "qty": "1 个蛋糕胚"},
                {"kind": "material", "material_id": mids["SOYLEC"], "batch_id": bids["B-SOY-2607"], "qty": "5g"},
                {"kind": "material", "material_id": mids["ALMOND"], "batch_id": bids["B-ALM-2609"], "qty": "20g"},
            ], note="添加扁桃仁粉调整口感，待双人放行")
            service.approve(conn, v4, rids["审核者A-王敏"])
    finally:
        conn.close()
    print("seed done")


if __name__ == "__main__":
    seed(reset="--keep" not in sys.argv)
