package com.miao.campuslogin;

import android.animation.ValueAnimator;
import android.app.Activity;
import android.app.Notification;
import android.app.NotificationChannel;
import android.app.NotificationManager;
import android.app.PendingIntent;
import android.content.ClipData;
import android.content.ClipboardManager;
import android.content.Intent;
import android.content.res.Configuration;
import android.graphics.drawable.Drawable;
import android.net.Network;
import android.os.Build;
import android.os.Bundle;
import android.os.Handler;
import android.os.Looper;
import android.view.View;
import android.view.Window;
import android.view.WindowManager;
import android.widget.EditText;
import android.widget.ProgressBar;
import android.widget.TextView;
import android.widget.Toast;

import java.net.HttpURLConnection;
import java.net.SocketTimeoutException;
import java.text.SimpleDateFormat;
import java.util.Date;
import java.util.Locale;

/**
 * 主界面。只有一块卡片：要么让你填账号，要么显示"正在认证 / 结果"。
 * 打开就自动开始认证 —— 和桌面版双击图标是同一个动作。
 */
public class MainActivity extends Activity {

    private static final String[] ISP_SUFFIX = {"@telecom", "@cmcc", "@unicom"};
    private static final String APP_VER = "2.1";   // 和 build.sh 的 --version-name 保持一致

    private final Handler ui = new Handler(Looper.getMainLooper());
    private int ispIndex = 0;

    // 登录态控件
    private View dot;
    private TextView title;
    private TextView body;
    private TextView meta;
    private ProgressBar bar;
    private View retry;
    private TextView copy;

    /** 认证中让状态圆点轻轻呼吸；出结果就停 */
    private ValueAnimator pulse;

    /** 凭据缓存：软件密钥那条路径要跑 PBKDF2，重复解密会明显拖慢启动 */
    private Creds.Data cachedCreds;

    /** 认证是否已在跑：连上 Wi-Fi 的回调和「再试一次」可能同时触发，别重复提交 */
    private volatile boolean loggingIn;

    /** "找网"流程是否在跑，防止 onResume / 重复点击并发触发 */
    private volatile boolean deciding;

    /** 卡片是否停在"等用户连上 Wi-Fi"的状态 —— 回到前台时用它决定要不要重新检查 */
    private volatile boolean waitingWifi;

    private static final int REQ_WIFI = 1002;

    private Creds.Data creds() {
        if (cachedCreds == null) {
            cachedCreds = Creds.load(this);
        }
        return cachedCreds;
    }

    @Override
    protected void onCreate(Bundle savedInstanceState) {
        super.onCreate(savedInstanceState);
        applyGlassWindow();
        askNotifyPermission();          // Android 13+ 要它系统才会显示「允许此网络」的提示
        if (creds() != null) {
            showLogin();
            autoConnectThenLogin();     // 先自动连校园网，连上再认证
        } else {
            showSetup();
        }
    }

    /** Android 13+ 通知权限：没有它，系统的「允许连接该网络」提示可能不出现 */
    private void askNotifyPermission() {
        if (Build.VERSION.SDK_INT >= 33) {
            try {
                if (checkSelfPermission("android.permission.POST_NOTIFICATIONS")
                        != android.content.pm.PackageManager.PERMISSION_GRANTED) {
                    requestPermissions(
                            new String[]{"android.permission.POST_NOTIFICATIONS"}, 1001);
                }
            } catch (Throwable ignored) {
            }
        }
    }

    /** 透明窗口 + 背景模糊：和桌面上那块毛玻璃一个思路。不支持的机器自动退化成半透明。 */
    private void applyGlassWindow() {
        Window w = getWindow();
        try {
            w.addFlags(WindowManager.LayoutParams.FLAG_BLUR_BEHIND);
        } catch (Throwable ignored) {
        }
        if (Build.VERSION.SDK_INT >= 31) {
            try {
                w.setBackgroundBlurRadius((int) dp(48));
            } catch (Throwable ignored) {
            }
        }
        // 浅色模式下状态栏要用深色图标，否则白图标压在白底上看不见
        try {
            boolean night = (getResources().getConfiguration().uiMode
                    & Configuration.UI_MODE_NIGHT_MASK) == Configuration.UI_MODE_NIGHT_YES;
            int flags = View.SYSTEM_UI_FLAG_LAYOUT_FULLSCREEN;
            if (!night) {
                flags |= View.SYSTEM_UI_FLAG_LIGHT_STATUS_BAR;
            }
            w.getDecorView().setSystemUiVisibility(flags);
        } catch (Throwable ignored) {
        }
    }

