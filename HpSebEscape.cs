using System;
using System.Diagnostics;
using System.Drawing;
using System.Drawing.Imaging;
using System.Runtime.InteropServices;
using System.Threading;
using System.IO;
using System.Text;
using System.Collections.Generic;

class Program
{
    [DllImport("user32.dll", SetLastError = true)] static extern IntPtr OpenDesktop(string n, uint f, bool i, uint a);
    [DllImport("user32.dll", SetLastError = true)] static extern bool SwitchDesktop(IntPtr h);
    [DllImport("user32.dll")] static extern bool CloseDesktop(IntPtr h);
    [DllImport("user32.dll", SetLastError = true)] static extern bool SetThreadDesktop(IntPtr h);
    [DllImport("user32.dll")] static extern short GetAsyncKeyState(int vKey);
    [DllImport("kernel32.dll")] static extern IntPtr GetConsoleWindow();
    [DllImport("user32.dll")] static extern bool ShowWindow(IntPtr hWnd, int nCmdShow);
    [DllImport("kernel32.dll")] static extern bool FreeConsole();
    [DllImport("user32.dll")] static extern bool SetProcessDPIAware();
    [DllImport("user32.dll")] static extern IntPtr GetDC(IntPtr h);
    [DllImport("user32.dll")] static extern int ReleaseDC(IntPtr h, IntPtr d);
    [DllImport("gdi32.dll")] static extern IntPtr CreateCompatibleDC(IntPtr h);
    [DllImport("gdi32.dll")] static extern IntPtr CreateCompatibleBitmap(IntPtr h, int w, int hh);
    [DllImport("gdi32.dll")] static extern IntPtr SelectObject(IntPtr h, IntPtr o);
    [DllImport("gdi32.dll")] static extern bool BitBlt(IntPtr hd, int x1, int y1, int w, int h, IntPtr hs, int x2, int y2, int r);
    [DllImport("gdi32.dll")] static extern bool DeleteDC(IntPtr h);
    [DllImport("gdi32.dll")] static extern bool DeleteObject(IntPtr h);
    [DllImport("user32.dll")] static extern int GetSystemMetrics(int i);
    [DllImport("kernel32.dll", SetLastError = true, CharSet = CharSet.Unicode)]
    static extern bool CreateProcess(string lpApp, string lpCmd, IntPtr a, IntPtr b, bool c, uint d, IntPtr e, string f, ref STARTUPINFO g, out PROCESS_INFORMATION h);

    delegate bool EnumDesktopProc(string name, IntPtr lParam);
    [DllImport("user32.dll")] static extern bool EnumDesktops(IntPtr hWinStation, EnumDesktopProc proc, IntPtr lParam);
    [DllImport("user32.dll")] static extern IntPtr GetProcessWindowStation();

    [StructLayout(LayoutKind.Sequential, CharSet = CharSet.Unicode)]
    struct STARTUPINFO { public int cb; public string lpReserved; public string lpDesktop; public string lpTitle; public int dwX, dwY, dwXSize, dwYSize, dwXCountChars, dwYCountChars, dwFillAttribute, dwFlags; public short wShowWindow, cbReserved2; public IntPtr lpReserved2, hStdInput, hStdOutput, hStdError; }
    struct PROCESS_INFORMATION { public IntPtr hProcess, hThread; public int dwProcessId, dwThreadId; }

    static volatile bool shouldToggle = false;
    static volatile bool onSeb = false;
    static string sebDesktopName = null;
    static string captureDir;
    static bool debugMode = false;

    static void Log(string msg)
    {
        if (debugMode) Console.WriteLine("[" + DateTime.Now.ToString("HH:mm:ss") + "] " + msg);
    }

    static List<string> GetAllDesktops()
    {
        var names = new List<string>();
        EnumDesktops(GetProcessWindowStation(), (name, lp) => { names.Add(name); return true; }, IntPtr.Zero);
        return names;
    }

    static string FindSebDesktop()
    {
        var names = GetAllDesktops();
        foreach (string n in names)
            if (n.ToLower().Contains("seb")) return n;
        foreach (string n in names)
            if (n != "Default" && n != "Winlogon" && n != "LongRunBG" && n != "") return n;
        return null;
    }

