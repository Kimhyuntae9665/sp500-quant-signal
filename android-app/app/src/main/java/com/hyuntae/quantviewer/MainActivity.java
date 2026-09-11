package com.hyuntae.quantviewer;

import android.annotation.SuppressLint;
import android.app.Activity;
import android.app.AlertDialog;
import android.content.ActivityNotFoundException;
import android.content.Intent;
import android.content.SharedPreferences;
import android.graphics.Bitmap;
import android.net.Uri;
import android.net.http.SslError;
import android.os.Build;
import android.os.Bundle;
import android.view.View;
import android.view.inputmethod.EditorInfo;
import android.webkit.CookieManager;
import android.webkit.JavascriptInterface;
import android.webkit.SslErrorHandler;
import android.webkit.WebChromeClient;
import android.webkit.WebResourceError;
import android.webkit.WebResourceRequest;
import android.webkit.WebResourceResponse;
import android.webkit.WebSettings;
import android.webkit.WebView;
import android.webkit.WebViewClient;
import android.widget.Button;
import android.widget.EditText;
import android.widget.ProgressBar;
import android.widget.TextView;
import android.widget.Toast;

import java.util.Locale;

public final class MainActivity extends Activity {
    private static final String PREFERENCES_NAME = "quant_signal_viewer";
    private static final String SERVER_URL_KEY = "server_url";
    private static final String WEBVIEW_STATE_KEY = "webview_state";
    private static final String STATE_SERVER_URL_KEY = "state_server_url";

    private WebView webView;
    private ProgressBar pageProgress;
    private TextView connectionStatus;
    private View errorPanel;
    private TextView errorTitle;
    private TextView errorMessage;
    private SharedPreferences preferences;
    private AlertDialog settingsDialog;
    private String configuredUrl = "";
    private Uri serverOrigin;
    private boolean mainFrameFailed;

    @Override
    protected void onCreate(Bundle savedInstanceState) {
        super.onCreate(savedInstanceState);
        setContentView(R.layout.activity_main);

        preferences = getSharedPreferences(PREFERENCES_NAME, MODE_PRIVATE);
        configuredUrl = preferences.getString(SERVER_URL_KEY, BuildConfig.DEFAULT_SERVER_URL);
        serverOrigin = parseOrigin(configuredUrl);

        bindViews();
        configureWebView();
        bindActions();

        if (configuredUrl.isEmpty()) {
            showErrorPanel(
                    getString(R.string.connection_ready),
                    "처음 한 번만 PC 서버 주소를 저장하면 다음 실행부터 자동으로 연결됩니다."
            );
            showServerSettings(true);
            return;
        }

        boolean restored = restoreWebViewState(savedInstanceState);
        if (restored) {
            setConnectedStatus();
        } else {
            loadConfiguredServer();
        }
    }

    private void bindViews() {
        webView = findViewById(R.id.web_view);
        pageProgress = findViewById(R.id.page_progress);
        connectionStatus = findViewById(R.id.connection_status);
        errorPanel = findViewById(R.id.error_panel);
        errorTitle = findViewById(R.id.error_title);
        errorMessage = findViewById(R.id.error_message);
    }

    private void bindActions() {
        findViewById(R.id.settings_button).setOnClickListener(view -> showServerSettings(false));
        findViewById(R.id.error_settings_button).setOnClickListener(view -> showServerSettings(false));
        findViewById(R.id.retry_button).setOnClickListener(view -> loadConfiguredServer());
    }