    // ------------------------------------------------------------------ 填账号

    private void showSetup() {
        setContentView(R.layout.activity_setup);
        bindScrimClose();

        final EditText account = findViewById(R.id.account);
        final EditText password = findViewById(R.id.password);
        final TextView err = findViewById(R.id.err);
        TextView[] isps = {findViewById(R.id.isp0), findViewById(R.id.isp1),
                findViewById(R.id.isp2)};

        for (int i = 0; i < isps.length; i++) {
            final int idx = i;
            isps[i].setOnClickListener(v -> {
                ispIndex = idx;
                for (int j = 0; j < isps.length; j++) {
                    boolean on = j == idx;
                    isps[j].setBackgroundResource(
                            on ? R.drawable.bg_button : R.drawable.bg_input);
                    isps[j].setTextColor(getColor(
                            on ? R.color.accent : R.color.text_body));
                }
            });
        }

        findViewById(R.id.save).setOnClickListener(v -> {
            String a = account.getText().toString().trim();
            String p = password.getText().toString();
            if (a.isEmpty() || p.isEmpty()) {
                err.setText(R.string.need_fill);
                return;
            }
            try {
                cachedCreds = null;
                Creds.save(this, a, p, ISP_SUFFIX[ispIndex]);
            } catch (Exception e) {
                // 把异常类型也显示出来 —— 只有 message 时经常看不出是哪一层出的问题
                err.setText("保存失败：" + e.getClass().getSimpleName()
                        + "\n" + String.valueOf(e.getMessage()) + "\nv" + APP_VER);
                return;
            }
            showLogin();
            autoConnectThenLogin();
        });
    }

    // ------------------------------------------------------------------ 认证

    private void showLogin() {
        setContentView(R.layout.activity_login);
        bindScrimClose();
        dot = findViewById(R.id.dot);
        title = findViewById(R.id.title);
        body = findViewById(R.id.body);
        meta = findViewById(R.id.meta);
        bar = findViewById(R.id.bar);
        retry = findViewById(R.id.retry);
        retry.setOnClickListener(v -> autoConnectThenLogin());

        // 连不上时给用户一个直达系统 Wi-Fi 面板的按钮
        findViewById(R.id.wifi).setOnClickListener(v -> WifiAuto.openWifiPanel(this));

        // 失败时一键把 IP / Wi-Fi / 服务器原话拷走
        copy = findViewById(R.id.copy);
        copy.setOnClickListener(v -> copyDiagnosis());

        // 密码填错了也能改，不用卸载重装
        findViewById(R.id.change).setOnClickListener(v -> {
            cachedCreds = null;
            Creds.clear(this);
            showSetup();
        });
    }

    /**
     * 打开图标后要做的事（v1.7 重写）。
     *
     * 逻辑就是用户说的那三条，不再有任何"反复找"：
     *   ① 已经连着校园网 → **直接认证，零扫描**
     *   ② 连着别的 Wi-Fi   → 不扫描（这时候扫也是白扫），直接开列表让用户切
     *   ③ Wi-Fi 开着但没连 → **扫，最多 5 次，找到即停**
     *        · 找到了 → 立刻停止扫描，去连它
     *        · 5 次都没有 → 停，明确告诉用户"不在覆盖范围"，不再空转
     *
     * 读 SSID 需要「附近设备/位置」权限；没权限就退回"按连接状态判断"。
     */
    private void autoConnectThenLogin() {
        if (creds() == null) {
            showSetup();
            return;
        }
        // 权限只是"锦上添花"：有它就能读出当前 Wi-Fi 名字（提示更准）；
        // 没有也能正常工作 —— 主路径靠 wlan0 的地址，不需要任何权限。
        // 所以这里**不拦路**，只在没问过的时候问一次。
        if (!WifiAuto.canScan(this)) {
            try {
                boolean asked = getSharedPreferences("campus_login", MODE_PRIVATE)
                        .getBoolean("perm_asked", false);
                if (!asked) {
                    getSharedPreferences("campus_login", MODE_PRIVATE)
                            .edit().putBoolean("perm_asked", true).apply();
                    requestPermissions(new String[]{WifiAuto.permissionName()}, REQ_WIFI);
                }
            } catch (Throwable ignored) {
            }
        }
        decide();
    }

