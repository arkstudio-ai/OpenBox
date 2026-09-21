import { useId, useState, type ReactNode } from "react"
import { useTranslation } from "react-i18next"

export const INPUT =
  "border-hair bg-bg text-ink min-h-9 w-full rounded-lg border px-3 py-2 text-sm outline-none focus:border-ink disabled:opacity-50"
export const BUTTON =
  "border-hair text-ink hover:bg-hairsoft rounded-full border px-3 py-1.5 text-xs disabled:opacity-40"
export const PRIMARY = "bg-ink text-bg rounded-full px-4 py-2 text-sm disabled:opacity-40"

export function Field({
  label,
  value,
  onChange,
  multiline = false,
  maxLength,
  type = "text",
  min,
  max,
  required = false,
}: {
  label: string
  value: string | number
  onChange: (value: string) => void
  multiline?: boolean
  maxLength?: number
  type?: string
  min?: number
  max?: number
  required?: boolean
}) {
  const id = useId()
  return (
    <div>
      <label htmlFor={id} className="text-n600 mb-1.5 block text-xs">
        {label}
        {required && (
          <span aria-hidden className="text-a700">
            {" "}
            *
          </span>
        )}
      </label>
      {multiline ? (
        <textarea
          id={id}
          className={`${INPUT} min-h-28 resize-y`}
          value={value}
          onChange={(event) => onChange(event.target.value)}
          maxLength={maxLength}
          required={required}
        />
      ) : (
        <input
          id={id}
          className={INPUT}
          type={type}
          min={min}
          max={max}
          value={value}
          onChange={(event) => onChange(event.target.value)}
          maxLength={maxLength}
          required={required}
        />
      )}
    </div>
  )
}
export function FormSection({ title, children }: { title: string; children: ReactNode }) {
  return (
    <section className="border-hair bg-card space-y-4 rounded-xl border p-4">
      <h2 className="text-sm font-medium">{title}</h2>
      {children}
    </section>
  )
}
export function JsonField({
  label,
  value,
  onChange,
}: {
  label: string
  value: unknown
  onChange: (value: unknown) => void
}) {
  const { t } = useTranslation("teams")
  const [text, setText] = useState(value == null ? "" : JSON.stringify(value, null, 2))
  const [error, setError] = useState(false)
  return (
    <div>
      <Field
        label={label}
        multiline
        value={text}
        onChange={(next) => {
          setText(next)
          try {
            onChange(next.trim() ? (JSON.parse(next) as unknown) : null)
            setError(false)
          } catch {
            setError(true)
          }
        }}
      />
      {error && (
        <p className="text-danger mt-1 text-xs" role="alert">
          {t("invalidJson")}
        </p>
      )}
    </div>
  )
}
