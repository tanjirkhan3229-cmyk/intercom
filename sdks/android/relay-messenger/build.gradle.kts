plugins {
    id("com.android.library")
    kotlin("android")
    kotlin("plugin.serialization")
    id("maven-publish")
}

android {
    namespace = "dev.relay.messenger"
    compileSdk = 34

    defaultConfig {
        minSdk = 24
        consumerProguardFiles("consumer-rules.pro")
    }

    buildFeatures {
        compose = true
    }
    composeOptions {
        // Compose compiler is provided by the Kotlin 2.0 compose plugin path; pin for AGP.
        kotlinCompilerExtensionVersion = "1.5.14"
    }

    compileOptions {
        sourceCompatibility = JavaVersion.VERSION_1_8
        targetCompatibility = JavaVersion.VERSION_1_8
    }
    kotlinOptions {
        jvmTarget = "1.8"
    }

    publishing {
        singleVariant("release") {
            withSourcesJar()
        }
    }
}

dependencies {
    val composeBom = platform("androidx.compose:compose-bom:2024.09.00")
    implementation(composeBom)

    implementation("org.jetbrains.kotlinx:kotlinx-coroutines-android:1.8.1")
    implementation("com.squareup.okhttp3:okhttp:4.12.0")
    implementation("org.jetbrains.kotlinx:kotlinx-serialization-json:1.7.1")

    implementation("androidx.core:core-ktx:1.13.1")
    implementation("androidx.activity:activity-compose:1.9.2")
    implementation("androidx.compose.ui:ui")
    implementation("androidx.compose.foundation:foundation")
    implementation("androidx.compose.material3:material3")

    // Push. firebase-messaging pulls a small footprint; see README size budget.
    implementation("com.google.firebase:firebase-messaging:24.0.1")
}

publishing {
    publications {
        register<MavenPublication>("release") {
            groupId = project.findProperty("RELAY_GROUP") as String? ?: "dev.relay"
            artifactId = "relay-messenger"
            version = project.findProperty("RELAY_VERSION") as String? ?: "0.1.0-beta01"
            afterEvaluate { from(components["release"]) }
        }
    }
}
