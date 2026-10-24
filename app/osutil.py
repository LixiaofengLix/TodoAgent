"""与操作系统相关的操作，支持 macOS 和 Windows：目录选择框、系统通知、启动/结束进程树。"""
import os
import shutil
import signal
import subprocess
import sys

IS_MAC = sys.platform == "darwin"
IS_WIN = os.name == "nt"


class Cancelled(Exception):
    pass


# ---------- 目录选择框 ----------

# argv[1] 为可选的初始目录；在最前面的应用（通常是浏览器）中弹出
_MAC_PICK = """
on run argv
    if (count of argv) > 0 then set loc to (POSIX file (item 1 of argv)) as alias
    tell application (path to frontmost application as text)
        activate
        if (count of argv) > 0 then
            set p to choose folder with prompt "选择工作目录" default location loc
        else
            set p to choose folder with prompt "选择工作目录"
        end if
    end tell
    return POSIX path of p
end run
"""

# 初始目录经环境变量 PICK_START 传入；置顶的隐藏窗口作为父窗口，让对话框出现在最前面
_WIN_PICK = """
[Console]::OutputEncoding = [Text.Encoding]::UTF8
Add-Type -AssemblyName System.Windows.Forms
$d = New-Object System.Windows.Forms.FolderBrowserDialog
$d.Description = '选择工作目录'
$d.ShowNewFolderButton = $true
if ($env:PICK_START) { $d.SelectedPath = $env:PICK_START }
$owner = New-Object System.Windows.Forms.Form -Property @{TopMost = $true; ShowInTaskbar = $false}
if ($d.ShowDialog($owner) -eq [System.Windows.Forms.DialogResult]::OK) { $d.SelectedPath }
$owner.Dispose()
"""


def pick_dir(start: str = "", timeout: int = 600) -> str:
    """在本机弹出系统目录选择框，返回所选绝对路径；取消或超时抛 Cancelled，不支持的系统抛 NotImplementedError。"""
    start = start if start and os.path.isdir(start) else ""
    try:
        if IS_MAC:
            p = subprocess.run(["osascript", "-e", _MAC_PICK, *([start] if start else [])],
                               capture_output=True, text=True, timeout=timeout)
            if p.returncode != 0:
                if "-128" in p.stderr:
                    raise Cancelled()
                raise RuntimeError(p.stderr.strip())
            return p.stdout.strip().rstrip("/") or "/"
        if IS_WIN:
            p = _powershell(_WIN_PICK, {"PICK_START": start}, timeout)
            if p.returncode != 0:
                raise RuntimeError(p.stderr.strip())
            path = p.stdout.strip()
            if not path:
                raise Cancelled()
            return path
    except subprocess.TimeoutExpired:
        raise Cancelled()
    raise NotImplementedError("directory picker is only supported on macOS and Windows")


# ---------- 系统通知 ----------

# argv: 标题 正文
_MAC_NOTIFY = 'on run argv\ndisplay notification (item 2 of argv) with title (item 1 of argv) sound name "Glass"\nend run'

# 标题、正文经环境变量传入；借用 PowerShell 的 AppID 发 Toast 通知（需 Windows PowerShell 5.1，Win10/11 自带）
_WIN_NOTIFY = """
$null = [Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType = WindowsRuntime]
$xml = [Windows.UI.Notifications.ToastNotificationManager]::GetTemplateContent([Windows.UI.Notifications.ToastTemplateType]::ToastText02)
$t = $xml.GetElementsByTagName('text')
$null = $t.Item(0).AppendChild($xml.CreateTextNode($env:NOTIFY_TITLE))
$null = $t.Item(1).AppendChild($xml.CreateTextNode($env:NOTIFY_BODY))
$app = '{1AC14E77-02E7-4E5D-B744-2EB1AE5198B7}\\WindowsPowerShell\\v1.0\\powershell.exe'
[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier($app).Show([Windows.UI.Notifications.ToastNotification]::new($xml))
"""


def notify(title: str, body: str) -> None:
    """弹出系统通知；失败抛异常，不支持的系统静默跳过。"""
    if IS_MAC:
        p = subprocess.run(["osascript", "-e", _MAC_NOTIFY, title, body], capture_output=True, text=True, timeout=10)
    elif IS_WIN:
        p = _powershell(_WIN_NOTIFY, {"NOTIFY_TITLE": title, "NOTIFY_BODY": body}, 30)
    else:
        return
    if p.returncode != 0:
        raise RuntimeError(p.stderr.strip())


def _powershell(script: str, env: dict, timeout: int) -> subprocess.CompletedProcess:
    # 必须用 Windows PowerShell（powershell.exe）：pwsh 7 不支持 WinRT 通知
    return subprocess.run(
        ["powershell", "-NoProfile", "-NonInteractive", "-STA", "-ExecutionPolicy", "Bypass", "-Command", script],
        env={**os.environ, **env}, capture_output=True, encoding="utf-8", errors="replace", timeout=timeout,
        creationflags=subprocess.CREATE_NO_WINDOW,
    )


# ---------- 进程树 ----------

def popen_tree(cmd: list, **kwargs) -> subprocess.Popen:
    """启动进程并放入独立进程组，之后可用 kill_tree 连同它启动的所有子进程一起结束。"""
    # Windows 下 Popen 不会按 PATHEXT 查找 .cmd / .exe，先解析成完整路径
    cmd = [shutil.which(cmd[0]) or cmd[0], *cmd[1:]]
    if IS_WIN:
        return subprocess.Popen(cmd, creationflags=subprocess.CREATE_NEW_PROCESS_GROUP, **kwargs)
    return subprocess.Popen(cmd, start_new_session=True, **kwargs)


def kill_tree(p: subprocess.Popen) -> None:
    """强制结束 popen_tree 启动的进程及其所有子进程。"""
    if IS_WIN:
        subprocess.run(["taskkill", "/F", "/T", "/PID", str(p.pid)], capture_output=True,
                       creationflags=subprocess.CREATE_NO_WINDOW)
        if p.poll() is None:
            p.kill()
        return
    try:
        os.killpg(p.pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        pass
