package io.github.xiaofan6ya.workbuddy2api;

import android.Manifest;
import android.app.Activity;
import android.content.*;
import android.content.pm.PackageManager;
import android.graphics.Color;
import android.graphics.Typeface;
import android.graphics.drawable.GradientDrawable;
import android.net.Uri;
import android.os.*;
import android.view.*;
import android.webkit.*;
import android.widget.*;
import org.json.JSONObject;
import com.chaquo.python.Python;

public class MainActivity extends Activity {
    private final Handler handler = new Handler(Looper.getMainLooper());
    private WebView web;
    private TextView status;
    private FrameLayout boot;
    private TextView bootTitle;
    private ProgressBar progress;
    private Button details;
    private ValueCallback<Uri[]> upload;
    private String loadedOrigin;
    private boolean destroyed;
    private Button restart;
    private final Runnable waitForServer = new Runnable() {
        @Override public void run() {
            if (destroyed) return;
            String origin = LocalApiService.origin;
            if (origin != null && !origin.equals(loadedOrigin)) {
                loadedOrigin = origin;
                web.loadUrl(origin + "/admin?desktop=1&android=1");
            } else if (origin == null) {
                boot.setVisibility(View.VISIBLE);
                boolean failed = LocalApiService.error != null;
                bootTitle.setText(failed ? "服务启动失败" : LocalApiService.starting ? "正在打开工作台" : "本地服务已停止");
                status.setText(failed ? "请重试启动。若仍无法打开，可查看错误详情。" : LocalApiService.starting ? "Python 和项目依赖已内置\n正在启动手机本地 API 服务…" : "点击下方按钮，继续使用手机本地 API。");
                progress.setVisibility(LocalApiService.starting ? View.VISIBLE : View.GONE);
                restart.setVisibility(LocalApiService.starting ? View.GONE : View.VISIBLE);
                details.setVisibility(failed ? View.VISIBLE : View.GONE);
                if (loadedOrigin != null) { loadedOrigin = null; web.loadUrl("about:blank"); }
            }
            handler.postDelayed(this, 500);
        }
    };

