# 校园网一键登录 v1.0.0

双击就登录。自己认 SSID、自己切 Wi-Fi、认证完再验一遍到底能不能上网。

## 下载哪个

`CampusAutologin.exe`，单文件，不用装 Python。下下来放桌面双击。

第一次跑会问学号、密码、运营商，直接回车就是中国电信。密码用 Windows 自带的
DPAPI 加密后存在同目录的 `config.dat` 里，密钥跟当前 Windows 账户绑定，
把文件拷到别的电脑、或者给别人，都解不开。

## 这版支持什么

- 宿舍 `6#SS-WiFi` 和教学楼 `FYLG-WIFI` 都认，不用手动选
- 认证成功后跑四个端点复核，发现是假的连上会自己注销重登
- 面板是自绘的毛玻璃，装了 Pillow 就是真模糊，没有就用系统 Acrylic
- 记住密码后可以命令行静默跑，不带界面

## 只在一个学校实测过

阜阳理工学院，Dr.COM eportal 老接口 `/eportal/portal/login`。用新版
`InterFace.do` 的学校得自己改，适配方法在 README 里。

## 已知问题

- 只有 Windows。安卓版源码在 `android/` 目录，要自己用 build.sh 编
- 没有 Pillow 时面板会朴素一些，功能不受影响

## 校验

同目录的 `CampusAutologin.exe.sha256` 是它的 SHA256，下载后可以核对一下。
