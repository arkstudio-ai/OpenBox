package com.bossip.bipmobile

import android.app.Activity
import android.content.Intent
import android.net.Uri
import android.os.Build
import android.provider.Settings
import io.flutter.plugin.common.BinaryMessenger
import io.flutter.plugin.common.MethodChannel

/** Owns the OS permission handoff; the service owns background lifetime. */
class VoiceCallBridge(private val activity: Activity, messenger: BinaryMessenger) {
    private val channel = MethodChannel(messenger, "com.bossip.bipmobile/voice_call")
    private var overlayResult: MethodChannel.Result? = null
    private var returnedFromSettings = false

    init {
        emit = { method, value -> channel.invokeMethod(method, value) }
        channel.setMethodCallHandler { call, result ->
            when (call.method) {
                "state" -> result.success(mapOf(
                    "active" to (VoiceCallService.instance != null),
                    "surface" to "overlay",
                    "visible" to visible,
                    "overlayGranted" to Settings.canDrawOverlays(activity),
                    "overlayVisible" to (VoiceCallService.instance?.overlayVisible == true),
                ))
                "start" -> {
                    if (VoiceCallService.instance != null) {
                        result.success(null)
                    } else {
                        val intent = Intent(activity, VoiceCallService::class.java)
                        for (key in listOf("name", "connecting", "ongoing", "returnLabel", "endLabel")) {
                            intent.putExtra(key, call.argument<String>(key))
                        }
                        VoiceCallService.startResult = result
                        try {
                            if (Build.VERSION.SDK_INT >= 26) activity.startForegroundService(intent)
                            else activity.startService(intent)
                        } catch (error: Exception) {
                            VoiceCallService.startResult = null
                            result.error("CALL_SERVICE_FAILED", error.javaClass.simpleName, null)
                        }
                    }
                }
                "connected" -> {
                    VoiceCallService.instance?.connected(call.argument<Number>("at")?.toLong())
                    result.success(null)
                }
                "end" -> {
                    VoiceCallService.startResult?.error("CALL_CANCELLED", "Call cancelled", null)
                    VoiceCallService.startResult = null
                    activity.stopService(Intent(activity, VoiceCallService::class.java))
                    result.success(null)
                }
                "mute" -> result.success(null)
                "needsOverlayPermission" -> result.success(!Settings.canDrawOverlays(activity))
                "requestOverlayPermission" -> {
                    if (Settings.canDrawOverlays(activity)) {
                        result.success(true)
                    } else if (overlayResult != null) {
                        result.success(false)
                    } else {
                        overlayResult = result
                        try {
                            @Suppress("DEPRECATION")
                            activity.startActivityForResult(
                                Intent(Settings.ACTION_MANAGE_OVERLAY_PERMISSION,
                                    Uri.parse("package:${activity.packageName}")),
                                OVERLAY_REQUEST,
                            )
                        } catch (_: Exception) {
                            overlayResult = null
                            result.success(false)
                        }
                    }
                }
                else -> result.notImplemented()
            }
        }
    }

    fun onActivityResult(requestCode: Int) {
        if (requestCode == OVERLAY_REQUEST) returnedFromSettings = true
    }

    fun onResume() {
        visible = true
        VoiceCallService.instance?.setVisible(true)
        // Start the microphone service only after the permission Activity has
        // returned AND our Activity is foreground again (Android 14+).
        if (returnedFromSettings) {
            returnedFromSettings = false
            overlayResult?.success(Settings.canDrawOverlays(activity))
            overlayResult = null
        }
    }

    fun onStop() {
        visible = false
        VoiceCallService.instance?.setVisible(false)
    }

    fun onIntent(intent: Intent?) {
        if (intent?.action == ACTION_OPEN) {
            intent.action = Intent.ACTION_MAIN // consume once
            emit?.invoke("open", null)
        }
    }

    fun dispose() {
        overlayResult?.success(false)
        overlayResult = null
        channel.setMethodCallHandler(null)
        emit = null
    }

    companion object {
        const val ACTION_OPEN = "com.bossip.bipmobile.VOICE_OPEN"
        const val OVERLAY_REQUEST = 9042
        var visible = true
        var emit: ((String, Any?) -> Unit)? = null
    }
}
