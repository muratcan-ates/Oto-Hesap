"""Trendyol Marketplace **salt-okur** sipariş adaptörü (yol haritası prototipi).

Kapsam: yalnız satıcının sipariş paketlerini OKUMAK. Stok/fiyat gönderimi, iade, kargo, ürün
servisleri YOK. `docs/DECISIONS.md` D11: Trendyol tarafında yalnız güncel (V2) servisler hedeflenir;
Product V1'e tek satır kod yazılmaz (V1 15 Eyl 2026'da kapanıyor).

İki mod (`TRENDYOL_MODE`):

* ``mock`` (varsayılan) — ``app/data/trendyol_ornek_siparisler.json`` okunur. **Ağa çıkılmaz**,
  anahtar gerekmez. Gerçek satıcı anahtarımız olmadığı için demo ve testler bu modda koşar.
* ``live`` — gerçek uca gidilir. Satıcı bilgileri eksikse istek yapılmadan açık hata verilir.

Resmî dokümandan (developers.trendyol.com, 13 Eyl 2026) **doğrulanan** noktalar:
  - Temel adres: ``https://apigw.trendyol.com/integration`` (stage: ``stageapigw.trendyol.com``)
  - Sipariş paketleri servisi (getShipmentPackages): ``GET /order/sellers/{sellerId}/orders``
  - Sorgu parametreleri: ``startDate``, ``endDate`` (epoch **milisaniye**), ``page`` (0 tabanlı),
    ``size`` (en çok 200), ``status``, ``orderByField``, ``orderByDirection``
  - Kimlik doğrulama: HTTP Basic (API Key / API Secret, satıcı panelindeki "Entegrasyon Bilgileri")
  - ``User-Agent``: ``"{satıcıId} - SelfIntegration"``
  - Hız sınırı: aynı uca 10 saniyede en çok 50 istek; yanıt gövdesi
    ``{totalElements, totalPages, page, size, content:[{orderNumber, shipmentPackageId, orderDate,
    status, lines:[{lineId, quantity, productName, barcode, stockCode, lineUnitPrice,
    lineGrossAmount, ...}]}]}``

Doğrulayamadıklarımız kodda ``TODO(murat)`` ile işaretlidir (bkz. `_live_url`, `_headers`).
"""

from __future__ import annotations

import json
import logging
import pathlib
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from functools import lru_cache
from typing import Any

import httpx

from ..config import settings

log = logging.getLogger("otohesap.trendyol")

SOURCE = "trendyol"

#: Örnek veri kümesi (mock modun tek kaynağı). Gerçek Trendyol yanıtı değildir.
MOCK_PATH = pathlib.Path(__file__).resolve().parents[1] / "data" / "trendyol_ornek_siparisler.json"

TIMEOUT_SECONDS = 20.0
PAGE_SIZE = 200  # resmî üst sınır
MAX_PAGES = 50  # 200 × 50 = 10.000 kayıt (maxQueryWindowResult); sonsuz döngü emniyeti
MAX_ATTEMPTS = 3  # 429/5xx için toplam deneme sayısı
BACKOFF_BASE_SECONDS = 1.0  # 1 sn, 2 sn (üstel)
RATE_LIMIT_SECONDS = 1.0  # saniyede 1 istek (resmî sınır 10 sn'de 50; fazlasıyla altında)
#: `startDate`–`endDate` arası resmî olarak en fazla 2 hafta; canlı modda varsayılan pencere.
LIVE_WINDOW_DAYS = 14
_BODY_PREVIEW = 300
_TWO_PLACES = Decimal("0.01")

MISSING_CREDENTIALS = (
    "Trendyol satıcı bilgileri tanımlı değil "
    "(TRENDYOL_SUPPLIER_ID, TRENDYOL_API_KEY, TRENDYOL_API_SECRET)."
)


class TrendyolError(RuntimeError):
    """Trendyol'dan sipariş okunamadı (yapılandırma, ağ, HTTP ya da gövde hatası)."""


# --- veri taşıyıcıları -----------------------------------------------------------------


