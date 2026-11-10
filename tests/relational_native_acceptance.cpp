// SPDX-License-Identifier: GPL-3.0-only
// Native-plugin automation is separate from physical input and VoiceOver acceptance.
#include "mainwindow.h"
#include "relationalworkbench.h"
#include "queryworkbench.h"
#include "peak_rss.h"
#include <QAccessible>
#include <QApplication>
#include <QAction>
#include <QCheckBox>
#include <QDialog>
#include <QDialogButtonBox>
#include <QDir>
#include <QElapsedTimer>
#include <QFile>
#include <QFileInfo>
#include <QJsonDocument>
#include <QLabel>
#include <QLineEdit>
#include <QMessageBox>
#include <QPlainTextEdit>
#include <QPushButton>
#include <QSignalSpy>
#include <QTableWidget>
#include <QTabWidget>
#include <QTemporaryDir>
#include <QTest>
#include <QTimer>
#include <QTreeWidget>
#include <stdexcept>

static void require(bool ok,const char *message){if(!ok)throw std::runtime_error(message);}
template<class Predicate>static void waitFor(Predicate ready,const char *message){QElapsedTimer timer;timer.start();while(!ready() && timer.elapsed()<15000)QTest::qWait(5);require(ready(),message);}
int main(int argc,char **argv){
    QApplication app(argc,argv);
    try{
        require(argc==3 || argc==4,"Usage: relational_native_acceptance fixture.dhwb report.json [trusted-handler.py]");require(QApplication::platformName()!="offscreen" && QApplication::platformName()!="minimal","Native plugin required");
        const QFileInfo reportPath(QString::fromLocal8Bit(argv[2]));QDir().mkpath(reportPath.absolutePath());QJsonArray screenshots;auto capture=[&](QWidget *widget,const QString &name){const auto file=reportPath.dir().filePath(reportPath.completeBaseName()+"-"+name+".png");widget->repaint();require(widget->grab().save(file),"Screenshot failed");screenshots.append(QFileInfo(file).fileName());};
        QTemporaryDir temp;StartupOptions options;options.automaticWorkbook=false;options.viewStatePath=temp.filePath("views.ini");if(argc==4)options.handler=QString::fromLocal8Bit(argv[3]);MainWindow window(options);window.resize(1120,820);QSignalSpy ready(&window,&MainWindow::serviceReady),opened(&window,&MainWindow::workbookReady),operations(&window,&MainWindow::operationFinished);window.show();window.raise();window.activateWindow();waitFor([&]{return ready.count()==1;},"Service startup failed");window.openBook(QString::fromLocal8Bit(argv[1]));waitFor([&]{return opened.count()==1;},"Native workbook open failed");window.findChild<QAction *>("relationalWorkbenchAction")->trigger();
        auto *objects=window.findChild<QTreeWidget *>("relationalObjects");waitFor([&]{return objects->topLevelItemCount()==3 && objects->topLevelItem(2)->childCount()==1;},"Saved query browser missing");objects->setCurrentItem(objects->topLevelItem(2)->child(0));auto *query=window.findChild<QueryWorkbench *>();waitFor([&]{return window.findChild<QLabel *>("savedQueryName")->text()=="Customer totals";},"Saved definition did not open");require(!query->hasActiveJob(),"Opening definition executed SQL");
        enum Mode{None,Parameters,ValidEdit,InvalidEdit,AwaitRule,Export};Mode mode=None;bool ruleRejected=false;QTimer automation;automation.setInterval(10);QObject::connect(&automation,&QTimer::timeout,&window,[&]{auto *dialog=qobject_cast<QDialog *>(QApplication::activeModalWidget());if(!dialog)return;
            if(mode==Parameters && dialog->objectName()=="queryParameterValues"){mode=None;dialog->findChild<QLineEdit *>("recordInput_0")->setText("0");dialog->findChild<QDialogButtonBox *>()->button(QDialogButtonBox::Save)->click();}
            else if((mode==ValidEdit || mode==InvalidEdit) && dialog->objectName()=="recordInputDialog"){const bool invalid=mode==InvalidEdit;mode=invalid?AwaitRule:None;dialog->findChild<QLineEdit *>("recordInput_0")->setText(invalid?"0":"4");capture(dialog,invalid?"quantity-rule-input":"typed-record");dialog->findChild<QDialogButtonBox *>()->button(QDialogButtonBox::Save)->click();}
            else if(mode==AwaitRule && dialog->objectName()=="recordInputDialog" && dialog->findChild<QLabel *>("recordEditStatus")->text().contains("positive")){mode=None;ruleRejected=true;capture(dialog,"required-rule-rollback");dialog->findChild<QDialogButtonBox *>()->button(QDialogButtonBox::Cancel)->click();}
            else if(mode==Export && dialog->objectName()=="relationalExportReview"){mode=None;capture(dialog,"complete-export-review");qobject_cast<QMessageBox *>(dialog)->button(QMessageBox::Yes)->click();}
        });automation.start();auto *pages=window.findChild<QTabWidget *>("relationalPages");auto *results=window.findChild<QTableWidget *>("queryResults");
        auto run=[&](const QString &ada){pages->setCurrentIndex(1);mode=Parameters;window.findChild<QPushButton *>("runQuery")->click();waitFor([&]{return results->rowCount()==2 && results->item(0,3) && results->item(0,3)->text()==ada && results->item(1,3)->text()=="21";},"Customer totals mismatch");};run("35");capture(&window,"customer-totals");
        auto *sql=window.findChild<QPlainTextEdit *>("querySql");sql->setFocus();QTest::keyClick(sql,Qt::Key_Tab);require(QApplication::focusWidget()!=sql,"SQL editor traps Tab navigation");auto *accessible=QAccessible::queryAccessibleInterface(results);require(accessible && accessible->childCount()<256,"Query accessibility is unbounded");
        pages->setCurrentIndex(0);QTreeWidgetItem *orders=nullptr;for(int i=0;i<objects->topLevelItem(0)->childCount();++i)if(objects->topLevelItem(0)->child(i)->text(0)=="Orders")orders=objects->topLevelItem(0)->child(i);require(orders,"Orders table missing");objects->setCurrentItem(orders);auto *records=window.findChild<QTableWidget *>("tableRecords");waitFor([&]{return records->rowCount()==3;},"Orders records did not load");int quantity=-1,orderId=-1;for(int col=0;col<records->columnCount();++col){if(records->horizontalHeaderItem(col)->text()=="Quantity")quantity=col;if(records->horizontalHeaderItem(col)->text()=="OrderID")orderId=col;}require(quantity>=0 && orderId>=0,"Typed columns missing");int row=-1;for(int r=0;r<records->rowCount();++r)if(records->item(r,orderId)->text()=="O100")row=r;require(row>=0,"O100 missing");capture(&window,"orders-table");records->setCurrentCell(row,quantity);records->setFocus();mode=ValidEdit;const int before=opened.count();QTest::keyClick(records,Qt::Key_F2);waitFor([&]{return opened.count()==before+1;},"Typed record edit did not commit");run("60");
        pages->setCurrentIndex(0);waitFor([&]{return records->rowCount()==3;},"Edited table did not reload");window.findChild<QPushButton *>("undoTableRecords")->click();waitFor([&]{return opened.count()==before+2;},"Record undo failed");run("35");
        if(argc==4){pages->setCurrentIndex(0);waitFor([&]{return records->rowCount()==3;},"Rule table did not load");records->setCurrentCell(row,quantity);mode=InvalidEdit;window.findChild<QPushButton *>("editTableCell")->click();waitFor([&]{return ruleRejected && !QApplication::activeModalWidget();},"Required rule rejection missing");require(records->item(row,quantity)->text()=="2","Rejected quantity changed data");run("35");}
        for(const auto &format:QStringList{"csv","xlsx","ods"}){const auto path=reportPath.dir().filePath(reportPath.completeBaseName()+"-totals."+format);mode=Export;query->exportTo(path,format);waitFor([&]{return QFileInfo::exists(path);},"Complete native export missing");}
        const auto cache=window.findChild<RelationalWorkbench *>()->cacheMetrics();require(cache["estimated_bytes"].toInteger()<=16*1024*1024,"Combined GUI cache target exceeded");window.resize(820,680);QTest::qWait(50);capture(&window,"narrow-query");
        bool released=false;window.findChild<RelationalWorkbench *>()->closeQuery([&](QString error){require(error.isEmpty(),"Query release failed");released=true;});waitFor([&]{return released;},"Query release not confirmed");automation.stop();QJsonObject report{{"platform",QApplication::platformName()},{"device_pixel_ratio",window.devicePixelRatioF()},{"fixture",QFileInfo(QString::fromLocal8Bit(argv[1])).fileName()},{"saved_open_without_execution",true},{"baseline_totals",QJsonArray{35,21}},{"changed_totals",QJsonArray{60,21}},{"undo_restored",true},{"required_rule_rejected",ruleRejected},{"tab_navigation",true},{"bounded_accessibility",true},{"exports",QJsonArray{"csv","xlsx","ods"}},{"combined_cache",cache},{"gui_peak_rss_bytes_including_screenshot_capture",processPeakRssBytes()},{"narrow_width",window.width()},{"narrow_height",window.height()},{"screenshots",screenshots},{"physical_input_and_voiceover","pending owner acceptance"}};QFile output(reportPath.absoluteFilePath());require(output.open(QIODevice::WriteOnly),"Cannot write native report");output.write(QJsonDocument(report).toJson());window.close();return 0;
    }catch(const std::exception &error){qCritical("%s",error.what());return 1;}
}