    @Override
    public void onRequestPermissionsResult(int req, String[] perms, int[] res) {
        super.onRequestPermissionsResult(req, perms, res);
        if (req == REQ_WIFI) {
            decide();                           // 给没给都继续
        }
    }

    @Override
    protected void onResume() {
        super.onResume();
        // 用户可能刚从 Wi-Fi 列表回来：这时立刻看一眼，连上就直接认证，不等任何定时器
        if (!waitingWifi || loggingIn) {
            return;
        }
        ui.postDelayed(() -> {
            if (!waitingWifi || loggingIn) {
                return;
            }
            if (LocalIp.ipv4(this) != null) {
                onWifiConnected();          // 从列表回来时已经连上了，直接继续
            }
        }, 350);
    }

    /**
     * 判断 + 分支。
     *
     * v1.8 彻底去掉了"扫描"这条路径：真机实测扫描返回空列表
     * （安卓 9 起要系统定位开关打开才给结果，部分定制 ROM 直接给空），
     * 拿它当主路径就永远是"扫 5 次都没找到"。
     *
     * 现在的逻辑只有两步，没有循环：
     *   ① 已经拿到 Wi-Fi 的 IPv4 → 直接认证（这就是"已经连上了"）
     *   ② 拿不到 → 打开 Wi-Fi 列表，用户点一下；连上的瞬间自动认证
     */
    private void decide() {
        if (loggingIn || deciding) {
            return;
        }
        deciding = true;
        setBusy("正在检查网络", "看看现在是不是已经连着校园网…");

        new Thread(() -> {
            try {
                // 已经连着别的网络？先提醒（读得到名字才判断，读不到就交给服务器）
                String ssid = WifiAuto.currentSsid(this);
                if (ssid != null && !WifiAuto.SSID.equalsIgnoreCase(ssid)) {
                    WifiAuto.suggest(this);
                    ui.post(() -> {
                        showWaitingWifi("现在连的是「" + ssid + "」",
                                "列表已打开，点 " + WifiAuto.SSID
                                        + "（没看到就往下翻「已保存的网络」）\n连上后会自动认证");
                        WifiAuto.openWifiPanel(this);
                    });
                    WifiAuto.watchWifi(this, () -> ui.post(this::onWifiConnected));
                    return;
                }

                // ① 已经有 Wi-Fi 地址 → 直接认证，不扫描、不等待
                final String ip = LocalIp.ipv4(this);
                if (ip != null) {
                    ui.post(() -> {
                        setBusy("正在认证", "已检测到 Wi-Fi 地址 " + ip + "，正在提交账号…");
                        startLogin();
                    });
                    return;
                }

                // ② 没有地址（没连、或还没分到 IP）→ 开列表，让用户点一下，之后全自动
                WifiAuto.suggest(this);
                ui.post(() -> {
                    showWaitingWifi("请连上校园网",
                            "列表已打开，点 " + WifiAuto.SSID
                                    + "（没看到就往下翻「已保存的网络」）\n连上后会自动认证，不用再点");
                    WifiAuto.openWifiPanel(this);
                });
                WifiAuto.watchWifi(this, () -> ui.post(this::onWifiConnected));
            } finally {
                deciding = false;
            }
        }, "wifi-decide").start();
    }

    /** 停在这里等用户连，**不转圈、不计时、不报失败** —— 连上就自动走。 */
    private void showWaitingWifi(String title, String bodyText) {
        stopPulse();
        waitingWifi = true;
        this.title.setText(title);
        body.setText(bodyText);
        tintDot(R.color.accent);
        bar.setVisibility(View.GONE);          // 关键：不转圈，避免"一直在处理"的错觉
        retry.setVisibility(View.GONE);
        meta.setText(diag());
        showWifiButton();
    }

