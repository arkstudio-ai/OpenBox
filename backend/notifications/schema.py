from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

BUNDLE_ID = "com.bossip.bipmobile"


class PresenceReport(BaseModel):
    model_config = ConfigDict(extra="forbid")

    state: Literal["resumed", "inactive", "hidden", "paused", "detached"]
    # Persisted by the installation before sending; fences reordered requests
    # across both lifecycle transitions and process restarts. Never a clock.
    sequence: int = Field(strict=True, ge=1, le=9007199254740991)


class DeviceRegistration(BaseModel):
    model_config = ConfigDict(populate_by_name=True, extra="forbid", str_strip_whitespace=True)

    platform: Literal["ios", "android"]
    provider: Literal["apns", "jpush"]
    token: str = Field(min_length=16, max_length=4096)
    bundle_id: Literal["com.bossip.bipmobile"] = Field(default=BUNDLE_ID, alias="bundleId")
    app_version: str = Field(default="", max_length=64, alias="appVersion")
    locale: str = Field(default="zh-CN", max_length=32)
    apns_environment: Literal["sandbox", "production"] = Field(default="production", alias="apnsEnvironment")
    notifications_enabled: bool = Field(default=True, alias="notificationsEnabled")

    @model_validator(mode="after")
    def provider_matches_platform(self):
        if self.provider != ("apns" if self.platform == "ios" else "jpush"):
            raise ValueError("Provider does not match platform")
        if self.provider == "apns":
            if any(c not in "0123456789abcdefABCDEF" for c in self.token) or len(self.token) % 2:
                raise ValueError("APNs token must be hexadecimal")
            self.token = self.token.lower()
        else:
            if not self.token.isascii() or not self.token.isalnum():
                raise ValueError("Invalid JPush registration ID")
            self.apns_environment = "production"
        return self
