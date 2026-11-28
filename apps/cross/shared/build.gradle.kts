plugins {
    alias(libs.plugins.kotlin.multiplatform)
    alias(libs.plugins.kotlin.serialization)
    alias(libs.plugins.kotlinter)
}

kotlin {
    jvmToolchain(25)
    jvm {
        // Tests against a real dock server; run by `liveTest` only, never by check or allTests.
        val main = this.compilations.getByName("main")
        val live =
            this.compilations.create("liveTest") {
                associateWith(main)
                defaultSourceSet.dependencies {
                    implementation(kotlin("test-junit"))
                    implementation(libs.kotlinx.coroutines.test)
                }
            }
        tasks.register<Test>("liveTest") {
            description = "Runs the client against a real dock-server (needs uv and Docker or podman)."
            group = "verification"
            classpath = live.runtimeDependencyFiles + live.output.allOutputs
            testClassesDirs = live.output.classesDirs
            useJUnit()
            systemProperty("dock.repo", rootDir.parentFile.parentFile.absolutePath)
            outputs.upToDateWhen { false }
            testLogging { showStandardStreams = true }
        }
    }

    sourceSets {
        commonMain.dependencies {
            api(libs.kotlinx.serialization.json)
            api(libs.kotlinx.coroutines.core)
        }
        commonTest.dependencies {
            implementation(kotlin("test"))
            implementation(libs.kotlinx.coroutines.test)
        }
    }
}
