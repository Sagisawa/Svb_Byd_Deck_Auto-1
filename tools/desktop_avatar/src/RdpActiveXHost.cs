using System;
using System.Drawing;
using System.Globalization;
using System.Reflection;
using System.Runtime.InteropServices;
using System.Windows.Forms;

namespace DesktopAvatar
{
    internal sealed class RdpActiveXHost : AxHost
    {
        // Windows 10+ 自带的 RDP ActiveX 控件 (MsRdpClient10)
        private const string RdpClientClsid = "A0C63C30-F08D-4AB4-907C-34905D770C7D";
        private const int DisableRemoteAudio = 2;
        private const int RedirectAudioToClient = 0;

        private static readonly RemoteKey LeftWindowsKey = new RemoteKey(0x5B, true);
        private static readonly RemoteKey DKey = new RemoteKey(0x20, false);
        private static readonly RemoteKey TabKey = new RemoteKey(0x0F, false);

        private ConnectionPointCookie _eventCookie;
        private RdpEventSink _eventSink;
        private bool _smartSizingEnabled = true;
        private bool _sendSystemShortcutsToRemote = true;
        private bool _audioMuted = false;
        private Size? _pendingReconnectDesktopSize;
        private bool _disconnectRequested = false;
        private string _userName;
        private string _domain;
        private string _password;

        internal event EventHandler ConnectionFailed;
        internal event EventHandler LoginCompleted;
        internal event EventHandler Connected;
        internal event EventHandler Disconnected;

        internal RdpActiveXHost() : base(RdpClientClsid)
        {
            Dock = DockStyle.Fill;
        }

        internal void SetCredentials(string userName, string domain, string password)
        {
            _userName = userName;
            _domain = domain;
            _password = password;
        }

        internal int ConnectedState
        {
            get
            {
                if (!IsHandleCreated)
                {
                    return 0;
                }
                try
                {
                    var ocx = GetRequiredOcx();
                    return Convert.ToInt32(
                        GetComProperty(ocx, "Connected"),
                        CultureInfo.InvariantCulture);
                }
                catch
                {
                    return 0;
                }
            }
        }

        internal void ConnectToChildSession(Size desktopSize, string userName = null, string domain = null, string password = null)
        {
            if (userName != null) _userName = userName;
            if (domain != null) _domain = domain;
            if (password != null) _password = password;

            if (ConnectedState != 0)
            {
                return;
            }

            ChildSessionNativeMethods.ClearRdpInputWindowCache(Handle);
            var client = GetRequiredOcx();
            var width = MathHelper.Clamp(desktopSize.Width, 200, 8192);
            var height = MathHelper.Clamp(desktopSize.Height, 200, 8192);

            SetComProperty(client, "Server", "localhost");
            SetComProperty(client, "DesktopWidth", width);
            SetComProperty(client, "DesktopHeight", height);
            SetComProperty(client, "ColorDepth", 32);
            SetComProperty(client, "ConnectingText", "正在连接桌面分身...");
            SetComProperty(client, "DisconnectedText", "桌面分身已断开");

            if (!string.IsNullOrEmpty(_userName))
            {
                string u = _userName;
                string d = _domain;
                if (string.IsNullOrEmpty(d) && u.Contains("\\"))
                {
                    var parts = u.Split(new char[] { '\\' }, 2);
                    d = parts[0];
                    u = parts[1];
                }
                SetComProperty(client, "UserName", u);
                if (!string.IsNullOrEmpty(d))
                {
                    SetComProperty(client, "Domain", d);
                }
            }

            var securedSettings = GetComProperty(client, "SecuredSettings2");
            if (securedSettings != null)
            {
                SetComProperty(securedSettings, "KeyboardHookMode", _sendSystemShortcutsToRemote ? 1 : 0);
                SetComProperty(securedSettings, "AudioRedirectionMode", _audioMuted ? DisableRemoteAudio : RedirectAudioToClient);
            }

            var advancedSettings = GetComProperty(client, "AdvancedSettings7");
            if (advancedSettings != null)
            {
                SetComProperty(advancedSettings, "RDPPort", ChildSessionNativeMethods.GetConfiguredRdpPort());
                SetComProperty(advancedSettings, "EnableCredSspSupport", true);
                SetComProperty(advancedSettings, "EnableWindowsKey", 1);
                SetComProperty(advancedSettings, "SmartSizing", _smartSizingEnabled);
                if (!string.IsNullOrEmpty(_password))
                {
                    SetComProperty(advancedSettings, "ClearTextPassword", _password);
                }
            }

            // 核心：设置 ConnectToChildSession = true
            try
            {
                var extendedSettings = (IMsRdpExtendedSettings)client;
                object enableZoom = true;
                TrySetExtendedProperty(extendedSettings, "EnableZoom", enableZoom);

                object connectToChildSession = true;
                extendedSettings.set_Property("ConnectToChildSession", ref connectToChildSession);
            }
            catch (Exception ex)
            {
                throw new InvalidOperationException("设置 ConnectToChildSession 失败: " + ex.Message, ex);
            }

            try
            {
                InvokeComMethod(client, "Connect");
            }
            catch (Exception ex)
            {
                throw new InvalidOperationException("启动 RDP 连接失败: " + ex.Message, ex);
            }
        }

