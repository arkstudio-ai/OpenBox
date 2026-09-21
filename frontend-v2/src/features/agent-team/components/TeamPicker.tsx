import { useState } from "react"
import { Link } from "react-router"
import { useTranslation } from "react-i18next"
import { Check, ChevronDown, Users } from "lucide-react"
import { Menu, MenuItem } from "@/shared/ui/Menu"
import { paths } from "@/shared/router/paths"
import type { TeamRequest } from "@/shared/types/api"
import type { Definition, TeamSpec } from "../types"

export function TeamPicker({
  templates,
  value,
  onChange,
  disabled = false,
}: {
  templates: Definition<TeamSpec>[]
  value: TeamRequest
  onChange: (value: TeamRequest) => void
  disabled?: boolean
}) {
  const { t } = useTranslation("teams")
  const [open, setOpen] = useState(false)
  const selected = templates.find((entry) => entry.id === value.template_id)
  const allow = value.allow_supplement ?? selected?.version.spec.policy.member_selection !== "explicit_only"
  return (
    <div className="relative flex-none">
      <Menu open={open} onClose={() => setOpen(false)} className="start-0 bottom-10 w-72">
        <MenuItem
          onClick={() => {
            onChange({ ...value, template_id: null, allow_supplement: true })
            setOpen(false)
          }}
        >
          <span className="flex items-center gap-2">
            <Users className="size-4" />
            {t("autoTeam")}
          </span>
          <span className="text-n600 mt-1 block text-xs">{t("autoTeamHint")}</span>
        </MenuItem>
        {templates.map((entry) => (
          <MenuItem
            key={entry.id}
            onClick={() => {
              onChange({ ...value, template_id: entry.id, allow_supplement: undefined })
              setOpen(false)
            }}
          >
            <span className="flex items-center gap-2">
              <Check className={`size-3.5 ${entry.id === value.template_id ? "" : "opacity-0"}`} />
              {entry.name}
            </span>
            <span className="text-n600 mt-1 block truncate text-xs">{entry.version.spec.description}</span>
          </MenuItem>
        ))}
        <label className="border-hair mx-2 mt-2 flex cursor-pointer items-start gap-2 border-t py-3 text-sm">
          <input
            type="checkbox"
            checked={allow}
            disabled={!value.template_id && !value.requested_agent_ids?.length}
            onChange={(e) => onChange({ ...value, allow_supplement: e.target.checked })}
            className="accent-accent mt-0.5 size-4"
          />
          <span>
            {t("allowSupplement")}
            <small className="text-n600 mt-1 block">{t("oneRunOnly")}</small>
          </span>
        </label>
        <Link to={paths.agents} className="text-n600 hover:bg-hairsoft block rounded-lg px-3 py-2 text-sm">
          {t("manageTeams")}
        </Link>
      </Menu>
      <button
        type="button"
        aria-label={t("chooseTeam")}
        aria-expanded={open}
        disabled={disabled}
        onClick={() => setOpen(!open)}
        className="text-n600 hover:bg-hairsoft flex h-8 items-center gap-1 rounded-full px-2.5 text-sm disabled:opacity-40"
      >
        <span className="max-w-35 truncate">{selected?.name ?? t("autoTeam")}</span>
        <ChevronDown className="size-3.5" />
      </button>
    </div>
  )
}
