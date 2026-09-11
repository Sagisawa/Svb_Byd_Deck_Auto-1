using System;
using System.Collections.Generic;
using System.Drawing;
using System.IO;
using System.Threading.Tasks;
using System.Windows.Forms;

namespace DesktopAvatar
{
    internal sealed class AvatarForm : Form
    {
        private Size _currentDesktopSize;
        private readonly List<AutoLaunchItem> _autoLaunchItems;
        private readonly string _userName;
        private readonly string _password;
        private string _scriptPath;
        private readonly string _scriptArguments;
        private readonly string _scriptWorkingDirectory;
        private readonly RdpActiveXHost _rdpHost;
        private readonly Panel _topPanel;
        private readonly Label _lblStatus;
        private readonly ComboBox _cbResolution;
        private readonly Button _btnFitWindow;
        private readonly Button _btnSmartSizing;
        private readonly Button _btnReconnect;
        private readonly Button _btnWinD;
        private readonly Button _btnWinTab;
        private readonly Button _btnTopMost;
        private readonly Button _btnLaunchScript;
        private readonly Button _btnLaunch;
        private readonly Button _btnLogoff;
        private bool _isSmartSizing = true;
        private bool _isTopMost = false;
        private bool _autoLaunchDone = false;

        public AvatarForm(
            Size desktopSize,
            List<AutoLaunchItem> autoLaunchItems = null,
            string windowTitle = null,
            string userName = null,
            string password = null,
            string scriptPath = null,
            string scriptArguments = null,
            string scriptWorkingDirectory = null)
        {
            _currentDesktopSize = desktopSize;
            _autoLaunchItems = autoLaunchItems ?? new List<AutoLaunchItem>();
            _userName = userName;
            _password = password;
            _scriptPath = scriptPath;
            _scriptArguments = scriptArguments;
            _scriptWorkingDirectory = scriptWorkingDirectory;

            Text = string.IsNullOrEmpty(windowTitle) ? "影之诗桌面分身 (Svb Desktop Avatar)" : windowTitle;
            StartPosition = FormStartPosition.CenterScreen;
            ClientSize = new Size(Math.Max(1000, Math.Min(1280, desktopSize.Width)), Math.Min(720, desktopSize.Height) + 38);
            MinimumSize = new Size(920, 480);
            BackColor = Color.FromArgb(24, 24, 37); // Dark theme

            // 顶部工具栏
            _topPanel = new Panel
            {
                Dock = DockStyle.Top,
                Height = 38,
                BackColor = Color.FromArgb(30, 30, 46),
                Padding = new Padding(8, 4, 8, 4)
            };

            _lblStatus = new Label
            {
                Text = "准备连接...",
                ForeColor = Color.FromArgb(205, 214, 244),
                AutoSize = true,
                Font = new Font("Segoe UI", 9.5f, FontStyle.Regular),
                Location = new Point(10, 9)
            };

            // 分辨率下拉选择框
            _cbResolution = new ComboBox
            {
                DropDownStyle = ComboBoxStyle.DropDownList,
                FlatStyle = FlatStyle.Flat,
                DrawMode = DrawMode.OwnerDrawFixed,
                BackColor = Color.FromArgb(49, 50, 68),
                ForeColor = Color.FromArgb(205, 214, 244),
                Font = new Font("Segoe UI", 9f),
                Width = 180,
                Height = 28,
                Margin = new Padding(3, 4, 3, 2),
                Cursor = Cursors.Hand
            };

            _cbResolution.DrawItem += (s, e) =>
            {
                if (e.Index < 0) return;
                var cb = (ComboBox)s;
                bool isSelected = (e.State & DrawItemState.Selected) == DrawItemState.Selected;
                using (var bgBrush = new SolidBrush(isSelected ? Color.FromArgb(69, 71, 90) : Color.FromArgb(40, 40, 60)))
                using (var textBrush = new SolidBrush(Color.FromArgb(205, 214, 244)))
                {
                    e.Graphics.FillRectangle(bgBrush, e.Bounds);
                    string text = cb.Items[e.Index].ToString();
                    var sf = new StringFormat
                    {
                        LineAlignment = StringAlignment.Center,
                        Alignment = StringAlignment.Near
                    };
                    var textRect = new Rectangle(e.Bounds.X + 4, e.Bounds.Y, e.Bounds.Width - 4, e.Bounds.Height);
                    e.Graphics.DrawString(text, cb.Font, textBrush, textRect, sf);
                }
            };

            PopulateResolutionPresets();

            _btnFitWindow = CreateButton("1:1 视口", 68);
            _btnSmartSizing = CreateButton("自适应: 开", 80);
            _btnReconnect = CreateButton("重连", 52);
            _btnWinD = CreateButton("Win+D", 58);
            _btnWinTab = CreateButton("Win+Tab", 68);
            _btnTopMost = CreateButton("置顶: 关", 68);
            _btnLaunchScript = new Button
            {
                Text = "启动脚本程序",
                Width = 118,
                Height = 28,
                FlatStyle = FlatStyle.Flat,
                BackColor = Color.FromArgb(37, 99, 235), // Royal blue accent
                ForeColor = Color.White,
                Font = new Font("Segoe UI", 9f, FontStyle.Bold),
                Margin = new Padding(3, 1, 3, 1),
                Cursor = Cursors.Hand
            };
            _btnLaunchScript.FlatAppearance.BorderSize = 0;
            var ttScript = new ToolTip();
            ttScript.SetToolTip(_btnLaunchScript, "在分身中以最高权限启动 Svb_Byd_Deck_Auto 自动化程序");

            _btnLaunch = CreateButton("运行程序", 76);
            _btnLogoff = CreateButton("注销会话", 76);

            var flowPanel = new FlowLayoutPanel
            {
                Dock = DockStyle.Right,
                AutoSize = true,
                FlowDirection = FlowDirection.LeftToRight,
                WrapContents = false,
                BackColor = Color.Transparent,
                Margin = new Padding(0)
            };

            flowPanel.Controls.Add(_cbResolution);
            flowPanel.Controls.Add(_btnFitWindow);
            flowPanel.Controls.Add(_btnSmartSizing);
            flowPanel.Controls.Add(_btnReconnect);
            flowPanel.Controls.Add(_btnWinD);
            flowPanel.Controls.Add(_btnWinTab);
            flowPanel.Controls.Add(_btnTopMost);
            flowPanel.Controls.Add(_btnLaunchScript);
            flowPanel.Controls.Add(_btnLaunch);
            flowPanel.Controls.Add(_btnLogoff);

            _topPanel.Controls.Add(_lblStatus);
            _topPanel.Controls.Add(flowPanel);

            // RDP 控件承载
            _rdpHost = new RdpActiveXHost();
            _rdpHost.Dock = DockStyle.Fill;
            _rdpHost.LoginCompleted += OnLoginCompleted;
            _rdpHost.Connected += (s, e) => UpdateStatus("已建立 RDP 连接，正在登入...");
            _rdpHost.Disconnected += (s, e) => UpdateStatus("桌面分身已断开连接");
            _rdpHost.ConnectionFailed += (s, e) => UpdateStatus("连接失败，请检查登录凭据或网络");

            Controls.Add(_rdpHost);
            Controls.Add(_topPanel);

            // 绑定工具栏事件
            _cbResolution.SelectionChangeCommitted += OnResolutionSelectionChanged;
            _btnFitWindow.Click += OnFitWindowClicked;
            _btnSmartSizing.Click += OnToggleSmartSizing;
            _btnReconnect.Click += (s, e) => TriggerConnect();
            _btnWinD.Click += (s, e) => { try { _rdpHost.SendShowDesktopShortcut(); } catch { } };
            _btnWinTab.Click += (s, e) => { try { _rdpHost.SendTaskViewShortcut(); } catch { } };
            _btnTopMost.Click += OnToggleTopMost;
            _btnLaunchScript.Click += OnLaunchScriptClicked;
            _btnLaunch.Click += OnLaunchProgramClicked;
            _btnLogoff.Click += OnLogoffClicked;
        }

