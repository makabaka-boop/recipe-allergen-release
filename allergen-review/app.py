"""过敏原标签复核台 — FastAPI + SQLite 服务端。

核心规则:
- 配方版本由若干条目组成, 条目可引用原料(可绑定供应批次)或"更早创建"的子配方版本(保证无环)。
- 快照哈希覆盖条目及其绑定的批次状态、子配方版本哈希, 任何变化都会改变哈希。
- 放行须两名不同审核者对同一快照哈希确认; 第二次确认前配方或批次变化会使既有确认失效。
- 批次召回不改写既有放行记录, 而是把受影响版本标为 re_review 并记录传播路径; 需新建版本重新放行。
- 替换原料走乐观并发(期望哈希), 并发替换冲突返回 409。
"""
import json
import sqlite3
import hashlib
import threading
from datetime import datetime, timezone

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel

SCHEMA = """
CREATE TABLE IF NOT EXISTS ingredients(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  name TEXT UNIQUE NOT NULL,
  allergens TEXT NOT NULL DEFAULT '[]'
);
CREATE TABLE IF NOT EXISTS batches(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ingredient_id INTEGER NOT NULL REFERENCES ingredients(id),
  code TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'active'  -- active | recalled | expired
);
CREATE TABLE IF NOT EXISTS recipes(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  name TEXT UNIQUE NOT NULL
);
CREATE TABLE IF NOT EXISTS recipe_versions(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  recipe_id INTEGER NOT NULL REFERENCES recipes(id),
  version_no INTEGER NOT NULL,
  status TEXT NOT NULL DEFAULT 'draft',  -- draft | pending_approval | released | re_review | superseded
  snapshot_hash TEXT
);
CREATE TABLE IF NOT EXISTS items(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  version_id INTEGER NOT NULL REFERENCES recipe_versions(id),
  seq INTEGER NOT NULL,
  kind TEXT NOT NULL,  -- ingredient | recipe
  ingredient_id INTEGER REFERENCES ingredients(id),
  batch_id INTEGER REFERENCES batches(id),
  sub_version_id INTEGER REFERENCES recipe_versions(id),
  qty REAL NOT NULL DEFAULT 1
);
CREATE TABLE IF NOT EXISTS confirmations(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  version_id INTEGER NOT NULL REFERENCES recipe_versions(id),
  reviewer TEXT NOT NULL,
  snapshot_hash TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'valid'  -- valid | invalidated
);
CREATE TABLE IF NOT EXISTS releases(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  version_id INTEGER NOT NULL REFERENCES recipe_versions(id),
  snapshot_hash TEXT NOT NULL,
  released_by TEXT NOT NULL,
  released_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS substitutions(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  version_id INTEGER NOT NULL REFERENCES recipe_versions(id),
  item_id INTEGER NOT NULL REFERENCES items(id),
  new_ingredient_id INTEGER NOT NULL REFERENCES ingredients(id),
  new_batch_id INTEGER REFERENCES batches(id),
  proposer TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'proposed'  -- proposed | applied | superseded
);
CREATE TABLE IF NOT EXISTS recall_marks(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  version_id INTEGER NOT NULL REFERENCES recipe_versions(id),
  batch_id INTEGER NOT NULL REFERENCES batches(id),
  paths TEXT NOT NULL,
  created_at TEXT NOT NULL
);
"""


def now():
    return datetime.now(timezone.utc).isoformat()


class DB:
    def __init__(self, path):
        self.lock = threading.RLock()
        self.conn = sqlite3.connect(path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)

    def q(self, sql, args=()):
        with self.lock:
            return self.conn.execute(sql, args).fetchall()

    def one(self, sql, args=()):
        with self.lock:
            return self.conn.execute(sql, args).fetchone()

    def exec(self, sql, args=()):
        with self.lock:
            cur = self.conn.execute(sql, args)
            self.conn.commit()
            return cur


# ---------- 领域逻辑 ----------

def items_of(db, version_id):
    return db.q("SELECT * FROM items WHERE version_id=? ORDER BY seq", (version_id,))