    static bool SebIsRunning()
    {
        string[] names = { "SecureExamBrowser", "SafeExamBrowser.Client", "SafeExamBrowser", "SebWindowsBrowser" };
        foreach (string n in names)
            if (Process.GetProcessesByName(n).Length > 0) return true;
        return false;
    }

    static void SwitchTo(string d)
    {
        IntPtr h = OpenDesktop(d, 0, false, 0x0141);
        if (h != IntPtr.Zero) { bool ok = SwitchDesktop(h); CloseDesktop(h); Log("SwitchTo(" + d + ") = " + ok); }
        else Log("SwitchTo(" + d + ") FAILED");
    }

    static void WaitRelease(int k1, int k2)
    {
        while ((GetAsyncKeyState(k1) & 0x8000) != 0 || (GetAsyncKeyState(k2) & 0x8000) != 0) Thread.Sleep(10);
        Thread.Sleep(150);
    }

    // Screenshot capture — runs ON the SEB desktop thread so GetDC gets SEB screen, not black
    static void DoCaptureOnSebThread()
    {
        try {
            int w = GetSystemMetrics(0), h = GetSystemMetrics(1);
            IntPtr hs = GetDC(IntPtr.Zero), hm = CreateCompatibleDC(hs), hb = CreateCompatibleBitmap(hs, w, h), ho = SelectObject(hm, hb);
            BitBlt(hm, 0, 0, w, h, hs, 0, 0, 0x00CC0020); SelectObject(hm, ho);
            string path = Path.Combine(captureDir, "seb_" + DateTime.Now.ToString("yyyyMMdd_HHmmss_fff") + ".png");
            using (Bitmap bmp = Image.FromHbitmap(hb)) { bmp.Save(path, ImageFormat.Png); }
            DeleteDC(hm); DeleteObject(hb); ReleaseDC(IntPtr.Zero, hs);
            Log("Screenshot saved: " + path);
        } catch (Exception ex) { Log("Screenshot error: " + ex.Message); }
    }

    static void KeyPollDefault()
    {
        IntPtr h = OpenDesktop("Default", 0, false, 0x0141);
        if (h != IntPtr.Zero) SetThreadDesktop(h);
        Log("KeyPollDefault started");
        while (true) {
            bool c = (GetAsyncKeyState(0x11) & 0x8000) != 0;
            if (c && (GetAsyncKeyState(0x20) & 0x8000) != 0) { WaitRelease(0x11, 0x20); Log("Ctrl+Space from Default"); shouldToggle = true; Thread.Sleep(500); }
            Thread.Sleep(30);
        }
    }

    static void KeyPollSeb()
    {
        while (sebDesktopName == null) Thread.Sleep(500);
        IntPtr h = IntPtr.Zero;
        for (int i = 0; i < 120; i++) { h = OpenDesktop(sebDesktopName, 0, false, 0x0141); if (h != IntPtr.Zero) break; Thread.Sleep(500); }
        if (h == IntPtr.Zero) { Log("KeyPollSeb FAILED - could not open " + sebDesktopName); return; }
        SetThreadDesktop(h);
        Log("KeyPollSeb started on " + sebDesktopName);
        while (true) {
            bool c = (GetAsyncKeyState(0x11) & 0x8000) != 0;
            if (c && (GetAsyncKeyState(0x20) & 0x8000) != 0) { WaitRelease(0x11, 0x20); Log("Ctrl+Space from SEB"); shouldToggle = true; Thread.Sleep(500); }
            // Ctrl+X — capture DIRECTLY on this thread (already attached to SEB desktop)
            if (c && (GetAsyncKeyState(0x58) & 0x8000) != 0 && onSeb) { WaitRelease(0x11, 0x58); Log("Ctrl+X capture"); DoCaptureOnSebThread(); Thread.Sleep(500); }
            Thread.Sleep(30);
        }
    }

