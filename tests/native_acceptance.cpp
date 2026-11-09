// SPDX-License-Identifier: GPL-3.0-only
// Native-plugin automation, separate from physical input/screen-reader acceptance.
#include "mainwindow.h"
#include <QAccessible>
#include <QApplication>
#include <QAction>
#include <QClipboard>
#include <QDateTime>
#include <QElapsedTimer>
#include <QFile>
#include <QFileInfo>
#include <QDir>
#include <QJsonArray>
#include <QJsonDocument>
#include <QLineEdit>
#include <QInputDialog>
#include <QComboBox>
#include <QSpinBox>
#include <QPlainTextEdit>
#include <QHeaderView>
#include <QScrollBar>
#include <QPushButton>
#include <QPointer>
#include <QMimeData>
#include <QScopeGuard>
#include <QSignalSpy>
#include <QSysInfo>
#include <QTableView>
#include <QTemporaryDir>
#include <QTimer>
#include <QUuid>
#include <QTest>
#include <memory>
#include <stdexcept>

static void require(bool value,const char *message) { if(!value) throw std::runtime_error(message); }
template<class Test> static void waitFor(Test test,const char *message) {
    QElapsedTimer deadline; deadline.start();
    while(!test() && deadline.elapsed()<15000) QTest::qWait(5);
    require(test(),message);
}
int main(int argc,char **argv) {
    QApplication app(argc,argv);
    try {
        require(argc==2,"Usage: native_acceptance output.json");
        const auto plugin=QApplication::platformName();
        require(plugin!="offscreen" && plugin!="minimal","Acceptance requires a native platform plugin");
        QTemporaryDir temp; require(temp.isValid(),"Cannot create fixture directory");
        StartupOptions options; options.automaticWorkbook=false;
        options.viewStatePath=temp.filePath("views.ini");
        MainWindow window(options); window.resize(820,620);
        QSignalSpy ready(&window,&MainWindow::serviceReady), opened(&window,&MainWindow::workbookReady);
        window.show(); window.raise(); window.activateWindow();
        waitFor([&]{return ready.count()==1;},"Native service startup failed");
        window.openBook(temp.filePath("native.dhwb"),true);
        waitFor([&]{return opened.count()==1;},"Native open failed");
        auto *grid=window.findChild<QTableView *>("grid");
        auto *source=window.findChild<QLineEdit *>("source");
        auto *address=window.findChild<QLineEdit *>("address");
        auto *model=window.model();
        require(grid && source && address,"Native controls missing");
        waitFor([&]{model->data(model->index(0,0)); return source->isEnabled();},"Native input never became editable");
        source->setFocus(); source->selectAll(); QTest::keyClicks(source,"=SUM(7,13.5)");
        QTest::keyClick(source,Qt::Key_Return);
        waitFor([&]{return opened.count()==2;},"Keyboard edit failed");
        waitFor([&]{return model->data(model->index(0,0)).toString()=="20.5";},"Native calculation mismatch");
        waitFor([&]{return grid->currentIndex()==model->index(1,0) && source->isEnabled();},"Acknowledged Enter did not advance to A2");
        source->setFocus(); source->selectAll(); QTest::keyClicks(source,"999");
        QTest::keyClick(source,Qt::Key_Escape);
        require(!source->isModified() && source->text().isEmpty(),"Escape did not restore the blank A2 input");
        require(model->data(model->index(0,0)).toString()=="20.5","Escape changed stored input");
        address->setFocus(); address->selectAll(); QTest::keyClicks(address,"B42"); QTest::keyClick(address,Qt::Key_Return);
        waitFor([&]{return grid->currentIndex()==model->index(41,1);},"Native address navigation failed");
        waitFor([&]{return source->isEnabled();},"Navigated input never became editable");

        // Save every clipboard format; restore only if nobody replaced our text.
        auto saved=std::make_unique<QMimeData>();
        if(const auto *mime=app.clipboard()->mimeData()) {
            for(const auto &format:mime->formats()) saved->setData(format,mime->data(format));
            if(mime->hasImage()) saved->setImageData(mime->imageData());
        }
        const QString marker="DataHorizon native clipboard "+QUuid::createUuid().toString();
        app.clipboard()->setText(marker);
        const auto restore=qScopeGuard([&]{if(app.clipboard()->text()==marker) app.clipboard()->setMimeData(saved.release());});
        source->setFocus(); source->selectAll();
        const auto paste=QKeySequence(QKeySequence::Paste);
        QTest::keySequence(source,paste);
        require(source->text()==marker,"Native clipboard paste failed");
        QTest::keyClick(source,Qt::Key_Escape);
        address->setText("A1"); QTest::keyClick(address,Qt::Key_Return);
        waitFor([&]{return model->data(grid->currentIndex()).toString()=="20.5";},"Returning to calculated cell failed");

        QSignalSpy edits(model,&SheetModel::editRequested);
        grid->setFocus();
        QTest::keyClick(grid,Qt::Key_Right,Qt::ShiftModifier);
        require(grid->selectionModel()->selectedIndexes().size()==2,"Keyboard range selection failed");
        QTest::keyClick(grid,Qt::Key_Left);
        require(grid->currentIndex()==model->index(0,0),"Keyboard range collapse failed");
        QTest::keyClick(grid,Qt::Key_Tab);
        require(grid->currentIndex()==model->index(0,1),"Grid Tab navigation failed");
        QTest::keyClick(grid,Qt::Key_Backtab);
        require(grid->currentIndex()==model->index(0,0),"Grid Backtab navigation failed");
        waitFor([&]{return model->sourceReady(grid->currentIndex());},"Cell source never loaded");
#ifdef Q_OS_MACOS
        QTest::keyClick(grid,Qt::Key_Return);
#else
        QTest::keyClick(grid,Qt::Key_F2);
#endif
        QPointer<QLineEdit> editor=grid->findChild<QLineEdit *>();
        require(editor && editor->isVisible(),"Keyboard cell editor did not open");
        auto *editorAccessible=QAccessible::queryAccessibleInterface(editor);
        require(editorAccessible && editorAccessible->text(QAccessible::Name)=="Cell A1 input","Cell editor has no source-address accessible name");
        editor->selectAll(); QTest::keyClicks(editor,"999"); QTest::keyClick(editor,Qt::Key_Escape);
        waitFor([&]{return !editor || !editor->isVisible();},"Cell editor Escape failed");

        // Exercise a real Qt modal on the native plugin. A watchdog also bounds
        // failures in nested dialog event loops, so CI cannot wait for a person.
        auto *addSheet=window.findChild<QAction *>("addSheetAction");
        require(addSheet,"Add sheet action missing");
        bool dialogSeen=false;
        QTimer cancelDialog,watchdog;
        cancelDialog.setInterval(10); watchdog.setSingleShot(true);
        QObject::connect(&cancelDialog,&QTimer::timeout,[&]{
            if(auto *dialog=qobject_cast<QInputDialog *>(QApplication::activeModalWidget())) {
                dialogSeen=true; QTest::keyClick(dialog,Qt::Key_Escape);
            }
        });
        QObject::connect(&watchdog,&QTimer::timeout,[&]{
            if(auto *dialog=qobject_cast<QDialog *>(QApplication::activeModalWidget())) dialog->reject();
        });
        cancelDialog.start(); watchdog.start(5000); addSheet->trigger();
        cancelDialog.stop(); watchdog.stop();
        require(dialogSeen,"Native-plugin sheet dialog did not appear");
        require(edits.isEmpty() && opened.count()==2,"Navigation or cancelled editor/dialog submitted an edit");
        require(model->data(model->index(0,0)).toString()=="20.5","Cancelled native interaction changed a value");
        QJsonArray jobControls;bool jobsSeen=false,jobTabPassed=false;
        bool jobActivated=false;QString jobFocus;
        QTimer inspectJobs;inspectJobs.setInterval(10);
        QObject::connect(&inspectJobs,&QTimer::timeout,[&]{
            if(auto *dialog=window.findChild<QDialog *>("operationJobsDialog")){
                inspectJobs.stop();jobsSeen=true;
                dialog->raise();dialog->activateWindow();const bool activated=QTest::qWaitForWindowActive(dialog,3000);
                for(const auto &name:QStringList{"operationJobMode","operationJobId","operationJobSource","operationJobRows","operationJobReport","prepareOperationJob","inspectOperationJob","startOperationJob","cancelOperationJob","closeOperationJob"}){
                    auto *widget=dialog->findChild<QWidget *>(name);auto *interface=widget?QAccessible::queryAccessibleInterface(widget):nullptr;
                    jobControls.append(QJsonObject{{"control",name},{"valid",interface && interface->isValid()},{"name",interface?interface->text(QAccessible::Name):QString()},{"role",interface?int(interface->role()):0}});
                }
                auto *id=dialog->findChild<QLineEdit *>("operationJobId");id->setFocus(Qt::TabFocusReason);QTest::keyClick(id,Qt::Key_Tab);
                jobActivated=activated;jobFocus=dialog->focusWidget()?dialog->focusWidget()->objectName():QStringLiteral("<none>");
                jobTabPassed=dialog->focusWidget()==dialog->findChild<QLineEdit *>("operationJobSource");dialog->reject();
            }
        });
        inspectJobs.start();watchdog.start(5000);window.findChild<QAction *>("operationJobsAction")->trigger();inspectJobs.stop();watchdog.stop();
        require(jobsSeen && jobControls.size()==10,"Job accessibility inspection missing");
        if(!jobActivated || !jobTabPassed) qWarning().noquote()<<"Job dialog native activation:"<<jobActivated<<"Tab focus:"<<jobFocus;
        require(jobActivated,"Job dialog did not acquire native window activation");
        require(jobTabPassed,"Job dialog Tab did not focus operationJobSource");
        for(const auto &entry:jobControls){const auto control=entry.toObject();require(control["valid"].toBool() && !control["name"].toString().isEmpty(),"Job control accessible name/role missing");}
        window.resize(560,440); QTest::qWait(100);
        grid->scrollTo(model->index(0,0));
        require(grid->viewport()->width()>100 && grid->viewport()->height()>100,"Narrow window lost usable grid geometry");

        QJsonArray controls;
        for(QWidget *widget:QList<QWidget *>{address,source,grid}) {
            auto *accessible=QAccessible::queryAccessibleInterface(widget);
            require(accessible && accessible->isValid(),"Native accessible interface missing");
            require(!accessible->text(QAccessible::Name).isEmpty(),"Native accessible name missing");
            controls.append(QJsonObject{{"name",accessible->text(QAccessible::Name)},{"role",int(accessible->role())}});
        }
        auto *surface=QAccessible::queryAccessibleInterface(grid);
        require(surface && surface->role()==QAccessible::Pane && surface->childCount()==1 && !surface->tableInterface(),"Accessible keyboard surface must remain bounded");
        const QRect cell=grid->visualRect(grid->currentIndex());
        require(cell.width()>0 && cell.height()>0 && grid->viewport()->rect().intersects(cell),"Native cell geometry invalid");
        require(window.devicePixelRatioF()>=qEnvironmentVariable("QT_SCALE_FACTOR","1").toDouble(),"Requested scale factor was not applied");
        window.submit({{"action","edit"},{"cells",QJsonArray{
            QJsonObject{{"row",1},{"col",1},{"style",QJsonObject{{"border","box"}}}},
            QJsonObject{{"row",2},{"col",1},{"source","Wrapped text stays below the frozen top row while the body scrolls."},{"kind","text"},
                {"style",QJsonObject{{"wrap",true},{"border","box"}}}}}}});
        waitFor([&]{return opened.count()==3;},"Native everyday styling failed");
        window.findChild<QAction *>("freezeTopRowAction")->trigger();
        auto *top=window.findChild<QTableView *>("frozenTopRow");
        require(top && top->isVisible(),"Native top row did not freeze");
        grid->setColumnWidth(0,180);
        waitFor([&]{return top->model()->data(top->model()->index(0,0)).toString()=="20.5";},"Native frozen value mismatch");
        require(top->columnWidth(0)==180,"Native frozen column size mismatch");
        grid->selectColumn(1); window.findChild<QAction *>("hideColumnsAction")->trigger();
        require(top->isColumnHidden(1) && grid->isColumnHidden(1),"Native frozen column hiding mismatch");
        require(window.findChild<QPushButton *>("showHiddenButton")->isVisible(),"Native hidden-section recovery control missing");
        auto *frozenAccessible=QAccessible::queryAccessibleInterface(top);
        require(frozenAccessible && frozenAccessible->role()==QAccessible::Pane && frozenAccessible->childCount()==1,"Native frozen keyboard accessibility missing");
        grid->setCurrentIndex(model->index(0,0));
        require(top->viewport()->mapToGlobal(QPoint(0,top->rowHeight(0))).y()<=grid->viewport()->mapToGlobal(QPoint()).y(),"Native frozen row covers body cells");
        grid->scrollTo(model->index(10000,0),QAbstractItemView::PositionAtTop);
        waitFor([&]{return model->loaded(model->index(10000,0));},"Native frozen distant scroll failed");
        require(top->model()->data(top->model()->index(0,0)).toString()=="20.5","Native frozen header scrolled away");
        grid->scrollTo(model->index(1,0),QAbstractItemView::PositionAtTop);
        waitFor([&]{return model->loaded(model->index(1,0)) && grid->rowHeight(1)>26;},"Native text wrapping did not fit");
        require(model->cacheMetrics()["tiles"].toInt()<=16,"Native frozen view exceeded the shared cache");
        const QFileInfo reportFile(QString::fromLocal8Bit(argv[1]));
        const auto screenshot=reportFile.dir().filePath(reportFile.completeBaseName()+".png");
        require(window.grab().save(screenshot),"Native screenshot failed");
        QJsonObject report{{"recorded_at_utc",QDateTime::currentDateTimeUtc().toString(Qt::ISODate)},
            {"os",QSysInfo::prettyProductName()},{"qt",qVersion()},{"platform_plugin",plugin},
            {"device_pixel_ratio",window.devicePixelRatioF()},{"scale_request",qEnvironmentVariable("QT_SCALE_FACTOR","1")},
            {"active_window",window.isActiveWindow()},{"accessible_controls",controls},{"job_accessible_controls",jobControls},{"job_dialog_tab_order",true},
            {"keyboard_edit_calculation_escape_navigation",true},{"clipboard_paste",true},
            {"acknowledged_enter_advances",true},
            {"keyboard_range_tab_and_editor_cancel",true},{"cell_editor_accessible_address",true},
            {"qt_modal_escape_without_mutation",true},{"narrow_window_usable_grid",true},
            {"frozen_header_shared_cache_geometry",true},{"native_wrap_border_and_column_size",true},
            {"native_hidden_gap_and_recovery_control",true},
            {"window_logical_width",window.width()},{"cell_logical_width",cell.width()},
            {"palette_base",grid->palette().color(QPalette::Base).name()},
            {"screenshot",QFileInfo(screenshot).fileName()},
            {"limitations",QJsonArray{"QTest widget events are synthetic; no physical input or compositor-latency qualification.",
                "Bounded keyboard accessibility is not full screen-reader table navigation or spoken-output acceptance.",
                "Native dialogs, multiple monitors and clean-machine installation remain separate acceptance."}}};
        QFile output(reportFile.filePath()); require(output.open(QIODevice::WriteOnly),"Cannot save native report");
        const auto bytes=QJsonDocument(report).toJson(); require(output.write(bytes)==bytes.size(),"Native report truncated");
        return 0;
    } catch(const std::exception &error) { qCritical("%s",error.what()); return 1; }
}
