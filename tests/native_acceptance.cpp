// SPDX-License-Identifier: GPL-3.0-only
// Native-plugin automation, separate from physical input/screen-reader acceptance.
#include "mainwindow.h"
#include "pythonworkspace.h"
#include <QAccessible>
#include <QApplication>
#include <QAction>
#include <QCheckBox>
#include <QLabel>
#include <QListWidget>
#include <QMessageBox>
#include <QTabBar>
#include <QTabWidget>
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
#include <functional>
#include <memory>
#include <stdexcept>

static void require(bool value,const char *message) { if(!value) throw std::runtime_error(message); }
template<class Test> static void waitFor(Test test,const char *message) {
    QElapsedTimer deadline; deadline.start();
    while(!test() && deadline.elapsed()<15000) QTest::qWait(5);
    require(test(),message);
}

// Answers the modal dialogs a step opens; handle() returns true when it acted.
class ModalDriver {
public:
    explicit ModalDriver(std::function<bool(QWidget *)> handle):m_handle(std::move(handle)) {
        m_timer.setInterval(10);
        QObject::connect(&m_timer,&QTimer::timeout,[this]{if(auto *modal=QApplication::activeModalWidget())if(m_handle(modal))++handled;});
        m_timer.start();
    }
    int handled=0;
private:
    std::function<bool(QWidget *)> m_handle;
    QTimer m_timer;
};

// Clicking Yes on any "clears Undo history" confirmation, as a user who has
// read it would.
static bool acceptHistoryPrompt(QWidget *modal) {
    auto *box=qobject_cast<QMessageBox *>(modal);
    if(!box || !box->objectName().endsWith("HistoryPrompt"))return false;
    box->button(QMessageBox::Yes)->click();return true;
}

