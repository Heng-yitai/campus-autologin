package com.miao.campuslogin;

import android.content.Context;
import android.net.ConnectivityManager;
import android.net.LinkAddress;
import android.net.LinkProperties;
import android.net.Network;
import android.net.NetworkCapabilities;

import java.net.Inet4Address;
import java.net.InetAddress;
import java.net.InterfaceAddress;
import java.net.NetworkInterface;
import java.util.ArrayList;
import java.util.Collections;
import java.util.List;

/**
 * 找出"校园网那张 Wi-Fi 网卡"及其地址。
 *
 * ⚠️ 这里**绝不能再用 NetworkInfo**：
 * `ConnectivityManager.getNetworkInfo(Network)` 从 Android 10（API 29）起被废弃，
 * 在很多 ROM（尤其国产深度定制 ROM）上直接返回 null。用它判断"有没有连 Wi-Fi"，
 * 就会出现「Wi-Fi 明明连着，App 却认为没连」这种最气人的 bug。
 * 一律改用 `NetworkCapabilities.hasTransport(TRANSPORT_WIFI)`。
 *
 * 手机和电脑不一样：插着移动数据时，系统默认路由走的是流量，
 * 而 192.168.0.252 这种内网地址**只能从 Wi-Fi 出去**。
 * 所以这里既给出地址，也给出 Network 对象 —— 后者用来把请求绑定到 Wi-Fi 上。
 *
 * 关键原则：**没有 Wi-Fi 就返回 null**，绝不退而求其次用移动数据的地址 ——
 * 把错误的地头发给服务器，只会得到一句莫名其妙的"账号或密码错误"。
 */
final class LocalIp {

    private LocalIp() {
    }

    /** 所有已连上的 Wi-Fi 网络（通常 0 或 1 个，双 Wi-Fi 机型可能 2 个）。 */
    static List<Network> wifiNetworks(Context ctx) {
        List<Network> out = new ArrayList<>();
        try {
            ConnectivityManager cm =
                    (ConnectivityManager) ctx.getSystemService(Context.CONNECTIVITY_SERVICE);
            if (cm == null) {
                return out;
            }
            for (Network n : cm.getAllNetworks()) {
                NetworkCapabilities c = cm.getNetworkCapabilities(n);
                if (c != null && c.hasTransport(NetworkCapabilities.TRANSPORT_WIFI)) {
                    out.add(n);
                }
            }
        } catch (Throwable ignored) {
        }
        return out;
    }

    /** 当前连着的 Wi-Fi 网络对象；没有就 null。优先返回已经拿到 IPv4 的那个。 */
    static Network wifiNetwork(Context ctx) {
        Network backup = null;
        try {
            ConnectivityManager cm =
                    (ConnectivityManager) ctx.getSystemService(Context.CONNECTIVITY_SERVICE);
            if (cm == null) {
                return null;
            }
            for (Network n : wifiNetworks(ctx)) {
                if (ipv4Of(cm, n) != null) {
                    return n;
                }
                if (backup == null) {
                    backup = n;            // 正在 DHCP 的，先记下
                }
            }
        } catch (Throwable ignored) {
        }
        return backup;
    }

    private static String ipv4Of(ConnectivityManager cm, Network n) {
        try {
            LinkProperties lp = cm.getLinkProperties(n);
            if (lp == null) {
                return null;
            }
            for (LinkAddress la : lp.getLinkAddresses()) {
                InetAddress a = la.getAddress();
                if (a instanceof Inet4Address
                        && !a.isLoopbackAddress() && !a.isLinkLocalAddress()) {
                    return a.getHostAddress();
                }
            }
        } catch (Throwable ignored) {
        }
        return null;
    }

    /**
     * Wi-Fi 上的 IPv4；还没拿到地址（DHCP 中）或没连 Wi-Fi 都返回 null。
     *
     * 两条路都试：
     *  ① ConnectivityManager（准，但个别 ROM 在"连上但无外网"时枚举不到 Network）
     *  ② 直接问系统网卡列表里的 wlan* 接口（不需要任何权限，最不容易失灵）
     *
     * 第二条是 v1.8 加的：真机上出现过「系统里明明连着 6#SS-WiFi，
     * 程序却说你没连 Wi-Fi」—— 第 ① 条失效时，第 ② 条能兜住。
     */
    static String ipv4(Context ctx) {
        try {
            ConnectivityManager cm =
                    (ConnectivityManager) ctx.getSystemService(Context.CONNECTIVITY_SERVICE);
            if (cm != null) {
                Network n = wifiNetwork(ctx);
                if (n != null) {
                    String ip = ipv4Of(cm, n);
                    if (ip != null) {
                        return ip;
                    }
                }
            }
        } catch (Throwable ignored) {
        }
        return ipv4FromWlanInterface();
    }

    /** 直接遍历网卡：wlan0 上有 IPv4 就说明连着 Wi-Fi，且这就是要报给服务器的地址。 */
    private static String ipv4FromWlanInterface() {
        try {
            for (NetworkInterface nif : Collections.list(NetworkInterface.getNetworkInterfaces())) {
                String name = nif.getName();
                if (name == null || !name.startsWith("wlan") || !nif.isUp()) {
                    continue;
                }
                for (InterfaceAddress ia : nif.getInterfaceAddresses()) {
                    InetAddress a = ia.getAddress();
                    if (a instanceof Inet4Address
                            && !a.isLoopbackAddress() && !a.isLinkLocalAddress()) {
                        return a.getHostAddress();
                    }
                }
            }
        } catch (Throwable ignored) {
        }
        return null;
    }
}
