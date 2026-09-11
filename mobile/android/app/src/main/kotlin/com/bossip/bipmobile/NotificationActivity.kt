package com.bossip.bipmobile

import android.Manifest
import android.app.Notification
import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.PendingIntent
import android.content.Context
import android.content.Intent
import android.content.pm.ApplicationInfo
import android.content.pm.PackageManager
import android.net.Uri
import android.os.Build
import android.os.Bundle
import android.os.Handler
import android.os.Looper
import android.provider.Settings
import cn.jpush.android.api.JPushInterface
import io.flutter.embedding.android.FlutterActivity
import io.flutter.embedding.engine.FlutterEngine
import io.flutter.plugin.common.MethodChannel
import org.json.JSONObject
import java.text.SimpleDateFormat
import java.util.Date
import java.util.Locale
import java.util.TimeZone

open class NotificationActivity : FlutterActivity() {
    private var systemNotificationChannel: MethodChannel? = null
    private var pendingAuthorizationCompletion: ((Boolean) -> Unit)? = null
    private var notificationUserId: String? = null
    private val pendingEvents = mutableListOf<Map<String, Any?>>()
    private var dartNotificationBridgeReady = false
    private var initialNotificationPayload: Map<String, Any?>? = null
    private var jpushRegistrationId: String? = null
    private var jpushInitialized = false
    private val localTestHandler = Handler(Looper.getMainLooper())

