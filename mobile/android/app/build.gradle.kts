plugins {
    id("com.android.application")
    // The Flutter Gradle Plugin must be applied after the Android and Kotlin Gradle plugins.
    id("dev.flutter.flutter-gradle-plugin")
}

android {
    namespace = "com.bossip.bipmobile"
    compileSdk = flutter.compileSdkVersion
    ndkVersion = flutter.ndkVersion

    compileOptions {
        sourceCompatibility = JavaVersion.VERSION_17
        targetCompatibility = JavaVersion.VERSION_17
    }

    defaultConfig {
        applicationId = "com.bossip.bipmobile"
        manifestPlaceholders["JPUSH_PKGNAME"] = "com.bossip.bipmobile"
        manifestPlaceholders["JPUSH_APPKEY"] = "20c609b5064f10d52d8351d8"
        manifestPlaceholders["JPUSH_CHANNEL"] = "developer-default"
        // You can update the following values to match your application needs.
        // For more information, see: https://flutter.dev/to/review-gradle-config.
        minSdk = flutter.minSdkVersion
        targetSdk = flutter.targetSdkVersion
        versionCode = flutter.versionCode
        versionName = flutter.versionName
    }

    buildTypes {
        release {
            // Development fallback. push-vendors.gradle replaces this when
            // android/key.properties supplies the registered release key.
            signingConfig = signingConfigs.getByName("debug")
        }
    }
}

kotlin {
    compilerOptions {
        jvmTarget = org.jetbrains.kotlin.gradle.dsl.JvmTarget.JVM_17
    }
}

flutter {
    source = "../.."
}

dependencies {
    implementation("cn.jiguang.sdk:jpush:6.2.1") {
        exclude(group = "cn.jiguang.sdk", module = "jcore")
    }
    implementation("cn.jiguang.sdk:jcore:5.5.1")
    // Official Alipay App Pay SDK. The order string is generated and signed
    // by our backend; no merchant private key is ever bundled in the APK.
    implementation("com.alipay.sdk:alipaysdk-android:15.8.42")
    testImplementation("junit:junit:4.13.2")
    testImplementation("org.json:json:20240303")
}

apply(from = rootProject.file("push-vendors.gradle"))
