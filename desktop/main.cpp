// SPDX-License-Identifier: GPL-3.0-only
#include "mainwindow.h"
#include "servicepaths.h"
#include <QApplication>
#include <QCommandLineParser>
#include <QIcon>
#include <QJsonArray>
#include <QTemporaryDir>
#include <QTimer>

int main(int argc, char **argv) {
    QApplication app(argc, argv);
    app.setOrganizationName("DataHorizon"); app.setApplicationName("DataHorizon"); app.setApplicationVersion("0.1.0");
    app.setWindowIcon(QIcon(":/icons/datahorizon.png"));
    app.setDesktopFileName("datahorizon");
    QCommandLineParser parser; parser.setApplicationDescription("DataHorizon native spreadsheet development preview"); parser.addHelpOption(); parser.addVersionOption();
    parser.addOption({"connect", "Attach to a localhost service; credential comes from DATAHORIZON_TOKEN", "url"});
    parser.addOption({"handler", "Explicitly activate a trusted Python handler for the owned service", "path"});
    parser.addOption({"smoke-test", "Create a temporary workbook, exercise managed calculation, and exit"});
    parser.addPositionalArgument("workbook", "Native workbook to open"); parser.process(app);
    StartupOptions options; options.url = parser.value("connect"); options.token = qEnvironmentVariable("DATAHORIZON_TOKEN"); options.handler = parser.value("handler");
    const auto files = parser.positionalArguments(); const bool smoke=parser.isSet("smoke-test"); options.automaticWorkbook = files.isEmpty() && !smoke;
    if (!smoke && !files.isEmpty()) options.initialWorkbook=files.first();
    QTemporaryDir smokeDirectory;
    if(smoke) options.viewStatePath=smokeDirectory.filePath("views.ini");
    if(smoke && options.url.isEmpty()) {
        const auto paths=resolveServicePaths(QCoreApplication::applicationDirPath(),qEnvironmentVariable("DATAHORIZON_PYTHON"));
        if(!paths.error.isEmpty()) {qCritical("%s",qPrintable(paths.error));return 4;}
    }
    bool smokeSubmitted=false;
    MainWindow window(options);
    if (smoke) {
        QObject::connect(&window,&MainWindow::serviceReady,&window,[&]{window.openBook(smokeDirectory.filePath("smoke.dhwb"),true);},Qt::SingleShotConnection);
        QObject::connect(&window,&MainWindow::workbookReady,&window,[&]{
            if(smokeSubmitted) return;
            smokeSubmitted=true;
            window.submit({{"action","edit"},{"cells",QJsonArray{
                QJsonObject{{"row",1},{"col",1},{"source","=SUM(7,13.5)"}}}}});
        });
        QObject::connect(&window,&MainWindow::operationFinished,&window,[&](bool ok){if(!ok)app.exit(3);});
        auto *timer=new QTimer(&window); timer->setInterval(50);
        QObject::connect(timer,&QTimer::timeout,&window,[&]{
            if(smokeSubmitted && window.model()->data(window.model()->index(0,0)).toString()=="20.5") app.exit(0);
        });
        timer->start(); QTimer::singleShot(20000,&app,[&]{app.exit(2);});
    }
    window.show(); return app.exec();
}
