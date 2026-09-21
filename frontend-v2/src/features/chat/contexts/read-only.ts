import { createContext, useContext } from "react"

export const ChatReadOnlyContext = createContext(false)
export const useChatReadOnly = () => useContext(ChatReadOnlyContext)