// The storyboard session in the real workspace window, one linear scenario:
// each step is recorded by name and the first failure stops the run.
static QJsonObject pythonWorkspaceScenario(const QString &directory,const QString &screenshot) {
    QJsonArray steps;QString current;QElapsedTimer total;total.start();
    auto step=[&](const char *name,const std::function<void()> &body) {
        current=QString::fromLatin1(name);QElapsedTimer clock;clock.start();
        try{body();}catch(const std::exception &error){throw std::runtime_error(("pythonWorkspace step "+current+": "+error.what()).toStdString());}
        steps.append(QJsonObject{{"step",current},{"passed",true},{"elapsed_ms",double(clock.elapsed())}});
    };
    StartupOptions options;options.automaticWorkbook=false;
    options.viewStatePath=directory+"/workspace-views.ini";options.recoveryDirectory=directory+"/workspace-recovery";
    MainWindow window(options);window.resize(820,620);
    QSignalSpy ready(&window,&MainWindow::serviceReady),opened(&window,&MainWindow::workbookReady);
    QSignalSpy finished(&window,&MainWindow::operationFinished);
    window.show();window.raise();window.activateWindow();
    PythonWorkspace *workspace=nullptr;QTableView *grid=nullptr;QPlainTextEdit *code=nullptr;
    auto cell=[&](int row,int col){return window.model()->data(window.model()->index(row,col)).toString();};
    auto apply=[&](const QJsonObject &operation,const char *message) {
        const int before=int(finished.count()),views=int(opened.count());window.submit(operation);
        waitFor([&]{return finished.count()==before+1 && opened.count()>views;},message);
        require(finished.last()[0].toBool(),message);
    };
    auto select=[&](const QString &address) {
        auto *box=window.findChild<QLineEdit *>("address");box->setFocus();box->selectAll();
        QTest::keyClicks(box,address);QTest::keyClick(box,Qt::Key_Return);
    };
    auto typeLine=[&](const QString &text){QTest::keyClicks(code,text);QTest::keyClick(code,Qt::Key_Return);};
    auto approval=[&]{return workspace->findChild<QLabel *>("workspaceApproval")->text();};
    auto approve=[&] {
        ModalDriver driver([](QWidget *modal){
            auto *dialog=qobject_cast<QDialog *>(modal);if(!dialog || dialog->objectName()!="codeApprovalDialog")return false;
            auto *button=dialog->findChild<QPushButton *>("approveButton");require(button && button->isEnabled(),"Approve button unavailable");
            button->click();return true;});
        auto *review=workspace->findChild<QAction *>("reviewApproveAction");
        waitFor([&]{return review->isEnabled();},"Review & approve never became available");
        review->trigger();
        require(driver.handled==1,"Review & approve dialog did not appear");
        waitFor([&]{return approval()=="Approved for this session";},"Approval was not confirmed by the service");
    };
    auto heading=[&]{return workspace->findChild<QPlainTextEdit *>("runOutput")->toPlainText().section('\n',0,0);};
    auto run=[&](const QString &extra) {
        auto *macro=workspace->findChild<QComboBox *>("macroSelect");const int index=macro->findText("supplies.restock_pens");
        require(index>=0,"restock_pens is not offered to run");macro->setCurrentIndex(index);
        auto *argument=workspace->findChild<QLineEdit *>("argument_extra");require(argument,"No field for argument extra");
        argument->setFocus();argument->selectAll();QTest::keyClicks(argument,extra);
        auto *button=workspace->findChild<QPushButton *>("runButton");
        waitFor([&]{return button->isEnabled();},"Run never became available");
        button->click();
        waitFor([&]{return !heading().startsWith("Running") && !heading().isEmpty();},"Run produced no outcome");
    };

    step("start_service_and_open_supplies",[&] {
        waitFor([&]{return ready.count()==1;},"Native service startup failed");
        window.openBook(directory+"/Supplies.dhwb",true);
        waitFor([&]{return opened.count()==1;},"Native open failed");
        grid=window.findChild<QTableView *>("grid");require(grid,"Grid missing");
        apply({{"action","sheet_add"},{"name","Supplies"}},"Supplies sheet was not added");
        auto *tabs=window.findChild<QTabBar *>("sheets");
        for(int index=0;index<tabs->count();++index)if(tabs->tabText(index)=="Supplies")tabs->setCurrentIndex(index);
        waitFor([&]{return window.model()->sheet()==tabs->tabData(tabs->currentIndex()).toString();},"Supplies sheet not shown");
        QJsonArray cells;
        const QList<QStringList> rows{{"Item","Quantity","Unit price","Amount"},{"Notebook","2","3.50","=B2*C2"},{"Pen","4","1.25","=B3*C3"},{"Folder","3","2.00","=B4*C4"}};
        for(int row=0;row<rows.size();++row)for(int col=0;col<4;++col) {
            QJsonObject item{{"row",row+1},{"col",col+1},{"source",rows[row][col]}};
            if(col==3 && row>0)item["style"]=QJsonObject{{"format","fixed2"}};
            cells.append(item);
        }
        cells.append(QJsonObject{{"row",6},{"col",4},{"source","=SUM(D2:D4)"},{"style",QJsonObject{{"format","fixed2"}}}});
        apply({{"action","edit"},{"cells",cells}},"Supplies data was not stored");
        waitFor([&]{return cell(5,3)=="18.00";},"Supplies total is not 18.00");
    });
    step("open_workspace",[&] {
        window.findChild<QAction *>("pythonWorkspaceAction")->trigger();
        workspace=window.findChild<PythonWorkspace *>("pythonWorkspace");require(workspace,"Workspace window missing");
        waitFor([&]{return workspace->isVisible() && !workspace->fingerprint().isEmpty();},"Workspace did not load the code catalog");
        require(workspace->isWindow() && window.isVisible(),"Workspace is not a separate window beside the sheet");
        workspace->move(window.frameGeometry().topRight()+QPoint(8,0));
        code=workspace->findChild<QPlainTextEdit *>("codeEditor");require(code,"Code editor missing");
    });
    step("new_module",[&] {
        ModalDriver driver([](QWidget *modal){
            auto *input=qobject_cast<QInputDialog *>(modal);if(!input || input->objectName()!="newModuleName")return false;
            input->setTextValue("supplies");input->accept();return true;});
        auto *button=workspace->findChild<QPushButton *>("newModuleButton");
        waitFor([&]{return button->isEnabled();},"New module unavailable");
        button->click();require(driver.handled==1,"New module dialog did not appear");
        waitFor([&]{return code->isEnabled() && !code->isReadOnly();},"New module is not editable");
    });
    step("type_module_and_pick_b3",[&] {
        code->setFocus();
        typeLine("def restock_pens(ctx, extra):");
        QTest::keyClicks(code,"    quantity = ctx.value(");
        auto *pick=workspace->findChild<QAction *>("pickFromSheetAction");
        require(pick->isEnabled(),"Pick from sheet unavailable");pick->trigger();require(pick->isChecked(),"Picking did not arm");
        QTest::mouseClick(grid->viewport(),Qt::LeftButton,{},grid->visualRect(window.model()->index(2,1)).center());
        pick->trigger();require(!pick->isChecked(),"Picking did not end");
        require(code->toPlainText().endsWith("ctx.value(\"'Supplies'!B3\""),"Picking did not insert the quoted address of B3");
        require(cell(2,1)=="4" && finished.count()==2,"Picking changed stored data");
        workspace->activateWindow();code->setFocus();
        typeLine(") + extra");
        for(const auto &line:QStringList{"    if quantity < 0:","        raise ValueError(\"Below zero\")","    ctx.set(\"PenQuantity\", quantity)","    return ctx.value(\"Total\")","","",
                "def check_quantity(ctx, changes):","    for change in changes:","        if change[\"after\"] is None:","            continue",
                "        value = ctx.value(change[\"sheet\"], change[\"row\"], change[\"col\"])","        if isinstance(value, (int, float)) and value < 0:","            raise ValueError(\"Below zero\")"})typeLine(line);
        require(code->toPlainText().contains("    quantity = ctx.value(\"'Supplies'!B3\") + extra\n    if quantity < 0:\n"),"Typed module text mismatch");
        require(workspace->findChild<QLabel *>("workspaceStatus")->text()=="Draft · not saved to the workbook","Typing did not make a draft");
    });
    step("declare_macro",[&] {
        ModalDriver driver([](QWidget *modal){
            if(modal->objectName()!="addMacroDialog")return false;
            auto *panel=modal->findChild<QWidget *>("newMacroPanel");
            panel->findChild<QLineEdit *>("macroName")->setText("restock_pens");panel->findChild<QPushButton *>("addArgumentButton")->click();
            panel->findChild<QLineEdit *>("argumentName_0")->setText("extra");
            auto *type=panel->findChild<QComboBox *>("argumentType_0");type->setCurrentIndex(type->findData("number"));
            auto *result=panel->findChild<QComboBox *>("macroResultType");result->setCurrentIndex(result->findData("number"));
            qobject_cast<QDialog *>(modal)->accept();return true;});
        workspace->findChild<QPushButton *>("addMacroButton")->click();require(driver.handled==1,"Add macro dialog did not appear");
        require(code->toPlainText().count("def restock_pens")==1,"Declaring the typed macro duplicated its function");
    });
    step("name_cells",[&] {
        workspace->findChild<QAction *>("showNamesAction")->trigger();
        auto *panel=workspace->findChild<QWidget *>("namesPanel");require(panel && panel->isVisible(),"Names panel not shown");
        for(const auto &entry:QList<QPair<QString,QString>>{{"PenQuantity","B3"},{"Quantities","B2:B4"},{"Total","D6"}}) {
            const QString name=entry.first,range=entry.second;const int before=int(window.names().size());
            select(range);
            auto *button=panel->findChild<QPushButton *>("newNameButton");
            waitFor([&]{return button->isEnabled();},"New name unavailable");
            ModalDriver driver([&name](QWidget *modal){
                if(auto *input=qobject_cast<QInputDialog *>(modal);input && input->objectName()=="newNameInput"){input->setTextValue(name);input->accept();return true;}
                return acceptHistoryPrompt(modal);});
            button->click();
            waitFor([&]{return window.names().size()==before+1;},"Name was not stored");
        }
        select("B3");waitFor([&]{return window.findChild<QLineEdit *>("address")->text()=="PenQuantity";},"Address box does not show the name");
        workspace->findChild<QAction *>("showMacroAction")->trigger();
    });
    step("save_module",[&] {
        ModalDriver driver(acceptHistoryPrompt);
        workspace->findChild<QAction *>("saveModuleAction")->trigger();
        waitFor([&]{return workspace->findChild<QLabel *>("workspaceStatus")->text()=="Saved in workbook" && !workspace->hasDrafts();},"Module was not saved");
        require(approval()=="Not approved to run","Saved code is approved without review");
    });
    step("review_and_approve",[&]{approve();});
    step("run_restock_pens",[&] {
        run("2");
        require(heading()=="Committed · returned 20.5 · 1 cells changed","Run did not commit 20.5");
        waitFor([&]{return cell(2,1)=="6" && cell(5,3)=="20.50";},"Sheet did not show the macro's change");
    });
    step("undo_run",[&] {
        QAction *undo=nullptr;
        waitFor([&]{for(auto *action:window.findChildren<QAction *>())if(action->text()=="Undo Run supplies.restock_pens")undo=action;return undo && undo->isEnabled();},"Undo does not name the macro");
        undo->trigger();
        waitFor([&]{return cell(2,1)=="4" && cell(5,3)=="18.00";},"Undo did not restore the sheet");
    });
    step("failing_run_leaves_sheet",[&] {
        run("-10");
        const auto output=workspace->findChild<QPlainTextEdit *>("runOutput")->toPlainText();
        require(heading()=="Rolled back" && output.contains("ValueError: Below zero"),"Failure was not reported as rolled back");
        QTest::qWait(200);require(cell(2,1)=="4" && cell(5,3)=="18.00","Failed run changed the sheet");
    });
    step("bind_check_quantity",[&] {
        auto *tabs=workspace->findChild<QTabWidget *>("workspaceTabs");tabs->setCurrentWidget(workspace->findChild<QWidget *>("eventsTab"));
        auto *event=workspace->findChild<QComboBox *>("bindingEvent");event->setCurrentIndex(event->findText("cells change"));
        auto *function=workspace->findChild<QComboBox *>("bindingFunction");
        waitFor([&]{return function->findText("supplies.check_quantity")>=0;},"check_quantity is not offered");
        function->setCurrentIndex(function->findText("supplies.check_quantity"));
        auto *scope=workspace->findChild<QLineEdit *>("bindingScope");scope->setFocus();scope->selectAll();QTest::keyClicks(scope,"Quantities");
        workspace->findChild<QCheckBox *>("bindingRequired")->setChecked(true);
        auto *add=workspace->findChild<QPushButton *>("addBindingButton");
        waitFor([&]{return add->isEnabled();},"Add rule unavailable");
        auto *list=workspace->findChild<QListWidget *>("bindingList");const int before=list->count();
        ModalDriver driver(acceptHistoryPrompt);add->click();
        waitFor([&]{return list->count()==before+1;},"Rule was not stored");
        waitFor([&]{return approval()=="Not approved to run";},"Adding a rule did not require fresh approval");
        approve();
    });
    step("negative_edit_rejected",[&] {
        auto *source=window.findChild<QLineEdit *>("source");
        window.activateWindow();grid->setCurrentIndex(window.model()->index(1,1));
        waitFor([&]{return source->isEnabled() && window.model()->sourceReady(grid->currentIndex());},"B2 never became editable");
        const int before=int(finished.count());
        source->setFocus();source->selectAll();QTest::keyClicks(source,"-1");QTest::keyClick(source,Qt::Key_Return);
        waitFor([&]{return finished.count()==before+1;},"Edit produced no outcome");
        require(!finished.last()[0].toBool(),"Negative quantity was stored");
        const QString rejected="Rejected by supplies.check_quantity: ValueError: Below zero · nothing stored";
        waitFor([&]{return window.findChild<QLabel *>("storageStatus")->text()==rejected;},"Rejection reason not shown");
        require(source->text()=="-1" && source->isModified(),"Rejected draft was not kept");
        waitFor([&]{return cell(1,1)=="2";},"Rejected edit changed B2");
    });
    require(workspace->grab().save(screenshot),"Workspace screenshot failed");
    QJsonObject result{{"passed",true},{"steps",steps},{"elapsed_ms",double(total.elapsed())},
        {"device_pixel_ratio",workspace->devicePixelRatioF()},{"main_window_active_at_end",window.isActiveWindow()},
        {"workspace_screenshot",QFileInfo(screenshot).fileName()},{"final_status",window.findChild<QLabel *>("storageStatus")->text()}};
    // Leave nothing for the close prompt: discard the rejected cell draft.
    QTest::keyClick(window.findChild<QLineEdit *>("source"),Qt::Key_Escape);
    return result;
}

