import { useTranslation } from "react-i18next"
import { cn } from "@/shared/lib/cn"
import type { TeamMember, TeamSnapshot } from "../types"
import { AgentAvatar } from "./AgentAvatar"

interface Props {
  team: TeamSnapshot
  selected: string | null
  onSelect: (id: string) => void
  onMessages: (pair: string) => void
}

function MemberNode({
  member,
  selected,
  onSelect,
}: {
  member: TeamMember
  selected: string | null
  onSelect: Props["onSelect"]
}) {
  const { t } = useTranslation("teams")
  return (
    <button
      type="button"
      aria-pressed={selected === member.id}
      onClick={() => onSelect(member.id)}
      className={cn(
        "bg-card focus-visible:outline-accent flex w-full min-w-0 flex-col items-center gap-1.5 rounded-xl border p-3 text-center focus-visible:outline-2 focus-visible:outline-offset-2",
        selected === member.id ? "border-accent" : "border-hair hover:border-n500",
        member.membership_state !== "active" && "opacity-60",
      )}
    >
      <AgentAvatar display={member.display} className="size-8 rounded-full" />
      <strong className="max-w-full truncate text-xs">{member.name}</strong>
      <span className="text-n600 text-2xs">
        {t(`state.${member.membership_state === "retired" ? "retired" : member.execution_state}`)}
      </span>
    </button>
  )
}

export function TeamRosterGraph({ team, selected, onSelect, onMessages }: Props) {
  const { t } = useTranslation("teams")
  const workers = team.members.filter((member) => member.role !== "coordinator")
  const height = 160 + Math.max(1, Math.ceil(workers.length / 3)) * 180
  const positions = new Map(
    team.members.map((member) => {
      const index = workers.findIndex((entry) => entry.id === member.id)
      return [
        member.id,
        member.role === "coordinator"
          ? { x: 300, y: 60 }
          : { x: 100 + (index % 3) * 200, y: 240 + Math.floor(index / 3) * 180 },
      ]
    }),
  )
  const links = team.links.map((link) => {
    const start = positions.get(link.from)!
    const end = positions.get(link.to)!
    const offset = link.kind === "message" ? 65 : 0
    const center = { x: (start.x + end.x) / 2 + offset, y: (start.y + end.y) / 2 }
    return {
      ...link,
      start,
      end,
      center,
      label: t(`link.${link.kind}`, {
        from: team.members.find((member) => member.id === link.from)?.name,
        to: team.members.find((member) => member.id === link.to)?.name,
        count: link.count,
      }),
    }
  })
  const activate = (link: TeamSnapshot["links"][number]) => onMessages(`${link.from},${link.to}`)
  return (
    <div className="@container my-4">
      <figure className="relative hidden @min-[400px]:block" style={{ aspectRatio: `600 / ${height}` }}>
        <figcaption className="sr-only">{t("rosterGraphHint")}</figcaption>
        <svg
          viewBox={`0 0 600 ${height}`}
          className="text-n500 absolute inset-0 size-full"
          aria-hidden="true"
        >
          {links.map((link) => (
            <path
              key={`${link.kind}:${link.from}:${link.to}`}
              d={`M ${link.start.x} ${link.start.y} Q ${link.center.x} ${link.center.y} ${link.end.x} ${link.end.y}`}
              fill="none"
              stroke="currentColor"
              strokeWidth="1.5"
              strokeDasharray={link.kind === "message" ? "5 5" : undefined}
            />
          ))}
        </svg>
        {links.map((link) => (
          <button
            key={`${link.kind}:${link.from}:${link.to}`}
            type="button"
            aria-label={link.label}
            title={link.label}
            onClick={() => activate(link)}
            style={{
              left: `${(link.start.x + 2 * link.center.x + link.end.x) / 4 / 6}%`,
              top: `${(link.center.y / height) * 100}%`,
            }}
            className={cn(
              "bg-card text-n700 border-hair text-2xs focus-visible:outline-accent absolute z-10 flex min-h-6 min-w-6 -translate-x-1/2 -translate-y-1/2 items-center justify-center rounded-full border px-1.5 focus-visible:outline-2",
              link.kind === "message" && "border-dashed",
            )}
          >
            {link.kind === "message" ? "↔ " : "↓ "}
            {link.count}
          </button>
        ))}
        {team.members.map((member) => (
          <div
            key={member.id}
            className="absolute z-20 w-[28%] -translate-x-1/2 -translate-y-1/2"
            style={{
              left: `${positions.get(member.id)!.x / 6}%`,
              top: `${(positions.get(member.id)!.y / height) * 100}%`,
            }}
          >
            <MemberNode member={member} selected={selected} onSelect={onSelect} />
          </div>
        ))}
      </figure>
      <div className="space-y-3 @min-[400px]:hidden">
        <div className="grid grid-cols-2 gap-2">
          {team.members.map((member) => (
            <MemberNode key={member.id} member={member} selected={selected} onSelect={onSelect} />
          ))}
        </div>
        <div className="flex flex-wrap gap-2">
          {links.map((link) => (
            <button
              key={`${link.kind}:${link.from}:${link.to}`}
              type="button"
              onClick={() => activate(link)}
              className={cn(
                "border-hair text-n600 hover:bg-hairsoft min-h-11 rounded-full border px-2.5 py-2 text-xs",
                link.kind === "message" && "border-dashed",
              )}
            >
              {link.label}
            </button>
          ))}
        </div>
      </div>
    </div>
  )
}
