plugins {
    id("com.android.application")
    id("org.jetbrains.kotlin.android")
}

// Fælles versionsnummer for hele projektet (Android, Windows-klient og hub) – se VERSION i roden.
val appVersion = rootProject.file("VERSION").readText().trim()
val appVersionCode = appVersion.split(".").map { it.toIntOrNull() ?: 0 }
    .let { (it.getOrElse(0) { 0 } * 10000) + (it.getOrElse(1) { 0 } * 100) + it.getOrElse(2) { 0 } }

android {
    namespace = "dk.pttradio"
    compileSdk = 34

    defaultConfig {
        applicationId = "dk.pttradio"
        minSdk = 26
        targetSdk = 34
        versionCode = appVersionCode
        versionName = appVersion
    }

    // Fast nøgle, så nye versioner kan installeres oven på de gamle uden at afinstallere først.
    signingConfigs {
        create("fast") {
            storeFile = file("pttradio-release.keystore")
            storePassword = "pttradio-signing"
            keyAlias = "pttradio"
            keyPassword = "pttradio-signing"
            storeType = "PKCS12"
        }
    }
    buildTypes {
        getByName("debug") { signingConfig = signingConfigs.getByName("fast") }
        getByName("release") {
            isMinifyEnabled = false
            signingConfig = signingConfigs.getByName("fast")
        }
    }
    compileOptions {
        sourceCompatibility = JavaVersion.VERSION_17
        targetCompatibility = JavaVersion.VERSION_17
    }
    kotlinOptions { jvmTarget = "17" }
    lint { abortOnError = false }
}
