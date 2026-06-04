plugins {
    alias(libs.plugins.android.application)
    alias(libs.plugins.kotlin.android)
    alias(libs.plugins.kotlin.compose)
}

// The watch app no longer holds the JBrain domain or access key: it dictates a note
// and relays the transcript to the phone over the Wear Data Layer, and the phone (which
// already has the key from its setup code) forwards it to JBrain. So there are no
// compile-time secrets here at all — nothing to maintain, nothing to rebuild on a key
// change.
//
// `applicationId` MUST match the phone module's (com.jbrain.tracker): the Wear Data
// Layer only routes messages between apps that share an applicationId, and the same
// match is what lets this APK be embedded into and auto-delivered by the phone app.
android {
    namespace = "com.jbrain.watch"
    compileSdk = 35

    defaultConfig {
        applicationId = "com.jbrain.tracker"
        minSdk = 30
        targetSdk = 34
        versionCode = 2
        versionName = "2.0"
    }

    buildFeatures {
        compose = true
    }

    compileOptions {
        sourceCompatibility = JavaVersion.VERSION_17
        targetCompatibility = JavaVersion.VERSION_17
    }
    kotlinOptions {
        jvmTarget = "17"
    }

    // Sign with the same key as the phone app. The embedded-wear mechanism requires the
    // watch APK and its host phone APK to share a signature, so CI supplies one keystore
    // (KEYSTORE_FILE/…) for both; without one both fall back to the shared debug key.
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

    // Wear Data Layer: relay the dictated note to the phone (no direct HTTP, no key).
    implementation(libs.play.services.wearable)

    // Tile (one-tap capture from the watch face)
    implementation(libs.wear.tiles)
    implementation(libs.wear.tiles.material)
    implementation(libs.wear.protolayout)
    implementation(libs.wear.protolayout.material)
    implementation(libs.guava)
}
