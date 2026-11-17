import { Navigate } from "react-router"
import { AuthShell, SsoEntry, RegisterForm } from "@/features/auth"
import { useRegistrationEnabled } from "@/features/auth/api/auth"
import { paths } from "@/shared/router/paths"

export default function RegisterRoute() {
  // A deployment whose accounts are created by ops has no sign-up page.
  if (!useRegistrationEnabled()) return <Navigate to={paths.login} replace />
  return (
    <AuthShell>
      <SsoEntry screen="register">
        <RegisterForm />
      </SsoEntry>
    </AuthShell>
  )
}
