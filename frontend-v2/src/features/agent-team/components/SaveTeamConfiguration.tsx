import { useRef, useState } from "react"
import { Link } from "react-router"
import { useTranslation } from "react-i18next"
import { Dialog, DialogActions, DialogBody, DialogTitle } from "@/shared/ui/Dialog"
import { paths } from "@/shared/router/paths"
import { useSaveTeamConfiguration } from "../api/teams"
import type { TeamMember, TeamSnapshot } from "../types"
import { Field, PRIMARY } from "./FormFields"

export function SaveTeamConfiguration({ team, member }: { team: TeamSnapshot; member?: TeamMember }) {
  const { t } = useTranslation("teams")
  const [open, setOpen] = useState(false)
  return (
    <>
      <button type="button" className="text-a700 text-xs" onClick={() => setOpen(true)}>
        {t(member ? "saveMember" : "saveTemplate")}
      </button>
      {open && <SaveDialog team={team} member={member} onClose={() => setOpen(false)} />}
    </>
  )
}

function SaveDialog({
  team,
  member,
  onClose,
}: {
  team: TeamSnapshot
  member?: TeamMember
  onClose: () => void
}) {
  const { t } = useTranslation("teams")
  const workers = team.members.filter((entry) => entry.role === "member")
  const [name, setName] = useState(member ? member.name : team.run.title.slice(0, 80))
  const [selected, setSelected] = useState(workers.map((entry) => entry.id))
  const [names, setNames] = useState<Record<string, string>>(() =>
    Object.fromEntries(
      workers
        .filter((entry) => !entry.definition_id)
        .map((entry) => [entry.id, `${entry.name.slice(0, 29)} · ${team.id.slice(-6)}`]),
    ),
  )
  const save = useSaveTeamConfiguration(team.id, member?.id)
  const receipt = useRef({ payload: "", key: "" })
  const submit = () => {
    const body = { name: name.trim(), ...(!member ? { member_ids: selected, member_names: names } : {}) }
    const payload = JSON.stringify(body)
    if (receipt.current.payload !== payload) receipt.current = { payload, key: crypto.randomUUID() }
    save.mutate({ ...body, key: receipt.current.key })
  }
  return (
    <Dialog open onClose={onClose} label={t(member ? "saveMember" : "saveTemplate")}>
      <DialogTitle>{t(member ? "saveMember" : "saveTemplate")}</DialogTitle>
      <DialogBody>{t(member ? "saveMemberHint" : "saveTemplateHint")}</DialogBody>
      {save.data ? (
        <Link
          className="text-a700 py-3 text-sm"
          to={member ? paths.agentEditor(save.data.id) : paths.teamEditor(save.data.id)}
          onClick={onClose}
        >
          {t("openSavedConfiguration")}
        </Link>
      ) : (
        <>
          <Field label={t("name")} value={name} onChange={setName} maxLength={member ? 40 : 80} required />
          {!member && (
            <div className="space-y-3">
              {workers.map((entry) => (
                <div key={entry.id} className="border-hair rounded-lg border p-3">
                  <label className="flex items-center gap-2 text-sm">
                    <input
                      type="checkbox"
                      className="accent-ink size-4 rounded-full"
                      checked={selected.includes(entry.id)}
                      onChange={(event) =>
                        setSelected((prior) =>
                          event.target.checked ? [...prior, entry.id] : prior.filter((id) => id !== entry.id),
                        )
                      }
                    />
                    {entry.name}
                  </label>
                  {selected.includes(entry.id) && !entry.definition_id && (
                    <div className="mt-2">
                      <Field
                        label={t("savedMemberName")}
                        value={names[entry.id] ?? ""}
                        maxLength={40}
                        onChange={(value) => setNames((prior) => ({ ...prior, [entry.id]: value }))}
                      />
                    </div>
                  )}
                  <p className="text-n600 mt-2 text-xs">
                    {entry.model} · {entry.responsibility || entry.description}
                  </p>
                </div>
              ))}
            </div>
          )}
          {save.error && (
            <p role="alert" className="text-danger text-xs">
              {save.error.message}
            </p>
          )}
        </>
      )}
      <DialogActions>
        <button type="button" className="text-n600 text-sm" onClick={onClose}>
          {t(save.data ? "close" : "cancel")}
        </button>
        {!save.data && (
          <button
            type="button"
            className={PRIMARY}
            disabled={
              save.isPending ||
              !name.trim() ||
              (!member && (!selected.length || selected.some((id) => id in names && !names[id].trim())))
            }
            onClick={submit}
          >
            {t(member ? "saveDraft" : "saveAndEnable")}
          </button>
        )}
      </DialogActions>
    </Dialog>
  )
}
