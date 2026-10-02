"""Pydantic 请求 / 响应模型。"""
from __future__ import annotations

from typing import Literal, Optional

from pydantic import BaseModel


# ---------- 基础资料 ----------
class AllergenOut(BaseModel):
    code: str
    name: str


class ReviewerOut(BaseModel):
    id: int
    name: str


class MaterialOut(BaseModel):
    id: int
    code: str
    name: str
    allergen_codes: list[str] = []


class BatchOut(BaseModel):
    id: int
    material_id: int
    code: str
    received_on: str
    expires_on: str
    status: Literal["active", "recalled", "expired"]
    recalled_reason: Optional[str] = None


# ---------- 配方组件 ----------
class ComponentIn(BaseModel):
    kind: Literal["material", "subrecipe"]
    material_id: Optional[int] = None
    batch_id: Optional[int] = None
    child_version_id: Optional[int] = None
    qty: str = ""


class ComponentOut(BaseModel):
    id: int
    position: int
    kind: Literal["material", "subrecipe"]
    material_id: Optional[int]
    material_code: Optional[str]
    material_name: Optional[str]
    batch_id: Optional[int]
    batch_code: Optional[str]
    child_version_id: Optional[int]
    child_version_label: Optional[str]
    qty: str
    material_labels: list[str]


# ---------- 版本 / 审批 ----------
class VersionCreate(BaseModel):
    recipe_id: int
    created_by: str
    note: str = ""
    expected_current_version_id: Optional[int] = None  # 并发编辑保护（乐观锁）
    components: list[ComponentIn]


class ApprovalIn(BaseModel):
    reviewer_id: int


class ApprovalOut(BaseModel):
    id: int
    reviewer_id: int
    reviewer_name: str
    decided_on: str
    stale: bool
    stale_reason: str


class VersionOut(BaseModel):
    id: int
    recipe_id: int
    version_no: int
    created_on: str
    created_by: str
    content_hash: str
    status: Literal["in_approval", "released"]
    needs_review: bool
    released_on: Optional[str]
    note: str
    is_current: bool
    components: list[ComponentOut]
    approvals: list[ApprovalOut]
    violations: list[str]
    can_release: bool
    release_blockers: list[str]


class RecipeSummary(BaseModel):
    id: int
    code: str
    name: str
    revision: int
    current_version_id: Optional[int]
    current_version_no: Optional[int]
    current_status: Optional[str]
    current_needs_review: bool


# ---------- 标签传播 ----------
class PropagationHop(BaseModel):
    depth: int
    kind: Literal["material", "subrecipe"]
    name: str
    code: str
    batch_code: Optional[str] = None
    qty: str = ""


class LabelPropagation(BaseModel):
    code: str
    name: str
    paths: list[list[PropagationHop]]


class BatchPropagation(BaseModel):
    batch_id: int
    batch_code: str
    material_code: str
    material_name: str
    status: str
    paths: list[list[PropagationHop]]


class VersionGraph(BaseModel):
    """页面标签清单 / 传播路径 / 导出共用的同一份服务端版本视图。"""
    version_id: int
    content_hash: str
    labels: list[LabelPropagation]
    batches: list[BatchPropagation]
    violations: list[str]


# ---------- 差异 ----------
class DiffEntry(BaseModel):
    change: Literal["added", "removed", "modified"]
    kind: str
    name: str
    before: Optional[str] = None
    after: Optional[str] = None
    labels_before: list[str] = []
    labels_after: list[str] = []


class VersionDiff(BaseModel):
    base_version_id: int
    target_version_id: int
    same_version: bool
    entries: list[DiffEntry]
    labels_added: list[str]
    labels_removed: list[str]


# ---------- 替换提案 ----------
class ProposalCreate(BaseModel):
    recipe_id: int
    base_version_id: int
    component_id: int
    new_material_id: int
    new_batch_id: int
    proposed_by: int
    reason: str = ""


class ProposalOut(BaseModel):
    id: int
    recipe_id: int
    base_version_id: int
    component_id: Optional[int]
    new_material_id: int
    new_material_name: str
    new_batch_id: int
    new_batch_code: str
    proposed_by: int
    proposer_name: str
    reason: str
    created_on: str
    status: Literal["open", "applied", "rejected", "superseded"]
    resulting_version_id: Optional[int] = None


# ---------- 召回 ----------
class RecallCreate(BaseModel):
    reason: str


class RecallOut(BaseModel):
    batch_id: int
    batch_code: str
    reason: str
    recalled_on: str
    affected_versions: list[int]
