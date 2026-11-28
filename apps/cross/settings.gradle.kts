rootProject.name = "dock-cross"

pluginManagement {
    repositories {
        gradlePluginPortal()
        mavenCentral()
    }
}

dependencyResolutionManagement {
    repositories {
        mavenCentral()
    }
}

// :desktopApp joins in a later phase.
include(":shared")
