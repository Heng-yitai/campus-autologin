package com.miao.campuslogin;

import android.app.Activity;
import android.content.Context;
import android.content.Intent;
import android.net.ConnectivityManager;
import android.net.Network;
import android.net.NetworkCapabilities;
import android.net.NetworkRequest;
import android.net.wifi.WifiConfiguration;
import android.net.wifi.WifiInfo;
import android.net.wifi.WifiManager;
import android.net.wifi.WifiNetworkSuggestion;
import android.os.Build;
import android.provider.Settings;

import java.net.InetSocketAddress;
import java.net.Socket;
import java.util.Collections;
import java.util.List;

/**
 * 自动把手机接到校园网。
 *
 * Android 的平台限制（必须讲清楚，否则用户以为程序坏了）：
 *  · **Android 10（API 29）起，普通 App 不能自己开关 Wi-Fi**，`setWifiEnabled` 直接失效。
 *    能做的最大程度是拉起系统的 Wi-Fi 面板，让用户点一下那个开关。
 *  · **不能静默连接任意 Wi-Fi**。官方留的通道是「网络建议」(WifiNetworkSuggestion)：
 *    把校园网加进建议列表，系统扫到就自动连；**第一次要用户在通知里允许一次**。
 *
 * 这个类的原则（v1.7 定下来的）：
 *  1. **先读"现在连的是哪个网"**，能读就读 —— 读到了就不用猜、不用扫。
 *  2. 需要扫描时**最多扫 5 次，找到即停**；绝不无限循环。
 *  3. 端口探测只用来当提示，**绝不拿它当"能不能认证"的闸门**。
 */
final class WifiAuto {

    static final String SSID = "6#SS-WiFi";

    private WifiAuto() {
    }

    // ------------------------------------------------------------------ 权限

    /** 读 SSID / 扫描 Wi-Fi 需要的权限：33+ 用「附近设备」，之前用定位。 */
    static boolean canScan(Context ctx) {
        try {
            if (Build.VERSION.SDK_INT >= 33) {
                return ctx.checkSelfPermission("android.permission.NEARBY_WIFI_DEVICES")
                        == android.content.pm.PackageManager.PERMISSION_GRANTED;
            }
            return ctx.checkSelfPermission(android.Manifest.permission.ACCESS_FINE_LOCATION)
                    == android.content.pm.PackageManager.PERMISSION_GRANTED;
        } catch (Throwable e) {
            return false;
        }
    }

    static String permissionName() {
        return (Build.VERSION.SDK_INT >= 33)
                ? "android.permission.NEARBY_WIFI_DEVICES"
                : android.Manifest.permission.ACCESS_FINE_LOCATION;
    }

    // ------------------------------------------------------------------ 当前连的是哪个网

    /**
     * 当前 Wi-Fi 的 SSID（去掉引号）；没连、没权限、读不到都返回 null。
     *
     * 这个方法是 v1.7 的关键：以前靠"门户端口通不通"反推，会误判；
     * 直接读名字，一眼就知道"是不是已经连着校园网了"。
     */
    static String currentSsid(Context ctx) {
        try {
            WifiManager wm = (WifiManager) ctx.getApplicationContext()
                    .getSystemService(Context.WIFI_SERVICE);
            if (wm == null || !wm.isWifiEnabled()) {
                return null;
            }
            WifiInfo info = wm.getConnectionInfo();
            return clean(info == null ? null : info.getSSID());
        } catch (Throwable e) {
            return null;
        }
    }

    private static String clean(String s) {
        if (s == null) {
            return null;
        }
        s = s.trim();
        if (s.length() >= 2 && s.startsWith("\"") && s.endsWith("\"")) {
            s = s.substring(1, s.length() - 1);
        }
        if (s.isEmpty() || s.contains("unknown") || s.contains("UNKNOWN")
                || s.equals("<none>")) {
            return null;
        }
        return s;
    }

    // ------------------------------------------------------------------ 开关 Wi-Fi

    static boolean isWifiOn(Context ctx) {
        try {
            WifiManager wm = (WifiManager) ctx.getApplicationContext()
                    .getSystemService(Context.WIFI_SERVICE);
            return wm != null && wm.isWifiEnabled();
        } catch (Throwable e) {
            return false;
        }
    }

    /**
     * 尝试打开 Wi-Fi。
     *
     * @return true = 已经开着或成功打开；false = 打不开（已把系统面板拉起来让用户点）
     */
    static boolean ensureWifiOn(Activity act) {
        try {
            WifiManager wm = (WifiManager) act.getApplicationContext()
                    .getSystemService(Context.WIFI_SERVICE);
            if (wm != null && wm.isWifiEnabled()) {
                return true;
            }
            if (Build.VERSION.SDK_INT < 29 && wm != null) {
                //noinspection deprecation
                if (wm.setWifiEnabled(true)) {
                    return true;
                }
            }
            openWifiPanel(act);
        } catch (Throwable ignored) {
            openWifiPanel(act);
        }
        return false;
    }

