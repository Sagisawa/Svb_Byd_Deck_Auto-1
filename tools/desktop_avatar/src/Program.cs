using System;
using System.Collections.Generic;
using System.Drawing;
using System.IO;
using System.Web.Script.Serialization;
using System.Windows.Forms;

namespace DesktopAvatar
{
    internal sealed class AutoLaunchItem
    {
        public string Name { get; set; }
        public string Path { get; set; }
        public string Arguments { get; set; }
        public string WorkingDirectory { get; set; }
        public int DelaySeconds { get; set; }
        public bool Enabled { get; set; }

        public AutoLaunchItem()
        {
            Name = "";
            Path = "";
            Arguments = "";
            WorkingDirectory = "";
            DelaySeconds = 0;
            Enabled = true;
        }
    }

    internal static class Program
    {
        [STAThread]
        private static int Main(string[] args)
        {
            Application.EnableVisualStyles();
            Application.SetCompatibleTextRenderingDefault(false);

            int width = 1920;
            int height = 1080;
            string launchTarget = null;
            string launchConfigFile = null;
            string launchOnlyPath = null;
            string launchOnlyArgs = null;
            string launchOnlyWorkDir = null;
            string title = null;
            bool enableOnly = false;
            bool logoffOnly = false;

            for (int i = 0; i < args.Length; i++)
            {
                var arg = args[i];
                if (string.Equals(arg, "--width", StringComparison.OrdinalIgnoreCase) && i + 1 < args.Length)
                {
                    int.TryParse(args[++i], out width);
                }
                else if (string.Equals(arg, "--height", StringComparison.OrdinalIgnoreCase) && i + 1 < args.Length)
                {
                    int.TryParse(args[++i], out height);
                }
                else if (string.Equals(arg, "--launch", StringComparison.OrdinalIgnoreCase) && i + 1 < args.Length)
                {
                    launchTarget = args[++i];
                }
                else if (string.Equals(arg, "--launch-config", StringComparison.OrdinalIgnoreCase) && i + 1 < args.Length)
                {
                    launchConfigFile = args[++i];
                }
                else if (string.Equals(arg, "--launch-only", StringComparison.OrdinalIgnoreCase) && i + 1 < args.Length)
                {
                    launchOnlyPath = args[++i];
                }
                else if (string.Equals(arg, "--args", StringComparison.OrdinalIgnoreCase) && i + 1 < args.Length)
                {
                    launchOnlyArgs = args[++i];
                }
                else if (string.Equals(arg, "--workdir", StringComparison.OrdinalIgnoreCase) && i + 1 < args.Length)
                {
                    launchOnlyWorkDir = args[++i];
                }
                else if (string.Equals(arg, "--title", StringComparison.OrdinalIgnoreCase) && i + 1 < args.Length)
                {
                    title = args[++i];
                }
                else if (string.Equals(arg, "--enable-only", StringComparison.OrdinalIgnoreCase))
                {
                    enableOnly = true;
                }
                else if (string.Equals(arg, "--logoff-only", StringComparison.OrdinalIgnoreCase))
                {
                    logoffOnly = true;
                }
            }

            // 单次注入启动模式（向已运行的子会话直接拉起进程）
            if (!string.IsNullOrEmpty(launchOnlyPath))
            {
                var sessionId = ChildSessionNativeMethods.TryGetChildSessionId();
                if (!sessionId.HasValue)
                {
                    MessageBox.Show(
                        "未检测到活动的桌面分身 (Child Session) 会话，请先开启桌面分身。",
                        "提示",
                        MessageBoxButtons.OK,
                        MessageBoxIcon.Warning);
                    return 1;
                }

                try
                {
                    var workDir = string.IsNullOrEmpty(launchOnlyWorkDir)
                        ? Path.GetDirectoryName(Path.GetFullPath(launchOnlyPath))
                        : launchOnlyWorkDir;

                    ChildSessionProcessLauncher.LaunchElevatedAsync(
                        sessionId.Value,
                        launchOnlyPath,
                        launchOnlyArgs ?? "",
                        workDir ?? "").Wait();

                    return 0;
                }
                catch (Exception ex)
                {
                    MessageBox.Show(
                        "向分身注入启动程序失败：\n" + ex.Message,
                        "启动错误",
                        MessageBoxButtons.OK,
                        MessageBoxIcon.Error);
                    return 2;
                }
            }

            if (logoffOnly)
            {
                try
                {
                    ChildSessionNativeMethods.TerminateChildSession(false);
                    return 0;
                }
                catch (Exception ex)
                {
                    Console.WriteLine("注销失败: " + ex.Message);
                    return 1;
                }
            }

            // 检测 RDP Wrapper 冲突
            if (ChildSessionNativeMethods.IsRdpWrapperEnabled())
            {
                var promptResult = MessageBox.Show(
                    "系统检测到当前正在使用 RDP Wrapper / SuperRDP。\n" +
                    "Windows 终端服务 (TermService) 会与 Child Session 互斥，可能导致连接失败。\n\n" +
                    "是否仍尝试继续？",
                    "兼容性提示",
                    MessageBoxButtons.YesNo,
                    MessageBoxIcon.Warning);

                if (promptResult != DialogResult.Yes)
                {
                    return 2;
                }
            }

            // 启用 Child Session
            try
            {
                ChildSessionNativeMethods.EnableChildSessions();
            }
            catch (Exception ex)
            {
                MessageBox.Show(
                    "启用 Windows Child Session 失败：\n" + ex.Message + "\n\n" +
                    "提示：可能需要以管理员身份运行本程序，或当前 Windows 版本精简了终端服务。",
                    "初始化错误",
                    MessageBoxButtons.OK,
                    MessageBoxIcon.Error);
                return 3;
            }

            if (enableOnly)
            {
                return 0;
            }

            var autoLaunchItems = new List<AutoLaunchItem>();
            if (!string.IsNullOrEmpty(launchConfigFile) && File.Exists(launchConfigFile))
            {
                try
                {
                    var content = File.ReadAllText(launchConfigFile);
                    var serializer = new JavaScriptSerializer();
                    if (content.TrimStart().StartsWith("["))
                    {
                        var list = serializer.Deserialize<List<AutoLaunchItem>>(content);
                        if (list != null) autoLaunchItems.AddRange(list);
                    }
                    else
                    {
                        var dict = serializer.Deserialize<Dictionary<string, object>>(content);
                        if (dict != null && dict.ContainsKey("auto_launch_programs"))
                        {
                            var progsJson = serializer.Serialize(dict["auto_launch_programs"]);
                            var list = serializer.Deserialize<List<AutoLaunchItem>>(progsJson);
                            if (list != null) autoLaunchItems.AddRange(list);
                        }
                    }
                }
                catch (Exception ex)
                {
                    Console.WriteLine("[WARN] 读取启动配置清单失败: " + ex.Message);
                }
            }

            if (!string.IsNullOrEmpty(launchTarget))
            {
                autoLaunchItems.Add(new AutoLaunchItem
                {
                    Path = launchTarget,
                    Name = Path.GetFileName(launchTarget),
                    Enabled = true
                });
            }

            var desktopSize = new Size(Math.Max(640, width), Math.Max(480, height));
            Application.Run(new AvatarForm(desktopSize, autoLaunchItems, title));
            return 0;
        }
    }
}
