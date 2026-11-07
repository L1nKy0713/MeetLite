// MeetLite Windows 外壳：起 Python 后端 → 等端口就绪 → WebView2 加载界面。
// 关窗最小化到托盘；退出时用 Job Object 一次收掉 python 与它拉起的子进程。
using System.Diagnostics;
using System.Drawing;
using System.Net;
using System.Net.Http;
using System.Net.Sockets;
using System.Runtime.InteropServices;
using System.Text;
using Microsoft.Web.WebView2.Core;
using Microsoft.Web.WebView2.WinForms;

namespace MeetLite.Win;

internal static class Program
{
    [DllImport("shcore.dll")]
    private static extern int SetProcessDpiAwareness(int value);

    [STAThread]
    private static void Main()
    {
        // net48 没有 ApplicationConfiguration.Initialize()，等价写法：
        Application.EnableVisualStyles();
        Application.SetCompatibleTextRenderingDefault(false);
        try { SetProcessDpiAwareness(2); } catch { }   // PerMonitorV2
        using var shell = new Shell();
        Application.Run(shell);
    }
}

internal sealed class Shell : ApplicationContext
{
    private readonly Form _win = new();
    private readonly WebView2 _web = new();
    private readonly NotifyIcon _tray;
    private readonly Label _splash = new();
    private readonly StringBuilder _log = new();
    private readonly string _dataDir;

    private Process? _server;
    private IntPtr _job = IntPtr.Zero;
    private int _port;
    private bool _webReady;
    private bool _serverReady;
    private bool _useBrowser;      // 缺 WebView2 时退化为系统浏览器
    private bool _browserOpened;
    private bool _exiting;

    public Shell()
    {
        _dataDir = Path.Combine(
            Environment.GetFolderPath(Environment.SpecialFolder.ApplicationData), "MeetLite");
        Directory.CreateDirectory(_dataDir);

        _win.Text = "MeetLite";
        _win.Width = 1220;
        _win.Height = 800;
        _win.MinimumSize = new Size(880, 540);
        _win.StartPosition = FormStartPosition.CenterScreen;
        _win.BackColor = Color.FromArgb(247, 246, 243);
        _win.Icon = LoadIcon();
        _win.FormClosing += OnClosing;
        _win.Resize += (_, _) => { if (_win.WindowState == FormWindowState.Minimized) HideToTray(); };

        _splash.Dock = DockStyle.Fill;
        _splash.TextAlign = ContentAlignment.MiddleCenter;
        _splash.ForeColor = Color.FromArgb(90, 90, 90);
        _splash.BackColor = Color.FromArgb(247, 246, 243);
        _splash.Font = new Font("Microsoft YaHei UI", 11f);
        _splash.Text = "正在启动转写与摘要服务…";
        _splash.Cursor = Cursors.Hand;
        _splash.Click += (_, _) => { if (_useBrowser && _serverReady) OpenExternal($"http://127.0.0.1:{_port}/"); };
        _win.Controls.Add(_splash);

        _web.Dock = DockStyle.Fill;
        _web.Visible = false;
        _web.DefaultBackgroundColor = Color.FromArgb(247, 246, 243);
        _win.Controls.Add(_web);

        _tray = new NotifyIcon
        {
            Icon = _win.Icon ?? SystemIcons.Application,
            Text = "MeetLite",
            Visible = true,
            ContextMenuStrip = BuildTrayMenu(),
        };
        _tray.DoubleClick += (_, _) => ShowWindow();

        _win.Shown += async (_, _) => await InitWebView();
        _ = Boot();
    }

    private ContextMenuStrip BuildTrayMenu()
    {
        var m = new ContextMenuStrip();
        m.Items.Add("显示主窗口", null, (_, _) => ShowWindow());
        m.Items.Add("打开数据目录", null, (_, _) =>
            Process.Start(new ProcessStartInfo("explorer.exe", _dataDir) { UseShellExecute = true }));
        m.Items.Add(new ToolStripSeparator());
        m.Items.Add("退出 MeetLite", null, (_, _) => Quit());
        return m;
    }

    private static Icon? LoadIcon()
    {
        try
        {
            var p = Path.Combine(AppContext.BaseDirectory, "meetlite.ico");
            return File.Exists(p) ? new Icon(p) : null;
        }
        catch { return null; }
    }

