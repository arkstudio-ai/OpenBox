import { lazy } from "react"
import { createBrowserRouter, Navigate } from "react-router"
import { AppErrorBoundary } from "@/app/router/AppErrorBoundary"
import { RequireAuth, RequireAdmin, RedirectIfAuthed } from "@/app/router/guards"
import { routePatterns, paths } from "@/app/router/paths"

const LandingRoute = lazy(() => import("@/routes/landing/LandingRoute"))
const LoginRoute = lazy(() => import("@/routes/auth/LoginRoute"))
const RegisterRoute = lazy(() => import("@/routes/auth/RegisterRoute"))
const SsoCallbackRoute = lazy(() => import("@/routes/auth/SsoCallbackRoute"))
const WorkspaceLayout = lazy(() => import("@/app/layouts/WorkspaceLayout"))
const EmptyChatRoute = lazy(() => import("@/routes/workspace/EmptyChatRoute"))
const ChatRoute = lazy(() => import("@/routes/workspace/ChatRoute"))
const SettingsRoute = lazy(() => import("@/routes/settings/SettingsRoute"))
const BillingRoute = lazy(() => import("@/routes/billing/BillingRoute"))
const CronRoute = lazy(() => import("@/routes/cron/CronRoute"))
const ResourcesRoute = lazy(() => import("@/routes/resources/ResourcesRoute"))
const SkillsRoute = lazy(() => import("@/routes/skills/SkillsRoute"))
const AuthCenterRoute = lazy(() => import("@/routes/auth-center/AuthCenterRoute"))
const NotFoundRoute = lazy(() => import("@/routes/NotFoundRoute"))
const InviteRoute = lazy(() => import("@/routes/invite/InviteRoute"))
const AdminRoute = lazy(() => import("@/routes/admin/AdminRoute"))
const AdminNotificationsRoute = lazy(() => import("@/routes/admin/AdminNotificationsRoute"))
const AdminMessagesRoute = lazy(() => import("@/routes/admin/AdminMessagesRoute"))
const AdminFleetRoute = lazy(() => import("@/routes/admin/AdminFleetRoute"))
const AdminSkillsRoute = lazy(() => import("@/routes/admin/AdminSkillsRoute"))
const AdminBillingRoute = lazy(() => import("@/routes/admin/AdminBillingRoute"))
const AdminWorkspaceRoute = lazy(() => import("@/routes/admin/AdminWorkspaceRoute"))

export const router = createBrowserRouter([
  {
    errorElement: <AppErrorBoundary />,
    children: [
      { path: paths.landing, element: <LandingRoute /> },
      {
        path: paths.login,
        element: (
          <RedirectIfAuthed>
            <LoginRoute />
          </RedirectIfAuthed>
        ),
      },
      {
        path: paths.register,
        element: (
          <RedirectIfAuthed>
            <RegisterRoute />
          </RedirectIfAuthed>
        ),
      },
      { path: paths.ssoCallback, element: <SsoCallbackRoute /> },
      {
        path: routePatterns.invite,
        element: (
          <RequireAuth>
            <InviteRoute />
          </RequireAuth>
        ),
      },
      {
        path: paths.app,
        element: (
          <RequireAuth>
            <WorkspaceLayout />
          </RequireAuth>
        ),
        children: [
          { index: true, element: <EmptyChatRoute /> },
          { path: routePatterns.chat, element: <ChatRoute /> },
          { path: routePatterns.settings, element: <SettingsRoute /> },
          { path: routePatterns.billing, element: <BillingRoute /> },
          { path: routePatterns.cron, element: <CronRoute /> },
          { path: routePatterns.resources, element: <ResourcesRoute /> },
          { path: routePatterns.skills, element: <SkillsRoute /> },
          { path: routePatterns.authCenter, element: <AuthCenterRoute /> },
          {
            // The console shell sits behind one role check; every column below
            // it is a plain child, so `RequireAdmin` runs exactly once (§4.2).
            path: routePatterns.admin,
            element: (
              <RequireAdmin>
                <AdminRoute />
              </RequireAdmin>
            ),
            children: [
              { index: true, element: <Navigate to={paths.adminFleet} replace /> },
              { path: routePatterns.adminFleet, element: <AdminFleetRoute /> },
              { path: routePatterns.adminNotifications, element: <AdminNotificationsRoute /> },
              { path: routePatterns.adminMessages, element: <AdminMessagesRoute /> },
              { path: routePatterns.adminSkills, element: <AdminSkillsRoute /> },
              // React Router ranks branches by score before matching, and
              // `billing/workspaces/:workspaceId` scores higher than
              // `billing/:tab?` (two static segments beat one dynamic), so the
              // detail route wins whatever the array order. Listed first anyway
              // so the file reads the way it resolves.
              { path: routePatterns.adminWorkspace, element: <AdminWorkspaceRoute /> },
              { path: routePatterns.adminBilling, element: <AdminBillingRoute /> },
            ],
          },
        ],
      },
      { path: "*", element: <NotFoundRoute /> },
    ],
  },
])
