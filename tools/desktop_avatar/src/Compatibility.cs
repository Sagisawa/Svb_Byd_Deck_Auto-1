using System;

namespace System.Runtime.CompilerServices
{
    // 用于支持 C# 9+ 的 init 属性和 record
    internal static class IsExternalInit { }
}

namespace DesktopAvatar
{
    internal static class MathHelper
    {
        public static int Clamp(int value, int min, int max)
        {
            if (value < min) return min;
            if (value > max) return max;
            return value;
        }
    }
}