@dataclass(frozen=True)
class TrendyolOrderLine:
    """Sipariş paketindeki tek kalem. `raw` denetim izi olarak `external_orders`'a yazılır."""

    line_id: str
    product_name: str
    quantity: int
    unit_price: Decimal
    barcode: str | None = None
    stock_code: str | None = None
    status: str | None = None
    raw: dict[str, Any] = field(default_factory=dict, repr=False)

    @property
    def total(self) -> Decimal:
        """Kalem tutarı = adet × birim fiyat (D16: gelir, satış anı fiyatıyla hesaplanır).

        Trendyol indirim/komisyon alanları (`lineTotalDiscount`, `commission`) bu prototipte
        yansıtılmaz; bkz. docs/ozellikler/trendyol-entegrasyonu.md "Ne YAPMAZ".
        """
        return (Decimal(self.quantity) * self.unit_price).quantize(
            _TWO_PLACES, rounding=ROUND_HALF_UP
        )


@dataclass(frozen=True)
class TrendyolOrder:
    """Bir sipariş paketi (shipment package)."""

    order_number: str
    ordered_at: datetime
    status: str
    lines: list[TrendyolOrderLine]
    shipment_package_id: str | None = None

    def external_id(self, line: TrendyolOrderLine) -> str:
        """Mükerrerlik anahtarı: ``trendyol:<orderNumber>:<lineId>``."""
        return f"{SOURCE}:{self.order_number}:{line.line_id}"


# --- yapılandırma ----------------------------------------------------------------------


def mode() -> str:
    """`mock` ya da `live` (config pattern'i başka değere izin vermez)."""
    return settings.trendyol_mode


def is_configured() -> bool:
    """Canlı çağrı için üç satıcı bilgisi de dolu mu?"""
    return all(
        bool(v and v.strip())
        for v in (
            settings.trendyol_supplier_id,
            settings.trendyol_api_key,
            settings.trendyol_api_secret,
        )
    )


def require_credentials() -> tuple[str, str, str]:
    """Satıcı bilgilerini döner; eksikse ağa çıkmadan açık Türkçe hata fırlatır."""
    if not is_configured():
        raise TrendyolError(MISSING_CREDENTIALS)
    return (
        str(settings.trendyol_supplier_id).strip(),
        str(settings.trendyol_api_key).strip(),
        str(settings.trendyol_api_secret).strip(),
    )


def default_since(until: datetime) -> datetime:
    """İstekte tarih verilmediğinde kullanılacak başlangıç.

    * ``live``: ``until - 14 gün`` (resmî `startDate`/`endDate` aralık sınırı).
    * ``mock``: örnek veri kümesindeki en eski sipariş anı — örnek küme tarihten bağımsız
      olarak bütün hâlinde okunsun diye (kümede sabit 2026 tarihleri var).
    """
    if mode() == "mock":
        orders = _load_mock_orders()
        if orders:
            return min(o.ordered_at for o in orders)
    return until - timedelta(days=LIVE_WINDOW_DAYS)


# --- ortak ayrıştırma ------------------------------------------------------------------


def _as_decimal(value: Any) -> Decimal | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None


def _line_unit_price(raw: dict[str, Any]) -> Decimal | None:
    """Birim fiyat. Dokümandan doğrulanan ad `lineUnitPrice`; diğerleri toleranslı yedek."""
    for key in ("lineUnitPrice", "price", "unitPrice", "amount"):
        value = _as_decimal(raw.get(key))
        if value is not None:
            return value
    return None


def _parse_line(raw: Any) -> TrendyolOrderLine | None:
    """Tek kalemi çevirir. Zorunlu alanları (lineId, productName, quantity) eksikse None."""
    if not isinstance(raw, dict):
        return None
    line_id = raw.get("lineId") if raw.get("lineId") is not None else raw.get("id")
    product_name = raw.get("productName")
    quantity = raw.get("quantity")
    unit_price = _line_unit_price(raw)
    if line_id is None or not product_name or unit_price is None:
        return None
    try:
        qty = int(quantity)
    except (TypeError, ValueError):
        return None
    return TrendyolOrderLine(
        line_id=str(line_id),
        product_name=str(product_name),
        quantity=qty,
        unit_price=unit_price,
        barcode=_opt_str(raw.get("barcode")),
        stock_code=_opt_str(raw.get("stockCode") or raw.get("merchantSku")),
        status=_opt_str(raw.get("orderLineItemStatusName")),
        raw=raw,
    )


def _opt_str(value: Any) -> str | None:
    return str(value) if value not in (None, "") else None


