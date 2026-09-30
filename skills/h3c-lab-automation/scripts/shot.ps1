# shot.ps1 - 把某个窗口（只截它，不截整屏）保存成 PNG，供人/模型查看。
#
# 只截目标窗口矩形，不抓整个桌面，避免带入无关内容。
#
# 用法：
#   pwsh -ExecutionPolicy Bypass -File shot.ps1 -TitleMatch SecureCRT -Out D:\DSH\NET\hcl\shot.png

param(
    [string]$TitleMatch = 'SecureCRT',
    [string]$ProcessName = '',
    [string]$Out = 'D:\DSH\NET\hcl\shot.png',
    [switch]$NoFocus
)

Add-Type -AssemblyName System.Drawing

Add-Type @"
using System;
using System.Runtime.InteropServices;
public class WinApi {
    [StructLayout(LayoutKind.Sequential)]
    public struct RECT { public int Left; public int Top; public int Right; public int Bottom; }

    [DllImport("user32.dll")]
    public static extern bool GetWindowRect(IntPtr hWnd, out RECT lpRect);

    [DllImport("user32.dll")]
    public static extern bool SetForegroundWindow(IntPtr hWnd);

    [DllImport("user32.dll")]
    public static extern bool ShowWindow(IntPtr hWnd, int nCmdShow);

    // 直接让窗口把自身内容画到我们的 DC 上：不受遮挡影响，也不会拍到桌面上别的东西
    [DllImport("user32.dll")]
    public static extern bool PrintWindow(IntPtr hWnd, IntPtr hdcBlt, uint nFlags);
}
"@

$candidates = Get-Process | Where-Object {
    $_.MainWindowHandle -ne 0 -and $_.MainWindowTitle -like "*$TitleMatch*"
}
if ($ProcessName) {
    $candidates = $candidates | Where-Object { $_.ProcessName -like "*$ProcessName*" }
}
$proc = $candidates | Select-Object -First 1

if (-not $proc) {
    Write-Error "找不到窗口（TitleMatch='$TitleMatch' ProcessName='$ProcessName'）"
    exit 1
}

$h = $proc.MainWindowHandle
if (-not $NoFocus) {
    [void][WinApi]::ShowWindow($h, 9)          # SW_RESTORE，仅防止最小化
}

$rect = New-Object WinApi+RECT
[void][WinApi]::GetWindowRect($h, [ref]$rect)
$width  = $rect.Right - $rect.Left
$height = $rect.Bottom - $rect.Top

if ($width -le 0 -or $height -le 0) {
    Write-Error "窗口尺寸异常：${width}x${height}"
    exit 1
}

$bmp = New-Object System.Drawing.Bitmap $width, $height
$gfx = [System.Drawing.Graphics]::FromImage($bmp)
$hdc = $gfx.GetHdc()
try {
    # nFlags=2 是 PW_RENDERFULLCONTENT，对带合成/GPU 渲染的窗口也有效
    $ok = [WinApi]::PrintWindow($h, $hdc, 2)
} finally {
    $gfx.ReleaseHdc($hdc)
}
$bmp.Save($Out, [System.Drawing.Imaging.ImageFormat]::Png)
$gfx.Dispose()
$bmp.Dispose()

Write-Output "已保存 $Out  (${width}x${height})  窗口标题='$($proc.MainWindowTitle)'  PrintWindow=$ok"
