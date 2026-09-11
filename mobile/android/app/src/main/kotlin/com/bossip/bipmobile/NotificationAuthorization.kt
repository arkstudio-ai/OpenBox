package com.bossip.bipmobile

/** Effective Android permission state shared with the Flutter push controller. */
internal object NotificationAuthorization {
    fun status(
        runtimePermissionGranted: Boolean,
        notificationsEnabled: Boolean,
        channelEnabled: Boolean,
        permissionRequested: Boolean,
    ): String {
        // Before Android 13 there is no runtime notification permission;
        // callers pass true and the app/channel settings remain authoritative.
        if (!runtimePermissionGranted) {
            return if (permissionRequested) "denied" else "notDetermined"
        }
        return if (notificationsEnabled && channelEnabled) "granted" else "denied"
    }
}
