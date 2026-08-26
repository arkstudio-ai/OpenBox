// Public surface of the resource centre — the page, and the data the chat
// composer's "@" menu renders (wired together in the workspace routes).
export { ResourceCenter } from "./components/ResourceCenter"
export { useResourceMention, type ResourceMention } from "./hooks/useResourceMention"
export { useResourceProjects } from "./api/resources"
export { ALL_PROJECTS, NO_PROJECT } from "./constants"
export type { Resource, ResourceKind, ResourceSource, SourceFilter } from "./types"
