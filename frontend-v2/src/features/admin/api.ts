import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query"
import { http } from "@/shared/api/http"
import type { FleetAlert, FleetDesktop, FleetSnapshot, PoolSummary } from "./types"


const keys = {
  all: ["admin-fleet"] as const,
  desktops: ["admin-fleet", "desktops"] as const,
  pool: ["admin-fleet", "pool"] as const,
  alerts: ["admin-fleet", "alerts"] as const,
  snapshot: ["admin-fleet", "snapshot"] as const,
}

export function useFleetDesktops() {
  return useQuery({
    queryKey: keys.desktops,
    queryFn: () => http.get<{ items: FleetDesktop[]; total: number }>("/api/admin/fleet/desktops"),
    refetchInterval: 30_000,
  })
}

export function usePoolSummary() {
  return useQuery({
    queryKey: keys.pool,
    queryFn: () => http.get<PoolSummary>("/api/admin/fleet/pool"),
    refetchInterval: 30_000,
  })
}

export function useFleetAlerts() {
  return useQuery({
    queryKey: keys.alerts,
    queryFn: () => http.get<{ items: FleetAlert[] }>("/api/admin/fleet/alerts?state=open"),
    refetchInterval: 30_000,
  })
}

export function useFleetSnapshot() {
  return useQuery({
    queryKey: keys.snapshot,
    queryFn: () => http.get<FleetSnapshot>("/api/admin/fleet/snapshots/latest"),
    refetchInterval: 60_000,
  })
}

function useFleetMutation<T>(mutationFn: (value: T) => Promise<unknown>) {
  const client = useQueryClient()
  return useMutation({
    mutationFn,
    onSuccess: () => client.invalidateQueries({ queryKey: keys.all }),
  })
}

export function useAckAlert() {
  return useFleetMutation((id: string) =>
    http.post(`/api/admin/fleet/alerts/${encodeURIComponent(id)}/ack`),
  )
}

export function useMuteAlert() {
  return useFleetMutation(({ id, until }: { id: string; until: string }) =>
    http.post(`/api/admin/fleet/alerts/${encodeURIComponent(id)}/mute`, { until }),
  )
}

export function useReleaseDesktop() {
  return useFleetMutation((id: string) =>
    http.post(`/api/admin/fleet/desktops/${encodeURIComponent(id)}/release`),
  )
}

export function useRecycleDesktop() {
  return useFleetMutation((id: string) =>
    http.post(`/api/admin/fleet/desktops/${encodeURIComponent(id)}/recycle`, { approve: true }),
  )
}

export function useRetireDesktop() {
  return useFleetMutation((id: string) =>
    http.post(`/api/admin/fleet/desktops/${encodeURIComponent(id)}/retire`),
  )
}

export function useAdoptDesktop() {
  return useFleetMutation((input: {
    id: string
    poolState: "reserve" | "prewarm"
    rebuild: boolean
    gatewayReleaseVerified: boolean
  }) =>
    http.post(`/api/admin/fleet/desktops/${encodeURIComponent(input.id)}/adopt`, {
      pool_state: input.poolState,
      rebuild: input.rebuild,
      approve: input.rebuild,
      gateway_release_verified: input.gatewayReleaseVerified,
    }),
  )
}
