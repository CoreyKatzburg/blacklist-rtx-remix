#pragma once
// Path resolution, timestamped file/crash logging, vectored exception reporting,
// and loaded-module address formatting for the D3D9 proxy.

#define WIN32_LEAN_AND_MEAN
#define NOMINMAX
#include <windows.h>
#include <cstddef>
#include <cstdint>

namespace proxy {

// Resolves the game executable directory, d3d9_proxy.ini path, and d3d9_proxy.log path.
bool GetExecutableDirectory();
const wchar_t* ExecutableDirectory();
const wchar_t* IniPath();
const wchar_t* LogPath();

void SetLoggingEnabled(bool enabled);
bool LoggingEnabled();

bool CombinePath(const wchar_t* directory, const wchar_t* leaf, wchar_t output[MAX_PATH]);
bool IsAbsolutePath(const wchar_t* path);
bool ResolveConfiguredPath(const wchar_t* configured, wchar_t output[MAX_PATH]);
bool GetSystemD3D9Path(wchar_t output[MAX_PATH]);

// Creates <exe_dir>\<name> if missing and writes its path into out[0..count-1].
void DumpFolder(const wchar_t* name, wchar_t* out, size_t count);

void WriteLog(const char* format, ...);
void WriteCrashLog(const char* format, ...);
void AppendToFile(const wchar_t* path, const char* data, size_t* length);
// Background directory-watcher notification for d3d9_proxy.ini edits (zero render-thread disk reads)
// and async background file writer control.
void StartBackgroundLogAndIniServices();
bool ConsumeIniChanged();
uint32_t PeekIniChangeEpoch();
void FlushLogs(bool shuttingDown);

bool IsExecutableProtection(DWORD protection);
bool DescribeMappedAddress(uintptr_t address,
                           wchar_t moduleName[MAX_PATH],
                           uintptr_t* moduleOffset,
                           bool* executable);
void DescribeAddress(void* address, char* text, size_t size);

// Installs/removes the first-chance vectored exception logger for crash diagnostics.
void InstallCrashHandler();
void RemoveCrashHandler();

}  // namespace proxy