        internal bool ChangeDesktopResolution(Size desktopSize)
        {
            if (desktopSize.Width < 200 || desktopSize.Height < 200)
            {
                return false;
            }

            if (ConnectedState == 0)
            {
                ConnectToChildSession(desktopSize);
                return true;
            }

            if (TryUpdateSessionDisplaySettings(desktopSize))
            {
                return true;
            }

            ReconnectToChildSession(desktopSize);
            return true;
        }

        private bool TryUpdateSessionDisplaySettings(Size desktopSize)
        {
            try
            {
                var ocx = GetRequiredOcx();
                InvokeComMethod(
                    ocx,
                    "UpdateSessionDisplaySettings",
                    (uint)desktopSize.Width,
                    (uint)desktopSize.Height,
                    (uint)desktopSize.Width,
                    (uint)desktopSize.Height,
                    0u,
                    100u,
                    100u);
                return true;
            }
            catch
            {
                return false;
            }
        }

        internal void ReconnectToChildSession(Size desktopSize)
        {
            if (_disconnectRequested || _pendingReconnectDesktopSize.HasValue)
            {
                _pendingReconnectDesktopSize = desktopSize;
                return;
            }

            if (ConnectedState == 0)
            {
                ConnectToChildSession(desktopSize);
                return;
            }

            _pendingReconnectDesktopSize = desktopSize;
            try
            {
                if (!DisconnectCore())
                {
                    _pendingReconnectDesktopSize = null;
                    ConnectToChildSession(desktopSize);
                }
            }
            catch
            {
                _pendingReconnectDesktopSize = null;
                throw;
            }
        }

        internal void DisconnectSession()
        {
            _pendingReconnectDesktopSize = null;
            _ = DisconnectCore();
        }

        private bool DisconnectCore()
        {
            if (IsHandleCreated)
            {
                ChildSessionNativeMethods.ClearRdpInputWindowCache(Handle);
            }

            if (ConnectedState == 0)
            {
                return false;
            }

            _disconnectRequested = true;
            try
            {
                InvokeComMethod(GetRequiredOcx(), "Disconnect");
                return true;
            }
            catch
            {
                _disconnectRequested = false;
                throw;
            }
        }

        internal void SetSmartSizing(bool enabled)
        {
            _smartSizingEnabled = enabled;
            if (!IsHandleCreated || ConnectedState == 0)
            {
                return;
            }

            try
            {
                var client = GetRequiredOcx();
                var advancedSettings = GetComProperty(client, "AdvancedSettings7");
                if (advancedSettings != null)
                {
                    SetComProperty(advancedSettings, "SmartSizing", enabled);
                }
            }
            catch { }
        }

        internal void SendShowDesktopShortcut()
        {
            KeyStroke[] strokes = new KeyStroke[]
            {
                new KeyStroke(LeftWindowsKey, false),
                new KeyStroke(DKey, false),
                new KeyStroke(DKey, true),
                new KeyStroke(LeftWindowsKey, true)
            };
            SendShortcut(strokes, "Win+D");
        }

        internal void SendTaskViewShortcut()
        {
            KeyStroke[] strokes = new KeyStroke[]
            {
                new KeyStroke(LeftWindowsKey, false),
                new KeyStroke(TabKey, false),
                new KeyStroke(TabKey, true),
                new KeyStroke(LeftWindowsKey, true)
            };
            SendShortcut(strokes, "Win+Tab");
        }

