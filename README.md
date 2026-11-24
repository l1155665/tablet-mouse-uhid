# 平板物理键鼠切换器 v2.3

把 Windows 电脑的物理鼠标和键盘，经 ADB 与 scrcpy 的 UHID 模式映射到已授权调试的安卓平板。不投屏、不传音频、不使用无障碍服务。

## 环境与依赖

- Windows 桌面，Python 3.12（验证版本），Python 依赖见 `requirements.txt`。
- [Android SDK Platform-Tools](https://developer.android.com/tools/releases/platform-tools) 和 [scrcpy for Windows](https://github.com/Genymobile/scrcpy/releases)。可使用 scrcpy 官方 Windows 包自带的 ADB，或单独安装 Platform-Tools。第三方工具不随本仓分发，适用各自许可证。
- 安卓平板需启用调试，并在平板上确认此电脑的 ADB 授权。USB 连接使用支持数据传输的线；无线连接需先在系统中配对，并确保设备已连接到此电脑的 ADB。

## 安装和使用

1. 下载或克隆本仓，在项目目录打开 PowerShell，运行：

   ```powershell
   python -m venv .venv
   .\.venv\Scripts\python.exe -m pip install -r requirements.txt
   ```

2. 将解压后的 scrcpy 完整目录放进项目并命名为 `scrcpy`（保留 DLL 和配套文件）。若该包没有 `adb.exe`，将 Platform-Tools 解压至项目下的 `platform-tools`，或将它加入 `PATH`。已有安装也可以通过 `PATH` 或本地 `config.json` 的 `adb_path`、`scrcpy_path` 指定。
3. 双击 `启动平板物理键鼠.vbs`，也可用同名 `.cmd`。点击 **启动服务**；到「等待切入」后按 **F9**、点击 **切入平板** 或使用屏幕边条。轻按并松开 **左 Alt** 回到电脑。点击窗口关闭按钮只会收进托盘；要结束工具，请在托盘选择 **退出**。
4. 如需共享键盘，在 **输入设置** 开启 **共享键盘**，然后选择 **实体键盘 UHID** 或 **系统输入 SDK**。UHID 初次使用可在平板的物理键盘设置中选取布局。

## 多设备与兼容性

- 支持已授权的 USB 设备和已连接的无线 ADB 设备；同时看见多台设备时可选择目标，每次只控制一台。运行中锁定目标；目标断线会释放输入，不会自动切到另一台。模拟器不作为输入目标。
- 开启 ADB 不代表设备一定支持 UHID。系统版本、厂商权限与内核均可能影响鼠标和键盘。UHID 失败时可检查平板的安全调试权限，或按需改用 SDK 模式；USB AOA 为实验模式，不支持无线。
- 隔离测试不等于完整的真机兼容矩阵。不同机型、无线延迟、多屏焦点与 DPI 行为请按实际环境验证。

## 安全与排障

- 只连接自己管理或获准操作的设备，只在可信电脑和网络上开启调试。不使用时可关闭调试或撤销调试授权。
- 打开界面只探测本机 ADB；点击启动后，确认端口空闲才会尝试唤起 ADB。不要在共用 ADB 正忙时重置或切换不同版本的服务。高级诊断里的重置功能会要求确认，并可能中断依赖同一 ADB 服务的其他应用。
- `config.json` 和日志仅保存在本地，可能含设备序列号、地址和安装路径；分享诊断前先脱敏。在项目目录运行 `.\.venv\Scripts\python.exe -m unittest -q test_switcher` 可执行隔离单元测试。

本仓代码使用 [MIT 许可证](LICENSE)。
