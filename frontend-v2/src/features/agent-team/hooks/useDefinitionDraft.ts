import { useCallback, useEffect, useRef, useState } from "react"
import { useDefinitionWrite } from "../api/teams"
import type { AgentSpec, Definition, TeamSpec } from "../types"

/** Serialize autosaves and publishing so a late autosave cannot overwrite a
 * newer form value or publish against a revision that has already changed. */
export function useDefinitionDraft<T extends AgentSpec | TeamSpec>(
  kind: "agent" | "team",
  initial: T,
  definition: Definition<T> | undefined,
  valid: (spec: T) => boolean,
) {
  const [spec, setSpec] = useState(initial)
  const [record, setRecord] = useState(definition)
  const [saved, setSaved] = useState(definition ? JSON.stringify(initial) : "")
  const [error, setError] = useState<Error | null>(null)
  const latest = useRef({ spec, record, saved })
  const flight = useRef<Promise<Definition<T>> | null>(null)
  const retry = useRef<{ content: string; key: string } | null>(null)
  const write = useDefinitionWrite<T>(kind)
  const { mutateAsync } = write
  useEffect(() => {
    latest.current = { spec, record, saved }
  }, [spec, record, saved])
  const save = useCallback(async (): Promise<Definition<T>> => {
    if (flight.current) await flight.current
    const current = latest.current
    if (current.record && JSON.stringify(current.spec) === current.saved) return current.record
    const content = JSON.stringify(current.spec)
    if (retry.current?.content !== content) retry.current = { content, key: crypto.randomUUID() }
    const pending = mutateAsync({
      action: current.record ? "draft-version" : "create",
      id: current.record?.id,
      expected_revision: current.record?.revision,
      spec: current.spec,
      key: retry.current.key,
    })
    flight.current = pending
    try {
      const result = await pending
      latest.current = { ...latest.current, record: result, saved: content }
      setRecord(result)
      setSaved(content)
      setError(null)
      retry.current = null
      return result
    } catch (cause) {
      setError(cause instanceof Error ? cause : new Error(String(cause)))
      throw cause
    } finally {
      if (flight.current === pending) flight.current = null
    }
  }, [mutateAsync])
  useEffect(() => {
    if (
      !valid(spec) ||
      JSON.stringify(spec) === saved ||
      definition?.readonly ||
      definition?.status === "archived"
    )
      return
    const timer = window.setTimeout(() => {
      void save().catch(() => undefined)
    }, 900)
    return () => window.clearTimeout(timer)
  }, [spec, saved, valid, save, definition?.readonly, definition?.status])
  const publish = async () => {
    const current = await save()
    const result = await mutateAsync({
      action: "versions",
      id: current.id,
      expected_revision: current.revision,
    })
    setRecord(result)
    latest.current.record = result
    return result
  }
  return {
    spec,
    setSpec,
    record,
    save,
    publish,
    saved: saved === JSON.stringify(spec),
    pending: write.isPending,
    error: error ?? write.error,
    published: record?.status === "active" && !record.draft_version_id && saved === JSON.stringify(spec),
  }
}