def _parse_order(raw: Any) -> TrendyolOrder | None:
    """Bir sipariş paketini çevirir; `orderNumber` ya da `orderDate` yoksa None (atlanır)."""
    if not isinstance(raw, dict):
        return None
    order_number = raw.get("orderNumber")
    ordered_at = _epoch_ms_to_dt(raw.get("orderDate"))
    if not order_number or ordered_at is None:
        return None
    lines = [line for line in map(_parse_line, raw.get("lines") or []) if line is not None]
    return TrendyolOrder(
        order_number=str(order_number),
        ordered_at=ordered_at,
        status=str(raw.get("shipmentPackageStatus") or raw.get("status") or "Bilinmiyor"),
        lines=lines,
        shipment_package_id=_opt_str(raw.get("shipmentPackageId")),
    )


def _epoch_ms_to_dt(value: Any) -> datetime | None:
    """Trendyol tarihleri epoch **milisaniye**dir; UTC datetime'a çevirir."""
    if value is None or isinstance(value, bool):
        return None
    try:
        return datetime.fromtimestamp(int(value) / 1000, tz=UTC)
    except (TypeError, ValueError, OverflowError, OSError):
        return None


def _as_utc(value: datetime) -> datetime:
    return value.astimezone(UTC) if value.tzinfo else value.replace(tzinfo=UTC)


def _in_range(order: TrendyolOrder, since: datetime, until: datetime) -> bool:
    """Yarı açık aralık [since, until) — AGENTS.md §7/11 ile aynı kural."""
    return since <= order.ordered_at < until


# --- mock modu -------------------------------------------------------------------------


@lru_cache(maxsize=1)
def _load_mock_orders() -> tuple[TrendyolOrder, ...]:
    """Örnek veri kümesini bir kez okur. Dosya bozuksa TrendyolError."""
    try:
        document = json.loads(MOCK_PATH.read_text(encoding="utf-8"))
    except FileNotFoundError as e:
        raise TrendyolError(f"Örnek sipariş dosyası bulunamadı: {MOCK_PATH.name}") from e
    except ValueError as e:
        raise TrendyolError(f"Örnek sipariş dosyası okunamadı (geçersiz JSON): {e}") from e

    packages = document.get("content") if isinstance(document, dict) else document
    if not isinstance(packages, list):
        raise TrendyolError("Örnek sipariş dosyasında 'content' listesi yok.")
    orders = [order for order in map(_parse_order, packages) if order is not None]
    return tuple(sorted(orders, key=lambda o: (o.ordered_at, o.order_number)))


def _fetch_mock(since: datetime, until: datetime) -> list[TrendyolOrder]:
    orders = [o for o in _load_mock_orders() if _in_range(o, since, until)]
    log.info(
        "trendyol mock: %d sipariş (%s → %s), örnek veri kümesi=%s",
        len(orders),
        since.isoformat(),
        until.isoformat(),
        MOCK_PATH.name,
    )
    return orders


# --- live modu -------------------------------------------------------------------------


def _new_client() -> httpx.Client:
    """HTTP istemcisi. Testler bu fonksiyonu sahte bir istemciyle monkeypatch'ler."""
    return httpx.Client(timeout=TIMEOUT_SECONDS)


def _sleep(seconds: float) -> None:
    """Hız sınırı ve geri çekilme beklemesi. Testler bunu monkeypatch'ler."""
    time.sleep(seconds)


def _live_url(supplier_id: str) -> str:
    """Sipariş paketleri ucu.

    Doğrulanan yol: ``{base}/order/sellers/{sellerId}/orders`` (getShipmentPackages).
    TODO(murat): Dokümanın bir sayfasında aynı servis ``/order/sellers/{sellerId}/v2/orders``
    olarak da geçiyor. Gerçek satıcı hesabı açıldığında stage ortamında hangisinin yanıt
    verdiğini doğrula ve burada sabitle.
    """
    base = settings.trendyol_base_url.rstrip("/")
    return f"{base}/order/sellers/{supplier_id}/orders"


def _headers(supplier_id: str) -> dict[str, str]:
    """Zorunlu başlıklar.

    ``User-Agent`` biçimi resmî dokümandan doğrulandı: ``"{satıcıId} - SelfIntegration"``.
    TODO(murat): Bazı doküman sayfaları ayrıca zorunlu bir ``storeFrontCode`` başlığından söz
    ediyor (yurt içi satıcı için beklenen değer doğrulanamadı). Yanlış değer göndermektense
    göndermiyoruz; satıcı panelinden doğrulanınca buraya eklenecek.
    """
    return {
        "User-Agent": f"{supplier_id} - SelfIntegration",
        "Accept": "application/json",
    }


