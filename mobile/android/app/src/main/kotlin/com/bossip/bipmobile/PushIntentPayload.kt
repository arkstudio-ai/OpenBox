package com.bossip.bipmobile

import org.json.JSONArray
import org.json.JSONObject

/** JPush's documented intent envelope for both ordinary and vendor channels. */
internal object PushIntentPayload {
    fun parse(raw: String?): Map<String, Any?>? {
        if (raw.isNullOrBlank() || raw.length > 16384) return null
        return runCatching {
            val envelope = JSONObject(raw)
            val extras = when (val value = envelope.opt("n_extras")) {
                is JSONObject -> value
                is String -> JSONObject(value)
                else -> envelope
            }
            if (extras.optString("source") != "openbox" || extras.optInt("schemaVersion") != 1 ||
                extras.optString("eventId").isBlank() || extras.optString("recipientId").isBlank()) {
                return null
            }
            toMap(extras)
        }.getOrNull()
    }

    private fun toMap(value: JSONObject): Map<String, Any?> =
        value.keys().asSequence().associateWith { key -> normalize(value.opt(key)) }

    private fun normalize(value: Any?): Any? = when (value) {
        null, JSONObject.NULL -> null
        is JSONObject -> toMap(value)
        is JSONArray -> (0 until value.length()).map { normalize(value.opt(it)) }
        else -> value
    }
}
