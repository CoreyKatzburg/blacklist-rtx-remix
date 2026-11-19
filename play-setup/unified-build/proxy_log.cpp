#include "proxy_log.h"

#include <cstdarg>
#include <cstdio>
#include <cstring>
#include <cwchar>
#include <deque>
#include <string>
#include <vector>
namespace proxy {
namespace {

wchar_t g_executableDirectory[MAX_PATH] = {};
wchar_t g_iniPath[MAX_PATH] = {};
wchar_t g_logPath[MAX_PATH] = {};
bool g_loggingEnabled = true;
SRWLOCK g_logLock = SRWLOCK_INIT;
CONDITION_VARIABLE g_logCv = CONDITION_VARIABLE_INIT;
PVOID g_vectoredExceptionHandler = nullptr;
LONG g_handlingException = 0;

struct WriteItem {
    wchar_t path[MAX_PATH];
    std::string bytes;
};
struct OpenFileEntry {
    wchar_t path[MAX_PATH];
    HANDLE handle;
};
std::deque<WriteItem>* g_writeQueue = nullptr;
std::vector<OpenFileEntry>* g_openHandles = nullptr;
SRWLOCK g_handleLock = SRWLOCK_INIT;
volatile LONG g_servicesStarted = 0;
volatile LONG g_writerStop = 0;
volatile LONG g_iniDirty = 0;
volatile LONG g_iniEpoch = 1;
HANDLE g_writerThread = nullptr;

HANDLE GetPersistentAppendHandle(const wchar_t* path) {
    AcquireSRWLockExclusive(&g_handleLock);
    if (!g_openHandles) g_openHandles = new std::vector<OpenFileEntry>();
    for (const auto& entry : *g_openHandles) {
        if (_wcsicmp(entry.path, path) == 0 && entry.handle != INVALID_HANDLE_VALUE) {
            const HANDLE h = entry.handle;
            ReleaseSRWLockExclusive(&g_handleLock);
            return h;
        }
    }
    const HANDLE h = CreateFileW(path, FILE_APPEND_DATA,
                                 FILE_SHARE_READ | FILE_SHARE_WRITE | FILE_SHARE_DELETE,
                                 nullptr, OPEN_ALWAYS, FILE_ATTRIBUTE_NORMAL, nullptr);
    if (h != INVALID_HANDLE_VALUE) {
        OpenFileEntry e = {};
        wcscpy_s(e.path, path);
        e.handle = h;
        g_openHandles->push_back(e);
    }
    ReleaseSRWLockExclusive(&g_handleLock);
    return h;
}

void WriteDirectPersistent(const wchar_t* path, const char* data, size_t len) {
    if (!len || !path || !path[0]) return;
    const HANDLE h = GetPersistentAppendHandle(path);
    if (h != INVALID_HANDLE_VALUE) {
        DWORD written = 0;
        WriteFile(h, data, static_cast<DWORD>(len), &written, nullptr);
    }
}

void EnqueueAsyncWrite(const wchar_t* path, const char* data, size_t len) {
    if (!len || !path || !path[0]) return;
    if (!g_servicesStarted || g_writerStop) {
        WriteDirectPersistent(path, data, len);
        return;
    }
    AcquireSRWLockExclusive(&g_logLock);
    if (!g_writeQueue) g_writeQueue = new std::deque<WriteItem>();
    if (!g_writeQueue->empty() && _wcsicmp(g_writeQueue->back().path, path) == 0 &&
        g_writeQueue->back().bytes.size() + len <= 65536) {
        g_writeQueue->back().bytes.append(data, len);
    } else {
        WriteItem item = {};
        wcscpy_s(item.path, path);
        item.bytes.assign(data, len);
        g_writeQueue->push_back(static_cast<WriteItem&&>(item));
    }
    ReleaseSRWLockExclusive(&g_logLock);
    WakeConditionVariable(&g_logCv);
}

DWORD WINAPI AsyncLogWriterProc(void*) {
    std::deque<WriteItem> local;
    for (;;) {
        AcquireSRWLockExclusive(&g_logLock);
        while ((!g_writeQueue || g_writeQueue->empty()) && !g_writerStop) {
            SleepConditionVariableSRW(&g_logCv, &g_logLock, INFINITE, 0);
        }
        if (g_writeQueue && !g_writeQueue->empty()) local.swap(*g_writeQueue);
        const bool stop = g_writerStop != 0 && (!g_writeQueue || g_writeQueue->empty());
        ReleaseSRWLockExclusive(&g_logLock);
        for (const auto& item : local) {
            WriteDirectPersistent(item.path, item.bytes.data(), item.bytes.size());
        }
        local.clear();
        if (stop) break;
    }
    return 0;
}

DWORD WINAPI IniWatcherProc(void*) {
    if (!g_executableDirectory[0]) return 0;
    const HANDLE dir = CreateFileW(
        g_executableDirectory, FILE_LIST_DIRECTORY,
        FILE_SHARE_READ | FILE_SHARE_WRITE | FILE_SHARE_DELETE,
        nullptr, OPEN_EXISTING, FILE_FLAG_BACKUP_SEMANTICS, nullptr);
    if (dir == INVALID_HANDLE_VALUE) return 0;
    alignas(DWORD) BYTE buffer[4096];
    FILETIME lastIniWrite = {};
    WIN32_FILE_ATTRIBUTE_DATA attr = {};
    if (GetFileAttributesExW(g_iniPath, GetFileExInfoStandard, &attr)) lastIniWrite = attr.ftLastWriteTime;
    while (!g_writerStop) {
        DWORD returned = 0;
        const BOOL ok = ReadDirectoryChangesW(
            dir, buffer, sizeof(buffer), FALSE,
            FILE_NOTIFY_CHANGE_LAST_WRITE | FILE_NOTIFY_CHANGE_FILE_NAME | FILE_NOTIFY_CHANGE_CREATION,
            &returned, nullptr, nullptr);
        if (!ok || g_writerStop) break;
        bool maybeIni = returned == 0;  // overflow -> check anyway
        for (DWORD off = 0; !maybeIni && off + sizeof(FILE_NOTIFY_INFORMATION) <= returned;) {
            const auto* info = reinterpret_cast<const FILE_NOTIFY_INFORMATION*>(buffer + off);
            const size_t chars = info->FileNameLength / sizeof(wchar_t);
            if (chars == 14 && _wcsnicmp(info->FileName, L"d3d9_proxy.ini", 14) == 0) maybeIni = true;
            if (!info->NextEntryOffset) break;
            off += info->NextEntryOffset;
        }
        if (maybeIni) {
            WIN32_FILE_ATTRIBUTE_DATA nowAttr = {};
            if (GetFileAttributesExW(g_iniPath, GetFileExInfoStandard, &nowAttr) &&
                CompareFileTime(&nowAttr.ftLastWriteTime, &lastIniWrite) != 0) {
                lastIniWrite = nowAttr.ftLastWriteTime;
                InterlockedIncrement(&g_iniEpoch);
                InterlockedExchange(&g_iniDirty, 1);
            }
        }
    }
    CloseHandle(dir);
    return 0;
}
LONG CALLBACK LogVectoredException(EXCEPTION_POINTERS* exceptionPointers) {
    if (!exceptionPointers || !exceptionPointers->ExceptionRecord ||
        !exceptionPointers->ContextRecord ||
        InterlockedCompareExchange(&g_handlingException, 1, 0) != 0) {
        return EXCEPTION_CONTINUE_SEARCH;
    }

    EXCEPTION_RECORD* record = exceptionPointers->ExceptionRecord;
    CONTEXT* context = exceptionPointers->ContextRecord;
    const DWORD code = record->ExceptionCode;
    if (code != EXCEPTION_ACCESS_VIOLATION &&
        code != EXCEPTION_IN_PAGE_ERROR &&
        code != EXCEPTION_ILLEGAL_INSTRUCTION &&
        code != EXCEPTION_STACK_OVERFLOW) {
        InterlockedExchange(&g_handlingException, 0);
        return EXCEPTION_CONTINUE_SEARCH;
    }
    wchar_t module[MAX_PATH] = L"unknown";
    uintptr_t offset = 0;
    bool executable = false;
    const uintptr_t exceptionAddress =
        reinterpret_cast<uintptr_t>(record->ExceptionAddress);
    const bool mapped = DescribeMappedAddress(exceptionAddress, module,
                                               &offset, &executable);
    WriteCrashLog("exception code=0x%08lX flags=0x%08lX address=0x%08lX (%ls%s0x%08lX)",
                  record->ExceptionCode, record->ExceptionFlags,
                  static_cast<unsigned long>(exceptionAddress),
                  mapped ? module : L"unknown", mapped ? L"+" : L" ",
                  static_cast<unsigned long>(offset));

    if (record->ExceptionCode == EXCEPTION_ACCESS_VIOLATION &&
        record->NumberParameters >= 2) {
        const ULONG_PTR operation = record->ExceptionInformation[0];
        const char* operationName = operation == 0 ? "read" :
                                    operation == 1 ? "write" :
                                    operation == 8 ? "execute" : "unknown";
        WriteCrashLog("access violation operation=%s(%lu) address=0x%08lX",
                      operationName, static_cast<unsigned long>(operation),
                      static_cast<unsigned long>(record->ExceptionInformation[1]));
    }

#if defined(_M_IX86)
    WriteCrashLog("registers EAX=%08lX EBX=%08lX ECX=%08lX EDX=%08lX "
                  "ESI=%08lX EDI=%08lX EBP=%08lX ESP=%08lX EIP=%08lX EFLAGS=%08lX",
                  context->Eax, context->Ebx, context->Ecx, context->Edx,
                  context->Esi, context->Edi, context->Ebp, context->Esp,
                  context->Eip, context->EFlags);

    const uintptr_t stackPointer = context->Esp;
    unsigned mappedReturns = 0;
    for (unsigned i = 0; i < 96 && mappedReturns < 24; ++i) {
        uintptr_t candidate = 0;
        __try {
            candidate = reinterpret_cast<const uintptr_t*>(stackPointer)[i];
        } __except (EXCEPTION_EXECUTE_HANDLER) {
            break;
        }
        wchar_t candidateModule[MAX_PATH];
        uintptr_t candidateOffset = 0;
        bool candidateExecutable = false;
        if (DescribeMappedAddress(candidate, candidateModule, &candidateOffset,
                                  &candidateExecutable) && candidateExecutable) {
            WriteCrashLog("stack +0x%03X = 0x%08lX -> %ls+0x%08lX",
                          i * sizeof(uintptr_t),
                          static_cast<unsigned long>(candidate), candidateModule,
                          static_cast<unsigned long>(candidateOffset));
            ++mappedReturns;
        }
    }
    WriteCrashLog("stack scan complete: %u executable module candidates", mappedReturns);
#else
    (void)context;
#endif

    InterlockedExchange(&g_handlingException, 0);
    return EXCEPTION_CONTINUE_SEARCH;
}

}  // namespace

bool CombinePath(const wchar_t* directory, const wchar_t* leaf, wchar_t output[MAX_PATH]) {
    const size_t directoryLength = wcslen(directory);
    const size_t leafLength = wcslen(leaf);
    const bool needsSlash = directoryLength != 0 &&
                            directory[directoryLength - 1] != L'\\' &&
                            directory[directoryLength - 1] != L'/';
    if (directoryLength + (needsSlash ? 1 : 0) + leafLength >= MAX_PATH) {
        return false;
    }
    wcscpy_s(output, MAX_PATH, directory);
    if (needsSlash) {
        wcscat_s(output, MAX_PATH, L"\\");
    }
    wcscat_s(output, MAX_PATH, leaf);
    return true;
}

bool g_replayMode = false;
bool ReplayMode() { return g_replayMode; }

bool GetExecutableDirectory(HMODULE proxyModule) {
    wchar_t replay[4];
    g_replayMode = GetEnvironmentVariableW(L"BLPROXY_REPLAY", replay, ARRAYSIZE(replay)) == 1 && replay[0] == L'1';
    wchar_t executablePath[MAX_PATH];
    const DWORD length = GetModuleFileNameW(g_replayMode ? proxyModule : nullptr, executablePath, ARRAYSIZE(executablePath));
    if (length == 0 || length >= ARRAYSIZE(executablePath)) {
        return false;
    }
    wchar_t* slash = wcsrchr(executablePath, L'\\');
    if (!slash) {
        slash = wcsrchr(executablePath, L'/');
    }
    if (!slash) {
        return false;
    }
    *slash = L'\0';
    wcscpy_s(g_executableDirectory, executablePath);
    return CombinePath(g_executableDirectory, L"d3d9_proxy.ini", g_iniPath) &&
           CombinePath(g_executableDirectory, L"d3d9_proxy.log", g_logPath);
}

const wchar_t* ExecutableDirectory() { return g_executableDirectory; }
const wchar_t* IniPath() { return g_iniPath; }
const wchar_t* LogPath() { return g_logPath; }

void SetLoggingEnabled(bool enabled) { g_loggingEnabled = enabled; }
bool LoggingEnabled() { return g_loggingEnabled; }

bool IsAbsolutePath(const wchar_t* path) {
    return (path[0] && path[1] == L':') ||
           (path[0] == L'\\' && path[1] == L'\\');
}

bool ResolveConfiguredPath(const wchar_t* configured, wchar_t output[MAX_PATH]) {
    wchar_t combined[MAX_PATH];
    const wchar_t* source = configured;
    if (!IsAbsolutePath(configured)) {
        if (!CombinePath(g_executableDirectory, configured, combined)) {
            return false;
        }
        source = combined;
    }
    const DWORD length = GetFullPathNameW(source, MAX_PATH, output, nullptr);
    return length != 0 && length < MAX_PATH;
}

bool GetSystemD3D9Path(wchar_t output[MAX_PATH]) {
    wchar_t directory[MAX_PATH];
    const UINT length = GetSystemDirectoryW(directory, ARRAYSIZE(directory));
    return length != 0 && length < ARRAYSIZE(directory) &&
           CombinePath(directory, L"d3d9.dll", output);
}

void DumpFolder(const wchar_t* name, wchar_t* out, size_t count) {
    wcscpy_s(out, count, g_iniPath);
    wchar_t* slash = wcsrchr(out, L'\\');
    if (slash) *slash = 0;
    wcscat_s(out, count, L"\\");
    wcscat_s(out, count, name);
    CreateDirectoryW(out, nullptr);
}

void WriteLog(const char* format, ...) {
    if (!g_loggingEnabled || g_logPath[0] == L'\0') {
        return;
    }

    char message[768];
    va_list arguments;
    va_start(arguments, format);
    _vsnprintf_s(message, ARRAYSIZE(message), _TRUNCATE, format, arguments);
    va_end(arguments);

    SYSTEMTIME time;
    GetLocalTime(&time);
    char line[1024];
    const int lineLength = _snprintf_s(
        line, ARRAYSIZE(line), _TRUNCATE,
        "%04u-%02u-%02u %02u:%02u:%02u.%03u [pid %lu] %s\r\n",
        time.wYear, time.wMonth, time.wDay, time.wHour, time.wMinute,
        time.wSecond, time.wMilliseconds, GetCurrentProcessId(), message);
    if (lineLength <= 0) {
        return;
    }

    EnqueueAsyncWrite(g_logPath, line, static_cast<size_t>(lineLength));
}

// Exception-path logging deliberately avoids g_logLock: an access violation may
// have interrupted a thread while it held that lock.
void WriteCrashLog(const char* format, ...) {
    if (!g_loggingEnabled || g_logPath[0] == L'\0') {
        return;
    }
    char message[768];
    va_list arguments;
    va_start(arguments, format);
    _vsnprintf_s(message, ARRAYSIZE(message), _TRUNCATE, format, arguments);
    va_end(arguments);

    SYSTEMTIME time;
    GetLocalTime(&time);
    char line[1024];
    const int lineLength = _snprintf_s(
        line, ARRAYSIZE(line), _TRUNCATE,
        "%04u-%02u-%02u %02u:%02u:%02u.%03u [pid %lu] EXCEPTION(first-chance) %s\r\n",
        time.wYear, time.wMonth, time.wDay, time.wHour, time.wMinute,
        time.wSecond, time.wMilliseconds, GetCurrentProcessId(), message);
    if (lineLength <= 0) {
        return;
    }
    HANDLE file = CreateFileW(g_logPath, FILE_APPEND_DATA,
                              FILE_SHARE_READ | FILE_SHARE_WRITE | FILE_SHARE_DELETE,
                              nullptr, OPEN_ALWAYS, FILE_ATTRIBUTE_NORMAL, nullptr);
    if (file != INVALID_HANDLE_VALUE) {
        DWORD written;
        WriteFile(file, line, static_cast<DWORD>(lineLength), &written, nullptr);
        CloseHandle(file);
    }
}

void AppendToFile(const wchar_t* path, const char* data, size_t* length) {
    if (*length == 0) {
        return;
    }
    EnqueueAsyncWrite(path, data, *length);
    *length = 0;
}

void StartBackgroundLogAndIniServices() {
    if (InterlockedCompareExchange(&g_servicesStarted, 1, 0) != 0) return;
    g_writerThread = CreateThread(nullptr, 0, &AsyncLogWriterProc, nullptr, 0, nullptr);
    if (g_writerThread) SetThreadPriority(g_writerThread, THREAD_PRIORITY_BELOW_NORMAL);
    const HANDLE watcher = CreateThread(nullptr, 0, &IniWatcherProc, nullptr, 0, nullptr);
    if (watcher) {
        SetThreadPriority(watcher, THREAD_PRIORITY_BELOW_NORMAL);
        CloseHandle(watcher);
    }
}

bool ConsumeIniChanged() {
    return InterlockedExchange(&g_iniDirty, 0) != 0;
}

uint32_t PeekIniChangeEpoch() {
    return static_cast<uint32_t>(InterlockedCompareExchange(&g_iniEpoch, 0, 0));
}

void FlushLogs(bool shuttingDown) {
    if (shuttingDown) InterlockedExchange(&g_writerStop, 1);
    WakeAllConditionVariable(&g_logCv);
    if (!TryAcquireSRWLockExclusive(&g_logLock)) return;
    std::deque<WriteItem> local;
    if (g_writeQueue && !g_writeQueue->empty()) local.swap(*g_writeQueue);
    ReleaseSRWLockExclusive(&g_logLock);
    for (const auto& item : local) {
        WriteDirectPersistent(item.path, item.bytes.data(), item.bytes.size());
    }
}
bool IsExecutableProtection(DWORD protection) {
    const DWORD base = protection & 0xffu;
    return base == PAGE_EXECUTE || base == PAGE_EXECUTE_READ ||
           base == PAGE_EXECUTE_READWRITE || base == PAGE_EXECUTE_WRITECOPY;
}

bool DescribeMappedAddress(uintptr_t address,
                           wchar_t moduleName[MAX_PATH],
                           uintptr_t* moduleOffset,
                           bool* executable) {
    MEMORY_BASIC_INFORMATION memory = {};
    if (!address || VirtualQuery(reinterpret_cast<const void*>(address), &memory,
                                 sizeof(memory)) != sizeof(memory) ||
        memory.State != MEM_COMMIT) {
        return false;
    }
    *executable = IsExecutableProtection(memory.Protect);
    const HMODULE module = static_cast<HMODULE>(memory.AllocationBase);
    wchar_t modulePath[MAX_PATH];
    const DWORD length = GetModuleFileNameW(module, modulePath, ARRAYSIZE(modulePath));
    if (length == 0 || length >= ARRAYSIZE(modulePath)) {
        return false;
    }
    const wchar_t* baseName = wcsrchr(modulePath, L'\\');
    baseName = baseName ? baseName + 1 : modulePath;
    wcscpy_s(moduleName, MAX_PATH, baseName);
    *moduleOffset = address - reinterpret_cast<uintptr_t>(module);
    return true;
}

void DescribeAddress(void* address, char* text, size_t size) {
    HMODULE module = nullptr;
    char path[MAX_PATH] = "?";
    if (address && GetModuleHandleExA(GET_MODULE_HANDLE_EX_FLAG_FROM_ADDRESS |
                                          GET_MODULE_HANDLE_EX_FLAG_UNCHANGED_REFCOUNT,
                                      static_cast<const char*>(address), &module)) {
        GetModuleFileNameA(module, path, ARRAYSIZE(path));
    }
    const char* name = strrchr(path, '\\');
    name = name ? name + 1 : path;
    _snprintf_s(text, size, _TRUNCATE, "%s+0x%IX", name,
                reinterpret_cast<uintptr_t>(address) - reinterpret_cast<uintptr_t>(module));
}

void InstallCrashHandler() {
    g_vectoredExceptionHandler = AddVectoredExceptionHandler(1, LogVectoredException);
    WriteLog("vectored exception handler %s (%p)",
             g_vectoredExceptionHandler ? "installed" : "FAILED",
             g_vectoredExceptionHandler);
}

void RemoveCrashHandler() {
    if (g_vectoredExceptionHandler) {
        RemoveVectoredExceptionHandler(g_vectoredExceptionHandler);
        g_vectoredExceptionHandler = nullptr;
    }
}

}  // namespace proxy
