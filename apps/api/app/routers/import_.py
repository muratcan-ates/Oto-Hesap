"""CSV içe aktarma uçları.

    POST /api/import/preview   multipart/form-data: file, kind=sales|expenses  -> önizleme + token
    POST /api/import/commit    {token, dry_run?}                               -> yazma sonucu
    GET  /api/import/template/sales.csv · /api/import/template/expenses.csv    -> örnek şablon

Notlar:
- İçe aktarılan satışlar **stoğu düşürmez** (geçmiş veri); arayüzde de yazılır.
- Sınırlar: dosya ≤ 5 MB (aşılırsa 413), ≤ 10.000 satır (aşılırsa 422).
- Önizleme hiçbir şey yazmaz; `token` 10 dk geçerlidir ve bir kez commit edilebilir.
- `python-multipart` bağımlılığı eklenmediği için gövde services/importer.py içinde elle çözülür.
"""

from __future__ import annotations

import logging
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import Response
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session
from starlette.concurrency import run_in_threadpool

from ..db import get_db
from ..schemas.importer import (
    ImportCommitIn,
    ImportCommitOut,
    ImportPreviewOut,
    ImportPreviewRow,
    ImportRowError,
)
from ..services import importer
from .export import csv_response, to_csv

router = APIRouter(prefix="/api", tags=["import"])

DbDep = Annotated[Session, Depends(get_db)]

log = logging.getLogger("otohesap.import")

TOO_LARGE = "Dosya 5 MB sınırını aşıyor. Dosyayı bölüp parça parça aktarın."
NO_FILE = "Dosya seçilmedi (form alanı: file)."
BAD_KIND = "Geçersiz tür; 'sales' veya 'expenses' olmalı."
NOT_MULTIPART = "Dosya multipart/form-data olarak gönderilmeli."


def _reject(exc: importer.ImportRejected) -> HTTPException:
    return HTTPException(exc.status_code, detail=exc.detail)


async def _read_body(request: Request) -> bytes:
    """Gövdeyi 5 MB sınırıyla okur (sınır aşılırsa okumayı bırakır)."""
    declared = request.headers.get("content-length", "")
    if declared.isdigit() and int(declared) > importer.MAX_UPLOAD_BYTES:
        raise HTTPException(status.HTTP_413_REQUEST_ENTITY_TOO_LARGE, detail=TOO_LARGE)
    body = bytearray()
    async for chunk in request.stream():
        body += chunk
        if len(body) > importer.MAX_UPLOAD_BYTES:
            raise HTTPException(status.HTTP_413_REQUEST_ENTITY_TOO_LARGE, detail=TOO_LARGE)
    return bytes(body)


@router.post("/import/preview", response_model=ImportPreviewOut)
async def preview_import(request: Request, db: DbDep) -> ImportPreviewOut:
    content_type = request.headers.get("content-type", "")
    if "multipart/form-data" not in content_type.lower():
        raise HTTPException(status.HTTP_415_UNSUPPORTED_MEDIA_TYPE, detail=NOT_MULTIPART)
    body = await _read_body(request)
    try:
        fields, files = importer.parse_multipart(body, content_type)
    except importer.ImportRejected as exc:
        raise _reject(exc) from exc

    upload = files.get("file")
    if upload is None or not upload.content:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, detail=NO_FILE)
    kind = fields.get("kind", "").strip().lower()
    if kind not in ("sales", "expenses"):
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, detail=BAD_KIND)
    if upload.filename.lower().endswith((".xlsx", ".xls")):
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, detail=importer.XLSX_DETAIL)

    try:
        staged = await run_in_threadpool(
            importer.build_preview, db, kind, upload.content, upload.filename
        )
    except importer.ImportRejected as exc:
        raise _reject(exc) from exc

    log.info(
        "içe aktarma önizleme: %s · %s · %d satır (%d geçerli)",
        kind,
        upload.filename or "-",
        staged.total_rows,
        staged.valid_rows,
    )
    return _preview_out(staged)


def _preview_out(staged: importer.StagedImport) -> ImportPreviewOut:
    return ImportPreviewOut(
        kind=staged.kind,
        total_rows=staged.total_rows,
        valid_rows=staged.valid_rows,
        mapping=staged.mapping,
        preview=[
            ImportPreviewRow(row=row.row, ok=row.ok, values=row.values, error=row.error)
            for row in staged.preview
        ],
        errors=[ImportRowError(row=e.row, field=e.field, message=e.message) for e in staged.errors],
        warnings=staged.warnings,
        token=staged.token,
        filename=staged.filename,
        note=importer.STOCK_NOTE if staged.kind == "sales" else "",
    )


@router.post("/import/commit", response_model=ImportCommitOut)
def commit_import(body: ImportCommitIn, db: DbDep) -> ImportCommitOut:
    try:
        staged = importer.fetch(body.token)
    except importer.ImportRejected as exc:
        raise _reject(exc) from exc

    try:
        result = importer.commit(db, staged, dry_run=body.dry_run)
    except SQLAlchemyError as exc:
        db.rollback()
        log.exception("içe aktarma yazılamadı: %s", exc)
        raise HTTPException(
            status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Kayıtlar yazılamadı; hiçbir satır eklenmedi. Lütfen tekrar deneyin.",
        ) from exc

    log.info(
        "içe aktarma %s: %s · %d eklendi · %d atlandı",
        "denemesi" if body.dry_run else "tamamlandı",
        staged.kind,
        result.inserted,
        result.skipped,
    )
    return ImportCommitOut(
        kind=staged.kind,
        inserted=result.inserted,
        skipped=result.skipped,
        errors=[ImportRowError(row=e.row, field=e.field, message=e.message) for e in result.errors],
        dry_run=body.dry_run,
    )


def _template(kind: str, filename: str) -> Response:
    header, rows = importer.TEMPLATES[kind]
    return csv_response(filename, to_csv(header, rows))


@router.get("/import/template/sales.csv")
def sales_template() -> Response:
    return _template("sales", "satis-sablonu.csv")


@router.get("/import/template/expenses.csv")
def expenses_template() -> Response:
    return _template("expenses", "gider-sablonu.csv")
