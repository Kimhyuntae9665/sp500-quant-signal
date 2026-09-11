from __future__ import annotations

from pathlib import Path
import unittest


PROJECT_ROOT = Path(__file__).resolve().parents[1]
ANDROID_ROOT = PROJECT_ROOT / "android-app"


class AndroidAppContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.gradle = (ANDROID_ROOT / "app" / "build.gradle").read_text(encoding="utf-8")
        cls.manifest = (
            ANDROID_ROOT / "app" / "src" / "main" / "AndroidManifest.xml"
        ).read_text(encoding="utf-8")
        cls.activity = (
            ANDROID_ROOT
            / "app"
            / "src"
            / "main"
            / "java"
            / "com"
            / "hyuntae"
            / "quantviewer"
            / "MainActivity.java"
        ).read_text(encoding="utf-8")
        cls.build_script = (ANDROID_ROOT / "build-apk.ps1").read_text(encoding="utf-8")
        cls.splash = (
            ANDROID_ROOT / "app" / "src" / "main" / "res" / "values-v31" / "styles.xml"
        ).read_text(encoding="utf-8")

    def test_galaxy_compatible_sdk_and_version_contract(self) -> None:
        self.assertIn('compileSdk 34', self.gradle)
        self.assertIn('minSdk 24', self.gradle)
        self.assertIn('targetSdk 34', self.gradle)
        self.assertIn('versionCode 2', self.gradle)
        self.assertIn('versionName "1.1.0"', self.gradle)

    def test_personal_release_is_optimized_signed_and_not_debuggable(self) -> None:
        self.assertIn('minifyEnabled true', self.gradle)
        self.assertIn('shrinkResources true', self.gradle)
        self.assertIn('signingConfig signingConfigs.debug', self.gradle)
        self.assertIn('WebView.setWebContentsDebuggingEnabled(BuildConfig.DEBUG)', self.activity)

    def test_private_webview_security_and_persistence_contract(self) -> None:
        self.assertIn('android:allowBackup="false"', self.manifest)
        self.assertIn('android:hardwareAccelerated="true"', self.manifest)
        self.assertIn('android:windowSoftInputMode="adjustResize"', self.manifest)
        self.assertIn('settings.setAllowFileAccess(false)', self.activity)
        self.assertIn('settings.setAllowContentAccess(false)', self.activity)
        self.assertIn('settings.setMixedContentMode(WebSettings.MIXED_CONTENT_NEVER_ALLOW)', self.activity)
        self.assertIn('setAcceptThirdPartyCookies(webView, false)', self.activity)
        self.assertIn('preferences.edit().putString(SERVER_URL_KEY, configuredUrl).apply()', self.activity)

    def test_s23_smooth_rendering_and_splash_contract(self) -> None:
        self.assertIn('settings.setOffscreenPreRaster(true)', self.activity)
        self.assertIn('WebView.RENDERER_PRIORITY_BOUND', self.activity)
        self.assertIn('View.LAYER_TYPE_HARDWARE', self.activity)
        self.assertIn('android:windowSplashScreenAnimatedIcon', self.splash)

    def test_personal_apk_artifact_is_hashed_and_signature_checked(self) -> None:
        self.assertIn('SP500-Quant-Signal-Galaxy-S23.apk', self.build_script)
        self.assertIn('Get-FileHash -Algorithm SHA256', self.build_script)
        self.assertIn('apksigner.bat', self.build_script)
        self.assertIn('verify --verbose --print-certs', self.build_script)


if __name__ == "__main__":
    unittest.main()
