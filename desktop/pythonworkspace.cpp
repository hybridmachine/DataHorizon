// SPDX-License-Identifier: GPL-3.0-only
#include "pythonworkspace.h"
#include "codeeditor.h"
#include "macrodeclarationpanel.h"
#include "pythonhighlighter.h"
#include <QAction>
#include <QCheckBox>
#include <QCloseEvent>
#include <QDialog>
#include <QDialogButtonBox>
#include <QHBoxLayout>
#include <QInputDialog>
#include <QLabel>
#include <QLineEdit>
#include <QListWidget>
#include <QMessageBox>
#include <QPushButton>
#include <QRegularExpression>
#include <QScopedValueRollback>
#include <QSet>
#include <QSettings>
#include <QShortcut>
#include <QSplitter>
#include <QStackedWidget>
#include <QStatusBar>
#include <QToolBar>
#include <QUuid>
#include <QVBoxLayout>

namespace {
const char *kNewPrefix="new:";
QString draftStatus(){return QObject::tr("Draft · not saved to the workbook");}
}

PythonWorkspace::PythonWorkspace(RestClient *client,QWidget *owner):QMainWindow(owner,Qt::Window),m_client(client) {
    setObjectName("pythonWorkspace");setWindowTitle(tr("Python workspace"));setAccessibleName(tr("Python workspace"));

    auto *toolbar=addToolBar(tr("Workspace"));toolbar->setObjectName("workspaceToolbar");toolbar->setMovable(false);
    m_save=toolbar->addAction(tr("Save"));m_save->setObjectName("saveModuleAction");m_save->setShortcut(QKeySequence::Save);m_save->setShortcutContext(Qt::WindowShortcut);
    connect(m_save,&QAction::triggered,this,&PythonWorkspace::save);
    toolbar->addSeparator();
    m_showMacro=toolbar->addAction(tr("Declaration"));m_showMacro->setObjectName("showMacroAction");m_showMacro->setCheckable(true);m_showMacro->setChecked(true);
    m_showMacro->setToolTip(tr("Show the selected macro's declaration"));
    m_find=new QAction(tr("Find…"),this);m_find->setObjectName("findAction");m_find->setShortcut(QKeySequence::Find);m_find->setShortcutContext(Qt::WindowShortcut);addAction(m_find);

    auto *splitter=new QSplitter(Qt::Horizontal);splitter->setObjectName("workspaceSplitter");setCentralWidget(splitter);

    auto *modulesColumn=new QWidget;auto *modulesLayout=new QVBoxLayout(modulesColumn);modulesLayout->setContentsMargins(4,4,4,4);
    modulesLayout->addWidget(new QLabel(tr("Modules")));
    m_modules=new QListWidget;m_modules->setObjectName("moduleList");m_modules->setAccessibleName(tr("Workbook modules"));modulesLayout->addWidget(m_modules,1);
    m_emptyHint=new QLabel(tr("This workbook has no Python modules yet. Create one to write macros."));m_emptyHint->setObjectName("moduleEmptyHint");m_emptyHint->setWordWrap(true);modulesLayout->addWidget(m_emptyHint);
    m_newModule=new QPushButton(tr("New module…"));m_newModule->setObjectName("newModuleButton");modulesLayout->addWidget(m_newModule);
    m_discard=new QPushButton(tr("Discard draft…"));m_discard->setObjectName("discardDraftButton");modulesLayout->addWidget(m_discard);
    splitter->addWidget(modulesColumn);

    auto *editorColumn=new QWidget;auto *editorLayout=new QVBoxLayout(editorColumn);editorLayout->setContentsMargins(0,4,0,4);
    m_findBar=new QWidget;m_findBar->setObjectName("findBar");auto *findLayout=new QHBoxLayout(m_findBar);findLayout->setContentsMargins(0,0,0,0);
    findLayout->addWidget(new QLabel(tr("Find")));
    m_findField=new QLineEdit;m_findField->setObjectName("findField");m_findField->setAccessibleName(tr("Find in module"));m_findField->setPlaceholderText(tr("Return finds the next match"));findLayout->addWidget(m_findField,1);
    auto *closeFind=new QPushButton(tr("Close"));closeFind->setObjectName("closeFindButton");findLayout->addWidget(closeFind);
    m_findBar->hide();editorLayout->addWidget(m_findBar);
    m_editor=new CodeEditor;m_editor->setObjectName("codeEditor");m_editor->setAccessibleName(tr("Module source"));editorLayout->addWidget(m_editor,1);
    new PythonHighlighter(m_editor->document());
    splitter->addWidget(editorColumn);

    m_side=new QStackedWidget;m_side->setObjectName("sidePanel");
    auto *macroPage=new QWidget;macroPage->setObjectName("macroPage");auto *macroLayout=new QVBoxLayout(macroPage);macroLayout->setContentsMargins(4,4,4,4);
    macroLayout->addWidget(new QLabel(tr("Macros")));
    m_macros=new QListWidget;m_macros->setObjectName("macroList");m_macros->setAccessibleName(tr("Declared macros"));m_macros->setMaximumHeight(140);macroLayout->addWidget(m_macros);
    auto *macroButtons=new QHBoxLayout;macroLayout->addLayout(macroButtons);
    m_addMacro=new QPushButton(tr("Add declaration…"));m_addMacro->setObjectName("addMacroButton");macroButtons->addWidget(m_addMacro);
    m_removeMacro=new QPushButton(tr("Remove declaration"));m_removeMacro->setObjectName("removeMacroButton");macroButtons->addWidget(m_removeMacro);
    m_panel=new MacroDeclarationPanel;macroLayout->addWidget(m_panel,1);
    m_side->addWidget(macroPage);splitter->addWidget(m_side);
    splitter->setStretchFactor(0,1);splitter->setStretchFactor(1,4);splitter->setStretchFactor(2,2);

    m_status=new QLabel;m_status->setObjectName("workspaceStatus");m_status->setAccessibleName(tr("Draft or saved state"));
    m_approval=new QLabel;m_approval->setObjectName("workspaceApproval");m_approval->setAccessibleName(tr("Approval state"));
    m_message=new QLabel;m_message->setObjectName("workspaceMessage");m_message->setAccessibleName(tr("Workspace messages"));m_message->setWordWrap(true);
    m_message->setTextInteractionFlags(Qt::TextSelectableByMouse);
    statusBar()->addWidget(m_status);statusBar()->addWidget(m_message,1);statusBar()->addPermanentWidget(m_approval);

    connect(m_showMacro,&QAction::toggled,m_side,&QWidget::setVisible);
    connect(m_find,&QAction::triggered,this,[this]{m_findBar->show();m_findField->setFocus();m_findField->selectAll();});
    connect(m_findField,&QLineEdit::returnPressed,this,&PythonWorkspace::findNext);
    connect(closeFind,&QPushButton::clicked,this,[this]{m_findBar->hide();m_editor->setFocus();});
    auto *escape=new QShortcut(QKeySequence(Qt::Key_Escape),m_findField,nullptr,nullptr,Qt::WidgetShortcut);
    connect(escape,&QShortcut::activated,this,[this]{m_findBar->hide();m_editor->setFocus();});
    connect(m_modules,&QListWidget::currentItemChanged,this,[this](QListWidgetItem *item){
        if(m_loading || !item)return;m_current=item->data(Qt::UserRole).toString();m_currentMacro=-1;loadCurrent();});
    connect(m_editor,&QPlainTextEdit::textChanged,this,[this]{
        if(m_loading || m_current.isEmpty())return;m_editor->clearMarks();updateDraft(m_editor->toPlainText(),effective(m_current).macros);});
    connect(m_macros,&QListWidget::currentRowChanged,this,[this](int row){if(m_loading)return;m_currentMacro=row;loadMacros();});
    connect(m_panel,&MacroDeclarationPanel::changed,this,[this]{
        if(m_loading || m_current.isEmpty())return;auto macros=effective(m_current).macros;
        if(m_currentMacro<0 || m_currentMacro>=macros.size())return;
        const auto declaration=m_panel->declaration();macros[m_currentMacro]=declaration;
        if(auto *item=m_macros->item(m_currentMacro))item->setText(declaration["name"].toString());
        updateDraft(m_editor->toPlainText(),macros);});
    connect(m_newModule,&QPushButton::clicked,this,&PythonWorkspace::newModule);
    connect(m_discard,&QPushButton::clicked,this,&PythonWorkspace::discardDraft);
    connect(m_addMacro,&QPushButton::clicked,this,&PythonWorkspace::addMacro);
    connect(m_removeMacro,&QPushButton::clicked,this,&PythonWorkspace::removeMacro);
    updateState();
}