int main(int argc,char **argv) {
    QApplication app(argc,argv);
    try {
        const bool onlyWorkspace=argc==3 && QByteArray(argv[2])=="pythonWorkspace";
        require(argc==2 || onlyWorkspace,"Usage: native_acceptance output.json [pythonWorkspace]");
        const auto plugin=QApplication::platformName();
        require(plugin!="offscreen" && plugin!="minimal","Acceptance requires a native platform plugin");
        QTemporaryDir temp; require(temp.isValid(),"Cannot create fixture directory");
        auto writeReport=[](const QString &path,const QJsonObject &report) {
            QFile output(path); require(output.open(QIODevice::WriteOnly),"Cannot save native report");
            const auto bytes=QJsonDocument(report).toJson(); require(output.write(bytes)==bytes.size(),"Native report truncated");
        };
        if(onlyWorkspace) {
            // The storyboard scenario alone, without the general keyboard and dialog checks.
            const QFileInfo reportFile(QString::fromLocal8Bit(argv[1]));
            const auto pythonWorkspace=pythonWorkspaceScenario(temp.path(),reportFile.dir().filePath(reportFile.completeBaseName()+"-python-workspace.png"));
            require(pythonWorkspace["device_pixel_ratio"].toDouble()>=qEnvironmentVariable("QT_SCALE_FACTOR","1").toDouble(),"Requested scale factor was not applied");
            writeReport(reportFile.filePath(),QJsonObject{{"recorded_at_utc",QDateTime::currentDateTimeUtc().toString(Qt::ISODate)},
                {"os",QSysInfo::prettyProductName()},{"qt",qVersion()},{"platform_plugin",plugin},
                {"scale_request",qEnvironmentVariable("QT_SCALE_FACTOR","1")},{"scenario","pythonWorkspace"},{"pythonWorkspace",pythonWorkspace},
                {"limitations",QJsonArray{"QTest widget events are synthetic; no physical input or compositor-latency qualification.",
                    "Run alone, without the general keyboard, clipboard and dialog checks of the full acceptance run.",
                    "No VoiceOver or spoken-output acceptance of the workspace window."}}});
            return 0;
        }
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
        const auto pythonWorkspace=pythonWorkspaceScenario(temp.path(),reportFile.dir().filePath(reportFile.completeBaseName()+"-python-workspace.png"));
        QJsonObject report{{"recorded_at_utc",QDateTime::currentDateTimeUtc().toString(Qt::ISODate)},
            {"os",QSysInfo::prettyProductName()},{"qt",qVersion()},{"platform_plugin",plugin},
            {"device_pixel_ratio",window.devicePixelRatioF()},{"scale_request",qEnvironmentVariable("QT_SCALE_FACTOR","1")},
            {"active_window",window.isActiveWindow()},{"accessible_controls",controls},{"job_accessible_controls",jobControls},{"job_dialog_tab_order",true},
            {"keyboard_edit_calculation_escape_navigation",true},{"clipboard_paste",true},
            {"acknowledged_enter_advances",true},
            {"keyboard_range_tab_and_editor_cancel",true},{"cell_editor_accessible_address",true},
            {"qt_modal_escape_without_mutation",true},{"narrow_window_usable_grid",true},
            {"frozen_header_shared_cache_geometry",true},{"native_wrap_border_and_column_size",true},
            {"native_hidden_gap_and_recovery_control",true},{"pythonWorkspace",pythonWorkspace},
            {"window_logical_width",window.width()},{"cell_logical_width",cell.width()},
            {"palette_base",grid->palette().color(QPalette::Base).name()},
            {"screenshot",QFileInfo(screenshot).fileName()},
            {"limitations",QJsonArray{"QTest widget events are synthetic; no physical input or compositor-latency qualification.",
                "Bounded keyboard accessibility is not full screen-reader table navigation or spoken-output acceptance.",
                "Native dialogs, multiple monitors and clean-machine installation remain separate acceptance."}}};
        writeReport(reportFile.filePath(),report);
        return 0;
    } catch(const std::exception &error) { qCritical("%s",error.what()); return 1; }
}
