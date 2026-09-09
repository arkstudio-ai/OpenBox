package com.bossip.bipmobile

import android.app.Activity
import android.app.ActivityManager
import android.content.Intent
import android.os.Bundle
import com.linusu.flutter_web_auth_2.FlutterWebAuth2Plugin

/**
 * Custom Tabs may deliver an OAuth redirect in a separate NEW_TASK. The stock
 * callback then creates an empty AuthenticationManagementActivity in that task,
 * leaving the original tab covering Flutter even though Dart received the URI.
 * Route back to the original app task explicitly, keeping empty task affinities.
 * Modern Auth Tabs complete through the plugin's ActivityResult path instead.
 */
class AuthCallbackActivity : Activity() {
    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        complete(intent)
    }

    override fun onNewIntent(intent: Intent) {
        super.onNewIntent(intent)
        setIntent(intent)
        complete(intent)
    }

    private fun complete(intent: Intent) {
        val uri = intent.data
        if (intent.action != Intent.ACTION_VIEW || uri?.scheme != "com.bossip.bipmobile" ||
            uri.host != "callback" || uri.port != -1 ||
            (!uri.path.isNullOrEmpty() && uri.path != "/")) {
            finish()
            return
        }

        // This only delivers the URI. Logto still verifies the redirect, state,
        // PKCE exchange and ID token. Remove first so duplicates cannot complete
        // twice. After process death/cancellation there is no pending callback:
        // open the app to retry, never manufacture an authenticated session.
        FlutterWebAuth2Plugin.callbacks.remove(uri.scheme)?.success(uri.toString())

        val returnIntent = Intent(this, MainActivity::class.java).apply {
            addFlags(Intent.FLAG_ACTIVITY_CLEAR_TOP or Intent.FLAG_ACTIVITY_SINGLE_TOP)
            // Never forward the OAuth code to Flutter's deep-link router.
        }
        val manager = getSystemService(ActivityManager::class.java)
        val originalTask = manager.appTasks.firstOrNull {
            it.taskInfo.baseIntent.component?.className == MainActivity::class.java.name
        }
        try {
            if (originalTask != null) {
                originalTask.startActivity(this, returnIntent, null)
            } else {
                startActivity(returnIntent.addFlags(Intent.FLAG_ACTIVITY_NEW_TASK))
            }
        } catch (_: IllegalArgumentException) {
            // The system can remove the old task between lookup and dispatch.
            startActivity(returnIntent.addFlags(Intent.FLAG_ACTIVITY_NEW_TASK))
        } finally {
            finish()
        }
    }
}
