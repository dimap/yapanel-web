import java.util.Properties

plugins {
    id("com.android.application")
    id("com.chaquo.python")
}

// Панель лежит уровнем выше: её файлы кладём в assets/panel, при первом запуске приложение распаковывает их
val panelRoot = rootDir.parentFile
val panelVersion = panelRoot.resolve("VERSION").readText().trim()
val panelFiles = listOf("server.py", "access.py", "agent.py", "glagol.py", "profiles.py", "midi.py", "managed_yam.py", "model_colors.py", "multi.py", "pyadb.py",
    "VERSION", "LICENSE", "README.md")

// 0.1.9 → 109, 1.2.3 → 10203; «-rc1» и подобное не учитываем
fun versionCodeOf(v: String): Int {
    val p = v.substringBefore("-").split(".").map { it.toIntOrNull() ?: 0 } + listOf(0, 0, 0)
    return p[0] * 10000 + p[1] * 100 + p[2]
}

val signing = Properties().apply {
    val f = rootDir.resolve("keystore.properties")
    if (f.exists()) f.inputStream().use { load(it) }
}

android {
    namespace = "io.github.dimap.yandexstation"
    compileSdk = 34

    defaultConfig {
        applicationId = "io.github.dimap.yandexstation"
        minSdk = 24
        targetSdk = 34
        versionCode = versionCodeOf(panelVersion)
        versionName = panelVersion
        ndk { abiFilters += listOf("arm64-v8a", "x86_64") }   // Python 3.14 (в нём zstd) — только 64 бита
    }

    signingConfigs {
        if (signing.containsKey("storeFile")) create("release") {
            storeFile = rootDir.resolve(signing.getProperty("storeFile"))
            storePassword = signing.getProperty("storePassword")
            keyAlias = signing.getProperty("keyAlias")
            keyPassword = signing.getProperty("keyPassword")
        }
    }
    buildTypes {
        release {
            isMinifyEnabled = false
            signingConfig = signingConfigs.findByName("release") ?: signingConfigs.getByName("debug")
        }
    }
    compileOptions {
        sourceCompatibility = JavaVersion.VERSION_17
        targetCompatibility = JavaVersion.VERSION_17
    }
    sourceSets["main"].assets.srcDir(layout.buildDirectory.dir("panel-assets"))
}

chaquopy {
    defaultConfig {
        version = "3.14"
        pyc { src = false }   // байткод соберётся на телефоне: не нужен Python той же версии на машине сборки
    }
}

val copyPanel by tasks.registering(Sync::class) {
    from(panelRoot) {
        include(panelFiles)
        include("static/**", "device/**")
        exclude("**/__pycache__/**")
    }
    into(layout.buildDirectory.dir("panel-assets/panel"))
}
tasks.named("preBuild") { dependsOn(copyPanel) }
