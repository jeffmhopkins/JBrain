import java.io.FileInputStream
import java.util.Properties

plugins {
    alias(libs.plugins.android.application)
    alias(libs.plugins.kotlin.android)
    alias(libs.plugins.kotlin.compose)
}

// Default server domain + access key come from local.properties (gitignored) → baked
// into BuildConfig as the prefilled defaults, so you can build one APK for the family
// and each phone only needs a Name. They remain editable on-device. Falls back to env
// vars (CI), then a harmless default so a fresh checkout still configures.
val localProps = Properties().apply {
    val f = rootProject.file("local.properties")
    if (f.exists()) FileInputStream(f).use { load(it) }
}
fun secret(key: String, default: String): String =
    localProps.getProperty(key) ?: System.getenv(key) ?: default

android {
    namespace = "com.jbrain.tracker"
    compileSdk = 35

    defaultConfig {
        applicationId = "com.jbrain.tracker"
        minSdk = 26
        targetSdk = 34
        versionCode = 8
        versionName = "1.7"

        buildConfigField("String", "JBRAIN_DOMAIN", "\"${secret("JBRAIN_DOMAIN", "https://example.com")}\"")
        buildConfigField("String", "JBRAIN_KEY", "\"${secret("JBRAIN_KEY", "")}\"")
    }

    buildFeatures {
        compose = true
        buildConfig = true
    }

    compileOptions {
        sourceCompatibility = JavaVersion.VERSION_17
        targetCompatibility = JavaVersion.VERSION_17
    }
    kotlinOptions {
        jvmTarget = "17"
    }

    // Sign the release build. CI supplies a real keystore via env vars
    // (KEYSTORE_FILE/KEYSTORE_PASSWORD/KEY_ALIAS/KEY_PASSWORD) for a stable signature so
    // updates install in place; without one we fall back to the debug key so the APK is
    // still signed and sideload-installable.
    val releaseKeystore = System.getenv("KEYSTORE_FILE")?.takeIf { file(it).exists() }
    signingConfigs {
        create("release") {
            if (releaseKeystore != null) {
                storeFile = file(releaseKeystore)
                storePassword = System.getenv("KEYSTORE_PASSWORD")
                keyAlias = System.getenv("KEY_ALIAS")
                keyPassword = System.getenv("KEY_PASSWORD")
            }
        }
    }

    buildTypes {
        release {
            isMinifyEnabled = false
            proguardFiles(getDefaultProguardFile("proguard-android-optimize.txt"), "proguard-rules.pro")
            signingConfig = if (releaseKeystore != null)
                signingConfigs.getByName("release")
            else
                signingConfigs.getByName("debug")
        }
    }
}

dependencies {
    implementation(platform(libs.compose.bom))
    implementation(libs.androidx.core.ktx)
    implementation(libs.androidx.lifecycle.runtime.ktx)
    implementation(libs.androidx.activity.compose)

    implementation(libs.compose.ui)
    implementation(libs.compose.ui.tooling.preview)
    debugImplementation(libs.compose.ui.tooling)
    implementation(libs.compose.material3)

    implementation(libs.kotlinx.coroutines.android)
    implementation(libs.okhttp)

    // Background location (FusedLocationProviderClient) for the trail tracker.
    implementation(libs.play.services.location)

    // Wearable Data Layer: receive notes dictated on the watch and forward them.
    implementation(libs.play.services.wearable)

    // Embed the Wear OS watch app inside this APK so a single phone install carries the
    // watch app, which the Wear companion delivers to a paired watch. Both modules share
    // an applicationId + signing key, which the embedding requires.
    wearApp(project(":wear"))
}