        private Button CreateButton(string text, int width)
        {
            var btn = new Button
            {
                Text = text,
                Width = width,
                Height = 28,
                FlatStyle = FlatStyle.Flat,
                BackColor = Color.FromArgb(49, 50, 68),
                ForeColor = Color.FromArgb(205, 214, 244),
                Font = new Font("Segoe UI", 9f),
                Margin = new Padding(3, 1, 3, 1),
                Cursor = Cursors.Hand
            };
            btn.FlatAppearance.BorderSize = 0;
            return btn;
        }

        private void PopulateResolutionPresets()
        {
            var primary = Screen.PrimaryScreen.Bounds;
            var presets = new ResolutionItem[]
            {
                new ResolutionItem("1920 × 1080 (1080P 推荐)", 1920, 1080),
                new ResolutionItem("2560 × 1440 (2K 超清)", 2560, 1440),
                new ResolutionItem("3840 × 2160 (4K 极清)", 3840, 2160),
                new ResolutionItem("1600 × 900 (900P)", 1600, 900),
                new ResolutionItem("1366 × 768", 1366, 768),
                new ResolutionItem("1280 × 720 (720P)", 1280, 720),
                new ResolutionItem(string.Format("跟随主屏幕 ({0}×{1})", primary.Width, primary.Height), primary.Width, primary.Height),
                new ResolutionItem("自定义分辨率...", -1, -1)
            };

            _cbResolution.Items.Clear();
            _cbResolution.Items.AddRange(presets);

            SyncResolutionDropdown();
        }

