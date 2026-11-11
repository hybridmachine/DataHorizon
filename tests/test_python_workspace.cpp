// SPDX-License-Identifier: GPL-3.0-only
#include "mainwindow.h"
#include "pythonworkspace.h"
#include "macrodeclarationpanel.h"
#include <QAction>
#include <QApplication>
#include <QCheckBox>
#include <QComboBox>
#include <QDialog>
#include <QDialogButtonBox>
#include <QFile>
#include <QInputDialog>
#include <QJsonArray>
#include <QLabel>
#include <QLineEdit>
#include <QListWidget>
#include <QMenuBar>
#include <QMessageBox>
#include <QPlainTextEdit>
#include <QProcess>
#include <QPushButton>
#include <QSettings>
#include <QSignalSpy>
#include <QStackedWidget>
#include <QTableView>
#include <QTemporaryDir>
#include <QTest>
#include <QTimer>
#include <functional>

namespace {

// Answers modal dialogs while a test drives the workspace; handle() returns
// true when it acted on the dialog.
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

bool boot(MainWindow &window,const QString &path,bool create=true,bool readonly=false) {
    QSignalSpy ready(&window,&MainWindow::serviceReady);window.show();
    if(!QTest::qWaitFor([&]{return ready.count()>=1;},10000))return false;
    QSignalSpy opened(&window,&MainWindow::workbookReady);window.openBook(path,create,readonly);
    return QTest::qWaitFor([&]{return opened.count()>=1;},10000);
}

// Reads the stored catalog through the window's own REST session. The grid
// model's state can trail a just-committed save until its refresh lands, so a
// stale-state read is retried.
QJsonObject readCode(MainWindow &window) {
    auto *client=window.findChild<RestClient *>();QJsonObject result;
    for(int attempt=0;attempt<100;++attempt) {
        bool done=false;
        client->request("/v1/workbooks/"+window.model()->workbook()+"/code",{{"expected_state",window.model()->state()}},
            [&](QJsonObject reply,QString error){result=error.isEmpty()?reply:QJsonObject{{"error",error},{"code",reply["code"]}};done=true;});
        if(!QTest::qWaitFor([&]{return done;},10000) || result["code"]!="STALE_STATE")break;
        QTest::qWait(50);
    }
    return result;
}

PythonWorkspace *openWorkspace(MainWindow &window) {
    window.findChild<QAction *>("pythonWorkspaceAction")->trigger();
    auto *workspace=window.findChild<PythonWorkspace *>("pythonWorkspace");
    if(workspace)(void)QTest::qWaitFor([&]{return workspace->isVisible() && !workspace->fingerprint().isEmpty();},10000);
    return workspace;
}

bool createModule(PythonWorkspace *workspace,const QString &name) {
    ModalDriver driver([name](QWidget *modal){
        auto *input=qobject_cast<QInputDialog *>(modal);if(!input || input->objectName()!="newModuleName")return false;
        input->setTextValue(name);input->accept();return true;});
    workspace->findChild<QPushButton *>("newModuleButton")->click();
    return driver.handled==1;
}

QString status(PythonWorkspace *workspace){return workspace->findChild<QLabel *>("workspaceStatus")->text();}
QPlainTextEdit *editor(PythonWorkspace *workspace){return workspace->findChild<QPlainTextEdit *>("codeEditor");}
QString issues(MainWindow &window){return window.findChild<QPlainTextEdit *>("operationIssues")->toPlainText();}

void save(PythonWorkspace *workspace){workspace->findChild<QAction *>("saveModuleAction")->trigger();}

// Stores a module change through a second window and service on the same
// file, the way another user or script would. Omit module to create one.
bool competingSave(const StartupOptions &options,const QString &path,const QString &name,const QString &source,const QString &module={}) {
    MainWindow other(options);if(!boot(other,path,false))return false;
    const auto code=readCode(other);QSignalSpy finished(&other,&MainWindow::operationFinished);
    QJsonObject request{{"action","module_save"},{"name",name},{"source",source},{"macros",QJsonArray{}},{"fingerprint",code["fingerprint"]}};
    if(!module.isEmpty())request["module"]=module;
    other.submit(request);
    return QTest::qWaitFor([&]{return finished.count()==1;},10000) && finished[0][0].toBool();
}

bool refreshWindow(MainWindow &window) {
    QSignalSpy opened(&window,&MainWindow::workbookReady);
    for(auto *action:window.findChildren<QAction *>())if(action->text()=="Refresh stored data")action->trigger();
    return QTest::qWaitFor([&]{return opened.count()>=1;},10000);
}

bool clickButton(QWidget *modal,const QString &dialog,const QString &button) {
    if(modal->objectName()!=dialog)return false;
    auto *target=modal->findChild<QPushButton *>(button);if(!target)return false;
    target->click();return true;
}

const QString kDraft=QStringLiteral("Draft · not saved to the workbook");
const QString kSaved=QStringLiteral("Saved in workbook");

}

class PythonWorkspaceTests : public QObject {
    Q_OBJECT
private slots:
    void init(){PythonWorkspace::resetSessionChoices();}

