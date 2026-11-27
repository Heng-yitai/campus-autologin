#!/usr/bin/env bash
# 不用 Gradle 的最小构建：aapt2 编译资源 → javac 编译 → d8 转 dex
# → 塞进 apk → zipalign 对齐 → apksigner 签名
#
# 依赖（均为可下载的官方工具，需自行准备）：
#   jdk17/          JDK 17
#   bt/             Android build-tools 34（aapt2 / d8 / zipalign / apksigner）
#   platform/       对应平台（含 android.jar）
#
# 三个目录默认放在本脚本的上一级 `_android/` 下，也可以用环境变量覆盖：
#   ANDROID_SDK_ROOT=/path/to/sdk   （其下需有 jdk17/ bt/ platform/）
set -e
cd "$(dirname "$0")"

ROOT="${ANDROID_SDK_ROOT:-$PWD/../_android}"
JDK="$ROOT/jdk17"
BT="$ROOT/bt"
PLATFORM="$ROOT/platform/android.jar"
PY="${PYTHON:-python}"

export JAVA_HOME="$JDK"
export PATH="$JDK/bin:$PATH"

APP_NAME="校园网一键登录"

echo "== 1/6 清理旧产物 =="
# 旧产物移到带时间戳的目录而不是删除，方便对比两次构建的差异
if [ -d build ]; then mv build "build_old_$(date +%s)"; fi
mkdir -p build/gen build/classes build/dex

echo "== 2/6 编译资源 =="
"$BT/aapt2.exe" compile --dir res -o build/res.zip

echo "== 3/6 链接资源与清单 =="
# 注意：编译后的资源包要作为**位置参数**传入；
# 用 -R 是声明 overlay（资源叠加包），会报 "does not override an existing resource"
"$BT/aapt2.exe" link \
  -o build/base.apk \
  -I "$PLATFORM" \
  --manifest AndroidManifest.xml \
  build/res.zip \
  --java build/gen \
  --min-sdk-version 24 \
  --target-sdk-version 34 \
  --version-code 12 \
  --version-name 2.1

echo "== 4/6 编译 Java =="
# 不要加 -bootclasspath：android.jar 里 java.lang.invoke 是空壳，
# 用 lambda 会报 "找不到方法 metafactory"。让 java.* 走 JDK、安卓类走 classpath 即可。
"$JDK/bin/javac.exe" -encoding UTF-8 -source 8 -target 8 -nowarn \
  -classpath "$PLATFORM" \
  -d build/classes \
  $(find src -name '*.java') \
  build/gen/com/miao/campuslogin/R.java

echo "== 5/6 转 dex 并装进 apk =="
"$BT/d8.bat" --lib "$PLATFORM" --min-api 24 --output build/dex \
  $(find build/classes -name '*.class')

"$PY" - <<'PYEOF'
import zipfile, os
src, dex, out = 'build/base.apk', 'build/dex/classes.dex', 'build/unsigned.apk'
assert os.path.exists(dex), 'd8 没产出 classes.dex'
with zipfile.ZipFile(src) as zin, zipfile.ZipFile(out, 'w', zipfile.ZIP_DEFLATED) as zout:
    for it in zin.infolist():
        zout.writestr(it, zin.read(it.filename))
    zout.write(dex, 'classes.dex')
print('  已写入 classes.dex，大小 %.1f KB' % (os.path.getsize(dex) / 1024))
PYEOF

echo "== 6/6 对齐 + 签名 =="
"$BT/zipalign.exe" -f 4 build/unsigned.apk build/aligned.apk

# 签名证书必须固定（放在 keystore/，不能放 build/ 里）：
# 换证书签出来的包覆盖安装会被系统拒绝，用户只能卸载重装。
# 注意：路径要保持**相对形式** —— apksigner.bat 是 Windows 批处理，
# 喂给它 Git Bash 的 /d/... 绝对路径会报 "loadPrivateKeyAndCerts" 之类的错。
KEYSTORE="keystore/debug.jks"
mkdir -p keystore
if [ ! -f "$KEYSTORE" ]; then
  "$JDK/bin/keytool.exe" -genkeypair -v \
    -keystore "$KEYSTORE" -alias androiddebugkey \
    -storepass android -keypass android \
    -keyalg RSA -keysize 2048 -validity 10000 \
    -dname "CN=Android Debug,O=Android,C=US" > /dev/null 2>&1
  echo "  已新建签名证书（keystore/debug.jks）—— 别删，删了以后更新就得卸载重装"
fi

"$BT/apksigner.bat" sign \
  --ks "$KEYSTORE" \
  --ks-pass pass:android \
  --key-pass pass:android \
  --out "${APP_NAME}.apk" \
  build/aligned.apk

"$BT/apksigner.bat" verify --print-certs "${APP_NAME}.apk" | head -4

echo
echo "构建完成: $(pwd)/${APP_NAME}.apk"
ls -la "${APP_NAME}.apk" | awk '{printf "  大小: %.2f MB\n", $5/1024/1024}'