QString PythonWorkspace::fingerprint() const{return m_catalogReady?m_catalog["fingerprint"].toString():QString();}

void PythonWorkspace::showWorkspace() {
    if(!m_geometryRestored) {
        m_geometryRestored=true;bool restored=false;
        if(!m_viewStatePath.isEmpty()){QSettings settings(m_viewStatePath,QSettings::IniFormat);const auto geometry=settings.value("pythonWorkspace/geometry").toByteArray();restored=!geometry.isEmpty() && restoreGeometry(geometry);}
        if(!restored)resize(1000,680);
    }
    show();raise();activateWindow();
}

void PythonWorkspace::closeEvent(QCloseEvent *event) {
    // Closing the workspace only hides it; drafts stay in memory with the
    // workbook window, which asks about them when it closes.
    if(isVisible() && !m_viewStatePath.isEmpty()){QSettings settings(m_viewStatePath,QSettings::IniFormat);settings.setValue("pythonWorkspace/geometry",saveGeometry());settings.sync();}
    event->accept();
}

void PythonWorkspace::bind(const QJsonObject &info) {
    const auto workbook=info["workbook_id"].toString();
    if(!workbook.isEmpty() && !m_workbookId.isEmpty() && workbook!=m_workbookId) {
        if(!m_drafts.isEmpty())emit issue(tr("Python drafts for the previous workbook were discarded when another workbook opened."));
        m_drafts.clear();m_catalogs.clear();m_current.clear();m_catalog={};m_catalogReady=false;
    }
    if(!workbook.isEmpty())m_workbookId=workbook;
    // A new handle (reopen or service restart) needs a fresh catalog: approval
    // belongs to the service process, so it is never carried across.
    if(info["id"]!=m_info["id"])m_catalogReady=false;
    m_info=info;
    if(info.isEmpty()){++m_readGeneration;m_catalogReady=false;updateState();return;}
    readCatalog();updateState();
}