        private void SendShortcut(KeyStroke[] strokes, string displayName)
        {
            if (ConnectedState != 1)
            {
                throw new InvalidOperationException("桌面分身尚未连接，无法发送快捷键。");
            }

            if (!ChildSessionNativeMethods.TryFocusRdpInputWindow(Handle))
            {
                throw new InvalidOperationException("无法将键盘焦点切换到桌面分身。");
            }

            SendKeyStrokes(strokes);
        }

        private void SendKeyStrokes(KeyStroke[] strokes)
        {
            var nonScriptable = (IMsRdpClientNonScriptable)GetRequiredOcx();
            var keyUpStates = new short[strokes.Length];
            var keyData = new int[strokes.Length];

            for (var index = 0; index < strokes.Length; index++)
            {
                var stroke = strokes[index];
                keyUpStates[index] = stroke.IsKeyUp ? (short)1 : (short)0;
                keyData[index] = stroke.Key.ScanCode | (stroke.Key.IsExtended ? 0x0100 : 0);
            }

            nonScriptable.SendKeys(strokes.Length, ref keyUpStates[0], ref keyData[0]);
        }

        protected override void CreateSink()
        {
            base.CreateSink();
            _eventSink = new RdpEventSink(this);
            _eventCookie = new ConnectionPointCookie(
                GetOcx(),
                _eventSink,
                typeof(IMsTscAxEvents));
        }

        protected override void DetachSink()
        {
            try
            {
                if (_eventCookie != null)
                {
                    _eventCookie.Disconnect();
                    _eventCookie = null;
                }
                _eventSink = null;
            }
            finally
            {
                base.DetachSink();
            }
        }

        private object GetRequiredOcx()
        {
            if (!IsHandleCreated)
            {
                _ = Handle;
            }
            var ocx = GetOcx();
            if (ocx == null)
            {
                throw new InvalidOperationException("RDP ActiveX 控件尚未初始化。");
            }
            return ocx;
        }

        private static object GetComProperty(object target, string propertyName)
        {
            return target.GetType().InvokeMember(
                propertyName,
                BindingFlags.GetProperty,
                null,
                target,
                null,
                CultureInfo.InvariantCulture);
        }

        private static void SetComProperty(object target, string propertyName, object value)
        {
            target.GetType().InvokeMember(
                propertyName,
                BindingFlags.SetProperty,
                null,
                target,
                new object[] { value },
                CultureInfo.InvariantCulture);
        }

        private static void TrySetExtendedProperty(
            IMsRdpExtendedSettings extendedSettings,
            string propertyName,
            object value)
        {
            try
            {
                extendedSettings.set_Property(propertyName, ref value);
            }
            catch { }
        }

        private static object InvokeComMethod(object target, string methodName, params object[] args)
        {
            return target.GetType().InvokeMember(
                methodName,
                BindingFlags.InvokeMethod,
                null,
                target,
                args,
                CultureInfo.InvariantCulture);
        }

        // ======================== 事件响应 ========================
        internal void OnLoginCompleteInternal()
        {
            LoginCompleted?.Invoke(this, EventArgs.Empty);
        }

        internal void OnConnectedInternal()
        {
            Connected?.Invoke(this, EventArgs.Empty);
        }

        internal void OnDisconnectedInternal(int reason)
        {
            if (_disconnectRequested)
            {
                _disconnectRequested = false;
                if (_pendingReconnectDesktopSize.HasValue && !IsDisposed && !Disposing)
                {
                    try
                    {
                        BeginInvoke(new Action(ConnectPendingReconnect));
                    }
                    catch (InvalidOperationException)
                    {
                        _pendingReconnectDesktopSize = null;
                    }
                }
                Disconnected?.Invoke(this, EventArgs.Empty);
                return;
            }

            Disconnected?.Invoke(this, EventArgs.Empty);
        }

        private async void ConnectPendingReconnect()
        {
            await System.Threading.Tasks.Task.Delay(800);

            var desktopSize = _pendingReconnectDesktopSize;
            _pendingReconnectDesktopSize = null;
            if (!desktopSize.HasValue || IsDisposed || Disposing)
            {
                return;
            }

            try
            {
                ConnectToChildSession(desktopSize.Value);
            }
            catch
            {
                ConnectionFailed?.Invoke(this, EventArgs.Empty);
            }
        }

