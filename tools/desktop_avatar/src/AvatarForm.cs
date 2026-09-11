using System;
using System.Drawing;
using System.IO;
using System.Windows.Forms;

namespace DesktopAvatar
{
    internal sealed class AvatarForm : Form
    {
        private readonly Size _desktopSize;
        private readonly string _autoLaunchTarget;
        private readonly RdpActiveXHost _rdpHost;
        private readonly Panel _topPanel;
        private readonly Label _lblStatus;
        private readonly Button _btnReconnect;
        private readonly Button _btnWinD;
        private readonly Button _btnWinTab;
        private readonly Button _btnSmartSizing;
        private readonly Button _btnTopMost;
        private readonly Button _btnLaunch;
        private readonly Button _btnLogoff;
        private bool _isSmartSizing = true;
        private bool _isTopMost = false;
        private bool _autoLaunchDone = false;

        public AvatarForm(Size desktopSize, string autoLaunchTarget = null, string windowTitle = null)
        {
            _desktopSize = desktopSize;
            _autoLaunchTarget = autoLaunchTarget;

            Text = string.IsNullOrEmpty(windowTitle) ? "影之诗桌面分身 (Svb Desktop Avatar)" : windowTitle;
            StartPosition = FormStartPosition.CenterScreen;
            ClientSize = new Size(Math.Min(1280, desktopSize.Width), Math.Min(720, desktopSize.Height) + 40);
            MinimumSize = new Size(640, 400);
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

            int rightX = ClientSize.Width - 10;

            _btnLogoff = CreateButton("注销会话", 72);
            _btnLaunch = CreateButton("运行程序", 72);
            _btnTopMost = CreateButton("置顶: 关", 64);
            _btnSmartSizing = CreateButton("自适应: 开", 72);
            _btnWinTab = CreateButton("Win+Tab", 68);
            _btnWinD = CreateButton("Win+D", 58);
            _btnReconnect = CreateButton("重连", 50);

            var flowPanel = new FlowLayoutPanel
            {
                Dock = DockStyle.Right,
                AutoSize = true,
                FlowDirection = FlowDirection.LeftToRight,
                WrapContents = false,
                BackColor = Color.Transparent,
                Margin = new Padding(0)
            };

            flowPanel.Controls.Add(_btnReconnect);
            flowPanel.Controls.Add(_btnWinD);
            flowPanel.Controls.Add(_btnWinTab);
            flowPanel.Controls.Add(_btnSmartSizing);
            flowPanel.Controls.Add(_btnTopMost);
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
            _btnReconnect.Click += (s, e) => TriggerConnect();
            _btnWinD.Click += (s, e) => { try { _rdpHost.SendShowDesktopShortcut(); } catch { } };
            _btnWinTab.Click += (s, e) => { try { _rdpHost.SendTaskViewShortcut(); } catch { } };
            _btnSmartSizing.Click += OnToggleSmartSizing;
            _btnTopMost.Click += OnToggleTopMost;
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
            UpdateStatus("正在连接 Child Session (localhost)...");
            try
            {
                _rdpHost.ConnectToChildSession(_desktopSize);
            }
            catch (Exception ex)
            {
                UpdateStatus("连接异常: " + ex.Message);
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
                ? string.Format("已连接桌面分身 (Session {0})", sessionId.Value)
                : "已连接桌面分身";
            UpdateStatus(status);

            // 自动拉起目标程序
            if (!_autoLaunchDone && !string.IsNullOrEmpty(_autoLaunchTarget) && sessionId.HasValue)
            {
                _autoLaunchDone = true;
                try
                {
                    ChildSessionProcessLauncher.LaunchElevatedAsync(sessionId.Value, _autoLaunchTarget);
                }
                catch (Exception ex)
                {
                    UpdateStatus("自启动程序失败: " + ex.Message);
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
}
