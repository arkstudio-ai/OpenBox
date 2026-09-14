import type { TabId } from "../../utils/tabs"

export function tabDomId(idBase: string, tab: TabId): string {
  return `${idBase}-tab-${tab}`
}

export function panelDomId(idBase: string): string {
  return `${idBase}-panel`
}
