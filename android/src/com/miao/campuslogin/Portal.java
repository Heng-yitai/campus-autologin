package com.miao.campuslogin;

import java.io.BufferedReader;
import java.io.IOException;
import java.io.InputStream;
import java.io.InputStreamReader;
import java.net.HttpURLConnection;
import java.net.URL;
import java.net.URLEncoder;
import java.util.Random;
import java.util.regex.Matcher;
import java.util.regex.Pattern;

/**
 * 校园网认证（纯 Java，不含任何 Android API）。
 *
 * 参数和桌面版完全一致 —— 桌面版是抓真实请求抓出来的，这里照抄，别自己发明。
 * 关键点：门户按"来源 IP"认人，所以 wlan_user_ip 必须是**这台手机自己**在校园网里的地址。
 *
 * 之所以不碰 Android API：这样可以拿普通 JVM 直接跑测试（见 _test/）。
 */
final class Portal {

    static String HOST = "192.168.0.252";
    static int PORT = 801;
    static final String PATH = "/eportal/portal/login";
    static final String JS_VERSION = "4.2";

    /** 与桌面版**一字不差**的 UA —— 少一个变量，少一种出错的可能。 */
    static final String UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            + "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36";

    /**
     * 建连接的入口。手机上有多个网络时（Wi-Fi + 移动数据），必须**绑到 Wi-Fi 上**，
     * 否则请求可能从移动数据出去，192.168.0.252 这种内网地址根本到不了。
     * 默认实现走系统默认路由，JVM 测试用这个默认实现即可。
     */
    interface Connector {
        HttpURLConnection open(URL url) throws IOException;
    }

    static Connector connector = url -> (HttpURLConnection) url.openConnection();

    private static final Random RND = new Random();

    private Portal() {
    }

    /** 提交认证。返回服务器原文（JSONP）。 */
    static String login(String account, String password, String isp, String ip)
            throws IOException {
        String query = "callback=dr" + (1000 + RND.nextInt(9000))
                + "&login_method=1"
                + "&user_account=" + enc(account + isp)
                + "&user_password=" + enc(password)
                + "&wlan_user_ip=" + enc(ip)
                + "&wlan_user_ipv6="
                + "&wlan_user_mac=000000000000"
                + "&wlan_ac_ip="
                + "&wlan_ac_name="
                + "&jsVersion=" + enc(JS_VERSION)
                + "&terminal_type=1"
                + "&lang=zh-cn"
                + "&v=" + (1000 + RND.nextInt(9000));

        String url = "http://" + HOST + ":" + PORT + PATH + "?" + query;
        HttpURLConnection c = connector.open(new URL(url));
        c.setConnectTimeout(12000);
        c.setReadTimeout(12000);
        c.setRequestMethod("GET");
        c.setRequestProperty("User-Agent", UA);
        c.setRequestProperty("Referer", "http://" + HOST + "/a79.htm");
        // 跟随重定向 —— 和桌面版（urllib 默认跟随）保持一致，少一个变量
        c.setInstanceFollowRedirects(true);
        try {
            int code = c.getResponseCode();
            InputStream in = (code >= 400) ? c.getErrorStream() : c.getInputStream();
            if (in == null) {
                throw new IOException("HTTP " + code);
            }
            BufferedReader r = new BufferedReader(new InputStreamReader(in, "UTF-8"));
            StringBuilder sb = new StringBuilder();
            String line;
            while ((line = r.readLine()) != null) {
                sb.append(line);
            }
            r.close();
            if (sb.length() == 0) {
                throw new IOException("服务器返回空内容 (HTTP " + code + ")");
            }
            return sb.toString();
        } finally {
            c.disconnect();
        }
    }

    /** 解析 JSONP：返回 {是否成功, 提示文字}。判定规则与桌面版一致。 */
    static String[] judge(String raw) {
        Matcher block = Pattern.compile("\\{.*\\}", Pattern.DOTALL).matcher(raw);
        if (!block.find()) {
            return new String[]{"0", "服务器返回内容无法识别"};
        }
        String json = block.group();
        String msg = "";
        Matcher m = Pattern.compile("\"msg\"\\s*:\\s*\"(.*?)\"").matcher(json);
        if (m.find()) {
            msg = m.group(1);
        }

        // 注意：不能只要含"在线"就算成功 —— "Limit Users"（在线数超限）里也有"Limit"，
        // 但更坑的是某些提示含"在线"字样却是拒绝，所以按"限制"排除一次。
        boolean ok = json.contains("\"result\":1")
                || (msg.contains("在线") && !msg.contains("Limit") && !msg.contains("限制"));

        if (ok) {
            return new String[]{"1", msg.isEmpty() ? "认证成功，可以上网了" : msg};
        }

        String friendly = human(msg);
        if (!friendly.equals(msg)) {
            friendly = friendly + "\n（服务器原话：" + msg + "）";
        }
        return new String[]{"0", friendly.isEmpty() ? "认证失败，请稍后重试" : friendly};
    }

    /** 把服务器/Radius 的原始文案翻成人话；认不出来的原样返回。 */
    static String human(String raw) {
        if (raw == null || raw.trim().isEmpty()) {
            return "服务器没有返回内容";
        }
        if (raw.contains("系统忙") || raw.contains("繁忙")
                || raw.contains("busy") || raw.contains("Busy")) {
            return "学校的认证服务器现在繁忙\n不是你账号或密码的问题\n"
                    + "过几分钟再点一次；一直这样就是学校那边的事";
        }
        if (raw.contains("Limit Users")) {
            return "账号在线设备数已达上限（学校限制最多 2 台）\n"
                    + "先在自助服务注销一台，或让另一台设备先下线";
        }
        if (raw.contains("Passwd_Err")) {
            return "账号状态被服务器拒绝（登录密码通常是没问题的）\n"
                    + "可能是密码过期、账号被冻结，或宽带绑定异常";
        }
        if (raw.contains("UserName_Err")) {
            return "绑定的宽带账号不对\n自助服务里重新绑定：账号填手机号，密码 123456";
        }
        if (raw.contains("Status_Err")) {
            return "账号状态异常（可能欠费或停机）\n先确认一下账户余额";
        }
        if (raw.contains("已经在线")) {
            return "当前设备已经在线";
        }
        return raw;
    }

    /** 服务器是不是自己忙不过来（而不是拒了这个账号）。 */
    static boolean isBusy(String raw) {
        if (raw == null) {
            return false;
        }
        return raw.contains("系统忙") || raw.contains("繁忙")
                || raw.contains("busy") || raw.contains("Busy")
                || raw.contains("\"ret_code\":3");
    }

    /** 这类失败值不值得等一会儿再试（临时状态，不是账号被拒）。 */
    static boolean retryable(String raw) {
        if (raw == null) {
            return true;                      // 没拿到响应：可能是太早了，值得再试
        }
        return raw.contains("Limit Users")
                || raw.contains("系统忙") || raw.contains("繁忙")
                || raw.contains("busy") || raw.contains("Busy")
                || raw.contains("timeout") || raw.contains("\"ret_code\":3");
    }

    private static String enc(String s) {
        try {
            return URLEncoder.encode(s == null ? "" : s, "UTF-8");
        } catch (Exception e) {
            return "";
        }
    }
}
