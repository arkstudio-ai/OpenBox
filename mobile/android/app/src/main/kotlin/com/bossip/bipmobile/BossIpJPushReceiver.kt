package com.bossip.bipmobile

import android.content.Context
import android.content.Intent
import cn.jpush.android.api.CustomMessage
import cn.jpush.android.api.JPushInterface
import cn.jpush.android.api.NotificationMessage
import cn.jpush.android.service.JPushMessageReceiver
import org.json.JSONObject

/** JPush 6.x receiver mapped to BossIP's APNs-compatible Flutter contract. */
class BossIpJPushReceiver : JPushMessageReceiver() {
    override fun onRegister(context: Context, registrationId: String) {
        BossIpPushBridge.updateRegistrationId(context, registrationId)
    }

    override fun onConnected(context: Context, connected: Boolean) {
        if (!connected) return
        JPushInterface.getRegistrationID(context)
            ?.takeIf { it.isNotBlank() }
            ?.let { BossIpPushBridge.updateRegistrationId(context, it) }
    }

    override fun onNotifyMessageArrived(
        context: Context,
        message: NotificationMessage,
    ) {
        BossIpPushBridge.publishReceived(notificationPayload(message))
    }

    override fun onNotifyMessageUnShow(
        context: Context,
        message: NotificationMessage,
    ) {
        // The server deliberately sets display_foreground=0 so JPush does not
        // render a foreground banner. Forward only for an in-app hint; Dart
        // never mirrors a received push into another system notification.
        BossIpPushBridge.publishReceived(notificationPayload(message))
    }

    override fun onNotifyMessageOpened(
        context: Context,
        message: NotificationMessage,
    ) {
        val payload = notificationPayload(message)
        BossIpPushBridge.storeOpened(context, payload)
        val openIntent = Intent(context, MainActivity::class.java).apply {
            action = BossIpPushBridge.ACTION_SYSTEM_NOTIFICATION_OPENED
            flags = Intent.FLAG_ACTIVITY_NEW_TASK or
                Intent.FLAG_ACTIVITY_SINGLE_TOP or
                Intent.FLAG_ACTIVITY_CLEAR_TOP
            putExtra(
                BossIpPushBridge.EXTRA_SYSTEM_NOTIFICATION_PAYLOAD,
                HashMap(payload),
            )
        }
        context.startActivity(openIntent)
    }

    override fun onMessage(context: Context, message: CustomMessage) {
        val payload = mutableMapOf<String, Any?>()
        payload.putAll(parseExtras(message.extra))
        payload.putIfAbsent("type", "jpush_custom_message")
        payload["title"] = message.title?.takeIf { it.isNotBlank() } ?: "BossIP"
        payload["body"] = message.message ?: ""
        payload["messageId"] = message.messageId ?: ""
        payload["platform"] = "android"
        payload["provider"] = "jpush"
        payload.putIfAbsent("source", "jpush")
        BossIpPushBridge.publishReceived(payload)
    }

    override fun isNeedShowNotification(
        context: Context,
        message: NotificationMessage,
        source: String,
    ): Boolean = !BossIpPushBridge.shouldSuppressForegroundPresentation(
        context,
        notificationPayload(message),
    )

    private fun notificationPayload(message: NotificationMessage): Map<String, Any?> {
        val payload = parseExtras(message.notificationExtras).toMutableMap()
        payload["title"] = message.notificationTitle?.takeIf { it.isNotBlank() }
            ?: payload["title"]
            ?: "BossIP"
        payload["body"] = message.notificationContent?.takeIf { it.isNotBlank() }
            ?: payload["body"]
            ?: ""
        payload["messageId"] = message.msgId ?: ""
        payload["notificationId"] = message.notificationId
        payload["platform"] = "android"
        payload["provider"] = "jpush"
        payload.putIfAbsent("source", "jpush")
        return payload
    }

    private fun parseExtras(raw: String?): Map<String, Any?> {
        if (raw.isNullOrBlank()) return emptyMap()
        return runCatching {
            BossIpPushBridge.jsonObjectToMap(JSONObject(raw))
        }.getOrDefault(emptyMap())
    }
}