        private void SyncResolutionDropdown()
        {
            int matchedIndex = -1;
            for (int i = 0; i < _cbResolution.Items.Count; i++)
            {
                var item = (ResolutionItem)_cbResolution.Items[i];
                if (item.Width == _currentDesktopSize.Width && item.Height == _currentDesktopSize.Height)
                {
                    matchedIndex = i;
                    break;
                }
            }

            if (matchedIndex >= 0)
            {
                _cbResolution.SelectedIndex = matchedIndex;
            }
            else
            {
                var customItem = new ResolutionItem(
                    string.Format("当前: {0}×{1}", _currentDesktopSize.Width, _currentDesktopSize.Height),
                    _currentDesktopSize.Width,
                    _currentDesktopSize.Height);
                _cbResolution.Items.Insert(0, customItem);
                _cbResolution.SelectedIndex = 0;
            }
        }

        private void OnResolutionSelectionChanged(object sender, EventArgs e)
        {
            if (_cbResolution.SelectedItem == null) return;
            var item = (ResolutionItem)_cbResolution.SelectedItem;

            if (item.Width == -1)
            {
                using (var dlg = new CustomResolutionDialog(_currentDesktopSize.Width, _currentDesktopSize.Height))
                {
                    if (dlg.ShowDialog(this) == DialogResult.OK)
                    {
                        var customSize = dlg.SelectedResolution;
                        ApplyNewResolution(customSize, string.Format("自定义 ({0}×{1})", customSize.Width, customSize.Height));
                    }
                    else
                    {
                        SyncResolutionDropdown();
                    }
                }
                return;
            }

            if (item.Width == _currentDesktopSize.Width && item.Height == _currentDesktopSize.Height)
            {
                return;
            }

            ApplyNewResolution(new Size(item.Width, item.Height), item.DisplayName);
        }

        private void ApplyNewResolution(Size newSize, string displayName)
        {
            _currentDesktopSize = newSize;
            UpdateStatus("正在切换分辨率至 " + displayName + "...");
            try
            {
                _rdpHost.ChangeDesktopResolution(_currentDesktopSize);
                UpdateStatus(string.Format("已应用分辨率: {0}×{1}", _currentDesktopSize.Width, _currentDesktopSize.Height));
            }
            catch (Exception ex)
            {
                UpdateStatus("分辨率切换失败: " + ex.Message);
            }
            SyncResolutionDropdown();
        }

