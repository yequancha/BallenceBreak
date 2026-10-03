# 久坐休息提醒（Break Reminder）

Windows 个人工具：设置一个工作倒计时，到点弹全屏提醒，提醒你站起来休息、活动身体。

## 功能

- **工作倒计时**：设置分钟数 → 点「开始倒计时」→ 实时显示剩余时间
- **全屏提醒**：倒计时结束弹出全屏界面，只能点击「休息完成」按钮关闭（Esc / Alt+F4 无效，强化仪式感）
- **喝水提醒**（与休息提醒完全独立，默认开启）：**两种提醒方式可切换**（主窗口「喝水提醒设置…」）：
  ① **按饮水量推导**（默认）：按「每次饮水量 + 每日饮水量 + 工作时间段」自动推导提醒频率；
  ② **按指定时间**：直接设置每天的提醒时刻（如 `10:00`、`15:00`，可多行增删），到点就提醒，与饮水量／时段无关；
  到点在屏幕左上角弹出持久气泡（默认点击「好的，喝一口」才关闭；可在设置里勾选「喝水提示框自动消失」并设定秒数，到时自动关闭）；
  全屏休息遮罩显示时水气泡照弹并悬浮其上，同一时刻至多一个气泡（防堆积，人离开只挂一个等回来点）。
  主窗口「喝水提醒设置…」可切换提醒方式、改每次/每日饮水量、时段、提醒时刻、文案、自动消失；
  主窗口实时显示倒计时（推导模式「距下次喝水 mm:ss」／时间模式「距 15:00 喝水还有 mm:ss」；到点或气泡未点显示红色催促）。
- **窗口置顶**：未开始时主窗口置顶（可开关，默认开启，提醒你别忘记开始；倒计时中自动取消置顶，不挡工作）
- **窗口自适应**（换电脑不再"界面显示不全"）：界面尺寸不再写死，而是按控件「自然尺寸」自动贴合主屏，
  自动适配 100% / 125% / 150% 等显示缩放、任意分辨率与系统字体差异；内容超出屏幕时窗口自动收缩并出现
  滚动条（支持鼠标滚轮），保证「开始倒计时」「喝水提醒设置…」「休息完成」等按钮在任何机器上都能看到、点到；
  主窗口可自由缩放，且不会被拖出屏幕；全屏提醒的字号与位置也按主屏分辨率自动换算。
- **系统托盘**：最小化或关闭窗口即缩到托盘；左键单击显示主窗口，右键菜单有「显示窗口 ／ 退出」
- **单实例**：同一时刻只运行一个程序。再次启动 / 双击 exe / 点 run.bat 时不会开第二个窗口，
  而是自动唤醒已有实例并恢复界面显示（即使它已缩到托盘），新启动的进程随即退出
- **登录提醒**：开机登录自启拉起时若工作倒计时尚未开始，自动显示主界面并托盘冒泡提示
  「倒计时未开始」；锁屏后重新登录（解锁）或任意账号（快速切换）登录时若倒计时尚未开始，
  仅显示置顶主界面（不弹托盘冒泡）——提醒你回来继续开工
- **空闲检测**：鼠标键盘超过 15 分钟无操作 → 询问「是否重新开始本轮倒计时」（弹窗不暂停计时；若倒计时已结束，自动关闭弹窗回到未开始，不再打扰）
- **配置记忆**：分钟数、置顶开关、喝水提醒参数自动保存到 `config.json`，下次启动自动恢复

## 运行

### 方式一：双击 `久坐休息提醒.exe`（推荐）
无黑框、带图标的程序。它会自动用本机 Python 的 `pythonw.exe`（无窗口）在后台启动
`break_reminder.py`。原理见下方「技术说明」。

> 注意：exe 是"启动器"，它仍需本机装有 Python 3.8+。若想要"任何电脑免装 Python 都能跑"
> 的独立 exe，需用 PyInstaller 打包（见文末「完整打包成独立 exe」）。

### 方式二：双击 `run.bat`
优先启动 exe；若 exe 缺失则自动检查 Python、安装缺失的 Pillow，然后运行 py。

### 方式三：命令行

```bash
pip install Pillow     # 托盘图标需要；Tkinter 为 Python 自带
python break_reminder.py
```

要求 Python 3.8+（Windows 10）。

## 技术说明

