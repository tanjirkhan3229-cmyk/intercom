// Root build file. Plugin versions are declared here and applied per-module.
plugins {
    id("com.android.library") version "8.5.2" apply false
    id("com.android.application") version "8.5.2" apply false
    // Kotlin 1.9.24 pairs with Compose compiler 1.5.14 via composeOptions (no separate
    // compose plugin needed — that's the Kotlin 2.0 path).
    kotlin("android") version "1.9.24" apply false
    kotlin("plugin.serialization") version "1.9.24" apply false
    id("com.google.gms.google-services") version "4.4.2" apply false
}
