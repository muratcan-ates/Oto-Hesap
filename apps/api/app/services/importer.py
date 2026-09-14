"""CSV içe aktarma çekirdeği: ayrıştırma, sütun eşleme, doğrulama, geçici önizleme deposu.

Yeni bağımlılık yok — yalnız stdlib. `python-multipart` kurulu olmadığı için multipart/form-data
gövdesi burada elle ayrıştırılır (`parse_multipart`). XLSX kapsam dışıdır; ayrıntı ve sınırlar:
`docs/ozellikler/csv-ice-aktarma.md`.

İçe aktarılan satışlar **stoğu düşürmez** (geçmiş veri aktarımı; AGENTS.md §5 D17 mantığıyla aynı
dürüstlük ilkesi: yapılmayan iş yapılmış gibi gösterilmez).
"""

from __future__ import annotations

import csv
import io
import re
import secrets
import threading
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Literal

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import Expense, Product, Sale

Kind = Literal["sales", "expenses"]

# --- sınırlar -------------------------------------------------------------------------

MAX_UPLOAD_BYTES = 5 * 1024 * 1024  # 5 MB
MAX_ROWS = 10_000
TOKEN_TTL = timedelta(minutes=10)
MAX_STAGED = 32  # bellek içi önizleme sayısı (en eskisi düşer)
MAX_REPORTED_ERRORS = 100
PREVIEW_ROWS = 20

STOCK_NOTE = "İçe aktarılan satışlar stoğu düşürmez (geçmiş veri)."

# --- hata tipi ------------------------------------------------------------------------


class ImportRejected(Exception):
    """Kullanıcıya dönecek 4xx; `detail` Türkçe (AGENTS.md §6)."""

    def __init__(self, status_code: int, detail: str) -> None:
        super().__init__(detail)
        self.status_code = status_code
        self.detail = detail


# --- metin normalleştirme -------------------------------------------------------------

_TR_LOWER = str.maketrans({"İ": "i", "I": "ı", "Ş": "ş", "Ğ": "ğ", "Ü": "ü", "Ö": "ö", "Ç": "ç"})
_FOLD = str.maketrans({"ı": "i", "ş": "s", "ğ": "g", "ü": "u", "ö": "o", "ç": "c", "â": "a"})
_SEPARATORS = re.compile(r"[\s_\-./]+")


def lower_tr(text: str) -> str:
    """Türkçe büyük/küçük harf: 'İSTANBUL' -> 'istanbul', 'IŞIK' -> 'ışık'."""
    return text.translate(_TR_LOWER).lower()


def normalize_header(text: str) -> str:
    """'Birim Fiyat' / 'unit_price' / 'Ürün Adı' -> 'birim fiyat' / 'unit price' / 'urun adi'."""
    folded = lower_tr(text.strip().lstrip("﻿")).translate(_FOLD)
    return _SEPARATORS.sub(" ", folded).strip()


def normalize_name(text: str) -> str:
    """Ürün adı eşleştirme anahtarı: büyük/küçük harf ve boşluk farkını siler."""
    return re.sub(r"\s+", " ", lower_tr(text).strip())


# --- sütun sözlüğü --------------------------------------------------------------------

FIELDS: dict[str, tuple[str, ...]] = {
    "sales": ("sold_at", "product", "qty", "unit_price", "total", "channel"),
    "expenses": ("spent_at", "category", "amount", "vendor", "note"),
}

REQUIRED: dict[str, tuple[str, ...]] = {
    "sales": ("sold_at", "product", "qty"),
    "expenses": ("spent_at", "category", "amount"),
}

FIELD_LABELS: dict[str, str] = {
    "sold_at": "Tarih",
    "product": "Ürün",
    "qty": "Adet",
    "unit_price": "Birim fiyat",
    "total": "Toplam",
    "channel": "Kanal",
    "spent_at": "Tarih",
    "category": "Kategori",
    "amount": "Tutar",
    "vendor": "Tedarikçi",
    "note": "Not",
}

