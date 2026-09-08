// One installed group: the row, the reason it is off the shelf, and — for a
// pack — the skills that came with it.
import { useState } from "react"
import { useTranslation } from "react-i18next"
import { ChevronDown, ChevronRight, CloudOff, Download, Trash2, UploadCloud } from "lucide-react"
import type { SkillGroup } from "@/features/skills-center/lib/group-skills"
import type { InstalledSkill } from "@/features/skills-center/types"
import {
  canWithdraw,
  explainsItself,
  isResubmission,
  listingChipFor,
} from "@/features/skills-center/lib/listing"
import { EntryRow, IconButton } from "./EntryRow"
import { SkillGroupBadges } from "./SkillGroupBadges"

export interface SkillGroupActions {
  uninstallSkill: (dir: string, count: number) => void
  fixDependencies: (skill: InstalledSkill) => void
  publishSkill: (group: SkillGroup) => void
  withdrawSkill: (group: SkillGroup) => void
  downloadSkill: (dir: string) => void
  busy: boolean
}

interface Props {
  group: SkillGroup
  unmetFor: (skill: InstalledSkill) => { name: string }[]
  actions: SkillGroupActions
}

export function SkillGroupRow({ group, unmetFor, actions }: Props) {
  const { t } = useTranslation("skills")
  const [membersOpen, setMembersOpen] = useState(false)
  const [reasonOpen, setReasonOpen] = useState(false)

  const missing = [...new Set(group.members.flatMap((member) => unmetFor(member).map((d) => d.name)))]
  const personal = group.category === "personal"
  const chip = personal ? listingChipFor(group.publicationStatus, group.listing) : null
  const reason = chip && explainsItself(chip) ? group.listingNote : undefined
  const publishLabel = isResubmission(chip)
    ? "action.resubmit"
    : group.publicationStatus === "published"
      ? "action.updatePublish"
      : "action.publish"

  return (
    <div>
      <EntryRow
        icon={group.icon}
        name={group.name}
        description={group.isPack ? group.members.map((m) => m.name).join(", ") : group.description}
        warning={missing.length ? t("mine.missingDependency", { names: missing.join(", ") }) : undefined}
        onFixWarning={missing.length ? () => actions.fixDependencies(group.members[0]) : undefined}
        fixLabel={t("deps.fixNow")}
        fixDisabled={actions.busy}
        badges={<SkillGroupBadges group={group} chip={chip} />}
        actions={
          <>
            {personal ? (
              <>
                <IconButton
                  title={t(publishLabel)}
                  disabled={actions.busy}
                  onClick={() => actions.publishSkill(group)}
                >
                  <UploadCloud size={14} />
                </IconButton>
                {canWithdraw(chip) ? (
                  <IconButton
                    title={t("action.withdraw")}
                    disabled={actions.busy}
                    onClick={() => actions.withdrawSkill(group)}
                  >
                    <CloudOff size={14} />
                  </IconButton>
                ) : null}
                <IconButton
                  title={t("action.download")}
                  disabled={actions.busy}
                  onClick={() => actions.downloadSkill(group.id)}
                >
                  <Download size={14} />
                </IconButton>
              </>
            ) : null}
            {group.isPack ? (
              <IconButton
                title={membersOpen ? t("action.collapse") : t("action.expand")}
                onClick={() => setMembersOpen((open) => !open)}
              >
                {membersOpen ? <ChevronDown size={14} /> : <ChevronRight size={14} />}
              </IconButton>
            ) : null}
            {group.removable ? (
              <IconButton
                danger
                title={t("action.uninstall")}
                disabled={actions.busy}
                onClick={() => actions.uninstallSkill(group.id, group.members.length)}
              >
                <Trash2 size={14} />
              </IconButton>
            ) : null}
          </>
        }
      />

      {/* A refusal truncated into a tooltip is a refusal nobody reads. The
          reason gets its own line, in full, one click away. */}
      {reason ? (
        <div className="ms-6 mt-1 ps-3">
          <button
            type="button"
            aria-expanded={reasonOpen}
            onClick={() => setReasonOpen((open) => !open)}
            className="text-n600 hover:text-ink text-xs underline underline-offset-2"
          >
            {t(reasonOpen ? "mine.hideReason" : "mine.showReason")}
          </button>
          {reasonOpen ? (
            <p className="bg-hairsoft/60 text-n700 mt-1 rounded-lg px-3 py-2 text-xs leading-5 whitespace-pre-wrap">
              {t("mine.listingReason", { note: reason })}
            </p>
          ) : null}
        </div>
      ) : null}

      {group.isPack && membersOpen ? (
        <ul className="border-hair ms-6 mt-1 flex flex-col gap-1 border-s ps-3">
          {group.members.map((member) => (
            <li key={member.name} className="flex items-baseline gap-2 py-0.5">
              <span className="text-ink text-xs">{member.name}</span>
              <span className="text-n600 min-w-0 flex-1 truncate text-xs">{member.description}</span>
            </li>
          ))}
        </ul>
      ) : null}
    </div>
  )
}
