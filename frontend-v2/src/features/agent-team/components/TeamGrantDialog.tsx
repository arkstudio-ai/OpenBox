import { useRef, useState } from "react"
import { useTranslation } from "react-i18next"
import { Dialog, DialogActions, DialogTitle } from "@/shared/ui/Dialog"
import { useTeamGrant } from "../api/teams"
import type { TeamSnapshot } from "../types"
import { Field, PRIMARY } from "./FormFields"
import { PermissionScopes } from "./PermissionScopes"

export function TeamGrantControl({ team }: { team: TeamSnapshot }) {
  const { t } = useTranslation("teams")
  const [open, setOpen] = useState(false)
  return (
    <>
      <button type="button" className="text-a700 text-xs" onClick={() => setOpen(true)}>
        {t("editGrant")}
      </button>
      {open && <GrantDialog team={team} onClose={() => setOpen(false)} />}
    </>
  )
}

function GrantDialog({ team, onClose }: { team: TeamSnapshot; onClose: () => void }) {
  const { t } = useTranslation("teams")
  // Keep the reviewed version stable while events update the surrounding panel.
  const [revision] = useState(team.run.revision)
  const [policy, setPolicy] = useState(team.policy)
  const save = useTeamGrant(team.id)
  const receipt = useRef({ payload: "", key: "" })
  const valid = (policy.permission_rules ?? []).every((rule) => rule.pattern.trim())
  const submit = () => {
    const body = {
      expected_revision: revision,
      paid_tools: policy.paid_tools,
      permission_rules: policy.permission_rules ?? [],
      max_coordinator_turns: policy.max_coordinator_turns,
      max_wall_time_seconds: policy.max_wall_time_seconds,
    }
    const payload = JSON.stringify(body)
    if (receipt.current.payload !== payload) receipt.current = { payload, key: crypto.randomUUID() }
    save.mutate({ ...body, key: receipt.current.key })
  }
  return (
    <Dialog open wide onClose={onClose} label={t("editGrant")}>
      <DialogTitle>{t("editGrant")}</DialogTitle>
      <p className="text-n600 text-sm">{t("editGrantHint")}</p>
      <form
        onSubmit={(event) => {
          event.preventDefault()
          submit()
        }}
      >
        {save.isSuccess ? (
          <p role="status" className="text-a700 py-4 text-sm">
            {t("grantSaved")}
          </p>
        ) : (
          <fieldset disabled={save.isPending} className="space-y-4">
            <div className="grid gap-4 sm:grid-cols-2">
              <Field
                label={t("coordinatorTurnLimit")}
                type="number"
                min={1}
                max={500}
                required
                value={policy.max_coordinator_turns}
                onChange={(value) => setPolicy({ ...policy, max_coordinator_turns: Number(value) })}
              />
              <Field
                label={t("wallTimeLimit")}
                type="number"
                min={60}
                max={86400}
                required
                value={policy.max_wall_time_seconds}
                onChange={(value) => setPolicy({ ...policy, max_wall_time_seconds: Number(value) })}
              />
            </div>
            <PermissionScopes
              policy={policy}
              onChange={(rules) => setPolicy({ ...policy, permission_rules: rules })}
            />
          </fieldset>
        )}
        {save.error && (
          <p role="alert" className="text-danger mt-3 text-xs">
            {save.error.message}
          </p>
        )}
        <DialogActions>
          <button type="button" onClick={onClose} className="text-n600 text-sm">
            {t(save.isSuccess ? "close" : "cancel")}
          </button>
          {!save.isSuccess && (
            <button type="submit" className={PRIMARY} disabled={save.isPending || !valid}>
              {t("saveGrant")}
            </button>
          )}
        </DialogActions>
      </form>
    </Dialog>
  )
}
