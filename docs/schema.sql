-- OtoHesap veri modeli (PostgreSQL 16). Sahibi: Murat. Değişiklik = PR + WhatsApp duyurusu.
-- Uygulama: psql "$DATABASE_URL" -f docs/schema.sql
--
-- ÇOK KİRACILIK (D13): tek şema + `tenant_id` + PostgreSQL RLS (schema-per-tenant DEĞİL).
-- Mevcut bir veritabanını yükseltmek için: docs/migrations/002_tenant.sql (idempotent).
--
-- !!! ÜRETİMDE KRİTİK !!!
-- RLS, tablo SAHİBİNİ ve `BYPASSRLS` / superuser rolleri ES GEÇER. Uygulama bu veritabanına
-- tabloların sahibi OLMAYAN, superuser OLMAYAN ve NOBYPASSRLS bir rolle bağlanmalıdır; aksi
-- halde politikalar sessizce atlanır ve kiracılar birbirinin verisini görür.
-- Aşağıdaki `FORCE ROW LEVEL SECURITY` sahibi de politikaya tabi kılar (ikinci savunma hattı),
-- ama superuser'ı yine de durduramaz. Doğrulama: `docs/ozellikler/kimlik-ve-kiraci.md`.

-- ---------------------------------------------------------------- kiracı ve kullanıcı

