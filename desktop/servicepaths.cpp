// SPDX-License-Identifier: GPL-3.0-only
#include "servicepaths.h"
#include <QDir>
#include <QFileInfo>
#include <QStandardPaths>
#include <QStringList>

ServicePaths resolveServicePaths(const QString &applicationDir,const QString &pythonOverride) {
    ServicePaths result;
    const auto app=QDir(applicationDir).absolutePath();
    const auto build=QDir(QString::fromUtf8(DATAHORIZON_BUILD_DIR)).absolutePath();
    const QStringList developmentDirs{build,build+"/"+QString::fromUtf8(DATAHORIZON_BUILD_CONFIG),build+"/Debug",build+"/Release",build+"/RelWithDebInfo",build+"/MinSizeRel",build+"/DataHorizon.app/Contents/MacOS"};
    bool development=false;
    for(const auto &directory:developmentDirs) {
#ifdef Q_OS_WIN
        development=development || directory.compare(app,Qt::CaseInsensitive)==0;
#else
        development=development || directory==app;
#endif
    }
    const QStringList candidates{app+"/../Resources/service",app+"/../share/datahorizon/service"};
    QString root;
    for(const auto &candidate:candidates) if(QDir(candidate).exists()) {root=QDir(candidate).absolutePath();break;}
    if(root.isEmpty() && development) {root=QString::fromUtf8(DATAHORIZON_SERVICE_DIR);result.developmentFallback=true;}
    result.launcher=QDir(root).absoluteFilePath("datahorizon/launch.py");
    if(root.isEmpty() || !QFileInfo(result.launcher).isFile()) {
        result.error="Service installation is incomplete: datahorizon/launch.py is missing. Reinstall the application."; return result;
    }
    if(!pythonOverride.isEmpty()) {result.python=pythonOverride;result.runtimeSource="override";return result;}
    const auto runtime=QFileInfo(root).dir().absoluteFilePath("runtime");
    if(QDir(runtime).exists()) {
#ifdef Q_OS_WIN
        result.python=runtime+"/python.exe";
#else
        result.python=runtime+"/bin/python3";
#endif
        result.runtimeSource="bundled";
        if(!QFileInfo(result.python).isFile()) result.error="The installed Python runtime is incomplete. Reinstall it or set DATAHORIZON_PYTHON to a trusted Python 3.11+ interpreter.";
        return result;
    }
    if(development) {result.python=QString::fromUtf8(DATAHORIZON_PYTHON);result.runtimeSource="development";return result;}
    result.python=QStandardPaths::findExecutable("python3");
#ifdef Q_OS_WIN
    if(result.python.isEmpty()) result.python=QStandardPaths::findExecutable("python");
#endif
    result.runtimeSource="external";
    if(result.python.isEmpty()) result.error="Python 3.11+ with SQLite is required. Install it or set DATAHORIZON_PYTHON to a trusted interpreter.";
    return result;
}
