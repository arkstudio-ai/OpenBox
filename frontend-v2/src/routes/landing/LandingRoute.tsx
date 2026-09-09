import { LandingTopbar } from "./LandingTopbar"
import { LandingHero } from "./LandingHero"
import { LandingBand } from "./LandingBand"
import { LandingCapabilities } from "./LandingCapabilities"
import { LandingFlow } from "./LandingFlow"
import { LandingUseCases } from "./LandingUseCases"
import { LandingFaq } from "./LandingFaq"
import { LandingOutro } from "./LandingOutro"

export default function LandingRoute() {
  return (
    <div className="scr flex min-h-screen flex-col overflow-x-hidden bg-bg text-ink">
      <LandingTopbar />
      <main className="flex-1">
        <LandingHero />
        <LandingBand />
        <LandingCapabilities />
        <LandingFlow />
        <LandingUseCases />
        <LandingFaq />
        <LandingOutro />
      </main>
    </div>
  )
}