    /** 系统报告 Wi-Fi 连上了（或用户刚从列表回来）—— 等地址就绪就认证。 */
    private void onWifiConnected() {
        if (loggingIn) {
            return;
        }
        if (LocalIp.ipv4(this) == null) {
            // 刚连上，DHCP 通常 1~3 秒；这里在后台等，不弹任何失败
            new Thread(() -> {
                for (int i = 0; i < 15; i++) {
                    if (LocalIp.ipv4(MainActivity.this) != null) {
                        break;
                    }
                    sleepQuiet(400);
                }
                ui.post(this::onWifiConnected);
            }, "wait-ip").start();
            return;
        }

        waitingWifi = false;
        setBusy("正在认证", "已连上 Wi-Fi，正在提交账号…");
        new Thread(() -> {
            for (int i = 0; i < 12; i++) {        // 最多等 4.8 秒拿到 IPv4
                if (LocalIp.ipv4(MainActivity.this) != null) {
                    break;
                }
                sleepQuiet(400);
            }
            ui.post(this::startLogin);
        }, "wait-ip").start();
    }

    private void showWifiButton() {
        View wb = findViewById(R.id.wifi);
        if (wb != null) {
            wb.setVisibility(View.VISIBLE);
        }
    }

    /** 失败时把关键状态写出来 —— 下次一看就知道卡在哪一层 */
    private String diag() {
        String ssid = WifiAuto.currentSsid(this);
        String ip = LocalIp.ipv4(this);
        return "当前 " + (ssid == null ? "未连 Wi-Fi" : ssid)
                + " · IP " + (ip == null ? "无" : ip)
                + " · v" + APP_VER;
    }

    static void sleepQuiet(long ms) {
        try {
            Thread.sleep(ms);
        } catch (InterruptedException ignored) {
        }
    }

    private void setBusy(String t, String b) {
        startPulse();
        title.setText(t);
        body.setText(b);
        tintDot(R.color.accent);
        bar.setVisibility(View.VISIBLE);
        retry.setVisibility(View.GONE);
        View wb = findViewById(R.id.wifi);
        if (wb != null) {
            wb.setVisibility(View.GONE);
        }
        meta.setText("");
    }

