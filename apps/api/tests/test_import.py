"""CSV içe aktarma: önizleme → commit akışı, biçim algılama, hata ve sınır durumları.

Koşum: cd apps/api && TEST_DB_NAME=otohesap_test_import uv run pytest -q tests/test_import.py
"""

from __future__ import annotations

from datetime import timedelta

from sqlalchemy import func, select

from app.models import Expense, Product, Sale
from app.services import importer

SALES_CSV = (
    "Tarih,Ürün,Adet,Birim Fiyat,Kanal\r\n"
    "2026-04-01,USB-C Kablo,2,120.00,magaza\r\n"
    "2026-04-02 14:30,Powerbank 10000,1,900.00,online\r\n"
)

EXPENSES_CSV = (
    "Tarih;Kategori;Tutar;Tedarikçi;Not\r\n"
    "01.04.2026;kira;3.000,00;Emlak Ofisi;Nisan kirası\r\n"
    "05.04.2026;reklam;500,50;Meta Ads;\r\n"
)


def preview(client, kind: str, body: bytes, filename: str = "veri.csv"):
    return client.post(
        "/api/import/preview",
        files={"file": (filename, body, "text/csv")},
        data={"kind": kind},
    )


def commit(client, token: str, dry_run: bool = False):
    return client.post("/api/import/commit", json={"token": token, "dry_run": dry_run})


# --- mutlu yol ------------------------------------------------------------------------


def test_sales_preview_and_commit(client, db, small_data):
    kablo: Product = small_data["products"][0]
    stock_before = kablo.stock_qty

    resp = preview(client, "sales", SALES_CSV.encode("utf-8"))
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["kind"] == "sales"
    assert body["total_rows"] == 2
    assert body["valid_rows"] == 2
    assert body["errors"] == []
    assert body["mapping"] == {
        "sold_at": "Tarih",
        "product": "Ürün",
        "qty": "Adet",
        "unit_price": "Birim Fiyat",
        "channel": "Kanal",
    }
    assert len(body["preview"]) == 2
    first = body["preview"][0]
    assert first["ok"] is True
    assert first["values"]["sold_at"] == "01.04.2026 00:00"
    assert first["values"]["total"] == "240,00"
    assert "stoğu düşürmez" in body["note"]

    done = commit(client, body["token"])
    assert done.status_code == 200, done.text
    assert done.json() == {
        "kind": "sales",
        "inserted": 2,
        "skipped": 0,
        "errors": [],
        "dry_run": False,
    }

    rows = db.scalars(select(Sale).order_by(Sale.sold_at)).all()
    assert len(rows) == 2
    assert [float(r.total) for r in rows] == [240.0, 900.0]
    assert [r.channel for r in rows] == ["magaza", "online"]
    assert rows[0].sold_at.isoformat() == "2026-04-01T00:00:00+00:00"
    db.expire_all()
    assert db.get(Product, kablo.id).stock_qty == stock_before  # stok DÜŞMEZ (geçmiş veri)


def test_expenses_semicolon_and_decimal_comma(client, db, small_data):
    resp = preview(client, "expenses", EXPENSES_CSV.encode("utf-8"))
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["total_rows"] == 2
    assert body["valid_rows"] == 2
    assert body["mapping"]["amount"] == "Tutar"
    assert [row["values"]["amount"] for row in body["preview"]] == ["3000,00", "500,50"]
    assert body["note"] == ""

    assert commit(client, body["token"]).json()["inserted"] == 2
    amounts = sorted(float(a) for a in db.scalars(select(Expense.amount)).all())
    assert amounts == [500.5, 3000.0]
    kira = db.scalars(select(Expense).where(Expense.category == "kira")).one()
    assert kira.vendor == "Emlak Ofisi"
    assert kira.note == "Nisan kirası"
    assert kira.spent_at.isoformat() == "2026-04-01T00:00:00+00:00"


def test_utf8_bom_and_english_headers(client, db, small_data):
    body = "﻿date;product_name;quantity;unit_price\r\n2026-05-06;usb-c  kablo;3;110,25\r\n"
    resp = preview(client, "sales", body.encode("utf-8"))
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["mapping"]["sold_at"] == "date"  # BOM başlığı bozmadı
    assert data["valid_rows"] == 1
    assert commit(client, data["token"]).json()["inserted"] == 1
    sale = db.scalars(select(Sale)).one()
    assert sale.product_id == small_data["products"][0].id  # büyük/küçük + boşluk normalize
    assert float(sale.total) == 330.75


# --- satır düzeyi hatalar -------------------------------------------------------------