        internal void OnFatalErrorInternal(int errorCode)
        {
            ConnectionFailed?.Invoke(this, EventArgs.Empty);
        }

        // ======================== COM 接口定义 ========================
        [ComImport]
        [Guid("302D8188-0052-4807-806A-362B628F9AC5")]
        [InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
        private interface IMsRdpExtendedSettings
        {
            void set_Property(
                [In, MarshalAs(UnmanagedType.BStr)] string propertyName,
                [In, MarshalAs(UnmanagedType.Struct)] ref object value);

            [return: MarshalAs(UnmanagedType.Struct)]
            object get_Property([In, MarshalAs(UnmanagedType.BStr)] string propertyName);
        }

        [ComImport]
        [Guid("2F079C4C-87B2-4AFD-97AB-20CDB43038AE")]
        [InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
        private interface IMsRdpClientNonScriptable
        {
            void put_ClearTextPassword([In, MarshalAs(UnmanagedType.BStr)] string value);
            void put_PortablePassword([In, MarshalAs(UnmanagedType.BStr)] string value);
            [return: MarshalAs(UnmanagedType.BStr)] string get_PortablePassword();
            void put_PortableSalt([In, MarshalAs(UnmanagedType.BStr)] string value);
            [return: MarshalAs(UnmanagedType.BStr)] string get_PortableSalt();
            void put_BinaryPassword([In, MarshalAs(UnmanagedType.BStr)] string value);
            [return: MarshalAs(UnmanagedType.BStr)] string get_BinaryPassword();
            void put_BinarySalt([In, MarshalAs(UnmanagedType.BStr)] string value);
            [return: MarshalAs(UnmanagedType.BStr)] string get_BinarySalt();
            void ResetPassword();
            void NotifyRedirectDeviceChange(UIntPtr wParam, IntPtr lParam);
            void SendKeys(int numKeys, [In] ref short keyUpStates, [In] ref int keyData);
        }

        [ComImport]
        [Guid("336D5562-EFA8-482E-8CB3-C5C0FC7A7DB6")]
        [InterfaceType(ComInterfaceType.InterfaceIsIDispatch)]
        [TypeLibType(TypeLibTypeFlags.FDispatchable)]
        private interface IMsTscAxEvents
        {
            [DispId(1)] void OnConnecting();
            [DispId(2)] void OnConnected();
            [DispId(3)] void OnLoginComplete();
            [DispId(4)] void OnDisconnected([In] int disconnectReason);
            [DispId(5)] void OnEnterFullScreenMode();
            [DispId(6)] void OnLeaveFullScreenMode();
            [DispId(7)] void OnChannelReceivedData([In, MarshalAs(UnmanagedType.BStr)] string channelName, [In, MarshalAs(UnmanagedType.BStr)] string data);
            [DispId(8)] void OnRequestGoFullScreen();
            [DispId(9)] void OnRequestLeaveFullScreen();
            [DispId(10)] void OnFatalError([In] int errorCode);
            [DispId(11)] void OnWarning([In] int warningCode);
            [DispId(12)] void OnRemoteDesktopSizeChange([In] int width, [In] int height);
            [DispId(13)] void OnIdleTimeoutNotification();
            [DispId(14)] void OnRequestContainerMinimize();
            [DispId(15)] void OnConfirmClose([Out] out bool allowClose);
            [DispId(16)] void OnReceivedTSPublicKey([In, MarshalAs(UnmanagedType.BStr)] string publicKey, [Out] out bool continueLogon);
            [DispId(17)] void OnAutoReconnecting([In] int disconnectReason, [In] int attemptCount, [Out] out int continueStatus);
            [DispId(18)] void OnAuthenticationWarningDisplayed();
            [DispId(19)] void OnAuthenticationWarningDismissed();
            [DispId(20)] void OnRemoteProgramResult([In, MarshalAs(UnmanagedType.BStr)] string remoteProgram, [In] int error, [In] bool isExecutable);
            [DispId(21)] void OnRemoteProgramDisplayed([In] bool displayed, [In] uint displayInformation);
            [DispId(29)] void OnRemoteWindowDisplayed([In] bool displayed, [In] IntPtr windowHandle, [In] int windowAttribute);
            [DispId(22)] void OnLogonError([In] int errorCode);
            [DispId(23)] void OnFocusReleased([In] int direction);
            [DispId(24)] void OnUserNameAcquired([In, MarshalAs(UnmanagedType.BStr)] string userName);
            [DispId(26)] void OnMouseInputModeChanged([In] bool isRelativeMouseMode);
            [DispId(28)] void OnServiceMessageReceived([In, MarshalAs(UnmanagedType.BStr)] string serviceMessage);
            [DispId(30)] void OnConnectionBarPullDown();
            [DispId(32)] void OnNetworkStatusChanged([In] uint qualityLevel, [In] int bandwidth, [In] int roundTripTime);
            [DispId(35)] void OnDevicesButtonPressed();
            [DispId(33)] void OnAutoReconnected();
            [DispId(34)] void OnAutoReconnecting2([In] int disconnectReason, [In] bool networkAvailable, [In] int attemptCount, [In] int maxAttemptCount);
        }

        [ComVisible(true)]
        [ClassInterface(ClassInterfaceType.None)]
        private sealed class RdpEventSink : IMsTscAxEvents
        {
            private readonly RdpActiveXHost _owner;

            public RdpEventSink(RdpActiveXHost owner)
            {
                _owner = owner;
            }

            public void OnConnecting() { }
            public void OnConnected() { _owner.OnConnectedInternal(); }
            public void OnLoginComplete() { _owner.OnLoginCompleteInternal(); }
            public void OnDisconnected(int disconnectReason) { _owner.OnDisconnectedInternal(disconnectReason); }
            public void OnEnterFullScreenMode() { }
            public void OnLeaveFullScreenMode() { }
            public void OnChannelReceivedData(string channelName, string data) { }
            public void OnRequestGoFullScreen() { }
            public void OnRequestLeaveFullScreen() { }
            public void OnFatalError(int errorCode) { _owner.OnFatalErrorInternal(errorCode); }
            public void OnWarning(int warningCode) { }
            public void OnRemoteDesktopSizeChange(int width, int height) { }
            public void OnIdleTimeoutNotification() { }
            public void OnRequestContainerMinimize() { }
            public void OnConfirmClose(out bool allowClose) { allowClose = true; }
            public void OnReceivedTSPublicKey(string publicKey, out bool continueLogon) { continueLogon = true; }
            public void OnAutoReconnecting(int disconnectReason, int attemptCount, out int continueStatus) { continueStatus = 0; }
            public void OnAuthenticationWarningDisplayed() { }
            public void OnAuthenticationWarningDismissed() { }
            public void OnRemoteProgramResult(string remoteProgram, int error, bool isExecutable) { }
            public void OnRemoteProgramDisplayed(bool displayed, uint displayInformation) { }
            public void OnRemoteWindowDisplayed(bool displayed, IntPtr windowHandle, int windowAttribute) { }
            public void OnLogonError(int errorCode) { }
            public void OnFocusReleased(int direction) { }
            public void OnUserNameAcquired(string userName) { }
            public void OnMouseInputModeChanged(bool isRelativeMouseMode) { }
            public void OnServiceMessageReceived(string serviceMessage) { }
            public void OnConnectionBarPullDown() { }
            public void OnNetworkStatusChanged(uint qualityLevel, int bandwidth, int roundTripTime) { }
            public void OnDevicesButtonPressed() { }
            public void OnAutoReconnected() { _owner.OnLoginCompleteInternal(); }
            public void OnAutoReconnecting2(int disconnectReason, bool networkAvailable, int attemptCount, int maxAttemptCount) { }
        }

        private struct RemoteKey
        {
            public int ScanCode;
            public bool IsExtended;
            public RemoteKey(int scanCode, bool isExtended)
            {
                ScanCode = scanCode;
                IsExtended = isExtended;
            }
        }

        private struct KeyStroke
        {
            public RemoteKey Key;
            public bool IsKeyUp;
            public KeyStroke(RemoteKey key, bool isKeyUp)
            {
                Key = key;
                IsKeyUp = isKeyUp;
            }
        }
    }
}
