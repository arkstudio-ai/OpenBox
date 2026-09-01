// Controller for the composer's @ / mention menu. Owns trigger detection,
// section building, keyboard nav and selection→text replacement. Keeps no DOM
// layout math: the menu renders in-flow above the input (see MentionMenu.tsx).
//
// Wiring (done by the Composer that owns the textarea):
//   const mention = useMentionMenu({ text, caret, textareaRef, containerId, onReplace })
//   - `text`         current textarea value (the Composer's state)
//   - `caret`        current caret index — read taRef.selectionStart on
//                    change / keyup / click / select and keep it in state
//   - `textareaRef`  the same ref passed to <textarea>
//   - `containerId`  running sandbox id, or null (from useRunningContainer)
//   - `onReplace`    (nextText, nextCaret) => void — set the Composer's text;
//                    the hook also restores the caret via setSelectionRange
//
//   In the textarea onKeyDown, call mention.onKeyDown(e) FIRST and `return`
//   when it yields true (the menu consumed the key), before the Enter-to-send
//   branch. Render <MentionMenu> inside a `relative` wrapper around the input
//   row when mention.open is true.
import { useCallback, useEffect, useMemo, useState } from "react"
import type { KeyboardEvent, RefObject } from "react"
import { resolveTrigger, replaceTrigger } from "../lib/mention"
import { useCommands, useFileSearch, useSkills } from "../api/mention"
import type { AttachableResource } from "../lib/resource-ref"

export type MentionItemKind = "resource" | "file" | "skill" | "command"

export interface MentionItem {
  id: string
  kind: MentionItemKind
  /** Display label (a full path for files — shortened by the menu). */
  label: string
  description?: string
  /** Text spliced in at the trigger span when the item is chosen. */
  insert: string
  /** Set on resource items: picking one attaches the file instead of typing
   *  anything, so the trigger span is simply removed. */
  resource?: AttachableResource
}

export interface MentionSection {
  kind: MentionItemKind
  loading: boolean
  needSandbox?: boolean
  items: MentionItem[]
}

/** The resource-centre slice the menu shows, supplied by the route (§4.2). */
export interface MentionScope {
  projects: Array<{ id: string; name: string }>
  project: string
  setProject: (id: string) => void
  source: "all" | "user" | "agent"
  setSource: (source: "all" | "user" | "agent") => void
  items: AttachableResource[]
  loading: boolean
}

interface Args {
  text: string
  caret: number
  textareaRef: RefObject<HTMLTextAreaElement | null>
  containerId: string | null
  sessionId?: string
  projectId?: string
  onReplace: (nextText: string, nextCaret: number) => void
  /** Resource centre, when the surrounding route wired one in. */
  scope?: MentionScope
  /** Called instead of inserting text when a resource is chosen. */
  onPickResource?: (resource: AttachableResource) => void
}

function matches(query: string, ...fields: Array<string | undefined>): boolean {
  const q = query.trim().toLowerCase()
  if (!q) return true
  return fields.filter(Boolean).join(" ").toLowerCase().includes(q)
}

