package com.bossip.bipmobile

import android.app.Notification
import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.PendingIntent
import android.app.Service
import android.content.Intent
import android.content.pm.ServiceInfo
import android.os.Build
import android.os.IBinder
import android.os.PowerManager
import io.flutter.plugin.common.MethodChannel

/** Started while visible, before recording. No restart may resurrect a call. */
class VoiceCallService : Service() {
    private var wakeLock: PowerManager.WakeLock? = null
    private var overlay: VoiceCallOverlay? = null
    private var name = "BossIP"
    private var connecting = "Connecting…"
    private var ongoing = "Call in progress"
    private var returnLabel = "Return to call"
    private var endLabel = "End call"
    private var connectedAt: Long? = null
    val overlayVisible: Boolean get() = overlay?.visible == true

    override fun onBind(intent: Intent?): IBinder? = null

    override fun onStartCommand(intent: Intent?, flags: Int, startId: Int): Int {
        if (intent?.action == ACTION_END) {
            VoiceCallBridge.emit?.invoke("end", null)
            stopSelf()
            return START_NOT_STICKY
        }
        if (intent == null) {
            stopSelf()
            return START_NOT_STICKY
        }
        name = intent.getStringExtra("name") ?: name
        connecting = intent.getStringExtra("connecting") ?: connecting
        ongoing = intent.getStringExtra("ongoing") ?: ongoing
        returnLabel = intent.getStringExtra("returnLabel") ?: returnLabel
        endLabel = intent.getStringExtra("endLabel") ?: endLabel
        try {
            val manager = getSystemService(NotificationManager::class.java)
            if (Build.VERSION.SDK_INT >= 26) {
                manager.createNotificationChannel(NotificationChannel(
                    CHANNEL, "BossIP · $ongoing", NotificationManager.IMPORTANCE_LOW,
                ).apply { setSound(null, null) })
            }
            if (Build.VERSION.SDK_INT >= 30) {
                startForeground(ID, notification(),
                    ServiceInfo.FOREGROUND_SERVICE_TYPE_MICROPHONE or
                        ServiceInfo.FOREGROUND_SERVICE_TYPE_MEDIA_PLAYBACK)
            } else {
                startForeground(ID, notification())
            }
            instance = this
            if (wakeLock == null) {
                wakeLock = getSystemService(PowerManager::class.java).newWakeLock(
                    PowerManager.PARTIAL_WAKE_LOCK, "BossIP:VoiceCall",
                ).apply { acquire() }
            }
            overlay = overlay ?: VoiceCallOverlay(this, returnLabel)
            setVisible(VoiceCallBridge.visible)
            startResult?.success(null)
        } catch (error: Exception) {
            startResult?.error("CALL_SERVICE_FAILED", error.javaClass.simpleName, null)
            stopSelf()
        }
        startResult = null
        return START_NOT_STICKY
    }

    fun connected(at: Long?) {
        connectedAt = at ?: System.currentTimeMillis()
        getSystemService(NotificationManager::class.java).notify(ID, notification())
    }

    fun setVisible(visible: Boolean) {
        if (visible) overlay?.hide() else overlay?.show()
    }

    private fun notification(): Notification {
        val open = PendingIntent.getActivity(this, ID, openIntent(),
            PendingIntent.FLAG_UPDATE_CURRENT or PendingIntent.FLAG_IMMUTABLE)
        val end = PendingIntent.getService(this, ID,
            Intent(this, VoiceCallService::class.java).setAction(ACTION_END),
            PendingIntent.FLAG_UPDATE_CURRENT or PendingIntent.FLAG_IMMUTABLE)
        val builder = if (Build.VERSION.SDK_INT >= 26) Notification.Builder(this, CHANNEL)
            else @Suppress("DEPRECATION") Notification.Builder(this)
        return builder.setSmallIcon(R.drawable.ic_voice_call)
            .setContentTitle("$name · ${if (connectedAt == null) connecting else ongoing}")
            .setContentText(returnLabel)
            .setContentIntent(open)
            .setCategory(Notification.CATEGORY_CALL)
            .setOngoing(true)
            .setOnlyAlertOnce(true)
            .setVisibility(Notification.VISIBILITY_PRIVATE)
            .setUsesChronometer(connectedAt != null)
            .setWhen(connectedAt ?: System.currentTimeMillis())
            .addAction(Notification.Action.Builder(null, endLabel, end).build())
            .build()
    }

    fun openIntent() = Intent(this, MainActivity::class.java).apply {
        action = VoiceCallBridge.ACTION_OPEN
        flags = Intent.FLAG_ACTIVITY_NEW_TASK or Intent.FLAG_ACTIVITY_SINGLE_TOP or
            Intent.FLAG_ACTIVITY_CLEAR_TOP
    }

    override fun onTaskRemoved(rootIntent: Intent?) {
        VoiceCallBridge.emit?.invoke("end", null)
        stopSelf()
    }

    override fun onDestroy() {
        overlay?.hide()
        overlay = null
        if (wakeLock?.isHeld == true) wakeLock?.release()
        wakeLock = null
        stopForeground(STOP_FOREGROUND_REMOVE)
        if (instance === this) instance = null
        super.onDestroy()
    }

    companion object {
        private const val CHANNEL = "bossip_voice_call"
        private const val ID = 9043
        private const val ACTION_END = "com.bossip.bipmobile.VOICE_END"
        var instance: VoiceCallService? = null
        var startResult: MethodChannel.Result? = null
    }
}
