package io.github.xiaofan6ya.workbuddy2api;

import android.app.*;
import android.content.Intent;
import android.content.pm.ServiceInfo;
import android.os.*;
import com.chaquo.python.Python;
import com.chaquo.python.PyObject;
import com.chaquo.python.android.AndroidPlatform;
import org.json.JSONObject;
import java.util.concurrent.atomic.AtomicInteger;

public class LocalApiService extends Service {
    static volatile String origin, token, error;
    static volatile boolean starting;
    private static final String CHANNEL = "local-api";
    private static final Object RUNTIME_LOCK = new Object();
    private static final AtomicInteger generation = new AtomicInteger();
    private int instanceGeneration;
    private Thread worker;
    private PowerManager.WakeLock wakeLock;
    private volatile boolean stopping;

    @Override public void onCreate() {
        super.onCreate();
        instanceGeneration = generation.incrementAndGet();
        if (Build.VERSION.SDK_INT >= 26) {
            NotificationChannel channel = new NotificationChannel(CHANNEL, "本地 API 服务", NotificationManager.IMPORTANCE_LOW);
            getSystemService(NotificationManager.class).createNotificationChannel(channel);
        }
        Notification notification = notification("正在启动本地 API 服务…");
        if (Build.VERSION.SDK_INT >= 34) startForeground(1, notification, ServiceInfo.FOREGROUND_SERVICE_TYPE_SPECIAL_USE);
        else startForeground(1, notification);
        PowerManager power = (PowerManager) getSystemService(POWER_SERVICE);
        wakeLock = power.newWakeLock(PowerManager.PARTIAL_WAKE_LOCK, getPackageName() + ":local-api");
        wakeLock.acquire();
    }

    private Notification notification(String text) {
        int flags = PendingIntent.FLAG_UPDATE_CURRENT | PendingIntent.FLAG_IMMUTABLE;
        PendingIntent open = PendingIntent.getActivity(this, 0, new Intent(this, MainActivity.class), flags);
        PendingIntent stop = PendingIntent.getService(this, 1, new Intent(this, LocalApiService.class).setAction("stop"), flags);
        Notification.Builder builder = Build.VERSION.SDK_INT >= 26 ? new Notification.Builder(this, CHANNEL) : new Notification.Builder(this);
        return builder.setSmallIcon(R.drawable.notification_icon).setContentTitle("WorkBuddy2API")
            .setContentText(text).setContentIntent(open).setOngoing(true)
            .addAction(new Notification.Action.Builder(null, "停止服务", stop).build()).build();
    }

    @Override public int onStartCommand(Intent intent, int flags, int startId) {
        if (intent != null && "stop".equals(intent.getAction())) { stopSelf(); return START_NOT_STICKY; }
        if (worker == null) {
            error = null;
            starting = true;
            worker = new Thread(() -> {
                try {
                    synchronized (RUNTIME_LOCK) {
                    if (!Python.isStarted()) Python.start(new AndroidPlatform(this));
                    PyObject runtime = Python.getInstance().getModule("android_runtime");
                    if (stopping) return;
                    JSONObject state = new JSONObject(runtime.callAttr("start", getFilesDir().getAbsolutePath()).toString());
                    if (stopping) { runtime.callAttr("stop"); return; }
                    token = state.getString("token");
                    origin = state.getString("origin");
                    getSystemService(NotificationManager.class).notify(1, notification(origin + "/v1 · 服务运行中"));
                    }
                } catch (Exception e) {
                    error = "启动失败：" + e.getMessage();
                    getSystemService(NotificationManager.class).notify(1, notification("启动失败，请重新打开应用"));
                    stopSelf();
                } finally {
                    starting = false;
                }
            }, "workbuddy-python-start");
            worker.start();
        }
        return START_STICKY;
    }

    @Override public void onDestroy() {
        stopping = true;
        starting = false;
        origin = null; token = null;
        new Thread(() -> {
            if (worker != null) try { worker.join(95000); } catch (InterruptedException ignored) { }
            synchronized (RUNTIME_LOCK) {
                // A delayed cleanup must not stop a service the user just restarted.
                if (generation.get() == instanceGeneration && Python.isStarted())
                    try { Python.getInstance().getModule("android_runtime").callAttr("stop"); } catch (Exception ignored) { }
            }
        }, "workbuddy-python-stop").start();
        if (wakeLock != null && wakeLock.isHeld()) wakeLock.release();
        stopForeground(true);
        super.onDestroy();
    }

    @Override public IBinder onBind(Intent intent) { return null; }
}