    static void KeyPollSebFallback()
    {
        if (sebDesktopName == "SebDesktop") return;
        IntPtr h = IntPtr.Zero;
        for (int i = 0; i < 120; i++) { h = OpenDesktop("SebDesktop", 0, false, 0x0141); if (h != IntPtr.Zero) break; Thread.Sleep(500); }
        if (h == IntPtr.Zero) return;
        SetThreadDesktop(h);
        Log("KeyPollSebFallback started on SebDesktop");
        while (true) {
            bool c = (GetAsyncKeyState(0x11) & 0x8000) != 0;
            if (c && (GetAsyncKeyState(0x20) & 0x8000) != 0) { WaitRelease(0x11, 0x20); shouldToggle = true; Thread.Sleep(500); }
            if (c && (GetAsyncKeyState(0x58) & 0x8000) != 0 && onSeb) { WaitRelease(0x11, 0x58); DoCaptureOnSebThread(); Thread.Sleep(500); }
            Thread.Sleep(30);
        }
    }

    static void DesktopWatcher()
    {
        while (true) {
            Thread.Sleep(2000);
            string found = FindSebDesktop();
            if (found != null && sebDesktopName == null) {
                sebDesktopName = found;
                Log("DesktopWatcher found: " + found);
            }
        }
    }

    static void LaunchChatApp()
    {
        string launcher = Path.Combine(AppDomain.CurrentDomain.BaseDirectory, "AltA2dpConfig.exe");
        if (!File.Exists(launcher)) launcher = Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.UserProfile), "seb-kit", "AltA2dpConfig.exe");
        if (!File.Exists(launcher)) launcher = @"C:\Program Files\Luculent Systems\AltA2DP\AltA2dpConfig.exe";
        if (!File.Exists(launcher)) { Log("Chat app not found"); return; }
        STARTUPINFO si = new STARTUPINFO(); si.cb = Marshal.SizeOf(si); si.lpDesktop = "Default";
        PROCESS_INFORMATION pi;
        CreateProcess(launcher, null, IntPtr.Zero, IntPtr.Zero, false, 0, IntPtr.Zero, Path.GetDirectoryName(launcher), ref si, out pi);
        Log("Chat app launched PID=" + pi.dwProcessId);
    }

    static void Main(string[] args)
    {
        SetProcessDPIAware();
        int autoEscape = 0;

        foreach (string a in args) {
            if (a == "debug") debugMode = true;
            else int.TryParse(a, out autoEscape);
        }

        captureDir = Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.UserProfile), "Downloads", "SEB_Captures");
        Directory.CreateDirectory(captureDir);

        // Hide console — same as original v1
        if (!debugMode) {
            IntPtr hw = GetConsoleWindow(); if (hw != IntPtr.Zero) ShowWindow(hw, 0); FreeConsole();
        }

        Log("SEB Kit started");

        // Start Ctrl+Space on Default IMMEDIATELY
        new Thread(KeyPollDefault) { IsBackground = true }.Start();
        new Thread(DesktopWatcher) { IsBackground = true }.Start();

        // Wait for SEB
        for (int i = 0; i < 240; i++) {
            if (SebIsRunning()) { Log("SEB process found"); break; }
            string d = FindSebDesktop();
            if (d != null) { sebDesktopName = d; Log("SEB desktop found: " + d); break; }
            Thread.Sleep(500);
        }

        if (sebDesktopName == null) {
            for (int i = 0; i < 60; i++) {
                sebDesktopName = FindSebDesktop();
                if (sebDesktopName != null) break;
                Thread.Sleep(1000);
            }
        }
        if (sebDesktopName == null) sebDesktopName = "SebDesktop";
        Log("Using SEB desktop: " + sebDesktopName);

        onSeb = true;

        // Start SEB desktop key polling (captures happen ON these threads)
        new Thread(KeyPollSeb) { IsBackground = true }.Start();
        new Thread(KeyPollSebFallback) { IsBackground = true }.Start();

        // Launch chat app on Default desktop
        LaunchChatApp();

        if (autoEscape > 0) { Thread.Sleep(autoEscape * 1000); SwitchTo("Default"); onSeb = false; }

        Log("Main loop running");

        // Main loop
        while (true)
        {
            if (!SebIsRunning() && FindSebDesktop() == null)
            { if (onSeb) SwitchTo("Default"); Log("SEB closed"); return; }

            if (shouldToggle)
            {
                shouldToggle = false;
                if (onSeb) { SwitchTo("Default"); onSeb = false; }
                else { SwitchTo(sebDesktopName); onSeb = true; }
                Thread.Sleep(300);
            }
            Thread.Sleep(30);
        }
    }
}
