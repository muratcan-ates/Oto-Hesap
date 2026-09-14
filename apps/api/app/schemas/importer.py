"""CSV içe aktarma şemaları — /api/import/preview ve /api/import/commit yanıtları.

Sözleşme: specs/002-csv-import/spec.md · kullanım: docs/ozellikler/csv-ice-aktarma.md
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

ImportKind = Literal["sales", "expenses"]


class ImportRowError(BaseModel):
    """Atlanan satırın gerekçesi. `row` dosyadaki satır numarası (başlık = 1)."""

    row: int
    field: str | None = None  # hedef alan adı (sold_at, qty, ...); dosya geneli ise null
    message: str


class ImportPreviewRow(BaseModel):
    """Önizleme tablosunun bir satırı. `values`: hedef alan -> ekrana yazılacak metin."""

    row: int
    ok: bool
    values: dict[str, str]
    error: str | None = None


class ImportPreviewOut(BaseModel):
    kind: ImportKind
    total_rows: int
    valid_rows: int
    mapping: dict[str, str]  # hedef alan -> kaynak sütun başlığı
    preview: list[ImportPreviewRow]
    errors: list[ImportRowError]
    warnings: list[str]
    token: str  # 10 dk geçerli; commit bu token'la gelir
    filename: str
    note: str  # "İçe aktarılan satışlar stoğu düşürmez (geçmiş veri)." vb.


class ImportCommitIn(BaseModel):
    token: str = Field(min_length=1)
    dry_run: bool = False  # true: hiçbir satır yazılmaz, token kullanılabilir kalır


class ImportCommitOut(BaseModel):
    kind: ImportKind
    inserted: int
    skipped: int
    errors: list[ImportRowError]
    dry_run: bool
