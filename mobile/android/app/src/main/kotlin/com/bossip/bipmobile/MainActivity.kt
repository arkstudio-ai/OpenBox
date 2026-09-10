package com.bossip.bipmobile

import android.app.Activity
import android.content.Intent
import com.alipay.sdk.app.PayTask
import io.flutter.embedding.engine.FlutterEngine
import io.flutter.plugin.common.MethodChannel
import java.io.File

class MainActivity : NotificationActivity() {
    private val saveFileRequestCode = 9021
    private var pendingSaveResult: MethodChannel.Result? = null
    private var pendingSavePath: String? = null

    override fun configureFlutterEngine(flutterEngine: FlutterEngine) {
        super.configureFlutterEngine(flutterEngine)
        MethodChannel(
            flutterEngine.dartExecutor.binaryMessenger,
            "com.bossip.bipmobile/alipay",
        ).setMethodCallHandler { call, result ->
            if (call.method != "pay") {
                result.notImplemented()
                return@setMethodCallHandler
            }
            val orderString = call.argument<String>("orderString")
            if (orderString.isNullOrBlank()) {
                result.error("INVALID_ORDER", "Missing Alipay order string", null)
                return@setMethodCallHandler
            }
            Thread {
                try {
                    val paymentResult = PayTask(this).payV2(orderString, true)
                    runOnUiThread { result.success(paymentResult) }
                } catch (error: Throwable) {
                    runOnUiThread {
                        result.error("PAY_FAILED", error.javaClass.simpleName, null)
                    }
                }
            }.start()
        }

        MethodChannel(
            flutterEngine.dartExecutor.binaryMessenger,
            "com.bossip.bipmobile/download",
        ).setMethodCallHandler { call, result ->
            if (call.method != "saveFile") {
                result.notImplemented()
                return@setMethodCallHandler
            }
            if (pendingSaveResult != null) {
                result.error("SAVE_BUSY", "Another file save is active", null)
                return@setMethodCallHandler
            }
            val path = call.argument<String>("path")
            val name = call.argument<String>("name")
            val mimeType = call.argument<String>("mimeType")
                ?.takeIf { it.isNotBlank() }
                ?: "application/octet-stream"
            if (path.isNullOrBlank() || name.isNullOrBlank() || !File(path).isFile) {
                result.error("INVALID_FILE", "The downloaded file is unavailable", null)
                return@setMethodCallHandler
            }

            pendingSaveResult = result
            pendingSavePath = path
            try {
                startActivityForResult(
                    Intent(Intent.ACTION_CREATE_DOCUMENT).apply {
                        addCategory(Intent.CATEGORY_OPENABLE)
                        type = mimeType
                        putExtra(Intent.EXTRA_TITLE, name)
                    },
                    saveFileRequestCode,
                )
            } catch (error: Throwable) {
                clearPendingSave()
                result.error("SAVE_UNAVAILABLE", error.javaClass.simpleName, null)
            }
        }
    }

    @Deprecated("Deprecated by Android; retained for FlutterActivity compatibility")
    override fun onActivityResult(requestCode: Int, resultCode: Int, data: Intent?) {
        super.onActivityResult(requestCode, resultCode, data)
        if (requestCode != saveFileRequestCode) return

        val callback = pendingSaveResult ?: return
        val sourcePath = pendingSavePath
        val destination = data?.data
        if (resultCode != Activity.RESULT_OK || destination == null || sourcePath == null) {
            clearPendingSave()
            callback.success(false)
            return
        }

        Thread {
            try {
                File(sourcePath).inputStream().use { input ->
                    val output = contentResolver.openOutputStream(destination, "w")
                        ?: throw IllegalStateException("Cannot open the selected destination")
                    output.use { input.copyTo(it) }
                }
                runOnUiThread {
                    clearPendingSave()
                    callback.success(true)
                }
            } catch (error: Throwable) {
                runOnUiThread {
                    clearPendingSave()
                    callback.error("SAVE_FAILED", error.javaClass.simpleName, null)
                }
            }
        }.start()
    }

    private fun clearPendingSave() {
        pendingSaveResult = null
        pendingSavePath = null
    }
}