_ALIASES: dict[str, dict[str, tuple[str, ...]]] = {
    "sales": {
        "sold_at": ("tarih", "date", "sold at", "satış tarihi", "işlem tarihi", "tarih saat"),
        "product": ("ürün", "urun", "product", "product name", "ürün adı", "ürün ismi"),
        "qty": ("adet", "miktar", "qty", "quantity"),
        "unit_price": ("birim fiyat", "unit price", "fiyat", "price"),
        "total": ("toplam", "total", "tutar", "toplam tutar"),
        "channel": ("kanal", "channel", "satış kanalı"),
    },
    "expenses": {
        "spent_at": ("tarih", "date", "spent at", "gider tarihi", "harcama tarihi"),
        "category": ("kategori", "category", "gider türü", "tür"),
        "amount": ("tutar", "amount", "miktar", "gider tutarı"),
        "vendor": ("tedarikçi", "tedarikci", "vendor", "satıcı", "firma", "supplier"),
        "note": ("not", "note", "açıklama", "aciklama", "description", "detay"),
    },
}

# normalize edilmiş takma ad -> hedef alan
ALIAS_INDEX: dict[str, dict[str, str]] = {
    kind: {normalize_header(alias): target for target, aliases in spec.items() for alias in aliases}
    for kind, spec in _ALIASES.items()
}

CHANNELS: dict[str, str] = {
    "magaza": "magaza",
    "mağaza": "magaza",
    "dükkan": "magaza",
    "store": "magaza",
    "offline": "magaza",
    "online": "online",
    "internet": "online",
    "web": "online",
    "e-ticaret": "online",
}

# Bilinen gider kategorileri (AGENTS.md §5); yazım farkları tek biçime indirgenir.
CATEGORY_ALIASES: dict[str, str] = {
    "kira": "kira",
    "maas": "maas",
    "maaş": "maas",
    "personel": "maas",
    "elektrik": "elektrik",
    "kargo": "kargo",
    "reklam": "reklam",
    "tedarik": "tedarik",
}


# --- sayı / tarih ayrıştırma ----------------------------------------------------------

_NUMBER_NOISE = re.compile(r"[^\d,.+-]")
_CENTS = Decimal("0.01")


def parse_decimal(raw: str) -> Decimal | None:
    """'1.234,50' · '1,234.50' · '120,00' · '₺ 90.5' -> Decimal; anlaşılmazsa None.

    Tek ayraç varsa ondalık kabul edilir (TR dosyalarda yaygın); birden çoksa binlik ayracıdır.
    """
    text = _NUMBER_NOISE.sub("", raw.strip())
    if not text:
        return None
    negative = text.startswith("-")
    text = text.lstrip("+-")
    if not text or not any(ch.isdigit() for ch in text):
        return None
    dots, commas = text.count("."), text.count(",")
    if dots and commas:
        decimal_sep = "." if text.rfind(".") > text.rfind(",") else ","
        text = text.replace("," if decimal_sep == "." else ".", "").replace(decimal_sep, ".")
    elif commas:
        text = text.replace(",", "") if commas > 1 else text.replace(",", ".")
    elif dots > 1:
        text = text.replace(".", "")
    try:
        value = Decimal(text)
    except InvalidOperation:
        return None
    return -value if negative else value


def parse_money(raw: str) -> Decimal | None:
    value = parse_decimal(raw)
    return None if value is None else value.quantize(_CENTS)


def parse_int(raw: str) -> int | None:
    value = parse_decimal(raw)
    if value is None or value != value.to_integral_value():
        return None
    return int(value)


_DMY = re.compile(r"^(\d{1,2})[./](\d{1,2})[./](\d{4})$")
_TIME = re.compile(r"^(\d{1,2}):(\d{2})(?::(\d{2}))?$")