def compute_hash(db, version_id, _memo=None):
    """快照哈希: 条目 + 绑定批次状态 + 子配方版本哈希(递归)。"""
    memo = _memo if _memo is not None else {}
    if version_id in memo:
        return memo[version_id]
    parts = []
    for it in items_of(db, version_id):
        if it["kind"] == "ingredient":
            bstatus = "-"
            if it["batch_id"]:
                b = db.one("SELECT status FROM batches WHERE id=?", (it["batch_id"],))
                bstatus = b["status"] if b else "missing"
            parts.append(f"ing:{it['ingredient_id']}:{it['batch_id']}:{bstatus}:{it['qty']}")
        else:
            sub = compute_hash(db, it["sub_version_id"], memo)
            parts.append(f"sub:{it['sub_version_id']}:{sub}:{it['qty']}")
    h = hashlib.sha256("|".join(parts).encode()).hexdigest()[:16]
    memo[version_id] = h
    return h


def _item_desc(db, it):
    if it["kind"] == "ingredient":
        ing = db.one("SELECT name FROM ingredients WHERE id=?", (it["ingredient_id"],))
        desc = f"原料:{ing['name'] if ing else it['ingredient_id']}"
        if it["batch_id"]:
            b = db.one("SELECT code, status FROM batches WHERE id=?", (it["batch_id"],))
            desc += f"(批次 {b['code']}/{b['status']})"
        return desc
    v = db.one(
        "SELECT rv.version_no, r.name FROM recipe_versions rv JOIN recipes r ON r.id=rv.recipe_id WHERE rv.id=?",
        (it["sub_version_id"],),
    )
    return f"子配方:{v['name']} v{v['version_no']}" if v else "子配方:?"


def resolve_labels(db, version_id):
    """返回 {allergen: [path, ...]}, path 为条目步骤列表(含配方名、条目描述)。"""
    out = {}

    def rec(vid, prefix, stack):
        if vid in stack:  # 防御: 正常流程靠"更早子配方"规则保证无环
            raise HTTPException(500, "检测到循环引用")
        v = db.one(
            "SELECT rv.version_no, r.name FROM recipe_versions rv JOIN recipes r ON r.id=rv.recipe_id WHERE rv.id=?",
            (vid,),
        )
        vlabel = f"{v['name']} v{v['version_no']}" if v else f"version {vid}"
        for it in items_of(db, vid):
            step = {"version_id": vid, "item_id": it["id"], "recipe": vlabel, "desc": _item_desc(db, it)}
            if it["kind"] == "ingredient":
                ing = db.one("SELECT name, allergens FROM ingredients WHERE id=?", (it["ingredient_id"],))
                for a in json.loads(ing["allergens"]):
                    out.setdefault(a, []).append(prefix + [step])
            else:
                rec(it["sub_version_id"], prefix + [step], stack | {vid})

    rec(version_id, [], frozenset())
    return out


def transitive_sub_versions(db, version_id):
    seen, stack = set(), [version_id]
    while stack:
        vid = stack.pop()
        for it in items_of(db, vid):
            if it["kind"] == "recipe" and it["sub_version_id"] not in seen:
                seen.add(it["sub_version_id"])
                stack.append(it["sub_version_id"])
    return seen


def transitive_batches(db, version_id):
    bids = set()
    for vid in {version_id} | transitive_sub_versions(db, version_id):
        for it in items_of(db, vid):
            if it["kind"] == "ingredient" and it["batch_id"]:
                bids.add(it["batch_id"])
    return bids


def versions_using_batch(db, batch_id):
    """不动点迭代: 直接使用该批次的版本 + 所有上级引用版本。"""
    affected = {r["version_id"] for r in db.q("SELECT version_id FROM items WHERE batch_id=?", (batch_id,))}
    frontier = set(affected)
    while frontier:
        marks = ",".join("?" * len(frontier))
        parents = {
            r["version_id"]
            for r in db.q(f"SELECT version_id FROM items WHERE sub_version_id IN ({marks})", tuple(frontier))
        }
        new = parents - affected
        affected |= new
        frontier = new
    return affected


def paths_to_batch(db, version_id, batch_id):
    results = []

    def rec(vid, prefix):
        for it in items_of(db, vid):
            step = {"version_id": vid, "item_id": it["id"], "desc": _item_desc(db, it)}
            if it["kind"] == "ingredient" and it["batch_id"] == batch_id:
                results.append(prefix + [step])
            elif it["kind"] == "recipe":
                rec(it["sub_version_id"], prefix + [step])

    rec(version_id, [])
    return results