    // ---------------------------------------------------------------- WebView2

    private async Task InitWebView()
    {
        try
        {
            var env = await CoreWebView2Environment.CreateAsync(
                null, Path.Combine(_dataDir, "WebView2"));
            await _web.EnsureCoreWebView2Async(env);
            if (_web.CoreWebView2 is null) { Fail("WebView2 初始化失败（CoreWebView2 为空）"); return; }

            var s = _web.CoreWebView2.Settings;
            s.AreDefaultContextMenusEnabled = false;
            s.IsStatusBarEnabled = false;
            s.AreDevToolsEnabled = true;
            s.IsZoomControlEnabled = false;
            _web.CoreWebView2.NewWindowRequested += (_, ev) =>
            {
                ev.Handled = true;
                OpenExternal(ev.Uri);
            };
            _web.CoreWebView2.DocumentTitleChanged += (_, _) =>
                _win.Text = string.IsNullOrWhiteSpace(_web.CoreWebView2.DocumentTitle)
                    ? "MeetLite" : _web.CoreWebView2.DocumentTitle;

            _webReady = true;
            TryShowUI();
        }
        catch (Exception ex)
        {
            // 缺 WebView2 运行库是常见情况（精简版 Windows、未装 Edge）
            // 不直接失败，退化成「系统浏览器 + 托盘常驻」，界面功能完全一致
            Append("[webview] " + ex.Message);
            _useBrowser = true;
            Append("[webview] 回退到系统浏览器模式");
            UiInvoke(() =>
            {
                _splash.Text = "未找到 WebView2 运行库，正在用系统浏览器打开界面…";
                _web.Visible = false;
                _splash.Visible = true;
            });
            TryShowUI();
        }
    }

    private void TryShowUI()
    {
        if (_exiting || !_serverReady) return;
        var url = $"http://127.0.0.1:{_port}/";

        if (_useBrowser)
        {
            if (_browserOpened) return;
            _browserOpened = true;
            OpenExternal(url);
            _win.Hide();
            try
            {
                _tray.ShowBalloonTip(4000, "MeetLite 已在浏览器中打开",
                    "缺少 WebView2 运行库，已改用系统浏览器。装好运行时后重开即是内置窗口。",
                    ToolTipIcon.Info);
            }
            catch { }
            return;
        }

        if (!_webReady) return;
        _splash.Visible = false;
        _web.Visible = true;
        _web.Source = new Uri(url);
    }

    // ---------------------------------------------------------------- 后端

    private async Task Boot()
    {
        try
        {
            var appDir = Path.Combine(AppContext.BaseDirectory, "app");
            var entry = Path.Combine(appDir, "server.py");
            if (!File.Exists(entry)) { Fail("安装不完整：缺少 app\\server.py"); return; }

            var py = FindPython();
            if (py is null) { Fail("找不到 Python 运行时（应随包附带 runtime\\python.exe）"); return; }

            _port = FreePort();
            CreateKillOnCloseJob();

            var psi = new ProcessStartInfo(py)
            {
                WorkingDirectory = appDir,
                UseShellExecute = false,
                CreateNoWindow = true,
                RedirectStandardInput = true,
                RedirectStandardOutput = true,
                RedirectStandardError = true,
                StandardOutputEncoding = Encoding.UTF8,
                StandardErrorEncoding = Encoding.UTF8,
            };
            psi.Arguments = $"-u server.py --port {_port}";
            psi.Environment["PYTHONIOENCODING"] = "utf-8";
            psi.Environment["PYTHONUTF8"] = "1";

            _server = new Process { StartInfo = psi, EnableRaisingEvents = true };
            _server.OutputDataReceived += (_, e) => Append(e.Data);
            _server.ErrorDataReceived += (_, e) => Append(e.Data);
            _server.Exited += (_, _) =>
            {
                if (_exiting) return;
                UiInvoke(() => Fail("服务进程意外退出。日志：" + LogPath + Environment.NewLine + Tail()));
            };
            _server.Start();
            _server.BeginOutputReadLine();
            _server.BeginErrorReadLine();
            AssignToJob(_server);

            if (!await WaitReady(TimeSpan.FromSeconds(150)))
            {
                Fail("服务启动超时。日志：" + LogPath + Environment.NewLine + Tail());
                return;
            }
            Append("[ui] 服务就绪 http://127.0.0.1:" + _port);
            _serverReady = true;
            UiInvoke(TryShowUI);
        }
        catch (Exception ex)
        {
            Fail("启动失败：" + ex.Message);
        }
    }

