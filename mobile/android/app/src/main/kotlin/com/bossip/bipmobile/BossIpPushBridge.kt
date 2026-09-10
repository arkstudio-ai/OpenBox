package com.bossip.bipmobile

import android.content.Context
import android.app.KeyguardManager
import android.os.PowerManager
import org.json.JSONArray
import org.json.JSONObject

/**
 * Process-local bridge plus the small amount of durable state needed when a
 * JPush callback arrives before Flutter exists. Presentation scope is cached
 * to suppress stale account/binding notifications; the server owns the binding.
 */
internal object BossIpPushBridge {
    const val ACTION_SYSTEM_NOTIFICATION_OPENED =
        "com.bossip.bipmobile.PUSH_OPEN"
    const val EXTRA_SYSTEM_NOTIFICATION_PAYLOAD =
        "com.bossip.bipmobile.SYSTEM_NOTIFICATION_PAYLOAD"

    private const val PREFERENCES = "bossip_jpush"
    private const val KEY_REGISTRATION_ID = "registration_id"
    private const val KEY_INITIAL_OPEN = "initial_open"
    private const val KEY_PRIVACY_CONSENT = "privacy_consent"
    private const val KEY_PRIVACY_DECISION = "privacy_decision"
    private const val KEY_PRESENTATION_LIFECYCLE = "presentation_lifecycle"
    private const val KEY_PRESENTATION_SECTION = "presentation_section"
    private const val KEY_PRESENTATION_THREAD_ID = "presentation_thread_id"

    @Volatile
    private var eventSink: ((String, Map<String, Any?>) -> Unit)? = null

    @Volatile
    private var appLifecycle = "paused"

    @Volatile
    var activityVisible = false

    @Volatile
    private var section = "newTask"

    @Volatile
    private var activeThreadId: String? = null

    fun attach(context: Context, sink: (String, Map<String, Any?>) -> Unit) {
        eventSink = sink
        registrationId(context)?.let { registrationId ->
            sink(
                "deviceTokenUpdated",
                mapOf(
                    "platform" to "android",
                    "provider" to "jpush",
                    "token" to registrationId,
                ),
            )
        }
    }

    fun detach() {
        eventSink = null
    }

    fun updateRegistrationId(context: Context, registrationId: String) {
        val normalized = registrationId.trim()
        if (normalized.isEmpty()) return
        sharedPreferences(context)
            .edit()
            .putString(KEY_REGISTRATION_ID, normalized)
            .apply()
        eventSink?.invoke(
            "deviceTokenUpdated",
            mapOf(
                "platform" to "android",
                "provider" to "jpush",
                "token" to normalized,
            ),
        )
    }

    fun registrationId(context: Context): String? =
        sharedPreferences(context)
            .getString(KEY_REGISTRATION_ID, null)
            ?.trim()
            ?.takeIf { it.isNotEmpty() }

    fun publishReceived(payload: Map<String, Any?>) {
        // When the process is not showing Flutter, Android's notification
        // center is already the durable UI. Only foreground Dart needs this
        // event in order to render the active-conversation notice.
        eventSink?.invoke("notificationReceived", payload)
    }

    fun storeOpened(context: Context, payload: Map<String, Any?>) {
        sharedPreferences(context)
            .edit()
            .putString(KEY_INITIAL_OPEN, JSONObject(jsonSafeMap(payload)).toString())
            .apply()
    }

    fun consumeStoredOpen(context: Context): Map<String, Any?>? {
        val preferences = sharedPreferences(context)
        val raw = preferences.getString(KEY_INITIAL_OPEN, null) ?: return null
        preferences.edit().remove(KEY_INITIAL_OPEN).apply()
        return runCatching { jsonObjectToMap(JSONObject(raw)) }.getOrNull()
    }

    fun clearStoredOpen(context: Context) {
        sharedPreferences(context)
            .edit()
            .remove(KEY_INITIAL_OPEN)
            .apply()
    }

    fun hasPrivacyDecision(context: Context): Boolean =
        sharedPreferences(context)
            .getBoolean(KEY_PRIVACY_DECISION, false)

    fun hasPrivacyConsent(context: Context): Boolean =
        sharedPreferences(context)
            .getBoolean(KEY_PRIVACY_CONSENT, false)

    fun setPrivacyConsent(context: Context, granted: Boolean) {
        sharedPreferences(context)
            .edit()
            .putBoolean(KEY_PRIVACY_DECISION, true)
            .putBoolean(KEY_PRIVACY_CONSENT, granted)
            .apply()
    }

