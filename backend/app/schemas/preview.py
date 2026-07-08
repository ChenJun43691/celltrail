# backend/app/schemas/preview.py
"""
Preview API response / request schemas（P9 Phase 2A.3）。

讓 preview endpoints 不再回裸 dict，OpenAPI 可見 contract，並與 frontend allowlist 對齊。
features 保持 List[dict]（GeoJSON Feature）以免與 DB model 綁死；response 絕不含 _records。
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field


class PreviewCreateResponse(BaseModel):
    preview_id: str
    features: List[Dict[str, Any]]
    total: int
    plotted: int
    skipped: int
    parser_type: str
    expires_at: str


class PreviewReadResponse(BaseModel):
    features: List[Dict[str, Any]]
    total: int
    plotted: int
    skipped: int


class PreviewSealResponse(BaseModel):
    ok: bool = True


class PreviewSaveRequest(BaseModel):
    project_id: str
    target_id: str = ""


class PreviewSaveResponse(BaseModel):
    ok: bool = True
    evidence_id: int
    sha256_full: str
    total: int
    inserted: int
    skipped: int


class PreviewDeleteResponse(BaseModel):
    ok: bool = True


# ── 統一 error contract（OpenAPI 可見）──
class ErrorDetail(BaseModel):
    code: str = Field(..., description="machine-readable error code, e.g. PREVIEW_EXPIRED")
    message: str = Field(..., description="使用者可讀訊息（可中文）")
    details: Dict[str, Any] = Field(default_factory=dict, description="非敏感補充資訊")


class ErrorResponse(BaseModel):
    error: ErrorDetail
    request_id: Optional[str] = None