export function useMentionMenu({
  text,
  caret,
  textareaRef,
  containerId,
  sessionId,
  projectId,
  onReplace,
  scope,
  onPickResource,
}: Args) {
  const trigger = useMemo(() => resolveTrigger(text, caret), [text, caret])
  const kind = trigger?.kind ?? null
  const query = trigger?.query ?? ""
  const triggerKey = trigger ? `${trigger.kind}:${trigger.start}:${trigger.query}` : null

  const [dismissed, setDismissed] = useState<string | null>(null)
  const [activeIndex, setActiveIndex] = useState(0)

  // Debounce the file query so every keystroke doesn't spawn a request.
  const [debouncedQuery, setDebouncedQuery] = useState(query)
  useEffect(() => {
    const id = window.setTimeout(() => setDebouncedQuery(query), 160)
    return () => window.clearTimeout(id)
  }, [query])

  const fileEnabled = kind === "at" && containerId !== null && debouncedQuery.trim().length > 0
  const fileSearch = useFileSearch({
    containerId,
    query: debouncedQuery.trim(),
    enabled: fileEnabled,
    sessionId,
    projectId,
  })
  const skills = useSkills()
  const commands = useCommands()

  const resourceItems = useMemo<MentionItem[]>(
    () =>
      (scope?.items ?? [])
        .filter((r) => matches(query, r.name))
        .slice(0, 40)
        .map((r) => ({
          id: `resource:${r.id}`,
          kind: "resource" as const,
          label: r.name,
          insert: "",
          resource: r,
        })),
    [scope?.items, query],
  )

  const fileItems = useMemo<MentionItem[]>(
    () =>
      (fileSearch.data?.files ?? []).map((path) => ({
        id: `file:${path}`,
        kind: "file",
        label: path,
        insert: `@${path}`,
      })),
    [fileSearch.data],
  )

  const skillItems = useMemo<MentionItem[]>(
    () =>
      (skills.data ?? [])
        .filter((s): s is { name: string; description?: string } => Boolean(s.name))
        .filter((s) => matches(query, s.name, s.description))
        .map((s) => ({
          id: `skill:${s.name}`,
          kind: "skill",
          label: s.name,
          description: s.description,
          insert: `@skill:${s.name}`,
        })),
    [skills.data, query],
  )

  const commandItems = useMemo<MentionItem[]>(
    () =>
      (commands.data ?? [])
        .filter((c): c is { name: string; description?: string } => Boolean(c.name))
        .filter((c) => matches(query, c.name, c.description))
        .map((c) => ({
          id: `command:${c.name}`,
          kind: "command",
          label: c.name,
          description: c.description,
          insert: `/${c.name} `,
        })),
    [commands.data, query],
  )

  const sections = useMemo<MentionSection[]>(() => {
    // The resource centre leads the "@" menu: referencing a file someone
    // already has beats searching the sandbox for one, and it is the only
    // source that survives the container being recycled.
    const resourceSection: MentionSection[] = scope
      ? [{ kind: "resource", loading: scope.loading, items: resourceItems }]
      : []
    if (kind === "at") {
      return [
        ...resourceSection,
        {
          kind: "file",
          loading: fileEnabled && fileSearch.isFetching,
          needSandbox: containerId === null,
          items: containerId === null ? [] : fileItems,
        },
        { kind: "skill", loading: skills.isLoading, items: skillItems },
      ]
    }
    if (kind === "slash") {
      return [
        { kind: "command", loading: commands.isLoading, items: commandItems },
        ...resourceSection,
        { kind: "skill", loading: skills.isLoading, items: skillItems },
      ]
    }
    return []
  }, [
    kind,
    scope,
    resourceItems,
    fileEnabled,
    fileSearch.isFetching,
    containerId,
    fileItems,
    skills.isLoading,
    skillItems,
    commands.isLoading,
    commandItems,
  ])

  const flat = useMemo(() => sections.flatMap((s) => s.items), [sections])
  const open = trigger !== null && dismissed !== triggerKey

  // Reset the highlight to the top whenever the trigger/query changes — done
  // during render (the "adjust state on prop change" pattern) rather than in an
  // effect, so there's no extra commit.
  const [lastKey, setLastKey] = useState(triggerKey)
  if (triggerKey !== lastKey) {
    setLastKey(triggerKey)
    setActiveIndex(0)
  }
  // Clamp on read so a shrinking list never points past the end.
  const clampedIndex = flat.length === 0 ? 0 : Math.min(activeIndex, flat.length - 1)

  const close = useCallback(() => setDismissed(triggerKey), [triggerKey])

  const select = useCallback(
    (item: MentionItem) => {
      if (!trigger) return
      // A resource is attached, not typed: drop the trigger span entirely
      // (replaceTrigger always leaves a trailing space, which would strand one
      // in the middle of a sentence) and hand the file to the composer.
      const next = item.resource
        ? {
            text: text.slice(0, trigger.start) + text.slice(trigger.end),
            caret: trigger.start,
          }
        : replaceTrigger(text, { start: trigger.start, end: trigger.end }, item.insert)
      if (item.resource) onPickResource?.(item.resource)
      onReplace(next.text, next.caret)
      setDismissed(null)
      window.requestAnimationFrame(() => {
        const ta = textareaRef.current
        ta?.focus()
        ta?.setSelectionRange(next.caret, next.caret)
      })
    },
    [trigger, text, onReplace, textareaRef, onPickResource],
  )

  const onKeyDown = useCallback(
    (e: KeyboardEvent<HTMLTextAreaElement>): boolean => {
      if (!open) return false
      if (e.key === "ArrowDown") {
        if (flat.length === 0) return true
        e.preventDefault()
        setActiveIndex((i) => (i + 1) % flat.length)
        return true
      }
      if (e.key === "ArrowUp") {
        if (flat.length === 0) return true
        e.preventDefault()
        setActiveIndex((i) => (i - 1 + flat.length) % flat.length)
        return true
      }
      if (e.key === "Enter" || e.key === "Tab") {
        const item = flat[clampedIndex]
        if (!item) return false
        e.preventDefault()
        select(item)
        return true
      }
      if (e.key === "Escape") {
        e.preventDefault()
        close()
        return true
      }
      return false
    },
    [open, flat, clampedIndex, select, close],
  )

  return { open, kind, sections, activeIndex: clampedIndex, setActiveIndex, onKeyDown, select, close }
}
