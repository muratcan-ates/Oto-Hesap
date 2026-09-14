"use client";

import { useCallback, useRef, useState, type ChangeEvent } from "react";
import { IconAlert, IconCheck, IconDownload, IconInfo } from "./Icons";
import { Modal } from "./Modal";
import { Button, Notice, Spinner } from "./ui";
import { errorMessage, importCommit, importPreview, importTemplateUrl } from "@/lib/api";
import { IMPORT_FIELD_LABELS, type ImportKind, type ImportPreview, type ImportResult, type ImportRowError } from "@/lib/types";

const KIND_LABEL: Record<ImportKind, string> = { sales: "satış", expenses: "gider" };
const MAX_MB = 5;
const SHOWN_ERRORS = 8;

type Stage = { s: "pick" } | { s: "preview"; data: ImportPreview } | { s: "done"; result: ImportResult };

function fieldLabel(field: string | null): string {
  if (!field) return "Dosya";
  return IMPORT_FIELD_LABELS[field] ?? field;
}

function ErrorList({ errors, total }: { errors: ImportRowError[]; total?: number }) {
  if (errors.length === 0) return null;
  const rest = (total ?? errors.length) - Math.min(errors.length, SHOWN_ERRORS);
  return (
    <div className="rounded-lg border border-danger/30 bg-danger-tint px-3.5 py-2.5">
      <p className="mb-1.5 flex items-center gap-2 text-[13px] font-medium text-danger-ink">
        <IconAlert size={16} />
        Atlanan satırlar
      </p>
      <ul className="space-y-1 text-[12.5px] text-danger-ink">
        {errors.slice(0, SHOWN_ERRORS).map((e, i) => (
          <li key={`${e.row}-${e.field}-${i}`}>
            <span className="font-medium tabular-nums">{e.row}. satır</span>
            {e.field ? ` · ${fieldLabel(e.field)}` : ""} — {e.message}
          </li>
        ))}
      </ul>
      {rest > 0 && <p className="mt-1.5 text-[12.5px] text-danger-ink opacity-80">…ve {rest} kayıt daha.</p>}
    </div>
  );
}

