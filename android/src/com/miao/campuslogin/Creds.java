package com.miao.campuslogin;

import android.content.Context;
import android.content.SharedPreferences;
import android.provider.Settings;
import android.security.keystore.KeyGenParameterSpec;
import android.security.keystore.KeyProperties;
import android.util.Base64;

import org.json.JSONObject;

import java.security.KeyStore;
import java.security.spec.KeySpec;

import javax.crypto.KeyGenerator;
import javax.crypto.SecretKey;
import javax.crypto.SecretKeyFactory;
import javax.crypto.spec.PBEKeySpec;
import javax.crypto.spec.SecretKeySpec;

/**
 * 账号密码保存。
 *
 * 首选 Android 密钥库里的 AES 密钥（密钥由系统保管，不出安全硬件，等价于桌面版的 DPAPI）。
 *
 * 实测教训（v1.2）：某些深度定制系统（vivo）的密钥库对 GCM 的"系统自动生成 IV"
 * 支持有问题，加密直接抛 "Caller-provided IV not permitted" —— 密码一个字节都存不进去。
 * 所以这里做成**四级退让**，宁可降一点强度，也不能让功能不可用：
 *
 *   1. 密钥库 AES/GCM（系统 IV）
 *   2. 删掉旧密钥重建，再试 GCM（旧密钥可能是早期参数生成的，属性冲突）
 *   3. 密钥库 AES/CBC（有的 ROM 只是 GCM 这条路径有毛病）
 *   4. 软件密钥 AES/GCM（PBKDF2 从设备标识派生，完全不碰密钥库）
 *
 * 成功用过哪种会记下来，下次直接走那条，不再重复试错。
 */
final class Creds {

    private static final String ALIAS_GCM = "campus_login_key_gcm_v2";
    private static final String ALIAS_CBC = "campus_login_key_cbc_v2";
    private static final String ALIAS_OLD = "campus_login_key_v1";
    private static final String PREF = "campus_login";
    private static final String KEY_BLOB = "blob";
    private static final String KEY_ISP = "isp";
    private static final String KEY_MODE = "crypto_mode";
    private static final String KEY_SALT = "salt";

    private Creds() {
    }

    static final class Data {
        final String account;
        final String password;
        final String isp;

        Data(String account, String password, String isp) {
            this.account = account;
            this.password = password;
            this.isp = isp;
        }
    }

    static boolean has(Context ctx) {
        return load(ctx) != null;
    }

    // ------------------------------------------------------------------ 保存 / 读取

    static void save(Context ctx, String account, String password, String isp) throws Exception {
        JSONObject o = new JSONObject();
        o.put("a", account);
        o.put("p", password);
        byte[] plain = o.toString().getBytes("UTF-8");

        prefs(ctx).edit()
                .putString(KEY_BLOB, Base64.encodeToString(seal(ctx, plain), Base64.NO_WRAP))
                .putString(KEY_ISP, isp)
                .apply();
    }

    static Data load(Context ctx) {
        try {
            String b64 = prefs(ctx).getString(KEY_BLOB, null);
            if (b64 == null || b64.isEmpty()) {
                return null;
            }
            JSONObject o = new JSONObject(new String(
                    open(ctx, Base64.decode(b64, Base64.NO_WRAP)), "UTF-8"));
            String isp = prefs(ctx).getString(KEY_ISP, "@telecom");
            return new Data(o.optString("a"), o.optString("p"), isp);
        } catch (Throwable e) {
            return null;      // 解不开（换机/清过密钥库/恢复出厂）就当没配过，让用户重填
        }
    }

    static void clear(Context ctx) {
        prefs(ctx).edit().clear().apply();
    }

    // ------------------------------------------------------------------ 加密（四级退让）

    private static byte[] seal(Context ctx, byte[] plain) throws Exception {
        int prefer = prefs(ctx).getInt(KEY_MODE, 0);

        if (prefer != 0) {
            byte[] r = tryMode(ctx, prefer, plain);
            if (r != null) {
                return r;
            }
        }

        Exception last = null;

        // 1. 密钥库 GCM
        if (prefer != Blob.KS_GCM) {
            try {
                byte[] r = Blob.pack(Blob.KS_GCM,
                        Blob.encryptGcm(ksKey(false, false), plain, true));
                note(ctx, Blob.KS_GCM);
                return r;
            } catch (Exception e) {
                last = e;
            }
        }

        // 2. 旧密钥可能属性冲突 —— 删掉重建后再试
        try {
            byte[] r = Blob.pack(Blob.KS_GCM,
                    Blob.encryptGcm(ksKey(true, false), plain, true));
            note(ctx, Blob.KS_GCM);
            return r;
        } catch (Exception e) {
            last = e;
        }

        // 3. 密钥库 CBC
        try {
            byte[] r = Blob.pack(Blob.KS_CBC,
                    Blob.encryptCbc(ksKey(false, true), plain));
            note(ctx, Blob.KS_CBC);
            return r;
        } catch (Exception e) {
            last = e;
        }

        // 4. 软件密钥兜底 —— 一定可用
        try {
            byte[] r = Blob.pack(Blob.SOFT_GCM,
                    Blob.encryptGcm(softKey(ctx), plain, false));
            note(ctx, Blob.SOFT_GCM);
            return r;
        } catch (Exception e) {
            last = e;
        }

        throw last == null ? new IllegalStateException("没有可用的加密方式") : last;
    }