void PythonWorkspace::setLocked(bool locked){m_locked=locked;updateState();}

void PythonWorkspace::readCatalog() {
    const auto generation=++m_readGeneration;
    m_client->request("/v1/workbooks/"+m_info["id"].toString()+"/code",{{"expected_state",m_info["state"]}},[this,generation](QJsonObject result,QString error){
        if(generation!=m_readGeneration)return;
        if(!error.isEmpty()){showMessage(tr("Could not read workbook code: %1").arg(error));return;}
        applyCatalog(result);
    });
}

void PythonWorkspace::applyCatalog(const QJsonObject &catalog) {
    m_catalog=catalog;m_catalogReady=true;const auto current=catalog["fingerprint"].toString();m_catalogs[current]=catalogModules();
    if(!m_rebaseTo.isEmpty()) {
        // Our own save moved the fingerprint; other drafts reviewed against the
        // previous catalog are still reviewed against everything but that save.
        if(m_rebaseTo==current)for(auto &draft:m_drafts)if(draft.baseFingerprint==m_rebaseFrom)draft.baseFingerprint=current;
        m_rebaseFrom.clear();m_rebaseTo.clear();
    }
    if(m_reviewChanges) {
        m_reviewChanges=false;
        if(m_drafts.contains(m_saveKey)) {
            auto &draft=m_drafts[m_saveKey];const auto changes=describeChanges(m_catalogs.value(draft.baseFingerprint),catalogModules());
            draft.baseFingerprint=current;
            showMessage(tr("Workbook code changed since this draft began: %1. Your draft is kept; review it and Save again to replace the stored module.").arg(changes));
        }
    }
    QSet<QString> referenced{current};for(const auto &draft:std::as_const(m_drafts))referenced.insert(draft.baseFingerprint);
    for(auto it=m_catalogs.begin();it!=m_catalogs.end();)it=referenced.contains(it.key())?std::next(it):m_catalogs.erase(it);
    refreshList();
}

