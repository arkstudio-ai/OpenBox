import { afterEach, beforeEach, expect, it, vi } from "vitest"
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react"
import { MemoryRouter } from "react-router"
import { createInstance } from "i18next"
import { I18nextProvider } from "react-i18next"
import zh from "@/locales/zh-CN/legal.json"
import auth from "@/locales/zh-CN/auth.json"
import { SsoEntry } from "./SsoEntry"
import { LoginForm } from "./LoginForm"

const mock = vi.hoisted(() => ({
  start: vi.fn(),
  stage: vi.fn(),
  complete: vi.fn(),
  login: vi.fn(),
}))
vi.mock("../api/auth", () => ({
  useLogtoConfig: () => ({ data: { enabled: true }, isLoading: false }),
  useLogin: () => ({ mutateAsync: mock.login }),
  useCompleteAuth: () => mock.complete,
}))
vi.mock("../lib/logto", () => ({ beginLogtoLogin: mock.start, rememberReturnPath: vi.fn() }))
vi.mock("@/shared/legal/consent", () => ({ stageLegalConsent: mock.stage }))
vi.mock("../lib/errors", () => ({ useAuthErrorMessage: () => () => "保存失败，请重试" }))

const i18n = createInstance()
await i18n.init({
  lng: "zh-CN",
  resources: { "zh-CN": { legal: zh, auth } },
  interpolation: { escapeValue: false },
})
function mount(child: React.ReactNode) {
  return render(
    <I18nextProvider i18n={i18n}>
      <MemoryRouter>{child}</MemoryRouter>
    </I18nextProvider>,
  )
}
beforeEach(() => vi.resetAllMocks())
afterEach(cleanup)

it("does not auto-redirect or preselect consent; reading a policy does not accept it", async () => {
  mount(
    <SsoEntry screen="sign_in">
      <div>fallback</div>
    </SsoEntry>,
  )
  const checkbox = screen.getByRole("checkbox") as HTMLInputElement
  const button = screen.getByRole("button", { name: auth.sso }) as HTMLButtonElement
  expect(checkbox.checked).toBe(false)
  expect(button.disabled).toBe(true)
  const link = screen.getByRole("link", { name: zh.documents.privacy.title })
  expect(link.getAttribute("href")).toBe("/legal/privacy/")
  expect(link.getAttribute("target")).toBe("_blank")
  expect(mock.start).not.toHaveBeenCalled()
  fireEvent.click(checkbox)
  fireEvent.click(button)
  await waitFor(() => expect(mock.start).toHaveBeenCalledOnce())
  expect(mock.stage).toHaveBeenCalledOnce()
})

it("keeps a failed policy receipt visible and allows retry after credentials succeeded", async () => {
  mock.login.mockResolvedValue({ access_token: "fresh", user: { id: "u" } })
  mock.complete.mockRejectedValue(new Error("receipt unavailable"))
  mount(<LoginForm />)
  fireEvent.change(screen.getByLabelText(auth.accountLabel), { target: { value: "tester" } })
  fireEvent.change(screen.getByLabelText(auth.pwLabel), { target: { value: "password1" } })
  fireEvent.click(screen.getByRole("button", { name: auth.signInBtn }))
  expect(screen.getByText(zh.consentRequired)).toBeTruthy()
  expect(mock.login).not.toHaveBeenCalled()
  fireEvent.click(screen.getByRole("checkbox", { name: zh.consentLabel }))
  fireEvent.click(screen.getByRole("button", { name: auth.signInBtn }))
  await waitFor(() => expect(screen.getByText("保存失败，请重试")).toBeTruthy())
  expect((screen.getByRole("button", { name: auth.signInBtn }) as HTMLButtonElement).disabled).toBe(false)
})
