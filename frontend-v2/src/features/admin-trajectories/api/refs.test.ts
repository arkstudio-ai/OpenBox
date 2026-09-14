// Content references (`$ref`): recognising them, putting read content in place
// without disturbing anything else, bounding what one value may pull in, and
// reading each digest through the protected blob endpoint at most once.
import { QueryClient } from "@tanstack/react-query"
import { afterEach, describe, expect, it, vi } from "vitest"
import { ApiError } from "@/shared/api/http"
import { isRefEnvelope } from "../types/protocol"
import { trajectoryApi } from "./endpoints"
import { trajectoryKeys } from "./keys"
import {
  blobQuery,
  containsRefs,
  loadBlob,
  MAX_BLOB_READS,
  MAX_REF_NESTING,
  MAX_REFS,
  reachableRefs,
  resolveTree,
  resolveValue,
  UnresolvedContentError,
  type BlobAnswer,
} from "./refs"

vi.mock("./endpoints", () => ({ trajectoryApi: { blob: vi.fn() } }))

const blob = vi.mocked(trajectoryApi.blob)
const TARGET = { viewer: "admin-a#0", sessionId: "ses_b", throughSeq: "12" }

const sha = (n: number) => n.toString(16).padStart(64, "0")
const ref = (digest: string, kind = "value") => ({
  $ref: { sha256: digest, size_bytes: 2048, media_type: "application/json", kind, payload_id: "pld_1" },
})
const available = (digest: string, value: unknown): BlobAnswer => ({
  sha256: digest,
  availability: "available",
  value,
})
const lookupOf = (answers: readonly BlobAnswer[]) => {
  const byDigest = new Map(answers.map((answer) => [answer.sha256, answer]))
  return (digest: string) => byDigest.get(digest)
}

afterEach(() => vi.resetAllMocks())

describe("reference envelopes", () => {
  it("recognises exactly {$ref: {sha256}} and leaves look-alikes as captured data", () => {
    expect(isRefEnvelope(ref(sha(1)))).toBe(true)
    expect(isRefEnvelope({ $ref: { sha256: sha(1).replace(/0/g, "A") } })).toBe(true)
    // A JSON Schema reference inside a tool definition is data, not content to read.
    expect(isRefEnvelope({ $ref: "#/$defs/Path" })).toBe(false)
    expect(isRefEnvelope({ $ref: { sha256: sha(1) }, description: "schema" })).toBe(false)
    expect(isRefEnvelope({ $ref: { sha256: "abc123" } })).toBe(false)
    expect(isRefEnvelope([ref(sha(1))])).toBe(false)
    expect(isRefEnvelope(null)).toBe(false)
    expect(containsRefs({ tools: [{ parameters: { $ref: "#/$defs/Path" } }] })).toBe(false)
    expect(containsRefs({ input: { messages: [{ role: "user" }, ref(sha(2))] } })).toBe(true)
  })
})