        private void OnFitWindowClicked(object sender, EventArgs e)
        {
            var screen = Screen.FromControl(this).WorkingArea;
            int targetClientW = _currentDesktopSize.Width;
            int targetClientH = _currentDesktopSize.Height + _topPanel.Height;

            if (targetClientW > screen.Width || targetClientH > screen.Height)
            {
                WindowState = FormWindowState.Maximized;
                UpdateStatus("窗口已最大化以适应高分辨率");
            }
            else
            {
                WindowState = FormWindowState.Normal;
                ClientSize = new Size(targetClientW, targetClientH);
                Location = new Point(
                    screen.Left + Math.Max(0, (screen.Width - Width) / 2),
                    screen.Top + Math.Max(0, (screen.Height - Height) / 2)
                );
                UpdateStatus(string.Format("已恢复 1:1 视口 ({0}×{1})", _currentDesktopSize.Width, _currentDesktopSize.Height));
            }
        }

        protected override void OnShown(EventArgs e)
        {
            base.OnShown(e);
            TriggerConnect();
        }

        protected override void OnFormClosing(FormClosingEventArgs e)
        {
            try
            {
                _rdpHost.DisconnectSession();
            }
            catch { }
            base.OnFormClosing(e);
        }

        private void TriggerConnect()
        {
            UpdateStatus(string.Format("正在连接 Child Session ({0}×{1})...", _currentDesktopSize.Width, _currentDesktopSize.Height));
            try
            {
                _rdpHost.ConnectToChildSession(_currentDesktopSize, _userName, null, _password);
            }
            catch (Exception ex)
            {
                UpdateStatus("连接异常: " + ex.Message);
            }
        }

        private string DetectScriptPath()
        {
            string appDir = AppDomain.CurrentDomain.BaseDirectory;
            string[] candidates = new string[]
            {
                Path.Combine(appDir, "Svb_Byd_Deck_Auto.exe"),
                Path.Combine(appDir, "..", "Svb_Byd_Deck_Auto.exe"),
                Path.Combine(appDir, "..", "..", "dist", "Svb_Byd_Deck_Auto", "Svb_Byd_Deck_Auto.exe"),
                Path.Combine(appDir, "..", "..", "Svb_Byd_Deck_Auto.exe"),
            };

            foreach (var path in candidates)
            {
                try
                {
                    var full = Path.GetFullPath(path);
                    if (File.Exists(full))
                    {
                        return full;
                    }
                }
                catch { }
            }
            return null;
        }

        private async void OnLaunchScriptClicked(object sender, EventArgs e)
        {
            var sessionId = ChildSessionNativeMethods.TryGetChildSessionId();
            if (!sessionId.HasValue)
            {
                MessageBox.Show(
                    "桌面分身尚未完全就绪，请稍候再试。",
                    "提示",
                    MessageBoxButtons.OK,
                    MessageBoxIcon.Information);
                return;
            }

            string targetPath = _scriptPath;
            string targetArgs = _scriptArguments ?? "";
            string targetWorkDir = _scriptWorkingDirectory ?? "";

            if (string.IsNullOrEmpty(targetPath) || !File.Exists(targetPath))
            {
                targetPath = DetectScriptPath();
            }

            if (string.IsNullOrEmpty(targetPath) || !File.Exists(targetPath))
            {
                using (var ofd = new OpenFileDialog())
                {
                    ofd.Title = "选择 Svb_Byd_Deck_Auto 自动化程序";
                    ofd.Filter = "可执行程序 (*.exe)|*.exe|Python 脚本 (*.py)|*.py|所有文件 (*.*)|*.*";
                    if (ofd.ShowDialog(this) == DialogResult.OK)
                    {
                        targetPath = ofd.FileName;
                        _scriptPath = targetPath;
                    }
                    else
                    {
                        return;
                    }
                }
            }

            if (string.IsNullOrEmpty(targetWorkDir))
            {
                targetWorkDir = Path.GetDirectoryName(targetPath);
            }

            try
            {
                UpdateStatus("正在启动脚本: " + Path.GetFileName(targetPath) + "...");
                await ChildSessionProcessLauncher.LaunchElevatedAsync(
                    sessionId.Value,
                    targetPath,
                    targetArgs,
                    targetWorkDir);
                UpdateStatus("已在分身中启动脚本: " + Path.GetFileName(targetPath));
            }
            catch (Exception ex)
            {
                MessageBox.Show("启动脚本程序失败: " + ex.Message, "错误", MessageBoxButtons.OK, MessageBoxIcon.Error);
                UpdateStatus("启动脚本失败: " + ex.Message);
            }
        }