    /**
     * 提交认证。
     *
     * v1.9 的重点是"**别急着判失败**"：
     *  ① 刚连上 Wi-Fi 时，系统还要配路由、建 ARP、门户侧建立会话 —— 先等网络真的就绪
     *  ② 提交后如果被临时拒绝（在线数超限 / 无响应），**隔几秒自动再来**，最多 3 次
     *  ③ 只有"确实被账号侧拒绝"（密码过期、绑定错误）才立刻停
     */
    private void startLogin() {
        final Creds.Data d = creds();
        if (d == null) {
            showSetup();
            return;
        }
        if (loggingIn) {
            return;
        }
        loggingIn = true;
        setBusy("正在认证", "先等网络稳住，再提交账号…");

        new Thread(() -> {
            // ① 等 Wi-Fi 地址（最多 8 秒）
            String ip = null;
            for (int i = 0; i < 20 && ip == null; i++) {
                ip = LocalIp.ipv4(MainActivity.this);
                if (ip == null) {
                    sleepQuiet(400);
                }
            }
            if (ip == null) {
                loggingIn = false;
                ui.post(() -> showResult(false,
                        "Wi-Fi 还没拿到地址\n等两秒再点「再试一次」", diag()));
                return;
            }
            final String fIp = ip;

            // ② 请求绑定到 Wi-Fi 网卡（拿不到 Network 就用系统路由直连）
            final Network wifi = LocalIp.wifiNetwork(MainActivity.this);
            if (wifi != null) {
                Portal.connector = url -> (HttpURLConnection) wifi.openConnection(url);
            } else {
                Portal.connector = url -> (HttpURLConnection) url.openConnection();
            }

            // ③ 等网络真的就绪：门户能连上，最多等 8 秒。
            //    这一步就是"沉住气"——太早提交，服务器容易当成异常会话直接拒。
            final long readyDeadline = System.currentTimeMillis() + 8000;
            while (System.currentTimeMillis() < readyDeadline) {
                if (WifiAuto.portalReachable(MainActivity.this, 600)) {
                    break;
                }
                sleepQuiet(500);
            }

            // ④ 提交，最多 3 次，间隔 4s / 6s
            String okCode = "0";
            String msg = "认证失败";
            String raw = null;
            int used = 0;

            for (int attempt = 1; attempt <= 3; attempt++) {
                used = attempt;
                if (attempt > 1) {
                    final int a = attempt;
                    final String hint = Portal.human(raw);
                    final boolean busy = Portal.isBusy(raw);
                    ui.post(() -> setBusy("正在重试认证",
                            "第 " + a + "/3 次，等 " + (busy ? "十几" : "几")
                                    + " 秒\n上一次：" + hint));
                    // 服务器自己说忙的时候，连着敲没用 —— 给它喘口气
                    if (busy) {
                        sleepQuiet(attempt == 2 ? 12000 : 25000);
                    } else {
                        sleepQuiet(attempt == 2 ? 4000 : 8000);
                    }
                }
                try {
                    raw = Portal.login(d.account, d.password, d.isp, fIp);
                    String[] r = Portal.judge(raw);
                    okCode = r[0];
                    msg = r[1];
                    if ("1".equals(okCode)) {
                        break;
                    }
                    if (!Portal.retryable(raw) || attempt == 3) {
                        break;                     // 账号侧拒绝，重试没意义
                    }
                } catch (SocketTimeoutException e) {
                    raw = null;
                    msg = "认证服务器没响应（6 秒超时）\n确认手机连的是 " + WifiAuto.SSID;
                    if (attempt == 3) {
                        break;
                    }
                } catch (Exception e) {
                    raw = null;
                    String extra = e.getMessage() == null ? "" : "\n" + e.getMessage();
                    msg = "请求失败：" + e.getClass().getSimpleName() + extra;
                    break;
                }
            }

            final String fOk = okCode;
            final String fMsg = msg;
            final int fUsed = used;
            final String fRaw = raw;
            loggingIn = false;
            ui.post(() -> {
                String info = "IP " + fIp + " · "
                        + (wifi != null ? "已绑定 Wi-Fi" : "经网卡直连")
                        + (fUsed > 1 ? " · 尝试 " + fUsed + " 次" : "")
                        + " · v" + APP_VER;
                if (fRaw != null) {
                    info += "\n返回 " + cut(fRaw, 160);
                }
                showResult("1".equals(fOk), fMsg, info);
            });
        }, "portal-login").start();
    }

    // ---------------------------------------------------------------- 交互与反馈

    /** 失败时一键复制：时间 + 标题 + 说明 + 我在意的那行（IP/SSID/版本） */
    private void copyDiagnosis() {
        try {
            String ts = new SimpleDateFormat("yyyy-MM-dd HH:mm:ss", Locale.CHINA)
                    .format(new Date());
            String txt = "校园网一键登录 诊断\n"
                    + ts + "\n"
                    + title.getText() + "：" + body.getText() + "\n"
                    + meta.getText();
            ClipboardManager cm = (ClipboardManager) getSystemService(CLIPBOARD_SERVICE);
            if (cm != null) {
                cm.setPrimaryClip(ClipData.newPlainText("校园网诊断", txt));
                Toast.makeText(this, "已复制，直接发给我就行", Toast.LENGTH_SHORT).show();
            }
        } catch (Throwable ignored) {
        }
    }