    void opensAsSeparateTopLevelWindow() {
        QTemporaryDir temp;StartupOptions options;options.automaticWorkbook=false;MainWindow window(options);
        QVERIFY(boot(window,temp.filePath("separate.dhwb")));
        QStringList menus;for(auto *action:window.menuBar()->actions())menus.append(action->text());
        QVERIFY(menus.indexOf("&Tools")==menus.indexOf("&Data")+1);QVERIFY(menus.indexOf("&View")==menus.indexOf("&Tools")+1);
        QCOMPARE(window.findChild<QAction *>("pythonWorkspaceAction")->text(),QString("Python workspace"));
        auto *workspace=openWorkspace(window);QVERIFY(workspace);
        QVERIFY(workspace->isWindow());QVERIFY(workspace->isVisible());QVERIFY(workspace->window()!=&window);
        QVERIFY(window.isVisible());QVERIFY(window.findChild<QTableView *>("grid")->isVisible());
        auto *side=workspace->findChild<QStackedWidget *>("sidePanel");QVERIFY(side);QVERIFY(side->currentWidget()->findChild<MacroDeclarationPanel *>("macroPanel"));
        QCOMPARE(workspace->findChild<QLabel *>("workspaceApproval")->text(),QString("Not approved to run"));
    }

    void emptyStateOffersNewModule() {
        QTemporaryDir temp;StartupOptions options;options.automaticWorkbook=false;MainWindow window(options);
        QVERIFY(boot(window,temp.filePath("empty.dhwb")));
        auto *workspace=openWorkspace(window);QVERIFY(workspace);
        QCOMPARE(workspace->findChild<QListWidget *>("moduleList")->count(),0);
        auto *button=workspace->findChild<QPushButton *>("newModuleButton");QVERIFY(button->isVisible());QVERIFY(button->isEnabled());
        QVERIFY(!workspace->findChild<QAction *>("saveModuleAction")->isEnabled());
    }

    void typingCreatesDraftNotStoredCode() {
        QTemporaryDir temp;StartupOptions options;options.automaticWorkbook=false;MainWindow window(options);
        QVERIFY(boot(window,temp.filePath("draft.dhwb")));
        auto *workspace=openWorkspace(window);QVERIFY(createModule(workspace,"supplies"));
        QCOMPARE(workspace->findChild<QListWidget *>("moduleList")->count(),1);
        editor(workspace)->setFocus();QTest::keyClicks(editor(workspace),"pens = 3");
        QCOMPARE(status(workspace),kDraft);
        QTest::keyClick(editor(workspace),Qt::Key_Return);QTest::keyClick(editor(workspace),Qt::Key_Tab);
        QCOMPARE(editor(workspace)->toPlainText(),QString("pens = 3\n    "));QVERIFY(workspace->hasDrafts());
        QVERIFY(workspace->findChild<QAction *>("saveModuleAction")->isEnabled());
        const auto code=readCode(window);QVERIFY(code.contains("modules"));QCOMPARE(code["modules"].toArray().size(),0);
    }

    void saveStoresModuleAndDeclaration() {
        QTemporaryDir temp;StartupOptions options;options.automaticWorkbook=false;MainWindow window(options);
        QVERIFY(boot(window,temp.filePath("save.dhwb")));
        auto *workspace=openWorkspace(window);QVERIFY(createModule(workspace,"supplies"));
        editor(workspace)->setPlainText("def restock_pens(ctx, extra):\n    return extra\n");
        ModalDriver driver([](QWidget *modal){
            if(modal->objectName()!="addMacroDialog")return false;auto *panel=modal->findChild<MacroDeclarationPanel *>("newMacroPanel");
            panel->setDeclaration({{"name","restock_pens"},{"parameters",QJsonArray{QJsonObject{{"name","extra"},{"type","integer"},{"nullable",false},{"default",2}}}},{"result",QJsonObject{{"type","integer"},{"nullable",false}}}});
            qobject_cast<QDialog *>(modal)->accept();return true;});
        workspace->findChild<QPushButton *>("addMacroButton")->click();QCOMPARE(driver.handled,1);
        QCOMPARE(editor(workspace)->toPlainText().count("def restock_pens"),1);
        QCOMPARE(workspace->findChild<QListWidget *>("macroList")->count(),1);
        save(workspace);QTRY_COMPARE_WITH_TIMEOUT(status(workspace),kSaved,10000);QVERIFY(!workspace->hasDrafts());
        const auto modules=readCode(window)["modules"].toArray();QCOMPARE(modules.size(),1);
        const auto module=modules[0].toObject();QCOMPARE(module["name"].toString(),QString("supplies"));
        const auto macro=module["macros"].toArray()[0].toObject();QCOMPARE(macro["name"].toString(),QString("restock_pens"));
        const auto parameter=macro["parameters"].toArray()[0].toObject();QCOMPARE(parameter["name"].toString(),QString("extra"));QCOMPARE(parameter["type"].toString(),QString("integer"));QCOMPARE(parameter["default"].toInt(),2);
        QCOMPARE(macro["result"].toObject()["type"].toString(),QString("integer"));
        QTRY_COMPARE_WITH_TIMEOUT(workspace->fingerprint(),readCode(window)["fingerprint"].toString(),5000);
    }