QString PythonWorkspace::describeChanges(const QJsonArray &before,const QJsonArray &after) const {
    QMap<QString,QJsonObject> old,now;for(const auto &value:before)old[value.toObject()["id"].toString()]=value.toObject();for(const auto &value:after)now[value.toObject()["id"].toString()]=value.toObject();
    QStringList changes;
    for(auto it=now.begin();it!=now.end();++it){if(!old.contains(it.key()))changes.append(tr("%1 added").arg(it.value()["name"].toString()));else if(old[it.key()]!=it.value())changes.append(tr("%1 changed").arg(it.value()["name"].toString()));}
    for(auto it=old.begin();it!=old.end();++it)if(!now.contains(it.key()))changes.append(tr("%1 deleted").arg(it.value()["name"].toString()));
    return changes.isEmpty()?tr("event bindings or other code changed"):changes.join(", ");
}

QJsonObject PythonWorkspace::storedModule(const QString &key) const {
    for(const auto &value:catalogModules())if(value.toObject()["id"].toString()==key)return value.toObject();
    return {};
}

PythonWorkspace::Draft PythonWorkspace::effective(const QString &key) const {
    if(m_drafts.contains(key))return m_drafts[key];
    const auto stored=storedModule(key);
    return {stored["id"].toString(),stored["name"].toString(),stored["source"].toString(),stored["macros"].toArray(),fingerprint()};
}

void PythonWorkspace::refreshList() {
    {
        QScopedValueRollback<bool> loading(m_loading,true);
        m_modules->clear();QSet<QString> listed;
        auto add=[this,&listed](const QString &key,const QString &name,const QString &tip){
            const bool draft=m_drafts.contains(key);auto *item=new QListWidgetItem(draft?name+QStringLiteral(" •"):name);
            item->setData(Qt::UserRole,key);item->setToolTip(draft?tip+"\n"+draftStatus():tip);m_modules->addItem(item);listed.insert(key);};
        for(const auto &value:catalogModules()){const auto module=value.toObject();add(module["id"].toString(),module["name"].toString(),tr("Stored module"));}
        for(auto it=m_drafts.begin();it!=m_drafts.end();++it)if(!listed.contains(it.key()))
            add(it.key(),it.value().name,it.key().startsWith(kNewPrefix)?tr("New module, not yet stored"):tr("No longer stored in the workbook"));
        if(!listed.contains(m_current)){m_current=m_modules->count()?m_modules->item(0)->data(Qt::UserRole).toString():QString();m_currentMacro=-1;}
        for(int row=0;row<m_modules->count();++row)if(m_modules->item(row)->data(Qt::UserRole).toString()==m_current)m_modules->setCurrentRow(row);
    }
    loadCurrent();
}

void PythonWorkspace::loadCurrent() {
    {
        QScopedValueRollback<bool> loading(m_loading,true);
        const auto module=effective(m_current);
        if(m_editor->toPlainText()!=module.source){m_editor->clearMarks();m_editor->setPlainText(module.source);}
        m_macros->clear();for(const auto &value:module.macros)m_macros->addItem(value.toObject()["name"].toString());
        if(m_currentMacro>=module.macros.size())m_currentMacro=int(module.macros.size())-1;
        if(m_currentMacro<0 && !module.macros.isEmpty())m_currentMacro=0;
        m_macros->setCurrentRow(m_currentMacro);
    }
    loadMacros();
}

void PythonWorkspace::loadMacros() {
    {
        QScopedValueRollback<bool> loading(m_loading,true);
        const auto macros=effective(m_current).macros;
        m_panel->setDeclaration(m_currentMacro>=0 && m_currentMacro<macros.size()?macros[m_currentMacro].toObject():QJsonObject{});
    }
    updateState();
}

bool PythonWorkspace::editable() const {
    return !m_info.isEmpty() && !m_info["readonly"].toBool() && !m_locked && m_catalogReady && m_saveOperation.isEmpty() && !m_awaitingAdmission;
}

