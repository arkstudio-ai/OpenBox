// The viewer's standing to read trajectories, as this tab last learned it.
// The auth store can still say "admin" after the database demoted the account
// (the server answers 403 while the JWT is unexpired), so a refusal latches
// here: every trajectory hook stops reading until the identity changes. The
// epoch is part of every query key, so data fetched before a purge can never
// be served to an observer after it.
import { create } from "zustand"

export type DenialReason =
  "unauthenticated" | "forbidden" | "signed_out" | "role_changed" | "identity_changed"

export interface AccessDenial {
  reason: DenialReason
  status: number | null
}

interface AccessState {
  epoch: number
  denied: AccessDenial | null
  /** Refused: drop to a new epoch and stay locked until the identity changes. */
  revoke: (reason: DenialReason, status?: number | null) => void
  /** A different (or re-authenticated) admin: new epoch, unlocked. */
  rotate: () => void
}

export const useTrajectoryAccess = create<AccessState>((set) => ({
  epoch: 0,
  denied: null,
  revoke: (reason, status = null) => set((state) => ({ epoch: state.epoch + 1, denied: { reason, status } })),
  rotate: () => set((state) => ({ epoch: state.epoch + 1, denied: null })),
}))

export function currentAccessEpoch(): number {
  return useTrajectoryAccess.getState().epoch
}