def parse_datetime(raw: str) -> datetime | None:
    """`YYYY-MM-DD`, `DD.MM.YYYY`, `DD/MM/YYYY` (+ opsiyonel saat). Belirsizse None.

    Saat dilimi yoksa UTC varsayılır (AGENTS.md §6: tarihler ISO 8601 UTC).
    """
    text = raw.strip().replace("T", " ")
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        parsed = None
    if parsed is None:
        head, _, tail = text.partition(" ")
        match = _DMY.match(head)
        if match is None:
            return None
        day, month, year = (int(g) for g in match.groups())
        hour = minute = second = 0
        if tail.strip():
            clock = _TIME.match(tail.strip())
            if clock is None:
                return None
            hour, minute = int(clock.group(1)), int(clock.group(2))
            second = int(clock.group(3) or 0)
        try:
            parsed = datetime(year, month, day, hour, minute, second)
        except ValueError:
            return None
    return parsed.astimezone(UTC) if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def fmt_datetime(value: datetime) -> str:
    return value.astimezone(UTC).strftime("%d.%m.%Y %H:%M")


def fmt_money(value: Decimal) -> str:
    return f"{value:.2f}".replace(".", ",")


# --- multipart/form-data ayrıştırma ---------------------------------------------------


@dataclass(slots=True)
class UploadedFile:
    filename: str
    content: bytes


_BOUNDARY = re.compile(r"boundary=\"?([^\";]+)\"?", re.IGNORECASE)
_PART_NAME = re.compile(r'name="([^"]*)"', re.IGNORECASE)
_PART_FILENAME = re.compile(r'filename="([^"]*)"', re.IGNORECASE)


def parse_multipart(body: bytes, content_type: str) -> tuple[dict[str, str], dict[str, UploadedFile]]:
    """Elle multipart/form-data çözer (python-multipart bağımlılığı eklenmedi)."""
    match = _BOUNDARY.search(content_type)
    if match is None:
        raise ImportRejected(400, "Dosya sınırı (boundary) okunamadı; formu yeniden gönderin.")
    separator = b"--" + match.group(1).encode("utf-8")
    fields: dict[str, str] = {}
    files: dict[str, UploadedFile] = {}
    for chunk in body.split(separator)[1:]:
        if chunk[:2] == b"--":  # kapanış sınırı
            break
        chunk = chunk[2:] if chunk[:2] == b"\r\n" else chunk.lstrip(b"\n")
        if chunk.endswith(b"\r\n"):
            chunk = chunk[:-2]
        head, sep, data = chunk.partition(b"\r\n\r\n")
        if not sep:
            continue
        headers = head.decode("utf-8", "replace")
        name_match = _PART_NAME.search(headers)
        if name_match is None:
            continue
        name = name_match.group(1)
        file_match = _PART_FILENAME.search(headers)
        if file_match is not None:
            files[name] = UploadedFile(filename=file_match.group(1), content=data)
        else:
            fields[name] = data.decode("utf-8", "replace").strip()
    return fields, files


# --- dosya çözme ve sütun eşleme ------------------------------------------------------

XLSX_SIGNATURE = b"PK\x03\x04"
XLSX_DETAIL = (
    "XLSX dosyaları bu sürümde desteklenmiyor. Excel'de 'Farklı Kaydet → CSV UTF-8' ile "
    "kaydedip tekrar deneyin."
)


def decode_csv(raw: bytes) -> tuple[str, list[str]]:
    """UTF-8 / UTF-8-BOM; olmazsa Windows-1254 (Excel TR) yedeği + uyarı."""
    if raw.startswith(XLSX_SIGNATURE):
        raise ImportRejected(422, XLSX_DETAIL)
    warnings: list[str] = []
    try:
        return raw.decode("utf-8-sig"), warnings
    except UnicodeDecodeError:
        pass
    try:
        text = raw.decode("cp1254")
    except UnicodeDecodeError as exc:
        raise ImportRejected(
            422, "Dosya kodlaması okunamadı; UTF-8 olarak kaydedip tekrar deneyin."
        ) from exc
    warnings.append("Dosya UTF-8 değil; Windows-1254 (Türkçe) olarak okundu.")
    return text, warnings


def detect_delimiter(header_line: str) -> str:
    """Başlık satırındaki en sık ayraç: `;`, `,` veya sekme."""
    counts = {sep: header_line.count(sep) for sep in (";", ",", "\t")}
    best = max(counts, key=lambda sep: counts[sep])
    return best if counts[best] > 0 else ";"