void PythonWorkspace::updateState() {
    const bool readonly=m_info["readonly"].toBool(),canEdit=editable(),selected=!m_current.isEmpty(),draft=m_drafts.contains(m_current);
    const QString readOnlyReason=tr("Read-only workbook: reopen it for editing to change code.");
    const QString waitReason=m_info.isEmpty()?tr("Open a workbook first."):!m_saveOperation.isEmpty() || m_awaitingAdmission?tr("Saving…"):tr("Wait for the current operation to finish.");
    m_editor->setReadOnly(!canEdit || !selected);
    m_save->setEnabled(canEdit && draft);
    m_save->setToolTip(readonly?readOnlyReason:!canEdit?waitReason:!draft?tr("No unsaved changes in this module."):tr("Save this module to the workbook"));
    m_newModule->setEnabled(canEdit && m_modules->count()<16);
    m_newModule->setToolTip(readonly?readOnlyReason:!canEdit?waitReason:m_modules->count()>=16?tr("A workbook holds at most 16 modules."):tr("Create a module draft"));
    m_discard->setEnabled(draft && m_saveOperation.isEmpty() && !m_awaitingAdmission);
    m_addMacro->setEnabled(canEdit && selected);m_addMacro->setToolTip(readonly?readOnlyReason:QString());
    m_removeMacro->setEnabled(canEdit && selected && m_currentMacro>=0);
    m_panel->setEnabled(canEdit && selected && m_currentMacro>=0);
    m_emptyHint->setVisible(m_modules->count()==0);
    m_status->setText(readonly?tr("Read-only workbook"):!selected?QString():draft?draftStatus():tr("Saved in workbook"));
    m_approval->setText(m_catalogReady && m_catalog["approved"].toBool()?tr("Approved for this session"):tr("Not approved to run"));
}

void PythonWorkspace::showMessage(const QString &text){m_message->setText(text);}

void PythonWorkspace::updateDraft(const QString &source,const QJsonArray &macros) {
    const auto stored=storedModule(m_current);
    Draft draft=m_drafts.contains(m_current)?m_drafts[m_current]:Draft{stored["id"].toString(),stored["name"].toString(),{},{},fingerprint()};
    draft.source=source;draft.macros=macros;
    const bool unchanged=!stored.isEmpty() && draft.name==stored["name"].toString() && source==stored["source"].toString() && macros==stored["macros"].toArray();
    if(unchanged)m_drafts.remove(m_current);else m_drafts[m_current]=draft;
    for(int row=0;row<m_modules->count();++row) {
        auto *item=m_modules->item(row);if(item->data(Qt::UserRole).toString()!=m_current)continue;
        item->setText(unchanged?draft.name:draft.name+QStringLiteral(" •"));
    }
    updateState();
}

bool PythonWorkspace::validName(const QString &name,QString *problem) const {
    static const QStringList keywords{"False","None","True","and","as","assert","async","await","break","class","continue","def","del","elif","else","except",
        "finally","for","from","global","if","import","in","is","lambda","nonlocal","not","or","pass","raise","return","try","while","with","yield"};
    static const QRegularExpression identifier(QStringLiteral("^[A-Za-z][A-Za-z0-9_]{0,63}$"));
    if(!identifier.match(name).hasMatch() || keywords.contains(name)) {
        *problem=tr("Use a name that starts with a letter and has only ASCII letters, digits and underscores (at most 64 characters). Python keywords are not allowed.");return false;
    }
    return true;
}

void PythonWorkspace::newModule() {
    if(!editable())return;
    QInputDialog input(this);input.setObjectName("newModuleName");input.setWindowTitle(tr("New module"));input.setLabelText(tr("Module name"));
    input.setOption(QInputDialog::UsePlainTextEditForTextInput,false);
    input.setTextValue(QString("Module%1").arg(m_modules->count()+1));
    if(input.exec()!=QDialog::Accepted)return;
    const auto name=input.textValue().trimmed();QString problem;
    if(!validName(name,&problem)){showMessage(problem);return;}
    for(int row=0;row<m_modules->count();++row)if(effective(m_modules->item(row)->data(Qt::UserRole).toString()).name==name){showMessage(tr("A module named %1 already exists.").arg(name));return;}
    const auto key=QString(kNewPrefix)+QUuid::createUuid().toString(QUuid::WithoutBraces);
    m_drafts[key]=Draft{{},name,{},{},fingerprint()};m_current=key;m_currentMacro=-1;showMessage({});
    refreshList();m_editor->setFocus();
}

