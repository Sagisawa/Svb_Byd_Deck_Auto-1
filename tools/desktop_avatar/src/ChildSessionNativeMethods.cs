using System;
using System.Collections.Concurrent;
using System.ComponentModel;
using System.IO;
using System.Runtime.InteropServices;
using System.Security;
using System.Text;
using Microsoft.Win32;

namespace DesktopAvatar
{
    internal static class ChildSessionNativeMethods
    {
        private const int DefaultRdpPort = 3389;
        private const int ErrorNotFound = 1168;
        private const string RdpTcpRegistryPath =
            @"SYSTEM\CurrentControlSet\Control\Terminal Server\WinStations\RDP-Tcp";
        private const string TermServiceParametersRegistryPath =
            @"SYSTEM\CurrentControlSet\Services\TermService\Parameters";
        private const string RdpWrapperLibraryName = "rdpwrap.dll";
        private const uint NoChildSessionId = uint.MaxValue;
        private static readonly IntPtr CurrentServerHandle = IntPtr.Zero;
        private static readonly ConcurrentDictionary<IntPtr, IntPtr> RdpInputWindows = new ConcurrentDictionary<IntPtr, IntPtr>();

        [DllImport("wtsapi32.dll", SetLastError = true)]
        [return: MarshalAs(UnmanagedType.Bool)]
        private static extern bool WTSEnableChildSessions(
            [MarshalAs(UnmanagedType.Bool)] bool enable);

        [DllImport("wtsapi32.dll", SetLastError = true)]
        [return: MarshalAs(UnmanagedType.Bool)]
        private static extern bool WTSIsChildSessionsEnabled(
            [MarshalAs(UnmanagedType.Bool)] out bool enabled);

        [DllImport("wtsapi32.dll", SetLastError = true)]
        private static extern bool WTSGetChildSessionId(out uint sessionId);

        [DllImport("wtsapi32.dll", SetLastError = true)]
        [return: MarshalAs(UnmanagedType.Bool)]
        private static extern bool WTSLogoffSession(
            IntPtr serverHandle,
            uint sessionId,
            [MarshalAs(UnmanagedType.Bool)] bool wait);

        [DllImport("user32.dll")]
        [return: MarshalAs(UnmanagedType.Bool)]
        private static extern bool EnumChildWindows(
            IntPtr parentWindow,
            EnumChildWindowCallback callback,
            IntPtr parameter);

        [DllImport("user32.dll", CharSet = CharSet.Unicode)]
        private static extern int GetWindowText(
            IntPtr window,
            StringBuilder text,
            int maximumLength);

        [DllImport("user32.dll")]
        private static extern IntPtr SetFocus(IntPtr window);

        [DllImport("user32.dll")]
        private static extern IntPtr GetFocus();

        [DllImport("user32.dll")]
        [return: MarshalAs(UnmanagedType.Bool)]
        private static extern bool IsWindow(IntPtr window);

        [DllImport("user32.dll")]
        [return: MarshalAs(UnmanagedType.Bool)]
        private static extern bool IsChild(IntPtr parentWindow, IntPtr window);

        internal static void EnableChildSessions()
        {
            if (!WTSEnableChildSessions(true))
            {
                throw CreateLastWin32Exception("无法启用 RDP Child Session");
            }
        }

        internal static int GetConfiguredRdpPort()
        {
            try
            {
                using (var localMachine = RegistryKey.OpenBaseKey(
                    RegistryHive.LocalMachine,
                    RegistryView.Registry64))
                using (var rdpTcpKey = localMachine.OpenSubKey(RdpTcpRegistryPath))
                {
                    var configuredPort = rdpTcpKey?.GetValue("PortNumber");
                    if (configuredPort is int port && port > 0 && port <= ushort.MaxValue)
                    {
                        return port;
                    }
                    return DefaultRdpPort;
                }
            }
            catch (Exception exception) when (exception is SecurityException
                                                  || exception is UnauthorizedAccessException
                                                  || exception is IOException)
            {
                return DefaultRdpPort;
            }
        }

