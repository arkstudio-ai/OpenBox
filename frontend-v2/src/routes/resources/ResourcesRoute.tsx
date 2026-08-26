import { ALL_PROJECTS, ResourceCenter } from "@/features/resources"

/** Thin shell. Scope, filters and the open resource all live in the URL, so
 *  the page has nothing of its own to hold. */
export default function ResourcesRoute() {
  return <ResourceCenter defaultProject={ALL_PROJECTS} />
}