def map_columns(
    kind: Kind, header: list[str]
) -> tuple[dict[str, str], dict[str, int], list[str]]:
    """Başlık satırı -> ({hedef alan: kaynak sütun}, {hedef alan: sütun sırası}, uyarılar)."""
    index = ALIAS_INDEX[kind]
    mapping: dict[str, str] = {}
    positions: dict[str, int] = {}
    warnings: list[str] = []
    ignored: list[str] = []
    for position, column in enumerate(header):
        target = index.get(normalize_header(column))
        if target is None:
            if column.strip():
                ignored.append(column.strip())
            continue
        if target in mapping:
            warnings.append(
                f"'{column.strip()}' sütunu '{mapping[target]}' ile aynı alana denk geliyor; "
                "ilki kullanıldı."
            )
            continue
        mapping[target] = column.strip()
        positions[target] = position
    if ignored:
        warnings.append("Yok sayılan sütunlar: " + ", ".join(ignored[:8]))
    missing = [f for f in REQUIRED[kind] if f not in mapping]
    if kind == "sales" and "unit_price" not in mapping and "total" not in mapping:
        missing.append("unit_price")
    if missing:
        labels = ", ".join(FIELD_LABELS[f] for f in dict.fromkeys(missing))
        raise ImportRejected(
            422,
            f"Başlık satırı tanınmadı; eksik sütun(lar): {labels}. "
            "Örnek şablonu indirip sütun adlarını karşılaştırın.",
        )
    # sözleşmedeki sıra korunsun (arayüz tabloyu bu sırayla çizer)
    ordered = {f: mapping[f] for f in FIELDS[kind] if f in mapping}
    return ordered, positions, warnings


# --- ayrıştırılmış satırlar -----------------------------------------------------------


@dataclass(slots=True)
class RowError:
    row: int
    field: str | None
    message: str


@dataclass(slots=True)
class ParsedRow:
    row: int
    ok: bool
    values: dict[str, str]
    payload: dict[str, object] = field(default_factory=dict)
    error: str | None = None


@dataclass(slots=True)
class StagedImport:
    token: str
    kind: Kind
    filename: str
    mapping: dict[str, str]
    rows: list[ParsedRow]  # yalnız geçerli satırlar (commit bunları yazar)
    preview: list[ParsedRow]  # ilk 20 satır (geçerli + hatalı, dosyadaki sırayla)
    errors: list[RowError]
    warnings: list[str]
    total_rows: int
    invalid_rows: int
    created_at: datetime
    used_at: datetime | None = None

    @property
    def valid_rows(self) -> int:
        return self.total_rows - self.invalid_rows


# --- ürün dizini ----------------------------------------------------------------------


@dataclass(slots=True)
class ProductIndex:
    exact: dict[str, int]
    normalized: dict[str, int]
    ambiguous: set[str]

    def find(self, name: str) -> tuple[int | None, str | None]:
        """(product_id, hata mesajı). Sessizce ürün OLUŞTURULMAZ."""
        hit = self.exact.get(name) or self.exact.get(name.strip())
        if hit is not None:
            return hit, None
        key = normalize_name(name)
        if key in self.ambiguous:
            return None, f"Birden fazla ürün '{name}' adıyla eşleşti; adı netleştirin."
        hit = self.normalized.get(key)
        if hit is not None:
            return hit, None
        return None, f"Ürün bulunamadı: '{name}'. Ürünü önce Stok ekranından ekleyin."


def build_product_index(db: Session) -> ProductIndex:
    exact: dict[str, int] = {}
    normalized: dict[str, int] = {}
    ambiguous: set[str] = set()
    for product_id, name in db.execute(select(Product.id, Product.name)).all():
        exact.setdefault(name, product_id)
        key = normalize_name(name)
        if key in normalized and normalized[key] != product_id:
            ambiguous.add(key)
        else:
            normalized[key] = product_id
    return ProductIndex(exact=exact, normalized=normalized, ambiguous=ambiguous)