        internal static bool IsRdpWrapperEnabled()
        {
            try
            {
                using (var localMachine = RegistryKey.OpenBaseKey(
                    RegistryHive.LocalMachine,
                    RegistryView.Registry64))
                using (var termServiceParametersKey =
                    localMachine.OpenSubKey(TermServiceParametersRegistryPath))
                {
                    var serviceLibraryPath =
                        termServiceParametersKey?.GetValue("ServiceDll") as string;
                    return serviceLibraryPath != null && serviceLibraryPath.IndexOf(
                        RdpWrapperLibraryName,
                        StringComparison.OrdinalIgnoreCase) >= 0;
                }
            }
            catch (Exception exception) when (exception is SecurityException
                                                  || exception is UnauthorizedAccessException
                                                  || exception is IOException)
            {
                return false;
            }
        }

        internal static bool IsChildSessionsEnabled()
        {
            if (!WTSIsChildSessionsEnabled(out var enabled))
            {
                throw CreateLastWin32Exception("无法读取 RDP Child Session 状态");
            }
            return enabled;
        }

        internal static uint? TryGetChildSessionId()
        {
            return WTSGetChildSessionId(out var sessionId) && sessionId != NoChildSessionId
                ? (uint?)sessionId
                : null;
        }

        internal static uint? TerminateChildSession(bool wait = true)
        {
            if (!WTSGetChildSessionId(out var childSessionId))
            {
                var error = Marshal.GetLastWin32Error();
                if (error == ErrorNotFound)
                {
                    return null;
                }
                throw new Win32Exception(
                    error,
                    $"无法取得 RDP Child Session ID（Win32 错误 {error}）");
            }

            if (childSessionId == NoChildSessionId)
            {
                return null;
            }

            if (!WTSLogoffSession(CurrentServerHandle, childSessionId, wait))
            {
                throw CreateLastWin32Exception($"无法注销 Child Session {childSessionId}");
            }

            return childSessionId;
        }

        internal static bool TryFocusRdpInputWindow(IntPtr rdpHostWindow)
        {
            var inputWindow = FindRdpInputWindow(rdpHostWindow);
            if (inputWindow == IntPtr.Zero)
            {
                return false;
            }

            _ = SetFocus(inputWindow);
            return GetFocus() == inputWindow;
        }

        internal static bool IsRdpInputWindowFocused(IntPtr rdpHostWindow)
        {
            var inputWindow = FindRdpInputWindow(rdpHostWindow);
            return inputWindow != IntPtr.Zero && GetFocus() == inputWindow;
        }

        internal static void ClearRdpInputWindowCache(IntPtr rdpHostWindow)
        {
            IntPtr dummy;
            RdpInputWindows.TryRemove(rdpHostWindow, out dummy);
        }

        private static IntPtr FindRdpInputWindow(IntPtr rdpHostWindow)
        {
            if (rdpHostWindow == IntPtr.Zero)
            {
                return IntPtr.Zero;
            }

            IntPtr cachedInputWindow;
            if (RdpInputWindows.TryGetValue(rdpHostWindow, out cachedInputWindow))
            {
                if (IsWindow(cachedInputWindow) && IsChild(rdpHostWindow, cachedInputWindow))
                {
                    return cachedInputWindow;
                }
                IntPtr dummy;
                RdpInputWindows.TryRemove(rdpHostWindow, out dummy);
            }

            var inputWindow = IntPtr.Zero;
            EnumChildWindowCallback callback = (window, param) =>
            {
                const int windowTextCapacity = 256;
                var windowText = new StringBuilder(windowTextCapacity);
                GetWindowText(window, windowText, windowText.Capacity);
                if (!string.Equals(
                        windowText.ToString(),
                        "Input Capture Window",
                        StringComparison.Ordinal))
                {
                    return true;
                }

                inputWindow = window;
                return false;
            };

            _ = EnumChildWindows(rdpHostWindow, callback, IntPtr.Zero);
            if (inputWindow != IntPtr.Zero)
            {
                RdpInputWindows[rdpHostWindow] = inputWindow;
            }
            return inputWindow;
        }

        private static Win32Exception CreateLastWin32Exception(string operation)
        {
            var error = Marshal.GetLastWin32Error();
            return new Win32Exception(error, $"{operation}（Win32 错误 {error}）");
        }

        private delegate bool EnumChildWindowCallback(IntPtr window, IntPtr parameter);
    }
}
