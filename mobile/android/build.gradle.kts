buildscript {
    repositories {
        google()
        mavenCentral()
        maven { url = uri("https://developer.huawei.com/repo/") }
    }
    dependencies {
        // Optional vendor build plugins. Their official config files are
        // private deployment inputs; a normal developer build needs neither.
        if (file("app/agconnect-services.json").exists()) {
            // AGConnect inspects the legacy buildscript dependency list even
            // when AGP has already been loaded by settings' plugins block.
            classpath("com.android.tools.build:gradle:9.0.1")
        }
        if (file("app/agconnect-services.json").exists()) {
            classpath("com.huawei.agconnect:agcp:1.9.1.301") {
                // AGConnect's monitoring plugin brings an obsolete AGP into
                // the parent classloader. Use this project's AGP instead.
                exclude(group = "com.android.tools.build")
            }
        }
        if (file("app/google-services.json").exists()) {
            classpath("com.google.gms:google-services:4.4.4") {
                exclude(group = "com.android.tools.build")
            }
        }
    }
}

allprojects {
    repositories {
        google()
        mavenCentral()
        maven {
            url = uri("https://developer.huawei.com/repo/")
            content { includeGroupByRegex("com\\.huawei\\..*") }
        }
        maven {
            url = uri("https://developer.hihonor.com/repo/")
            content { includeGroupByRegex("com\\.hihonor\\..*") }
        }
    }
}

val newBuildDir: Directory =
    rootProject.layout.buildDirectory
        .dir("../../build")
        .get()
rootProject.layout.buildDirectory.value(newBuildDir)

subprojects {
    val newSubprojectBuildDir: Directory = newBuildDir.dir(project.name)
    project.layout.buildDirectory.value(newSubprojectBuildDir)
}
subprojects {
    project.evaluationDependsOn(":app")
}

tasks.register<Delete>("clean") {
    delete(rootProject.layout.buildDirectory)
}