- UI 使用 Python 内置 **Tkinter**，无额外依赖。
- **界面自适应**（DPI / 分辨率）：程序是 DPI 感知的，Tk 的「点」字号会随系统 DPI 自动放大
  （96 DPI → 1.333 px/pt，120 DPI(125%) → 1.667 px/pt），所以**写死窗口像素尺寸必然在别的电脑上裁切**。
  现在统一改为：`fit_window_to_content()` 按控件自然尺寸计算窗口大小并夹紧到主屏工作区，
  `ScrollBox`（Canvas + 按需显隐的滚动条 + 滚轮）兜底，`dpi_px()` 换算像素类参数，
  全屏提醒按主屏实际分辨率与 DPI 推算字号和坐标。换台电脑/改缩放都不用再改代码。
- 系统托盘使用 **Windows 原生 Shell_NotifyIcon API**（ctypes 直接调用），不依赖 pystray。
- 空闲检测使用 Windows `GetLastInputInfo`。
- 唯一第三方依赖 **Pillow** 仅用于生成托盘图标；若想完全离线，可把图标换成 `.ico` 文件后移除该依赖。
- 托盘图标修复：生成 HICON 时同时提供**掩码位图（hbmMask）**，避免旧版 Shell 把带透明通道的图标识别成全透明导致"看不到"。
- `久坐休息提醒.exe` 是一个仅依赖 `KERNEL32 / USER32` 的极简启动器（MinGW `-nostdlib` 编译，
  零运行库依赖），用来免黑框地调用 `pythonw.exe` 启动主程序。

## 配置文件

运行后同目录生成 `config.json`：

```json
{
  "minutes": 50,
  "topmost": true,
  "auto_min": true,
  "message": "",
  "delay_minutes": 5,
  "restart_on_done": false,
  "autostart": true,
  "water_enabled": true,
  "water_target": 1500,
  "water_sip": 50,
  "water_windows": [["08:30", "12:30"], ["13:30", "17:30"]],
  "water_mode": "derive",
  "water_times": ["10:00", "15:00"],
  "water_message": "",
  "water_autoclose": false,
  "water_autoclose_seconds": 10
}
```

- `minutes` / `topmost` / `auto_min` / `message` / `delay_minutes` / `restart_on_done` / `autostart`：休息提醒配置。
- `water_*`：喝水提醒配置。
  - `water_mode`：提醒方式，`"derive"`（默认，按饮水量推导）或 `"time"`（按指定时间）。
  - `water_times`：时间模式下的提醒时刻 `["10:00", "15:00"]`（每天循环，可多行；删空 = 不提醒）。
  - `water_sip`（每次饮水量 ml，10–300）、`water_target`（每日饮水量 ml，500–4000）、
    `water_windows`（工作时间段，可多段）、`water_message`（提醒文案，空=默认）、
    `water_autoclose`（提示框自动消失，默认 false）、`water_autoclose_seconds`（自动消失秒数，3–120，默认 10）。
  - 缺字段一律走默认，旧配置自动升级（没有 `water_mode` 的旧配置＝按饮水量推导）。

删掉该文件即可恢复默认

## 完整打包成独立 exe（可选，需联网）

上面那个 `久坐休息提醒.exe` 是"启动器"，仍依赖本机 Python。
如果要一个 **不装 Python、拷到任何电脑都能跑** 的独立 exe（把解释器、Tk、Pillow 都打进去），
在有网时运行 PyInstaller：

```bash
pip install pyinstaller
pyinstaller --onefile --noconsole --icon icon.ico --name 久坐休息提醒 break_reminder.py
```

生成的 exe 在 `dist/` 下，约 15~30 MB。以后重新打包时，把 `break_reminder.py` 里
`_make_hicon`/图标相关代码可直接复用同一份 `icon.ico`。

## 重新编译启动器 exe（可选）

`launcher/` 目录内已包含源码；若改了 exe 想重新编译：

```bash
windres icon.rc -O coff -o icon.res
gcc launcher.c icon.res -o ..\久坐休息提醒.exe -mwindows -O2 -s -nostdlib -luser32 -lkernel32
```

## 许可证

本项目基于 **MIT License** 开源，详见 [LICENSE](LICENSE)。

```
MIT License
Copyright (c) 2026 yequancha
```

可自由使用、修改、分发（含商业用途），只需保留版权声明与许可声明。