def invalidate_confirmations(db, version_id):
    db.exec("UPDATE confirmations SET status='invalidated' WHERE version_id=? AND status='valid'", (version_id,))


def version_detail(db, vid):
    v = db.one(
        "SELECT rv.*, r.name AS recipe_name FROM recipe_versions rv JOIN recipes r ON r.id=rv.recipe_id WHERE rv.id=?",
        (vid,),
    )
    if not v:
        raise HTTPException(404, "版本不存在")
    d = dict(v)
    d["snapshot_hash"] = compute_hash(db, vid)
    d["items"] = [
        {**dict(it), "desc": _item_desc(db, it)} for it in items_of(db, vid)
    ]
    d["confirmations"] = [dict(c) for c in db.q("SELECT * FROM confirmations WHERE version_id=?", (vid,))]
    d["substitutions"] = [dict(s) for s in db.q("SELECT * FROM substitutions WHERE version_id=?", (vid,))]
    rel = db.one("SELECT * FROM releases WHERE version_id=?", (vid,))
    d["release"] = dict(rel) if rel else None
    return d


# ---------- 请求模型 ----------

class IngredientIn(BaseModel):
    name: str
    allergens: list[str] = []


class BatchIn(BaseModel):
    ingredient_id: int
    code: str


class RecipeIn(BaseModel):
    name: str


class ItemIn(BaseModel):
    kind: str  # ingredient | recipe
    ingredient_id: int | None = None
    batch_id: int | None = None
    sub_version_id: int | None = None
    qty: float = 1


class ConfirmIn(BaseModel):
    reviewer: str


class ReleaseIn(BaseModel):
    released_by: str


class SubstitutionIn(BaseModel):
    item_id: int
    new_ingredient_id: int
    new_batch_id: int | None = None
    proposer: str


class ApplyIn(BaseModel):
    expected_snapshot_hash: str


# ---------- 应用工厂 ----------