function PreviewTable({ data }: { data: ImportPreview }) {
  const fields = Object.keys(data.mapping);
  return (
    <div className="overflow-x-auto rounded-lg border border-line">
      <table className="w-full border-collapse text-[13px]">
        <caption className="sr-only">İçe aktarılacak ilk satırlar</caption>
        <thead>
          <tr className="bg-surface-2 text-left text-[12px] uppercase tracking-wide text-muted">
            <th scope="col" className="whitespace-nowrap px-3 py-2 font-medium">
              Satır
            </th>
            {fields.map((f) => (
              <th key={f} scope="col" className="whitespace-nowrap px-3 py-2 font-medium">
                {fieldLabel(f)}
                <span className="ml-1 normal-case text-[11px] text-muted opacity-70">({data.mapping[f]})</span>
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {data.preview.map((row) => (
            <tr key={row.row} className={`border-t border-line ${row.ok ? "" : "bg-danger-tint/60"}`}>
              <td className="whitespace-nowrap px-3 py-1.5 tabular-nums text-muted">{row.row}</td>
              {fields.map((f) => (
                <td key={f} className={`px-3 py-1.5 ${row.ok ? "text-navy" : "text-danger-ink"}`}>
                  {row.values[f] || <span className="text-muted">—</span>}
                </td>
              ))}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

interface Props {
  open: boolean;
  kind: ImportKind;
  onClose: () => void;
  onImported: (result: ImportResult) => void;
}

/** Dosya seç → önizleme (ilk 20 satır + eşleme + hatalar) → aktar → sonuç özeti. */
export function ImportDialog({ open, kind, onClose, onImported }: Props) {
  const [stage, setStage] = useState<Stage>({ s: "pick" });
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const inputRef = useRef<HTMLInputElement>(null);

  const reset = useCallback(() => {
    setStage({ s: "pick" });
    setBusy(false);
    setError(null);
    if (inputRef.current) inputRef.current.value = "";
  }, []);

  const close = useCallback(() => {
    reset();
    onClose();
  }, [onClose, reset]);

  async function pickFile(e: ChangeEvent<HTMLInputElement>) {
    const file = e.target.files?.[0];
    if (!file) return;
    setError(null);
    if (file.size > MAX_MB * 1024 * 1024) {
      setError(`Dosya ${MAX_MB} MB sınırını aşıyor (${(file.size / 1024 / 1024).toFixed(1)} MB).`);
      return;
    }
    setBusy(true);
    try {
      setStage({ s: "preview", data: await importPreview(kind, file) });
    } catch (err) {
      setStage({ s: "pick" });
      setError(errorMessage(err));
    } finally {
      setBusy(false);
    }
  }

  async function runImport(token: string) {
    setBusy(true);
    setError(null);
    try {
      const result = await importCommit(token);
      setStage({ s: "done", result });
      onImported(result);
    } catch (err) {
      setError(errorMessage(err));
    } finally {
      setBusy(false);
    }
  }

  const title = `${kind === "sales" ? "Satış" : "Gider"} kayıtlarını içe aktar`;

  return (
    <Modal
      open={open}
      title={title}
      size={stage.s === "preview" ? "lg" : "md"}
      onClose={close}
      footer={
        stage.s === "preview" ? (
          <>
            <span className="mr-auto text-[12.5px] text-muted">
              {stage.data.valid_rows > 0
                ? `${stage.data.valid_rows} satır aktarılacak`
                : "Aktarılabilir satır yok; dosyayı düzeltip tekrar yükleyin."}
            </span>
            <Button onClick={reset} disabled={busy}>
              Başka dosya
            </Button>
            <Button variant="primary" busy={busy} disabled={stage.data.valid_rows === 0} onClick={() => runImport(stage.data.token)}>
              Aktar
            </Button>
          </>
        ) : stage.s === "done" ? (
          <>
            <Button onClick={reset} disabled={busy}>
              Yeni dosya
            </Button>
            <Button variant="primary" onClick={close}>
              Kapat
            </Button>
          </>
        ) : (
          <Button onClick={close}>Vazgeç</Button>
        )
      }
    >
      <div className="space-y-4">
        {error && <Notice tone="danger">{error}</Notice>}

        {stage.s === "pick" && (
          <>
            <div className="space-y-2 text-[13px] text-muted">
              <p className="text-navy">
                Excel veya muhasebe programınızdan aldığınız <span className="font-medium">CSV</span> dosyasını seçin. Dosya yüklenmeden önce
                ekranda gösterilir; onaylamadan hiçbir kayıt eklenmez.
              </p>
              <ul className="list-inside list-disc space-y-1">
                <li>Başlık satırı zorunlu; Türkçe ve İngilizce sütun adları tanınır.</li>
                <li>
                  {kind === "sales"
                    ? "Zorunlu sütunlar: Tarih, Ürün, Adet ve Birim fiyat (veya Toplam). Ürün adı Stok listesiyle eşleşmeli; eşleşmeyen satır atlanır."
                    : "Zorunlu sütunlar: Tarih, Kategori, Tutar. Tedarikçi ve Not isteğe bağlı."}
                </li>
                <li>{"Ayraç (; veya ,) ve ondalık işareti (, veya .) kendiliğinden algılanır. En fazla 5 MB / 10.000 satır."}</li>
              </ul>
            </div>

            {kind === "sales" && (
              <Notice tone="info">
                <span className="flex items-start gap-2">
                  <IconInfo size={16} className="mt-0.5 shrink-0" />
                  İçe aktarılan satışlar stoğu düşürmez; geçmiş veri olarak kaydedilir.
                </span>
              </Notice>
            )}

            <div className="flex flex-wrap items-center gap-3 border-t border-line pt-4">
              <input
                ref={inputRef}
                type="file"
                accept=".csv,text/csv,text/plain"
                onChange={pickFile}
                disabled={busy}
                aria-label="CSV dosyası seç"
                className="block w-full max-w-xs text-[13px] text-muted file:mr-3 file:rounded-lg file:border-0 file:bg-brand-strong file:px-3 file:py-2 file:text-[13px] file:font-medium file:text-white hover:file:bg-brand-ink disabled:opacity-60"
              />
              {busy && (
                <span className="flex items-center gap-2 text-[13px] text-muted">
                  <Spinner className="size-4" /> Dosya okunuyor…
                </span>
              )}
              <a
                href={importTemplateUrl(kind)}
                className="ml-auto inline-flex items-center gap-1.5 text-[13px] font-medium text-brand-ink underline-offset-2 hover:underline"
              >
                <IconDownload size={15} />
                Örnek {KIND_LABEL[kind]} şablonu (CSV)
              </a>
            </div>
          </>
        )}

        {stage.s === "preview" && (
          <>
            <div className="flex flex-wrap items-center gap-x-4 gap-y-1 text-[13px]">
              <span className="font-medium text-navy">{stage.data.filename || "Seçilen dosya"}</span>
              <span className="text-muted">
                {stage.data.total_rows} satır okundu · <span className="font-medium text-navy">{stage.data.valid_rows} geçerli</span>
                {stage.data.total_rows - stage.data.valid_rows > 0 && ` · ${stage.data.total_rows - stage.data.valid_rows} atlanacak`}
              </span>
            </div>

            {stage.data.note && <Notice tone="info">{stage.data.note}</Notice>}

            {stage.data.warnings.length > 0 && (
              <Notice tone="warn">
                <ul className="space-y-1">
                  {stage.data.warnings.map((w) => (
                    <li key={w}>{w}</li>
                  ))}
                </ul>
              </Notice>
            )}

            <div>
              <p className="mb-2 text-[12.5px] text-muted">
                İlk {stage.data.preview.length} satır (dosyadaki sütun adı parantez içinde):
              </p>
              <PreviewTable data={stage.data} />
            </div>

            <ErrorList errors={stage.data.errors} total={stage.data.total_rows - stage.data.valid_rows} />
          </>
        )}

        {stage.s === "done" && (
          <div className="space-y-3">
            <Notice tone={stage.result.inserted > 0 ? "success" : "warn"}>
              <span className="flex items-center gap-2">
                <IconCheck size={16} />
                {stage.result.inserted} {KIND_LABEL[stage.result.kind]} kaydı eklendi
                {stage.result.skipped > 0 ? ` · ${stage.result.skipped} satır atlandı.` : "."}
              </span>
            </Notice>
            {stage.result.kind === "sales" && stage.result.inserted > 0 && (
              <p className="text-[13px] text-muted">Stok miktarları değişmedi; içe aktarılan satışlar geçmiş veri sayılır.</p>
            )}
            <ErrorList errors={stage.result.errors} total={stage.result.skipped} />
          </div>
        )}
      </div>
    </Modal>
  );
}
