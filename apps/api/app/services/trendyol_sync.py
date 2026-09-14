"""Trendyol sipariş kalemlerini OtoHesap `sales` tablosuna aktarır (salt-okur import).

Kurallar:

* **Ürün oluşturulmaz.** Eşleşme `products.name` üzerinden normalize edilmiş birebir eşleşmedir
  (büyük/küçük harf, fazla boşluk ve noktalama farkları yok sayılır). Eşleşmeyen kalem atlanır ve
  `skipped` listesinde adıyla raporlanır — jüriye "uydurma ürün" gösterilmez.
* **Mükerrerlik koruması DB düzeyindedir.** Her kalem `external_orders.external_id`
  (``trendyol:<orderNumber>:<lineId>``) ile tekil; aynı eşitleme iki kez koşarsa ikinci seferde
  hiçbir yeni `sales` satırı yazılmaz (`docs/schema.sql`).
* Aktarılan satış `channel='online'` olur; `sold_at` = Trendyol sipariş anı (UTC).
* Satış stoktan düşülür (yol haritası maddesi: pazar yeri satışı stoğu kritiğe indirsin). Stok
  eksiye düşmez; sayım farkı olursa 0'da durur ve loglanır.

Kapsam dışı: iade/iptal geri alma, indirim ve komisyon alanları, kargo, stok/fiyat gönderimi.
"""

from __future__ import annotations

import logging
import re
import unicodedata
from datetime import datetime
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ..models import ExternalOrder, Product, Sale
from . import trendyol
from .trendyol import SOURCE, TrendyolOrder, TrendyolOrderLine

log = logging.getLogger("otohesap.trendyol")

CHANNEL = "online"

# `skipped[].reason` kodları — arayüz bunları Türkçe metne çevirir (SettingsView).
REASON_NO_PRODUCT = "urun_eslesmedi"
REASON_DUPLICATE = "mukerrer"
REASON_CANCELLED = "iptal_edilmis"
REASON_INVALID = "gecersiz_kalem"

#: Satış sayılmayan paket/kalem durumları (küçük harfle karşılaştırılır).
#: `Returned` / `UnDelivered` **atlanmaz**: satış gerçekleşmiştir; iade akışı kapsam dışıdır.
NON_SALE_STATUSES = frozenset({"cancelled", "unsupplied"})

# Türkçe büyük/küçük harf tuzağı: "İ".lower() birleşik işaret üretir, "I".lower() → "i".
# Her iki tarafa da aynı dönüşüm uygulandığı için eşleşme tutarlıdır.
_TR_LOWER = str.maketrans({"İ": "i", "I": "ı", "Â": "a", "Î": "i", "Û": "u"})
_NON_WORD = re.compile(r"[^0-9a-zçğıöşü]+")


def normalize_name(name: str) -> str:
    """Ürün adı eşleşme anahtarı: 'USB-C  Şarj Kablosu 1 M ' → 'usb c şarj kablosu 1 m'."""
    text = unicodedata.normalize("NFC", name).translate(_TR_LOWER).lower()
    return _NON_WORD.sub(" ", text).strip()


def _product_index(db: Session) -> dict[str, Product]:
    """Normalize edilmiş ad → ürün. Aynı anahtara düşen ikinci ürün yok sayılır (ilk kazanır)."""
    index: dict[str, Product] = {}
    for product in db.execute(select(Product).order_by(Product.id)).scalars():
        index.setdefault(normalize_name(product.name), product)
    return index


def _is_cancelled(order: TrendyolOrder, line: TrendyolOrderLine) -> bool:
    statuses = {(order.status or "").lower(), (line.status or "").lower()}
    return bool(statuses & NON_SALE_STATUSES)


def _skip(
    reason: str, external_id: str, detail: str | None = None
) -> dict[str, Any]:  # pragma: no cover - veri taşıyıcı
    return {"reason": reason, "external_id": external_id, "detail": detail}


def _import_line(
    db: Session, order: TrendyolOrder, line: TrendyolOrderLine, product: Product
) -> None:
    """Tek kalemi savepoint içinde yazar. Tekil indeks çakışırsa IntegrityError yükselir."""
    with db.begin_nested():
        sale = Sale(
            sold_at=order.ordered_at,
            product_id=product.id,
            qty=line.quantity,
            unit_price=line.unit_price,
            total=line.total,
            channel=CHANNEL,
        )
        db.add(sale)
        db.flush()  # sale.id gerekir
        db.add(
            ExternalOrder(
                source=SOURCE,
                external_id=order.external_id(line),
                sale_id=sale.id,
                raw_json=line.raw,
            )
        )
        if product.stock_qty < line.quantity:
            log.warning(
                "trendyol: %s stoğu yetersiz (%d < %d); stok 0'a çekildi",
                product.name,
                product.stock_qty,
                line.quantity,
            )
        product.stock_qty = max(0, product.stock_qty - line.quantity)
        db.flush()  # tekil indeks çakışması burada patlar


def sync_orders(db: Session, since: datetime, until: datetime) -> dict[str, Any]:
    """[since, until) aralığını çeker ve `sales`'e aktarır.

    Dönüş: ``{mode, fetched, fetched_lines, imported, skipped, since, until}``
    ``skipped[] = {reason, external_id, detail}``; reason ∈ {urun_eslesmedi, mukerrer,
    iptal_edilmis, gecersiz_kalem}.
    """
    orders = trendyol.fetch_orders(since, until)
    products = _product_index(db)
    already = set(
        db.execute(select(ExternalOrder.external_id).where(ExternalOrder.source == SOURCE))
        .scalars()
        .all()
    )

    imported = 0
    line_count = 0
    skipped: list[dict[str, Any]] = []

    for order in orders:
        for line in order.lines:
            line_count += 1
            external_id = order.external_id(line)
            if external_id in already:
                skipped.append(_skip(REASON_DUPLICATE, external_id, order.order_number))
                continue
            if _is_cancelled(order, line):
                skipped.append(_skip(REASON_CANCELLED, external_id, line.product_name))
                continue
            if line.quantity <= 0 or line.unit_price < 0:
                skipped.append(_skip(REASON_INVALID, external_id, line.product_name))
                continue
            product = products.get(normalize_name(line.product_name))
            if product is None:
                skipped.append(_skip(REASON_NO_PRODUCT, external_id, line.product_name))
                continue
            try:
                _import_line(db, order, line, product)
            except IntegrityError:
                # Aynı anda koşan ikinci eşitleme: DB tekil indeksi son sözü söyler.
                skipped.append(_skip(REASON_DUPLICATE, external_id, order.order_number))
                continue
            already.add(external_id)
            imported += 1

    db.commit()
    log.info(
        "trendyol eşitleme (%s): %d sipariş / %d kalem → %d aktarıldı, %d atlandı",
        trendyol.mode(),
        len(orders),
        line_count,
        imported,
        len(skipped),
    )
    return {
        "mode": trendyol.mode(),
        "fetched": len(orders),
        "fetched_lines": line_count,
        "imported": imported,
        "skipped": skipped,
        "since": since,
        "until": until,
    }


def import_stats(db: Session) -> tuple[datetime | None, int]:
    """(son eşitleme anı, aktarılan toplam kalem) — `external_orders` üzerinden."""
    row = db.execute(
        select(func.max(ExternalOrder.imported_at), func.count(ExternalOrder.id)).where(
            ExternalOrder.source == SOURCE
        )
    ).one()
    return row[0], int(row[1] or 0)