    @SuppressLint("SetJavaScriptEnabled")
    private void configureWebView() {
        WebSettings settings = webView.getSettings();
        settings.setJavaScriptEnabled(true);
        settings.setDomStorageEnabled(true);
        settings.setCacheMode(WebSettings.LOAD_DEFAULT);
        settings.setDefaultTextEncodingName("UTF-8");
        settings.setJavaScriptCanOpenWindowsAutomatically(false);
        settings.setSupportMultipleWindows(false);
        settings.setGeolocationEnabled(false);
        settings.setAllowFileAccess(false);
        settings.setAllowContentAccess(false);
        settings.setAllowFileAccessFromFileURLs(false);
        settings.setAllowUniversalAccessFromFileURLs(false);
        settings.setMixedContentMode(WebSettings.MIXED_CONTENT_NEVER_ALLOW);
        settings.setBuiltInZoomControls(false);
        settings.setDisplayZoomControls(false);
        settings.setTextZoom(100);
        settings.setLoadsImagesAutomatically(true);
        settings.setOffscreenPreRaster(true);
        settings.setUserAgentString(settings.getUserAgentString() + " QuantSignalAndroid/" + BuildConfig.VERSION_NAME);
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
            settings.setSafeBrowsingEnabled(true);
            webView.setRendererPriorityPolicy(WebView.RENDERER_PRIORITY_BOUND, true);
        }

        WebView.setWebContentsDebuggingEnabled(BuildConfig.DEBUG);
        webView.setLayerType(View.LAYER_TYPE_HARDWARE, null);
        webView.setBackgroundColor(getColor(R.color.app_background));
        webView.setOverScrollMode(View.OVER_SCROLL_NEVER);
        webView.setScrollBarStyle(View.SCROLLBARS_INSIDE_OVERLAY);
        webView.setVerticalScrollBarEnabled(false);
        webView.setHorizontalScrollBarEnabled(false);
        webView.addJavascriptInterface(new AndroidBridge(), "AndroidQuant");

        CookieManager cookieManager = CookieManager.getInstance();
        cookieManager.setAcceptCookie(true);
        cookieManager.setAcceptThirdPartyCookies(webView, false);

        webView.setWebChromeClient(new WebChromeClient() {
            @Override
            public void onProgressChanged(WebView view, int newProgress) {
                pageProgress.setProgress(newProgress);
                pageProgress.setVisibility(newProgress >= 100 ? View.GONE : View.VISIBLE);
            }
        });