def test_unknown_product_row_is_skipped_with_reason(client, db, small_data):
    body = (
        "Tarih,Ürün,Adet,Birim Fiyat\r\n"
        "2026-04-01,USB-C Kablo,1,120\r\n"
        "2026-04-02,Olmayan Ürün,1,50\r\n"
    ).encode()
    data = preview(client, "sales", body).json()
    assert data["total_rows"] == 2
    assert data["valid_rows"] == 1
    assert len(data["errors"]) == 1
    error = data["errors"][0]
    assert error["row"] == 3 and error["field"] == "product"
    assert "Ürün bulunamadı" in error["message"]

    result = commit(client, data["token"]).json()
    assert (result["inserted"], result["skipped"]) == (1, 1)
    assert "Ürün bulunamadı" in result["errors"][0]["message"]
    assert db.scalar(select(func.count()).select_from(Sale)) == 1
    # Sessizce ürün oluşturulmadı
    assert db.scalar(select(func.count()).select_from(Product)) == 3


def test_broken_date_is_reported(client, db, small_data):
    body = (
        "Tarih,Ürün,Adet,Birim Fiyat\r\n"
        "31/02/2026,USB-C Kablo,1,120\r\n"
        "dün,USB-C Kablo,1,120\r\n"
        "2026-04-03,USB-C Kablo,1,120\r\n"
    ).encode()
    data = preview(client, "sales", body).json()
    assert data["valid_rows"] == 1
    assert [e["row"] for e in data["errors"]] == [2, 3]
    assert all(e["field"] == "sold_at" for e in data["errors"])
    assert "Tarih anlaşılmadı" in data["errors"][0]["message"]
    assert data["preview"][0]["ok"] is False
    assert data["preview"][0]["values"]["sold_at"] == "31/02/2026"  # ham değer gösterilir


def test_bad_quantity_and_amount(client, db, small_data):
    body = "Tarih;Kategori;Tutar\r\n01.04.2026;kira;-50\r\n02.04.2026;kira;abc\r\n".encode()
    data = preview(client, "expenses", body).json()
    assert data["valid_rows"] == 0
    assert [e["field"] for e in data["errors"]] == ["amount", "amount"]
    assert commit(client, data["token"]).json()["inserted"] == 0
    assert db.scalar(select(func.count()).select_from(Expense)) == 0


# --- dosya düzeyi hatalar -------------------------------------------------------------


def test_missing_header_is_422(client, db, small_data):
    body = "2026-04-01,USB-C Kablo,2,120\r\n2026-04-02,USB-C Kablo,1,120\r\n".encode()
    resp = preview(client, "sales", body)
    assert resp.status_code == 422
    assert "Başlık satırı tanınmadı" in resp.json()["detail"]
    assert "Tarih" in resp.json()["detail"]


def test_missing_required_column_is_422(client, db, small_data):
    body = "Tarih;Ürün;Kanal\r\n01.04.2026;USB-C Kablo;magaza\r\n".encode()
    resp = preview(client, "sales", body)
    assert resp.status_code == 422
    assert "Adet" in resp.json()["detail"]


def test_empty_file_is_422(client, db, small_data):
    assert preview(client, "sales", b"").status_code == 422
    resp = preview(client, "sales", "Tarih;Ürün;Adet;Birim Fiyat\r\n".encode())
    assert resp.status_code == 422
    assert "kayıt yok" in resp.json()["detail"]


def test_bad_kind_is_422(client, db, small_data):
    resp = preview(client, "urunler", SALES_CSV.encode("utf-8"))
    assert resp.status_code == 422
    assert "sales" in resp.json()["detail"]


def test_xlsx_is_out_of_scope(client, db, small_data):
    resp = preview(client, "sales", b"PK\x03\x04rest-of-zip", "veri.xlsx")
    assert resp.status_code == 422
    assert "XLSX" in resp.json()["detail"]


def test_file_over_5mb_is_413(client, db, small_data):
    body = b"Tarih;Ur\xc3\xbcn;Adet;Birim Fiyat\r\n" + b"x" * (importer.MAX_UPLOAD_BYTES + 1)
    resp = preview(client, "sales", body)
    assert resp.status_code == 413
    assert "5 MB" in resp.json()["detail"]


def test_row_limit_is_422(client, db, small_data, monkeypatch):
    monkeypatch.setattr(importer, "MAX_ROWS", 3)
    rows = "".join(f"2026-04-0{i},USB-C Kablo,1,120\r\n" for i in range(1, 6))
    resp = preview(client, "sales", ("Tarih,Ürün,Adet,Birim Fiyat\r\n" + rows).encode())
    assert resp.status_code == 422
    assert "satırdan fazla" in resp.json()["detail"]