    void addingDeclarationInsertsStub() {
        QTemporaryDir temp;StartupOptions options;options.automaticWorkbook=false;MainWindow window(options);
        QVERIFY(boot(window,temp.filePath("stub.dhwb")));
        auto *workspace=openWorkspace(window);QVERIFY(createModule(workspace,"supplies"));
        ModalDriver driver([](QWidget *modal){
            if(modal->objectName()!="addMacroDialog")return false;auto *panel=modal->findChild<MacroDeclarationPanel *>("newMacroPanel");
            panel->findChild<QLineEdit *>("macroName")->setText("restock_pens");panel->findChild<QPushButton *>("addArgumentButton")->click();
            panel->findChild<QLineEdit *>("argumentName_0")->setText("extra");
            auto *type=panel->findChild<QComboBox *>("argumentType_0");type->setCurrentIndex(type->findData("integer"));
            panel->findChild<QComboBox *>("macroResultType")->setCurrentIndex(panel->findChild<QComboBox *>("macroResultType")->findData("integer"));
            qobject_cast<QDialog *>(modal)->accept();return true;});
        workspace->findChild<QPushButton *>("addMacroButton")->click();QCOMPARE(driver.handled,1);
        QVERIFY(editor(workspace)->toPlainText().contains("def restock_pens(ctx, extra):"));
        QCOMPARE(status(workspace),kDraft);
        auto *panel=workspace->findChild<MacroDeclarationPanel *>("macroPanel");
        const auto declaration=panel->declaration();QCOMPARE(declaration["name"].toString(),QString("restock_pens"));
        QCOMPARE(declaration["parameters"].toArray()[0].toObject()["type"].toString(),QString("integer"));
        // Editing the selected declaration in the side panel changes the draft, not the service.
        panel->findChild<QCheckBox *>("macroResultNullable")->setChecked(true);
        save(workspace);QTRY_COMPARE_WITH_TIMEOUT(status(workspace),kSaved,10000);
        const auto macro=readCode(window)["modules"].toArray()[0].toObject()["macros"].toArray()[0].toObject();
        QCOMPARE(macro["result"].toObject()["nullable"].toBool(),true);QCOMPARE(macro["parameters"].toArray()[0].toObject()["name"].toString(),QString("extra"));
    }

    void staleSaveKeepsDraft() {
        QTemporaryDir temp;const auto path=temp.filePath("stale.dhwb");StartupOptions options;options.automaticWorkbook=false;MainWindow window(options);
        QVERIFY(boot(window,path));
        auto *workspace=openWorkspace(window);QVERIFY(createModule(workspace,"supplies"));
        const QString typed="def restock(ctx):\n    return 1\n";editor(workspace)->setPlainText(typed);QCOMPARE(status(workspace),kDraft);
        {
            MainWindow other(options);QVERIFY(boot(other,path,false));
            const auto code=readCode(other);QSignalSpy finished(&other,&MainWindow::operationFinished);
            other.submit({{"action","module_save"},{"name","other"},{"source","def g(ctx):\n    return 2\n"},{"macros",QJsonArray{}},{"fingerprint",code["fingerprint"]}});
            QTRY_COMPARE_WITH_TIMEOUT(finished.count(),1,10000);QCOMPARE(finished[0][0].toBool(),true);
        }
        QSignalSpy opened(&window,&MainWindow::workbookReady);
        for(auto *action:window.findChildren<QAction *>())if(action->text()=="Refresh stored data")action->trigger();
        QTRY_VERIFY_WITH_TIMEOUT(opened.count()>=1,10000);
        QTRY_COMPARE_WITH_TIMEOUT(workspace->findChild<QListWidget *>("moduleList")->count(),2,5000);
        int conflicts=0;ModalDriver noConflict([&](QWidget *modal){if(modal->objectName()!="codeConflictDialog")return false;++conflicts;qobject_cast<QDialog *>(modal)->reject();return true;});
        save(workspace);
        QTRY_VERIFY_WITH_TIMEOUT(issues(window).contains("CODE_CHANGED"),10000);
        QCOMPARE(editor(workspace)->toPlainText(),typed);QCOMPARE(status(workspace),kDraft);QVERIFY(workspace->hasDrafts());
        QTRY_VERIFY_WITH_TIMEOUT(workspace->findChild<QLabel *>("workspaceMessage")->text().contains("other"),5000);
        auto modules=readCode(window)["modules"].toArray();QCOMPARE(modules.size(),1);QCOMPARE(modules[0].toObject()["name"].toString(),QString("other"));
        // After reviewing what changed, saving again stores the draft beside the competing module.
        save(workspace);QTRY_COMPARE_WITH_TIMEOUT(status(workspace),kSaved,10000);
        modules=readCode(window)["modules"].toArray();QCOMPARE(modules.size(),2);QCOMPARE(conflicts,0);
        QVERIFY(!workspace->findChild<QLabel *>("workspaceConflict")->isVisible());
    }

