// SPDX-License-Identifier: GPL-3.0-only
#include "mainwindow.h"
#include "relationalworkbench.h"
#include "queryworkbench.h"
#include "peak_rss.h"
#include <QApplication>
#include <QAccessible>
#include <QAction>
#include <QElapsedTimer>
#include <QFile>
#include <QFileInfo>
#include <QJsonDocument>
#include <QLabel>
#include <QPlainTextEdit>
#include <QPushButton>
#include <QSignalSpy>
#include <QTableWidget>
#include <QTabWidget>
#include <QTemporaryDir>
#include <QTimer>
#include <QTreeWidget>
#include <QTest>
#include <algorithm>
#include <stdexcept>

static void require(bool ok,const char *message){if(!ok)throw std::runtime_error(message);}
template<class Predicate>static void waitFor(Predicate ready,const char *message){QElapsedTimer timer;timer.start();while(!ready() && timer.elapsed()<20000)QTest::qWait(2);require(ready(),message);}
static QJsonObject distribution(QList<double> data){require(!data.isEmpty(),"No timing samples");std::sort(data.begin(),data.end());return {{"samples",data.size()},{"median_ms",data[data.size()/2]},{"max_ms",data.last()}};}
int main(int argc,char **argv){
    QApplication app(argc,argv);
    try{
        require(argc==3,"Usage: relational_desktop_benchmark native.dhwb output.json");QTemporaryDir temp;StartupOptions options;options.automaticWorkbook=false;options.viewStatePath=temp.filePath("view.ini");MainWindow window(options);window.resize(1000,760);QSignalSpy ready(&window,&MainWindow::serviceReady),opened(&window,&MainWindow::workbookReady);window.show();waitFor([&]{return ready.count()==1;},"Service did not start");QElapsedTimer elapsed;elapsed.start();window.openBook(QString::fromLocal8Bit(argv[1]),false,true);waitFor([&]{return opened.count()==1;},"Large relational open failed");const double openMs=elapsed.nsecsElapsed()/1e6;
        elapsed.restart();window.findChild<QAction *>("relationalWorkbenchAction")->trigger();auto *objects=window.findChild<QTreeWidget *>("relationalObjects");waitFor([&]{return objects->topLevelItemCount()==3 && objects->topLevelItem(0)->childCount()==1;},"Table catalog did not load");objects->setCurrentItem(objects->topLevelItem(0)->child(0));auto *records=window.findChild<QTableWidget *>("tableRecords");waitFor([&]{return records->rowCount()==128;},"First table page did not load");const double firstPageMs=elapsed.nsecsElapsed()/1e6;
        QList<double> pages;for(int i=0;i<30;++i){const auto first=records->item(0,0)->data(Qt::UserRole);elapsed.restart();window.findChild<QPushButton *>("nextTablePage")->click();waitFor([&]{return records->item(0,0) && records->item(0,0)->data(Qt::UserRole)!=first;},"Table page did not advance");pages.append(elapsed.nsecsElapsed()/1e6);}
        auto *workbench=window.findChild<RelationalWorkbench *>();const auto tableCache=workbench->cacheMetrics(),sheetCache=window.model()->cacheMetrics();require(tableCache["estimated_bytes"].toInteger()<=16*1024*1024,"Relational cache exceeded its target");require(sheetCache["tile_charge_bytes"].toInt()==0 && sheetCache["source_charge_bytes"].toInt()==0,"Hidden sheet retained cached inputs");
        auto *accessible=QAccessible::queryAccessibleInterface(records);require(accessible && accessible->childCount()<4096,"Record accessibility enumeration is unbounded");const int accessibleChildren=accessible->childCount();
        window.findChild<QTabWidget *>("relationalPages")->setCurrentIndex(1);auto *sql=window.findChild<QPlainTextEdit *>("querySql");sql->setPlainText("SELECT count(*) FROM Large a CROSS JOIN Large b");window.findChild<QPushButton *>("runQuery")->click();auto *status=window.findChild<QLabel *>("queryStatus");waitFor([&]{return status->text().contains("Executing");},"Query never entered execution");QList<double> keys,gaps;QElapsedTimer clock;clock.start();qint64 previous=clock.nsecsElapsed();QTimer heartbeat;heartbeat.setInterval(1);heartbeat.setTimerType(Qt::PreciseTimer);QObject::connect(&heartbeat,&QTimer::timeout,&app,[&]{const auto now=clock.nsecsElapsed();gaps.append((now-previous)/1e6);previous=now;});heartbeat.start();sql->setFocus();sql->moveCursor(QTextCursor::End);for(int i=0;i<40;++i){elapsed.restart();QTest::keyClicks(sql," -- input");keys.append(elapsed.nsecsElapsed()/1e6);QTest::qWait(2);}heartbeat.stop();require(sql->toPlainText().endsWith(" -- input"),"Input was lost while SQL ran");
        elapsed.restart();window.findChild<QPushButton *>("cancelQuery")->click();waitFor([&]{return status->text().startsWith("cancelled");},"Query cancellation was not confirmed");const double cancelMs=elapsed.nsecsElapsed()/1e6;const auto queryCache=workbench->cacheMetrics();bool released=false;workbench->closeQuery([&](QString error){require(error.isEmpty(),"Query storage release failed");released=true;});waitFor([&]{return released;},"Query storage not released");workbench->discardQueryDraft();
        QJsonObject report{{"platform",QApplication::platformName()},{"device_pixel_ratio",window.devicePixelRatioF()},{"native_bytes",QFileInfo(QString::fromLocal8Bit(argv[1])).size()},{"fresh_process_open_ms",openMs},{"first_table_page_ms",firstPageMs},{"warm_next_page",distribution(pages)},{"input_while_query_runs",distribution(keys)},{"heartbeat",distribution(gaps)},{"gui_cancel_confirmed_ms",cancelMs},{"table_cache",tableCache},{"query_cache",queryCache},{"hidden_sheet_cache",sheetCache},{"accessible_children",accessibleChildren},{"gui_peak_rss_bytes",processPeakRssBytes()},{"provenance","Fresh GUI/service process on an already warmed native fixture; OS caches were not purged. GUI peak excludes its service child."}};
        QFile output(QString::fromLocal8Bit(argv[2]));require(output.open(QIODevice::WriteOnly),"Cannot write benchmark output");output.write(QJsonDocument(report).toJson());window.close();return 0;
    }catch(const std::exception &error){qCritical("%s",error.what());return 1;}
}
