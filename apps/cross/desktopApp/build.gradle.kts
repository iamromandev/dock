plugins {
    alias(libs.plugins.kotlin.jvm)
    alias(libs.plugins.kotlin.compose)
    alias(libs.plugins.compose)
    alias(libs.plugins.kotlinter)
}

kotlin {
    jvmToolchain(25)
}

dependencies {
    implementation(project(":shared"))
    implementation(compose.desktop.currentOs)
    implementation(libs.compose.material3)
    implementation(libs.java.keyring)
    // Not dependencies of ours: these pin what java-keyring brings, to the versions in libs.versions.toml.
    constraints {
        for (pinned in listOf(
            libs.jna,
            libs.jna.platform,
            libs.jkeychain,
            libs.secret.service,
            libs.dbus.java.core,
            libs.dbus.java.transport,
            libs.slf4j.api,
        )) {
            implementation(pinned) { because("java-keyring's transitive libraries are pinned, see libs.versions.toml") }
        }
    }
    testImplementation(kotlin("test"))
}

compose.desktop {
    application {
        mainClass = "dev.dock.cross.desktop.MainKt"
        jvmArgs("--enable-native-access=ALL-UNNAMED")
    }
}

tasks.test {
    jvmArgs("--enable-native-access=ALL-UNNAMED")
}
