// The transport lives in shared/api/inbox so the workspace sidebar can read the
// badge without crossing a feature boundary; the feature re-exports it.
export * from "@/shared/api/inbox"