    /** 拉起 Wi-Fi 面板/设置页。API 29+ 用快速面板，一键就能开。 */
    static void openWifiPanel(Activity act) {
        try {
            Intent i;
            if (Build.VERSION.SDK_INT >= 29) {
                i = new Intent(Settings.Panel.ACTION_WIFI);
            } else {
                i = new Intent(Settings.ACTION_WIFI_SETTINGS);
            }
            i.addFlags(Intent.FLAG_ACTIVITY_NEW_TASK);
            act.startActivity(i);
        } catch (Throwable e) {
            try {
                Intent fallback = new Intent(Settings.ACTION_WIFI_SETTINGS);
                fallback.addFlags(Intent.FLAG_ACTIVITY_NEW_TASK);
                act.startActivity(fallback);
            } catch (Throwable ignored) {
            }
        }
    }

    // ------------------------------------------------------------------ 自动连接

    /**
     * 把校园网加进「建议网络」。
     *
     * @return 0 表示成功/已存在；负数表示失败（不影响后续，只是不能自动连）
     */
    static int suggest(Context ctx) {
        try {
            WifiManager wm = (WifiManager) ctx.getApplicationContext()
                    .getSystemService(Context.WIFI_SERVICE);
            if (wm == null) {
                return -1;
            }

            if (Build.VERSION.SDK_INT >= 29) {
                WifiNetworkSuggestion s = new WifiNetworkSuggestion.Builder()
                        .setSsid(SSID)
                        .setIsHiddenSsid(false)
                        .build();
                int st = wm.addNetworkSuggestions(Collections.singletonList(s));
                // 0=成功 2=重复 3=已在活动列表中，都算"已经在了"
                return (st == 0 || st == 2 || st == 3) ? 0 : st;
            }

            // Android 9 及以下：直接加一个开放网络并启用
            WifiConfiguration cfg = new WifiConfiguration();
            cfg.SSID = "\"" + SSID + "\"";
            cfg.allowedKeyManagement.set(WifiConfiguration.KeyMgmt.NONE);
            //noinspection deprecation
            int netId = wm.addNetwork(cfg);
            if (netId < 0) {
                return -1;
            }
            //noinspection deprecation
            wm.disconnect();
            //noinspection deprecation
            boolean enabled = wm.enableNetwork(netId, true);
            //noinspection deprecation
            wm.reconnect();
            return enabled ? 0 : -2;
        } catch (Throwable e) {
            return -1;
        }
    }

    // ------------------------------------------------------------------ 探测（只做提示）

    /**
     * 认证门户是否可达。**只用来判断"要不要多试一次"，绝不当作能不能认证的闸门。**
     *
     * 两个端口都试：登录接口在 801，门户网页在 80，哪个通都算通。
     */
    static boolean portalReachable(Context ctx, int timeoutMs) {
        List<Network> nets = LocalIp.wifiNetworks(ctx);
        for (Network n : nets) {
            if (portalOn(n, timeoutMs)) {
                return true;
            }
        }
        return false;
    }

    private static boolean portalOn(Network n, int timeoutMs) {
        for (int port : new int[]{Portal.PORT, 80}) {
            Socket s = null;
            try {
                // 绑定到 Wi-Fi 网卡：默认路由是流量时，内网地址只能从这张网卡出去
                s = n.getSocketFactory().createSocket();
                s.connect(new InetSocketAddress(Portal.HOST, port), timeoutMs);
                return true;
            } catch (Throwable ignored) {
            } finally {
                if (s != null) {
                    try {
                        s.close();
                    } catch (Throwable ignored) {
                    }
                }
            }
        }
        return false;
    }

    // ------------------------------------------------------------------ 监听连上

    private static ConnectivityManager.NetworkCallback wifiCallback;

    /** 监听"Wi-Fi 连上了"，连上的那一瞬间回调，不用轮询。 */
    static boolean watchWifi(Context ctx, Runnable onConnected) {
        try {
            ConnectivityManager cm =
                    (ConnectivityManager) ctx.getApplicationContext()
                            .getSystemService(Context.CONNECTIVITY_SERVICE);
            if (cm == null) {
                return false;
            }
            unwatchWifi(ctx);
            NetworkRequest req = new NetworkRequest.Builder()
                    .addTransportType(NetworkCapabilities.TRANSPORT_WIFI)
                    .build();
            wifiCallback = new ConnectivityManager.NetworkCallback() {
                @Override
                public void onAvailable(Network network) {
                    try {
                        onConnected.run();
                    } catch (Throwable ignored) {
                    }
                }
            };
            cm.registerNetworkCallback(req, wifiCallback);
            return true;
        } catch (Throwable e) {
            return false;
        }
    }

    static void unwatchWifi(Context ctx) {
        if (wifiCallback == null) {
            return;
        }
        try {
            ConnectivityManager cm =
                    (ConnectivityManager) ctx.getApplicationContext()
                            .getSystemService(Context.CONNECTIVITY_SERVICE);
            if (cm != null) {
                cm.unregisterNetworkCallback(wifiCallback);
            }
        } catch (Throwable ignored) {
        }
        wifiCallback = null;
    }
}
