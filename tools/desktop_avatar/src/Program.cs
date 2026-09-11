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
            string userName = null;
            string password = null;
            string scriptPath = null;
            string scriptArgs = null;
            string scriptWorkDir = null;
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
                else if (string.Equals(arg, "--username", StringComparison.OrdinalIgnoreCase) && i + 1 < args.Length)
                {
                    userName = args[++i];
                }
                else if (string.Equals(arg, "--password", StringComparison.OrdinalIgnoreCase) && i + 1 < args.Length)
                {
                    password = args[++i];
                }
                else if (string.Equals(arg, "--script-path", StringComparison.OrdinalIgnoreCase) && i + 1 < args.Length)
                {
                    scriptPath = args[++i];
                }
                else if (string.Equals(arg, "--script-args", StringComparison.OrdinalIgnoreCase) && i + 1 < args.Length)
                {
                    scriptArgs = args[++i];
                }
                else if (string.Equals(arg, "--script-workdir", StringComparison.OrdinalIgnoreCase) && i + 1 < args.Length)
                {
                    scriptWorkDir = args[++i];
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
                        if (dict != null)
                        {
                            if (string.IsNullOrEmpty(userName) && dict.ContainsKey("UserName") && dict["UserName"] != null)
                                userName = Convert.ToString(dict["UserName"]);
                            if (string.IsNullOrEmpty(password) && dict.ContainsKey("Password") && dict["Password"] != null)
                                password = Convert.ToString(dict["Password"]);
                            if (string.IsNullOrEmpty(scriptPath) && dict.ContainsKey("ScriptPath") && dict["ScriptPath"] != null)
                                scriptPath = Convert.ToString(dict["ScriptPath"]);
                            if (string.IsNullOrEmpty(scriptArgs) && dict.ContainsKey("ScriptArguments") && dict["ScriptArguments"] != null)
                                scriptArgs = Convert.ToString(dict["ScriptArguments"]);
                            if (string.IsNullOrEmpty(scriptWorkDir) && dict.ContainsKey("ScriptWorkingDirectory") && dict["ScriptWorkingDirectory"] != null)
                                scriptWorkDir = Convert.ToString(dict["ScriptWorkingDirectory"]);

                            if (dict.ContainsKey("AutoLaunchItems") && dict["AutoLaunchItems"] != null)
                            {
                                var progsJson = serializer.Serialize(dict["AutoLaunchItems"]);
                                var list = serializer.Deserialize<List<AutoLaunchItem>>(progsJson);
                                if (list != null) autoLaunchItems.AddRange(list);
                            }
                            else if (dict.ContainsKey("auto_launch_programs") && dict["auto_launch_programs"] != null)
                            {
                                var progsJson = serializer.Serialize(dict["auto_launch_programs"]);
                                var list = serializer.Deserialize<List<AutoLaunchItem>>(progsJson);
                                if (list != null) autoLaunchItems.AddRange(list);
                            }
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
            Application.Run(new AvatarForm(
                desktopSize,
                autoLaunchItems,
                title,
                userName,
                password,
                scriptPath,
                scriptArgs,
                scriptWorkDir));
            return 0;
        }
    }
}