void PythonWorkspace::discardDraft() {
    if(!m_drafts.contains(m_current))return;
    QMessageBox box(QMessageBox::Question,tr("Discard draft?"),tr("Discard your unsaved changes to %1? The stored workbook code is not changed.").arg(m_drafts[m_current].name),QMessageBox::Discard|QMessageBox::Cancel,this);
    box.setObjectName("discardModuleDraft");box.setOption(QMessageBox::Option::DontUseNativeDialog);box.setDefaultButton(QMessageBox::Cancel);
    if(box.exec()!=QMessageBox::Discard)return;
    m_drafts.remove(m_current);showMessage({});refreshList();
}

void PythonWorkspace::addMacro() {
    if(!editable() || m_current.isEmpty())return;
    QDialog dialog(this);dialog.setObjectName("addMacroDialog");dialog.setWindowTitle(tr("Add macro declaration"));dialog.resize(520,380);
    auto *layout=new QVBoxLayout(&dialog);
    auto *explanation=new QLabel(tr("Declare the function's typed arguments and result. If the module has no function with this name, a stub is added to the end of the source."));
    explanation->setWordWrap(true);layout->addWidget(explanation);
    auto *panel=new MacroDeclarationPanel;panel->setObjectName("newMacroPanel");layout->addWidget(panel,1);
    auto *buttons=new QDialogButtonBox(QDialogButtonBox::Ok|QDialogButtonBox::Cancel);layout->addWidget(buttons);
    connect(buttons,&QDialogButtonBox::accepted,&dialog,&QDialog::accept);connect(buttons,&QDialogButtonBox::rejected,&dialog,&QDialog::reject);
    if(dialog.exec()!=QDialog::Accepted || !editable())return;
    const auto declaration=panel->declaration();const auto name=declaration["name"].toString();QString problem;
    if(!validName(name,&problem)){showMessage(tr("Macro not added. %1").arg(problem));return;}
    auto module=effective(m_current);
    for(const auto &value:module.macros)if(value.toObject()["name"].toString()==name){showMessage(tr("%1 is already declared in this module.").arg(name));return;}
    module.macros.append(declaration);
    auto source=m_editor->toPlainText();
    const QRegularExpression defined(QStringLiteral("^def\\s+%1\\s*\\(").arg(QRegularExpression::escape(name)),QRegularExpression::MultilineOption);
    if(!defined.match(source).hasMatch()) {
        QStringList arguments{QStringLiteral("ctx")};arguments.append(panel->argumentNames());
        if(!source.isEmpty())source+=source.endsWith('\n')?QStringLiteral("\n\n"):QStringLiteral("\n\n\n");
        source+=QStringLiteral("def %1(%2):\n    pass\n").arg(name,arguments.join(QStringLiteral(", ")));
        QScopedValueRollback<bool> loading(m_loading,true);m_editor->setPlainText(source);m_editor->moveCursor(QTextCursor::End);
    }
    updateDraft(source,module.macros);m_currentMacro=int(module.macros.size())-1;showMessage({});loadCurrent();
}

void PythonWorkspace::removeMacro() {
    if(!editable() || m_currentMacro<0)return;
    auto macros=effective(m_current).macros;if(m_currentMacro>=macros.size())return;
    macros.removeAt(m_currentMacro);updateDraft(m_editor->toPlainText(),macros);loadCurrent();
}

void PythonWorkspace::save() {
    if(!editable() || !m_drafts.contains(m_current))return;
    const auto draft=m_drafts[m_current];QString problem;
    if(!validName(draft.name,&problem)){showMessage(problem);return;}
    if(!s_skipHistoryPrompt && (m_info["undo"].isString() || m_info["redo"].isString())) {
        QMessageBox box(QMessageBox::Question,tr("Clear Undo history?"),tr("Saving code clears Undo history for this workbook. Continue?"),QMessageBox::Yes|QMessageBox::Cancel,this);
        box.setObjectName("codeSaveHistoryPrompt");box.setOption(QMessageBox::Option::DontUseNativeDialog);box.setDefaultButton(QMessageBox::Cancel);
        auto *again=new QCheckBox(tr("Don't ask again this session"));again->setObjectName("codeSaveHistoryDontAsk");box.setCheckBox(again);
        if(box.exec()!=QMessageBox::Yes)return;
        if(again->isChecked())s_skipHistoryPrompt=true;
        if(!editable() || !m_drafts.contains(m_current))return;
    }
    QJsonObject request{{"action","module_save"},{"name",draft.name},{"source",draft.source},{"macros",draft.macros},{"fingerprint",draft.baseFingerprint},{"label","Save Python module"}};
    if(!draft.moduleId.isEmpty())request["module"]=draft.moduleId;
    m_saveKey=m_current;m_saveBase=draft.baseFingerprint;m_editor->clearMarks();
    m_awaitingAdmission=true;updateState();
    emit submitRequested(request);
    if(m_awaitingAdmission){m_awaitingAdmission=false;m_saveKey.clear();showMessage(tr("Save did not start. Wait for the current operation to finish, then Save again."));updateState();}
}

