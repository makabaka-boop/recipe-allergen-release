# 过敏原标签复核台

FastAPI + SQLite 后端,React 复核台前端(由后端托管,无需前端构建)。

## 运行

```bash
pip install -r requirements.txt   # 或: python3 -m pip install fastapi "uvicorn[standard]" pytest httpx
python3 app.py                    # http://localhost:8000
```

前端页面通过 CDN 加载 React(需浏览器可访问 unpkg);所有标签、差异、导出数据均由服务端同一版本计算。

## 测试

```bash
python3 -m pytest tests/ -q
```

## 领域规则

| 规则 | 实现 |
|---|---|
| 配方引用原料(可绑批次)或**更早创建**的子配方版本 | `POST /api/versions/{id}/items` 校验引用方向,结构上保证无环 |
| 不可变快照 | 快照哈希递归覆盖条目、绑定批次状态、子版本哈希(`compute_hash`) |
| 双人放行 | 两名不同审核者对**同一快照哈希**确认;期间配方/批次变化 → 旧确认置 `invalidated` |
| 替换原料 | 提案 + 乐观并发应用(`expected_snapshot_hash` 不符 → 409 冲突) |
| 批次召回 | 不改写 `releases` 记录;受影响版本标 `re_review` 并在 `recall_marks` 记录传播路径;须新建版本重新审批放行 |
| 失效批次 | 绑定与放行两道关口均拒绝非 `active` 批次 |
| 审批竞争 | 放行在数据库锁内完成状态检查与写入,并发放行只有一个成功 |

## 主要接口

- `POST /api/ingredients` `POST /api/batches` `POST /api/batches/{id}/recall`
- `POST /api/recipes` `POST /api/recipes/{id}/versions`(继承上一版本条目)
- `POST /api/versions/{id}/items` `submit` `confirm` `release`
- `POST /api/versions/{id}/substitutions` `POST /api/substitutions/{id}/apply`
- `GET /api/versions/{id}/labels` `diff?against=` `export`(同源数据)
- `GET /api/re-review`(召回传播路径)