    void sameModuleConflictCancelThenTakeStored() {
        QTemporaryDir temp;const auto path=temp.filePath("conflict.dhwb");StartupOptions options;options.automaticWorkbook=false;MainWindow window(options);
        QVERIFY(boot(window,path));
        auto *workspace=openWorkspace(window);QVERIFY(createModule(workspace,"supplies"));editor(workspace)->setPlainText("pens = 1\n");
        save(workspace);QTRY_COMPARE_WITH_TIMEOUT(status(workspace),kSaved,10000);
        const auto id=readCode(window)["modules"].toArray()[0].toObject()["id"].toString();
        editor(workspace)->setPlainText("pens = 2  # mine\n");
        QVERIFY(competingSave(options,path,"supplies","pens = 3  # theirs\n",id));
        QVERIFY(refreshWindow(window));
        auto *banner=workspace->findChild<QLabel *>("workspaceConflict");QTRY_VERIFY_WITH_TIMEOUT(banner->isVisible(),5000);
        QCOMPARE(editor(workspace)->toPlainText(),QString("pens = 2  # mine\n"));
        // Unrelated actions do not clear the warning.
        workspace->findChild<QAction *>("findAction")->trigger();workspace->findChild<QLineEdit *>("findField")->setText("zzz");
        QTest::keyClick(workspace->findChild<QLineEdit *>("findField"),Qt::Key_Return);QVERIFY(banner->isVisible());
        QString stored,mine,title;QString choice="conflictCancel";bool storedReadOnly=false;
        ModalDriver driver([&](QWidget *modal){
            if(modal->objectName()!="codeConflictDialog")return false;title=modal->windowTitle();
            stored=modal->findChild<QPlainTextEdit *>("conflictStoredSource")->toPlainText();mine=modal->findChild<QPlainTextEdit *>("conflictDraftSource")->toPlainText();
            storedReadOnly=modal->findChild<QPlainTextEdit *>("conflictStoredSource")->isReadOnly();
            return clickButton(modal,"codeConflictDialog",choice);});
        save(workspace);QCOMPARE(driver.handled,1);
        QCOMPARE(title,QString("This module changed in the workbook"));QCOMPARE(stored,QString("pens = 3  # theirs\n"));QCOMPARE(mine,QString("pens = 2  # mine\n"));QVERIFY(storedReadOnly);
        QCOMPARE(readCode(window)["modules"].toArray()[0].toObject()["source"].toString(),QString("pens = 3  # theirs\n"));
        QVERIFY(workspace->hasDrafts());QVERIFY(banner->isVisible());QCOMPARE(status(workspace),kDraft);
        choice="conflictTakeStored";save(workspace);QCOMPARE(driver.handled,2);
        QVERIFY(!workspace->hasDrafts());QCOMPARE(editor(workspace)->toPlainText(),QString("pens = 3  # theirs\n"));QCOMPARE(status(workspace),kSaved);QVERIFY(!banner->isVisible());
        QCOMPARE(readCode(window)["modules"].toArray()[0].toObject()["source"].toString(),QString("pens = 3  # theirs\n"));
    }

    void sameModuleConflictKeepMineReplaces() {
        QTemporaryDir temp;const auto path=temp.filePath("keep.dhwb");StartupOptions options;options.automaticWorkbook=false;MainWindow window(options);
        QVERIFY(boot(window,path));
        auto *workspace=openWorkspace(window);QVERIFY(createModule(workspace,"supplies"));editor(workspace)->setPlainText("pens = 1\n");
        save(workspace);QTRY_COMPARE_WITH_TIMEOUT(status(workspace),kSaved,10000);
        const auto id=readCode(window)["modules"].toArray()[0].toObject()["id"].toString();
        editor(workspace)->setPlainText("pens = 2  # mine\n");
        QVERIFY(competingSave(options,path,"supplies","pens = 3  # theirs\n",id));
        QVERIFY(refreshWindow(window));QTRY_VERIFY_WITH_TIMEOUT(workspace->findChild<QLabel *>("workspaceConflict")->isVisible(),5000);
        ModalDriver driver([](QWidget *modal){return clickButton(modal,"codeConflictDialog","conflictKeepMine");});
        save(workspace);QCOMPARE(driver.handled,1);QTRY_COMPARE_WITH_TIMEOUT(status(workspace),kSaved,10000);
        const auto module=readCode(window)["modules"].toArray()[0].toObject();
        QCOMPARE(module["source"].toString(),QString("pens = 2  # mine\n"));QCOMPARE(module["id"].toString(),id);
        QVERIFY(!workspace->hasDrafts());
    }

    void validationErrorKeepsDraft() {
        QTemporaryDir temp;StartupOptions options;options.automaticWorkbook=false;MainWindow window(options);
        QVERIFY(boot(window,temp.filePath("invalid.dhwb")));
        auto *workspace=openWorkspace(window);QVERIFY(createModule(workspace,"supplies"));
        const QString typed="def broken(:\n    pass\n";editor(workspace)->setPlainText(typed);
        save(workspace);
        auto *message=workspace->findChild<QLabel *>("workspaceMessage");
        QTRY_VERIFY_WITH_TIMEOUT(message->text().contains("Invalid module syntax"),10000);
        QVERIFY(issues(window).contains("INVALID_CODE"));
        QCOMPARE(editor(workspace)->extraSelections().size(),1);
        QCOMPARE(editor(workspace)->extraSelections()[0].cursor.blockNumber(),0);
        QCOMPARE(editor(workspace)->toPlainText(),typed);QCOMPARE(status(workspace),kDraft);QVERIFY(workspace->hasDrafts());
        QCOMPARE(readCode(window)["modules"].toArray().size(),0);
        QVERIFY(workspace->findChild<QAction *>("saveModuleAction")->isEnabled());
    }

