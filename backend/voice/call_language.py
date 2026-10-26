"""Ephemeral language precedence; no profile, memory or task writes."""

_UNSET = object()


class CallLanguage:
    def __init__(self):
        self.saved = _UNSET
        self.call = None
        self.reply = None

    def sync(self, settings):
        if "language" not in settings:
            return
        language = settings["language"]
        if self.saved is not _UNSET and self.saved != language:
            # A later real settings change wins, including clearing the setting.
            self.call = self.reply = None
        self.saved = language

    def begin_turn(self):
        self.reply = None

    def apply(self, packet, utterance):
        from memory.providers.configured_read import spoken_language
        self.sync(packet.get("settings", {}))
        decision = packet.get("route", {}).get("spoken_language") or {}
        # Rebind to the actual ASR utterance. A cache/tool/background packet
        # cannot manufacture an instruction or replay a prior turn's quote.
        valid = spoken_language([decision.get(key) for key in ("role", "quote", "language", "scope", "confidence")], utterance)
        if not valid:
            return
        if valid["scope"] == "reset":
            self.call = self.reply = None
        elif valid["scope"] == "call":
            self.call, self.reply = valid["language"], None
        else:
            self.reply = valid["language"]

    def resolve(self, settings):
        self.sync(settings)
        return self.reply or self.call or settings.get("language")