    /** 结果也发一条通知：卡片 5 秒后自动关，关了还能回头看见 */
    private void notifyResult(boolean ok, String t, String msg) {
        try {
            NotificationManager nm =
                    (NotificationManager) getSystemService(NOTIFICATION_SERVICE);
            if (nm == null) {
                return;
            }
            final String chId = "login_result";
            if (Build.VERSION.SDK_INT >= 26) {
                NotificationChannel ch = new NotificationChannel(
                        chId, "认证结果", NotificationManager.IMPORTANCE_DEFAULT);
                ch.setShowBadge(false);
                nm.createNotificationChannel(ch);
            }
            Intent open = new Intent(this, MainActivity.class)
                    .addFlags(Intent.FLAG_ACTIVITY_SINGLE_TOP);
            int piFlags = PendingIntent.FLAG_UPDATE_CURRENT;
            if (Build.VERSION.SDK_INT >= 23) {
                piFlags |= PendingIntent.FLAG_IMMUTABLE;
            }
            PendingIntent pi = PendingIntent.getActivity(this, 0, open, piFlags);

            Notification.Builder b = (Build.VERSION.SDK_INT >= 26)
                    ? new Notification.Builder(this, chId)
                    : new Notification.Builder(this);
            b.setSmallIcon(R.drawable.ic_notify)
                    .setContentTitle(t)
                    .setContentText(String.valueOf(msg).replace('\n', ' '))
                    .setStyle(new Notification.BigTextStyle().bigText(String.valueOf(msg)))
                    .setAutoCancel(true)
                    .setContentIntent(pi);
            nm.notify(ok ? 1001 : 1002, b.build());
        } catch (Throwable ignored) {
        }
    }

    /** 认证中：圆点轻轻呼吸，让人一眼看出"还在干活"而不是卡死 */
    private void startPulse() {
        stopPulse();
        try {
            pulse = ValueAnimator.ofFloat(1f, 0.32f, 1f);
            pulse.setDuration(1200);
            pulse.setRepeatCount(ValueAnimator.INFINITE);
            pulse.addUpdateListener(a -> {
                if (dot != null) {
                    dot.setAlpha((Float) a.getAnimatedValue());
                }
            });
            pulse.start();
        } catch (Throwable ignored) {
        }
    }

    private void stopPulse() {
        try {
            if (pulse != null) {
                pulse.cancel();
                pulse = null;
            }
            if (dot != null) {
                dot.setAlpha(1f);
            }
        } catch (Throwable ignored) {
        }
    }

    /** 出结果：圆点轻轻弹一下，视线会自然落到标题上 */
    private void popDot() {
        try {
            if (dot == null) {
                return;
            }
            dot.setScaleX(1f);
            dot.setScaleY(1f);
            dot.animate().scaleX(1.22f).scaleY(1.22f).setDuration(150)
                    .withEndAction(() -> dot.animate()
                            .scaleX(1f).scaleY(1f).setDuration(220).start())
                    .start();
        } catch (Throwable ignored) {
        }
    }

    private static String cut(String s, int max) {
        if (s == null) {
            return "";
        }
        s = s.trim().replace("\r", " ").replace("\n", " ");
        return s.length() <= max ? s : s.substring(0, max) + "…";
    }

    private void showResult(boolean ok, String msg, String info) {
        stopPulse();
        title.setText(ok ? "登录成功" : "登录失败");
        body.setText(msg);
        tintDot(ok ? R.color.ok : R.color.err);
        bar.setVisibility(View.GONE);
        retry.setVisibility(ok ? View.GONE : View.VISIBLE);
        if (copy != null) {
            copy.setVisibility(ok ? View.GONE : View.VISIBLE);
        }
        popDot();
        notifyResult(ok, ok ? "登录成功" : "登录失败", msg);
        if (ok) {
            View wb = findViewById(R.id.wifi);
            if (wb != null) {
                wb.setVisibility(View.GONE);
            }
        }
        meta.setText(info);

        if (ok) {
            // 成功停 5 秒再退出，看得清；想早点走可以点卡片外面
            ui.postDelayed(this::finish, 5000);
        }
    }

    private void tintDot(int colorRes) {
        Drawable bg = dot.getBackground();
        if (bg != null) {
            Drawable d = bg.mutate();
            d.setTint(getColor(colorRes));
            dot.setBackground(d);
        }
        title.setTextColor(getColor(R.color.text_title));
    }

    /** 点卡片外面 = 关掉 */
    private void bindScrimClose() {
        View scrim = findViewById(R.id.scrim);
        if (scrim != null) {
            scrim.setOnClickListener(v -> finish());
        }
    }

    private float dp(float v) {
        return v * getResources().getDisplayMetrics().density;
    }

    @Override
    protected void onDestroy() {
        WifiAuto.unwatchWifi(this);
        super.onDestroy();
    }
}