    private static byte[] open(Context ctx, byte[] blob) throws Exception {
        if (blob == null || blob.length < 2) {
            throw new IllegalArgumentException("密文为空");
        }

        if (Blob.isPacked(blob)) {
            int mode = Blob.modeOf(blob);
            byte[] body = new byte[blob.length - 1];
            System.arraycopy(blob, 1, body, 0, body.length);
            switch (mode) {
                case Blob.KS_GCM:
                    return Blob.decryptGcm(ksKey(false, false), body);
                case Blob.KS_CBC:
                    return Blob.decryptCbc(ksKey(false, true), body);
                case Blob.SOFT_GCM:
                    return Blob.decryptGcm(softKey(ctx), body);
                default:
                    throw new IllegalStateException("未知的密文格式 " + mode);
            }
        }

        // 兼容 v1.0/v1.1 的无前缀格式（整段 = IV + 密文）
        return Blob.decryptGcm(ksKey(false, false), blob);
    }

    /** 按指定模式再试一次；不适用就返回 null 交给后面的链路。 */
    private static byte[] tryMode(Context ctx, int mode, byte[] plain) {
        try {
            switch (mode) {
                case Blob.KS_GCM:
                    return Blob.pack(mode, Blob.encryptGcm(ksKey(false, false), plain, true));
                case Blob.KS_CBC:
                    return Blob.pack(mode, Blob.encryptCbc(ksKey(false, true), plain));
                case Blob.SOFT_GCM:
                    return Blob.pack(mode, Blob.encryptGcm(softKey(ctx), plain, false));
                default:
                    return null;
            }
        } catch (Throwable e) {
            return null;
        }
    }

    private static void note(Context ctx, int mode) {
        prefs(ctx).edit().putInt(KEY_MODE, mode).apply();
    }

    // ------------------------------------------------------------------ 密钥

    private static SharedPreferences prefs(Context ctx) {
        return ctx.getSharedPreferences(PREF, Context.MODE_PRIVATE);
    }

    /**
     * @param reset true = 删掉旧密钥重新生成（旧密钥属性不兼容时用）
     * @param cbc   true = 取 CBC 用的那把密钥（和 GCM 分开，参数组合才标准，
     *              避免"一把密钥同时声明两种模式"在某些机型上生成失败）
     */
    private static SecretKey ksKey(boolean reset, boolean cbc) throws Exception {
        String alias = cbc ? ALIAS_CBC : ALIAS_GCM;

        KeyStore ks = KeyStore.getInstance("AndroidKeyStore");
        ks.load(null);

        if (reset) {
            try {
                if (ks.containsAlias(alias)) {
                    ks.deleteEntry(alias);
                }
                if (ks.containsAlias(ALIAS_OLD)) {
                    ks.deleteEntry(ALIAS_OLD);
                }
            } catch (Throwable ignored) {
                // 删不掉也别中断，继续往下走
            }
        }

        if (!ks.containsAlias(alias)) {
            KeyGenerator kg = KeyGenerator.getInstance(
                    KeyProperties.KEY_ALGORITHM_AES, "AndroidKeyStore");
            KeyGenParameterSpec.Builder b = new KeyGenParameterSpec.Builder(
                    alias, KeyProperties.PURPOSE_ENCRYPT | KeyProperties.PURPOSE_DECRYPT)
                    .setRandomizedEncryptionRequired(true);   // IV 必须由系统给

            if (cbc) {
                b.setBlockModes(KeyProperties.BLOCK_MODE_CBC)
                        .setEncryptionPaddings(KeyProperties.ENCRYPTION_PADDING_PKCS7);
            } else {
                b.setBlockModes(KeyProperties.BLOCK_MODE_GCM)
                        .setEncryptionPaddings(KeyProperties.ENCRYPTION_PADDING_NONE);
            }

            kg.init(b.build());
            kg.generateKey();
        }
        return (SecretKey) ks.getKey(alias, null);
    }

    /**
     * 软件密钥：PBKDF2(包名 + ANDROID_ID, 随机盐) —— 不依赖密钥库。
     * 强度不如硬件密钥库（拿到设备文件 + 会逆向的人理论上能还原），
     * 但只在系统密钥库彻底不可用时才启用，属于"能用"兜底。
     */
    static SecretKey softKey(Context ctx) throws Exception {
        SharedPreferences p = prefs(ctx);
        String saltB64 = p.getString(KEY_SALT, null);
        byte[] salt;
        if (saltB64 == null) {
            salt = Blob.random(16);
            p.edit().putString(KEY_SALT, Base64.encodeToString(salt, Base64.NO_WRAP)).apply();
        } else {
            salt = Base64.decode(saltB64, Base64.NO_WRAP);
        }

        String id;
        try {
            id = Settings.Secure.getString(ctx.getContentResolver(), Settings.Secure.ANDROID_ID);
        } catch (Throwable e) {
            id = null;
        }
        if (id == null) {
            id = "no-android-id";
        }

        char[] pw = (ctx.getPackageName() + "|" + id).toCharArray();
        KeySpec spec = new PBEKeySpec(pw, salt, 20000, 256);
        SecretKeyFactory f = SecretKeyFactory.getInstance("PBKDF2WithHmacSHA1");
        byte[] raw = f.generateSecret(spec).getEncoded();
        ((PBEKeySpec) spec).clearPassword();
        return new SecretKeySpec(raw, "AES");
    }
}