describe("resolving a value", () => {
  it("returns a value without references as the very same object", () => {
    const value = { input: { system: "Be precise", messages: [{ role: "user", content: "hi" }] } }
    const tree = resolveTree(value, () => undefined)
    expect(tree.value).toBe(value)
    expect(tree).toMatchObject({ refs: [], missing: [], unavailable: null, overflow: false })
  })

  it("puts read content in place and shares every container it did not touch", () => {
    const tools = [{ name: "read" }]
    const inline = { role: "user", content: "inline" }
    const value = { system: ref(sha(1), "system"), tools, messages: [ref(sha(2), "message"), inline] }
    const tree = resolveTree(
      value,
      lookupOf([available(sha(1), "Be precise"), available(sha(2), { role: "assistant", content: "ok" })]),
    )
    expect(tree.value).toEqual({
      system: "Be precise",
      tools,
      messages: [{ role: "assistant", content: "ok" }, inline],
    })
    const resolved = tree.value as typeof value
    expect(resolved.tools).toBe(tools)
    expect(resolved.messages[1]).toBe(inline)
    expect(value.system).toEqual(ref(sha(1), "system"))
    expect(tree.refs).toEqual([sha(1), sha(2)])
  })

  it("follows references inside read content and reports what is still unread", () => {
    const value = { input: ref(sha(1)) }
    const outer = available(sha(1), { messages: [ref(sha(2)), ref(sha(3))] })
    expect(reachableRefs(value, lookupOf([]))).toEqual([sha(1)])
    expect(reachableRefs(value, lookupOf([outer]))).toEqual([sha(1), sha(2), sha(3)])
    const partial = resolveTree(value, lookupOf([outer, available(sha(2), "second")]))
    expect(partial.missing).toEqual([sha(3)])
    const done = resolveTree(
      value,
      lookupOf([outer, available(sha(2), "second"), available(sha(3), "third")]),
    )
    expect(done).toMatchObject({ value: { input: { messages: ["second", "third"] } }, missing: [] })
    // The same digest twice is read once and shared.
    const twice = resolveTree([ref(sha(1)), ref(sha(1))], lookupOf([available(sha(1), { a: 1 })]))
    expect(twice.refs).toEqual([sha(1)])
    expect((twice.value as unknown[])[0]).toBe((twice.value as unknown[])[1])
  })

  it("reports the most severe reference without content", () => {
    const value = [ref(sha(1)), ref(sha(2)), ref(sha(3))]
    const tree = resolveTree(
      value,
      lookupOf([
        { sha256: sha(1), availability: "pending" },
        { sha256: sha(2), availability: "deleted" },
        { sha256: sha(3), availability: "corrupt" },
      ]),
    )
    expect(tree.unavailable).toBe("deleted")
    expect(tree.value).toEqual(value)
    const pending = resolveTree([ref(sha(1))], lookupOf([{ sha256: sha(1), availability: "pending" }]))
    expect(pending.unavailable).toBe("pending")
  })

  it("refuses references that loop, nest too deeply or are too many", () => {
    const looping = resolveTree(ref(sha(1)), lookupOf([available(sha(1), { again: ref(sha(1)) })]))
    expect(looping.overflow).toBe(true)

    const chain = (length: number) => [
      ...Array.from({ length: length - 1 }, (_, index) => available(sha(index + 1), ref(sha(index + 2)))),
      available(sha(length), "end"),
    ]
    expect(resolveTree(ref(sha(1)), lookupOf(chain(MAX_REF_NESTING)))).toMatchObject({
      value: "end",
      overflow: false,
    })
    expect(resolveTree(ref(sha(1)), lookupOf(chain(MAX_REF_NESTING + 1))).overflow).toBe(true)

    const many = Array.from({ length: MAX_REFS + 1 }, (_, index) => ref(sha(index + 1)))
    expect(resolveTree(many, () => undefined).overflow).toBe(true)
  })
})

