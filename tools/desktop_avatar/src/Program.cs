using System;
using System.Drawing;
using System.Windows.Forms;

namespace DesktopAvatar
{
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

            var desktopSize = new Size(Math.Max(640, width), Math.Max(480, height));
            Application.Run(new AvatarForm(desktopSize, launchTarget, title));
            return 0;
        }
    }
}
