# The JavaScript bridge methods are reached from WebView JavaScript.
-keepclassmembers class com.hyuntae.quantviewer.MainActivity$AndroidBridge {
    @android.webkit.JavascriptInterface <methods>;
}