# --- satır doğrulama ------------------------------------------------------------------


def _cell(record: list[str], positions: dict[str, int], target: str) -> str:
    position = positions.get(target)
    if position is None or position >= len(record):
        return ""
    return record[position].strip()


def _parse_sale_row(
    raw: dict[str, str], row: int, index: ProductIndex
) -> tuple[dict[str, object] | None, list[RowError]]:
    errors: list[RowError] = []
    sold_at = parse_datetime(raw.get("sold_at", ""))
    if sold_at is None:
        errors.append(
            RowError(row, "sold_at", "Tarih anlaşılmadı (örn. 2026-04-01 veya 01.04.2026).")
        )
    name = raw.get("product", "").strip()
    product_id: int | None = None
    if not name:
        errors.append(RowError(row, "product", "Ürün adı boş."))
    else:
        product_id, message = index.find(name)
        if message is not None:
            errors.append(RowError(row, "product", message))
    qty = parse_int(raw.get("qty", ""))
    if qty is None:
        errors.append(RowError(row, "qty", "Adet bir tam sayı olmalı."))
    elif qty <= 0:
        errors.append(RowError(row, "qty", "Adet 0'dan büyük olmalı."))
        qty = None
    price_raw, total_raw = raw.get("unit_price", "").strip(), raw.get("total", "").strip()
    unit_price = parse_money(price_raw) if price_raw else None
    total = parse_money(total_raw) if total_raw else None
    if price_raw and unit_price is None:
        errors.append(RowError(row, "unit_price", "Birim fiyat sayı olarak okunamadı."))
    elif total_raw and total is None:
        errors.append(RowError(row, "total", "Toplam sayı olarak okunamadı."))
    elif unit_price is None and total is None:
        errors.append(RowError(row, "unit_price", "Birim fiyat veya toplam gerekli."))
    elif unit_price is None and qty:
        unit_price = (total / qty).quantize(_CENTS)  # type: ignore[union-attr]
    if unit_price is not None and unit_price < 0:
        errors.append(RowError(row, "unit_price", "Birim fiyat negatif olamaz."))
    channel_raw = raw.get("channel", "").strip()
    channel = CHANNELS.get(lower_tr(channel_raw), "") if channel_raw else "magaza"
    if not channel:
        channel = "magaza"
        errors.append(
            RowError(row, "channel", f"Kanal tanınmadı: '{channel_raw}' (magaza veya online).")
        )
    if errors or sold_at is None or qty is None or unit_price is None or product_id is None:
        return None, errors
    return {
        "sold_at": sold_at,
        "product_id": product_id,
        "product": name,
        "qty": qty,
        "unit_price": unit_price,
        "total": (unit_price * qty).quantize(_CENTS),
        "channel": channel,
    }, errors


def _parse_expense_row(
    raw: dict[str, str], row: int
) -> tuple[dict[str, object] | None, list[RowError]]:
    errors: list[RowError] = []
    spent_at = parse_datetime(raw.get("spent_at", ""))
    if spent_at is None:
        errors.append(
            RowError(row, "spent_at", "Tarih anlaşılmadı (örn. 2026-04-01 veya 01.04.2026).")
        )
    category_raw = raw.get("category", "").strip()
    category = CATEGORY_ALIASES.get(lower_tr(category_raw), lower_tr(category_raw))
    if not category:
        errors.append(RowError(row, "category", "Kategori boş."))
    amount = parse_money(raw.get("amount", ""))
    if amount is None:
        errors.append(RowError(row, "amount", "Tutar sayı olmalı (örn. 1.250,00)."))
    elif amount <= 0:
        errors.append(RowError(row, "amount", "Tutar 0'dan büyük olmalı."))
        amount = None
    if errors or spent_at is None or amount is None:
        return None, errors
    return {
        "spent_at": spent_at,
        "category": category,
        "amount": amount,
        "vendor": raw.get("vendor", "").strip() or None,
        "note": raw.get("note", "").strip() or None,
    }, errors


