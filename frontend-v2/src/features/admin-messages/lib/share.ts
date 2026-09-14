import { paths } from "@/shared/router/paths"

/** Absolute, shareable URL of a published topic page. */
export function topicShareUrl(slug: string): string {
  return `${window.location.origin}${paths.topic(slug)}`
}
