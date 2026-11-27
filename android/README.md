# 安卓版

和电脑版同样的功能：**自己开 Wi-Fi → 自己找已保存的校园网 → 自己认证**。

- Android 7.0（API 24）~ 14（target API 34）
- 6 个 Java 文件，约 1750 行
- **不用 Gradle**，也不依赖 AndroidX

---

## 为什么不用 Gradle

电脑上没装 Android Studio，也不想为了一个 90 KB 的 APK 拖进来几百 MB 的构建系统。

用官方命令行工具串起来就够了：

```
aapt2 compile  →  aapt2 link  →  javac  →  d8  →  zipalign  →  apksigner
   编译资源        链接资源      编译Java   转dex    4字节对齐     签名
```

整条链子写在 `build.sh` 里，跑一次就出一个可安装的 apk。

---

## 构建

### 需要准备

| 工具 | 版本 | 说明 |
|---|---|---|
| JDK | 17 | `javac` + `keytool` |
| Android build-tools | 34 | `aapt2` / `d8` / `zipalign` / `apksigner` |
| Android platform | API 34 | 提供 `android.jar` |
| Python | 3.8+ | 只用来往 apk 里塞 `classes.dex` |

### 目录约定

默认放在本目录的上一级 `_android/` 下：

```
_android/
├── jdk17/                 JDK 17
├── bt/                    build-tools 34
└── platform/android.jar   API 34
```

也可以用环境变量指定：

```bash
export ANDROID_SDK_ROOT=/path/to/your/sdk   # 其下需有 jdk17/ bt/ platform/
```

### 构建

```bash
cd android
bash build.sh
```

产物：`校园网一键登录.apk`

> **签名证书**：首次构建会自动在 `keystore/debug.jks` 生成一张自签名证书
> （口令是 Android debug 的默认值 `android`）。
> **这张证书要留着** —— 换了证书签出来的包，覆盖安装会被系统拒绝，用户只能卸载重装。
> 它已被 `.gitignore` 排除，不会进仓库。

---

## 工作原理

### 不主动扫描 Wi-Fi

这一点和很多同类工具不同：**它不做 Wi-Fi 扫描，也不强制切换网络。**

原因是实测发现：部分国产 ROM（尤其是深度定制的系统）上
`WifiManager.startScan()` 返回空列表，或者要等十几秒才有结果 —— 扫描既慢又不可靠。

改成用系统自己的机制：

```
1. 检查 Wi-Fi 开关，没开就打开
2. 用 WifiNetworkSuggestion 把校园网「建议」给系统
   （系统自己会去连，不需要定位权限、不需要扫描）
3. registerNetworkCallback 等系统通知"某个 Wi-Fi 已连上"
4. 从 NetworkCapabilities 判断它是不是 Wi-Fi，拿到 SSID
5. SSID 在校园网名单里 → 直接认证
6. 不在名单里 → 不动它，提示用户
```

好处：不需要扫描、不需要定位权限（Android 13+）、不会被 ROM 的扫描限制坑到。

### 已保存的网络怎么找

早期版本靠扫描判断"校园网在不在附近"，在部分手机上恒为空，
于是改成**直接读系统已保存的网络配置**：

```java
WifiManager.getConfiguredNetworks()   // 已保存的 SSID 列表
```

只要用户以前连过一次校园网，它就在这个列表里，不依赖扫描结果。

### 请求绑定到当前 Wi-Fi

多网卡（Wi-Fi + 移动数据同时开）时，请求走哪条路是不确定的。
所以认证请求显式绑定到当前 Wi-Fi 网络：

```java
Network network = ...;                       // 从 NetworkCallback 拿到的那个
URLConnection conn = network.openConnection(url);
```

这样即使移动数据开着，认证请求也一定从 Wi-Fi 出去 —— 否则服务器看到的来源 IP
不对，会回"账号或密码错误"这种误导性结果。

### 密码怎么存

和电脑版一样的思路，用 `AndroidKeyStore` 里的 AES 密钥加密后存 SharedPreferences。

密钥不落盘（由系统 TEE / StrongBox 保管），`Creds.java` 里对"系统自动生成 IV"
这条路径做了四级兜底 —— 因为实测部分国产 ROM 的 KeyStore 实现在这条路径上会抛
`Caller-provided IV not permitted` 或直接返回 null。

---

## 权限说明

| 权限 | 为什么需要 |
|---|---|
| `INTERNET` | 访问认证服务器 |
| `ACCESS_NETWORK_STATE` | 判断当前连的是 Wi-Fi 还是移动数据 |
| `ACCESS_WIFI_STATE` / `CHANGE_WIFI_STATE` | 开关 Wi-Fi、读当前 SSID、提交「建议网络」 |
| `NEARBY_WIFI_DEVICES`（Android 13+） | 读 Wi-Fi 名称。已标注 `neverForLocation` |
| `ACCESS_FINE_LOCATION`（≤ Android 12） | **系统历史规定**：读 Wi-Fi 名称必须给定位权限 |
| `POST_NOTIFICATIONS`（Android 13+） | 系统靠通知显示「允许连接该网络」的提示 |

> **关于定位权限**：Android 12 及以下，"读取 Wi-Fi 名称"这个动作被系统强制归到定位权限下。
> 本程序只用它识别 Wi-Fi，不读取、不上传任何位置信息，清单里已声明 `neverForLocation`。

---

## 已知限制

- **必须先手动连过一次校园网**。程序靠"已保存网络列表"找它，从没连过就找不到。
- **系统可能弹「允许连接该网络」的提示**。这是 Android 10+ 对 `WifiNetworkSuggestion`
  的安全限制，用户点一次同意即可，之后不再打扰。
- **不能用 WPA2-Enterprise 认证的校园网**。只支持开放式（无密码）无线网络。
- **不做后台常驻**。点一下跑一次，不驻留内存 —— 省电，但也就没有电脑版那种自动守护。

---

## 离线测试

`test/MockPortal.py` 是一个本地模拟门户，可以在不连校园网的情况下验证认证流程：

```bash
python test/MockPortal.py
```

然后把 `Portal.java` 里的地址临时指向 `127.0.0.1` 即可。

---

## 目录结构

```
android/
├── AndroidManifest.xml
├── build.sh                       一键构建
├── src/com/miao/campuslogin/
│   ├── MainActivity.java          界面、状态机、结果展示
│   ├── Portal.java                认证协议（拼参数、发请求、解析结果）
│   ├── Creds.java                 AndroidKeyStore 加密存储
│   ├── Blob.java                  加解密兜底实现
│   ├── LocalIp.java               取本机在 Wi-Fi 上的出口 IP
│   └── WifiAuto.java              开 Wi-Fi、建议网络、等连接、读 SSID
├── res/
│   ├── layout/                    两个界面的布局
│   ├── drawable/                  卡片、按钮、状态圆点
│   ├── values/                    颜色、样式
│   └── values-night/              深色主题
└── test/
    └── MockPortal.py              本地模拟门户
```
