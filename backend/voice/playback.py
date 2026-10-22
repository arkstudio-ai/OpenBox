"""Provider completion and browser playback completion are different events."""
from core.log import create_logger

log = create_logger("voice.playback")


class PlaybackMixin:
    def _init_playback(self):
        self.playback_ack = False
        self._playbacks = {}
        self._audio_ack = False
        self._user_paused_reports = []

    def _playback_key(self, response_id):
        return f"{self.session}:{response_id}"

    def _pause_user_reports(self):
        """Only the reports actually playing yield; unrelated queued results remain due.

        The native reply already sees the current task facts and the user's
        interruption. Retrying the old note afterwards produces a second answer.
        Keep these results unacknowledged and available in the shared conversation.
        """
        refs = [ref for pending in self._playbacks.values() for ref in pending["refs"]]
        if self.audio_sent and (ref := self._delivery(self.response_kind)):
            refs.append(ref)
        for parent in refs:
            for ref in (parent, *(parent.riders or [])):
                if (ref.report is not None and not ref.asked and ref.status == "ok"
                        and not ref.cards and not ref.questions and not ref.decision_versions):
                    ref.paused_by_user = True
                    if ref not in self._user_paused_reports:
                        self._user_paused_reports.append(ref)

    def _finish_user_interruption(self, *, invalid=False):
        refs, self._user_paused_reports = self._user_paused_reports, []
        for ref in refs:
            if invalid and ref in self.deliveries:
                ref.paused_by_user = False
                if ref.delivery == "paused":
                    ref.delivery = "queued"

    async def _response_playback_done(self, event, refs, said):
        complete = event.status == "completed" and not self.interrupted and self.audio_sent
        if not complete:
            for ref in refs:
                ref.interrupted = ref.interrupted or self.audio_sent
                await self._delivered(ref, False, said)
            return
        if not self._audio_ack and self.reply_gap is None:
            for ref in refs:
                await self._delivered(ref, True, said)
            return
        key = self._playback_key(event.response_id)
        self._playbacks[key] = {"refs": refs, "said": said,
            "until": self._playing_until or self.clock(), "ack": self._audio_ack}
        for ref in refs:
            ref.delivery = "awaiting_playback"
        if self._audio_ack:
            self.emit({"type": "playback.finished", "response_id": key})

    async def playback_completed(self, key):
        pending = self._playbacks.get(key)
        if not self.playback_ack or not pending or not pending["ack"]:
            return  # unknown, premature, duplicate and pre-clear acknowledgements
        self._playbacks.pop(key)
        self._playing_until = self.clock() - 0.3  # actual browser completion replaces the estimate
        for ref in pending["refs"]:
            if ref in self.deliveries and ref.delivery == "awaiting_playback":
                await self._delivered(ref, True, pending["said"])
        log.info("voice playback completed call=%s response=%s", self.call_id, key)
        await self.fill_idle()

    async def _finish_playbacks(self):
        for key, pending in list(self._playbacks.items()):
            # Older clients use a conservative estimate. Negotiated clients
            # must acknowledge; a lost ACK never counts as successful delivery.
            deadline = pending["until"] + (10 if pending["ack"] else 0.3)
            if self.clock() < deadline:
                continue
            if pending["ack"]:
                # Silence from the browser is an unknown outcome, not license
                # to replay an entire answer. A late ACK or user interruption
                # resolves it; hang-up leaves the durable text result intact.
                if not pending.get("warned"):
                    pending["warned"] = True
                    log.warning("voice playback acknowledgement missing call=%s response=%s", self.call_id, key)
                continue
            self._playbacks.pop(key)
            for ref in pending["refs"]:
                if ref in self.deliveries and ref.delivery == "awaiting_playback":
                    await self._delivered(ref, True, pending["said"])

    async def _cancel_playbacks(self):
        pending, self._playbacks = self._playbacks, {}
        for playback in pending.values():
            for ref in playback["refs"]:
                if ref in self.deliveries and ref.delivery == "awaiting_playback":
                    ref.interrupted = True
                    await self._delivered(ref, False, playback["said"])
