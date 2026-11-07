// SPDX-License-Identifier: GPL-3.0-only
#pragma once
#include <QString>

struct ServicePaths {
    QString python, launcher, runtimeSource, error;
    bool developmentFallback=false;
};
ServicePaths resolveServicePaths(const QString &applicationDir, const QString &pythonOverride = {});
