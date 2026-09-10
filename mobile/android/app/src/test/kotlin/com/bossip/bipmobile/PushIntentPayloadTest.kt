package com.bossip.bipmobile

import org.json.JSONObject
import org.junit.Assert.*
import org.junit.Test

class PushIntentPayloadTest {
    private val payload = """{"source":"openbox","schemaVersion":1,"eventId":"e1","recipientId":"u1","bindingId":"b1","workspaceId":"w1","sessionId":"s1","type":"task_completed"}"""

    @Test fun parsesJPushAndVendorStringExtras() {
        val envelope = JSONObject().put("msg_id", "provider-id").put("n_extras", payload).toString()
        val parsed = PushIntentPayload.parse(envelope)
        assertEquals("e1", parsed?.get("eventId"))
        assertEquals("s1", parsed?.get("sessionId"))
        assertFalse(parsed!!.containsKey("msg_id"))
    }

    @Test fun parsesHuaweiDataWithObjectExtras() {
        val envelope = JSONObject().put("n_extras", JSONObject(payload)).toString()
        assertEquals("u1", PushIntentPayload.parse(envelope)?.get("recipientId"))
        assertEquals("b1", PushIntentPayload.parse(payload)?.get("bindingId"))
    }

    @Test fun ignoresOtherDeepLinksMalformedAndOversizedData() {
        for (raw in listOf(null, "", "https://example.com/invite/a", "{", "{}", "x".repeat(17000),
            """{"n_extras":"not json"}""", payload.replace("openbox", "legacy"),
            payload.replace("\"schemaVersion\":1", "\"schemaVersion\":2"))) {
            assertNull(PushIntentPayload.parse(raw))
        }
    }
}
