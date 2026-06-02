import java.io.FileInputStream
import java.util.Properties

plugins {
    alias(libs.plugins.android.application)
    alias(libs.plugins.kotlin.android)
    alias(libs.plugins.kotlin.compose)
}

// Pull the server domain + access key out of local.properties (gitignored) so the
// secret key is baked into BuildConfig at build time and never has to be typed on
// the watch nor committed. Falls back to env vars (handy for CI), then to a
// harmless default so a fresh checkout still configures.
val localProps = Properties().apply {
    val f = rootProject.file("local.properties")
    if (f.exists()) FileInputStream(f).use { load(it) }
}
fun secret(key: String, default: String): String =
    localProps.getProperty(key) ?: System.getenv(key) ?: default

android {
    namespace = "com.jbrain.watch"
    compileSdk = 35

    defaultConfig {
        applicationId = "com.jbrain.watch"
        minSdk = 30
        targetSdk = 34
        versionCode = 1
        versionName = "1.0"

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
    // (KEYSTORE_FILE/KEYSTORE_PASSWORD/KEY_ALIAS/KEY_PASSWORD) for a stable signature
    // so updates install in place; without one we fall back to the debug key below so
    // the APK is still signed and installable (e.g. via Wear Installer 2).
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
    implementation(libs.wear.compose.material)
    implementation(libs.wear.compose.foundation)

    implementation(libs.kotlinx.coroutines.android)
    implementation(libs.okhttp)

    // Background location (FusedLocationProviderClient) for the trail tracker.
    implementation(libs.play.services.location)

    // Tile (one-tap capture from the watch face)
    implementation(libs.wear.tiles)
    implementation(libs.wear.tiles.material)
    implementation(libs.wear.protolayout)
    implementation(libs.wear.protolayout.material)
    implementation(libs.guava)
}
