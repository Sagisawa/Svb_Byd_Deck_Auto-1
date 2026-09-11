using System;
using System.Globalization;
using System.IO;
using System.Reflection;
using System.Runtime.InteropServices;
using System.Security.Principal;
using System.Threading.Tasks;

namespace DesktopAvatar
{
    internal static class ChildSessionProcessLauncher
    {
        private const int TaskActionExecute = 0;
        private const int TaskCreate = 2;
        private const int TaskLogonInteractiveToken = 3;
        private const int TaskRunLevelHighest = 1;
        private const int TaskRunUseSessionId = 0x4;

        internal static Task LaunchElevatedAsync(uint childSessionId, string executablePath)
        {
            var fullPath = ValidateExecutablePath(executablePath);
            return LaunchElevatedAsync(
                childSessionId,
                fullPath,
                string.Empty,
                Path.GetDirectoryName(fullPath) ?? AppDomain.CurrentDomain.BaseDirectory);
        }

        internal static Task LaunchElevatedAsync(
            uint childSessionId,
            string executablePath,
            string arguments,
            string workingDirectory)
        {
            return Task.Factory.StartNew(() =>
                LaunchWithTemporaryTask(
                    childSessionId,
                    executablePath,
                    arguments,
                    workingDirectory));
        }

        private static void LaunchWithTemporaryTask(
            uint childSessionId,
            string executablePath,
            string arguments,
            string workingDirectory)
        {
            var actualChildSessionId = ChildSessionNativeMethods.TryGetChildSessionId();
            if (actualChildSessionId != childSessionId)
            {
                throw new InvalidOperationException(
                    string.Format(
                        "目标 Child Session 已发生变化。请求会话为 {0}，当前会话为 {1}。",
                        childSessionId,
                        actualChildSessionId.HasValue ? actualChildSessionId.Value.ToString(CultureInfo.InvariantCulture) : "无"));
            }

            var schedulerType = Type.GetTypeFromProgID("Schedule.Service");
            if (schedulerType == null)
            {
                throw new InvalidOperationException("当前 Windows 未提供任务计划程序 COM 服务。");
            }

            var taskName = string.Format("SvbAvatar-ElevatedLaunch-{0:N}", Guid.NewGuid());
            string accountName;
            using (var currentIdentity = WindowsIdentity.GetCurrent())
            {
                accountName = currentIdentity.Name;
            }

            object schedulerObject = null;
            object rootFolderObject = null;
            object taskDefinitionObject = null;
            object actionObject = null;
            object registeredTaskObject = null;
            object runningTaskObject = null;
            var taskRegistered = false;

            try
            {
                schedulerObject = Activator.CreateInstance(schedulerType);
                if (schedulerObject == null)
                {
                    throw new InvalidOperationException("无法创建任务计划程序 COM 对象。");
                }

                dynamic scheduler = schedulerObject;
                scheduler.Connect();

                rootFolderObject = scheduler.GetFolder("\\");
                dynamic rootFolder = rootFolderObject;
                taskDefinitionObject = scheduler.NewTask(0);
                dynamic taskDefinition = taskDefinitionObject;

                taskDefinition.RegistrationInfo.Author = "SvbDesktopAvatar";
                taskDefinition.RegistrationInfo.Description =
                    string.Format("临时启动 {0} 到 Child Session {1}", Path.GetFileName(executablePath), childSessionId);

                taskDefinition.Settings.Enabled = true;
                taskDefinition.Settings.Hidden = true;
                taskDefinition.Settings.AllowDemandStart = true;
                taskDefinition.Settings.DisallowStartIfOnBatteries = false;
                taskDefinition.Settings.StopIfGoingOnBatteries = false;
                taskDefinition.Settings.ExecutionTimeLimit = "PT0S";

                taskDefinition.Principal.UserId = accountName;
                taskDefinition.Principal.LogonType = TaskLogonInteractiveToken;
                taskDefinition.Principal.RunLevel = TaskRunLevelHighest;

                actionObject = taskDefinition.Actions.Create(TaskActionExecute);
                dynamic action = actionObject;
                action.Path = executablePath;
                action.Arguments = arguments ?? string.Empty;
                action.WorkingDirectory = workingDirectory ?? string.Empty;

                registeredTaskObject = rootFolder.RegisterTaskDefinition(
                    taskName,
                    taskDefinition,
                    TaskCreate,
                    accountName,
                    null,
                    TaskLogonInteractiveToken,
                    null);
                taskRegistered = true;

                dynamic registeredTask = registeredTaskObject;
                runningTaskObject = registeredTask.RunEx(
                    null,
                    TaskRunUseSessionId,
                    checked((int)childSessionId),
                    null);

                if (runningTaskObject == null)
                {
                    throw new InvalidOperationException("任务计划程序没有返回运行实例。");
                }
            }
            finally
            {
                if (taskRegistered && rootFolderObject != null)
                {
                    try
                    {
                        dynamic rootFolder = rootFolderObject;
                        rootFolder.DeleteTask(taskName, 0);
                    }
                    catch (COMException)
                    {
                        // 临时任务启动成功后，清理失败不应中断已经启动的目标程序。
                    }
                }

                ReleaseComObject(runningTaskObject);
                ReleaseComObject(registeredTaskObject);
                ReleaseComObject(actionObject);
                ReleaseComObject(taskDefinitionObject);
                ReleaseComObject(rootFolderObject);
                ReleaseComObject(schedulerObject);
            }
        }

        private static string ValidateExecutablePath(string executablePath)
        {
            var fullPath = Path.GetFullPath(executablePath);
            if (!File.Exists(fullPath))
            {
                throw new FileNotFoundException("要启动的程序不存在。", fullPath);
            }

            var ext = Path.GetExtension(fullPath);
            if (!string.Equals(ext, ".exe", StringComparison.OrdinalIgnoreCase) &&
                !string.Equals(ext, ".bat", StringComparison.OrdinalIgnoreCase) &&
                !string.Equals(ext, ".cmd", StringComparison.OrdinalIgnoreCase))
            {
                throw new ArgumentException("只允许选择可执行程序 (.exe / .bat / .cmd)。", "executablePath");
            }

            return fullPath;
        }

        private static void ReleaseComObject(object value)
        {
            if (value != null && Marshal.IsComObject(value))
            {
                Marshal.FinalReleaseComObject(value);
            }
        }
    }
}
