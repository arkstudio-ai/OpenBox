package com.bossip.bipmobile

import android.graphics.Color
import android.graphics.PixelFormat
import android.graphics.drawable.GradientDrawable
import android.os.Build
import android.provider.Settings
import android.view.Gravity
import android.view.MotionEvent
import android.view.ViewConfiguration
import android.view.WindowManager
import android.widget.ImageButton
import kotlin.math.abs

/** Small, draggable return button. Removing/hiding it never ends the call. */
class VoiceCallOverlay(private val service: VoiceCallService, private val label: String) {
    private val manager = service.getSystemService(WindowManager::class.java)
    private val density = service.resources.displayMetrics.density
    private var button: ImageButton? = null
    private var savedX = 0
    private var savedY = (160 * density).toInt()
    val visible: Boolean get() = button != null

    fun show() {
        if (button != null || !Settings.canDrawOverlays(service)) return
        val size = (56 * density).toInt()
        val params = WindowManager.LayoutParams(
            size, size,
            if (Build.VERSION.SDK_INT >= 26) WindowManager.LayoutParams.TYPE_APPLICATION_OVERLAY
            else @Suppress("DEPRECATION") WindowManager.LayoutParams.TYPE_PHONE,
            WindowManager.LayoutParams.FLAG_NOT_FOCUSABLE or
                WindowManager.LayoutParams.FLAG_NOT_TOUCH_MODAL,
            PixelFormat.TRANSLUCENT,
        ).apply {
            gravity = Gravity.TOP or Gravity.LEFT
            x = savedX
            y = savedY
        }
        val view = ImageButton(service).apply {
            contentDescription = label
            setImageResource(R.drawable.ic_voice_call)
            setColorFilter(Color.WHITE)
            val padding = (16 * density).toInt()
            setPadding(padding, padding, padding, padding)
            background = GradientDrawable().apply {
                shape = GradientDrawable.OVAL
                setColor(Color.rgb(24, 156, 91))
                setStroke((2 * density).toInt(), Color.WHITE)
            }
            elevation = 8 * density
            setOnClickListener {
                // Direct user action through a visible overlay may bring the
                // existing task forward. No background launch or new call.
                runCatching { service.startActivity(service.openIntent()) }
            }
        }
        var downX = 0f
        var downY = 0f
        var startX = 0
        var startY = 0
        var moved = false
        val slop = ViewConfiguration.get(service).scaledTouchSlop
        view.setOnTouchListener { _, event ->
            when (event.actionMasked) {
                MotionEvent.ACTION_DOWN -> {
                    downX = event.rawX; downY = event.rawY
                    startX = params.x; startY = params.y; moved = false
                    true
                }
                MotionEvent.ACTION_MOVE -> {
                    val dx = event.rawX - downX
                    val dy = event.rawY - downY
                    moved = moved || abs(dx) > slop || abs(dy) > slop
                    if (moved) {
                        val metrics = service.resources.displayMetrics
                        params.x = (startX + dx.toInt()).coerceIn(0, (metrics.widthPixels - size).coerceAtLeast(0))
                        params.y = (startY + dy.toInt()).coerceIn(0, (metrics.heightPixels - size * 2).coerceAtLeast(0))
                        runCatching { manager.updateViewLayout(view, params) }
                        savedX = params.x; savedY = params.y
                    }
                    true
                }
                MotionEvent.ACTION_UP -> {
                    if (!moved) view.performClick()
                    true
                }
                MotionEvent.ACTION_CANCEL -> true
                else -> false
            }
        }
        try {
            manager.addView(view, params)
            button = view
        } catch (_: Exception) {
            // Permission can be revoked at any time. The foreground service
            // and its notification keep the call alive without an overlay.
        }
    }

    fun hide() {
        button?.let { runCatching { manager.removeView(it) } }
        button = null
    }
}