    @Override public void onCreate(Bundle savedInstanceState) {
        super.onCreate(savedInstanceState);
        getWindow().setStatusBarColor(Color.rgb(245, 246, 248));
        getWindow().getDecorView().setSystemUiVisibility(View.SYSTEM_UI_FLAG_LIGHT_STATUS_BAR);
        FrameLayout layout = new FrameLayout(this);
        layout.setFitsSystemWindows(true);
        web = new WebView(this);
        WebView.setWebContentsDebuggingEnabled(BuildConfig.DEBUG);
        layout.addView(web, new FrameLayout.LayoutParams(-1, -1));
        boot = new FrameLayout(this);
        boot.setBackgroundColor(Color.rgb(245, 246, 248));
        LinearLayout card = new LinearLayout(this);
        card.setOrientation(LinearLayout.VERTICAL);
        card.setGravity(Gravity.CENTER_HORIZONTAL);
        card.setPadding(dp(28), dp(32), dp(28), dp(28));
        GradientDrawable surface = new GradientDrawable();
        surface.setColor(Color.WHITE); surface.setCornerRadius(dp(24));
        card.setBackground(surface); card.setElevation(dp(4));
        int width = Math.min(dp(340), getResources().getDisplayMetrics().widthPixels - dp(40));
        FrameLayout.LayoutParams cardParams = new FrameLayout.LayoutParams(width, -2, Gravity.CENTER);
        boot.addView(card, cardParams);
        ImageView logo = new ImageView(this);
        logo.setImageResource(R.drawable.app_icon);
        card.addView(logo, new LinearLayout.LayoutParams(dp(68), dp(68)));
        TextView brand = new TextView(this);
        brand.setText("WorkBuddy2API"); brand.setTextSize(24); brand.setTypeface(null, Typeface.BOLD);
        brand.setTextColor(Color.rgb(22, 24, 29)); brand.setGravity(Gravity.CENTER);
        brand.setPadding(0, dp(18), 0, dp(20)); card.addView(brand);
        bootTitle = new TextView(this);
        bootTitle.setText("正在打开工作台"); bootTitle.setTextSize(16);
        bootTitle.setTextColor(Color.rgb(22, 24, 29)); card.addView(bootTitle);
        status = new TextView(this);
        status.setText("Python 和项目依赖已内置\n正在启动手机本地 API 服务…");
        status.setTextSize(13); status.setGravity(Gravity.CENTER);
        status.setTextColor(Color.rgb(101, 105, 112)); status.setLineSpacing(dp(5), 1);
        status.setPadding(0, dp(12), 0, dp(20)); card.addView(status);
        progress = new ProgressBar(this);
        progress.setIndeterminateTintList(android.content.res.ColorStateList.valueOf(Color.rgb(20, 134, 101)));
        card.addView(progress, new LinearLayout.LayoutParams(dp(28), dp(28)));
        restart = new Button(this);
        restart.setText("启动服务");
        restart.setTextColor(Color.WHITE);
        GradientDrawable action = new GradientDrawable();
        action.setColor(Color.rgb(20, 134, 101)); action.setCornerRadius(dp(12));
        restart.setBackground(action); restart.setVisibility(View.GONE);
        restart.setOnClickListener(view -> startLocalService());
        card.addView(restart, new LinearLayout.LayoutParams(-1, dp(48)));
        details = new Button(this);
        details.setText("查看错误详情"); details.setTextSize(12); details.setTextColor(Color.rgb(101, 105, 112));
        details.setBackgroundColor(Color.TRANSPARENT); details.setVisibility(View.GONE);
        details.setOnClickListener(view -> new android.app.AlertDialog.Builder(this).setTitle("启动错误")
            .setMessage(LocalApiService.error).setPositiveButton("关闭", null).show());
        card.addView(details);
        layout.addView(boot, new FrameLayout.LayoutParams(-1, -1));
        setContentView(layout);
        web.getSettings().setJavaScriptEnabled(true);
        web.getSettings().setDomStorageEnabled(true);
        // Local assets change with APK upgrades, even while the origin stays fixed.
        web.getSettings().setCacheMode(WebSettings.LOAD_NO_CACHE);
        web.getSettings().setAllowFileAccess(false);
        web.getSettings().setAllowContentAccess(true);
        web.getSettings().setSupportMultipleWindows(true);
        web.getSettings().setJavaScriptCanOpenWindowsAutomatically(false);
        web.getSettings().setMixedContentMode(WebSettings.MIXED_CONTENT_NEVER_ALLOW);
        web.setWebViewClient(new WebViewClient() {
            @Override public boolean shouldOverrideUrlLoading(WebView view, WebResourceRequest request) {
                return route(request.getUrl());
            }
            @Override public void onPageFinished(WebView view, String url) {
                if (isAdmin(url) && LocalApiService.token != null) {
                    web.evaluateJavascript("window.WBMobile && WBMobile.initialize(" + JSONObject.quote(LocalApiService.token) + ")", null);
                    boot.setVisibility(View.GONE);
                }
            }
            @Override public void onReceivedError(WebView view, WebResourceRequest request, WebResourceError error) {
                if (request.isForMainFrame()) {
                    bootTitle.setText("工作台连接失败"); status.setText("请点击重试重新连接本地服务。");
                    progress.setVisibility(View.GONE); restart.setText("重新连接"); restart.setVisibility(View.VISIBLE);
                    boot.setVisibility(View.VISIBLE);
                }
            }
        });
        web.setWebChromeClient(new WebChromeClient() {
            @Override public boolean onCreateWindow(WebView view, boolean dialog, boolean userGesture, Message message) {
                if (!userGesture) return false;
                WebView popup = new WebView(MainActivity.this);
                popup.setWebViewClient(new WebViewClient() {
                    private boolean opened;
                    private void open(String url) { if (!opened) { opened = true; external(Uri.parse(url)); popup.destroy(); } }
                    @Override public boolean shouldOverrideUrlLoading(WebView v, WebResourceRequest r) { open(r.getUrl().toString()); return true; }
                    @Override public void onPageStarted(WebView v, String url, android.graphics.Bitmap icon) { if (!"about:blank".equals(url)) open(url); }
                });
                ((WebView.WebViewTransport) message.obj).setWebView(popup); message.sendToTarget(); return true;
            }
            @Override public boolean onShowFileChooser(WebView view, ValueCallback<Uri[]> callback, FileChooserParams params) {
                if (upload != null) upload.onReceiveValue(null);
                upload = callback;
                Intent intent = new Intent(Intent.ACTION_GET_CONTENT).setType("*/*").addCategory(Intent.CATEGORY_OPENABLE);
                intent.putExtra(Intent.EXTRA_ALLOW_MULTIPLE, params.getMode() == FileChooserParams.MODE_OPEN_MULTIPLE);
                try { startActivityForResult(Intent.createChooser(intent, "选择账号备份"), 10); return true; }
                catch (ActivityNotFoundException e) { upload.onReceiveValue(null); upload = null; return false; }
            }
        });
        if (Build.VERSION.SDK_INT >= 33 && checkSelfPermission(Manifest.permission.POST_NOTIFICATIONS) != PackageManager.PERMISSION_GRANTED)
            requestPermissions(new String[]{Manifest.permission.POST_NOTIFICATIONS}, 11);
        startLocalService();
        handler.post(waitForServer);
    }