    void firstSaveWithHistoryAsksOnce() {
        QTemporaryDir temp;StartupOptions options;options.automaticWorkbook=false;MainWindow window(options);
        QVERIFY(boot(window,temp.filePath("history.dhwb")));
        QSignalSpy finished(&window,&MainWindow::operationFinished);
        window.submit({{"action","edit"},{"cells",QJsonArray{QJsonObject{{"row",1},{"col",1},{"source","7"}}}}});QTRY_COMPARE_WITH_TIMEOUT(finished.count(),1,10000);
        auto *workspace=openWorkspace(window);QTRY_VERIFY_WITH_TIMEOUT(workspace->findChild<QPushButton *>("newModuleButton")->isEnabled(),5000);
        QVERIFY(createModule(workspace,"supplies"));editor(workspace)->setPlainText("pens = 1\n");
        QString prompt;
        ModalDriver driver([&](QWidget *modal){
            auto *box=qobject_cast<QMessageBox *>(modal);if(!box || box->objectName()!="codeSaveHistoryPrompt")return false;
            prompt=box->text();box->checkBox()->setChecked(true);box->button(QMessageBox::Yes)->click();return true;});
        save(workspace);QTRY_COMPARE_WITH_TIMEOUT(status(workspace),kSaved,10000);
        QCOMPARE(driver.handled,1);QCOMPARE(prompt,QString("Saving code clears Undo history for this workbook. Continue?"));
        window.submit({{"action","edit"},{"cells",QJsonArray{QJsonObject{{"row",2},{"col",1},{"source","8"}}}}});QTRY_COMPARE_WITH_TIMEOUT(finished.count(),3,10000);
        QTRY_VERIFY_WITH_TIMEOUT(workspace->findChild<QPushButton *>("newModuleButton")->isEnabled(),5000);
        editor(workspace)->setPlainText("pens = 2\n");QCOMPARE(status(workspace),kDraft);
        save(workspace);QTRY_COMPARE_WITH_TIMEOUT(status(workspace),kSaved,10000);
        QCOMPARE(driver.handled,1);
        QCOMPARE(readCode(window)["modules"].toArray()[0].toObject()["source"].toString(),QString("pens = 2\n"));
    }

    void closeWithDraftPromptsAndCancelStays() {
        QTemporaryDir temp;StartupOptions options;options.automaticWorkbook=false;MainWindow window(options);
        QVERIFY(boot(window,temp.filePath("close.dhwb")));
        auto *workspace=openWorkspace(window);QVERIFY(createModule(workspace,"supplies"));editor(workspace)->setPlainText("pens = 1\n");
        QMessageBox::StandardButton answer=QMessageBox::Cancel;QString text;
        ModalDriver driver([&](QWidget *modal){
            auto *box=qobject_cast<QMessageBox *>(modal);if(!box || box->objectName()!="pythonDraftsPrompt")return false;
            text=box->text();box->button(answer)->click();return true;});
        QVERIFY(!window.close());QCOMPARE(driver.handled,1);QVERIFY(text.contains("supplies"));
        QVERIFY(window.isVisible());QVERIFY(workspace->isVisible());QVERIFY(workspace->hasDrafts());
        QCOMPARE(editor(workspace)->toPlainText(),QString("pens = 1\n"));
        answer=QMessageBox::Discard;QVERIFY(window.close());QCOMPARE(driver.handled,2);
        QVERIFY(!window.isVisible());QVERIFY(!workspace->isVisible());QVERIFY(!workspace->hasDrafts());
    }

    void readOnlyWorkbookDisablesSaveWithReason() {
        QTemporaryDir temp;const auto path=temp.filePath("readonly.dhwb");StartupOptions options;options.automaticWorkbook=false;MainWindow window(options);
        QVERIFY(boot(window,path));
        auto *workspace=openWorkspace(window);QVERIFY(createModule(workspace,"supplies"));editor(workspace)->setPlainText("pens = 1\n");
        save(workspace);QTRY_COMPARE_WITH_TIMEOUT(status(workspace),kSaved,10000);
        QSignalSpy opened(&window,&MainWindow::workbookReady);window.openBook(path,false,true);QTRY_VERIFY_WITH_TIMEOUT(opened.count()>=1,10000);
        QTRY_COMPARE_WITH_TIMEOUT(status(workspace),QString("Read-only workbook"),5000);
        QTRY_COMPARE_WITH_TIMEOUT(workspace->findChild<QListWidget *>("moduleList")->count(),1,5000);
        QTRY_COMPARE_WITH_TIMEOUT(editor(workspace)->toPlainText(),QString("pens = 1\n"),5000);
        auto *saveAction=workspace->findChild<QAction *>("saveModuleAction");QVERIFY(!saveAction->isEnabled());
        QVERIFY(saveAction->toolTip().contains("read-only",Qt::CaseInsensitive));
        auto *button=workspace->findChild<QPushButton *>("newModuleButton");QVERIFY(button->isVisible());QVERIFY(!button->isEnabled());QVERIFY(button->toolTip().contains("read-only",Qt::CaseInsensitive));
        QVERIFY(editor(workspace)->isReadOnly());
        for(const auto *name:{"renameModuleButton","deleteModuleButton"}){auto *control=workspace->findChild<QPushButton *>(name);QVERIFY(!control->isEnabled());QVERIFY(control->toolTip().contains("read-only",Qt::CaseInsensitive));}
        QTest::keyClicks(editor(workspace),"x");QVERIFY(!workspace->hasDrafts());
    }

