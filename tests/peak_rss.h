// SPDX-License-Identifier: GPL-3.0-only
#pragma once
#include <QtGlobal>
#ifdef Q_OS_WIN
#ifndef NOMINMAX
#define NOMINMAX
#endif
#include <windows.h>
#include <psapi.h>
#else
#include <sys/resource.h>
#endif
#include <stdexcept>
inline qint64 processPeakRssBytes() {
#ifdef Q_OS_WIN
    PROCESS_MEMORY_COUNTERS counters{}; counters.cb=DWORD(sizeof(counters));
    if(!GetProcessMemoryInfo(GetCurrentProcess(),&counters,counters.cb)) throw std::runtime_error("Cannot read process working set");
    return qint64(counters.PeakWorkingSetSize);
#else
    rusage usage{};
    if(getrusage(RUSAGE_SELF,&usage)!=0) throw std::runtime_error("Cannot read process RSS");
#ifdef Q_OS_MACOS
    return usage.ru_maxrss;
#else
    return qint64(usage.ru_maxrss)*1024;
#endif
#endif
}
