// 技能商店 — the catalogue, shelved by who stands behind each entry.
import { useTranslation } from "react-i18next"
import type { CatalogMcp, CatalogSkill, StoreOrigin } from "@/features/skills-center/types"
import type { StoreEntry, StoreShelves } from "@/features/skills-center/lib/store-sections"
import { StoreEntryRow } from "./StoreEntryRow"

const SECTION_KEYS: Record<StoreOrigin, string> = {
  official: "section.storeOfficial",
  community: "section.storeCommunity",
  third_party: "section.storeThirdParty",
}

export function StoreList({
  shelves,
  onInstallSkill,
  onInstallMcp,
}: {
  shelves: StoreShelves
  onInstallSkill: (entry: CatalogSkill) => void
  onInstallMcp: (entry: CatalogMcp) => void
}) {
  const { t } = useTranslation("skills")

  function install(entry: StoreEntry) {
    if (entry.kind === "mcp") onInstallMcp(entry)
    else onInstallSkill(entry)
  }

  if (shelves.sections.length === 0) {
    // "Nothing here" and "nothing matches what you typed" are different
    // situations: one is the store's problem, the other is a search to widen.
    return (
      <p className="text-n600 py-12 text-center text-sm">
        {t(shelves.total === 0 ? "store.empty" : "store.noMatch")}
      </p>
    )
  }

  return (
    <div className="flex flex-col gap-5">
      {shelves.sections.map((section) => (
        <section key={section.origin}>
          <h2 className="text-n600 mb-2 text-xs font-medium">{t(SECTION_KEYS[section.origin])}</h2>
          <div className="flex flex-col gap-1.5">
            {section.entries.map((entry) => (
              <StoreEntryRow
                key={`${entry.kind}:${entry.id}`}
                entry={entry}
                onInstall={() => install(entry)}
              />
            ))}
          </div>
        </section>
      ))}
    </div>
  )
}