    void findSelectsNextMatchAndWraps() {
        QTemporaryDir temp;StartupOptions options;options.automaticWorkbook=false;MainWindow window(options);
        QVERIFY(boot(window,temp.filePath("find.dhwb")));
        auto *workspace=openWorkspace(window);QVERIFY(createModule(workspace,"supplies"));
        const QString source="pens = 1\npens += 2\nprint(pens)\n";editor(workspace)->setPlainText(source);
        auto cursor=editor(workspace)->textCursor();cursor.setPosition(0);editor(workspace)->setTextCursor(cursor);
        workspace->activateWindow();workspace->findChild<QAction *>("findAction")->trigger();
        auto *field=workspace->findChild<QLineEdit *>("findField");QVERIFY(field->isVisible());
        QCOMPARE(workspace->findChild<QAction *>("findAction")->shortcut(),QKeySequence(QKeySequence::Find));
        field->setText("pens");
        const QList<int> starts{0,int(source.indexOf("pens",1)),int(source.lastIndexOf("pens")),0};
        for(int start:starts){
            QTest::keyClick(field,Qt::Key_Return);
            const auto selection=editor(workspace)->textCursor();
            QCOMPARE(selection.selectionStart(),start);QCOMPARE(selection.selectedText(),QString("pens"));
        }
    }

    void unknownOutcomeCloseStillPromptsForDrafts() {
        QTemporaryDir temp;StartupOptions options;options.automaticWorkbook=false;options.recoveryDirectory=temp.filePath("recovery");
        options.handler=QString(DATAHORIZON_FIXTURES)+"/timeout_hook.py";MainWindow window(options);
        QVERIFY(boot(window,temp.filePath("unknown.dhwb")));
        auto *workspace=openWorkspace(window);QVERIFY(createModule(workspace,"supplies"));editor(workspace)->setPlainText("pens = 1\n");
        window.submit({{"action","edit"},{"cells",QJsonArray{QJsonObject{{"row",1},{"col",1},{"source","7"}}}}});
        auto *process=window.findChild<QProcess *>("ownedService");process->kill();QTRY_COMPARE_WITH_TIMEOUT(process->state(),QProcess::NotRunning,5000);
        QMessageBox::StandardButton drafts=QMessageBox::Cancel;int unknown=0,prompts=0;
        ModalDriver driver([&](QWidget *modal){
            auto *box=qobject_cast<QMessageBox *>(modal);if(!box)return false;
            if(box->objectName()=="pythonDraftsPrompt"){++prompts;box->button(drafts)->click();return true;}
            if(box->text().startsWith("The edit may have committed.")){++unknown;box->button(QMessageBox::Yes)->click();return true;}
            return false;});
        QVERIFY(!window.close());QCOMPARE(unknown,1);QCOMPARE(prompts,1);QVERIFY(window.isVisible());QVERIFY(workspace->hasDrafts());
        drafts=QMessageBox::Discard;QVERIFY(window.close());QCOMPARE(unknown,2);QCOMPARE(prompts,2);QVERIFY(!workspace->hasDrafts());QVERIFY(!workspace->isVisible());
    }

    void discardThenCancellingLaterClosePromptKeepsDrafts() {
        QTemporaryDir temp;StartupOptions options;options.automaticWorkbook=false;MainWindow window(options);
        QVERIFY(boot(window,temp.filePath("ordering.dhwb")));
        auto *workspace=openWorkspace(window);QVERIFY(createModule(workspace,"supplies"));editor(workspace)->setPlainText("pens = 1\n");
        auto *formula=window.findChild<QLineEdit *>("source");formula->setText("42");formula->setModified(true);
        QMessageBox::StandardButton editorAnswer=QMessageBox::No;int prompts=0,editorPrompts=0;
        ModalDriver driver([&](QWidget *modal){
            auto *box=qobject_cast<QMessageBox *>(modal);if(!box)return false;
            if(box->objectName()=="pythonDraftsPrompt"){++prompts;box->button(QMessageBox::Discard)->click();return true;}
            if(box->text().startsWith("Discard the unsubmitted formula-bar draft?")){++editorPrompts;box->button(editorAnswer)->click();return true;}
            return false;});
        QVERIFY(!window.close());QCOMPARE(prompts,1);QCOMPARE(editorPrompts,1);
        QVERIFY(window.isVisible());QVERIFY(workspace->hasDrafts());QCOMPARE(editor(workspace)->toPlainText(),QString("pens = 1\n"));
        editorAnswer=QMessageBox::Yes;QVERIFY(window.close());QCOMPARE(prompts,2);QCOMPARE(editorPrompts,2);QVERIFY(!workspace->hasDrafts());
    }

