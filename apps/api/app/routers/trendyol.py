"""Trendyol entegrasyon uçları (yol haritası prototipi, salt-okur sipariş importu).

POST /api/integrations/trendyol/sync   {since?, until?} -> {fetched, imported, skipped, mode, ...}
GET  /api/integrations/trendyol/status                  -> {mode, configured, last_sync, imported_total}

Hata eşlemesi: satıcı bilgileri eksik → 400 (kullanıcı/operatör düzeltir),
diğer Trendyol hataları (ağ, HTTP, bozuk gövde) → 502 (dış servis).
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from ..db import get_db
from ..schemas.trendyol import TrendyolStatusOut, TrendyolSyncIn, TrendyolSyncOut
from ..services import trendyol, trendyol_sync

log = logging.getLogger("otohesap.trendyol")

router = APIRouter(prefix="/api/integrations/trendyol", tags=["trendyol"])
DbDep = Annotated[Session, Depends(get_db)]


def _as_utc(value: datetime) -> datetime:
    return value.astimezone(UTC) if value.tzinfo else value.replace(tzinfo=UTC)


def _range(body: TrendyolSyncIn) -> tuple[datetime, datetime]:
    """İstekteki aralığı tamamlar: `until` boşsa şimdi, `since` boşsa moda göre varsayılan."""
    until = _as_utc(body.until) if body.until else datetime.now(UTC)
    since = _as_utc(body.since) if body.since else trendyol.default_since(until)
    if since >= until:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            detail="Tarih aralığı geçersiz: başlangıç, bitişten küçük olmalı.",
        )
    return since, until


def _fail(error: trendyol.TrendyolError) -> HTTPException:
    if str(error) == trendyol.MISSING_CREDENTIALS:
        return HTTPException(status.HTTP_400_BAD_REQUEST, detail=str(error))
    return HTTPException(status.HTTP_502_BAD_GATEWAY, detail=str(error))


@router.post("/sync", response_model=TrendyolSyncOut)
def sync(db: DbDep, body: TrendyolSyncIn | None = None) -> TrendyolSyncOut:
    """Aralıktaki Trendyol siparişlerini okur ve eşleşen kalemleri `sales`'e aktarır."""
    since, until = _range(body or TrendyolSyncIn())
    try:
        result = trendyol_sync.sync_orders(db, since, until)
    except trendyol.TrendyolError as e:
        log.warning("trendyol eşitleme başarısız: %s", e)
        raise _fail(e) from e
    return TrendyolSyncOut(**result)


@router.get("/status", response_model=TrendyolStatusOut)
def get_status(db: DbDep) -> TrendyolStatusOut:
    """Mod, bağlantı durumu ve şimdiye kadar aktarılan kalem sayısı."""
    last_sync, imported_total = trendyol_sync.import_stats(db)
    return TrendyolStatusOut(
        mode=trendyol.mode(),
        configured=trendyol.is_configured(),
        last_sync=last_sync,
        imported_total=imported_total,
    )
