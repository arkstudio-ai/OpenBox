import { useState } from "react"
import { useTranslation } from "react-i18next"
import { toast } from "@/shared/ui/Toast"
import { useApiErrorMessage } from "@/shared/hooks/useApiErrorMessage"
import { paths } from "@/shared/router/paths"
import {
  useChangeMemberRole,
  useCurrentWorkspaceQuery,
  useInviteMember,
  usePendingInvitationsQuery,
  useRemoveMember,
} from "@/shared/api/workspaces"

export function TeamPage() {
  const { t } = useTranslation("settings")
  const errorMessage = useApiErrorMessage()
  const current = useCurrentWorkspaceQuery()
  const pending = usePendingInvitationsQuery()
  const invite = useInviteMember()
  const changeRole = useChangeMemberRole()
  const remove = useRemoveMember()
  const [target, setTarget] = useState("")
  const [role, setRole] = useState<"admin" | "member">("member")
  const [inviteUrl, setInviteUrl] = useState("")
  const canManage = current.data?.role === "owner" || current.data?.role === "admin"

  const submit = (event: React.FormEvent) => {
    event.preventDefault()
    const normalized = target.trim()
    if (!normalized) return
    invite.mutate(
      { target: normalized, role },
      {
        onSuccess: (result) => {
          setTarget("")
          const url = `${window.location.origin}${paths.invite(result.token)}`
          setInviteUrl(url)
          void navigator.clipboard?.writeText(url)
          toast("success", t("team.inviteCreated", { url }))
        },
        onError: (error) => toast("error", errorMessage(error)),
      },
    )
  }

  return (
    <div className="flex flex-col gap-5">
      {canManage && (
        <form onSubmit={submit} className="flex flex-wrap gap-2 rounded-2xl border border-hair bg-panel p-4">
          <input
            value={target}
            onChange={(event) => setTarget(event.target.value)}
            placeholder={t("team.targetPlaceholder")}
            className="min-w-52 flex-1 rounded-full border border-hair bg-bg px-4 py-2 text-sm outline-none"
          />
          <select
            value={role}
            onChange={(event) => setRole(event.target.value as "admin" | "member")}
            className="rounded-full border border-hair bg-bg px-4 py-2 text-sm"
          >
            <option value="member">{t("team.roles.member")}</option>
            <option value="admin">{t("team.roles.admin")}</option>
          </select>
          <button
            type="submit"
            disabled={invite.isPending}
            className="rounded-full bg-ink px-5 py-2 text-sm text-bg disabled:opacity-50"
          >
            {t("team.invite")}
          </button>
        </form>
      )}
      {inviteUrl && (
        <input
          readOnly
          value={inviteUrl}
          aria-label={t("team.inviteLink")}
          onFocus={(event) => event.currentTarget.select()}
          className="rounded-full border border-hair bg-bg px-4 py-2 text-sm outline-none"
        />
      )}

      <section className="overflow-hidden rounded-2xl border border-hair bg-panel">
        <h2 className="border-b border-hair px-4 py-3 text-sm font-medium">{t("team.members")}</h2>
        {(current.data?.members ?? []).map((member) => (
          <div key={member.user_id} className="flex items-center gap-3 border-b border-hair px-4 py-3 last:border-0">
            <div className="min-w-0 flex-1">
              <div className="truncate text-sm text-ink">{member.username}</div>
              <div className="truncate text-xs text-n600">{member.email || t("account.emailNone")}</div>
              <div className="text-xs text-n600">
                {t("team.joined", { date: new Date(member.created_at).toLocaleDateString() })}
              </div>
            </div>
            <span className="text-xs text-n600">{t(`team.statuses.${member.status}`)}</span>
            {canManage && member.role !== "owner" ? (
              <>
                <select
                  value={member.role}
                  onChange={(event) =>
                    changeRole.mutate({
                      userId: member.user_id,
                      role: event.target.value as "admin" | "member",
                    })
                  }
                  className="rounded-full border border-hair bg-bg px-3 py-1.5 text-xs"
                >
                  <option value="member">{t("team.roles.member")}</option>
                  <option value="admin">{t("team.roles.admin")}</option>
                </select>
                <button
                  type="button"
                  onClick={() => remove.mutate(member.user_id)}
                  className="rounded-full px-3 py-1.5 text-xs text-red-600 hover:bg-hairsoft"
                >
                  {t("team.remove")}
                </button>
              </>
            ) : (
              <span className="rounded-full bg-hairsoft px-3 py-1.5 text-xs">
                {t(`team.roles.${member.role}`)}
              </span>
            )}
          </div>
        ))}
      </section>

      <section className="overflow-hidden rounded-2xl border border-hair bg-panel">
        <h2 className="border-b border-hair px-4 py-3 text-sm font-medium">{t("team.pending")}</h2>
        {(pending.data?.items ?? []).length === 0 ? (
          <p className="px-4 py-3 text-sm text-n600">{t("team.pendingEmpty")}</p>
        ) : (
          pending.data?.items.map((item) => (
            <div key={item.id} className="flex items-center gap-3 border-b border-hair px-4 py-3 last:border-0">
              <span className="min-w-0 flex-1 truncate text-sm">{item.workspace_name}</span>
              <span className="text-xs text-n600">{t(`team.roles.${item.role}`)}</span>
              <span className="text-xs text-n600">{item.target}</span>
            </div>
          ))
        )}
      </section>
    </div>
  )
}