    void openingAnotherWorkbookPromptsAndCancelKeepsBoth() {
        QTemporaryDir temp;StartupOptions options;options.automaticWorkbook=false;MainWindow window(options);
        QVERIFY(boot(window,temp.filePath("first.dhwb")));const auto first=window.model()->workbook();
        auto *workspace=openWorkspace(window);QVERIFY(createModule(workspace,"supplies"));editor(workspace)->setPlainText("pens = 1\n");
        QMessageBox::StandardButton answer=QMessageBox::Cancel;int prompts=0;
        ModalDriver driver([&](QWidget *modal){
            auto *box=qobject_cast<QMessageBox *>(modal);if(!box || box->objectName()!="pythonDraftsPrompt")return false;++prompts;box->button(answer)->click();return true;});
        const auto second=temp.filePath("second.dhwb");window.openBook(second,true);
        QCOMPARE(prompts,1);QCOMPARE(window.model()->workbook(),first);QVERIFY(!QFile::exists(second));QVERIFY(workspace->hasDrafts());
        // Discard is applied only once the other workbook opens: a failed open keeps the drafts.
        answer=QMessageBox::Discard;window.openBook(temp.filePath("missing/directory/third.dhwb"),true);
        QCOMPARE(prompts,2);QTRY_VERIFY_WITH_TIMEOUT(issues(window).contains("third.dhwb") || window.findChild<QLabel *>("storageStatus")->text().contains("Open failed"),10000);
        QTRY_VERIFY_WITH_TIMEOUT(workspace->findChild<QPushButton *>("newModuleButton")->isEnabled(),5000);
        QCOMPARE(window.model()->workbook(),first);QVERIFY(workspace->hasDrafts());
        QSignalSpy opened(&window,&MainWindow::workbookReady);window.openBook(second,true);QTRY_VERIFY_WITH_TIMEOUT(opened.count()>=1,10000);
        QCOMPARE(prompts,3);QVERIFY(window.model()->workbook()!=first);QVERIFY(!workspace->hasDrafts());
    }

    void renameKeepsModuleIdentityAndSource() {
        QTemporaryDir temp;StartupOptions options;options.automaticWorkbook=false;MainWindow window(options);
        QVERIFY(boot(window,temp.filePath("rename.dhwb")));
        auto *workspace=openWorkspace(window);QVERIFY(createModule(workspace,"supplies"));editor(workspace)->setPlainText("pens = 1\n");
        auto *rename=workspace->findChild<QPushButton *>("renameModuleButton");QVERIFY(!rename->isEnabled());QVERIFY(rename->toolTip().contains("Save"));
        save(workspace);QTRY_COMPARE_WITH_TIMEOUT(status(workspace),kSaved,10000);QTRY_VERIFY_WITH_TIMEOUT(rename->isEnabled(),5000);
        const auto before=readCode(window)["modules"].toArray()[0].toObject();
        editor(workspace)->setPlainText("pens = 2\n");QVERIFY(!rename->isEnabled());QVERIFY(rename->toolTip().contains("draft"));
        ModalDriver discard([](QWidget *modal){auto *box=qobject_cast<QMessageBox *>(modal);if(!box || box->objectName()!="discardModuleDraft")return false;box->button(QMessageBox::Discard)->click();return true;});
        workspace->findChild<QPushButton *>("discardDraftButton")->click();QVERIFY(rename->isEnabled());
        ModalDriver driver([](QWidget *modal){auto *input=qobject_cast<QInputDialog *>(modal);if(!input || input->objectName()!="renameModuleName")return false;input->setTextValue("stock");input->accept();return true;});
        rename->click();QCOMPARE(driver.handled,1);
        QTRY_COMPARE_WITH_TIMEOUT(workspace->findChild<QListWidget *>("moduleList")->item(0)->text(),QString("stock"),10000);
        const auto after=readCode(window)["modules"].toArray()[0].toObject();
        QCOMPARE(after["name"].toString(),QString("stock"));QCOMPARE(after["id"],before["id"]);QCOMPARE(after["source"],before["source"]);QCOMPARE(after["macros"],before["macros"]);
    }

    void deleteRemovesModuleFromCatalog() {
        QTemporaryDir temp;StartupOptions options;options.automaticWorkbook=false;MainWindow window(options);
        QVERIFY(boot(window,temp.filePath("delete.dhwb")));
        auto *workspace=openWorkspace(window);QVERIFY(createModule(workspace,"supplies"));editor(workspace)->setPlainText("pens = 1\n");
        save(workspace);QTRY_COMPARE_WITH_TIMEOUT(status(workspace),kSaved,10000);
        editor(workspace)->setPlainText("pens = 2\n");QVERIFY(workspace->hasDrafts());
        QTRY_VERIFY_WITH_TIMEOUT(workspace->findChild<QPushButton *>("deleteModuleButton")->isEnabled(),5000);
        QString text;
        ModalDriver driver([&](QWidget *modal){auto *box=qobject_cast<QMessageBox *>(modal);if(!box || box->objectName()!="deleteModuleConfirm")return false;text=box->text();box->button(QMessageBox::Yes)->click();return true;});
        workspace->findChild<QPushButton *>("deleteModuleButton")->click();QCOMPARE(driver.handled,1);
        QCOMPARE(text,QString("Delete module supplies? This cannot be undone with Undo."));
        QTRY_COMPARE_WITH_TIMEOUT(workspace->findChild<QListWidget *>("moduleList")->count(),0,10000);
        QVERIFY(!workspace->hasDrafts());QCOMPARE(readCode(window)["modules"].toArray().size(),0);
    }