def _display(kind: Kind, payload: dict[str, object]) -> dict[str, str]:
    if kind == "sales":
        return {
            "sold_at": fmt_datetime(payload["sold_at"]),  # type: ignore[arg-type]
            "product": str(payload["product"]),
            "qty": str(payload["qty"]),
            "unit_price": fmt_money(payload["unit_price"]),  # type: ignore[arg-type]
            "total": fmt_money(payload["total"]),  # type: ignore[arg-type]
            "channel": str(payload["channel"]),
        }
    return {
        "spent_at": fmt_datetime(payload["spent_at"]),  # type: ignore[arg-type]
        "category": str(payload["category"]),
        "amount": fmt_money(payload["amount"]),  # type: ignore[arg-type]
        "vendor": str(payload["vendor"] or ""),
        "note": str(payload["note"] or ""),
    }


# --- önizleme -------------------------------------------------------------------------


def build_preview(db: Session, kind: Kind, raw: bytes, filename: str) -> StagedImport:
    """Dosyayı ayrıştırır, doğrular ve geçici depoya koyar. Hiçbir şey yazılmaz."""
    text, warnings = decode_csv(raw)
    stripped = text.strip()
    if not stripped:
        raise ImportRejected(422, "Dosya boş.")
    first_line = next((line for line in text.splitlines() if line.strip()), "")
    reader = csv.reader(io.StringIO(text), delimiter=detect_delimiter(first_line))
    header: list[str] = []
    for record in reader:
        if any(cell.strip() for cell in record):
            header = record
            break
    mapping, positions, map_warnings = map_columns(kind, header)
    warnings.extend(map_warnings)

    index = build_product_index(db) if kind == "sales" else ProductIndex({}, {}, set())
    rows: list[ParsedRow] = []
    errors: list[RowError] = []
    total = 0
    invalid = 0
    mismatched = 0
    unknown_categories: set[str] = set()

    for record in reader:
        if not any(cell.strip() for cell in record):
            continue
        total += 1
        if total > MAX_ROWS:
            limit = f"{MAX_ROWS:,}".replace(",", ".")
            raise ImportRejected(
                422,
                f"Dosyada {limit} satırdan fazla kayıt var. Dosyayı bölüp parça parça aktarın.",
            )
        line = reader.line_num
        raw_cells = {target: _cell(record, positions, target) for target in mapping}
        if kind == "sales":
            payload, row_errors = _parse_sale_row(raw_cells, line, index)
            if payload is not None and raw_cells.get("total"):
                given = parse_money(raw_cells["total"])
                if given is not None and given != payload["total"]:
                    mismatched += 1
        else:
            payload, row_errors = _parse_expense_row(raw_cells, line)
            if payload is not None and payload["category"] not in CATEGORY_ALIASES:
                unknown_categories.add(str(payload["category"]))
        if payload is None:
            invalid += 1
            if len(errors) < MAX_REPORTED_ERRORS:
                errors.extend(row_errors)
            if len(rows) < PREVIEW_ROWS:
                rows.append(
                    ParsedRow(
                        row=line,
                        ok=False,
                        values=raw_cells,
                        error=row_errors[0].message if row_errors else "Satır okunamadı.",
                    )
                )
            continue
        rows.append(ParsedRow(row=line, ok=True, values=_display(kind, payload), payload=payload))

    if total == 0:
        raise ImportRejected(422, "Dosyada başlık satırından sonra kayıt yok.")
    if unknown_categories:
        sample = ", ".join(sorted(unknown_categories)[:5])
        warnings.append(f"Bilinen listede olmayan kategoriler olduğu gibi kaydedilecek: {sample}")
    if mismatched:
        warnings.append(
            f"{mismatched} satırda toplam sütunu birim fiyat × adet ile uyuşmuyor; "
            "toplam yeniden hesaplandı."
        )
    if invalid:
        warnings.append(f"{invalid} satır hatalı; aktarımda atlanacak.")
    if len(errors) >= MAX_REPORTED_ERRORS:
        warnings.append(f"Hata listesi ilk {MAX_REPORTED_ERRORS} kayıtla sınırlandı.")

    staged = StagedImport(
        token=secrets.token_urlsafe(16),
        kind=kind,
        filename=filename,
        mapping=mapping,
        rows=[row for row in rows if row.ok],
        preview=rows[:PREVIEW_ROWS],
        errors=errors[:MAX_REPORTED_ERRORS],
        warnings=warnings,
        total_rows=total,
        invalid_rows=invalid,
        created_at=datetime.now(UTC),
    )
    stage(staged)
    return staged