def test_json_body_is_415(client, db, small_data):
    resp = client.post("/api/import/preview", json={"kind": "sales"})
    assert resp.status_code == 415
    assert "multipart" in resp.json()["detail"]


# --- token yaşam döngüsü --------------------------------------------------------------


def test_dry_run_writes_nothing_and_keeps_token(client, db, small_data):
    token = preview(client, "sales", SALES_CSV.encode("utf-8")).json()["token"]
    result = commit(client, token, dry_run=True).json()
    assert (result["inserted"], result["dry_run"]) == (2, True)
    assert db.scalar(select(func.count()).select_from(Sale)) == 0
    # deneme token'ı tüketmez: gerçek aktarım hâlâ mümkün
    assert commit(client, token).json()["inserted"] == 2
    assert db.scalar(select(func.count()).select_from(Sale)) == 2


def test_token_cannot_be_committed_twice(client, db, small_data):
    token = preview(client, "sales", SALES_CSV.encode("utf-8")).json()["token"]
    assert commit(client, token).status_code == 200
    again = commit(client, token)
    assert again.status_code == 409
    assert "zaten içe aktarıldı" in again.json()["detail"]
    assert db.scalar(select(func.count()).select_from(Sale)) == 2  # mükerrer kayıt yok


def test_expired_token_is_404(client, db, small_data):
    token = preview(client, "sales", SALES_CSV.encode("utf-8")).json()["token"]
    importer.STAGED[token].created_at -= timedelta(minutes=11)
    resp = commit(client, token)
    assert resp.status_code == 404
    assert "süresi doldu" in resp.json()["detail"]
    assert db.scalar(select(func.count()).select_from(Sale)) == 0


def test_unknown_token_is_404(client, db, small_data):
    assert commit(client, "yok-boyle-bir-token").status_code == 404


# --- şablonlar ------------------------------------------------------------------------


def test_templates(client):
    for kind, filename, header in (
        ("sales", "satis-sablonu.csv", "Tarih;Ürün;Adet;Birim Fiyat;Toplam;Kanal"),
        ("expenses", "gider-sablonu.csv", "Tarih;Kategori;Tutar;Tedarikçi;Not"),
    ):
        resp = client.get(f"/api/import/template/{kind}.csv")
        assert resp.status_code == 200
        assert filename in resp.headers["content-disposition"]
        assert resp.content.startswith(b"\xef\xbb\xbf")
        lines = resp.content.decode("utf-8-sig").rstrip("\r\n").split("\r\n")
        assert lines[0] == header
        assert len(lines) == 3  # başlık + 2 örnek satır


def test_template_is_importable(client, db, small_data):
    template = client.get("/api/import/template/expenses.csv").content
    data = preview(client, "expenses", template, "gider-sablonu.csv").json()
    assert data["valid_rows"] == 2
    assert commit(client, data["token"]).json()["inserted"] == 2


# --- birim testler --------------------------------------------------------------------


def test_parse_decimal_variants():
    cases = {
        "1.234,50": "1234.50",
        "1,234.50": "1234.50",
        "120,00": "120.00",
        "₺ 90.5": "90.5",
        "1 250": "1250",
        "-7,5": "-7.5",
    }
    for raw, expected in cases.items():
        assert importer.parse_decimal(raw) == importer.Decimal(expected), raw
    assert importer.parse_decimal("abc") is None
    assert importer.parse_decimal("") is None


def test_parse_datetime_variants():
    iso = "2026-04-01T00:00:00+00:00"
    for raw in ("2026-04-01", "01.04.2026", "01/04/2026"):
        assert importer.parse_datetime(raw).isoformat() == iso
    assert importer.parse_datetime("01.04.2026 09:30").isoformat() == "2026-04-01T09:30:00+00:00"
    assert importer.parse_datetime("2026-04-01T12:00:00+03:00").isoformat() == (
        "2026-04-01T09:00:00+00:00"
    )
    for raw in ("01.04.26", "Nisan 2026", "2026/04/01", ""):
        assert importer.parse_datetime(raw) is None


def test_formula_prefix_is_kept_as_data(client, db, small_data):
    """Formül öneki temizlenmez; veri olarak saklanır (dışa aktarımda kaçışlanıyor)."""
    body = "Tarih;Kategori;Tutar;Not\r\n01.04.2026;kira;100;=1+1\r\n".encode()
    data = preview(client, "expenses", body).json()
    assert data["preview"][0]["values"]["note"] == "=1+1"
    commit(client, data["token"])
    assert db.scalars(select(Expense.note)).one() == "=1+1"
