import { createContext, type Dispatch, type SetStateAction } from "react"

export const WorkspaceActionsContext = createContext<{
  target: HTMLDivElement | null
  setTarget: Dispatch<SetStateAction<HTMLDivElement | null>>
} | null>(null)