def create_app(db_path=":memory:"):
    db = DB(db_path)
    app = FastAPI(title="过敏原标签复核台")

    def get_version(vid):
        v = db.one("SELECT * FROM recipe_versions WHERE id=?", (vid,))
        if not v:
            raise HTTPException(404, "版本不存在")
        return v

    # --- 原料与批次 ---
    @app.post("/api/ingredients")
    def create_ingredient(body: IngredientIn):
        try:
            cur = db.exec(
                "INSERT INTO ingredients(name, allergens) VALUES(?,?)",
                (body.name, json.dumps(body.allergens)),
            )
        except sqlite3.IntegrityError:
            raise HTTPException(409, "原料名已存在")
        return dict(db.one("SELECT * FROM ingredients WHERE id=?", (cur.lastrowid,)))

    @app.get("/api/ingredients")
    def list_ingredients():
        return [dict(r) for r in db.q("SELECT * FROM ingredients ORDER BY id")]

    @app.post("/api/batches")
    def create_batch(body: BatchIn):
        if not db.one("SELECT id FROM ingredients WHERE id=?", (body.ingredient_id,)):
            raise HTTPException(404, "原料不存在")
        cur = db.exec("INSERT INTO batches(ingredient_id, code) VALUES(?,?)", (body.ingredient_id, body.code))
        return dict(db.one("SELECT * FROM batches WHERE id=?", (cur.lastrowid,)))

    @app.get("/api/batches")
    def list_batches():
        return [dict(r) for r in db.q("SELECT * FROM batches ORDER BY id")]

    @app.post("/api/batches/{bid}/recall")
    def recall_batch(bid: int):
        b = db.one("SELECT * FROM batches WHERE id=?", (bid,))
        if not b:
            raise HTTPException(404, "批次不存在")
        if b["status"] == "recalled":
            raise HTTPException(409, "批次已召回")
        with db.lock:
            db.exec("UPDATE batches SET status='recalled' WHERE id=?", (bid,))
            affected = versions_using_batch(db, bid)
            marks = []
            for vid in sorted(affected):
                v = get_version(vid)
                paths = paths_to_batch(db, vid, bid)
                db.exec(
                    "INSERT INTO recall_marks(version_id, batch_id, paths, created_at) VALUES(?,?,?,?)",
                    (vid, bid, json.dumps(paths), now()),
                )
                if v["status"] == "released":
                    # 不改写 releases 记录, 仅把版本标为待复核
                    db.exec("UPDATE recipe_versions SET status='re_review' WHERE id=?", (vid,))
                if v["status"] in ("pending_approval", "draft"):
                    invalidate_confirmations(db, vid)
                marks.append({"version_id": vid, "status": v["status"], "paths": paths})
        return {"batch_id": bid, "affected": marks}

    @app.get("/api/re-review")
    def re_review_list():
        rows = db.q("SELECT * FROM recall_marks ORDER BY id DESC")
        return [
            {**dict(r), "paths": json.loads(r["paths"]), "version": version_detail(db, r["version_id"])}
            for r in rows
        ]

    # --- 配方与版本 ---
    @app.post("/api/recipes")
    def create_recipe(body: RecipeIn):
        try:
            cur = db.exec("INSERT INTO recipes(name) VALUES(?)", (body.name,))
        except sqlite3.IntegrityError:
            raise HTTPException(409, "配方名已存在")
        return dict(db.one("SELECT * FROM recipes WHERE id=?", (cur.lastrowid,)))

    @app.get("/api/recipes")
    def list_recipes():
        out = []
        for r in db.q("SELECT * FROM recipes ORDER BY id"):
            vs = [version_detail(db, v["id"]) for v in db.q("SELECT id FROM recipe_versions WHERE recipe_id=? ORDER BY version_no", (r["id"],))]
            out.append({**dict(r), "versions": vs})
        return out

    @app.post("/api/recipes/{rid}/versions")
    def create_version(rid: int):
        if not db.one("SELECT id FROM recipes WHERE id=?", (rid,)):
            raise HTTPException(404, "配方不存在")
        with db.lock:
            latest = db.one(
                "SELECT * FROM recipe_versions WHERE recipe_id=? ORDER BY version_no DESC LIMIT 1", (rid,)
            )
            vno = (latest["version_no"] + 1) if latest else 1
            cur = db.exec(
                "INSERT INTO recipe_versions(recipe_id, version_no) VALUES(?,?)", (rid, vno)
            )
            vid = cur.lastrowid
            if latest:  # 继承上一版本条目, 便于迭代
                for it in items_of(db, latest["id"]):
                    db.exec(
                        "INSERT INTO items(version_id, seq, kind, ingredient_id, batch_id, sub_version_id, qty) VALUES(?,?,?,?,?,?,?)",
                        (vid, it["seq"], it["kind"], it["ingredient_id"], it["batch_id"], it["sub_version_id"], it["qty"]),
                    )
        return version_detail(db, vid)

    @app.get("/api/versions/{vid}")
    def get_version_detail(vid: int):
        return version_detail(db, vid)

    @app.post("/api/versions/{vid}/items")
    def add_item(vid: int, body: ItemIn):
        v = get_version(vid)
        if v["status"] != "draft":
            raise HTTPException(409, "仅草稿版本可编辑条目")
        if body.kind == "ingredient":
            if not body.ingredient_id or not db.one("SELECT id FROM ingredients WHERE id=?", (body.ingredient_id,)):
                raise HTTPException(404, "原料不存在")
            if body.batch_id:
                b = db.one("SELECT * FROM batches WHERE id=?", (body.batch_id,))
                if not b or b["ingredient_id"] != body.ingredient_id:
                    raise HTTPException(400, "批次与原料不匹配")
                if b["status"] != "active":
                    raise HTTPException(400, f"批次不可用({b['status']})")
        elif body.kind == "recipe":
            sv = db.one("SELECT * FROM recipe_versions WHERE id=?", (body.sub_version_id or -1,))
            if not sv:
                raise HTTPException(404, "子配方版本不存在")
            if sv["recipe_id"] == v["recipe_id"]:
                raise HTTPException(400, "不能引用自身配方(循环)")
            if sv["recipe_id"] > v["recipe_id"]:
                raise HTTPException(400, "只能引用更早创建的子配方(防止循环引用)")
        else:
            raise HTTPException(400, "未知条目类型")
        seq = (db.one("SELECT COALESCE(MAX(seq),0) AS m FROM items WHERE version_id=?", (vid,))["m"]) + 1
        cur = db.exec(
            "INSERT INTO items(version_id, seq, kind, ingredient_id, batch_id, sub_version_id, qty) VALUES(?,?,?,?,?,?,?)",
            (vid, seq, body.kind, body.ingredient_id, body.batch_id, body.sub_version_id, body.qty),
        )
        return dict(db.one("SELECT * FROM items WHERE id=?", (cur.lastrowid,)))

    @app.delete("/api/versions/{vid}/items/{item_id}")
    def delete_item(vid: int, item_id: int):
        v = get_version(vid)
        if v["status"] != "draft":
            raise HTTPException(409, "仅草稿版本可编辑条目")
        db.exec("DELETE FROM items WHERE id=? AND version_id=?", (item_id, vid))
        return {"ok": True}

    @app.post("/api/versions/{vid}/submit")
    def submit(vid: int):
        v = get_version(vid)
        if v["status"] != "draft":
            raise HTTPException(409, "仅草稿可提交审批")
        if not items_of(db, vid):
            raise HTTPException(400, "空配方不能提交")
        h = compute_hash(db, vid)
        db.exec("UPDATE recipe_versions SET status='pending_approval', snapshot_hash=? WHERE id=?", (h, vid))
        return version_detail(db, vid)

    @app.post("/api/versions/{vid}/confirm")
    def confirm(vid: int, body: ConfirmIn):
        v = get_version(vid)
        if v["status"] != "pending_approval":
            raise HTTPException(409, "版本不在待审批状态")
        h = compute_hash(db, vid)
        dup = db.one(
            "SELECT id FROM confirmations WHERE version_id=? AND reviewer=? AND status='valid'", (vid, body.reviewer)
        )
        if dup:
            raise HTTPException(409, "同一审核者不能重复确认")
        db.exec(
            "INSERT INTO confirmations(version_id, reviewer, snapshot_hash) VALUES(?,?,?)",
            (vid, body.reviewer, h),
        )
        return version_detail(db, vid)

    @app.post("/api/versions/{vid}/release")
    def release(vid: int, body: ReleaseIn):
        with db.lock:  # 审批竞争: 同一时刻只允许一个放行成功
            v = get_version(vid)
            if v["status"] != "pending_approval":
                raise HTTPException(409, f"版本状态 {v['status']} 不可放行(待复核版本须新建版本重新走审批)")
            h = compute_hash(db, vid)
            rows = db.q(
                "SELECT DISTINCT reviewer FROM confirmations WHERE version_id=? AND status='valid' AND snapshot_hash=?",
                (vid, h),
            )
            reviewers = {r["reviewer"] for r in rows}
            if len(reviewers) < 2:
                raise HTTPException(400, "放行须两名不同审核者对同一快照确认")
            for svid in transitive_sub_versions(db, vid):
                sv = get_version(svid)
                if sv["status"] != "released":
                    raise HTTPException(400, f"子配方版本 {svid} 未放行(状态 {sv['status']})")
            for bid in transitive_batches(db, vid):
                b = db.one("SELECT status FROM batches WHERE id=?", (bid,))
                if not b or b["status"] != "active":
                    raise HTTPException(400, f"批次 {bid} 不可用, 禁止放行")
            db.exec(
                "UPDATE recipe_versions SET status='superseded' WHERE recipe_id=? AND status='released'",
                (v["recipe_id"],),
            )
            db.exec("UPDATE recipe_versions SET status='released', snapshot_hash=? WHERE id=?", (h, vid))
            db.exec(
                "INSERT INTO releases(version_id, snapshot_hash, released_by, released_at) VALUES(?,?,?,?)",
                (vid, h, body.released_by, now()),
            )
        return version_detail(db, vid)

    # --- 替换原料(乐观并发) ---
    @app.post("/api/versions/{vid}/substitutions")
    def propose_substitution(vid: int, body: SubstitutionIn):
        v = get_version(vid)
        if v["status"] not in ("draft", "pending_approval"):
            raise HTTPException(409, "当前状态不可替换")
        it = db.one("SELECT * FROM items WHERE id=? AND version_id=?", (body.item_id, vid))
        if not it or it["kind"] != "ingredient":
            raise HTTPException(404, "原料条目不存在")
        if not db.one("SELECT id FROM ingredients WHERE id=?", (body.new_ingredient_id,)):
            raise HTTPException(404, "替代原料不存在")
        if body.new_batch_id:
            b = db.one("SELECT * FROM batches WHERE id=?", (body.new_batch_id,))
            if not b or b["ingredient_id"] != body.new_ingredient_id:
                raise HTTPException(400, "批次与替代原料不匹配")
            if b["status"] != "active":
                raise HTTPException(400, f"批次不可用({b['status']})")
        cur = db.exec(
            "INSERT INTO substitutions(version_id, item_id, new_ingredient_id, new_batch_id, proposer) VALUES(?,?,?,?,?)",
            (vid, body.item_id, body.new_ingredient_id, body.new_batch_id, body.proposer),
        )
        return dict(db.one("SELECT * FROM substitutions WHERE id=?", (cur.lastrowid,)))

    @app.post("/api/substitutions/{sid}/apply")
    def apply_substitution(sid: int, body: ApplyIn):
        with db.lock:
            s = db.one("SELECT * FROM substitutions WHERE id=?", (sid,))
            if not s:
                raise HTTPException(404, "替换提案不存在")
            current = compute_hash(db, s["version_id"])
            if body.expected_snapshot_hash != current:
                raise HTTPException(409, "快照已变化, 替换冲突, 请刷新后重试")
            if s["status"] != "proposed":
                raise HTTPException(409, f"提案状态 {s['status']} 不可应用")
            db.exec(
                "UPDATE items SET ingredient_id=?, batch_id=? WHERE id=?",
                (s["new_ingredient_id"], s["new_batch_id"], s["item_id"]),
            )
            db.exec("UPDATE substitutions SET status='applied' WHERE id=?", (sid,))
            db.exec(
                "UPDATE substitutions SET status='superseded' WHERE item_id=? AND status='proposed'",
                (s["item_id"],),
            )
            invalidate_confirmations(db, s["version_id"])  # 配方变化 → 既有确认失效
        return version_detail(db, s["version_id"])

    # --- 标签 / 差异 / 导出(同一服务端版本) ---
    @app.get("/api/versions/{vid}/labels")
    def labels(vid: int):
        get_version(vid)
        return {
            "version_id": vid,
            "snapshot_hash": compute_hash(db, vid),
            "labels": resolve_labels(db, vid),
        }

    @app.get("/api/versions/{vid}/diff")
    def diff(vid: int, against: int):
        get_version(vid)
        get_version(against)

        def norm(v):
            rows = {}
            for it in items_of(db, v):
                key = (it["kind"], it["ingredient_id"], it["batch_id"], it["sub_version_id"])
                rows[key] = {"desc": _item_desc(db, it), "qty": it["qty"]}
            return rows

        a, b = norm(against), norm(vid)
        added = [v for k, v in b.items() if k not in a]
        removed = [v for k, v in a.items() if k not in b]
        changed = [
            {"desc": b[k]["desc"], "from_qty": a[k]["qty"], "to_qty": b[k]["qty"]}
            for k in a.keys() & b.keys()
            if a[k]["qty"] != b[k]["qty"]
        ]
        return {"version_id": vid, "against": against, "added": added, "removed": removed, "changed": changed}

    @app.get("/api/versions/{vid}/export")
    def export(vid: int):
        d = version_detail(db, vid)
        payload = {
            "recipe": d["recipe_name"],
            "version_no": d["version_no"],
            "status": d["status"],
            "snapshot_hash": d["snapshot_hash"],
            "exported_at": now(),
            "labels": resolve_labels(db, vid),
        }
        return JSONResponse(
            payload,
            headers={"Content-Disposition": f'attachment; filename="labels_v{vid}.json"'},
        )

    @app.get("/")
    def index():
        return FileResponse("static/index.html")

    return app


app = create_app("allergen.db")

if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8000)