        private void UpdateStatus(string message)
        {
            if (InvokeRequired)
            {
                BeginInvoke(new Action<string>(UpdateStatus), message);
                return;
            }
            _lblStatus.Text = message;
        }

        private void OnLoginCompleted(object sender, EventArgs e)
        {
            var sessionId = ChildSessionNativeMethods.TryGetChildSessionId();
            string status = sessionId.HasValue
                ? string.Format("已连接桌面分身 (Session {0}, {1}×{2})", sessionId.Value, _currentDesktopSize.Width, _currentDesktopSize.Height)
                : string.Format("已连接桌面分身 ({0}×{1})", _currentDesktopSize.Width, _currentDesktopSize.Height);
            UpdateStatus(status);

            // 自动拉起已配置的自启程序列表（系统级穿透，最高管理员权限，完全绕过 UAC 拦截）
            if (!_autoLaunchDone && sessionId.HasValue && _autoLaunchItems != null && _autoLaunchItems.Count > 0)
            {
                _autoLaunchDone = true;
                _ = LaunchConfiguredProgramsAsync(sessionId.Value);
            }
        }

        private async Task LaunchConfiguredProgramsAsync(uint sessionId)
        {
            foreach (var item in _autoLaunchItems)
            {
                if (item == null || !item.Enabled || string.IsNullOrWhiteSpace(item.Path))
                {
                    continue;
                }

                if (item.DelaySeconds > 0)
                {
                    UpdateStatus(string.Format("等待 {0} 秒后启动: {1}...", item.DelaySeconds, item.Name));
                    await Task.Delay(item.DelaySeconds * 1000);
                }

                try
                {
                    var workDir = string.IsNullOrWhiteSpace(item.WorkingDirectory)
                        ? Path.GetDirectoryName(Path.GetFullPath(item.Path))
                        : item.WorkingDirectory;

                    await ChildSessionProcessLauncher.LaunchElevatedAsync(
                        sessionId,
                        item.Path,
                        item.Arguments ?? "",
                        workDir ?? "");

                    UpdateStatus("已自启动: " + (string.IsNullOrWhiteSpace(item.Name) ? Path.GetFileName(item.Path) : item.Name));
                }
                catch (Exception ex)
                {
                    UpdateStatus(string.Format("自启动 {0} 失败: {1}", item.Name, ex.Message));
                }
            }
        }

        private void OnToggleSmartSizing(object sender, EventArgs e)
        {
            _isSmartSizing = !_isSmartSizing;
            _rdpHost.SetSmartSizing(_isSmartSizing);
            _btnSmartSizing.Text = _isSmartSizing ? "自适应: 开" : "自适应: 关";
        }

        private void OnToggleTopMost(object sender, EventArgs e)
        {
            _isTopMost = !_isTopMost;
            TopMost = _isTopMost;
            _btnTopMost.Text = _isTopMost ? "置顶: 开" : "置顶: 关";
        }

        private void OnLaunchProgramClicked(object sender, EventArgs e)
        {
            var sessionId = ChildSessionNativeMethods.TryGetChildSessionId();
            if (!sessionId.HasValue)
            {
                MessageBox.Show(
                    "桌面分身尚未完全就绪，请稍候再试。",
                    "提示",
                    MessageBoxButtons.OK,
                    MessageBoxIcon.Information);
                return;
            }

            using (var ofd = new OpenFileDialog())
            {
                ofd.Title = "选择在分身中运行的程序";
                ofd.Filter = "可执行程序 (*.exe;*.bat;*.cmd)|*.exe;*.bat;*.cmd|所有文件 (*.*)|*.*";
                if (ofd.ShowDialog(this) == DialogResult.OK)
                {
                    try
                    {
                        ChildSessionProcessLauncher.LaunchElevatedAsync(sessionId.Value, ofd.FileName);
                        UpdateStatus("已在分身中启动: " + Path.GetFileName(ofd.FileName));
                    }
                    catch (Exception ex)
                    {
                        MessageBox.Show("启动失败: " + ex.Message, "错误", MessageBoxButtons.OK, MessageBoxIcon.Error);
                    }
                }
            }
        }

