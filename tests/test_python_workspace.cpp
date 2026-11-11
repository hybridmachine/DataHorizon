// SPDX-License-Identifier: GPL-3.0-only
#include "mainwindow.h"
#include "pythonworkspace.h"
#include "macrodeclarationpanel.h"
#include "codeeditor.h"
#include "codeapprovaldialog.h"
#include <QAction>
#include <QApplication>
#include <QCheckBox>
#include <QClipboard>
#include <QComboBox>
#include <QCompleter>
#include <QDialog>
#include <QDialogButtonBox>
#include <QFile>
#include <QInputDialog>
#include <QItemSelectionModel>
#include <QJsonArray>
#include <QJsonDocument>
#include <QLabel>
#include <QLineEdit>
#include <QListWidget>
#include <QMenuBar>
#include <QMessageBox>
#include <QMouseEvent>
#include <QPlainTextEdit>
#include <QProcess>
#include <QPushButton>
#include <QSettings>
#include <QSignalSpy>
#include <QStackedWidget>
#include <QTabBar>
#include <QTableView>
#include <QTemporaryDir>
#include <QTextDocument>
#include <QTest>
#include <QTimer>
#include <QUuid>
#include <functional>
#include <memory>

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

// Stores a managed operation through the window and waits for its refresh.
bool apply(MainWindow &window,const QJsonObject &operation) {
    QSignalSpy finished(&window,&MainWindow::operationFinished);QSignalSpy opened(&window,&MainWindow::workbookReady);
    window.submit(operation);
    return QTest::qWaitFor([&]{return finished.count()==1 && opened.count()>=1;},10000) && finished[0][0].toBool();
}

QJsonObject findName(MainWindow &window,const QString &name) {
    for(const auto &value:window.names())if(value.toObject()["name"].toString()==name)return value.toObject();
    return {};
}

bool saveName(MainWindow &window,const QString &name,const QString &reference) {
    const int before=int(window.names().size());
    return apply(window,{{"action","name_save"},{"name",name},{"reference",reference}})
        && QTest::qWaitFor([&]{return window.names().size()==before+1;},10000);
}

void selectRange(MainWindow &window,const QString &address) {
    auto *box=window.findChild<QLineEdit *>("address");box->setFocus();box->selectAll();
    QTest::keyClicks(box,address);QTest::keyClick(box,Qt::Key_Return);
}

QListWidget *namesList(QWidget *host){return host->findChild<QListWidget *>("namesList");}
QString namesMessage(QWidget *host){return host->findChild<QLabel *>("namesMessage")->text();}
QListWidgetItem *nameItem(QWidget *host,const QString &name) {
    auto *list=namesList(host);
    for(int row=0;row<list->count();++row)if(list->item(row)->data(Qt::UserRole+1).toString()==name)return list->item(row);
    return nullptr;
}
void chooseName(QWidget *host,const QString &name){namesList(host)->setCurrentItem(nameItem(host,name));}

QWidget *openNameManager(MainWindow &window) {
    window.findChild<QAction *>("nameManagerAction")->trigger();
    auto *dialog=window.findChild<QDialog *>("nameManagerDialog");
    if(dialog)(void)QTest::qWaitFor([&]{return dialog->isVisible() && dialog->findChild<QPushButton *>("newNameButton")->isEnabled();},5000);
    return dialog;
}

// Records whether a widget's tooltip ever contained a text, even if it is
// replaced again before the test looks.
class TooltipWatch : public QObject {
public:
    TooltipWatch(QWidget *widget,QString text):m_widget(widget),m_text(std::move(text)){widget->installEventFilter(this);}
    bool sawStale=false;
protected:
    bool eventFilter(QObject *object,QEvent *event) override {
        if(object==m_widget && event->type()==QEvent::ToolTipChange && m_widget->toolTip().contains(m_text))sawStale=true;
        return false;
    }
private:
    QWidget *m_widget;QString m_text;
};

QString mainStatus(MainWindow &window){return window.findChild<QLabel *>("storageStatus")->text();}
QAction *pickAction(PythonWorkspace *workspace){return workspace->findChild<QAction *>("pickFromSheetAction");}
QTableView *grid(MainWindow &window){return window.findChild<QTableView *>("grid");}

void clickCell(MainWindow &window,int row,int col) {
    auto *view=grid(window);QTest::mouseClick(view->viewport(),Qt::LeftButton,{},view->visualRect(window.model()->index(row,col)).center());
}

// Presses on one cell, moves to another and releases there, as a mouse drag does.
void dragCells(MainWindow &window,int r1,int c1,int r2,int c2) {
    auto *view=grid(window);auto *viewport=view->viewport();
    const auto from=view->visualRect(window.model()->index(r1,c1)).center(),to=view->visualRect(window.model()->index(r2,c2)).center();
    const auto global=viewport->mapToGlobal(to);
    QMouseEvent press(QEvent::MouseButtonPress,from,viewport->mapToGlobal(from),Qt::LeftButton,Qt::LeftButton,Qt::NoModifier);QApplication::sendEvent(viewport,&press);
    QMouseEvent move(QEvent::MouseMove,to,global,Qt::NoButton,Qt::LeftButton,Qt::NoModifier);QApplication::sendEvent(viewport,&move);
    QMouseEvent release(QEvent::MouseButtonRelease,to,global,Qt::LeftButton,Qt::NoButton,Qt::NoModifier);QApplication::sendEvent(viewport,&release);
}

// Adds a sheet through the window and shows it.
bool showNewSheet(MainWindow &window,const QString &name) {
    if(!apply(window,{{"action","sheet_add"},{"name",name}}))return false;
    auto *tabs=window.findChild<QTabBar *>("sheets");
    for(int index=0;index<tabs->count();++index)if(tabs->tabText(index)==name)tabs->setCurrentIndex(index);
    return tabs->tabText(tabs->currentIndex())==name;
}

void placeCursor(PythonWorkspace *workspace,int position) {
    auto cursor=editor(workspace)->textCursor();cursor.setPosition(position);editor(workspace)->setTextCursor(cursor);
}

// A workbook window showing sheet Supplies, with a workspace module whose
// editor holds text and has its cursor at position.
struct PickSetup {
    QTemporaryDir temp;StartupOptions options;
    std::unique_ptr<MainWindow> window;PythonWorkspace *workspace=nullptr;
    bool start(const QString &file,const QString &sheet,const QString &text,int position) {
        options.automaticWorkbook=false;window=std::make_unique<MainWindow>(options);
        if(!boot(*window,temp.filePath(file)) || !showNewSheet(*window,sheet))return false;
        workspace=openWorkspace(*window);if(!workspace || !createModule(workspace,"supplies"))return false;
        editor(workspace)->setPlainText(text);placeCursor(workspace,position);
        return pickAction(workspace)->isEnabled();
    }
};

QAction *reviewAction(PythonWorkspace *workspace){return workspace->findChild<QAction *>("reviewApproveAction");}
QString approval(PythonWorkspace *workspace){return workspace->findChild<QLabel *>("workspaceApproval")->text();}
QString workspaceMessage(PythonWorkspace *workspace){return workspace->findChild<QLabel *>("workspaceMessage")->text();}

// What the Review & approve dialog showed, captured before it closed.
struct Review {
    QString title,changes,fingerprint,bindings,notice,key,warning,copied;
    bool shown=false,hasApprove=false,approveEnabled=false,hasBindings=false,hasKey=false,hasCopy=false;
    bool allPlain=false;  // every label shows its text literally
};

// Opens Review & approve and records the dialog. With approve it presses the
// approve button (after beforeApprove runs while the dialog is still open);
// otherwise it cancels.
Review review(PythonWorkspace *workspace,bool approve,const std::function<void()> &beforeApprove={}) {
    Review seen;
    ModalDriver driver([&](QWidget *modal){
        auto *dialog=qobject_cast<QDialog *>(modal);if(!dialog || dialog->objectName()!="codeApprovalDialog")return false;
        seen.shown=true;seen.title=dialog->windowTitle();
        seen.allPlain=true;for(auto *label:dialog->findChildren<QLabel *>())if(label->textFormat()!=Qt::PlainText)seen.allPlain=false;
        auto text=[dialog](const char *name){auto *label=dialog->findChild<QLabel *>(name);return label?label->text():QString();};
        seen.changes=text("approvalChanges");seen.fingerprint=text("approvalFingerprint");seen.notice=text("approvalNotice");seen.warning=text("approvalWarning");
        seen.hasBindings=dialog->findChild<QLabel *>("approvalBindings")!=nullptr;seen.bindings=text("approvalBindings");
        if(auto *key=dialog->findChild<QLineEdit *>("activationKeyField")){seen.hasKey=true;seen.key=key->text();}
        if(auto *copy=dialog->findChild<QPushButton *>("copyActivationKeyButton")){seen.hasCopy=true;copy->click();seen.copied=QApplication::clipboard()->text();}
        auto *button=dialog->findChild<QPushButton *>("approveButton");
        seen.hasApprove=button!=nullptr;seen.approveEnabled=button && button->isEnabled();
        if(approve && seen.approveEnabled){if(beforeApprove)beforeApprove();button->click();}
        else dialog->reject();
        return true;});
    if(auto *action=reviewAction(workspace))action->trigger();
    return seen;
}