describe("reading a reference", () => {
  it("answers 404, 410 and 409 without content and passes refusals and failures on", async () => {
    blob.mockResolvedValueOnce({ role: "user", content: "hi" })
    await expect(loadBlob("ses_b", sha(1), "12")).resolves.toEqual(
      available(sha(1), { role: "user", content: "hi" }),
    )
    expect(blob).toHaveBeenCalledWith("ses_b", sha(1), "12", undefined)
    for (const [status, availability] of [
      [404, "pending"],
      [410, "deleted"],
      [409, "corrupt"],
    ] as const) {
      blob.mockRejectedValueOnce(new ApiError(status, `HTTP_${status}`, "unavailable"))
      await expect(loadBlob("ses_b", sha(2), "12")).resolves.toEqual({ sha256: sha(2), availability })
    }
    blob.mockRejectedValueOnce(new ApiError(403, "HTTP_403", "Forbidden"))
    await expect(loadBlob("ses_b", sha(3), "12")).rejects.toMatchObject({ status: 403 })
    blob.mockRejectedValueOnce(new TypeError("offline"))
    await expect(loadBlob("ses_b", sha(3), "12")).rejects.toBeInstanceOf(TypeError)
  })

  it("keeps a bounded number of reads in flight and lets an aborted waiter go", async () => {
    const releases: Array<() => void> = []
    blob.mockImplementation(() => new Promise((resolve) => releases.push(() => resolve("ok"))))
    const reads = Array.from({ length: MAX_BLOB_READS + 1 }, (_, index) =>
      loadBlob("ses_b", sha(index + 1), "12"),
    )
    const controller = new AbortController()
    const abandoned = loadBlob("ses_b", sha(99), "12", controller.signal)
    await vi.waitFor(() => expect(blob).toHaveBeenCalledTimes(MAX_BLOB_READS))
    controller.abort()
    await expect(abandoned).rejects.toMatchObject({ name: "AbortError" })

    releases[0]()
    await vi.waitFor(() => expect(blob).toHaveBeenCalledTimes(MAX_BLOB_READS + 1))
    for (const release of releases.slice(1)) release()
    await expect(Promise.all(reads)).resolves.toHaveLength(MAX_BLOB_READS + 1)
    expect(blob.mock.calls.map(([, digest]) => digest)).not.toContain(sha(99))
  })

  it("caches content by digest for good and asks again about an answer without content", () => {
    const options = blobQuery(TARGET, sha(1))
    expect(options.queryKey).toEqual(trajectoryKeys.blob("admin-a#0", "ses_b", sha(1)))
    const staleTime = options.staleTime as unknown as (query: { state: { data?: BlobAnswer } }) => number
    expect(staleTime({ state: { data: available(sha(1), "x") } })).toBe(Infinity)
    expect(staleTime({ state: { data: { sha256: sha(1), availability: "pending" } } })).toBe(0)
    expect(staleTime({ state: {} })).toBe(0)
  })
})

describe("resolving for copy and save", () => {
  it("reads what the cache lacks round by round, never twice, and fails on missing content", async () => {
    const content: Record<string, unknown> = { [sha(1)]: { messages: [ref(sha(2))] }, [sha(2)]: "hello" }
    blob.mockImplementation(async (_sid, digest) => content[digest])
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })

    await expect(resolveValue(client, TARGET, { input: ref(sha(1)) })).resolves.toEqual({
      input: { messages: ["hello"] },
    })
    expect(blob.mock.calls.map(([sid, digest, seq]) => [sid, digest, seq])).toEqual([
      ["ses_b", sha(1), "12"],
      ["ses_b", sha(2), "12"],
    ])
    await expect(resolveValue(client, TARGET, [ref(sha(2))])).resolves.toEqual(["hello"])
    expect(blob).toHaveBeenCalledTimes(2)

    blob.mockRejectedValueOnce(new ApiError(410, "trajectory_content_deleted", "deleted"))
    const failure = await resolveValue(client, TARGET, ref(sha(3))).catch((error: unknown) => error)
    expect(failure).toBeInstanceOf(UnresolvedContentError)
    expect((failure as UnresolvedContentError).availability).toBe("deleted")
    client.clear()
  })

  it("asks once more about a cached answer without content before giving up", async () => {
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    const key = (digest: string) => trajectoryKeys.blob(TARGET.viewer, TARGET.sessionId, digest)
    client.setQueryData<BlobAnswer>(key(sha(4)), { sha256: sha(4), availability: "pending" })
    blob.mockResolvedValueOnce("visible now")
    await expect(resolveValue(client, TARGET, [ref(sha(4))])).resolves.toEqual(["visible now"])
    expect(blob).toHaveBeenCalledTimes(1)

    client.setQueryData<BlobAnswer>(key(sha(5)), { sha256: sha(5), availability: "pending" })
    blob.mockRejectedValueOnce(new ApiError(410, "trajectory_content_deleted", "deleted"))
    const failure = await resolveValue(client, TARGET, [ref(sha(5))]).catch((error: unknown) => error)
    expect((failure as UnresolvedContentError).availability).toBe("deleted")
    // Asked exactly once more, not in a loop.
    expect(blob).toHaveBeenCalledTimes(2)
    client.clear()
  })
})