    private string? FindPython()
    {
        var candidates = new List<string>
        {
            Path.Combine(AppContext.BaseDirectory, "runtime", "python.exe"),
            Path.Combine(AppContext.BaseDirectory, "runtime", "pythonw.exe"),
        };
        foreach (var dir in (Environment.GetEnvironmentVariable("PATH") ?? "").Split(';'))
        {
            if (string.IsNullOrWhiteSpace(dir)) continue;
            candidates.Add(Path.Combine(dir, "python.exe"));
            candidates.Add(Path.Combine(dir, "py.exe"));
        }
        return candidates.FirstOrDefault(File.Exists);
    }

    private static int FreePort()
    {
        var l = new TcpListener(IPAddress.Loopback, 0);
        l.Start();
        var port = ((IPEndPoint)l.LocalEndpoint).Port;
        l.Stop();
        return port;
    }

    private async Task<bool> WaitReady(TimeSpan limit)
    {
        using var http = new HttpClient { Timeout = TimeSpan.FromSeconds(4) };
        var url = $"http://127.0.0.1:{_port}/api/system";
        var sw = Stopwatch.StartNew();
        while (sw.Elapsed < limit)
        {
            if (_server is { HasExited: true }) return false;
            try
            {
                var txt = await http.GetStringAsync(url);
                if (txt.Contains("\"ok\": true") || txt.Contains("\"ok\":true")) return true;
            }
            catch { /* 还没起来 */ }
            await Task.Delay(300);
        }
        return false;
    }

    // ---------------------------------------------------------------- 托盘 / 退出

    private void ShowWindow()
    {
        _win.Show();
        if (_win.WindowState == FormWindowState.Minimized) _win.WindowState = FormWindowState.Normal;
        _win.Activate();
    }

    private void HideToTray()
    {
        _win.Hide();
        _tray.ShowBalloonTip(1200, "MeetLite 仍在运行",
            "录音与转写在后台继续，双击托盘图标回到界面。", ToolTipIcon.None);
    }

    private void OnClosing(object? sender, FormClosingEventArgs e)
    {
        if (_exiting) return;
        e.Cancel = true;
        HideToTray();
    }

    private void Quit()
    {
        _exiting = true;
        try { _server?.StandardInput.Close(); } catch { }
        try { if (_server is { HasExited: false }) _server.Kill(); } catch { }
        if (_job != IntPtr.Zero) { CloseHandle(_job); _job = IntPtr.Zero; }
        _tray.Visible = false;
        _web.Dispose();
        _win.Close();
        ExitThread();
    }

    private void Fail(string msg)
    {
        Append("[ui] " + msg);
        _web.Visible = false;
        _splash.Visible = true;
        _splash.ForeColor = Color.FromArgb(160, 60, 50);
        _splash.Text = msg;
        if (!_win.Visible) ShowWindow();
    }

    private void UiInvoke(Action a)
    {
        if (!_win.IsHandleCreated) return;
        try { _win.BeginInvoke(a); } catch { }
    }

    // ---------------------------------------------------------------- 杂项

    private string LogPath => Path.Combine(_dataDir, "server.log");

    private void Append(string? line)
    {
        if (string.IsNullOrEmpty(line)) return;
        lock (_log)
        {
            _log.AppendLine(line);
            if (_log.Length > 20000) _log.Remove(0, _log.Length - 16000);
        }
        try { File.AppendAllText(LogPath, line + Environment.NewLine, Encoding.UTF8); } catch { }
    }

    private string Tail()
    {
        lock (_log)
        {
            var lines = _log.ToString().Split('\n');
            return string.Join(Environment.NewLine, lines.Skip(Math.Max(0, lines.Length - 8)));
        }
    }

    private static void OpenExternal(string uri)
    {
        try { Process.Start(new ProcessStartInfo(uri) { UseShellExecute = true }); } catch { }
    }

    protected override void Dispose(bool disposing)
    {
        if (disposing) { _tray.Dispose(); _web.Dispose(); _win.Dispose(); }
        base.Dispose(disposing);
    }

