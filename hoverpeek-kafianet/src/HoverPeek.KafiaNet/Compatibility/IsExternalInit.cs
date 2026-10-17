#if NETFRAMEWORK
namespace System.Runtime.CompilerServices
{
    // Enables C# record/init metadata when targeting .NET Framework 4.8.
    internal static class IsExternalInit { }
}
#endif