// Approves through the dialog and waits for the service to confirm it.
bool approveCode(PythonWorkspace *workspace) {
    const auto seen=review(workspace,true);
    return seen.approveEnabled && QTest::qWaitFor([&]{return approval(workspace)=="Approved for this session";},10000);
}

// Edits the stored module's source directly in the workbook file, as an
// external SQLite tool would; no DataHorizon behavior runs.
bool editModuleDirectly(const QString &path,const QString &source) {
    QProcess direct;
    direct.start(qEnvironmentVariable("DATAHORIZON_PYTHON","python3"),{"-c",
        "import json,sqlite3,sys\n"
        "db=sqlite3.connect(sys.argv[1],isolation_level=None)\n"
        "value=json.loads(db.execute(\"SELECT payload FROM dh_relational WHERE kind='module'\").fetchone()[0])\n"
        "value['source']=sys.argv[2]\n"
        "db.execute(\"UPDATE dh_relational SET payload=? WHERE kind='module'\",(json.dumps(value),))\n"
        "db.close()\n",path,source});
    return direct.waitForFinished(10000) && direct.exitStatus()==QProcess::NormalExit && direct.exitCode()==0;
}

// Captures the url and token an owned service announces, so a second window
// can attach to it.
void observeOwnedServiceStartup(MainWindow &owner,QJsonObject &service) {
    auto *process=owner.findChild<QProcess *>("ownedService");
    QObject::connect(process,&QProcess::readyReadStandardOutput,process,[process,&service,bytes=QByteArray{}]() mutable {
        if(!service.isEmpty())return;
        bytes+=process->peek(process->bytesAvailable());
        const auto newline=bytes.indexOf('\n');
        if(newline>=0)service=QJsonDocument::fromJson(bytes.left(newline)).object();
    });
}

const QString kApprovalWarning=QStringLiteral("Approved code runs with your own account's access to files and the network. It is not sandboxed, and undo cannot reverse effects outside the workbook.");