# --- geçici depo ----------------------------------------------------------------------

STAGED: dict[str, StagedImport] = {}
_LOCK = threading.Lock()


def _purge(now: datetime) -> None:
    for token, item in list(STAGED.items()):
        if now - item.created_at > TOKEN_TTL:
            del STAGED[token]


def stage(item: StagedImport) -> None:
    with _LOCK:
        _purge(item.created_at)
        while len(STAGED) >= MAX_STAGED:
            STAGED.pop(next(iter(STAGED)))
        STAGED[item.token] = item


def fetch(token: str) -> StagedImport:
    now = datetime.now(UTC)
    with _LOCK:
        _purge(now)
        item = STAGED.get(token)
    if item is None:
        raise ImportRejected(
            404, "Önizleme bulunamadı veya süresi doldu (10 dk). Dosyayı yeniden yükleyin."
        )
    if item.used_at is not None:
        raise ImportRejected(409, "Bu önizleme zaten içe aktarıldı.")
    return item


def reset_store() -> None:
    """Testler için."""
    with _LOCK:
        STAGED.clear()


# --- yazma ----------------------------------------------------------------------------


@dataclass(slots=True)
class CommitResult:
    inserted: int
    skipped: int
    errors: list[RowError]


def commit(db: Session, staged: StagedImport, dry_run: bool = False) -> CommitResult:
    """Geçerli satırları TEK transaction'da yazar. Satış içe aktarımı stoğu düşürmez."""
    errors: list[RowError] = list(staged.errors)
    skipped = staged.invalid_rows
    inserted = 0
    index = build_product_index(db) if staged.kind == "sales" else None

    for row in staged.rows:
        payload = row.payload
        if staged.kind == "sales":
            assert index is not None
            product_id, message = index.find(str(payload["product"]))
            if product_id is None:
                errors.append(RowError(row.row, "product", message or "Ürün bulunamadı."))
                skipped += 1
                continue
            db.add(
                Sale(
                    sold_at=payload["sold_at"],
                    product_id=product_id,
                    qty=payload["qty"],
                    unit_price=payload["unit_price"],
                    total=payload["total"],
                    channel=payload["channel"],
                )
            )
        else:
            db.add(
                Expense(
                    spent_at=payload["spent_at"],
                    category=payload["category"],
                    amount=payload["amount"],
                    vendor=payload["vendor"],
                    note=payload["note"],
                )
            )
        inserted += 1

    if dry_run:
        db.rollback()  # deneme: hiçbir satır yazılmaz, token kullanılabilir kalır
    else:
        db.commit()
        staged.used_at = datetime.now(UTC)
    return CommitResult(
        inserted=inserted, skipped=skipped, errors=errors[:MAX_REPORTED_ERRORS]
    )


# --- örnek şablonlar ------------------------------------------------------------------

TEMPLATES: dict[str, tuple[tuple[str, ...], tuple[tuple[str, ...], ...]]] = {
    "sales": (
        ("Tarih", "Ürün", "Adet", "Birim Fiyat", "Toplam", "Kanal"),
        (
            ("01.04.2026", "USB-C Kablo", "2", "120,00", "240,00", "magaza"),
            ("02.04.2026 14:30", "Powerbank 10000", "1", "900,00", "900,00", "online"),
        ),
    ),
    "expenses": (
        ("Tarih", "Kategori", "Tutar", "Tedarikçi", "Not"),
        (
            ("01.04.2026", "kira", "3000,00", "Emlak Ofisi", "Nisan kirası"),
            ("05.04.2026", "reklam", "500,00", "Meta Ads", ""),
        ),
    ),
}
