import { http, requestBlob } from "@/shared/api/http"
import { wikiParams, type OrganizationRun } from "./platform-api"

export interface FieldDefinition {
  type: string
  required?: boolean
  enum?: string[]
  default?: unknown
  min?: number
  max?: number
}
export interface StageDefinition {
  id: string
  title?: string
  reads: string[]
  writes: string[]
  gates: string[]
  outputsRequired?: number
  relationWrites?: string[]
  artifactWrites?: string[]
  action?: "organize" | "compile"
}
export interface ProfileDefinition {
  schemaVersion: number
  profileId: string
  profileVersion?: string
  title: string
  entities: Record<
    string,
    {
      title?: string
      fields: Record<string, FieldDefinition>
      lifecycle?: {
        field: string
        initial: string
        transitions: Record<string, string[]>
        terminal: string[]
      }
    }
  >
  relations: Record<
    string,
    { from: string[]; to: string[]; direction: string; attributes?: Record<string, FieldDefinition> }
  >
  artifacts: Record<string, { mediaTypes: string[]; title?: string }>
  workflows: Record<
    string,
    { title?: string; inputs?: Record<string, FieldDefinition>; stages: StageDefinition[] }
  >
}
export interface Profile {
  id: string
  revision: number
  project_id: string | null
  title: string
  definition: ProfileDefinition
  definition_hash: string
}
export interface TypedRecord {
  id: string
  revision: number
  entity_type: string
  slug: string
  title: string | null
  fields: Record<string, unknown>
  page_id: string
  page_revision: number
  available: boolean
  content_hash: string
}
export interface WorkflowState {
  status: string
  outputs: Record<string, unknown>[]
  output_hash: string
  outputs_available: boolean
  approvals: Record<string, { digest: string; actor_id: string; at: string }>
  results: Record<string, unknown>[]
  task:
    (Partial<OrganizationRun> & { kind: string; id: string; status: string; candidate_id?: string }) | null
}
export interface WorkflowRun {
  id: string
  revision: number
  project_id: string | null
  profile_id: string
  workflow_id: string
  inputs: Record<string, unknown>
  status: string
  reason: string | null
  stage_index: number
  current_stage: string | null
  definition_changed: boolean
  definition: ProfileDefinition
  stages: Record<string, WorkflowState>
  events?: { id: string; version: number; action: string; stage_id: string; created_at: string }[]
  events_next_offset?: number | null
}
export interface Adaptation {
  allowed: boolean
  issues?: string[]
  mapping?: Record<string, string>
  preview_hash: string
  old_hash: string
  new_hash: string
  new_definition: ProfileDefinition
}
const root = "/api/memory-wiki"
export const workflowApi = {
  templates: () => http.get<{ templates: ProfileDefinition[] }>(root + "/profile-templates"),
  profiles: (projectId: string, offset = 0) =>
    http.get<{ profiles: Profile[]; next_offset: number | null }>(
      root + "/profiles" + wikiParams(projectId, { offset }),
    ),
  saveProfile: (projectId: string, definition: ProfileDefinition, previous?: Profile) =>
    http.post<Profile>(root + "/profiles", {
      project_id: projectId || null,
      definition,
      profile_id: previous?.id,
      expected_revision: previous?.revision ?? 0,
    }),
  records: (id: string, offset = 0) =>
    http.get<{ records: TypedRecord[]; next_offset: number | null }>(
      root + `/profiles/${id}/records?offset=${offset}`,
    ),
  statistics: (id: string) =>
    http.get<{
      entities: Record<
        string,
        { states: Record<string, number>; total: number; unavailable: number; unreachable: string[] }
      >
    }>(root + `/profiles/${id}/statistics`),
  mutateRecord: (profile: Profile, kind: string, payload: Record<string, unknown>) =>
    http.post(root + `/profiles/${profile.id}/records`, {
      profile_revision: profile.revision,
      kind,
      payload,
    }),
  runs: (projectId: string, offset = 0) =>
    http.get<{
      runs: Pick<WorkflowRun, "id" | "revision" | "workflow_id" | "profile_id" | "status" | "stage_index">[]
      next_offset: number | null
    }>(root + "/workflows" + wikiParams(projectId, { offset })),
  run: (id: string, eventOffset = 0) =>
    http.get<WorkflowRun>(root + `/workflows/${id}?event_offset=${eventOffset}`),
  start: (profile: Profile, workflowId: string, inputs: Record<string, unknown>) =>
    http.post<WorkflowRun>(root + "/workflows", {
      profile_id: profile.id,
      expected_profile_revision: profile.revision,
      workflow_id: workflowId,
      inputs,
      request_id: crypto.randomUUID(),
    }),
  act: (run: WorkflowRun, action: string, payload: Record<string, unknown> = {}) =>
    http.post<WorkflowRun>(root + `/workflows/${run.id}/actions`, {
      expected_revision: run.revision,
      request_id: crypto.randomUUID(),
      action,
      payload,
    }),
  adaptation: (id: string) => http.get<Adaptation>(root + `/workflows/${id}/adaptation`),
  history: (id: string) => requestBlob(root + `/workflows/${id}/history`),
  artifact: (id: string) => requestBlob(root + `/artifacts/${id}`),
}