const QString kPicking=QStringLiteral("Picking a reference for Python · Esc to cancel");
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

    void editingBeforePostSaveReadAttachesToSavedModule() {
        QTemporaryDir temp;StartupOptions options;options.automaticWorkbook=false;MainWindow window(options);
        QVERIFY(boot(window,temp.filePath("gap.dhwb")));
        auto *workspace=openWorkspace(window);QVERIFY(createModule(workspace,"supplies"));editor(workspace)->setPlainText("pens = 1\n");
        // Types at the two moments before the post-commit catalog read replies:
        // inside the committed receipt, and when the refreshed workbook info has
        // been accepted (the main window is no longer busy, but the workspace
        // still holds the pre-save catalog).
        bool committed=false,typed=false,refused=true;QString textAfter;
        auto attempt=[&]{editor(workspace)->setFocus();QTest::keyClicks(editor(workspace),"x");
            refused=refused && editor(workspace)->isReadOnly() && !workspace->hasDrafts();textAfter=editor(workspace)->toPlainText();};
        connect(&window,&MainWindow::operationReceipt,workspace,[&](QJsonObject receipt){if(!committed && receipt["outcome"]=="committed"){committed=true;attempt();}});
        connect(&window,&MainWindow::workbookReady,workspace,[&]{if(committed && !typed){typed=true;attempt();}});
        save(workspace);QTRY_VERIFY_WITH_TIMEOUT(typed,10000);
        QVERIFY(refused);QCOMPARE(textAfter,QString("pens = 1\n"));
        const auto id=readCode(window)["modules"].toArray()[0].toObject()["id"].toString();
        QTRY_VERIFY_WITH_TIMEOUT(!editor(workspace)->isReadOnly(),10000);
        QCOMPARE(status(workspace),kSaved);QVERIFY(!workspace->hasDrafts());
        QTest::keyClicks(editor(workspace),"y");QCOMPARE(status(workspace),kDraft);
        save(workspace);QTRY_COMPARE_WITH_TIMEOUT(status(workspace),kSaved,10000);
        const auto modules=readCode(window)["modules"].toArray();QCOMPARE(modules.size(),1);
        QCOMPARE(modules[0].toObject()["id"].toString(),id);QCOMPARE(modules[0].toObject()["name"].toString(),QString("supplies"));
        QVERIFY(modules[0].toObject()["source"].toString().contains("y"));
    }

    void keepMineThenCancellingHistoryKeepsConflict() {
        QTemporaryDir temp;const auto path=temp.filePath("conflict-history.dhwb");StartupOptions options;options.automaticWorkbook=false;MainWindow window(options);
        QVERIFY(boot(window,path));
        auto *workspace=openWorkspace(window);QVERIFY(createModule(workspace,"supplies"));editor(workspace)->setPlainText("pens = 1\n");
        save(workspace);QTRY_COMPARE_WITH_TIMEOUT(status(workspace),kSaved,10000);
        const auto id=readCode(window)["modules"].toArray()[0].toObject()["id"].toString();
        editor(workspace)->setPlainText("pens = 2  # mine\n");
        QVERIFY(competingSave(options,path,"supplies","pens = 3  # theirs\n",id));
        QVERIFY(refreshWindow(window));
        // Undo history makes Save ask the history question after the conflict choice.
        QSignalSpy finished(&window,&MainWindow::operationFinished);QSignalSpy opened(&window,&MainWindow::workbookReady);
        window.submit({{"action","edit"},{"cells",QJsonArray{QJsonObject{{"row",1},{"col",1},{"source","7"}}}}});
        QTRY_VERIFY_WITH_TIMEOUT(finished.count()==1 && opened.count()>=1,10000);QVERIFY(finished[0][0].toBool());
        auto *banner=workspace->findChild<QLabel *>("workspaceConflict");QTRY_VERIFY_WITH_TIMEOUT(banner->isVisible(),5000);
        QTRY_VERIFY_WITH_TIMEOUT(workspace->findChild<QAction *>("saveModuleAction")->isEnabled(),5000);
        int histories=0;
        ModalDriver driver([&](QWidget *modal){
            if(clickButton(modal,"codeConflictDialog","conflictKeepMine"))return true;
            auto *box=qobject_cast<QMessageBox *>(modal);if(!box || box->objectName()!="codeSaveHistoryPrompt")return false;
            ++histories;box->button(QMessageBox::Cancel)->click();return true;});
        save(workspace);QCOMPARE(histories,1);QCOMPARE(driver.handled,2);
        QVERIFY(banner->isVisible());QVERIFY(workspace->hasDrafts());
        QCOMPARE(readCode(window)["modules"].toArray()[0].toObject()["source"].toString(),QString("pens = 3  # theirs\n"));
        // The conflict is still unresolved, so the next Save compares again.
        save(workspace);QCOMPARE(driver.handled,4);QCOMPARE(histories,2);QVERIFY(banner->isVisible());
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
        QVERIFY(approveCode(workspace));QVERIFY(readCode(window)["approved"].toBool());
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
        // The same stored code is no longer approved by the new service process.
        QVERIFY(!readCode(window)["approved"].toBool());
        save(workspace);QTRY_COMPARE_WITH_TIMEOUT(status(workspace),kSaved,10000);
        QCOMPARE(readCode(window)["modules"].toArray()[0].toObject()["source"].toString(),typed);
        QCOMPARE(approval(workspace),QString("Not approved to run"));
    }

    void approveMakesCodeRunnable() {
        QTemporaryDir temp;StartupOptions options;options.automaticWorkbook=false;MainWindow window(options);
        QVERIFY(boot(window,temp.filePath("approve.dhwb")));
        QVERIFY(window.ownsService());
        auto *workspace=openWorkspace(window);QVERIFY(createModule(workspace,"supplies"));editor(workspace)->setPlainText("def check(ctx, event):\n    pass\n");
        save(workspace);QTRY_COMPARE_WITH_TIMEOUT(status(workspace),kSaved,10000);
        QTRY_COMPARE_WITH_TIMEOUT(workspace->fingerprint(),readCode(window)["fingerprint"].toString(),5000);
        auto code=readCode(window);QSignalSpy finished(&window,&MainWindow::operationFinished);
        window.submit({{"action","binding_save"},{"event","cell_change"},{"scope",QJsonObject{{"kind","range"},{"reference","Sheet1!A1:B2"}}},{"module","supplies"},{"function","check"},{"required",false},{"fingerprint",code["fingerprint"]}});
        QTRY_COMPARE_WITH_TIMEOUT(finished.count(),1,10000);QVERIFY2(finished[0][0].toBool(),qPrintable(issues(window)));
        code=readCode(window);QTRY_COMPARE_WITH_TIMEOUT(workspace->fingerprint(),code["fingerprint"].toString(),10000);
        QCOMPARE(approval(workspace),QString("Not approved to run"));QVERIFY(!code["approved"].toBool());
        QTRY_VERIFY_WITH_TIMEOUT(reviewAction(workspace)->isEnabled(),5000);
        QCOMPARE(reviewAction(workspace)->iconText(),QString("Review & approve"));
        // The channel refuses keys that are malformed or name another workbook, without writing.
        QVERIFY(!window.grantCodeApproval("not-a-key"));
        QVERIFY(!window.grantCodeApproval(QUuid::createUuid().toString(QUuid::WithoutBraces)+":"+code["fingerprint"].toString()));
        QVERIFY(!window.grantCodeApproval(code["activation_key"].toString().toUpper()));
        QVERIFY(!window.grantCodeApproval(code["activation_key"].toString()+"\n"));
        QCOMPARE(window.findChild<QProcess *>("ownedService")->bytesToWrite(),qint64(0));
        QVERIFY(!readCode(window)["approved"].toBool());
        const auto seen=review(workspace,true);
        QVERIFY(seen.shown);QCOMPARE(seen.title,QString("Approve Python code for approve.dhwb?"));
        QCOMPARE(seen.fingerprint,code["fingerprint"].toString());QCOMPARE(seen.warning,kApprovalWarning);
        QVERIFY2(seen.changes.contains("supplies") && seen.changes.contains("Nothing has been approved in this session."),qPrintable(seen.changes));
        QVERIFY(seen.hasBindings);QVERIFY2(seen.bindings.contains("check") && seen.bindings.contains("'Sheet1'!A1:B2"),qPrintable(seen.bindings));
        QVERIFY(seen.hasApprove);QVERIFY(!seen.hasKey);QVERIFY(seen.allPlain);
        QTRY_COMPARE_WITH_TIMEOUT(approval(workspace),QString("Approved for this session"),10000);
        QVERIFY(readCode(window)["approved"].toBool());
        QVERIFY2(!issues(window).contains(code["activation_key"].toString()),"the key is never logged");
        // Reviewing approved code shows it without offering to approve again.
        const auto again=review(workspace,false);
        QVERIFY(again.shown);QCOMPARE(again.notice,QString("This code is already approved for this session."));QVERIFY(!again.approveEnabled);
    }

    void editAfterApprovalReturnsToNotApproved() {
        QTemporaryDir temp;StartupOptions options;options.automaticWorkbook=false;MainWindow window(options);
        QVERIFY(boot(window,temp.filePath("reapprove.dhwb")));
        auto *workspace=openWorkspace(window);QVERIFY(createModule(workspace,"supplies"));editor(workspace)->setPlainText("pens = 1\n");
        save(workspace);QTRY_COMPARE_WITH_TIMEOUT(status(workspace),kSaved,10000);
        QTRY_VERIFY_WITH_TIMEOUT(reviewAction(workspace)->isEnabled(),5000);
        QVERIFY(approveCode(workspace));
        QVERIFY(createModule(workspace,"orders"));editor(workspace)->setPlainText("orders = 1\n");
        save(workspace);QTRY_COMPARE_WITH_TIMEOUT(status(workspace),kSaved,10000);
        QTRY_COMPARE_WITH_TIMEOUT(approval(workspace),QString("Not approved to run"),10000);
        QVERIFY(!readCode(window)["approved"].toBool());
        QTRY_VERIFY_WITH_TIMEOUT(reviewAction(workspace)->isEnabled(),5000);
        const auto seen=review(workspace,false);
        QVERIFY(seen.shown);QVERIFY2(seen.changes.contains("orders"),qPrintable(seen.changes));
        QVERIFY2(!seen.changes.contains("supplies"),qPrintable(seen.changes));
        QVERIFY(!seen.changes.contains("Nothing has been approved"));QVERIFY(seen.approveEnabled);QVERIFY(!seen.hasBindings);
        QVERIFY(approveCode(workspace));QVERIFY(readCode(window)["approved"].toBool());
    }

    void changedCodeDuringReviewIsRefused() {
        QTemporaryDir temp;const auto path=temp.filePath("review.dhwb");StartupOptions options;options.automaticWorkbook=false;MainWindow window(options);
        QVERIFY(boot(window,path));
        auto *workspace=openWorkspace(window);QVERIFY(createModule(workspace,"supplies"));editor(workspace)->setPlainText("pens = 1\n");
        save(workspace);QTRY_COMPARE_WITH_TIMEOUT(status(workspace),kSaved,10000);
        QTRY_COMPARE_WITH_TIMEOUT(workspace->fingerprint(),readCode(window)["fingerprint"].toString(),5000);
        const auto shown=workspace->fingerprint();QTRY_VERIFY_WITH_TIMEOUT(reviewAction(workspace)->isEnabled(),5000);
        bool edited=false;
        const auto seen=review(workspace,true,[&]{edited=editModuleDirectly(path,"pens = 2\n");});
        QVERIFY(seen.shown);QVERIFY(edited);QCOMPARE(seen.fingerprint,shown);
        QTRY_COMPARE_WITH_TIMEOUT(workspaceMessage(workspace),QString("The stored code changed while it was being reviewed. Review it again."),10000);
        QTRY_VERIFY_WITH_TIMEOUT(!workspace->fingerprint().isEmpty() && workspace->fingerprint()!=shown,10000);
        QCOMPARE(approval(workspace),QString("Not approved to run"));QVERIFY(!readCode(window)["approved"].toBool());
        // Restoring the reviewed code proves its key was never sent.
        QVERIFY(editModuleDirectly(path,"pens = 1\n"));QVERIFY(refreshWindow(window));
        QTRY_COMPARE_WITH_TIMEOUT(workspace->fingerprint(),shown,10000);
        QVERIFY(!readCode(window)["approved"].toBool());QCOMPARE(approval(workspace),QString("Not approved to run"));
    }

    void attachedServiceShowsKeyAndCannotApprove() {
        QTemporaryDir temp;QJsonObject service;StartupOptions ownerOptions;ownerOptions.automaticWorkbook=false;
        MainWindow owner(ownerOptions);observeOwnedServiceStartup(owner,service);
        const auto path=temp.filePath("attached.dhwb");QVERIFY(boot(owner,path));QVERIFY(!service["token"].toString().isEmpty());
        auto *ownerWorkspace=openWorkspace(owner);QVERIFY(createModule(ownerWorkspace,"supplies"));editor(ownerWorkspace)->setPlainText("pens = 1\n");
        save(ownerWorkspace);QTRY_COMPARE_WITH_TIMEOUT(status(ownerWorkspace),kSaved,10000);
        StartupOptions options;options.automaticWorkbook=false;options.url=service["url"].toString();options.token=service["token"].toString();options.recoveryDirectory=temp.filePath("recovery");
        MainWindow window(options);QVERIFY(boot(window,path,false));
        QVERIFY(!window.ownsService());
        auto *workspace=openWorkspace(window);QTRY_VERIFY_WITH_TIMEOUT(reviewAction(workspace)->isEnabled(),5000);
        const auto code=readCode(window);const auto key=code["activation_key"].toString();QVERIFY(!key.isEmpty());
        QVERIFY(!window.grantCodeApproval(key));
        const auto seen=review(workspace,true);
        QVERIFY(seen.shown);QVERIFY(!seen.hasApprove);QVERIFY(seen.hasKey);QCOMPARE(seen.key,key);
        QCOMPARE(seen.notice,QString("This service was started separately. Its operator approves code by starting it with --approve-code %1.").arg(key));
        QVERIFY(seen.hasCopy);QCOMPARE(seen.copied,key);QCOMPARE(seen.fingerprint,code["fingerprint"].toString());
        QTest::qWait(300);
        QVERIFY(!readCode(window)["approved"].toBool());QCOMPARE(approval(workspace),QString("Not approved to run"));
    }

    void workbookTextIsShownLiterally() {
        QTemporaryDir temp;StartupOptions options;options.automaticWorkbook=false;MainWindow window(options);
        QVERIFY(boot(window,temp.filePath("markup.dhwb")));
        // Sheet names cannot contain "/", so a lone opening tag stands in for markup.
        QVERIFY(apply(window,{{"action","sheet_add"},{"name","<b>x"}}));
        auto *workspace=openWorkspace(window);QVERIFY(createModule(workspace,"supplies"));editor(workspace)->setPlainText("def check(ctx, event):\n    pass\n");
        save(workspace);QTRY_COMPARE_WITH_TIMEOUT(status(workspace),kSaved,10000);
        QTRY_COMPARE_WITH_TIMEOUT(workspace->fingerprint(),readCode(window)["fingerprint"].toString(),5000);
        QVERIFY2(apply(window,{{"action","binding_save"},{"event","cell_change"},{"scope",QJsonObject{{"kind","range"},{"reference","'<b>x'!A1"}}},{"module","supplies"},{"function","check"},{"required",false},{"fingerprint",readCode(window)["fingerprint"]}}),qPrintable(issues(window)));
        QTRY_COMPARE_WITH_TIMEOUT(workspace->fingerprint(),readCode(window)["fingerprint"].toString(),10000);
        QTRY_VERIFY_WITH_TIMEOUT(reviewAction(workspace)->isEnabled(),5000);
        const auto seen=review(workspace,false);
        QVERIFY(seen.shown);QVERIFY(seen.hasBindings);QVERIFY(seen.allPlain);QVERIFY2(seen.bindings.contains("'<b>x'!A1"),qPrintable(seen.bindings));
        // Workspace messages echo text literally.
        auto *message=workspace->findChild<QLabel *>("workspaceMessage");QCOMPARE(message->textFormat(),Qt::PlainText);
        workspace->findChild<QAction *>("findAction")->trigger();auto *field=workspace->findChild<QLineEdit *>("findField");
        field->setText("<b>x</b>");QTest::keyClick(field,Qt::Key_Return);
        QCOMPARE(workspaceMessage(workspace),QString("No match for <b>x</b>."));
        for(const auto *name:{"workspaceStatus","workspaceApproval","workspaceConflict","moduleEmptyHint"})QCOMPARE(workspace->findChild<QLabel *>(name)->textFormat(),Qt::PlainText);
        // The Name Manager shows a reference on that sheet literally.
        QVERIFY(saveName(window,"Odd","'<b>x'!A1"));
        auto *manager=openNameManager(window);QVERIFY(manager);chooseName(manager,"Odd");
        const auto reference=manager->findChild<QLineEdit *>("nameManagerReference")->text();QVERIFY2(reference.contains("<b>x"),qPrintable(reference));
        QCOMPARE(manager->findChild<QLabel *>("namesMessage")->textFormat(),Qt::PlainText);
        QTextDocument tip;tip.setHtml(nameItem(manager,"Odd")->toolTip());QCOMPARE(tip.toPlainText(),reference);
    }

    void changesSinceApprovalFollowTheFingerprint() {
        const QJsonObject module{{"id","m1"},{"name","supplies"},{"source","pens = 1\n"},{"macros",QJsonArray{}}};
        const QJsonObject binding{{"id","b1"},{"event","cell_change"},{"module","supplies"},{"module_id","m1"},{"function","check"},{"required",false},
            {"scope",QJsonObject{{"kind","range"},{"range_id","r1"},{"sheet_id","s1"},{"row1",1},{"col1",1},{"row2",1},{"col2",1}}},{"reference","Sheet1!A1"},{"broken",false}};
        const QJsonObject approved{{"fingerprint",QString(64,'a')},{"modules",QJsonArray{module}},{"bindings",QJsonArray{binding}}};
        // Display-only binding fields and coordinates moved, fingerprint unchanged.
        auto moved=binding;moved["reference"]="Sheet1!B2";moved["broken"]=true;
        auto scope=moved["scope"].toObject();scope["row1"]=2;scope["col1"]=2;scope["row2"]=2;scope["col2"]=2;moved["scope"]=scope;
        auto same=approved;same["bindings"]=QJsonArray{moved};
        QCOMPARE(CodeApprovalDialog::describeChanges(same,approved),QString("No changes since the last approval."));
        // Nothing listed differs, but the fingerprint does.
        auto other=same;other["fingerprint"]=QString(64,'b');
        QCOMPARE(CodeApprovalDialog::describeChanges(other,approved),QString("The code fingerprint changed since the last approval."));
        // A covered binding field changed.
        auto required=binding;required["required"]=true;auto changed=approved;changed["bindings"]=QJsonArray{required};changed["fingerprint"]=QString(64,'c');
        QCOMPARE(CodeApprovalDialog::describeChanges(changed,approved),QString("Event bindings changed"));
    }

    void approveDisabledWithDrafts() {
        QTemporaryDir temp;StartupOptions options;options.automaticWorkbook=false;MainWindow window(options);
        QVERIFY(boot(window,temp.filePath("drafts.dhwb")));
        auto *workspace=openWorkspace(window);
        QVERIFY(!reviewAction(workspace)->isEnabled());
        QVERIFY(createModule(workspace,"supplies"));editor(workspace)->setPlainText("pens = 1\n");
        QVERIFY(!reviewAction(workspace)->isEnabled());
        save(workspace);QTRY_COMPARE_WITH_TIMEOUT(status(workspace),kSaved,10000);
        QTRY_VERIFY_WITH_TIMEOUT(reviewAction(workspace)->isEnabled(),5000);
        editor(workspace)->setPlainText("pens = 2\n");QCOMPARE(status(workspace),kDraft);
        QVERIFY(!reviewAction(workspace)->isEnabled());
        QCOMPARE(reviewAction(workspace)->toolTip(),QString("Save or discard your drafts before approving."));
        const auto seen=review(workspace,true);QVERIFY(!seen.shown);
        QVERIFY(!readCode(window)["approved"].toBool());
        editor(workspace)->setPlainText("pens = 1\n");QVERIFY(!workspace->hasDrafts());
        QVERIFY(reviewAction(workspace)->isEnabled());
    }

    void addressBoxShowsAndJumpsToName() {
        QTemporaryDir temp;StartupOptions options;options.automaticWorkbook=false;MainWindow window(options);
        QVERIFY(boot(window,temp.filePath("address.dhwb")));
        QVERIFY(saveName(window,"Total","Sheet1!D6"));QVERIFY(saveName(window,"Quantities","Sheet1!B2:B4"));
        auto *grid=window.findChild<QTableView *>("grid");auto *address=window.findChild<QLineEdit *>("address");
        grid->setFocus();grid->setCurrentIndex(window.model()->index(5,3));
        QTRY_COMPARE_WITH_TIMEOUT(address->text(),QString("Total"),5000);
        grid->setCurrentIndex(window.model()->index(5,4));QCOMPARE(address->text(),QString("E6"));
        selectRange(window,"quantities");
        const auto ranges=grid->selectionModel()->selection();QCOMPARE(ranges.size(),1);
        QCOMPARE(ranges[0].top(),1);QCOMPARE(ranges[0].bottom(),3);QCOMPARE(ranges[0].left(),1);QCOMPARE(ranges[0].right(),1);
        QCOMPARE(grid->currentIndex(),window.model()->index(1,1));QCOMPARE(address->text(),QString("Quantities"));
        // A typed address is always an address; a name on another sheet switches to it.
        selectRange(window,"B2");QCOMPARE(address->text(),QString("B2"));
        const auto first=window.model()->sheet();
        QVERIFY(apply(window,{{"action","sheet_add"},{"name","Other"}}));QVERIFY(saveName(window,"Far","Other!C3"));
        QCOMPARE(window.model()->sheet(),first);
        selectRange(window,"FAR");
        QVERIFY(window.model()->sheet()!=first);QCOMPARE(grid->currentIndex(),window.model()->index(2,2));QCOMPARE(address->text(),QString("Far"));
        selectRange(window,"Nowhere");QVERIFY(issues(window).contains("Nowhere"));
        // Another workbook's view never starts with these names, even before its own read lands.
        int shownWithNames=-1;QSignalSpy opened(&window,&MainWindow::workbookReady);
        connect(&window,&MainWindow::workbookReady,this,[&]{if(shownWithNames<0)shownWithNames=int(window.names().size());});
        window.openBook(temp.filePath("other.dhwb"),true);QTRY_VERIFY_WITH_TIMEOUT(opened.count()>=1,10000);
        QCOMPARE(shownWithNames,0);
        grid->setFocus();grid->setCurrentIndex(window.model()->index(5,3));QCOMPARE(address->text(),QString("D6"));
    }

    void newNameFromSelectionCreatesName() {
        QTemporaryDir temp;StartupOptions options;options.automaticWorkbook=false;MainWindow window(options);
        QVERIFY(boot(window,temp.filePath("create.dhwb")));
        QVERIFY(apply(window,{{"action","edit"},{"cells",QJsonArray{QJsonObject{{"row",1},{"col",1},{"source","7"}}}}}));
        auto *dialog=openNameManager(window);QVERIFY(dialog);
        auto *grid=window.findChild<QTableView *>("grid");
        // Several areas cannot be one name.
        grid->selectionModel()->select(QItemSelection(window.model()->index(0,0),window.model()->index(0,0)),QItemSelectionModel::ClearAndSelect);
        grid->selectionModel()->select(QItemSelection(window.model()->index(4,4),window.model()->index(4,4)),QItemSelectionModel::Select);
        dialog->findChild<QPushButton *>("newNameButton")->click();
        QVERIFY(namesMessage(dialog).contains("one rectangular range"));
        selectRange(window,"B2:B4");
        QString prompt,label;int prompts=0;bool dontAsk=true;
        ModalDriver driver([&](QWidget *modal){
            if(auto *input=qobject_cast<QInputDialog *>(modal);input && input->objectName()=="newNameInput"){label=input->labelText();input->setTextValue(prompts?"Spare":"Quantities");input->accept();return true;}
            auto *box=qobject_cast<QMessageBox *>(modal);if(!box || box->objectName()!="nameHistoryPrompt")return false;
            ++prompts;prompt=box->text();box->checkBox()->setChecked(dontAsk);box->button(QMessageBox::Yes)->click();return true;});
        QCOMPARE(dialog->findChild<QPushButton *>("newNameButton")->text(),QString("New name from selection…"));
        dialog->findChild<QPushButton *>("newNameButton")->click();
        QTRY_VERIFY_WITH_TIMEOUT(!findName(window,"Quantities").isEmpty(),10000);
        QCOMPARE(prompts,1);QCOMPARE(prompt,QString("Changing a name clears Undo history for this workbook. Continue?"));QVERIFY(label.contains("B2:B4"));
        const auto name=findName(window,"Quantities");QCOMPARE(name["reference"].toString(),QString("'Sheet1'!B2:B4"));
        QTRY_VERIFY_WITH_TIMEOUT(nameItem(dialog,"Quantities"),5000);QVERIFY(nameItem(dialog,"Quantities")->text().contains("'Sheet1'!B2:B4"));
        // The refresh after the change leaves the current cell selected; selecting the range again shows the name.
        grid->selectionModel()->select(QItemSelection(window.model()->index(1,1),window.model()->index(3,1)),QItemSelectionModel::ClearAndSelect);
        QCOMPARE(window.findChild<QLineEdit *>("address")->text(),QString("Quantities"));
        // The workspace's Names panel creates from the same selection and shares
        // the session's "Don't ask again" choice.
        QVERIFY(apply(window,{{"action","edit"},{"cells",QJsonArray{QJsonObject{{"row",1},{"col",1},{"source","8"}}}}}));
        auto *workspace=openWorkspace(window);QVERIFY(workspace);
        workspace->findChild<QAction *>("showNamesAction")->trigger();
        auto *panel=workspace->findChild<QWidget *>("namesPanel");QVERIFY(panel->isVisible());
        selectRange(window,"C1");
        QTRY_VERIFY_WITH_TIMEOUT(panel->findChild<QPushButton *>("newNameButton")->isEnabled(),5000);
        panel->findChild<QPushButton *>("newNameButton")->click();
        QTRY_VERIFY_WITH_TIMEOUT(!findName(window,"Spare").isEmpty(),10000);QCOMPARE(prompts,1);
        QCOMPARE(findName(window,"Spare")["reference"].toString(),QString("'Sheet1'!C1"));
        QTRY_VERIFY_WITH_TIMEOUT(nameItem(panel,"Spare"),5000);
    }

    void doubleClickInsertsQuotedNameAtCursor() {
        QTemporaryDir temp;StartupOptions options;options.automaticWorkbook=false;MainWindow window(options);
        QVERIFY(boot(window,temp.filePath("insert.dhwb")));
        QVERIFY(saveName(window,"Total","Sheet1!D6"));
        auto *workspace=openWorkspace(window);QVERIFY(workspace);
        workspace->findChild<QAction *>("showNamesAction")->trigger();
        auto *side=workspace->findChild<QStackedWidget *>("sidePanel");QCOMPARE(side->currentWidget()->objectName(),QString("namesPage"));
        QTRY_VERIFY_WITH_TIMEOUT(nameItem(workspace,"Total"),5000);
        auto *list=namesList(workspace);auto doubleClick=[&]{const auto rect=list->visualItemRect(nameItem(workspace,"Total"));QTest::mouseClick(list->viewport(),Qt::LeftButton,{},rect.center());QTest::mouseDClick(list->viewport(),Qt::LeftButton,{},rect.center());};
        // Without an editable module, the insert is refused and nothing becomes a draft.
        doubleClick();QVERIFY(workspace->findChild<QLabel *>("workspaceMessage")->text().contains("editable"));QVERIFY(!workspace->hasDrafts());
        QVERIFY(createModule(workspace,"supplies"));editor(workspace)->setPlainText("total = ctx.value()\n");
        auto cursor=editor(workspace)->textCursor();cursor.setPosition(QString("total = ctx.value(").size());editor(workspace)->setTextCursor(cursor);
        doubleClick();
        QCOMPARE(editor(workspace)->toPlainText(),QString("total = ctx.value(\"Total\")\n"));QCOMPARE(status(workspace),kDraft);
        workspace->findChild<QAction *>("showMacroAction")->trigger();QCOMPARE(side->currentWidget()->objectName(),QString("macroPage"));
    }

    void invalidNameShowsServiceMessage() {
        QTemporaryDir temp;StartupOptions options;options.automaticWorkbook=false;MainWindow window(options);
        QVERIFY(boot(window,temp.filePath("invalidname.dhwb")));
        auto *dialog=openNameManager(window);QVERIFY(dialog);
        selectRange(window,"B2");
        ModalDriver driver([](QWidget *modal){
            auto *input=qobject_cast<QInputDialog *>(modal);if(!input || input->objectName()!="newNameInput")return false;
            input->setTextValue("A1");input->accept();return true;});
        dialog->findChild<QPushButton *>("newNameButton")->click();QCOMPARE(driver.handled,1);
        QTRY_VERIFY_WITH_TIMEOUT(namesMessage(dialog).contains("NAME_INVALID"),10000);
        QVERIFY(namesMessage(dialog).contains("not cell addresses"));QVERIFY(issues(window).contains("not cell addresses"));
        QVERIFY(window.names().isEmpty());QCOMPARE(namesList(dialog)->count(),0);
    }

    void brokenNameIsMarkedAndDoesNotJump() {
        QTemporaryDir temp;StartupOptions options;options.automaticWorkbook=false;MainWindow window(options);
        QVERIFY(boot(window,temp.filePath("broken.dhwb")));
        QVERIFY(saveName(window,"Spare","Sheet1!A2"));QVERIFY(saveName(window,"Kept","Sheet1!B5"));
        QVERIFY(apply(window,{{"action","rows_delete"},{"start",2},{"count",1}}));
        QVERIFY(issues(window).contains("Names that no longer refer to any cells: Spare"));
        QTRY_VERIFY_WITH_TIMEOUT(findName(window,"Spare")["broken"].toBool(),10000);
        QCOMPARE(findName(window,"Kept")["reference"].toString(),QString("'Sheet1'!B4"));
        auto *dialog=openNameManager(window);QVERIFY(dialog);
        QTRY_VERIFY_WITH_TIMEOUT(nameItem(dialog,"Spare"),5000);QVERIFY(nameItem(dialog,"Spare")->text().contains("Broken"));
        auto *workspace=openWorkspace(window);QVERIFY(workspace);
        QTRY_VERIFY_WITH_TIMEOUT(nameItem(workspace,"Spare"),5000);QVERIFY(nameItem(workspace,"Spare")->text().contains("Broken"));
        auto *grid=window.findChild<QTableView *>("grid");
        selectRange(window,"E7");const auto before=grid->selectionModel()->selection();
        selectRange(window,"spare");
        QVERIFY(issues(window).contains("Name Spare no longer refers to any cells"));
        QCOMPARE(grid->selectionModel()->selection(),before);QCOMPARE(grid->currentIndex(),window.model()->index(6,4));
        // A broken name can be retargeted to the selection, then deleted.
        selectRange(window,"C3");chooseName(dialog,"Spare");
        dialog->findChild<QPushButton *>("retargetNameButton")->click();
        QTRY_COMPARE_WITH_TIMEOUT(findName(window,"Spare")["reference"].toString(),QString("'Sheet1'!C3"),10000);
        QTRY_VERIFY_WITH_TIMEOUT(nameItem(dialog,"Spare") && !nameItem(dialog,"Spare")->text().contains("Broken"),5000);
        chooseName(dialog,"Spare");
        ModalDriver driver([](QWidget *modal){auto *box=qobject_cast<QMessageBox *>(modal);if(!box || box->objectName()!="deleteNameConfirm")return false;box->button(QMessageBox::Yes)->click();return true;});
        dialog->findChild<QPushButton *>("deleteNameButton")->click();QCOMPARE(driver.handled,1);
        QTRY_VERIFY_WITH_TIMEOUT(findName(window,"Spare").isEmpty(),10000);QCOMPARE(window.names().size(),1);
    }

    void staleNamesReadRefreshesAndReadsAgain() {
        QTemporaryDir temp;const auto path=temp.filePath("stale-names.dhwb");StartupOptions options;options.automaticWorkbook=false;MainWindow window(options);
        QVERIFY(boot(window,path));QVERIFY(saveName(window,"Total","Sheet1!D6"));
        auto *dialog=openNameManager(window);QVERIFY(dialog);
        // With the relational view shown the grid reads no cells, so the names
        // read is the only request that can notice the competing change.
        window.findChild<QAction *>("relationalWorkbenchAction")->trigger();
        TooltipWatch summaryWatch(window.findChild<QLabel *>("selectionSummary"),"Stored data changed");
        // Right after the next commit, while the post-commit refresh is already
        // in flight, another process stores a name in the same file. The
        // refreshed state the window accepts is then stale when it reads names.
        bool competed=false;int exitCode=-1;
        connect(&window,&MainWindow::operationReceipt,this,[&](QJsonObject receipt){
            if(competed || receipt["outcome"]!="committed")return;competed=true;
            QProcess cli;auto environment=QProcessEnvironment::systemEnvironment();environment.insert("PYTHONPATH",QString(DATAHORIZON_FIXTURES)+"/../../service");cli.setProcessEnvironment(environment);
            cli.start("python3",{"-m","datahorizon.cli",path,"names","save"});cli.write(R"({"name":"Remote","reference":"Sheet1!C1"})");cli.closeWriteChannel();
            if(cli.waitForFinished(30000))exitCode=cli.exitCode();});
        window.submit({{"action","name_save"},{"name","Local"},{"reference","Sheet1!B1"}});
        QTRY_VERIFY_WITH_TIMEOUT(competed,10000);QCOMPARE(exitCode,0);
        // No manual refresh: the stale names reply refreshes the workbook and reads again.
        QTRY_VERIFY_WITH_TIMEOUT(!findName(window,"Remote").isEmpty(),15000);
        QVERIFY(!findName(window,"Local").isEmpty());QVERIFY(!findName(window,"Total").isEmpty());
        QTRY_VERIFY_WITH_TIMEOUT(dialog->findChild<QPushButton *>("newNameButton")->isEnabled(),5000);
        QTRY_VERIFY_WITH_TIMEOUT(nameItem(dialog,"Remote"),5000);
        // The names reply itself started the refresh: the selection summary
        // (debounced 250 ms) never had to discover the stale state.
        QTest::qWait(400);QVERIFY(!summaryWatch.sawStale);
    }

    void namesPendingReadAreNotUsed() {
        QTemporaryDir temp;StartupOptions options;options.automaticWorkbook=false;MainWindow window(options);
        QVERIFY(boot(window,temp.filePath("pending.dhwb")));QVERIFY(saveName(window,"Total","Sheet1!D6"));
        auto *grid=window.findChild<QTableView *>("grid");auto *address=window.findChild<QLineEdit *>("address");
        // While the names read for a new state is pending, the address box shows
        // plain addresses and name jumps wait.
        bool checked=false;QString shown;
        connect(&window,&MainWindow::workbookReady,this,[&]{
            if(checked)return;checked=true;
            grid->setFocus();grid->setCurrentIndex(window.model()->index(5,3));shown=address->text();
            address->setText("total");address->setModified(true);emit address->returnPressed();});
        QVERIFY(refreshWindow(window));
        QVERIFY(checked);QCOMPARE(shown,QString("D6"));QVERIFY(issues(window).contains("Names are being read. Try again in a moment."));
        QCOMPARE(grid->currentIndex(),window.model()->index(5,3));
        grid->setFocus();grid->setCurrentIndex(window.model()->index(5,4));grid->setCurrentIndex(window.model()->index(5,3));
        QTRY_COMPARE_WITH_TIMEOUT(address->text(),QString("Total"),5000);
    }

    void completionOnlyInsideStringsAndAfterCtx() {
        QTemporaryDir temp;StartupOptions options;options.automaticWorkbook=false;MainWindow window(options);
        QVERIFY(boot(window,temp.filePath("contexts.dhwb")));
        QVERIFY(saveName(window,"Total","Sheet1!D6"));
        auto *workspace=openWorkspace(window);QVERIFY(createModule(workspace,"supplies"));
        auto *code=qobject_cast<CodeEditor *>(editor(workspace));auto *completer=code->findChild<QCompleter *>("codeCompleter");
        workspace->activateWindow();code->setFocus();
        QTRY_VERIFY_WITH_TIMEOUT(code->completionNames().contains("Total"),5000);
        // Plain code: no popup, Return is a newline and Tab indents.
        QTest::keyClicks(code,"to");QVERIFY(!completer->popup()->isVisible());
        QTest::keyClick(code,Qt::Key_Return);QTest::keyClick(code,Qt::Key_Tab);QCOMPARE(code->toPlainText(),QString("to\n    "));
        // Names inside a string literal; nothing after the string closes or in a comment.
        QTest::keyClicks(code,"ctx.value(\"To");QTRY_VERIFY_WITH_TIMEOUT(completer->popup()->isVisible(),2000);QCOMPARE(completer->currentCompletion(),QString("Total"));
        QTest::keyClick(code,Qt::Key_Escape);QTest::keyClicks(code,"\") # To");QVERIFY(!completer->popup()->isVisible());
        QTest::keyClick(code,Qt::Key_Return);
        // Context methods directly after ctx. only.
        QTest::keyClicks(code,"ctx.va");QTRY_VERIFY_WITH_TIMEOUT(completer->popup()->isVisible(),2000);
        QStringList offered;for(int row=0;completer->setCurrentRow(row);++row)offered.append(completer->currentCompletion());
        QCOMPARE(offered,(QStringList{"value","values"}));
        QTest::keyClick(code,Qt::Key_Escape);QTest::keyClicks(code," + other.va");QVERIFY(!completer->popup()->isVisible());
        QCOMPARE(code->toPlainText(),QString("to\n    ctx.value(\"To\") # To\nctx.va + other.va"));
    }

    void completionOffersNames() {
        QTemporaryDir temp;StartupOptions options;options.automaticWorkbook=false;MainWindow window(options);
        QVERIFY(boot(window,temp.filePath("complete.dhwb")));
        QVERIFY(saveName(window,"Total","Sheet1!D6"));
        auto *workspace=openWorkspace(window);QVERIFY(createModule(workspace,"supplies"));
        auto *code=qobject_cast<CodeEditor *>(editor(workspace));QVERIFY(code);
        auto *completer=code->findChild<QCompleter *>("codeCompleter");QVERIFY(completer);
        workspace->activateWindow();code->setFocus();
        QTRY_VERIFY_WITH_TIMEOUT(code->completionNames().contains("Total"),5000);
        QTest::keyClicks(code,"x = \"T");QVERIFY(!completer->popup()->isVisible());
        QTest::keyClicks(code,"o");QTRY_VERIFY_WITH_TIMEOUT(completer->popup()->isVisible(),2000);QCOMPARE(completer->currentCompletion(),QString("Total"));
        QTest::keyClick(code,Qt::Key_Tab);QVERIFY(!completer->popup()->isVisible());
        QCOMPARE(code->toPlainText(),QString("x = \"Total"));
        // After ctx. it offers the context methods; Escape dismisses without changing text.
        QTest::keyClicks(code,"\"");QTest::keyClick(code,Qt::Key_Return);QTest::keyClicks(code,"ctx.ta");QTRY_VERIFY_WITH_TIMEOUT(completer->popup()->isVisible(),2000);
        QStringList offered;for(int row=0;completer->setCurrentRow(row);++row)offered.append(completer->currentCompletion());
        QCOMPARE(offered,(QStringList{"table_delete","table_get","table_insert","table_schema","table_set"}));
        QTest::keyClick(code,Qt::Key_Escape);QVERIFY(!completer->popup()->isVisible());
        QCOMPARE(code->toPlainText(),QString("x = \"Total\"\nctx.ta"));
        QTest::keyClicks(code,"ble_sc");QTRY_VERIFY_WITH_TIMEOUT(completer->popup()->isVisible(),2000);
        QTest::keyClick(code,Qt::Key_Return);QCOMPARE(code->toPlainText(),QString("x = \"Total\"\nctx.table_schema"));
        // With no popup, Tab still indents by four spaces.
        QTest::keyClick(code,Qt::Key_Return);QTest::keyClick(code,Qt::Key_Tab);
        QCOMPARE(code->toPlainText(),QString("x = \"Total\"\nctx.table_schema\n    "));
        QCOMPARE(status(workspace),kDraft);
    }

    void clickInsertsQualifiedCellAtCursor() {
        PickSetup setup;const QString text="q = ctx.value()\n";QVERIFY(setup.start("click.dhwb","Supplies",text,text.indexOf(')')));
        auto &window=*setup.window;auto *workspace=setup.workspace;auto *pick=pickAction(workspace);
        QCOMPARE(pick->text(),QString("Pick from sheet"));QVERIFY(pick->isCheckable());
        QSignalSpy finished(&window,&MainWindow::externalPickFinished);
        pick->trigger();QVERIFY(pick->isChecked());QCOMPARE(mainStatus(window),kPicking);
        clickCell(window,2,1);
        QCOMPARE(editor(workspace)->toPlainText(),QString("q = ctx.value(\"'Supplies'!B3\")\n"));QCOMPARE(status(workspace),kDraft);
        // Still armed: the next click inserts again, separated only by what the user typed.
        QVERIFY(pick->isChecked());QCOMPARE(mainStatus(window),kPicking);QCOMPARE(finished.count(),0);
        QTest::keyClicks(editor(workspace),", ");clickCell(window,0,0);
        QCOMPARE(editor(workspace)->toPlainText(),QString("q = ctx.value(\"'Supplies'!B3\", \"'Supplies'!A1\")\n"));
        // A second press of the button ends it.
        pick->trigger();QVERIFY(!pick->isChecked());QCOMPARE(finished.count(),1);QCOMPARE(finished[0][0].toBool(),true);
        QVERIFY(mainStatus(window)!=kPicking);
        clickCell(window,3,3);QCOMPARE(editor(workspace)->toPlainText(),QString("q = ctx.value(\"'Supplies'!B3\", \"'Supplies'!A1\")\n"));
    }

    void dragInsertsRange() {
        PickSetup setup;const QString text="rows = ctx.values()\n";QVERIFY(setup.start("drag.dhwb","Supplies",text,text.indexOf(')')));
        auto &window=*setup.window;auto *workspace=setup.workspace;auto *pick=pickAction(workspace);
        pick->trigger();QVERIFY(pick->isChecked());
        dragCells(window,1,1,3,1);
        QCOMPARE(editor(workspace)->toPlainText(),QString("rows = ctx.values(\"'Supplies'!B2:B4\")\n"));
        // Dragged upward and left, the range is normalised; a drag ending on its start cell is one cell.
        QTest::keyClicks(editor(workspace),", ");dragCells(window,4,3,2,2);
        QTest::keyClicks(editor(workspace),", ");dragCells(window,5,0,5,0);
        QCOMPARE(editor(workspace)->toPlainText(),QString("rows = ctx.values(\"'Supplies'!B2:B4\", \"'Supplies'!C3:D5\", \"'Supplies'!A6\")\n"));
        // Hidden cells and over-limit ranges are refused as in formula picking, and picking stays armed.
        grid(window)->setCurrentIndex(window.model()->index(7,0));window.findChild<QAction *>("hideRowsAction")->trigger();QVERIFY(grid(window)->isRowHidden(7));
        const auto before=editor(workspace)->toPlainText();
        dragCells(window,6,0,8,0);
        QCOMPARE(editor(workspace)->toPlainText(),before);QVERIFY(issues(window).contains("Show hidden cells before picking"));QVERIFY(pick->isChecked());
        workspace->activateWindow();QTRY_VERIFY_WITH_TIMEOUT(workspace->isActiveWindow(),5000);QTest::keyClick(editor(workspace),Qt::Key_Escape);QVERIFY(!pick->isChecked());
    }

    void exactNameMatchInsertsName() {
        PickSetup setup;const QString text="x = \n";QVERIFY(setup.start("names.dhwb","Supplies",text,4));
        auto &window=*setup.window;auto *workspace=setup.workspace;auto *pick=pickAction(workspace);
        QVERIFY(saveName(window,"Total","Supplies!D6"));QVERIFY(saveName(window,"Quantities","Supplies!B2:B4"));
        QVERIFY(saveName(window,"OtherSheet","Sheet1!D5"));
        placeCursor(workspace,4);pick->trigger();QVERIFY(pick->isChecked());
        clickCell(window,5,3);QTest::keyClicks(editor(workspace)," + ");
        dragCells(window,1,1,3,1);QTest::keyClicks(editor(workspace)," + ");
        // Part of a named range, or a name's cell on another sheet, is an address.
        dragCells(window,1,1,2,1);QTest::keyClicks(editor(workspace)," + ");
        clickCell(window,4,3);
        QCOMPARE(editor(workspace)->toPlainText(),QString("x = \"Total\" + \"Quantities\" + \"'Supplies'!B2:B3\" + \"'Supplies'!D5\"\n"));
    }

    void quotedSheetNameRoundTrips() {
        PickSetup setup;const QString text="def run(ctx):\n    return ctx.value()\n";
        QVERIFY(setup.start("quoted.dhwb","Bob's Sheet",text,text.lastIndexOf(')')));
        auto &window=*setup.window;auto *workspace=setup.workspace;
        QVERIFY(apply(window,{{"action","edit"},{"cells",QJsonArray{QJsonObject{{"row",3},{"col",2},{"source","42"}}}}}));
        placeCursor(workspace,text.lastIndexOf(')'));
        pickAction(workspace)->trigger();clickCell(window,2,1);pickAction(workspace)->trigger();
        const auto source=editor(workspace)->toPlainText();
        QCOMPARE(source,QString("def run(ctx):\n    return ctx.value(\"'Bob''s Sheet'!B3\")\n"));
        // Stored and run by an operator-approved service, the macro reads that cell.
        QJsonObject declaration{{"name","run"},{"parameters",QJsonArray{}},{"result",QJsonObject{{"type","number"}}}};
        QVERIFY(apply(window,{{"action","module_save"},{"name","quoted"},{"source",source},{"macros",QJsonArray{declaration}},{"fingerprint",readCode(window)["fingerprint"]}}));
        const auto code=readCode(window);QVERIFY(!code["activation_key"].toString().isEmpty());
        QProcess cli;auto environment=QProcessEnvironment::systemEnvironment();environment.insert("PYTHONPATH",QString(DATAHORIZON_FIXTURES)+"/../../service");cli.setProcessEnvironment(environment);
        cli.start("python3",{"-m","datahorizon.cli","--approve-code",code["activation_key"].toString(),setup.temp.filePath("quoted.dhwb"),"macro","quoted","run","--fingerprint",code["fingerprint"].toString()});
        cli.write("{}");cli.closeWriteChannel();QVERIFY(cli.waitForFinished(30000));
        const auto output=cli.readAllStandardOutput();QVERIFY2(cli.exitCode()==0,(output+cli.readAllStandardError()).constData());
        QCOMPARE(QJsonDocument::fromJson(output).object()["macro_result"].toDouble(),42.0);
    }

    void pickingLeavesDataSelectionAndStateUnchanged() {
        PickSetup setup;const QString text="x = \n";QVERIFY(setup.start("unchanged.dhwb","Supplies",text,4));
        auto &window=*setup.window;auto *workspace=setup.workspace;auto *pick=pickAction(workspace);auto *view=grid(window);
        auto *address=window.findChild<QLineEdit *>("address");auto *source=window.findChild<QLineEdit *>("source");
        selectRange(window,"C5:D6");
        const auto state=window.model()->state();const auto selection=view->selectionModel()->selection();const auto current=view->currentIndex();
        const auto addressText=address->text();const auto sourceText=source->text();const auto before=mainStatus(window);
        QSignalSpy admitted(&window,&MainWindow::operationAdmitted);QSignalSpy finished(&window,&MainWindow::operationFinished);
        pick->trigger();QVERIFY(pick->isChecked());
        clickCell(window,2,1);dragCells(window,1,1,3,1);
        // Double-click and typing in the grid neither edit nor navigate.
        const auto point=view->visualRect(window.model()->index(6,6)).center();
        QTest::mouseDClick(view->viewport(),Qt::LeftButton,{},point);
        view->setFocus();QTest::keyClicks(view,"7");QTest::keyClick(view,Qt::Key_Down);QTest::keyClick(view,Qt::Key_Return);
        for(auto *line:view->findChildren<QLineEdit *>())QVERIFY(!line->isVisible());
        QVERIFY(pick->isChecked());QCOMPARE(view->selectionModel()->selection(),selection);QCOMPARE(view->currentIndex(),current);
        QCOMPARE(address->text(),addressText);QCOMPARE(source->text(),sourceText);QVERIFY(!source->isModified());QVERIFY(!address->isModified());
        // Navigation through the address box during picking is undone when it ends.
        selectRange(window,"A1");QCOMPARE(view->currentIndex(),window.model()->index(0,0));
        QTest::keyClick(view,Qt::Key_Escape);QVERIFY(!pick->isChecked());
        QCOMPARE(view->selectionModel()->selection(),selection);QCOMPARE(view->currentIndex(),current);QCOMPARE(mainStatus(window),before);
        QTest::qWait(300);
        QCOMPARE(window.model()->state(),state);QCOMPARE(admitted.count(),0);QCOMPARE(finished.count(),0);
        QCOMPARE(editor(workspace)->toPlainText(),QString("x = \"'Supplies'!B3\"\"'Supplies'!B2:B4\"\n"));
        // A sheet switch during picking keeps it armed, and the new sheet's selection is left alone.
        pick->trigger();auto *tabs=window.findChild<QTabBar *>("sheets");tabs->setCurrentIndex(0);
        QVERIFY(pick->isChecked());view->setCurrentIndex(window.model()->index(1,1));
        clickCell(window,0,0);QVERIFY(editor(workspace)->toPlainText().contains("\"'Sheet1'!A1\""));
        QTest::keyClick(view,Qt::Key_Escape);QVERIFY(!pick->isChecked());QCOMPARE(view->currentIndex(),window.model()->index(1,1));
    }

    void escapeCancelsFromEitherWindow() {
        PickSetup setup;const QString text="x = \n";QVERIFY(setup.start("escape.dhwb","Supplies",text,4));
        auto &window=*setup.window;auto *workspace=setup.workspace;auto *pick=pickAction(workspace);
        const auto before=mainStatus(window);QSignalSpy finished(&window,&MainWindow::externalPickFinished);
        // In the workspace window.
        pick->trigger();QVERIFY(pick->isChecked());QCOMPARE(mainStatus(window),kPicking);
        QVERIFY(!workspace->findChild<QLabel *>("workspaceMessage")->text().isEmpty());
        workspace->activateWindow();QTRY_VERIFY_WITH_TIMEOUT(workspace->isActiveWindow(),5000);QTest::keyClick(editor(workspace),Qt::Key_Escape);
        QVERIFY(!pick->isChecked());QCOMPARE(mainStatus(window),before);QCOMPARE(finished.count(),1);QCOMPARE(finished[0][0].toBool(),false);
        clickCell(window,2,1);QCOMPARE(editor(workspace)->toPlainText(),text);
        // In the workbook window, from the grid and from the formula bar.
        pick->trigger();QVERIFY(pick->isChecked());window.activateWindow();QTRY_VERIFY_WITH_TIMEOUT(window.isActiveWindow(),5000);grid(window)->setFocus();QTest::keyClick(grid(window),Qt::Key_Escape);
        QVERIFY(!pick->isChecked());QCOMPARE(finished.count(),2);
        pick->trigger();QVERIFY(pick->isChecked());auto *source=window.findChild<QLineEdit *>("source");source->setFocus();QTest::keyClick(source,Qt::Key_Escape);
        QVERIFY(!pick->isChecked());QCOMPARE(finished.count(),3);
        clickCell(window,2,1);QCOMPARE(editor(workspace)->toPlainText(),text);QCOMPARE(mainStatus(window),before);
        // Selecting another module ends picking; so does a busy window.
        pick->trigger();QVERIFY(pick->isChecked());QVERIFY(createModule(workspace,"other"));
        QVERIFY(!pick->isChecked());QCOMPARE(finished.count(),4);
        pick->trigger();QVERIFY(pick->isChecked());
        QVERIFY(apply(window,{{"action","edit"},{"cells",QJsonArray{QJsonObject{{"row",1},{"col",1},{"source","1"}}}}}));
        QVERIFY(!pick->isChecked());QCOMPARE(finished.count(),5);
    }

    void refusedWhileCellDraftActive() {
        PickSetup setup;const QString text="x = \n";QVERIFY(setup.start("refused.dhwb","Supplies",text,4));
        auto &window=*setup.window;auto *workspace=setup.workspace;auto *pick=pickAction(workspace);auto *view=grid(window);
        auto *source=window.findChild<QLineEdit *>("source");QSignalSpy finished(&window,&MainWindow::externalPickFinished);
        // A formula-bar draft.
        view->setCurrentIndex(window.model()->index(3,3));QTRY_VERIFY_WITH_TIMEOUT(source->isEnabled(),5000);
        source->setFocus();QTest::keyClicks(source,"12");QVERIFY(source->isModified());
        pick->trigger();QVERIFY(!pick->isChecked());QVERIFY(issues(window).contains("cell draft"));
        QVERIFY(workspace->findChild<QLabel *>("workspaceMessage")->text().contains("cell draft"));
        QCOMPARE(source->text(),QString("12"));QVERIFY(source->isModified());
        QTest::keyClick(source,Qt::Key_Escape);QVERIFY(!source->isModified());
        // An in-cell editor.
        view->setCurrentIndex(window.model()->index(4,4));QTRY_VERIFY_WITH_TIMEOUT(window.model()->sourceReady(view->currentIndex()),5000);
        view->edit(view->currentIndex());auto *cellEditor=view->findChild<QLineEdit *>();QVERIFY(cellEditor);cellEditor->setFocus();QTest::keyClicks(cellEditor,"5");
        pick->trigger();QVERIFY(!pick->isChecked());QTest::keyClick(cellEditor,Qt::Key_Escape);
        // The relational view.
        window.findChild<QAction *>("relationalWorkbenchAction")->trigger();
        pick->trigger();QVERIFY(!pick->isChecked());QVERIFY(issues(window).contains("Return to sheets before picking"));
        QCOMPARE(finished.count(),0);QCOMPARE(editor(workspace)->toPlainText(),text);
        // A read-only workbook's editor is not editable, so picking is unavailable there.
        StartupOptions options;options.automaticWorkbook=false;MainWindow readonly(options);QVERIFY(boot(readonly,setup.temp.filePath("refused.dhwb"),false,true));
        auto *readWorkspace=openWorkspace(readonly);QVERIFY(readWorkspace);
        QVERIFY(!pickAction(readWorkspace)->isEnabled());QVERIFY(pickAction(readWorkspace)->toolTip().contains("Read-only"));
    }

    void formulaReferencePickStillWorks() {
        PickSetup setup;const QString text="x = \n";QVERIFY(setup.start("formula.dhwb","Supplies",text,4));
        auto &window=*setup.window;auto *workspace=setup.workspace;auto *pick=pickAction(workspace);auto *view=grid(window);auto *model=window.model();
        auto *source=window.findChild<QLineEdit *>("source");auto *formulaPick=window.findChild<QAction *>("pickReferenceAction");
        QVERIFY(apply(window,{{"action","edit"},{"cells",QJsonArray{QJsonObject{{"row",1},{"col",1},{"source","1"}},QJsonObject{{"row",1},{"col",2},{"source","2"}},QJsonObject{{"row",2},{"col",1},{"source","3"}},QJsonObject{{"row",2},{"col",2},{"source","4"}}}}}));
        // The existing formula pick scenario: a dragged range goes into the formula draft and is stored on Enter.
        view->setCurrentIndex(model->index(3,3));QTRY_VERIFY_WITH_TIMEOUT(source->isEnabled(),5000);source->setFocus();QTest::keyClicks(source,"=SUM(");
        formulaPick->trigger();QVERIFY(formulaPick->isChecked());dragCells(window,0,0,1,1);
        QCOMPARE(source->text(),QString("=SUM(A1:B2"));QCOMPARE(view->currentIndex(),model->index(3,3));QCOMPARE(editor(workspace)->toPlainText(),text);
        // Starting Python picking cancels a formula pick; the formula draft is kept, so Python picking is refused.
        formulaPick->trigger();QVERIFY(formulaPick->isChecked());
        pick->trigger();QVERIFY(!formulaPick->isChecked());QVERIFY(!pick->isChecked());QCOMPARE(source->text(),QString("=SUM(A1:B2"));
        // Arming the formula pick selected the operand for replacement; type after it.
        source->end(false);QTest::keyClicks(source,")");QTest::keyClick(source,Qt::Key_Return);
        QTRY_COMPARE_WITH_TIMEOUT(model->data(model->index(3,3)).toString(),QString("10"),10000);
        // Python picking, then a formula pick in the same session: neither inserts into the other.
        QTRY_VERIFY_WITH_TIMEOUT(pick->isEnabled(),5000);placeCursor(workspace,4);
        pick->trigger();QVERIFY(pick->isChecked());clickCell(window,0,1);
        QCOMPARE(editor(workspace)->toPlainText(),QString("x = \"'Supplies'!B1\"\n"));QVERIFY(!source->isModified());
        formulaPick->trigger();QVERIFY(!pick->isChecked());QVERIFY(!formulaPick->isChecked());
        view->setCurrentIndex(model->index(4,3));QTRY_VERIFY_WITH_TIMEOUT(source->isEnabled(),5000);source->setFocus();QTest::keyClicks(source,"=A1+");
        formulaPick->trigger();QVERIFY(formulaPick->isChecked());clickCell(window,1,1);
        QCOMPARE(source->text(),QString("=A1+B2"));QCOMPARE(editor(workspace)->toPlainText(),QString("x = \"'Supplies'!B1\"\n"));
        QTest::keyClick(source,Qt::Key_Escape);
    }
};

QTEST_MAIN(PythonWorkspaceTests)
#include "test_python_workspace.moc"
