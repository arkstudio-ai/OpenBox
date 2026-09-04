import { AuthShell, SsoEntry, RegisterForm } from "@/features/auth"

export default function RegisterRoute() {
  return (
    <AuthShell>
      <SsoEntry screen="register">
        <RegisterForm />
      </SsoEntry>
    </AuthShell>
  )
}