CREATE TABLE IF NOT EXISTS tenants (
  id         UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  name       TEXT NOT NULL,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Varsayılan kiracı: AUTH_ENABLED=false iken (ve göçten sonra) tüm veri buna bağlıdır.
-- UUID, uygulamadaki DEFAULT_TENANT_ID ayarıyla birebir aynı olmalıdır.
INSERT INTO tenants (id, name)
VALUES ('00000000-0000-0000-0000-000000000001', 'OtoHesap Demo Mağaza')
ON CONFLICT (id) DO NOTHING;

CREATE TABLE IF NOT EXISTS users (
  id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id     UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
  email         TEXT NOT NULL UNIQUE,          -- küçük harfe indirgenmiş olarak saklanır
  password_hash TEXT NOT NULL,                 -- scrypt$n$r$p$<salt_b64>$<hash_b64> (stdlib hashlib)
  role          TEXT NOT NULL DEFAULT 'member' CHECK (role IN ('owner', 'member')),
  created_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS ix_users_tenant ON users (tenant_id);

-- `tenants` ve `users` bilerek RLS DIŞINDADIR: oturum doğrulaması kiracı bağlamı kurulmadan
-- ÖNCE çalışır (yumurta-tavuk). Bu iki tabloya yalnız kimlik katmanı dokunur; asistanın
-- salt-okur rolüne SELECT verilmez (aşağıdaki REVOKE'lara bakın).

-- ---------------------------------------------------------------- iş tabloları
-- Her iş tablosunda `tenant_id` vardır. Varsayılan, oturumun kiracı bağlamıdır:
-- `SET LOCAL app.tenant_id = '<uuid>'` yapılmadan yapılan ekleme NOT NULL ile patlar (sessizce
-- yanlış kiracıya yazmaktansa gürültülü hata).

CREATE TABLE IF NOT EXISTS suppliers (
  id              SERIAL PRIMARY KEY,
  tenant_id       UUID NOT NULL REFERENCES tenants(id)
                  DEFAULT NULLIF(current_setting('app.tenant_id', true), '')::uuid,
  name            TEXT NOT NULL,
  contact_channel TEXT NOT NULL CHECK (contact_channel IN ('telegram', 'email')),
  contact_address TEXT NOT NULL,          -- Telegram chat_id veya e-posta
  lead_time_days  INT  NOT NULL DEFAULT 3
);

CREATE TABLE IF NOT EXISTS products (
  id            SERIAL PRIMARY KEY,
  tenant_id     UUID NOT NULL REFERENCES tenants(id)
                DEFAULT NULLIF(current_setting('app.tenant_id', true), '')::uuid,
  name          TEXT NOT NULL,
  category      TEXT NOT NULL,
  unit_cost     NUMERIC(12,2) NOT NULL,
  sale_price    NUMERIC(12,2) NOT NULL,
  stock_qty     INT NOT NULL DEFAULT 0,
  reorder_point INT NOT NULL,             -- kritik eşik
  target_stock  INT NOT NULL,             -- sipariş sonrası hedef
  supplier_id   INT REFERENCES suppliers(id)
);

CREATE TABLE IF NOT EXISTS sales (
  id         SERIAL PRIMARY KEY,
  tenant_id  UUID NOT NULL REFERENCES tenants(id)
             DEFAULT NULLIF(current_setting('app.tenant_id', true), '')::uuid,
  sold_at    TIMESTAMPTZ NOT NULL,
  product_id INT NOT NULL REFERENCES products(id),
  qty        INT NOT NULL CHECK (qty > 0),
  unit_price NUMERIC(12,2) NOT NULL,
  total      NUMERIC(12,2) NOT NULL,
  channel    TEXT NOT NULL DEFAULT 'magaza' CHECK (channel IN ('magaza', 'online'))
);

CREATE TABLE IF NOT EXISTS expenses (
  id        SERIAL PRIMARY KEY,
  tenant_id UUID NOT NULL REFERENCES tenants(id)
            DEFAULT NULLIF(current_setting('app.tenant_id', true), '')::uuid,
  spent_at  TIMESTAMPTZ NOT NULL,
  category  TEXT NOT NULL,                  -- kira, maas, elektrik, kargo, reklam, tedarik
  amount    NUMERIC(12,2) NOT NULL CHECK (amount > 0),
  vendor    TEXT,
  note      TEXT
);

CREATE TABLE IF NOT EXISTS purchase_orders (
  id           SERIAL PRIMARY KEY,
  tenant_id    UUID NOT NULL REFERENCES tenants(id)
               DEFAULT NULLIF(current_setting('app.tenant_id', true), '')::uuid,
  created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
  product_id   INT NOT NULL REFERENCES products(id),
  supplier_id  INT NOT NULL REFERENCES suppliers(id),
  qty          INT NOT NULL CHECK (qty > 0),
  est_amount   NUMERIC(12,2),
  status       TEXT NOT NULL DEFAULT 'draft' CHECK (status IN ('draft', 'approved', 'sent', 'rejected')),
  message_text TEXT,
  sent_at      TIMESTAMPTZ,
  notify_ref   TEXT                     -- Telegram message_id, 'dry-run' vb.; gönderim izi
);

-- Mevcut veritabanları için (idempotent)
ALTER TABLE purchase_orders ADD COLUMN IF NOT EXISTS notify_ref TEXT;

-- Ürün başına yalnız BİR açık sipariş: DB düzeyinde mükerrerlik koruması (çift tıklama, çift
-- zamanlayıcı). `product_id` zaten tek bir kiracıya ait olduğu için kiracılar arası çakışma olmaz.
CREATE UNIQUE INDEX IF NOT EXISTS ux_open_order_per_product
  ON purchase_orders (product_id) WHERE status IN ('draft', 'approved', 'sent');

CREATE TABLE IF NOT EXISTS chat_log (
  id        SERIAL PRIMARY KEY,
  tenant_id UUID NOT NULL REFERENCES tenants(id)
            DEFAULT NULLIF(current_setting('app.tenant_id', true), '')::uuid,
  asked_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
  question  TEXT NOT NULL,
  sql_text  TEXT,
  answer    TEXT,
  ok        BOOLEAN NOT NULL DEFAULT false
);

CREATE INDEX IF NOT EXISTS ix_sales_sold_at     ON sales (sold_at);
CREATE INDEX IF NOT EXISTS ix_sales_product     ON sales (product_id);
CREATE INDEX IF NOT EXISTS ix_expenses_spent_at ON expenses (spent_at);
CREATE INDEX IF NOT EXISTS ix_orders_status     ON purchase_orders (status);
-- Kiracı filtresi her sorguya girdiği için tenant_id önde olan indeksler
CREATE INDEX IF NOT EXISTS ix_sales_tenant      ON sales (tenant_id, sold_at);
CREATE INDEX IF NOT EXISTS ix_expenses_tenant   ON expenses (tenant_id, spent_at);
CREATE INDEX IF NOT EXISTS ix_products_tenant   ON products (tenant_id);
CREATE INDEX IF NOT EXISTS ix_suppliers_tenant  ON suppliers (tenant_id);
CREATE INDEX IF NOT EXISTS ix_orders_tenant     ON purchase_orders (tenant_id, status);
CREATE INDEX IF NOT EXISTS ix_chat_log_tenant   ON chat_log (tenant_id, asked_at);

-- `security_invoker` (PostgreSQL 15+) ŞART: varsayılanda görünüm SAHİBİNİN haklarıyla okur ve
-- sahibi superuser ise alttaki tabloların RLS'i atlanır → görünüm tüm kiracıları sızdırır.
CREATE OR REPLACE VIEW v_monthly_cashflow WITH (security_invoker = true) AS
WITH months AS (
  SELECT date_trunc('month', sold_at)  AS month FROM sales
  UNION
  SELECT date_trunc('month', spent_at) AS month FROM expenses
),
inc AS (SELECT date_trunc('month', sold_at)  AS month, SUM(total)  AS income  FROM sales    GROUP BY 1),
exp AS (SELECT date_trunc('month', spent_at) AS month, SUM(amount) AS expense FROM expenses GROUP BY 1)
SELECT m.month,
       COALESCE(inc.income, 0)  AS income,
       COALESCE(exp.expense, 0) AS expense,
       COALESCE(inc.income, 0) - COALESCE(exp.expense, 0) AS net
FROM months m
LEFT JOIN inc ON inc.month = m.month
LEFT JOIN exp ON exp.month = m.month
ORDER BY m.month;

-- ---------------------------------------------------------------- satır düzeyi güvenlik (D13)
-- Politika: satır yalnız oturumun kiracı bağlamına aitse görünür/yazılabilir.
-- `current_setting('app.tenant_id', true)` tanımsızsa NULL, boş dizeyse NULLIF ile NULL olur;
-- NULL = hiçbir satır (fail-closed). Uygulama her istekte `SET LOCAL app.tenant_id` yapar.
DO $$
DECLARE t text;
BEGIN
  FOREACH t IN ARRAY ARRAY['suppliers', 'products', 'sales', 'expenses', 'purchase_orders', 'chat_log']
  LOOP
    EXECUTE format('ALTER TABLE %I ENABLE ROW LEVEL SECURITY', t);
    EXECUTE format('ALTER TABLE %I FORCE ROW LEVEL SECURITY', t);  -- sahibi de politikaya tabi
    EXECUTE format('DROP POLICY IF EXISTS tenant_isolation ON %I', t);
    EXECUTE format($p$
      CREATE POLICY tenant_isolation ON %I
        USING      (tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid)
        WITH CHECK (tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid)
    $p$, t);
  END LOOP;
END $$;

-- ---------------------------------------------------------------- roller
-- Uygulama rolü (üretim; Neon SQL editöründe bir kez, parolayı .env'e koy, depoya koyma):
-- CREATE ROLE otohesap_app LOGIN PASSWORD '<parola>' NOSUPERUSER NOCREATEDB NOCREATEROLE NOBYPASSRLS;
-- GRANT CONNECT ON DATABASE neondb TO otohesap_app;
-- GRANT USAGE ON SCHEMA public TO otohesap_app;
-- GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO otohesap_app;
-- GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO otohesap_app;
-- ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO otohesap_app;
-- Doğrula (üçü de false olmalı):
--   SELECT rolsuper, rolbypassrls FROM pg_roles WHERE rolname = 'otohesap_app';
--   SELECT tableowner = 'otohesap_app' FROM pg_tables WHERE tablename = 'sales';
--
-- Asistan için salt-okur rol (AGENTS.md §7):
-- CREATE ROLE otohesap_ro LOGIN PASSWORD '<parola>' NOSUPERUSER NOBYPASSRLS;
-- GRANT CONNECT ON DATABASE neondb TO otohesap_ro;
-- GRANT USAGE ON SCHEMA public TO otohesap_ro;
-- GRANT SELECT ON ALL TABLES IN SCHEMA public TO otohesap_ro;
-- ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT SELECT ON TABLES TO otohesap_ro;
-- ALTER ROLE otohesap_ro SET statement_timeout = '5s';
-- Gizli iletişim alanı asistana kapalı (sütun düzeyi):
-- REVOKE SELECT ON suppliers FROM otohesap_ro;
-- GRANT SELECT (id, tenant_id, name, contact_channel, lead_time_days) ON suppliers TO otohesap_ro;
-- Kimlik tabloları asistana tamamen kapalı (parola özeti asla sorgulanamaz):
-- REVOKE ALL ON users, tenants FROM otohesap_ro;