def _params(since: datetime, until: datetime, page: int) -> dict[str, Any]:
    """Tarih parametreleri epoch milisaniyedir; `page` 0 tabanlı."""
    return {
        "startDate": int(since.timestamp() * 1000),
        "endDate": int(until.timestamp() * 1000),
        "page": page,
        "size": PAGE_SIZE,
        # TODO(murat): `status` ile yalnız ilgilendiğimiz durumlar çekilebilir
        # (Created, Picking, Invoiced, Shipped, Delivered ...). Bugün hepsi çekilip
        # iptal kalemleri trendyol_sync tarafında elenir.
    }


def _request_page(
    client: Any, url: str, headers: dict[str, str], auth: tuple[str, str], params: dict[str, Any]
) -> dict[str, Any]:
    """Tek sayfa; 429/5xx'te üstel geri çekilmeyle en çok `MAX_ATTEMPTS` deneme."""
    last_status: int | None = None
    last_preview = ""
    for attempt in range(1, MAX_ATTEMPTS + 1):
        try:
            response = client.get(url, headers=headers, auth=auth, params=params)
        except httpx.TimeoutException as e:
            raise TrendyolError(
                f"Trendyol yanıt vermedi (zaman aşımı {TIMEOUT_SECONDS:.0f} sn)."
            ) from e
        except httpx.HTTPError as e:
            # str(e) URL ve dolayısıyla kimlik bilgisi içerebilir; yalnız tür adı loglanır.
            raise TrendyolError(f"Trendyol'a bağlanılamadı ({type(e).__name__}).") from e

        status_code = response.status_code
        last_preview = str(getattr(response, "text", ""))[:_BODY_PREVIEW]
        if status_code < 400:
            try:
                body = response.json()
            except ValueError as e:
                raise TrendyolError(f"Trendyol geçersiz yanıt verdi: {last_preview}") from e
            if not isinstance(body, dict):
                raise TrendyolError("Trendyol yanıtı beklenen nesne biçiminde değil.")
            return body

        last_status = status_code
        retryable = status_code == 429 or status_code >= 500
        if not retryable or attempt == MAX_ATTEMPTS:
            break
        wait = BACKOFF_BASE_SECONDS * (2 ** (attempt - 1))
        log.warning(
            "trendyol HTTP %s (deneme %d/%d); %.0f sn sonra tekrar",
            status_code,
            attempt,
            MAX_ATTEMPTS,
            wait,
        )
        _sleep(wait)

    raise TrendyolError(f"Trendyol HTTP {last_status}: {last_preview}")


def _fetch_live(since: datetime, until: datetime) -> list[TrendyolOrder]:
    supplier_id, api_key, api_secret = require_credentials()
    url = _live_url(supplier_id)
    headers = _headers(supplier_id)
    auth = (api_key, api_secret)

    orders: list[TrendyolOrder] = []
    page = 0
    total_pages = 1
    with _new_client() as client:
        while page < min(total_pages, MAX_PAGES):
            if page > 0:
                _sleep(RATE_LIMIT_SECONDS)  # saniyede 1 istek
            body = _request_page(client, url, headers, auth, _params(since, until, page))
            content = body.get("content") or []
            if not isinstance(content, list):
                raise TrendyolError("Trendyol yanıtındaki 'content' liste değil.")
            orders.extend(o for o in map(_parse_order, content) if o is not None)
            try:
                total_pages = int(body.get("totalPages") or 1)
            except (TypeError, ValueError):
                total_pages = page + 1
            if not content:
                break
            page += 1

    if page >= MAX_PAGES:
        log.warning("trendyol: sayfa sınırına (%d) ulaşıldı; aralığı daraltın", MAX_PAGES)
    log.info("trendyol live: %d sipariş, %d sayfa okundu", len(orders), page)
    return [o for o in orders if _in_range(o, since, until)]


# --- genel arayüz ----------------------------------------------------------------------


def fetch_orders(since: datetime, until: datetime) -> list[TrendyolOrder]:
    """[since, until) aralığındaki sipariş paketlerini döner (yarı açık aralık).

    `mock` modda yerel örnek veri kümesinden, `live` modda gerçek uçtan okur. Hata → TrendyolError.
    """
    since, until = _as_utc(since), _as_utc(until)
    if since >= until:
        raise TrendyolError("Tarih aralığı geçersiz: başlangıç, bitişten küçük olmalı.")
    if mode() == "live":
        return _fetch_live(since, until)
    return _fetch_mock(since, until)
