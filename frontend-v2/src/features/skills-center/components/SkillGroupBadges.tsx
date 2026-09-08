// The badges one installed group wears, in the order they answer questions:
// what is it, who owns it, where does it stand.
import { useTranslation } from "react-i18next"
import type { SkillGroup } from "@/features/skills-center/lib/group-skills"
import type { ListingChip } from "@/features/skills-center/lib/listing"
import { Badge } from "./EntryRow"
import { ListingBadge } from "./ListingBadge"

export function SkillGroupBadges({ group, chip }: { group: SkillGroup; chip: ListingChip | null }) {
  const { t } = useTranslation("skills")
  const personal = group.category === "personal"

  return (
    <>
      {group.isPack ? <Badge>{t("badge.packCount", { count: group.members.length })}</Badge> : null}
      {personal ? (
        <>
          <Badge>{t("badge.personal")}</Badge>
          {group.isOfficial ? <Badge tone="ok">{t("badge.official")}</Badge> : null}
          {chip ? (
            <ListingBadge chip={chip} note={group.listingNote} />
          ) : (
            // No release to place on a shelf yet — the operator's vocabulary
            // would only confuse a draft nobody has been asked to look at.
            <Badge tone="warn">{t("badge.unpublished")}</Badge>
          )}
        </>
      ) : group.category === "store" ? (
        <Badge>{t("badge.storeInstalled")}</Badge>
      ) : group.origin !== "container" ? (
        <Badge title={t(`badge.${group.origin}Hint`)}>{t(`badge.${group.origin}`)}</Badge>
      ) : null}
    </>
  )
}
