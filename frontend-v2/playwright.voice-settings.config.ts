import { defineConfig } from "@playwright/test"
import base from "./playwright.assistant-reminders.config"

export default defineConfig({ ...base, testMatch: "voice-settings.spec.ts" })
