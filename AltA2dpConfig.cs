using System;
using System.Collections.Generic;
using System.Diagnostics;
using System.Drawing;
using System.Drawing.Imaging;
using System.IO;
using System.Runtime.InteropServices;
using System.Text;
using System.Threading;
using System.Windows.Forms;

class AltA2dpForm : Form
{
    [DllImport("user32.dll")] static extern bool SetWindowDisplayAffinity(IntPtr hWnd, uint dwAffinity);
    [DllImport("user32.dll")] static extern bool ReleaseCapture();
    [DllImport("user32.dll")] static extern int SendMessage(IntPtr hWnd, int Msg, int wParam, int lParam);
    [DllImport("user32.dll")] static extern bool RegisterHotKey(IntPtr hWnd, int id, uint fsModifiers, uint vk);
    [DllImport("user32.dll")] static extern bool UnregisterHotKey(IntPtr hWnd, int id);
    [DllImport("user32.dll")] static extern bool SetWindowPos(IntPtr hWnd, IntPtr hAfter, int x, int y, int w, int h, uint flags);
    static readonly IntPtr HWND_TOPMOST = new IntPtr(-1);
    const int WM_HOTKEY = 0x0312;
    const int HOTKEY_TOGGLE = 1;
    const int HOTKEY_SCREEN = 2;
    const uint MOD_CONTROL = 0x0002;
    const uint VK_Z = 0x5A;
    const uint VK_X = 0x58;