    void deletingModuleUsedByBindingShowsRejection() {
        QTemporaryDir temp;StartupOptions options;options.automaticWorkbook=false;MainWindow window(options);
        QVERIFY(boot(window,temp.filePath("inuse.dhwb")));
        auto *workspace=openWorkspace(window);QVERIFY(createModule(workspace,"supplies"));editor(workspace)->setPlainText("def check(ctx, event):\n    pass\n");
        save(workspace);QTRY_COMPARE_WITH_TIMEOUT(status(workspace),kSaved,10000);
        const auto code=readCode(window);QSignalSpy finished(&window,&MainWindow::operationFinished);
        window.submit({{"action","binding_save"},{"event","cell_change"},{"scope",QJsonObject{{"kind","sheet"},{"sheet",window.model()->sheet()}}},{"module","supplies"},{"function","check"},{"required",false},{"fingerprint",code["fingerprint"]}});
        QTRY_COMPARE_WITH_TIMEOUT(finished.count(),1,10000);QVERIFY2(finished[0][0].toBool(),qPrintable(issues(window)));
        QTRY_COMPARE_WITH_TIMEOUT(workspace->fingerprint(),readCode(window)["fingerprint"].toString(),10000);
        QTRY_VERIFY_WITH_TIMEOUT(workspace->findChild<QPushButton *>("deleteModuleButton")->isEnabled(),5000);
        ModalDriver driver([](QWidget *modal){auto *box=qobject_cast<QMessageBox *>(modal);if(!box || box->objectName()!="deleteModuleConfirm")return false;box->button(QMessageBox::Yes)->click();return true;});
        workspace->findChild<QPushButton *>("deleteModuleButton")->click();QCOMPARE(driver.handled,1);
        QTRY_VERIFY_WITH_TIMEOUT(workspace->findChild<QLabel *>("workspaceMessage")->text().contains("MODULE_IN_USE"),10000);
        QVERIFY(issues(window).contains("MODULE_IN_USE"));
        QCOMPARE(readCode(window)["modules"].toArray().size(),1);QCOMPARE(workspace->findChild<QListWidget *>("moduleList")->count(),1);
        QTRY_VERIFY_WITH_TIMEOUT(workspace->findChild<QPushButton *>("deleteModuleButton")->isEnabled(),5000);
    }

    void geometryIsRestoredOnReopen() {
        QTemporaryDir temp;StartupOptions options;options.automaticWorkbook=false;options.viewStatePath=temp.filePath("views.ini");
        {
            MainWindow window(options);QVERIFY(boot(window,temp.filePath("geometry.dhwb")));
            auto *workspace=openWorkspace(window);workspace->resize(777,555);QTRY_COMPARE_WITH_TIMEOUT(workspace->size(),QSize(777,555),5000);
            QVERIFY(window.close());QVERIFY(!workspace->isVisible());
        }
        QVERIFY(QSettings(options.viewStatePath,QSettings::IniFormat).contains("pythonWorkspace/geometry"));
        MainWindow window(options);QVERIFY(boot(window,temp.filePath("geometry.dhwb"),false));
        auto *workspace=openWorkspace(window);QCOMPARE(workspace->size(),QSize(777,555));
    }

    void serviceRestartKeepsDraftsAndClearsApproval() {
        QTemporaryDir temp;StartupOptions options;options.automaticWorkbook=false;options.recoveryDirectory=temp.filePath("recovery");MainWindow window(options);
        QVERIFY(boot(window,temp.filePath("restart.dhwb")));
        auto *workspace=openWorkspace(window);QVERIFY(createModule(workspace,"supplies"));editor(workspace)->setPlainText("pens = 1\n");
        save(workspace);QTRY_COMPARE_WITH_TIMEOUT(status(workspace),kSaved,10000);
        QTRY_COMPARE_WITH_TIMEOUT(workspace->fingerprint(),readCode(window)["fingerprint"].toString(),5000);
        const QString typed="pens = 1\nmarkers = 2\n";editor(workspace)->setPlainText(typed);QCOMPARE(status(workspace),kDraft);
        const auto before=workspace->fingerprint();
        QSignalSpy ready(&window,&MainWindow::serviceReady);QSignalSpy opened(&window,&MainWindow::workbookReady);
        auto *process=window.findChild<QProcess *>("ownedService");process->kill();QTRY_COMPARE_WITH_TIMEOUT(process->state(),QProcess::NotRunning,5000);
        QTRY_VERIFY_WITH_TIMEOUT(editor(workspace)->isReadOnly(),5000);
        QCOMPARE(workspace->findChild<QLabel *>("workspaceApproval")->text(),QString("Not approved to run"));
        window.findChild<QAction *>("restartServiceAction")->trigger();
        QTRY_COMPARE_WITH_TIMEOUT(ready.count(),1,10000);QTRY_COMPARE_WITH_TIMEOUT(opened.count(),1,10000);
        QTRY_VERIFY_WITH_TIMEOUT(!workspace->fingerprint().isEmpty() && !editor(workspace)->isReadOnly(),10000);
        QCOMPARE(workspace->fingerprint(),before);
        QCOMPARE(editor(workspace)->toPlainText(),typed);QCOMPARE(status(workspace),kDraft);QVERIFY(workspace->hasDrafts());
        QCOMPARE(workspace->findChild<QLabel *>("workspaceApproval")->text(),QString("Not approved to run"));
        save(workspace);QTRY_COMPARE_WITH_TIMEOUT(status(workspace),kSaved,10000);
        QCOMPARE(readCode(window)["modules"].toArray()[0].toObject()["source"].toString(),typed);
    }
};

QTEST_MAIN(PythonWorkspaceTests)
#include "test_python_workspace.moc"