void PythonWorkspace::operationAdmitted(const QString &operation) {
    if(!m_awaitingAdmission)return;
    m_awaitingAdmission=false;m_saveOperation=operation;showMessage(tr("Saving %1…").arg(m_drafts.value(m_saveKey).name));updateState();
}

void PythonWorkspace::operationReceipt(const QJsonObject &receipt) {
    if(m_saveOperation.isEmpty() || receipt["operation_id"].toString()!=m_saveOperation)return;
    const auto outcome=receipt["outcome"].toString();const auto name=m_drafts.value(m_saveKey).name;
    if(outcome=="unknown"){showMessage(tr("The outcome of saving %1 is unknown. Your draft is kept while the operation is reconciled.").arg(name));return;}
    m_saveOperation.clear();
    if(outcome=="committed") {
        m_drafts.remove(m_saveKey);
        if(m_current==m_saveKey && receipt["module_id"].isString())m_current=receipt["module_id"].toString();
        if(receipt["code_fingerprint"].isString()){m_rebaseFrom=m_saveBase;m_rebaseTo=receipt["code_fingerprint"].toString();}
        showMessage(tr("Saved %1 in the workbook.").arg(name));
    } else {
        const auto code=receipt["code"].toString(),message=receipt["message"].toString();
        showMessage(tr("%1 was not saved (%2): %3").arg(name,code,message));
        emit issue(tr("Python module %1 was not saved (%2): %3 Your draft is kept.").arg(name,code,message));
        static const QRegularExpression line(QStringLiteral("line (\\d+)"));const auto match=line.match(message);
        if(code=="INVALID_CODE" && match.hasMatch() && m_current==m_saveKey)m_editor->markLine(match.captured(1).toInt(),true);
        if(code=="CODE_CHANGED"){m_reviewChanges=true;readCatalog();}
    }
    updateState();
}

bool PythonWorkspace::resolveDrafts() {
    if(m_drafts.isEmpty())return true;
    QStringList names;for(const auto &draft:std::as_const(m_drafts))names.append(draft.name);
    const bool canSave=editable();
    QMessageBox box(QMessageBox::Question,tr("Unsaved Python drafts"),
        tr("These Python modules have changes that are not saved to the workbook: %1").arg(names.join(", ")),
        (canSave?QMessageBox::Save:QMessageBox::NoButton)|QMessageBox::Discard|QMessageBox::Cancel,isVisible()?static_cast<QWidget *>(this):parentWidget());
    box.setObjectName("pythonDraftsPrompt");box.setOption(QMessageBox::Option::DontUseNativeDialog);box.setDefaultButton(QMessageBox::Cancel);
    box.setInformativeText(canSave?tr("Save stores the selected draft and keeps this window open. Discard drops every draft."):tr("Discard drops every draft."));
    const auto answer=box.exec();
    if(answer==QMessageBox::Discard){m_drafts.clear();showMessage({});refreshList();return true;}
    if(answer==QMessageBox::Save) {
        if(!m_drafts.contains(m_current)){m_current=m_drafts.firstKey();m_currentMacro=-1;refreshList();}
        showWorkspace();save();
    }
    return false;
}

void PythonWorkspace::findNext() {
    const auto text=m_findField->text();if(text.isEmpty())return;
    if(!m_editor->find(text)) {
        auto cursor=m_editor->textCursor();cursor.movePosition(QTextCursor::Start);m_editor->setTextCursor(cursor);
        if(!m_editor->find(text)){showMessage(tr("No match for %1.").arg(text));return;}
    }
    showMessage({});
}