    fun updatePresentationContext(
        context: Context,
        appLifecycle: String,
        section: String,
        activeThreadId: String?,
        userId: String? = null,
        workspaceId: String? = null,
        bindingId: String? = null,
    ) {
        val normalizedThread = activeThreadId?.trim()?.takeIf { it.isNotEmpty() }
        this.appLifecycle = appLifecycle
        this.section = section
        this.activeThreadId = normalizedThread
        sharedPreferences(context)
            .edit()
            .putString(KEY_PRESENTATION_LIFECYCLE, appLifecycle)
            .putString(KEY_PRESENTATION_SECTION, section)
            .putString(KEY_PRESENTATION_THREAD_ID, normalizedThread ?: "")
            .putString("presentation_user_id", userId.orEmpty())
            .putString("presentation_workspace_id", workspaceId.orEmpty())
            .putString("presentation_binding_id", bindingId.orEmpty())
            .apply()
    }

    fun shouldSuppressForegroundPresentation(
        context: Context,
        payload: Map<String, Any?>,
    ): Boolean {
        val prefs = sharedPreferences(context)
        val recipient = payload["recipientId"]?.toString()
        if (recipient != null && recipient != prefs.getString("presentation_user_id", "")) return true
        val binding = payload["bindingId"]?.toString()
        val currentBinding = prefs.getString("presentation_binding_id", "").orEmpty()
        if (binding != null && currentBinding.isNotEmpty() && binding != currentBinding) return true
        // onPause can still be visible (dialogs, shade, multi-window). Never
        // infer visibility from a persisted Flutter route or a socket status.
        val power = context.getSystemService(Context.POWER_SERVICE) as PowerManager
        val keyguard = context.getSystemService(Context.KEYGUARD_SERVICE) as KeyguardManager
        return activityVisible && power.isInteractive && !keyguard.isKeyguardLocked
    }

    fun setLifecycle(context: Context, value: String) {
        appLifecycle = value
        sharedPreferences(context).edit().putString(KEY_PRESENTATION_LIFECYCLE, value).apply()
    }

    private fun presentationContext(context: Context): PresentationContext {
        val preferences = sharedPreferences(context)
        val storedLifecycle = preferences
            .getString(KEY_PRESENTATION_LIFECYCLE, appLifecycle)
            ?: appLifecycle
        val storedSection = preferences
            .getString(KEY_PRESENTATION_SECTION, section)
            ?: section
        val storedThreadId = preferences
            .getString(KEY_PRESENTATION_THREAD_ID, activeThreadId.orEmpty())
            ?.trim()
            ?.takeIf { it.isNotEmpty() }
        return PresentationContext(
            appLifecycle = storedLifecycle,
            section = storedSection,
            activeThreadId = storedThreadId,
        )
    }

    @Suppress("DEPRECATION")
    private fun sharedPreferences(context: Context) =
        context.getSharedPreferences(
            PREFERENCES,
            Context.MODE_PRIVATE or Context.MODE_MULTI_PROCESS,
        )

    private data class PresentationContext(
        val appLifecycle: String,
        val section: String,
        val activeThreadId: String?,
    )

    fun jsonObjectToMap(value: JSONObject): Map<String, Any?> =
        value.keys().asSequence().associateWith { key -> jsonValue(value.opt(key)) }

    private fun jsonArrayToList(value: JSONArray): List<Any?> =
        (0 until value.length()).map { index -> jsonValue(value.opt(index)) }

    private fun jsonValue(value: Any?): Any? = when (value) {
        null, JSONObject.NULL -> null
        is JSONObject -> jsonObjectToMap(value)
        is JSONArray -> jsonArrayToList(value)
        is Number, is Boolean, is String -> value
        else -> value.toString()
    }

    private fun jsonSafeMap(input: Map<String, Any?>): Map<String, Any?> =
        input.mapValues { (_, value) -> jsonSafeValue(value) }

    private fun jsonSafeValue(value: Any?): Any? = when (value) {
        null -> JSONObject.NULL
        is Map<*, *> -> JSONObject(
            value.entries.associate { (key, entry) ->
                key.toString() to jsonSafeValue(entry)
            },
        )
        is Iterable<*> -> JSONArray(value.map(::jsonSafeValue))
        is Array<*> -> JSONArray(value.map(::jsonSafeValue))
        is Number, is Boolean, is String -> value
        else -> value.toString()
    }
}
