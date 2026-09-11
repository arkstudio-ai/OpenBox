package com.bossip.bipmobile

import org.junit.Assert.assertEquals
import org.junit.Test

class NotificationAuthorizationTest {
    @Test fun freshAndroid13InstallNeedsSystemPermission() {
        assertEquals("notDetermined", NotificationAuthorization.status(false, false, true, false))
    }

    @Test fun systemDenialDoesNotTriggerAutomaticPermissionRequestsAgain() {
        assertEquals("denied", NotificationAuthorization.status(false, false, true, true))
    }

    @Test fun existingOrPreGrantedPermissionDoesNotNeedAnAppConsentFlag() {
        assertEquals("granted", NotificationAuthorization.status(true, true, true, false))
    }

    @Test fun grantingInSystemSettingsOverridesPreviousDenial() {
        assertEquals("granted", NotificationAuthorization.status(true, true, true, true))
    }

    @Test fun appNotificationsDisabledOnOlderAndroidAreDeniedWithoutARuntimeRequest() {
        assertEquals("denied", NotificationAuthorization.status(true, false, true, false))
    }

    @Test fun disabledBusinessChannelBlocksAnOtherwiseGrantedPermission() {
        assertEquals("denied", NotificationAuthorization.status(true, true, false, true))
    }
}
