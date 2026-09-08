// The one chip that says where an author's release stands.
import { useTranslation } from "react-i18next"
import { LISTING_TONES, type ListingChip } from "@/features/skills-center/lib/listing"
import { Badge } from "./EntryRow"

export function ListingBadge({ chip, note }: { chip: ListingChip; note?: string }) {
  const { t } = useTranslation("skills")
  // The reason also hangs off the chip as a tooltip, for the hover that
  // precedes the click; the expandable line below the row is what makes a
  // long reason actually readable.
  return (
    <Badge tone={LISTING_TONES[chip]} title={note}>
      {t(`badge.listing.${chip}`)}
    </Badge>
  )
}