        private void OnLogoffClicked(object sender, EventArgs e)
        {
            var result = MessageBox.Show(
                "确定要注销桌面分身会话吗？分身内正在运行的所有未保存程序将被关闭。",
                "注销确认",
                MessageBoxButtons.YesNo,
                MessageBoxIcon.Question);

            if (result == DialogResult.Yes)
            {
                try
                {
                    _rdpHost.DisconnectSession();
                    ChildSessionNativeMethods.TerminateChildSession(false);
                    UpdateStatus("已注销桌面分身会话");
                }
                catch (Exception ex)
                {
                    UpdateStatus("注销失败: " + ex.Message);
                }
            }
        }
    }

    internal sealed class ResolutionItem
    {
        public string DisplayName { get; }
        public int Width { get; }
        public int Height { get; }

        public ResolutionItem(string displayName, int width, int height)
        {
            DisplayName = displayName;
            Width = width;
            Height = height;
        }

        public override string ToString() => DisplayName;
    }

    internal sealed class CustomResolutionDialog : Form
    {
        private readonly NumericUpDown _numWidth;
        private readonly NumericUpDown _numHeight;

        public Size SelectedResolution => new Size((int)_numWidth.Value, (int)_numHeight.Value);

        public CustomResolutionDialog(int currentWidth, int currentHeight)
        {
            Text = "自定义桌面分辨率";
            FormBorderStyle = FormBorderStyle.FixedDialog;
            MaximizeBox = false;
            MinimizeBox = false;
            StartPosition = FormStartPosition.CenterParent;
            ClientSize = new Size(290, 150);
            BackColor = Color.FromArgb(30, 30, 46);
            ForeColor = Color.FromArgb(205, 214, 244);
            Font = new Font("Segoe UI", 9f);

            var lblW = new Label
            {
                Text = "宽度 (Width):",
                Location = new Point(20, 22),
                AutoSize = true
            };
            _numWidth = new NumericUpDown
            {
                Location = new Point(130, 20),
                Width = 130,
                Minimum = 640,
                Maximum = 7680,
                Value = MathHelper.Clamp(currentWidth, 640, 7680),
                BackColor = Color.FromArgb(49, 50, 68),
                ForeColor = Color.FromArgb(205, 214, 244)
            };

            var lblH = new Label
            {
                Text = "高度 (Height):",
                Location = new Point(20, 58),
                AutoSize = true
            };
            _numHeight = new NumericUpDown
            {
                Location = new Point(130, 56),
                Width = 130,
                Minimum = 480,
                Maximum = 4320,
                Value = MathHelper.Clamp(currentHeight, 480, 4320),
                BackColor = Color.FromArgb(49, 50, 68),
                ForeColor = Color.FromArgb(205, 214, 244)
            };

            var btnOk = new Button
            {
                Text = "确定",
                Location = new Point(100, 102),
                Width = 75,
                Height = 30,
                FlatStyle = FlatStyle.Flat,
                BackColor = Color.FromArgb(203, 166, 247), // Mauve
                ForeColor = Color.FromArgb(30, 30, 46),
                DialogResult = DialogResult.OK,
                Cursor = Cursors.Hand
            };
            btnOk.FlatAppearance.BorderSize = 0;

            var btnCancel = new Button
            {
                Text = "取消",
                Location = new Point(185, 102),
                Width = 75,
                Height = 30,
                FlatStyle = FlatStyle.Flat,
                BackColor = Color.FromArgb(49, 50, 68),
                ForeColor = Color.FromArgb(205, 214, 244),
                DialogResult = DialogResult.Cancel,
                Cursor = Cursors.Hand
            };
            btnCancel.FlatAppearance.BorderSize = 0;

            AcceptButton = btnOk;
            CancelButton = btnCancel;

            Controls.Add(lblW);
            Controls.Add(_numWidth);
            Controls.Add(lblH);
            Controls.Add(_numHeight);
            Controls.Add(btnOk);
            Controls.Add(btnCancel);
        }
    }
}