    // ---------------------------------------------------------------- Win32

    [DllImport("dwmapi.dll")]
    private static extern int DwmSetWindowAttribute(IntPtr hwnd, int attr, ref int value, int size);

    [DllImport("kernel32.dll", CharSet = CharSet.Unicode)]
    private static extern IntPtr CreateJobObject(IntPtr sec, string? name);

    [DllImport("kernel32.dll")]
    private static extern bool SetInformationJobObject(IntPtr job, int cls, IntPtr info, int len);

    [DllImport("kernel32.dll")]
    private static extern bool AssignProcessToJobObject(IntPtr job, IntPtr proc);

    [DllImport("kernel32.dll")]
    private static extern bool CloseHandle(IntPtr h);

    // x64 布局（sizeof 144）：
    //   BasicLimitInformation 0..63（LimitFlags 在 16）
    //   IoCounters 64..111
    //   ProcessMemoryLimit 112 / JobMemoryLimit 120
    //   PeakProcessMemoryUsed 128 / PeakJobMemoryUsed 136
    // 必须用结构体而不是手写偏移：早先写成 WriteInt64(info,128,0x2000)，
    // 把 0x2000 写进了 PeakProcessMemoryUsed，LimitFlags 仍是 0，
    // KILL_ON_JOB_CLOSE 根本没生效 —— 关窗后 python 进程会留下来。
    [StructLayout(LayoutKind.Sequential)]
    private struct JOBOBJECT_BASIC_LIMIT_INFORMATION
    {
        public long PerProcessUserTimeLimit;
        public long PerJobUserTimeLimit;
        public uint LimitFlags;
        public UIntPtr MinimumWorkingSetSize;
        public UIntPtr MaximumWorkingSetSize;
        public uint ActiveProcessLimit;
        public UIntPtr Affinity;
        public uint PriorityClass;
        public uint SchedulingClass;
    }

    [StructLayout(LayoutKind.Sequential)]
    private struct IO_COUNTERS
    {
        public ulong ReadOperationCount;
        public ulong WriteOperationCount;
        public ulong OtherOperationCount;
        public ulong ReadTransferCount;
        public ulong WriteTransferCount;
        public ulong OtherTransferCount;
    }

    [StructLayout(LayoutKind.Sequential)]
    private struct JOBOBJECT_EXTENDED_LIMIT_INFORMATION
    {
        public JOBOBJECT_BASIC_LIMIT_INFORMATION BasicLimitInformation;
        public IO_COUNTERS IoInfo;
        public UIntPtr ProcessMemoryLimit;
        public UIntPtr JobMemoryLimit;
        public UIntPtr PeakProcessMemoryUsed;
        public UIntPtr PeakJobMemoryUsed;
    }

    private const int JobObjectExtendedLimitInformation = 9;
    private const uint JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x2000;
    private const uint JOB_OBJECT_LIMIT_DIE_ON_UNHANDLED_EXCEPTION = 0x0400;

    private void CreateKillOnCloseJob()
    {
        _job = CreateJobObject(IntPtr.Zero, null);
        if (_job == IntPtr.Zero) return;
        var info = new JOBOBJECT_EXTENDED_LIMIT_INFORMATION();
        info.BasicLimitInformation.LimitFlags =
            JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE | JOB_OBJECT_LIMIT_DIE_ON_UNHANDLED_EXCEPTION;
        var size = Marshal.SizeOf<JOBOBJECT_EXTENDED_LIMIT_INFORMATION>();
        var buf = Marshal.AllocHGlobal(size);
        try
        {
            Marshal.StructureToPtr(info, buf, false);
            if (!SetInformationJobObject(_job, JobObjectExtendedLimitInformation, buf, size))
                Append("[job] SetInformationJobObject failed: err=" + Marshal.GetLastWin32Error());
            else
                Append("[job] kill-on-close armed (" + size + " bytes)");
        }
        finally { Marshal.FreeHGlobal(buf); }
    }

    private void AssignToJob(Process p)
    {
        if (_job == IntPtr.Zero) return;
        try { AssignProcessToJobObject(_job, p.Handle); } catch { }
    }

    protected override void OnMainFormClosed(object? sender, EventArgs e)
    {
        if (!_exiting) Quit();
        base.OnMainFormClosed(sender, e);
    }
}