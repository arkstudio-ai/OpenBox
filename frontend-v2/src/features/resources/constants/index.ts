// Value → icon / i18n-key maps. Explicit tables rather than dynamic key
// building, so the i18n extractor can see every key (§10.3).
import {
  Archive,
  Bot,
  Code2,
  FileAudio,
  FileVideo,
  FileText,
  Files,
  Image as ImageIcon,
  User,
} from "lucide-react"
import type { ComponentType } from "react"
import type { KindFilter, ResourceKind, ResourceSort, SourceFilter } from "@/features/resources/types"

type Icon = ComponentType<{ className?: string; strokeWidth?: number }>

export const KIND_ICON: Record<ResourceKind, Icon> = {
  image: ImageIcon,
  video: FileVideo,
  audio: FileAudio,
  document: FileText,
  archive: Archive,
  code: Code2,
  other: Files,
}

export const KIND_LABEL: Record<KindFilter, string> = {
  all: "kind.all",
  image: "kind.image",
  video: "kind.video",
  audio: "kind.audio",
  document: "kind.document",
  archive: "kind.archive",
  code: "kind.code",
  other: "kind.other",
}

export const KIND_FILTERS: KindFilter[] = [
  "all",
  "image",
  "video",
  "audio",
  "document",
  "code",
  "archive",
  "other",
]

export const SOURCE_ICON: Record<Exclude<SourceFilter, "all">, Icon> = {
  user: User,
  agent: Bot,
}

export const SOURCE_LABEL: Record<SourceFilter, string> = {
  all: "source.all",
  user: "source.user",
  agent: "source.agent",
}

export const SOURCE_FILTERS: SourceFilter[] = ["all", "user", "agent"]

export const SORT_LABEL: Record<ResourceSort, string> = {
  created: "sort.created",
  name: "sort.name",
  size: "sort.size",
}

export const SORT_OPTIONS: ResourceSort[] = ["created", "name", "size"]

/** Sentinel project filters — real ids are opaque ULIDs, so these can't clash. */
export const ALL_PROJECTS = "all"
export const NO_PROJECT = "none"
