"""Trendyol entegrasyon uçlarının Pydantic v2 şemaları (yol haritası prototipi)."""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, field_validator

from .core import to_utc

TrendyolMode = Literal["mock", "live"]


class TrendyolSyncIn(BaseModel):
    """Eşitleme isteği. Boş bırakılırsa varsayılan aralık kullanılır (bkz. router)."""

    since: datetime | None = None
    until: datetime | None = None


class TrendyolSkippedOut(BaseModel):
    """Atlanan kalem. `reason` ∈ {urun_eslesmedi, mukerrer, iptal_edilmis, gecersiz_kalem}."""

    reason: str
    external_id: str
    detail: str | None = None  # ürün adı ya da sipariş numarası (arayüzde listelenir)


class TrendyolSyncOut(BaseModel):
    mode: TrendyolMode
    fetched: int  # okunan sipariş (paket) sayısı
    fetched_lines: int  # okunan kalem sayısı
    imported: int  # `sales`'e yazılan yeni satır sayısı
    skipped: list[TrendyolSkippedOut]
    since: datetime
    until: datetime

    @field_validator("since", "until")
    @classmethod
    def _utc(cls, value: datetime) -> datetime:
        return to_utc(value)


class TrendyolStatusOut(BaseModel):
    mode: TrendyolMode
    configured: bool  # canlı çağrı için satıcı bilgileri tam mı
    last_sync: datetime | None
    imported_total: int

    @field_validator("last_sync")
    @classmethod
    def _utc(cls, value: datetime | None) -> datetime | None:
        return to_utc(value) if value is not None else None
