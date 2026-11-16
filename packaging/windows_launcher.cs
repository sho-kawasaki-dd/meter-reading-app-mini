using System;
using System.ComponentModel;
using System.Diagnostics;
using System.IO;
using System.Runtime.InteropServices;
using Microsoft.Win32.SafeHandles;

internal static class Program
{
    private const uint GenericRead = 0x80000000;
    private const uint FileShareRead = 1;
    private const uint OpenAlways = 4;
    private const uint FileAttributeNormal = 0x80;

    [DllImport("kernel32.dll", CharSet = CharSet.Unicode, SetLastError = true)]
    private static extern SafeFileHandle CreateFileW(
        string path, uint access, uint share, IntPtr security, uint creation, uint flags,
        IntPtr template);

    [DllImport("user32.dll", CharSet = CharSet.Unicode)]
    private static extern int MessageBoxW(IntPtr parent, string text, string caption, uint type);

    private static void ShowError(string message)
    {
#if MRM_TESTING
        Console.Error.WriteLine(message);
#else
        MessageBoxW(IntPtr.Zero, message, "アプリケーションを起動できません", 0x10);
#endif
    }

    private static string GuardPath()
    {
#if MRM_TESTING
        string testPath = Environment.GetEnvironmentVariable("MRM_TEST_GUARD_PATH");
        if (!String.IsNullOrEmpty(testPath))
            return testPath;
#endif
        string local = Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData);
        if (String.IsNullOrEmpty(local))
            throw new InvalidOperationException("ユーザーのローカル保存先を取得できません。");
        return Path.Combine(local, "meter-reading-mini", "installation.guard");
    }

    private static string ApplicationPath()
    {
#if MRM_TESTING
        string testPath = Environment.GetEnvironmentVariable("MRM_TEST_CHILD_PATH");
        if (!String.IsNullOrEmpty(testPath))
            return testPath;
#endif
        return Path.Combine(AppDomain.CurrentDomain.BaseDirectory, "app", "meter-reading-mini.exe");
    }

    [STAThread]
    private static int Main()
    {
        try
        {
            string path = GuardPath();
            Directory.CreateDirectory(Path.GetDirectoryName(path));
            using (SafeFileHandle guard = CreateFileW(
                path, GenericRead, FileShareRead, IntPtr.Zero, OpenAlways, FileAttributeNormal,
                IntPtr.Zero))
            {
                if (guard.IsInvalid)
                {
                    int error = Marshal.GetLastWin32Error();
                    if (error == 32 || error == 33)
                    {
                        ShowError("アプリケーションの更新または削除が進行中です。完了後に起動してください。");
                        return 2;
                    }
                    throw new Win32Exception(error, "更新・削除ガードを確保できません。");
                }

                string application = ApplicationPath();
                var start = new ProcessStartInfo
                {
                    FileName = application,
                    WorkingDirectory = Path.GetDirectoryName(application),
                    UseShellExecute = false,
                };
                using (Process child = Process.Start(start))
                {
                    if (child == null)
                        throw new InvalidOperationException("アプリケーションを起動できません。");
                    child.WaitForExit();
                    return child.ExitCode;
                }
            }
        }
        catch (Exception error)
        {
            ShowError("アプリケーションの起動に失敗しました。保存先と実行環境を確認してください。\n" +
                error.Message);
            return 1;
        }
    }
}
