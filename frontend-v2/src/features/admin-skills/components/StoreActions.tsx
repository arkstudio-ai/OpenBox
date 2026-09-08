import { useTranslation } from "react-i18next"
import { isCommunityEntry } from "@/features/admin-skills/lib/entry"
import type { StoreEntry } from "@/features/admin-skills/types"

const BUTTON =
  "rounded-full border border-hair px-3 py-1.5 text-xs text-n800 hover:bg-hairsoft disabled:opacity-50"

interface Props {
  entry: StoreEntry
  busy: boolean
  onRelist: (entry: StoreEntry) => void
  onDelist: (entry: StoreEntry) => void
  onFeature: (entry: StoreEntry) => void
  onOfficial: (entry: StoreEntry) => void
  onView: (entry: StoreEntry) => void
}

/** Row actions. Featuring only means something once an entry is on the shelf. */
export function StoreActions({ entry, busy, onRelist, onDelist, onFeature, onOfficial, onView }: Props) {
  const { t } = useTranslation("admin-skills")
  const listed = entry.listing === "listed"
  const community = isCommunityEntry(entry.catalog_id)

  return (
    <div className="flex flex-wrap justify-end gap-1.5">
      {listed ? (
        <button type="button" className={BUTTON} disabled={busy} onClick={() => onDelist(entry)}>
          {t("action.delist")}
        </button>
      ) : (
        <button type="button" className={BUTTON} disabled={busy} onClick={() => onRelist(entry)}>
          {t("action.list")}
        </button>
      )}
      <button type="button" className={BUTTON} disabled={busy || !listed} onClick={() => onFeature(entry)}>
        {entry.featured ? t("action.unfeature") : t("action.feature")}
      </button>
      {community && (
        <>
          <button type="button" className={BUTTON} disabled={busy} onClick={() => onOfficial(entry)}>
            {entry.is_official ? t("action.unmarkOfficial") : t("action.markOfficial")}
          </button>
          {/* Only community rows have a submitted archive to look inside. */}
          <button type="button" className={BUTTON} onClick={() => onView(entry)}>
            {t("action.view")}
          </button>
        </>
      )}
    </div>
  )
}