        webView.setWebViewClient(new QuantWebViewClient());
        webView.setDownloadListener((url, userAgent, contentDisposition, mimeType, contentLength) -> {
            Uri target = safeParse(url);
            if (target != null) {
                openExternal(target);
            }
        });
    }

    private void loadConfiguredServer() {
        ValidationResult validation = validateAndNormalizeUrl(configuredUrl);
        if (!validation.valid) {
            showErrorPanel(getString(R.string.connection_failed), validation.message);
            showServerSettings(configuredUrl.isEmpty());
            return;
        }

        configuredUrl = validation.url;
        serverOrigin = parseOrigin(configuredUrl);
        mainFrameFailed = false;
        errorPanel.setVisibility(View.GONE);
        pageProgress.setProgress(5);
        pageProgress.setVisibility(View.VISIBLE);
        connectionStatus.setText(R.string.connection_loading);
        webView.setVisibility(View.VISIBLE);
        webView.loadUrl(configuredUrl);
    }

    private void reloadCurrentPage() {
        Uri current = safeParse(webView.getUrl());
        if (current != null && isSameServerOrigin(current)) {
            mainFrameFailed = false;
            errorPanel.setVisibility(View.GONE);
            webView.reload();
        } else {
            loadConfiguredServer();
        }
    }

    private void showServerSettings(boolean required) {
        if (isFinishing() || (settingsDialog != null && settingsDialog.isShowing())) {
            return;
        }

        View content = getLayoutInflater().inflate(R.layout.dialog_server_settings, null, false);
        EditText input = content.findViewById(R.id.server_url_input);
        TextView validationError = content.findViewById(R.id.server_url_error);
        input.setText(configuredUrl);
        input.setSelection(input.length());

        AlertDialog.Builder builder = new AlertDialog.Builder(this)
                .setTitle(R.string.server_settings)
                .setView(content)
                .setPositiveButton(R.string.save_and_connect, null)
                .setNegativeButton(required ? R.string.exit : R.string.cancel, (dialog, which) -> {
                    if (required) {
                        finish();
                    }
                });

        settingsDialog = builder.create();
        settingsDialog.setCancelable(!required);
        settingsDialog.setCanceledOnTouchOutside(!required);
        settingsDialog.setOnDismissListener(dialog -> settingsDialog = null);
        settingsDialog.setOnShowListener(dialog -> {
            Button positive = settingsDialog.getButton(AlertDialog.BUTTON_POSITIVE);
            View.OnClickListener saveAction = view -> {
                ValidationResult validation = validateAndNormalizeUrl(input.getText().toString());
                if (!validation.valid) {
                    validationError.setText(validation.message);
                    validationError.setVisibility(View.VISIBLE);
                    input.requestFocus();
                    return;
                }

                validationError.setVisibility(View.GONE);
                configuredUrl = validation.url;
                serverOrigin = parseOrigin(configuredUrl);
                preferences.edit().putString(SERVER_URL_KEY, configuredUrl).apply();
                settingsDialog.dismiss();
                loadConfiguredServer();
            };
            positive.setOnClickListener(saveAction);
            input.setOnEditorActionListener((view, actionId, event) -> {
                if (actionId == EditorInfo.IME_ACTION_DONE) {
                    saveAction.onClick(view);
                    return true;
                }
                return false;
            });
        });
        settingsDialog.show();
    }

    private ValidationResult validateAndNormalizeUrl(String rawValue) {
        String candidate = rawValue == null ? "" : rawValue.trim();
        if (candidate.isEmpty()) {
            return ValidationResult.error("서버 주소를 입력해 주세요.");
        }
        if (!candidate.contains("://")) {
            candidate = "http://" + candidate;
        }
        for (int index = 0; index < candidate.length(); index++) {
            if (Character.isWhitespace(candidate.charAt(index))) {
                return ValidationResult.error("주소에는 공백을 넣을 수 없습니다.");
            }
        }

        Uri uri = safeParse(candidate);
        if (uri == null || uri.getScheme() == null || uri.getHost() == null) {
            return ValidationResult.error(getString(R.string.invalid_url));
        }

        String scheme = uri.getScheme().toLowerCase(Locale.US);
        if (!"http".equals(scheme) && !"https".equals(scheme)) {
            return ValidationResult.error("http:// 또는 https:// 주소만 사용할 수 있습니다.");
        }
        if (uri.getUserInfo() != null) {
            return ValidationResult.error("아이디·비밀번호는 주소에 넣지 말고 발급된 token 쿼리를 사용해 주세요.");
        }
        if (uri.getPort() < -1 || uri.getPort() > 65535) {
            return ValidationResult.error("포트 번호는 1~65535 범위여야 합니다.");
        }
        if ("http".equals(scheme) && !isAllowedCleartextHost(uri.getHost())) {
            return ValidationResult.error(
                    "HTTP는 사설 LAN IP, Tailscale IP·호스트명에서만 허용합니다. 외부 주소는 HTTPS를 사용해 주세요."
            );
        }

        return ValidationResult.success(candidate);
    }

    private boolean isAllowedCleartextHost(String hostValue) {
        if (hostValue == null) {
            return false;
        }
        String host = hostValue.toLowerCase(Locale.US);
        if ("localhost".equals(host) || "127.0.0.1".equals(host) || "::1".equals(host)) {
            return false;
        }
        if (host.endsWith(".ts.net") || host.endsWith(".local") || host.endsWith(".lan")
                || host.endsWith(".home.arpa")) {
            return true;
        }
        if (!host.contains(".") && !host.contains(":")) {
            return true;
        }
        if (host.contains(":")) {
            return host.startsWith("fc") || host.startsWith("fd") || host.startsWith("fe8")
                    || host.startsWith("fe9") || host.startsWith("fea") || host.startsWith("feb");
        }

        String[] octets = host.split("\\.");
        if (octets.length != 4) {
            return false;
        }
        int[] values = new int[4];
        try {
            for (int index = 0; index < 4; index++) {
                values[index] = Integer.parseInt(octets[index]);
                if (values[index] < 0 || values[index] > 255) {
                    return false;
                }
            }
        } catch (NumberFormatException ignored) {
            return false;
        }

        if (values[0] == 10 || (values[0] == 172 && values[1] >= 16 && values[1] <= 31)
                || (values[0] == 192 && values[1] == 168) || (values[0] == 169 && values[1] == 254)) {
            return true;
        }
        return values[0] == 100 && values[1] >= 64 && values[1] <= 127;
    }

    private Uri parseOrigin(String value) {
        Uri uri = safeParse(value);
        if (uri == null || uri.getScheme() == null || uri.getHost() == null) {
            return null;
        }
        return uri;
    }

    private Uri safeParse(String value) {
        if (value == null || value.isEmpty()) {
            return null;
        }
        try {
            return Uri.parse(value);
        } catch (RuntimeException ignored) {
            return null;
        }
    }

    private boolean isSameServerOrigin(Uri target) {
        if (serverOrigin == null || target == null || target.getScheme() == null || target.getHost() == null) {
            return false;
        }
        return serverOrigin.getScheme().equalsIgnoreCase(target.getScheme())
                && serverOrigin.getHost().equalsIgnoreCase(target.getHost())
                && effectivePort(serverOrigin) == effectivePort(target);
    }

    private int effectivePort(Uri uri) {
        if (uri.getPort() >= 0) {
            return uri.getPort();
        }
        return "https".equalsIgnoreCase(uri.getScheme()) ? 443 : 80;
    }

    private boolean handleNavigation(Uri target, boolean mainFrame) {
        if (target == null) {
            return true;
        }
        if ("about".equalsIgnoreCase(target.getScheme())) {
            return false;
        }
        if (isSameServerOrigin(target)) {
            return false;
        }
        if (mainFrame) {
            openExternal(target);
        }
        return true;
    }

    private void openExternal(Uri target) {
        String scheme = target.getScheme();
        if (scheme == null || "javascript".equalsIgnoreCase(scheme) || "file".equalsIgnoreCase(scheme)
                || "data".equalsIgnoreCase(scheme)) {
            Toast.makeText(this, "안전하지 않은 외부 링크를 차단했습니다.", Toast.LENGTH_SHORT).show();
            return;
        }
        try {
            startActivity(new Intent(Intent.ACTION_VIEW, target));
        } catch (ActivityNotFoundException error) {
            Toast.makeText(this, "이 링크를 열 수 있는 앱이 없습니다.", Toast.LENGTH_SHORT).show();
        }
    }

    private void showErrorPanel(String title, String message) {
        mainFrameFailed = true;
        pageProgress.setVisibility(View.GONE);
        connectionStatus.setText(R.string.connection_failed);
        errorTitle.setText(title);
        errorMessage.setText(message);
        errorPanel.setVisibility(View.VISIBLE);
    }

    private void setConnectedStatus() {
        Uri origin = serverOrigin;
        if (origin == null) {
            connectionStatus.setText(R.string.connection_ready);
            return;
        }
        String host = origin.getHost();
        String port = origin.getPort() >= 0 ? ":" + origin.getPort() : "";
        connectionStatus.setText(getString(R.string.connection_connected, host, port));
    }

    private boolean restoreWebViewState(Bundle savedInstanceState) {
        if (savedInstanceState == null || !configuredUrl.equals(savedInstanceState.getString(STATE_SERVER_URL_KEY, ""))) {
            return false;
        }
        Bundle webViewState = savedInstanceState.getBundle(WEBVIEW_STATE_KEY);
        return webViewState != null && webView.restoreState(webViewState) != null;
    }

    @Override
    protected void onSaveInstanceState(Bundle outState) {
        Bundle webViewState = new Bundle();
        webView.saveState(webViewState);
        outState.putBundle(WEBVIEW_STATE_KEY, webViewState);
        outState.putString(STATE_SERVER_URL_KEY, configuredUrl);
        super.onSaveInstanceState(outState);
    }

    @Override
    public void onBackPressed() {
        if (settingsDialog != null && settingsDialog.isShowing()) {
            if (configuredUrl.isEmpty()) {
                finish();
            } else {
                settingsDialog.dismiss();
            }
            return;
        }
        if (webView.canGoBack()) {
            webView.goBack();
        } else {
            super.onBackPressed();
        }
    }

    @Override
    protected void onResume() {
        super.onResume();
        if (webView != null) {
            webView.onResume();
        }
    }

    @Override
    protected void onPause() {
        if (webView != null) {
            webView.onPause();
        }
        CookieManager.getInstance().flush();
        super.onPause();
    }

    @Override
    protected void onDestroy() {
        if (settingsDialog != null) {
            settingsDialog.dismiss();
        }
        if (webView != null) {
            webView.removeJavascriptInterface("AndroidQuant");
            webView.stopLoading();
            webView.setWebChromeClient(null);
            webView.setWebViewClient(null);
            webView.destroy();
        }
        super.onDestroy();
    }

    public final class AndroidBridge {
        @JavascriptInterface
        public void openSettings() {
            runOnUiThread(() -> showServerSettings(false));
        }

        @JavascriptInterface
        public void reload() {
            runOnUiThread(MainActivity.this::reloadCurrentPage);
        }
    }

    private final class QuantWebViewClient extends WebViewClient {
        @Override
        public boolean shouldOverrideUrlLoading(WebView view, WebResourceRequest request) {
            return handleNavigation(request.getUrl(), request.isForMainFrame());
        }

        @Override
        @SuppressWarnings("deprecation")
        public boolean shouldOverrideUrlLoading(WebView view, String url) {
            return handleNavigation(safeParse(url), true);
        }

        @Override
        public void onPageStarted(WebView view, String url, Bitmap favicon) {
            mainFrameFailed = false;
            errorPanel.setVisibility(View.GONE);
            connectionStatus.setText(R.string.connection_loading);
            pageProgress.setVisibility(View.VISIBLE);
        }

        @Override
        public void onPageFinished(WebView view, String url) {
            if (!mainFrameFailed) {
                errorPanel.setVisibility(View.GONE);
                setConnectedStatus();
            }
        }

        @Override
        public void onReceivedError(WebView view, WebResourceRequest request, WebResourceError error) {
            if (request.isForMainFrame()) {
                String detail = error.getDescription() == null ? "" : "\n\n" + error.getDescription();
                showErrorPanel(
                        getString(R.string.connection_failed),
                        getString(R.string.error_help) + detail
                );
            }
        }

        @Override
        public void onReceivedHttpError(
                WebView view,
                WebResourceRequest request,
                WebResourceResponse errorResponse
        ) {
            if (!request.isForMainFrame()) {
                return;
            }
            int status = errorResponse.getStatusCode();
            if (status == 401 || status == 403) {
                showErrorPanel(
                        "인증에 실패했습니다",
                        "서버 주소의 token 값이 맞는지 확인한 뒤 다시 연결하세요. (HTTP " + status + ")"
                );
            } else if (status >= 400) {
                showErrorPanel(
                        "서버 오류가 발생했습니다",
                        "서버가 HTTP " + status + " 응답을 보냈습니다. 잠시 후 다시 시도해 주세요."
                );
            }
        }

        @Override
        public void onReceivedSslError(WebView view, SslErrorHandler handler, SslError error) {
            handler.cancel();
            Uri failedTarget = safeParse(error.getUrl());
            if (failedTarget != null && isSameServerOrigin(failedTarget)) {
                showErrorPanel(
                        "보안 인증서를 확인할 수 없습니다",
                        "HTTPS 인증서 검증에 실패해 연결을 차단했습니다. 인증서를 갱신하거나 LAN/Tailscale 서버 주소를 확인하세요."
                );
            }
        }
    }

    private static final class ValidationResult {
        final boolean valid;
        final String url;
        final String message;

        private ValidationResult(boolean valid, String url, String message) {
            this.valid = valid;
            this.url = url;
            this.message = message;
        }

        static ValidationResult success(String url) {
            return new ValidationResult(true, url, "");
        }

        static ValidationResult error(String message) {
            return new ValidationResult(false, "", message);
        }
    }
}
