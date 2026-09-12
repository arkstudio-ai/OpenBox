// Query keys (ENGINEERING_SPEC §7.2). Every key starts with the viewer — the
// admin who is looking — and every per-session key names the target session;
// any key whose answer depends on a watermark also carries that watermark H, so
// a page fetched at H can never be served for H' and pages stay immutable.
import type { Seq } from "../types/protocol"

const ROOT = "admin-trajectories"

/** Marks keys that follow the moving head rather than a fixed watermark. */
export const LIVE = "live"

export const trajectoryKeys = {
  /** Prefix for purging everything a viewer may have seen. */
  root: [ROOT] as const,
  viewer: (viewerId: string) => [ROOT, viewerId] as const,
  sessions: (viewerId: string, params: string) => [ROOT, viewerId, "sessions", params] as const,
  sessionsProbe: (viewerId: string, params: string) => [ROOT, viewerId, "sessions-probe", params] as const,
  target: (viewerId: string, sessionId: string) => [ROOT, viewerId, "target", sessionId] as const,
  header: (viewerId: string, sessionId: string, at: Seq | typeof LIVE) =>
    [ROOT, viewerId, "target", sessionId, "header", at] as const,
  recordPages: (viewerId: string, sessionId: string, at: Seq, filters: string) =>
    [ROOT, viewerId, "target", sessionId, "records", at, filters] as const,
  record: (viewerId: string, sessionId: string, at: Seq, recordId: string) =>
    [ROOT, viewerId, "target", sessionId, "record", at, recordId] as const,
  search: (viewerId: string, sessionId: string, at: Seq, query: string) =>
    [ROOT, viewerId, "target", sessionId, "search", at, query] as const,
  payload: (viewerId: string, sessionId: string, at: Seq, payloadId: string) =>
    [ROOT, viewerId, "target", sessionId, "payload", at, payloadId] as const,
  exportJob: (viewerId: string, sessionId: string, exportId: string) =>
    [ROOT, viewerId, "target", sessionId, "export", exportId] as const,
}
