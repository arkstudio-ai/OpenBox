import { AuthShell, SsoEntry, LoginForm } from "@/features/auth"

export default function LoginRoute() {
  return (
    <AuthShell>
      <SsoEntry screen="sign_in">
        <LoginForm />
      </SsoEntry>
    </AuthShell>
  )
}