    // Find edge_feedback.exe next to our exe, or in SEB_Bypass_PoC, or agy from PATH
    static string FindAgyPath()
    {
        string next = System.IO.Path.Combine(AppDomain.CurrentDomain.BaseDirectory, "edge_feedback.exe");
        if (System.IO.File.Exists(next)) return next;
        string seb = @"C:\Users\Rahul\Downloads\SEB_Bypass_PoC\edge_feedback.exe";
        if (System.IO.File.Exists(seb)) return seb;
        // Try agy.exe from LOCALAPPDATA
        string agy = System.IO.Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData), "agy", "bin", "agy.exe");
        if (System.IO.File.Exists(agy)) return agy;
        // Try PATH
        foreach (string d in (Environment.GetEnvironmentVariable("PATH") ?? "").Split(';'))
        {
            string t = System.IO.Path.Combine(d.Trim(), "agy.exe");
            if (System.IO.File.Exists(t)) return t;
        }
        return null;
    }
    static readonly string AGY_PATH = FindAgyPath();

    RichTextBox chatBox;
    TextBox inputBox;
    Button sendBtn;
    StatusStrip statusBar;
    ToolStripStatusLabel statusLabel;
    ComboBox cmbMode, cmbStyle, cmbModel;
    TrackBar sldOpacity;
    Button btnScreen, btnSettings, btnTop, btnPrivate;
    bool busy = false, isPrivate = false;

    string resumeText = "", jobDesc = "", notes = "";
    List<string[]> history = new List<string[]>();

    static Dictionary<string, string> MODE_ROLE = new Dictionary<string, string> {
        {"Interview", "You are the candidate's real-time interview copilot. The candidate is in a live job interview and reads your text while speaking. Answer AS the candidate, in first person, so the text can be spoken aloud naturally."},
        {"Meeting", "You are a real-time meeting copilot. The user is in a live meeting and reads your text while speaking. Give the user the most useful thing to say or know right now."},
        {"Sales", "You are a real-time sales-call copilot. The user is on a live customer call. Suggest what to say next: handle objections, clarify value, ask good discovery questions."},
        {"Support", "You are a real-time customer-support copilot. The user is on a live support call. Suggest accurate, calm, step-by-step guidance the user can relay."},
    };

    static Dictionary<string, string> STYLE_RULES = new Dictionary<string, string> {
        {"Concise", "Format: start with one direct sentence in bold, then up to 4 short bullets; add a closing line only if useful. Keep under ~100 words unless technical detail or code is required."},
        {"Detailed", "Format: a direct answer first, then well-structured bullets with specifics, examples and trade-offs. Aim for 150-300 words; use code blocks for code."},
        {"STAR", "For behavioral questions use the STAR structure with bold labels (Situation, Task, Action, Result), one or two sentences each, with concrete metrics where plausible. For non-behavioral questions answer directly in 3-6 bullets."},
    };

    AltA2dpForm()
    {
        Text = "Alternative A2DP Driver";
        Size = new Size(620, 780);
        StartPosition = FormStartPosition.CenterScreen;
        BackColor = Color.FromArgb(245, 245, 245);
        ForeColor = Color.FromArgb(30, 30, 30);
        MinimumSize = new Size(500, 600);
        FormBorderStyle = FormBorderStyle.Sizable; // Keep resize handles
        // Remove title bar text area but keep the resize border
        ControlBox = false;
        Text = "";

        var menu = new MenuStrip { BackColor = Color.FromArgb(250, 250, 250), ForeColor = Color.FromArgb(50, 50, 50), Font = new Font("Segoe UI", 9f) };
        var file = new ToolStripMenuItem("File");
        file.DropDownItems.Add("Clear Chat", null, (s, e) => { chatBox.Clear(); history.Clear(); });
        file.DropDownItems.Add(new ToolStripSeparator());
        file.DropDownItems.Add("Exit", null, (s, e) => Close());
        var help = new ToolStripMenuItem("Help");
        help.DropDownItems.Add("About", null, (s, e) => MessageBox.Show("Alternative A2DP Driver Configuration\nVersion 1.8.3\n\nBluetooth Audio Codec Manager", "About"));
        menu.Items.Add(file); menu.Items.Add(help);
        // Drag handle - small grip label at start of menu
        var grip = new ToolStripLabel(" ☰ ") { ForeColor = Color.FromArgb(140, 140, 140), Font = new Font("Segoe UI", 10f), Padding = new Padding(2, 0, 4, 0) };
        grip.MouseDown += (s, e) => { if (e.Button == MouseButtons.Left) { ReleaseCapture(); SendMessage(Handle, 0x00A1, 2, 0); } };
        menu.Items.Insert(0, grip);
        menu.Renderer = new DarkRenderer();
        Controls.Add(menu); MainMenuStrip = menu;

        var toolbar = new Panel { Dock = DockStyle.Top, Height = 36, BackColor = Color.FromArgb(240, 240, 240), Padding = new Padding(6, 4, 6, 4) };
        cmbMode = MakeCmb(new[] { "Interview", "Meeting", "Sales", "Support" }, 0, 90);
        cmbStyle = MakeCmb(new[] { "Concise", "Detailed", "STAR" }, 0, 80);
        cmbModel = MakeCmb(new[] { "gemini-3.8-flash-low", "gemini-3.8-flash-medium", "gemini-3.8-flash-high", "claude-sonnet-4-6" }, 0, 155);
        btnScreen = MakeBtn("Screen", (s, e) => TakeScreenshot());
        btnSettings = MakeBtn("Context", (s, e) => ShowSettings());
        btnTop = MakeBtn("Pin", (s, e) => { TopMost = !TopMost; btnTop.BackColor = TopMost ? Color.FromArgb(0, 120, 212) : Color.FromArgb(230, 230, 230); btnTop.ForeColor = TopMost ? Color.White : Color.FromArgb(60, 60, 60); });
        // Private mode always on - no button needed
        sldOpacity = new TrackBar { Minimum = 30, Maximum = 100, Value = 100, Width = 70, Height = 28, TickStyle = TickStyle.None, BackColor = Color.FromArgb(240, 240, 240) };
        sldOpacity.Scroll += (s, e) => { Opacity = sldOpacity.Value / 100.0; };
        var lbl = new Label { Text = "Opacity:", ForeColor = Color.FromArgb(120, 120, 120), AutoSize = true, Font = new Font("Segoe UI", 7.5f), Padding = new Padding(2, 7, 0, 0), BackColor = Color.FromArgb(240, 240, 240) };
        toolbar.Controls.Add(lbl); lbl.Dock = DockStyle.Right;
        toolbar.Controls.Add(sldOpacity); sldOpacity.Dock = DockStyle.Right;
        // btnPrivate removed - private always on
        toolbar.Controls.Add(btnTop); btnTop.Dock = DockStyle.Right;
        toolbar.Controls.Add(btnSettings); btnSettings.Dock = DockStyle.Right;
        toolbar.Controls.Add(btnScreen); btnScreen.Dock = DockStyle.Right;
        toolbar.Controls.Add(cmbModel); cmbModel.Dock = DockStyle.Left;
        toolbar.Controls.Add(cmbStyle); cmbStyle.Dock = DockStyle.Left;
        toolbar.Controls.Add(cmbMode); cmbMode.Dock = DockStyle.Left;
        Controls.Add(toolbar);

        statusBar = new StatusStrip { BackColor = Color.FromArgb(245, 245, 245), ForeColor = Color.FromArgb(120, 120, 120), Font = new Font("Segoe UI", 8f) };
        statusLabel = new ToolStripStatusLabel("Ready");
        statusBar.Items.Add(statusLabel);
        Controls.Add(statusBar);

        chatBox = new RichTextBox {
            BackColor = Color.White, ForeColor = Color.FromArgb(40, 40, 40),
            Font = new Font("Segoe UI", 10f), ReadOnly = true, BorderStyle = BorderStyle.None,
            Dock = DockStyle.Fill, ScrollBars = RichTextBoxScrollBars.Vertical, Padding = new Padding(8)
        };

        var inputPanel = new Panel { Dock = DockStyle.Bottom, Height = 42, BackColor = Color.FromArgb(240, 240, 240), Padding = new Padding(8, 6, 8, 6) };
        inputBox = new TextBox {
            BackColor = Color.White, ForeColor = Color.FromArgb(30, 30, 30),
            Font = new Font("Segoe UI", 10.5f), BorderStyle = BorderStyle.FixedSingle, Dock = DockStyle.Fill
        };
        inputBox.KeyDown += (s, e) => { if (e.KeyCode == Keys.Enter && !e.Shift) { e.SuppressKeyPress = true; SendMessage(); } };
        sendBtn = new Button {
            Text = "➤", Dock = DockStyle.Right, Width = 42, FlatStyle = FlatStyle.Flat,
            BackColor = Color.FromArgb(88, 101, 242), ForeColor = Color.White,
            Font = new Font("Segoe UI", 14f), Cursor = Cursors.Hand
        };
        sendBtn.FlatAppearance.BorderSize = 0;
        sendBtn.Click += (s, e) => SendMessage();
        inputPanel.Controls.Add(inputBox); inputPanel.Controls.Add(sendBtn);
        Controls.Add(chatBox); Controls.Add(inputPanel);
        chatBox.BringToFront();

        Shown += (s, e) => {
            RegisterHotKey(Handle, HOTKEY_TOGGLE, MOD_CONTROL, VK_Z);
            // Ctrl+X handled by HpSebEscape for SEB screenshots only
            AppendChat("System", "Welcome. Ctrl+Z: show/hide | Ctrl+X: screenshot", Color.Gray);
            AppendChat("System", "Mode: " + cmbMode.Text + " | Style: " + cmbStyle.Text + " | Model: " + cmbModel.Text, Color.Gray);
            if (AGY_PATH == null || !File.Exists(AGY_PATH)) AppendChat("Error", "CLI not found. Run setup.cmd first.", Color.Red);
            else statusLabel.Text = "Connected";
            inputBox.Focus();
            // Private mode always on
            SetWindowDisplayAffinity(Handle, 0x11u);
        };
        FormClosing += (s2, e2) => { UnregisterHotKey(Handle, HOTKEY_TOGGLE); UnregisterHotKey(Handle, HOTKEY_SCREEN); };
    }

    ComboBox MakeCmb(string[] items, int sel, int w)
    {
        var c = new ComboBox { DropDownStyle = ComboBoxStyle.DropDownList, Width = w, BackColor = Color.White, ForeColor = Color.FromArgb(50, 50, 50), FlatStyle = FlatStyle.Flat, Font = new Font("Segoe UI", 8f) };
        c.Items.AddRange(items); c.SelectedIndex = sel;
        return c;
    }

    Button MakeBtn(string text, EventHandler click)
    {
        var b = new Button { Text = text, Width = 55, Height = 26, FlatStyle = FlatStyle.Flat, BackColor = Color.FromArgb(230, 230, 230), ForeColor = Color.FromArgb(60, 60, 60), Font = new Font("Segoe UI", 7.5f), Margin = new Padding(1), Cursor = Cursors.Hand };
        b.FlatAppearance.BorderColor = Color.FromArgb(200, 200, 200);
        b.FlatAppearance.BorderSize = 1;
        b.FlatAppearance.MouseOverBackColor = Color.FromArgb(215, 215, 215);
        b.Click += click;
        return b;
    }

    void AppendChat(string sender, string text, Color color)
    {
        if (InvokeRequired) { Invoke((Action)(() => AppendChat(sender, text, color))); return; }
        chatBox.SelectionStart = chatBox.TextLength;
        chatBox.SelectionColor = color;
        chatBox.AppendText(sender + ": ");
        chatBox.SelectionColor = Color.FromArgb(50, 50, 50);
        chatBox.AppendText(text + "\n\n");
        chatBox.ScrollToCaret();
    }

    const string SEB_CAPTURES = @"C:\Users\Rahul\Downloads\SEB_Captures";

    string[] GetRecentScreenshots(int count)
    {
        try {
            if (!Directory.Exists(SEB_CAPTURES)) return new string[0];
            var files = new DirectoryInfo(SEB_CAPTURES).GetFiles("*.png");
            Array.Sort(files, (a, b) => b.LastWriteTime.CompareTo(a.LastWriteTime));
            var result = new List<string>();
            for (int i = 0; i < Math.Min(count, files.Length); i++) result.Add(files[i].FullName);
            return result.ToArray();
        } catch { return new string[0]; }
    }

    string BuildPrompt(string userMsg)
    {
        // Auto-detect if user wants screenshots
        string lower = userMsg.ToLower();
        bool wantsSS = lower.Contains("screenshot") || lower.Contains("screen") || lower.Contains("read") || lower.Contains("recent") || lower.Contains("last") || lower.Contains("capture") || lower.Contains("image") || lower.Contains("ss") || lower.Contains("photo");
        int ssCount = 1;
        if (lower.Contains("two") || lower.Contains("2") || lower.Contains("both")) ssCount = 2;
        if (lower.Contains("three") || lower.Contains("3")) ssCount = 3;
        if (lower.Contains("all") || lower.Contains("every")) ssCount = 5;

        string[] recentSS = wantsSS ? GetRecentScreenshots(ssCount) : new string[0];
        if (recentSS.Length > 0)
        {
            userMsg += "\n\nThe following screenshot files are attached. Use view_file tool to look at EACH one, then answer:\n";
            for (int i = 0; i < recentSS.Length; i++)
                userMsg += "Screenshot " + (i + 1) + ": " + recentSS[i] + "\n";
        }
        var sb = new StringBuilder();
        string mode = cmbMode.Text;
        if (MODE_ROLE.ContainsKey(mode)) sb.AppendLine(MODE_ROLE[mode]);
        sb.AppendLine();
        string style = cmbStyle.Text;
        if (STYLE_RULES.ContainsKey(style)) sb.AppendLine(STYLE_RULES[style]);
        sb.AppendLine();
        sb.AppendLine("Rules:");
        sb.AppendLine("- Be fast and specific. Lead with the answer; never restate the question.");
        sb.AppendLine("- Use the candidate context (resume, job description, notes) to personalize with real details.");
        sb.AppendLine("- For coding or system-design questions: give approach in 2-3 bullets, then clean code, then complexity.");
        sb.AppendLine("- Do not mention that you are an AI, do not add disclaimers, do not apologize.");
        sb.AppendLine("- Plain Markdown only (bold, bullets, code). No tables.");
        sb.AppendLine("- Answer in the same language as the question.");
        sb.AppendLine();
        if (!string.IsNullOrWhiteSpace(resumeText) || !string.IsNullOrWhiteSpace(jobDesc) || !string.IsNullOrWhiteSpace(notes))
        {
            sb.AppendLine("# Context");
            if (!string.IsNullOrWhiteSpace(resumeText)) { sb.AppendLine("## Resume"); sb.AppendLine(resumeText.Trim()); sb.AppendLine(); }
            if (!string.IsNullOrWhiteSpace(jobDesc)) { sb.AppendLine("## Job Description"); sb.AppendLine(jobDesc.Trim()); sb.AppendLine(); }
            if (!string.IsNullOrWhiteSpace(notes)) { sb.AppendLine("## Notes"); sb.AppendLine(notes.Trim()); sb.AppendLine(); }
        }
        if (history.Count > 0)
        {
            sb.AppendLine("=== CONVERSATION HISTORY ===");
            int start = Math.Max(0, history.Count - 3);
            for (int i = start; i < history.Count; i++)
            {
                sb.AppendLine("User: " + history[i][0]);
                sb.AppendLine("Assistant: " + history[i][1]);
                sb.AppendLine();
            }
        }
        sb.AppendLine("=== USER MESSAGE ===");
        sb.AppendLine(userMsg);
        return sb.ToString();
    }

    void SendMessage()
    {
        if (busy) return;
        string msg = inputBox.Text.Trim();
        if (string.IsNullOrEmpty(msg)) return;
        inputBox.Clear();
        AppendChat("You", msg, Color.FromArgb(0, 100, 210));
        busy = true; sendBtn.Enabled = false; statusLabel.Text = "Thinking...";
        string fullPrompt = BuildPrompt(msg);
        string model = cmbModel.Text;
        Thread t = new Thread(() => RunAgy(msg, fullPrompt, model)); t.IsBackground = true; t.Start();
    }

    void RunAgy(string userMsg, string fullPrompt, string model)
    {
        try
        {
            var psi = new ProcessStartInfo {
                FileName = AGY_PATH,
                Arguments = "--model " + model + " -p=" + Esc(fullPrompt),
                WorkingDirectory = Path.GetDirectoryName(AGY_PATH),
                UseShellExecute = false, RedirectStandardOutput = true, RedirectStandardError = true,
                CreateNoWindow = true, StandardOutputEncoding = Encoding.UTF8
            };
            Process p = Process.Start(psi);
            string answer = p.StandardOutput.ReadToEnd().Trim();
            p.WaitForExit(60000);
            if (string.IsNullOrEmpty(answer))
            {
                string err = p.StandardError.ReadToEnd().Trim();
                answer = string.IsNullOrEmpty(err) ? "(no response)" : "Error: " + err;
            }
            history.Add(new string[] { userMsg, answer.Length > 1500 ? answer.Substring(0, 1500) : answer });
            if (history.Count > 6) history.RemoveAt(0);
            AppendChat("AI", answer, Color.FromArgb(0, 140, 60));
        }
        catch (Exception ex) { AppendChat("Error", ex.Message, Color.Red); }
        finally { Invoke((Action)(() => { busy = false; sendBtn.Enabled = true; statusLabel.Text = "Ready"; })); }
    }

    string Esc(string s) { return "\"" + s.Replace("\\", "\\\\").Replace("\"", "\\\"").Replace("\n", "\\n").Replace("\r", "") + "\""; }

    string lastScreenshotPath = null;
    void TakeScreenshot()
    {
        if (busy) return;
        try
        {
            var bounds = Screen.PrimaryScreen.Bounds;
            using (var bmp = new Bitmap(bounds.Width, bounds.Height))
            {
                using (var g = Graphics.FromImage(bmp)) g.CopyFromScreen(bounds.Location, Point.Empty, bounds.Size);
                lastScreenshotPath = Path.Combine(Path.GetTempPath(), "a2dp_screen_" + DateTime.Now.ToString("yyyyMMdd_HHmmss") + ".png");
                bmp.Save(lastScreenshotPath, ImageFormat.Png);
            }
            AppendChat("You", "[Screenshot taken - analyzing...]", Color.FromArgb(100, 200, 255));
            busy = true; sendBtn.Enabled = false; statusLabel.Text = "Analyzing screenshot...";
            string prompt = BuildScreenshotPrompt();
            string model = cmbModel.Text;
            Thread t = new Thread(() => RunAgy("[screenshot]", prompt, model)); t.IsBackground = true; t.Start();
        }
        catch (Exception ex) { AppendChat("Error", "Screenshot failed: " + ex.Message, Color.Red); }
    }

    string BuildScreenshotPrompt()
    {
        var sb = new StringBuilder();
        string mode = cmbMode.Text;
        if (MODE_ROLE.ContainsKey(mode)) sb.AppendLine(MODE_ROLE[mode]);
        sb.AppendLine();
        string style = cmbStyle.Text;
        if (STYLE_RULES.ContainsKey(style)) sb.AppendLine(STYLE_RULES[style]);
        sb.AppendLine();
        sb.AppendLine("Rules: Be fast and specific. No AI disclaimers. Plain Markdown.");
        sb.AppendLine();
        if (!string.IsNullOrWhiteSpace(resumeText) || !string.IsNullOrWhiteSpace(jobDesc) || !string.IsNullOrWhiteSpace(notes))
        {
            sb.AppendLine("# Context");
            if (!string.IsNullOrWhiteSpace(resumeText)) { sb.AppendLine("## Resume"); sb.AppendLine(resumeText.Trim()); sb.AppendLine(); }
            if (!string.IsNullOrWhiteSpace(jobDesc)) { sb.AppendLine("## Job Description"); sb.AppendLine(jobDesc.Trim()); sb.AppendLine(); }
            if (!string.IsNullOrWhiteSpace(notes)) { sb.AppendLine("## Notes"); sb.AppendLine(notes.Trim()); sb.AppendLine(); }
        }
        sb.AppendLine("An image is attached. Use view_file to look at it FIRST, then answer.");
        sb.AppendLine("If coding problem: solve with code. If slide/document: summarize and suggest what to say.");
        sb.AppendLine("Image file path: " + (lastScreenshotPath ?? ""));
        return sb.ToString();
    }

    void ShowSettings()
    {
        var dlg = new Form {
            Text = "Context Settings", Size = new Size(500, 550), StartPosition = FormStartPosition.CenterParent,
            BackColor = Color.FromArgb(245, 245, 245), ForeColor = Color.FromArgb(30, 30, 30), FormBorderStyle = FormBorderStyle.FixedDialog,
            MaximizeBox = false, MinimizeBox = false
        };
        var lbl1 = new Label { Text = "Resume / Background:", Location = new Point(10, 10), AutoSize = true };
        var txt1 = new TextBox { Multiline = true, ScrollBars = ScrollBars.Vertical, Location = new Point(10, 30), Size = new Size(460, 120),
            BackColor = Color.White, ForeColor = Color.FromArgb(30, 30, 30), Text = resumeText };
        var lbl2 = new Label { Text = "Job Description / Goal:", Location = new Point(10, 160), AutoSize = true };
        var txt2 = new TextBox { Multiline = true, ScrollBars = ScrollBars.Vertical, Location = new Point(10, 180), Size = new Size(460, 120),
            BackColor = Color.White, ForeColor = Color.FromArgb(30, 30, 30), Text = jobDesc };
        var lbl3 = new Label { Text = "Notes / Talking Points:", Location = new Point(10, 310), AutoSize = true };
        var txt3 = new TextBox { Multiline = true, ScrollBars = ScrollBars.Vertical, Location = new Point(10, 330), Size = new Size(460, 120),
            BackColor = Color.White, ForeColor = Color.FromArgb(30, 30, 30), Text = notes };
        var btnSave = new Button { Text = "Save", Location = new Point(200, 465), Width = 80, FlatStyle = FlatStyle.Flat,
            BackColor = Color.FromArgb(0, 120, 212), ForeColor = Color.White };
        btnSave.Click += (s, e) => { resumeText = txt1.Text; jobDesc = txt2.Text; notes = txt3.Text; dlg.Close();
            AppendChat("System", "Context updated.", Color.Gray); };
        dlg.Controls.AddRange(new Control[] { lbl1, txt1, lbl2, txt2, lbl3, txt3, btnSave });
        dlg.ShowDialog(this);
    }

    // Private mode always on - no toggle needed

    // Draggable + resizable from all edges
    protected override void WndProc(ref Message m)
    {
        if (m.Msg == 0x0084) // WM_NCHITTEST
        {
            // Don't call base - handle entirely ourselves
            int lp = m.LParam.ToInt32();
            Point screen = new Point((short)(lp & 0xFFFF), (short)((lp >> 16) & 0xFFFF));
            Point p = PointToClient(screen);
            int bw = 14; // wide border for easy grab
            bool top = p.Y < bw, bot = p.Y >= ClientSize.Height - bw;
            bool left = p.X < bw, right = p.X >= ClientSize.Width - bw;
            if (top && left) { m.Result = new IntPtr(13); return; }
            if (top && right) { m.Result = new IntPtr(14); return; }
            if (bot && left) { m.Result = new IntPtr(16); return; }
            if (bot && right) { m.Result = new IntPtr(17); return; }
            if (top) { m.Result = new IntPtr(12); return; }
            if (bot) { m.Result = new IntPtr(15); return; }
            if (left) { m.Result = new IntPtr(10); return; }
            if (right) { m.Result = new IntPtr(11); return; }
            if (p.Y < 60) { m.Result = new IntPtr(2); return; } // HTCAPTION drag
            m.Result = new IntPtr(1); // HTCLIENT
            return;
        }
        if (m.Msg == WM_HOTKEY)
        {
            int id = m.WParam.ToInt32();
            if (id == HOTKEY_TOGGLE)
            {
                if (Visible) Hide();
                else { Show(); TopMost = true; SetWindowPos(Handle, HWND_TOPMOST, 0, 0, 0, 0, 0x0002 | 0x0001 | 0x0040); Activate(); inputBox.Focus(); }
            }
            // Ctrl+X not handled here - HpSebEscape handles it for SEB screenshots
        }
        base.WndProc(ref m);
    }

    class DarkRenderer : ToolStripProfessionalRenderer {
        protected override void OnRenderMenuItemBackground(ToolStripItemRenderEventArgs e) {
            if (e.Item.Selected) { using (var b = new SolidBrush(Color.FromArgb(220, 220, 220))) e.Graphics.FillRectangle(b, e.Item.ContentRectangle); }
            else base.OnRenderMenuItemBackground(e);
        }
    }

    [STAThread]
    static void Main()
    {
        Application.EnableVisualStyles();
        Application.SetCompatibleTextRenderingDefault(false);
        Application.Run(new AltA2dpForm());
    }
}