    private void startLocalService() {
        if (LocalApiService.origin != null && loadedOrigin != null) {
            web.loadUrl(loadedOrigin + "/admin?desktop=1&android=1"); return;
        }
        Intent service = new Intent(this, LocalApiService.class);
        if (Build.VERSION.SDK_INT >= 26) startForegroundService(service); else startService(service);
    }

    private int dp(int value) { return Math.round(value * getResources().getDisplayMetrics().density); }

    private boolean isAdmin(String url) {
        if (loadedOrigin == null || url == null) return false;
        Uri uri = Uri.parse(url);
        return "http".equals(uri.getScheme()) && "127.0.0.1".equals(uri.getHost())
            && uri.getPort() == Uri.parse(loadedOrigin).getPort() && "/admin".equals(uri.getPath());
    }
    private boolean route(Uri uri) {
        if (isAdmin(uri.toString())) return false;
        external(uri); return true;
    }
    private void external(Uri uri) {
        if (!"https".equals(uri.getScheme()) && !"http".equals(uri.getScheme())) return;
        try { startActivity(new Intent(Intent.ACTION_VIEW, uri).addCategory(Intent.CATEGORY_BROWSABLE)); }
        catch (ActivityNotFoundException e) { Toast.makeText(this, "请先安装浏览器", Toast.LENGTH_SHORT).show(); }
    }
    @Override protected void onResume() {
        super.onResume();
        if (web != null) web.evaluateJavascript("window.dispatchEvent(new Event('wb-shell-resume'))", null);
        if (LocalApiService.origin != null && Python.isStarted()) new Thread(() -> {
            try {
                String refreshed = Python.getInstance().getModule("android_runtime").callAttr("session_token").toString();
                if (!refreshed.isEmpty()) {
                    LocalApiService.token = refreshed;
                    handler.post(() -> {
                        if (!destroyed && isAdmin(web.getUrl())) web.evaluateJavascript("window.WBMobile && WBMobile.initialize(" + JSONObject.quote(refreshed) + ")", null);
                    });
                }
            } catch (Exception ignored) { }
        }, "workbuddy-session-refresh").start();
    }
    @Override protected void onActivityResult(int request, int result, Intent data) {
        super.onActivityResult(request, result, data);
        if (request == 10 && upload != null) {
            upload.onReceiveValue(WebChromeClient.FileChooserParams.parseResult(result, data)); upload = null;
        }
    }
    @Override public void onBackPressed() {
        if (web != null && isAdmin(web.getUrl())) web.evaluateJavascript("if(document.querySelector('dialog[open]'))document.querySelector('dialog[open]').close();else if(!document.getElementById('modal').classList.contains('hidden'))closeModal();else closeSideMenu();", null);
        else super.onBackPressed();
    }
    @Override protected void onDestroy() {
        destroyed = true; handler.removeCallbacksAndMessages(null);
        if (upload != null) upload.onReceiveValue(null);
        if (web != null) web.destroy();
        super.onDestroy();
    }
}
