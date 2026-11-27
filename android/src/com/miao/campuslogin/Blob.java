package com.miao.campuslogin;

import java.security.SecureRandom;

import javax.crypto.Cipher;
import javax.crypto.SecretKey;
import javax.crypto.spec.GCMParameterSpec;
import javax.crypto.spec.IvParameterSpec;

/**
 * 密文容器的格式层。
 *
 * 刻意**不引用任何 android.* 类**，这样整套加解密逻辑可以在电脑的 JVM 上直接跑测试，
 * 而不是只能靠"装上手机点点看"。
 *
 * 格式： [mode 1 字节] [IV] [密文(+认证标签)]
 *   mode 1 = 系统密钥库 AES/GCM（IV 由系统生成）
 *   mode 2 = 系统密钥库 AES/CBC（IV 由系统生成）
 *   mode 3 = 软件密钥 AES/GCM（IV 自己生成）
 *
 * 为什么要三种：某些深度定制系统（实测部分国产 ROM）的密钥库对"系统自动生成 IV"这条路径
 * 支持有问题，加密时抛 "Caller-provided IV not permitted"，密码一个字节都存不进去。
 * 单一方案不可靠，必须能逐级退让 —— 存得进 > 更漂亮。
 */
final class Blob {

    static final int KS_GCM = 1;
    static final int KS_CBC = 2;
    static final int SOFT_GCM = 3;

    static final int IV_GCM = 12;
    static final int IV_CBC = 16;
    static final int TAG_BITS = 128;

    private static final SecureRandom RND = new SecureRandom();

    private Blob() {
    }

    /** [mode][body] */
    static byte[] pack(int mode, byte[] body) {
        byte[] out = new byte[body.length + 1];
        out[0] = (byte) mode;
        System.arraycopy(body, 0, out, 1, body.length);
        return out;
    }

    static int modeOf(byte[] blob) {
        return (blob == null || blob.length < 1) ? 0 : (blob[0] & 0xFF);
    }

    /** 是新格式（首字节是合法 mode 且长度够）？用来和旧版无前缀格式区分。 */
    static boolean isPacked(byte[] blob) {
        int m = modeOf(blob);
        int ivLen = ivLenOf(m);
        return ivLen > 0 && blob.length >= 1 + ivLen + (TAG_BITS / 8);
    }

    static int ivLenOf(int mode) {
        if (mode == KS_GCM || mode == SOFT_GCM) {
            return IV_GCM;
        }
        if (mode == KS_CBC) {
            return IV_CBC;
        }
        return 0;
    }

    // ------------------------------------------------------------------ GCM

    /**
     * @param systemIv true = 不给 IV，让 Cipher 自己生成（系统密钥库的硬性要求）
     *                 false = 自己随机生成 IV（普通密钥用）
     * @return [IV][密文+标签]
     */
    static byte[] encryptGcm(SecretKey k, byte[] plain, boolean systemIv) throws Exception {
        Cipher c = Cipher.getInstance("AES/GCM/NoPadding");
        byte[] iv;
        if (systemIv) {
            c.init(Cipher.ENCRYPT_MODE, k);
            iv = c.getIV();
            if (iv == null || iv.length == 0) {
                throw new IllegalStateException("系统没有返回 IV");
            }
        } else {
            iv = random(IV_GCM);
            c.init(Cipher.ENCRYPT_MODE, k, new GCMParameterSpec(TAG_BITS, iv));
        }
        return join(iv, c.doFinal(plain));
    }

    static byte[] decryptGcm(SecretKey k, byte[] body) throws Exception {
        byte[] iv = slice(body, 0, IV_GCM);
        byte[] ct = slice(body, IV_GCM, body.length - IV_GCM);
        Cipher c = Cipher.getInstance("AES/GCM/NoPadding");
        c.init(Cipher.DECRYPT_MODE, k, new GCMParameterSpec(TAG_BITS, iv));
        return c.doFinal(ct);
    }

    // ------------------------------------------------------------------ CBC

    /** @return [IV][密文]；CBC 的 IV 同样由系统生成 */
    static byte[] encryptCbc(SecretKey k, byte[] plain) throws Exception {
        Cipher c = Cipher.getInstance("AES/CBC/PKCS5Padding");
        c.init(Cipher.ENCRYPT_MODE, k);
        byte[] iv = c.getIV();
        if (iv == null || iv.length == 0) {
            throw new IllegalStateException("系统没有返回 IV");
        }
        return join(iv, c.doFinal(plain));
    }

    static byte[] decryptCbc(SecretKey k, byte[] body) throws Exception {
        byte[] iv = slice(body, 0, IV_CBC);
        byte[] ct = slice(body, IV_CBC, body.length - IV_CBC);
        Cipher c = Cipher.getInstance("AES/CBC/PKCS5Padding");
        c.init(Cipher.DECRYPT_MODE, k, new IvParameterSpec(iv));
        return c.doFinal(ct);
    }

    // ------------------------------------------------------------------ 工具

    static byte[] random(int n) {
        byte[] b = new byte[n];
        RND.nextBytes(b);
        return b;
    }

    private static byte[] join(byte[] a, byte[] b) {
        byte[] out = new byte[a.length + b.length];
        System.arraycopy(a, 0, out, 0, a.length);
        System.arraycopy(b, 0, out, a.length, b.length);
        return out;
    }

    private static byte[] slice(byte[] src, int off, int len) {
        if (off < 0 || len < 0 || off + len > src.length) {
            throw new IllegalArgumentException("密文长度不合法");
        }
        byte[] out = new byte[len];
        System.arraycopy(src, off, out, 0, len);
        return out;
    }
}
