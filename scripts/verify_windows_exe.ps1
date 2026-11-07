#Requires -Version 7.0
param(
    [Parameter(Mandatory = $true)]
    [string]$Executable
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

Add-Type -AssemblyName UIAutomationClient
Add-Type -AssemblyName UIAutomationTypes
Add-Type -TypeDefinition @'
using System;
using System.Runtime.InteropServices;
using System.Text;

public static class MeterSmokeNative
{
    [StructLayout(LayoutKind.Sequential)]
    public struct Point { public int x; public int y; }
    private delegate bool EnumCallback(IntPtr window, IntPtr context);

    [DllImport("user32.dll")] private static extern bool EnumWindows(EnumCallback callback, IntPtr context);
    [DllImport("user32.dll")] private static extern bool IsWindowVisible(IntPtr window);
    [DllImport("user32.dll")] private static extern uint GetWindowThreadProcessId(IntPtr window, out uint processId);
    [DllImport("user32.dll", CharSet = CharSet.Unicode)] private static extern int GetWindowText(IntPtr window, StringBuilder text, int length);
    [DllImport("user32.dll", CharSet = CharSet.Unicode)] public static extern IntPtr FindWindowEx(IntPtr parent, IntPtr after, string className, string title);
    [DllImport("user32.dll")] public static extern IntPtr GetDlgItem(IntPtr window, int id);
    [DllImport("user32.dll")] public static extern IntPtr WindowFromPoint(Point point);
    [DllImport("user32.dll")] public static extern bool ScreenToClient(IntPtr window, ref Point point);
    [DllImport("user32.dll")] public static extern bool PostMessage(IntPtr window, uint message, UIntPtr wParam, IntPtr lParam);
    [DllImport("user32.dll", CharSet = CharSet.Unicode, EntryPoint = "SendMessageW")] public static extern IntPtr SetText(IntPtr window, uint message, IntPtr wParam, string text);

    public static uint ProcessId(IntPtr window)
    {
        uint id;
        GetWindowThreadProcessId(window, out id);
        return id;
    }

    public static IntPtr FindWindow(int processId, string title)
    {
        IntPtr found = IntPtr.Zero;
        EnumWindows((window, context) =>
        {
            if (ProcessId(window) != processId || !IsWindowVisible(window))
                return true;
            var text = new StringBuilder(1024);
            GetWindowText(window, text, text.Capacity);
            if (text.ToString() != title)
                return true;
            found = window;
            return false;
        }, IntPtr.Zero);
        return found;
    }
}
'@

function Wait-Window([int]$ProcessId, [string]$Title, [int]$Seconds = 30) {
    $deadline = [DateTime]::UtcNow.AddSeconds($Seconds)
    do {
        $handle = [MeterSmokeNative]::FindWindow($ProcessId, $Title)
        if ($handle -ne [IntPtr]::Zero) {
            return [System.Windows.Automation.AutomationElement]::FromHandle($handle)
        }
        if (-not (Get-Process -Id $ProcessId -ErrorAction SilentlyContinue)) {
            throw "画面「$Title」の表示前にプロセス $ProcessId が終了しました。"
        }
        Start-Sleep -Milliseconds 200
    } while ([DateTime]::UtcNow -lt $deadline)
    throw "プロセス $ProcessId の画面「$Title」を確認できませんでした。"
}

function Find-Name(
    [System.Windows.Automation.AutomationElement]$Parent,
    [string]$Name,
    [System.Windows.Automation.ControlType]$Type
) {
    $condition = [System.Windows.Automation.PropertyCondition]::new(
        [System.Windows.Automation.AutomationElement]::NameProperty, $Name
    )
    $items = $Parent.FindAll([System.Windows.Automation.TreeScope]::Descendants, $condition)
    foreach ($item in $items) {
        if ($null -eq $Type -or $item.Current.ControlType -eq $Type) {
            return $item
        }
    }
    throw "操作部品「$Name」が見つかりません。"
}

function Find-IdSuffix(
    [System.Windows.Automation.AutomationElement]$Parent,
    [string]$Suffix
) {
    $items = $Parent.FindAll(
        [System.Windows.Automation.TreeScope]::Descendants,
        [System.Windows.Automation.Condition]::TrueCondition
    )
    foreach ($item in $items) {
        if ($item.Current.AutomationId.EndsWith($Suffix, [StringComparison]::Ordinal)) {
            return $item
        }
    }
    throw "操作部品の ID「$Suffix」が見つかりません。"
}

function Send-Click(
    [IntPtr]$Window,
    [System.Windows.Automation.AutomationElement]$Element
) {
    if (-not $Element.Current.IsEnabled) {
        throw "操作部品「$($Element.Current.Name)」が無効です。"
    }
    if ([MeterSmokeNative]::ProcessId($Window) -ne $Element.Current.ProcessId) {
        throw '操作先のプロセスが一致しません。'
    }
    $rectangle = $Element.Current.BoundingRectangle
    if ($rectangle.IsEmpty) {
        throw "操作部品「$($Element.Current.Name)」の位置を取得できません。"
    }
    $point = [MeterSmokeNative+Point]::new()
    $point.x = [int]($rectangle.Left + $rectangle.Width / 2)
    $point.y = [int]($rectangle.Top + $rectangle.Height / 2)
    if (-not [MeterSmokeNative]::ScreenToClient($Window, [ref]$point)) {
        throw 'クリック位置を変換できません。'
    }
    $position = [IntPtr](($point.y -shl 16) -bor ($point.x -band 0xffff))
    if (
        -not [MeterSmokeNative]::PostMessage($Window, 0x0200, [UIntPtr]::Zero, $position) -or
        -not [MeterSmokeNative]::PostMessage($Window, 0x0201, [UIntPtr]1, $position) -or
        -not [MeterSmokeNative]::PostMessage($Window, 0x0202, [UIntPtr]::Zero, $position)
    ) {
        throw "操作部品「$($Element.Current.Name)」をクリックできません。"
    }
}

function Send-PopupClick([System.Windows.Automation.AutomationElement]$Element) {
    $rectangle = $Element.Current.BoundingRectangle
    $point = [MeterSmokeNative+Point]::new()
    $point.x = [int]($rectangle.Left + $rectangle.Width / 2)
    $point.y = [int]($rectangle.Top + $rectangle.Height / 2)
    $window = [MeterSmokeNative]::WindowFromPoint($point)
    if ($window -eq [IntPtr]::Zero) {
        throw 'ポップアップの操作先を取得できません。'
    }
    Send-Click $window $Element
}

function Open-FileAction(
    [System.Windows.Automation.AutomationElement]$Main,
    [string]$Name
) {
    $menu = Find-Name $Main 'ファイル' ([System.Windows.Automation.ControlType]::MenuItem)
    $menu.GetCurrentPattern([System.Windows.Automation.ExpandCollapsePattern]::Pattern).Expand()
    Start-Sleep -Milliseconds 150
    $action = Find-Name $Main $Name ([System.Windows.Automation.ControlType]::MenuItem)
    Send-PopupClick $action
}

function Close-Notice([System.Windows.Automation.AutomationElement]$Notice) {
    $processId = $Notice.Current.ProcessId
    $title = $Notice.Current.Name
    $button = Find-Name $Notice 'OK' ([System.Windows.Automation.ControlType]::Button)
    Send-Click ([IntPtr]$Notice.Current.NativeWindowHandle) $button
    $deadline = [DateTime]::UtcNow.AddSeconds(10)
    while (
        [MeterSmokeNative]::FindWindow($processId, $title) -ne [IntPtr]::Zero -and
        [DateTime]::UtcNow -lt $deadline
    ) {
        Start-Sleep -Milliseconds 100
    }
    if ([MeterSmokeNative]::FindWindow($processId, $title) -ne [IntPtr]::Zero) {
        throw "通知「$title」を閉じられませんでした。"
    }
}

function Save-FileDialog(
    [System.Windows.Automation.AutomationElement]$Dialog,
    [string]$Path
) {
    $hostControl = Find-IdSuffix $Dialog 'FileNameControlHost'
    $edit = [MeterSmokeNative]::FindWindowEx(
        [IntPtr]$hostControl.Current.NativeWindowHandle, [IntPtr]::Zero, 'Edit', $null
    )
    $save = [MeterSmokeNative]::GetDlgItem([IntPtr]$Dialog.Current.NativeWindowHandle, 1)
    if ($edit -eq [IntPtr]::Zero -or $save -eq [IntPtr]::Zero) {
        throw '保存ダイアログのファイル名欄または保存ボタンが見つかりません。'
    }
    if ([MeterSmokeNative]::SetText($edit, 0x000C, [IntPtr]::Zero, $Path) -eq [IntPtr]::Zero) {
        throw '保存先を設定できません。'
    }
    if (-not [MeterSmokeNative]::PostMessage($save, 0x00F5, [UIntPtr]::Zero, [IntPtr]::Zero)) {
        throw '保存ボタンを操作できません。'
    }
}

function Wait-LockProcess([string]$DataDirectory, [int]$PreviousProcessId = 0) {
    $lock = Join-Path $DataDirectory 'meter_reading.lock'
    $deadline = [DateTime]::UtcNow.AddSeconds(30)
    do {
        if (Test-Path -LiteralPath $lock) {
            try {
                $line = Get-Content -LiteralPath $lock -TotalCount 1 -ErrorAction Stop
                if ($null -ne $line) {
                    $id = 0
                    if ([int]::TryParse($line, [ref]$id) -and $id -gt 0 -and $id -ne $PreviousProcessId) {
                        return $id
                    }
                }
            }
            catch [System.IO.IOException] {
                Start-Sleep -Milliseconds 100
            }
            catch [System.Management.Automation.ItemNotFoundException] {
                Start-Sleep -Milliseconds 100
            }
        }
        Start-Sleep -Milliseconds 200
    } while ([DateTime]::UtcNow -lt $deadline)
    throw '一時保存先のロックから起動プロセスを確認できませんでした。'
}

function Check-DatabasePath([int]$ProcessId, [string]$Expected) {
    $main = Wait-Window $ProcessId '検針管理 mini'
    Open-FileAction $main 'データベースの保存先'
    $notice = Wait-Window $ProcessId 'データベースの保存先'
    $null = Find-Name $notice $Expected ([System.Windows.Automation.ControlType]::Text)
    Close-Notice $notice
}

function Check-Timezone([int]$ProcessId, [string]$Expected) {
    $main = Wait-Window $ProcessId '検針管理 mini'
    $button = Find-Name $main '設定' ([System.Windows.Automation.ControlType]::Button)
    Send-Click ([IntPtr]$main.Current.NativeWindowHandle) $button
    $dialog = Wait-Window $ProcessId '設定'
    $null = Find-Name $dialog $Expected ([System.Windows.Automation.ControlType]::Text)
    $dialog.GetCurrentPattern([System.Windows.Automation.WindowPattern]::Pattern).Close()
}

function Invoke-ProjectPython([string[]]$Arguments) {
    & uv run --locked --no-sync python @Arguments
    if ($LASTEXITCODE -ne 0) {
        throw "検証用 Python コマンドが失敗しました: 終了コード $LASTEXITCODE"
    }
}

function Register-Process([int]$ProcessId) {
    $null = $unverifiedProcesses.Add($ProcessId)
    $deadline = [DateTime]::UtcNow.AddSeconds(10)
    $observedPath = $null
    do {
        $process = Get-Process -Id $ProcessId -ErrorAction SilentlyContinue
        if ($null -eq $process) {
            $null = $unverifiedProcesses.Remove($ProcessId)
            throw "プロセス $ProcessId が確認前に終了しました。"
        }
        $observedPath = $process.Path
        if ($observedPath -eq $exePath) {
            $processes[$ProcessId] = $process.StartTime.ToUniversalTime()
            $null = $unverifiedProcesses.Remove($ProcessId)
            return
        }
        Start-Sleep -Milliseconds 100
    } while ([DateTime]::UtcNow -lt $deadline)
    throw "プロセス $ProcessId の実行ファイルを確認できませんでした: $observedPath (指定: $exePath)"
}

$projectRoot = Split-Path -Parent $PSScriptRoot
$exePath = (Resolve-Path -LiteralPath $Executable).Path
if (-not (Test-Path -LiteralPath $exePath -PathType Leaf)) {
    throw '検証する exe が見つかりません。'
}
$runDirectory = Join-Path ([IO.Path]::GetTempPath()) ("meter-exe-test-" + [guid]::NewGuid().ToString('N'))
New-Item -ItemType Directory -Path $runDirectory | Out-Null
$dataDirectory = Join-Path $runDirectory 'data'
$emptyDataDirectory = Join-Path $runDirectory 'empty-data'
$emptyTimezoneDirectory = Join-Path $runDirectory 'empty-zoneinfo'
$backupPath = Join-Path $runDirectory 'snapshot.db'
$sheetPath = Join-Path $runDirectory 'sheet.html'
$chartPath = Join-Path $runDirectory 'chart.png'
$processes = [System.Collections.Generic.Dictionary[int, DateTime]]::new()
$unverifiedProcesses = [System.Collections.Generic.HashSet[int]]::new()
$environmentNames = @('METER_READING_MINI_DATA_DIR', 'PYTHONTZPATH', 'QT_QPA_PLATFORM', 'PYTHONUTF8')
$previousEnvironment = @{}
$restorePending = $false
foreach ($name in $environmentNames) {
    $previousEnvironment[$name] = [Environment]::GetEnvironmentVariable($name, 'Process')
}

Push-Location $projectRoot
try {
    $env:PYTHONUTF8 = '1'
    New-Item -ItemType Directory -Path $emptyTimezoneDirectory | Out-Null
    $env:PYTHONTZPATH = $emptyTimezoneDirectory
    [Environment]::SetEnvironmentVariable('QT_QPA_PLATFORM', $null, 'Process')
    Invoke-ProjectPython @('scripts\create_manual_test_data.py', '--data-dir', $dataDirectory, '--days-back', '14', '--measured-percent', '100')
    $env:METER_READING_MINI_DATA_DIR = $dataDirectory
    $initial = Start-Process -FilePath $exePath -WorkingDirectory $runDirectory -PassThru
    Register-Process $initial.Id
    $processId = Wait-LockProcess $dataDirectory
    Register-Process $processId
    Check-DatabasePath $processId (Join-Path $dataDirectory 'meter_reading.db')
    Check-Timezone $processId 'Asia/Tokyo'
    Write-Output '起動・DB 保存先・Asia/Tokyo の同梱データを確認しました。'

    $duplicate = Start-Process -FilePath $exePath -WorkingDirectory $runDirectory -PassThru
    Register-Process $duplicate.Id
    $notice = Wait-Window $duplicate.Id 'アプリケーションを起動できません'
    $null = Find-Name $notice 'アプリケーションは既に起動しています。' ([System.Windows.Automation.ControlType]::Text)
    Close-Notice $notice
    if (-not $duplicate.WaitForExit(30000) -or $duplicate.ExitCode -ne 2) {
        throw '二重起動の拒否を確認できませんでした。'
    }
    Write-Output '二重起動の拒否と GUI 通知を確認しました。'

    $main = Wait-Window $processId '検針管理 mini'
    $button = Find-Name $main '使用量集計' ([System.Windows.Automation.ControlType]::Button)
    Send-Click ([IntPtr]$main.Current.NativeWindowHandle) $button
    $dashboard = Wait-Window $processId '使用量の集計'
    $combo = Find-IdSuffix $dashboard 'aggregationTarget'
    $combo.GetCurrentPattern([System.Windows.Automation.ExpandCollapsePattern]::Pattern).Expand()
    Start-Sleep -Milliseconds 150
    $items = $main.FindAll([System.Windows.Automation.TreeScope]::Descendants, [System.Windows.Automation.Condition]::TrueCondition)
    $target = $items | Where-Object {
        $_.Current.ControlType -eq [System.Windows.Automation.ControlType]::ListItem -and
        $_.Current.Name -like '*受電電力量*'
    } | Select-Object -First 1
    if ($null -eq $target) {
        throw '集計対象の受電電力量が見つかりません。'
    }
    Send-PopupClick $target
    Start-Sleep -Milliseconds 500
    $table = Find-IdSuffix $dashboard 'aggregationPeriodTable'
    $null = Find-Name $table '1.25' ([System.Windows.Automation.ControlType]::DataItem)
    $png = Find-Name $dashboard 'グラフを PNG 出力' ([System.Windows.Automation.ControlType]::Button)
    if (-not $png.Current.IsEnabled) {
        throw '数値系列の Qt Charts グラフを作成できませんでした。'
    }
    $scrollbars = $dashboard.FindAll(
        [System.Windows.Automation.TreeScope]::Descendants,
        [System.Windows.Automation.PropertyCondition]::new(
            [System.Windows.Automation.AutomationElement]::ControlTypeProperty,
            [System.Windows.Automation.ControlType]::ScrollBar
        )
    )
    $scrollbar = $scrollbars |
        Where-Object { $_.Current.BoundingRectangle.Height -gt $_.Current.BoundingRectangle.Width } |
        Sort-Object { $_.Current.BoundingRectangle.Height } -Descending |
        Select-Object -First 1
    if ($null -eq $scrollbar) {
        throw '集計画面の縦スクロールバーが見つかりません。'
    }
    $range = $scrollbar.GetCurrentPattern([System.Windows.Automation.RangeValuePattern]::Pattern)
    if ($range.Current.IsReadOnly) {
        throw '集計画面をスクロールできません。'
    }
    $range.SetValue($range.Current.Maximum)
    Start-Sleep -Milliseconds 200
    if (
        $png.Current.BoundingRectangle.Top -lt $dashboard.Current.BoundingRectangle.Top -or
        $png.Current.BoundingRectangle.Bottom -gt $dashboard.Current.BoundingRectangle.Bottom
    ) {
        throw 'PNG 出力ボタンが画面内に表示されていません。'
    }
    Send-Click ([IntPtr]$dashboard.Current.NativeWindowHandle) $png
    $save = Wait-Window $processId '表示中のグラフをPNG出力'
    Save-FileDialog $save $chartPath
    $notice = Wait-Window $processId 'PNG 出力'
    Close-Notice $notice
    $chartCheck = @'
import sys
from PySide6.QtGui import QImage

image = QImage(sys.argv[1])
if image.isNull() or image.width() < 100 or image.height() < 100:
    raise RuntimeError("グラフ画像の形式または大きさが不正です。")
colors = {
    image.pixelColor(x, y).rgba()
    for x in range(0, image.width(), 20)
    for y in range(0, image.height(), 20)
}
if len(colors) < 2:
    raise RuntimeError("グラフ画像に描画された内容を確認できません。")
'@
    Invoke-ProjectPython @('-c', $chartCheck, $chartPath)
    $dashboard.GetCurrentPattern([System.Windows.Automation.WindowPattern]::Pattern).Close()
    Write-Output '実測値の集計結果と Qt Charts の PNG 出力を確認しました。'

    $main = Wait-Window $processId '検針管理 mini'
    $button = Find-Name $main 'スマホ検針' ([System.Windows.Automation.ControlType]::Button)
    Send-Click ([IntPtr]$main.Current.NativeWindowHandle) $button
    $mobile = Wait-Window $processId 'スマホ検針'
    $button = Find-Name $mobile 'HTML シートを出力' ([System.Windows.Automation.ControlType]::Button)
    Send-Click ([IntPtr]$mobile.Current.NativeWindowHandle) $button
    $save = Wait-Window $processId '検針シートをHTML出力'
    Save-FileDialog $save $sheetPath
    $notice = Wait-Window $processId '検針シートを保存しました'
    if (-not (Test-Path -LiteralPath $sheetPath -PathType Leaf)) {
        throw 'スマホシートが出力されませんでした。'
    }
    Close-Notice $notice
    $mobile.GetCurrentPattern([System.Windows.Automation.WindowPattern]::Pattern).Close()
    $browserCheck = @'
import sys
from pathlib import Path
from playwright.sync_api import sync_playwright

errors = []
external = []
with sync_playwright() as playwright:
    browser = playwright.chromium.launch()
    page = browser.new_page(viewport={"width": 360, "height": 640})
    page.on("pageerror", lambda error: errors.append(str(error)))
    page.on("request", lambda request: external.append(request.url) if not request.url.startswith("file:") else None)
    page.goto(Path(sys.argv[1]).as_uri(), wait_until="load")
    page.locator('.tab[data-tab="all"]').click()
    if page.locator(".card").count() < 1 or errors or external:
        raise RuntimeError(f"シート検証に失敗: errors={errors}, external={external}")
    browser.close()
'@
    Invoke-ProjectPython @('-c', $browserCheck, $sheetPath)
    Write-Output 'exe が出力したシートを file: URL の Chromium で確認しました。'

    $main = Wait-Window $processId '検針管理 mini'
    Open-FileAction $main 'バックアップ・復元'
    $backup = Wait-Window $processId 'バックアップ・復元'
    $button = Find-Name $backup 'バックアップを保存' ([System.Windows.Automation.ControlType]::Button)
    Send-Click ([IntPtr]$backup.Current.NativeWindowHandle) $button
    $save = Wait-Window $processId 'バックアップを保存'
    Save-FileDialog $save $backupPath
    $notice = Wait-Window $processId 'バックアップ'
    if (-not (Test-Path -LiteralPath $backupPath -PathType Leaf)) {
        throw 'バックアップが保存されませんでした。'
    }
    Close-Notice $notice
    $marker = @'
import sys
from pathlib import Path
from meter_reading_mini.app_context import create_app_context
from meter_reading_mini.core.meter_reading_sync_repositories import StaffCreateRequest
from meter_reading_mini.db.connection import connect

connection = connect(Path(sys.argv[1]))
try:
    create_app_context(connection).staff.create_staff(StaffCreateRequest(display_name="復元前変更"))
finally:
    connection.close()
'@
    Invoke-ProjectPython @('-c', $marker, (Join-Path $dataDirectory 'meter_reading.db'))
    $source = Find-IdSuffix $backup 'restoreSource'
    $source.GetCurrentPattern([System.Windows.Automation.ValuePattern]::Pattern).SetValue($backupPath)
    $button = Find-Name $backup '復元候補を検査' ([System.Windows.Automation.ControlType]::Button)
    Send-Click ([IntPtr]$backup.Current.NativeWindowHandle) $button
    $button = Find-Name $backup '内容を確認して復元' ([System.Windows.Automation.ControlType]::Button)
    $deadline = [DateTime]::UtcNow.AddSeconds(30)
    while (-not $button.Current.IsEnabled -and [DateTime]::UtcNow -lt $deadline) {
        Start-Sleep -Milliseconds 200
    }
    if (-not $button.Current.IsEnabled) {
        throw '復元候補の検査が完了しませんでした。'
    }
    $preview = Find-IdSuffix $backup 'restorePreview'
    $previewText = $preview.GetCurrentPattern([System.Windows.Automation.ValuePattern]::Pattern).Current.Value
    if ($previewText -notmatch '元のスキーマ版: 4' -or $previewText -notmatch '担当者: 1 件') {
        throw '復元候補の内容が期待値と一致しません。'
    }
    Send-Click ([IntPtr]$backup.Current.NativeWindowHandle) $button
    $confirmation = Wait-Window $processId '復元の最終確認'
    $yes = Find-Name $confirmation 'Yes' ([System.Windows.Automation.ControlType]::Button)
    $restorePending = $true
    Send-Click ([IntPtr]$confirmation.Current.NativeWindowHandle) $yes
    $restartedId = Wait-LockProcess $dataDirectory $processId
    Register-Process $restartedId
    $restorePending = $false
    $notice = Wait-Window $restartedId '復元が完了しました'
    $noticeItems = $notice.FindAll([System.Windows.Automation.TreeScope]::Descendants, [System.Windows.Automation.Condition]::TrueCondition)
    if (-not ($noticeItems | Where-Object { $_.Current.Name -like '*保全バックアップ:*' })) {
        throw '保全バックアップの通知がありません。'
    }
    Close-Notice $notice
    Check-DatabasePath $restartedId (Join-Path $dataDirectory 'meter_reading.db')
    $preserved = @(Get-ChildItem -LiteralPath (Join-Path $dataDirectory 'backups') -Filter 'pre-restore_*.db' -File)
    if ($preserved.Count -ne 1) {
        throw '復元前の保全バックアップを特定できません。'
    }
    $checkRestore = @'
import sqlite3
import sys

for path, expected in ((sys.argv[1], 0), (sys.argv[2], 1)):
    with sqlite3.connect(path) as connection:
        count = connection.execute("SELECT COUNT(*) FROM staff WHERE name = ?", ("復元前変更",)).fetchone()[0]
        if count != expected:
            raise RuntimeError(f"復元後または保全バックアップの内容が不正: {path}")
'@
    Invoke-ProjectPython @('-c', $checkRestore, (Join-Path $dataDirectory 'meter_reading.db'), $preserved[0].FullName)
    $main = Wait-Window $restartedId '検針管理 mini'
    $main.GetCurrentPattern([System.Windows.Automation.WindowPattern]::Pattern).Close()
    Write-Output 'バックアップ・DB 置換・旧 DB 保全・exe の再起動と保存先引継ぎを確認しました。'

    $otherData = Join-Path $runDirectory 'new-york'
    $prepareTimezone = @'
import sys
from pathlib import Path
from meter_reading_mini.app_context import create_app_context
from meter_reading_mini.config import AppPaths, ensure_dirs
from meter_reading_mini.core.timeutil import local_timestamp_for_filename
from meter_reading_mini.db.migrations import open_database

data = Path(sys.argv[1])
paths = AppPaths.from_dirs(data, data / "logs")
ensure_dirs(paths)
connection = open_database(paths.db_path, paths.backup_dir, backup_timestamp=local_timestamp_for_filename)
try:
    context = create_app_context(connection)
    context.startup_preparation.prepare()
    context.timezone_settings.save_timezone("America/New_York")
finally:
    connection.close()
'@
    Invoke-ProjectPython @('-c', $prepareTimezone, $otherData)
    $env:METER_READING_MINI_DATA_DIR = $otherData
    $other = Start-Process -FilePath $exePath -WorkingDirectory $runDirectory -PassThru
    Register-Process $other.Id
    $otherId = Wait-LockProcess $otherData
    Register-Process $otherId
    Check-Timezone $otherId 'America/New_York'
    $main = Wait-Window $otherId '検針管理 mini'
    $main.GetCurrentPattern([System.Windows.Automation.WindowPattern]::Pattern).Close()
    Write-Output 'OS 標準のタイムゾーン検索先を除外して America/New_York を確認しました。'

    $env:METER_READING_MINI_DATA_DIR = $emptyDataDirectory
    $empty = Start-Process -FilePath $exePath -WorkingDirectory $runDirectory -PassThru
    Register-Process $empty.Id
    $emptyId = Wait-LockProcess $emptyDataDirectory
    Register-Process $emptyId
    Check-DatabasePath $emptyId (Join-Path $emptyDataDirectory 'meter_reading.db')
    $main = Wait-Window $emptyId '検針管理 mini'
    $main.GetCurrentPattern([System.Windows.Automation.WindowPattern]::Pattern).Close()
    $emptyProcess = Get-Process -Id $emptyId -ErrorAction SilentlyContinue
    if ($null -ne $emptyProcess -and -not $emptyProcess.WaitForExit(30000)) {
        throw '空の保存先から起動した exe が終了しませんでした。'
    }
    $schemaCheck = @'
import sqlite3
import sys
from pathlib import Path
from meter_reading_mini.db.migrations import SCHEMA_VERSION

with sqlite3.connect(Path(sys.argv[1]).as_uri() + "?mode=ro", uri=True) as connection:
    actual = connection.execute("PRAGMA user_version").fetchone()[0]
    if actual != SCHEMA_VERSION:
        raise RuntimeError(f"空の保存先から作成した DB のスキーマ版が不正: {actual}")
'@
    Invoke-ProjectPython @('-c', $schemaCheck, (Join-Path $emptyDataDirectory 'meter_reading.db'))
    Write-Output '空の保存先で exe が最新版スキーマの DB を作成したことを確認しました。'

    $invalidDataPath = Join-Path $runDirectory 'not-a-directory'
    Set-Content -LiteralPath $invalidDataPath -Value 'invalid' -NoNewline
    if (-not (Test-Path -LiteralPath $invalidDataPath -PathType Leaf)) {
        throw '不正な保存先の検証用ファイルを作成できませんでした。'
    }
    $env:METER_READING_MINI_DATA_DIR = $invalidDataPath
    $failed = Start-Process -FilePath $exePath -WorkingDirectory $runDirectory -PassThru
    Register-Process $failed.Id
    $notice = Wait-Window $failed.Id 'アプリケーションを起動できません'
    $failureMessage = 'アプリケーションの起動に失敗しました。保存先と実行環境を確認してください。'
    $null = Find-Name $notice $failureMessage ([System.Windows.Automation.ControlType]::Text)
    Close-Notice $notice
    if (-not $failed.WaitForExit(30000) -or $failed.ExitCode -ne 1) {
        throw '不正な保存先での起動失敗を確認できませんでした。'
    }
    Write-Output 'コンソールなしでの早期起動エラー通知を確認しました。'
}
catch {
    Write-Output "検証エラー: $($_.Exception.Message)"
    foreach ($directory in @($dataDirectory, (Join-Path $runDirectory 'new-york'), $emptyDataDirectory)) {
        $log = Join-Path $directory 'logs\app.log'
        if (Test-Path -LiteralPath $log -PathType Leaf) {
            Write-Output "一時データのアプリログ: $log"
            Get-Content -LiteralPath $log -Encoding utf8 -Tail 30
        }
    }
    throw
}
finally {
    $cleanupBlocked = $false
    if ($restorePending) {
        try {
            $restartedId = Wait-LockProcess $dataDirectory $processId
            Register-Process $restartedId
        }
        catch [System.Management.Automation.RuntimeException] {
            Write-Output "再起動先を確認できませんでした: $($_.Exception.Message)"
            $cleanupBlocked = $true
        }
    }
    foreach ($entry in $processes.GetEnumerator()) {
        $process = Get-Process -Id $entry.Key -ErrorAction SilentlyContinue
        if (
            $null -ne $process -and
            $process.Path -eq $exePath -and
            $process.StartTime.ToUniversalTime() -eq $entry.Value
        ) {
            try {
                Stop-Process -Id $entry.Key -ErrorAction Stop
            }
            catch [Microsoft.PowerShell.Commands.ProcessCommandException] {
                if (-not $process.HasExited) {
                    throw
                }
            }
            if (-not $process.WaitForExit(10000)) {
                throw "検証用プロセス $($entry.Key) を終了できませんでした。"
            }
        }
    }
    $unverifiedRunning = @(
        foreach ($id in $unverifiedProcesses) {
            if (Get-Process -Id $id -ErrorAction SilentlyContinue) { $id }
        }
    )
    foreach ($name in $environmentNames) {
        [Environment]::SetEnvironmentVariable($name, $previousEnvironment[$name], 'Process')
    }
    Pop-Location
    if ($cleanupBlocked -or $unverifiedRunning.Count -gt 0) {
        throw "未確認のプロセスがあるため一時データを保持しました: $runDirectory"
    }
    Remove-Item -LiteralPath $runDirectory -Recurse -Force
}
