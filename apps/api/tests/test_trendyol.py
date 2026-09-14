"""Trendyol salt-okur sipariş importu: mock okuma, ürün eşleme, mükerrerlik, tarih aralığı,
canlı modda anahtar kontrolü ve 429 sonrası yeniden deneme (sahte httpx). **Ağa çıkılmaz.**

Koşum:
    cd apps/api && TEST_DB_NAME=otohesap_test_trendyol uv run pytest -q tests/test_trendyol.py
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import httpx
import pytest
from sqlalchemy import func, select

from app.config import settings
from app.models import ExternalOrder, Product, Sale, Supplier
from app.services import trendyol, trendyol_sync
from app.services.trendyol import TrendyolError

# data/seed.py kataloğuyla birebir adlar (örnek veri kümesi bunlara eşleşir)
CATALOG = (
    ("Kablosuz Kulaklık Pro", "kulaklik", 1500),
    ("Bluetooth Kulak İçi Kulaklık", "kulaklik", 700),
    ("Oyuncu Kulaklığı RGB", "kulaklik", 1300),
    ("Spor Kulaklık Su Geçirmez", "kulaklik", 900),
    ("Silikon Telefon Kılıfı", "kilif", 60),
    ("Deri Cüzdan Kılıf", "kilif", 250),
    ("Şeffaf Darbe Emici Kılıf", "kilif", 80),
    ("Tablet Kılıfı 10 inç", "kilif", 300),
    ("USB-C Şarj Kablosu 1 m", "kablo-sarj", 70),
    ("Lightning Kablo 2 m", "kablo-sarj", 120),
    ("65 W GaN Hızlı Şarj Adaptörü", "kablo-sarj", 700),
    ("Araç İçi Şarj Cihazı", "kablo-sarj", 200),
    ("Powerbank 20000 mAh", "powerbank", 1100),
    ("Powerbank 10000 mAh", "powerbank", 600),
    ("Mini Powerbank 5000 mAh", "powerbank", 320),
    ("Kablosuz Şarjlı Powerbank", "powerbank", 1400),
    ("Araç İçi Telefon Tutucu", "aksesuar", 160),
    ("Temperli Ekran Koruyucu", "aksesuar", 40),
    ("Alüminyum Laptop Standı", "aksesuar", 500),
    ("Mini Bluetooth Hoparlör", "aksesuar", 1000),
)

MOCK_ORDER_COUNT = 25  # örnek veri kümesindeki sipariş paketi sayısı
MOCK_LINE_COUNT = 36  # toplam kalem
MOCK_IMPORTABLE = 33  # 36 − 2 katalog dışı − 1 iptal
SEPTEMBER = (datetime(2026, 9, 1, tzinfo=UTC), datetime(2026, 10, 1, tzinfo=UTC))
WIDE = (datetime(2026, 1, 1, tzinfo=UTC), datetime(2027, 1, 1, tzinfo=UTC))

SYNC_URL = "/api/integrations/trendyol/sync"
STATUS_URL = "/api/integrations/trendyol/status"


@pytest.fixture
def catalog(db):
    """Örnek siparişlerin eşleşeceği ürün kataloğu (stok bol; kritik eşiğe düşmesin)."""
    supplier = Supplier(
        name="Anadolu Güç Sistemleri",
        contact_channel="telegram",
        contact_address="111",
        lead_time_days=3,
    )
    db.add(supplier)
    db.flush()
    products = [
        Product(
            name=name,
            category=category,
            unit_cost=Decimal(cost),
            sale_price=Decimal(cost) * Decimal("1.5"),
            stock_qty=200,
            reorder_point=10,
            target_stock=250,
            supplier_id=supplier.id,
        )
        for name, category, cost in CATALOG
    ]
    db.add_all(products)
    db.commit()
    return {p.name: p for p in products}


def _body(since: datetime, until: datetime) -> dict[str, str]:
    return {"since": since.isoformat(), "until": until.isoformat()}


def _sale_qty(db, name: str) -> int:
    return int(
        db.execute(
            select(func.coalesce(func.sum(Sale.qty), 0))
            .join(Product, Sale.product_id == Product.id)
            .where(Product.name == name)
        ).scalar_one()
    )


# --- mock okuma ------------------------------------------------------------------------


def test_mock_25_siparis_okunur():
    orders = trendyol.fetch_orders(*WIDE)
    assert len(orders) == MOCK_ORDER_COUNT
    assert sum(len(o.lines) for o in orders) == MOCK_LINE_COUNT
    assert all(o.order_number.startswith("TY-2026-") for o in orders)
    assert all(o.ordered_at.tzinfo is not None for o in orders)


def test_mock_varsayilan_aralik_tum_kumeyi_kapsar():
    until = datetime.now(UTC)
    since = trendyol.default_since(until)
    assert len(trendyol.fetch_orders(since, until)) == MOCK_ORDER_COUNT


def test_tarih_araligi_filtresi():
    eylul = trendyol.fetch_orders(*SEPTEMBER)
    assert len(eylul) == 10
    assert all(o.ordered_at.month == 9 for o in eylul)
    # yarı açık aralık [başlangıç, bitiş): bitişe eşit an dışarıda kalır
    tek = trendyol.fetch_orders(
        datetime(2026, 9, 12, 11, 35, tzinfo=UTC), datetime(2026, 9, 12, 23, 18, tzinfo=UTC)
    )
    assert len(tek) == 1
    assert tek[0].ordered_at == datetime(2026, 9, 12, 11, 35, tzinfo=UTC)


def test_ters_aralik_hata_verir():
    with pytest.raises(TrendyolError, match="Tarih aralığı geçersiz"):
        trendyol.fetch_orders(WIDE[1], WIDE[0])


# --- eşleştirme ve yazma ---------------------------------------------------------------


def test_ad_normalizasyonu():
    n = trendyol_sync.normalize_name
    assert n(" usb-c şarj kablosu 1 M ") == n("USB-C Şarj Kablosu 1 m")
    assert n("POWERBANK 20000  MAH") == n("Powerbank 20000 mAh")
    assert n("Kablosuz Şarjlı Powerbank") != n("Kablosuz Şarj Standı 15 W")


def test_sync_eslesen_kalemleri_aktarir(client, db, catalog):
    r = client.post(SYNC_URL, json=_body(*WIDE))
    assert r.status_code == 200, r.text
    data = r.json()

    assert data["mode"] == "mock"
    assert data["fetched"] == MOCK_ORDER_COUNT
    assert data["fetched_lines"] == MOCK_LINE_COUNT
    assert data["imported"] == MOCK_IMPORTABLE
    assert db.execute(select(func.count(Sale.id))).scalar_one() == MOCK_IMPORTABLE
    assert db.execute(select(func.count(ExternalOrder.id))).scalar_one() == MOCK_IMPORTABLE

    # ürün eşleşmeleri (yazım farklı olan kalemler de doğru ürüne düştü)
    assert _sale_qty(db, "Powerbank 20000 mAh") == 5  # 1 + 2 + 1 (farklı yazım) + 1
    assert _sale_qty(db, "USB-C Şarj Kablosu 1 m") == 11  # 2+1+1+2+1 (farklı yazım) +4
    assert _sale_qty(db, "Temperli Ekran Koruyucu") == 7

    sale = db.execute(
        select(Sale).join(Product).where(Product.name == "Powerbank 10000 mAh")
    ).scalar_one()
    assert sale.channel == "online"
    assert sale.unit_price == Decimal("870.00")
    assert sale.total == Decimal("870.00")
    assert sale.sold_at.astimezone(UTC) == datetime(2026, 7, 19, 13, 40, tzinfo=UTC)

    ext = db.execute(
        select(ExternalOrder).where(ExternalOrder.sale_id == sale.id)
    ).scalar_one()
    assert ext.source == "trendyol"
    assert ext.external_id.startswith("trendyol:TY-2026-")
    assert ext.raw_json["productName"] == "Powerbank 10000 mAh"


def test_eslesmeyen_urun_atlanir_ve_raporlanir(client, catalog):
    data = client.post(SYNC_URL, json=_body(*WIDE)).json()
    atlanan = [s for s in data["skipped"] if s["reason"] == "urun_eslesmedi"]
    assert {s["detail"] for s in atlanan} == {"Kablosuz Şarj Standı 15 W", "Type-C OTG Adaptörü"}
    assert all(s["external_id"].startswith("trendyol:") for s in atlanan)


def test_eslesmeyen_urun_olusturulmaz(client, db, catalog):
    client.post(SYNC_URL, json=_body(*WIDE))
    assert db.execute(select(func.count(Product.id))).scalar_one() == len(CATALOG)


def test_iptal_edilmis_kalem_atlanir(client, db, catalog):
    data = client.post(SYNC_URL, json=_body(*WIDE)).json()
    iptal = [s for s in data["skipped"] if s["reason"] == "iptal_edilmis"]
    assert len(iptal) == 1
    assert _sale_qty(db, "Oyuncu Kulaklığı RGB") == 0


def test_ayni_sync_iki_kez_kosunca_ikinci_seferde_yeni_satir_yok(client, db, catalog):
    ilk = client.post(SYNC_URL, json=_body(*WIDE)).json()
    assert ilk["imported"] == MOCK_IMPORTABLE

    ikinci = client.post(SYNC_URL, json=_body(*WIDE)).json()
    assert ikinci["imported"] == 0
    assert len([s for s in ikinci["skipped"] if s["reason"] == "mukerrer"]) == MOCK_IMPORTABLE
    assert db.execute(select(func.count(Sale.id))).scalar_one() == MOCK_IMPORTABLE


def test_mukerrerlik_db_duzeyinde_tekil(db, catalog):
    """Uygulama içi ön kontrol atlansa bile tekil indeks ikinci yazmayı reddeder."""
    from sqlalchemy.exc import IntegrityError

    db.add(ExternalOrder(source="trendyol", external_id="trendyol:TY-1:1", raw_json={}))
    db.commit()
    db.add(ExternalOrder(source="trendyol", external_id="trendyol:TY-1:1", raw_json={}))
    with pytest.raises(IntegrityError):
        db.commit()
    db.rollback()


def test_import_stok_dusurur(client, db, catalog):
    client.post(SYNC_URL, json=_body(*WIDE))
    db.expire_all()
    powerbank = db.execute(
        select(Product).where(Product.name == "Powerbank 20000 mAh")
    ).scalar_one()
    assert powerbank.stock_qty == 200 - 5


def test_sync_tarih_araligi_yalniz_o_kalemleri_aktarir(client, db, catalog):
    data = client.post(SYNC_URL, json=_body(*SEPTEMBER)).json()
    assert data["fetched"] == 10
    assert data["imported"] < MOCK_IMPORTABLE
    en_eski = db.execute(select(func.min(Sale.sold_at))).scalar_one()
    assert en_eski.astimezone(UTC) >= SEPTEMBER[0]


# --- durum ucu -------------------------------------------------------------------------


def test_status_bos_veritabaninda(client):
    data = client.get(STATUS_URL).json()
    assert data == {"mode": "mock", "configured": False, "last_sync": None, "imported_total": 0}


def test_status_sync_sonrasi(client, catalog):
    client.post(SYNC_URL, json=_body(*WIDE))
    data = client.get(STATUS_URL).json()
    assert data["mode"] == "mock"
    assert data["imported_total"] == MOCK_IMPORTABLE
    assert data["last_sync"] is not None


# --- live modu: anahtar kontrolü -------------------------------------------------------


@pytest.fixture
def live_mode(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(settings, "trendyol_mode", "live")
    monkeypatch.setattr(settings, "trendyol_supplier_id", None)
    monkeypatch.setattr(settings, "trendyol_api_key", None)
    monkeypatch.setattr(settings, "trendyol_api_secret", None)
    monkeypatch.setattr(
        trendyol, "_new_client", _forbid_network("canlı modda anahtar yokken ağa çıkılmamalı")
    )


def _forbid_network(message: str):
    def factory() -> Any:
        raise AssertionError(message)

    return factory


def test_live_anahtar_yoksa_acik_hata(live_mode):
    with pytest.raises(TrendyolError) as exc:
        trendyol.fetch_orders(*WIDE)
    assert str(exc.value) == (
        "Trendyol satıcı bilgileri tanımlı değil "
        "(TRENDYOL_SUPPLIER_ID, TRENDYOL_API_KEY, TRENDYOL_API_SECRET)."
    )


def test_live_anahtar_yoksa_uc_400_doner(client, live_mode):
    r = client.post(SYNC_URL, json=_body(*WIDE))
    assert r.status_code == 400
    assert "TRENDYOL_SUPPLIER_ID" in r.json()["detail"]


def test_status_live_configured_false(client, live_mode):
    data = client.get(STATUS_URL).json()
    assert data["mode"] == "live" and data["configured"] is False


# --- live modu: sahte httpx ile sayfalama, hız sınırı ve yeniden deneme ----------------


class FakeResponse:
    def __init__(self, status_code: int, body: Any) -> None:
        self.status_code = status_code
        self._body = body
        self.text = str(body)

    def json(self) -> Any:
        if isinstance(self._body, Exception):
            raise self._body
        return self._body


class FakeClient:
    """`httpx.Client` yerine geçer: `with ... as c: c.get(url, headers=, auth=, params=)`."""

    def __init__(self, responses: list[FakeResponse | Exception]) -> None:
        self.responses = responses
        self.calls: list[dict[str, Any]] = []

    def __enter__(self) -> FakeClient:
        return self

    def __exit__(self, *exc: object) -> None:
        return None

    def get(self, url: str, **kwargs: Any) -> FakeResponse:
        self.calls.append({"url": url, **kwargs})
        response = self.responses[min(len(self.calls) - 1, len(self.responses) - 1)]
        if isinstance(response, Exception):
            raise response
        return response


def _page(orders: list[dict[str, Any]], total_pages: int = 1) -> FakeResponse:
    return FakeResponse(200, {"totalPages": total_pages, "content": orders})


ORNEK_PAKET = {
    "orderNumber": "TY-CANLI-1",
    "shipmentPackageId": 987654,
    "orderDate": int(datetime(2026, 9, 10, 12, 0, tzinfo=UTC).timestamp() * 1000),
    "status": "Delivered",
    "lines": [
        {
            "lineId": 5150,
            "quantity": 2,
            "productName": "Powerbank 20000 mAh",
            "barcode": "8690000000013",
            "stockCode": "PWB-20000",
            "lineUnitPrice": 1705.0,
        }
    ],
}


@pytest.fixture
def live_configured(monkeypatch: pytest.MonkeyPatch):
    """Canlı mod + sahte anahtarlar; ağ istemcisi testte enjekte edilir."""
    monkeypatch.setattr(settings, "trendyol_mode", "live")
    monkeypatch.setattr(settings, "trendyol_supplier_id", "4321")
    monkeypatch.setattr(settings, "trendyol_api_key", "anahtar")
    monkeypatch.setattr(settings, "trendyol_api_secret", "sir")
    monkeypatch.setattr(trendyol, "_sleep", lambda _s: None)  # testte beklemeyiz


def _install(monkeypatch: pytest.MonkeyPatch, responses: list[Any]) -> FakeClient:
    client = FakeClient(responses)
    monkeypatch.setattr(trendyol, "_new_client", lambda: client)
    return client


def test_live_tek_sayfa_okur(monkeypatch: pytest.MonkeyPatch, live_configured):
    fake = _install(monkeypatch, [_page([ORNEK_PAKET])])
    orders = trendyol.fetch_orders(*WIDE)

    assert len(orders) == 1 and orders[0].order_number == "TY-CANLI-1"
    assert orders[0].lines[0].total == Decimal("3410.00")
    call = fake.calls[0]
    assert call["url"] == "https://apigw.trendyol.com/integration/order/sellers/4321/orders"
    assert call["auth"] == ("anahtar", "sir")
    assert call["headers"]["User-Agent"] == "4321 - SelfIntegration"
    assert call["params"]["page"] == 0 and call["params"]["size"] == 200
    assert call["params"]["startDate"] == int(WIDE[0].timestamp() * 1000)
    assert call["params"]["endDate"] == int(WIDE[1].timestamp() * 1000)


def test_live_sayfalama(monkeypatch: pytest.MonkeyPatch, live_configured):
    ikinci = {**ORNEK_PAKET, "orderNumber": "TY-CANLI-2"}
    fake = _install(monkeypatch, [_page([ORNEK_PAKET], 2), _page([ikinci], 2)])
    orders = trendyol.fetch_orders(*WIDE)

    assert [o.order_number for o in orders] == ["TY-CANLI-1", "TY-CANLI-2"]
    assert [c["params"]["page"] for c in fake.calls] == [0, 1]


def test_live_429_sonrasi_yeniden_dener(monkeypatch: pytest.MonkeyPatch, live_configured):
    beklemeler: list[float] = []
    monkeypatch.setattr(trendyol, "_sleep", beklemeler.append)
    fake = _install(
        monkeypatch, [FakeResponse(429, "Too Many Requests"), _page([ORNEK_PAKET])]
    )
    orders = trendyol.fetch_orders(*WIDE)

    assert len(orders) == 1
    assert len(fake.calls) == 2  # ilk istek 429, ikincisi başarılı
    assert beklemeler == [1.0]  # üstel geri çekilme: 1 sn


def test_live_surekli_429_en_fazla_uc_deneme(monkeypatch: pytest.MonkeyPatch, live_configured):
    beklemeler: list[float] = []
    monkeypatch.setattr(trendyol, "_sleep", beklemeler.append)
    fake = _install(monkeypatch, [FakeResponse(429, "Too Many Requests")])

    with pytest.raises(TrendyolError, match="HTTP 429"):
        trendyol.fetch_orders(*WIDE)
    assert len(fake.calls) == 3
    assert beklemeler == [1.0, 2.0]


def test_live_5xx_yeniden_dener(monkeypatch: pytest.MonkeyPatch, live_configured):
    fake = _install(monkeypatch, [FakeResponse(503, "bakim"), _page([ORNEK_PAKET])])
    assert len(trendyol.fetch_orders(*WIDE)) == 1
    assert len(fake.calls) == 2


def test_live_401_yeniden_denemez(monkeypatch: pytest.MonkeyPatch, live_configured):
    fake = _install(monkeypatch, [FakeResponse(401, "Unauthorized")])
    with pytest.raises(TrendyolError, match="HTTP 401"):
        trendyol.fetch_orders(*WIDE)
    assert len(fake.calls) == 1


def test_live_zaman_asimi_acik_hata(monkeypatch: pytest.MonkeyPatch, live_configured):
    _install(monkeypatch, [httpx.TimeoutException("timeout")])
    with pytest.raises(TrendyolError, match="zaman aşımı 20 sn"):
        trendyol.fetch_orders(*WIDE)


def test_live_hiz_siniri_sayfalar_arasinda_bekler(
    monkeypatch: pytest.MonkeyPatch, live_configured
):
    beklemeler: list[float] = []
    monkeypatch.setattr(trendyol, "_sleep", beklemeler.append)
    ikinci = {**ORNEK_PAKET, "orderNumber": "TY-CANLI-2"}
    _install(monkeypatch, [_page([ORNEK_PAKET], 2), _page([ikinci], 2)])

    trendyol.fetch_orders(*WIDE)
    assert beklemeler == [trendyol.RATE_LIMIT_SECONDS]  # saniyede 1 istek


def test_live_sync_ucu_calisir(client, db, catalog, monkeypatch: pytest.MonkeyPatch,
                               live_configured):
    _install(monkeypatch, [_page([ORNEK_PAKET])])
    data = client.post(SYNC_URL, json=_body(*WIDE)).json()

    assert data["mode"] == "live" and data["imported"] == 1
    assert _sale_qty(db, "Powerbank 20000 mAh") == 2


def test_live_ust_servis_hatasi_502(client, catalog, monkeypatch: pytest.MonkeyPatch,
                                    live_configured):
    _install(monkeypatch, [FakeResponse(401, "Unauthorized")])
    r = client.post(SYNC_URL, json=_body(*WIDE))
    assert r.status_code == 502
    assert "HTTP 401" in r.json()["detail"]