    override fun configureFlutterEngine(flutterEngine: FlutterEngine) {
        super.configureFlutterEngine(flutterEngine)
        jpushRegistrationId = BossIpPushBridge.registrationId(this)
        systemNotificationChannel = MethodChannel(
            flutterEngine.dartExecutor.binaryMessenger,
            SYSTEM_NOTIFICATION_CHANNEL,
        ).also { channel ->
            channel.setMethodCallHandler { call, result ->
                when (call.method) {
                    "requestAuthorization" -> requestNotificationAuthorization(result)
                    "getAuthorizationStatus" -> result.success(notificationAuthorizationStatus())
                    "getDeviceToken" -> result.success(
                        jpushRegistrationId ?: BossIpPushBridge.registrationId(this),
                    )
                    // Keep the cross-platform MethodChannel contract complete.
                    // Android never routes through APNs, so production is the
                    // stable diagnostic/default value sent with JPush binding.
                    "getApnsEnvironment" -> result.success("production")
                    "getInitialNotification" -> {
                        result.success(initialNotificationPayload)
                        initialNotificationPayload = null
                    }
                    "getAppVersion" -> {
                        val info = packageManager.getPackageInfo(packageName, 0)
                        val code = if (Build.VERSION.SDK_INT >= 28) info.longVersionCode else info.versionCode.toLong()
                        result.success("${info.versionName}+$code")
                    }
                    "openSettings" -> {
                        val intent = if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
                            Intent(Settings.ACTION_APP_NOTIFICATION_SETTINGS).putExtra(Settings.EXTRA_APP_PACKAGE, packageName)
                        } else {
                            Intent(Settings.ACTION_APPLICATION_DETAILS_SETTINGS, Uri.parse("package:$packageName"))
                        }
                        startActivity(intent)
                        result.success(true)
                    }
                    "clearNotifications" -> {
                        localTestHandler.removeCallbacksAndMessages(null)
                        initialNotificationPayload = null
                        pendingEvents.removeAll { it["method"].toString().startsWith("notification") }
                        BossIpPushBridge.clearStoredOpen(this)
                        (getSystemService(Context.NOTIFICATION_SERVICE) as NotificationManager).cancelAll()
                        result.success(true)
                    }
                    "flushNotificationEvents" -> {
                        val events = pendingEvents.toList()
                        pendingEvents.clear()
                        dartNotificationBridgeReady = true
                        result.success(events)
                    }
                    "showLocalNotification" -> showLocalNotification(
                        normalizeMap(call.arguments),
                        result,
                    )
                    "setPresentationContext" -> {
                        setPresentationContext(normalizeMap(call.arguments))
                        result.success(true)
                    }
                    else -> result.notImplemented()
                }
            }
        }
        BossIpPushBridge.attach(this) { method, rawPayload ->
            if (method == "deviceTokenUpdated") {
                jpushRegistrationId = rawPayload["token"]?.toString()
            }
            val payload = if (
                method == "notificationReceived" || method == "notificationOpened"
            ) {
                notificationPayload(rawPayload)
            } else {
                jsonSafeMap(rawPayload)
            }
            publishSystemNotificationEvent(method, payload)
        }
        createSystemNotificationChannel()
        captureNotificationOpen(intent, initial = true)
    }

    private fun setPresentationContext(context: Map<String, Any?>) {
        notificationUserId = context["userId"]?.toString()?.trim()?.takeIf { it.isNotEmpty() }
        BossIpPushBridge.updatePresentationContext(
            context = this,
            appLifecycle = context["appLifecycle"]?.toString() ?: "resumed",
            section = context["section"]?.toString() ?: "newTask",
            activeThreadId = context["activeSessionId"]?.toString(),
            userId = context["userId"]?.toString(),
            workspaceId = context["workspaceId"]?.toString(),
            bindingId = context["bindingId"]?.toString(),
        )
        initializeJPush()
    }

    override fun onPostResume() {
        super.onPostResume()
        BossIpPushBridge.setLifecycle(this, "resumed")
        initializeJPush()
        publishAuthorizationChanged()
    }

    override fun onStart() {
        super.onStart()
        BossIpPushBridge.activityVisible = true
        BossIpPushBridge.setLifecycle(this, "inactive")
    }

    override fun onPause() {
        BossIpPushBridge.setLifecycle(this, "inactive")
        super.onPause()
    }

    override fun onStop() {
        BossIpPushBridge.activityVisible = false
        BossIpPushBridge.setLifecycle(this, "paused")
        super.onStop()
    }

    override fun onNewIntent(intent: Intent) {
        super.onNewIntent(intent)
        setIntent(intent)
        captureNotificationOpen(intent, initial = false)
    }

    override fun onRequestPermissionsResult(
        requestCode: Int,
        permissions: Array<out String>,
        grantResults: IntArray,
    ) {
        super.onRequestPermissionsResult(requestCode, permissions, grantResults)
        if (requestCode != REQUEST_POST_NOTIFICATIONS) return
        // Read the effective OS state, including a disabled notification channel.
        val granted = notificationAuthorizationStatus() == "granted"
        val completion = pendingAuthorizationCompletion
        pendingAuthorizationCompletion = null
        completion?.invoke(granted)
        publishAuthorizationChanged()
    }

    private fun requestNotificationAuthorization(result: MethodChannel.Result) {
        requestPostNotificationPermission { granted ->
            if (granted) initializeJPush()
            result.success(granted)
        }
    }

    private fun requestPostNotificationPermission(completion: (Boolean) -> Unit) {
        if (Build.VERSION.SDK_INT < Build.VERSION_CODES.TIRAMISU) {
            val granted = notificationAuthorizationStatus() == "granted"
            publishAuthorizationChanged()
            completion(granted)
            return
        }
        if (checkSelfPermission(Manifest.permission.POST_NOTIFICATIONS)
            == PackageManager.PERMISSION_GRANTED
        ) {
            val granted = notificationAuthorizationStatus() == "granted"
            publishAuthorizationChanged()
            completion(granted)
            return
        }
        pendingAuthorizationCompletion?.invoke(false)
        pendingAuthorizationCompletion = completion
        getPreferences(Context.MODE_PRIVATE).edit().putBoolean("push_permission_requested", true).apply()
        requestPermissions(
            arrayOf(Manifest.permission.POST_NOTIFICATIONS),
            REQUEST_POST_NOTIFICATIONS,
        )
    }

    private fun publishAuthorizationChanged() {
        publishSystemNotificationEvent(
            "authorizationChanged",
            mapOf(
                "platform" to "android",
                "provider" to "jpush",
                "status" to notificationAuthorizationStatus(),
            ),
        )
    }

    private fun notificationAuthorizationStatus(): String {
        val manager = getSystemService(Context.NOTIFICATION_SERVICE) as NotificationManager
        return NotificationAuthorization.status(
            runtimePermissionGranted = Build.VERSION.SDK_INT < Build.VERSION_CODES.TIRAMISU ||
                checkSelfPermission(Manifest.permission.POST_NOTIFICATIONS) == PackageManager.PERMISSION_GRANTED,
            notificationsEnabled = Build.VERSION.SDK_INT < 24 || manager.areNotificationsEnabled(),
            channelEnabled = Build.VERSION.SDK_INT < 26 ||
                manager.getNotificationChannel(SYSTEM_NOTIFICATION_ANDROID_CHANNEL_ID)?.importance != NotificationManager.IMPORTANCE_NONE,
            permissionRequested = getPreferences(Context.MODE_PRIVATE).getBoolean("push_permission_requested", false),
        )
    }

    private fun showLocalNotification(
        payload: Map<String, Any?>,
        result: MethodChannel.Result,
    ) {
        val delay = (payload["delaySeconds"] as? Number)?.toLong() ?: 0
        if (delay > 0) {
            localTestHandler.postDelayed({
                showLocalNotification(payload - "delaySeconds", object : MethodChannel.Result {
                    override fun success(result: Any?) {}
                    override fun error(code: String, message: String?, details: Any?) {}
                    override fun notImplemented() {}
                })
            }, delay.coerceAtMost(30) * 1000)
            result.success(true)
            return
        }
        if (BossIpPushBridge.shouldSuppressForegroundPresentation(this, payload)) {
            result.success(false)
            return
        }
        if (
            Build.VERSION.SDK_INT >= Build.VERSION_CODES.TIRAMISU &&
            checkSelfPermission(Manifest.permission.POST_NOTIFICATIONS)
            != PackageManager.PERMISSION_GRANTED
        ) {
            result.error(
                "notification_permission_denied",
                "POST_NOTIFICATIONS permission has not been granted",
                null,
            )
            return
        }
        createSystemNotificationChannel()
        val manager = getSystemService(Context.NOTIFICATION_SERVICE) as NotificationManager
        val notificationId = (payload["notificationId"] as? Number)?.toInt()
            ?: (payload["eventId"]?.toString()?.hashCode() ?: System.currentTimeMillis().toInt())
        val openIntent = Intent(this, MainActivity::class.java).apply {
            action = BossIpPushBridge.ACTION_SYSTEM_NOTIFICATION_OPENED
            flags = Intent.FLAG_ACTIVITY_SINGLE_TOP or Intent.FLAG_ACTIVITY_CLEAR_TOP
            putExtra(
                BossIpPushBridge.EXTRA_SYSTEM_NOTIFICATION_PAYLOAD,
                HashMap(jsonSafeMap(payload)),
            )
        }
        val pendingIntent = PendingIntent.getActivity(
            this,
            notificationId,
            openIntent,
            PendingIntent.FLAG_UPDATE_CURRENT or PendingIntent.FLAG_IMMUTABLE,
        )
        val builder = if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
            Notification.Builder(this, SYSTEM_NOTIFICATION_ANDROID_CHANNEL_ID)
        } else {
            @Suppress("DEPRECATION")
            Notification.Builder(this)
        }
        val notification = builder
            .setSmallIcon(R.drawable.jpush_notification_icon)
            .setContentTitle(payload["title"]?.toString() ?: "BossIP")
            .setContentText(payload["body"]?.toString() ?: "")
            .setStyle(
                Notification.BigTextStyle()
                    .bigText(payload["body"]?.toString() ?: ""),
            )
            .setContentIntent(pendingIntent)
            .setAutoCancel(true)
            .setShowWhen(true)
            .build()
        manager.notify(notificationId, notification)
        result.success(true)
    }

    private fun createSystemNotificationChannel() {
        if (Build.VERSION.SDK_INT < Build.VERSION_CODES.O) return
        val manager = getSystemService(Context.NOTIFICATION_SERVICE) as NotificationManager
        val existing = manager.getNotificationChannel(SYSTEM_NOTIFICATION_ANDROID_CHANNEL_ID)
        if (existing != null) return
        val channel = NotificationChannel(
            SYSTEM_NOTIFICATION_ANDROID_CHANNEL_ID,
            "BossIP System Notifications",
            NotificationManager.IMPORTANCE_DEFAULT,
        ).apply {
            description = "Native system notifications for BossIP task updates"
        }
        manager.createNotificationChannel(channel)
    }

    private fun captureNotificationOpen(intent: Intent?, initial: Boolean) {
        val payload = notificationPayloadFromIntent(intent)
            ?: BossIpPushBridge.consumeStoredOpen(this)?.let(::notificationPayload)
            ?: return
        if (initial) {
            initialNotificationPayload = payload
        }
        publishSystemNotificationEvent("notificationOpened", payload)
    }

    private fun notificationPayloadFromIntent(intent: Intent?): Map<String, Any?>? {
        if (intent == null) return null
        // With an explicit intent target the SDK does not call
        // onNotifyMessageOpened. Huawei uses Intent.data; JPush and the other
        // vendors use JMessageExtra. Both wrap our payload in n_extras.
        val vendorPayload = PushIntentPayload.parse(intent.data?.toString())
            ?: PushIntentPayload.parse(intent.getStringExtra("JMessageExtra"))
        if (vendorPayload != null) {
            BossIpPushBridge.clearStoredOpen(this)
            return notificationPayload(vendorPayload)
        }
        val explicitPayload = if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.TIRAMISU) {
            intent.getSerializableExtra(
                BossIpPushBridge.EXTRA_SYSTEM_NOTIFICATION_PAYLOAD,
                HashMap::class.java,
            )
        } else {
            @Suppress("DEPRECATION")
            intent.getSerializableExtra(
                BossIpPushBridge.EXTRA_SYSTEM_NOTIFICATION_PAYLOAD,
            ) as? HashMap<*, *>
        }
        if (explicitPayload != null) {
            BossIpPushBridge.clearStoredOpen(this)
            return notificationPayload(normalizeMap(explicitPayload))
        }
        val extras = intent.extras ?: return null
        val direct = bundleToMap(extras)
        if (direct.containsKey("type") || direct.containsKey("route")) {
            BossIpPushBridge.clearStoredOpen(this)
            return notificationPayload(direct)
        }
        val jpushExtras = extras.getString(JPushInterface.EXTRA_EXTRA)
        if (!jpushExtras.isNullOrBlank()) {
            val parsed = runCatching {
                BossIpPushBridge.jsonObjectToMap(JSONObject(jpushExtras))
            }.getOrNull()
            if (!parsed.isNullOrEmpty()) {
                BossIpPushBridge.clearStoredOpen(this)
                return notificationPayload(parsed)
            }
        }
        return null
    }

    private fun notificationPayload(input: Map<String, Any?>): Map<String, Any?> {
        return jsonSafeMap(input).toMutableMap().apply {
            put("platform", "android")
            putIfAbsent("provider", if (jpushRegistrationId == null) "local" else "jpush")
            putIfAbsent("source", get("provider"))
            put("receivedAt", isoNow())
        }
    }

    private fun publishSystemNotificationEvent(method: String, payload: Map<String, Any?>) {
        val event: Map<String, Any?> = mapOf("method" to method, "payload" to payload)
        runOnUiThread {
            if (!dartNotificationBridgeReady) {
                pendingEvents.add(event)
                if (pendingEvents.size > 64) pendingEvents.removeAt(0)
            } else {
                systemNotificationChannel?.invokeMethod(method, payload)
            }
        }
    }

    private fun bundleToMap(bundle: Bundle): Map<String, Any?> {
        return bundle.keySet().associateWith { key -> normalizeValue(bundle.get(key)) }
    }

    private fun normalizeMap(input: Any?): Map<String, Any?> {
        if (input !is Map<*, *>) return emptyMap()
        return input.entries.associate { (key, value) ->
            key.toString() to normalizeValue(value)
        }
    }

    private fun normalizeValue(value: Any?): Any? {
        return when (value) {
            null -> null
            is Map<*, *> -> normalizeMap(value)
            is Bundle -> bundleToMap(value)
            is Array<*> -> value.map { normalizeValue(it) }
            is Iterable<*> -> value.map { normalizeValue(it) }
            is Number, is Boolean, is String -> value
            else -> value.toString()
        }
    }

    private fun jsonSafeMap(input: Map<String, Any?>): Map<String, Any?> {
        return input.mapValues { (_, value) -> normalizeValue(value) }
    }

    private fun isoNow(): String {
        val formatter = SimpleDateFormat("yyyy-MM-dd'T'HH:mm:ss.SSS'Z'", Locale.US)
        formatter.timeZone = TimeZone.getTimeZone("UTC")
        return formatter.format(Date())
    }

    private fun initializeJPush() {
        if (jpushInitialized || notificationUserId == null || notificationAuthorizationStatus() != "granted") return
        // Start after login and OS authorization, including an existing grant
        // or a grant made in system settings while the app was backgrounded.
        val debuggable = applicationInfo.flags and ApplicationInfo.FLAG_DEBUGGABLE != 0
        JPushInterface.setDebugMode(debuggable)
        JPushInterface.setThirdPushEnable(applicationContext, true)
        JPushInterface.setKeepLongConnInBackground(applicationContext, true)
        JPushInterface.init(applicationContext)
        jpushInitialized = true
        JPushInterface.getRegistrationID(applicationContext)
            ?.takeIf { it.isNotBlank() }
            ?.let { registrationId ->
                jpushRegistrationId = registrationId
                BossIpPushBridge.updateRegistrationId(this, registrationId)
            }
    }

    override fun onDestroy() {
        localTestHandler.removeCallbacksAndMessages(null)
        BossIpPushBridge.detach()
        pendingAuthorizationCompletion?.invoke(false)
        pendingAuthorizationCompletion = null
        super.onDestroy()
    }

    companion object {
        private const val SYSTEM_NOTIFICATION_CHANNEL = "bossip/system_notifications"
        private const val SYSTEM_NOTIFICATION_ANDROID_CHANNEL_ID =
            "bossip_system_notifications"
        private const val REQUEST_POST_NOTIFICATIONS = 6101
    }
}
