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
                QJsonObject{{"row",1},{"col",1},{"source","=SUM(7,13.5)"}},
                QJsonObject{{"row",2},{"col",1},{"source","=DATE(2024,2,29)+1"}},
                QJsonObject{{"row",3},{"col",1},{"source","2024-02-29T12:34:56.123456"},{"kind","datetime"}},
                QJsonObject{{"row",4},{"col",1},{"source","=YEAR(A2)+MONTH(A2)+DAY(A2)"}},
                QJsonObject{{"row",5},{"col",1},{"source","=HOUR(TIME(1,2,3))+MINUTE(TIME(1,2,3))+SECOND(TIME(1,2,3))"}},
                QJsonObject{{"row",6},{"col",1},{"source","=YEAR(TODAY())>=2020"}},
                QJsonObject{{"row",7},{"col",1},{"source","=HOUR(NOW())>=0"}}}}});
        });
        QObject::connect(&window,&MainWindow::operationFinished,&window,[&](bool ok){if(!ok)app.exit(3);});
        auto *timer=new QTimer(&window); timer->setInterval(50);
        QObject::connect(timer,&QTimer::timeout,&window,[&]{
            const QStringList expected{"20.5","2024-03-01","2024-02-29T12:34:56.123456","2028","6","TRUE","TRUE"};bool valid=smokeSubmitted;
            for(int row=0;row<expected.size();++row)valid=window.model()->data(window.model()->index(row,0)).toString()==expected[row] && valid;
            if(valid) app.exit(0);
        });
        timer->start(); QTimer::singleShot(20000,&app,[&]{app.exit(2);});
    }
    window.show(); return app.exec();
}
