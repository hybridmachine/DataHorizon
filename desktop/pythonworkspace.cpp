// SPDX-License-Identifier: GPL-3.0-only
#include "pythonworkspace.h"
#include "codeeditor.h"
#include "macrodeclarationpanel.h"
#include "namemanager.h"
#include "pythonhighlighter.h"
#include <QAction>
#include <QActionGroup>
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
    m_showNames=toolbar->addAction(tr("Names"));m_showNames->setObjectName("showNamesAction");m_showNames->setCheckable(true);
    m_showNames->setToolTip(tr("Show the workbook's names"));
    toolbar->addSeparator();
    m_pick=toolbar->addAction(tr("Pick from sheet"));m_pick->setObjectName("pickFromSheetAction");m_pick->setCheckable(true);
    auto *sideGroup=new QActionGroup(this);sideGroup->setExclusionPolicy(QActionGroup::ExclusionPolicy::ExclusiveOptional);sideGroup->addAction(m_showMacro);sideGroup->addAction(m_showNames);
    m_find=new QAction(tr("Find…"),this);m_find->setObjectName("findAction");m_find->setShortcut(QKeySequence::Find);m_find->setShortcutContext(Qt::WindowShortcut);addAction(m_find);

    auto *splitter=new QSplitter(Qt::Horizontal);splitter->setObjectName("workspaceSplitter");setCentralWidget(splitter);

    auto *modulesColumn=new QWidget;auto *modulesLayout=new QVBoxLayout(modulesColumn);modulesLayout->setContentsMargins(4,4,4,4);
    modulesLayout->addWidget(new QLabel(tr("Modules")));
    m_modules=new QListWidget;m_modules->setObjectName("moduleList");m_modules->setAccessibleName(tr("Workbook modules"));modulesLayout->addWidget(m_modules,1);
    m_emptyHint=new QLabel(tr("This workbook has no Python modules yet. Create one to write macros."));m_emptyHint->setObjectName("moduleEmptyHint");m_emptyHint->setWordWrap(true);modulesLayout->addWidget(m_emptyHint);
    m_newModule=new QPushButton(tr("New module…"));m_newModule->setObjectName("newModuleButton");modulesLayout->addWidget(m_newModule);
    m_renameModule=new QPushButton(tr("Rename module…"));m_renameModule->setObjectName("renameModuleButton");modulesLayout->addWidget(m_renameModule);
    m_deleteModule=new QPushButton(tr("Delete module…"));m_deleteModule->setObjectName("deleteModuleButton");modulesLayout->addWidget(m_deleteModule);
    m_discard=new QPushButton(tr("Discard draft…"));m_discard->setObjectName("discardDraftButton");modulesLayout->addWidget(m_discard);
    splitter->addWidget(modulesColumn);

    auto *editorColumn=new QWidget;auto *editorLayout=new QVBoxLayout(editorColumn);editorLayout->setContentsMargins(0,4,0,4);
    m_findBar=new QWidget;m_findBar->setObjectName("findBar");auto *findLayout=new QHBoxLayout(m_findBar);findLayout->setContentsMargins(0,0,0,0);
    findLayout->addWidget(new QLabel(tr("Find")));
    m_findField=new QLineEdit;m_findField->setObjectName("findField");m_findField->setAccessibleName(tr("Find in module"));m_findField->setPlaceholderText(tr("Return finds the next match"));findLayout->addWidget(m_findField,1);
    auto *closeFind=new QPushButton(tr("Close"));closeFind->setObjectName("closeFindButton");findLayout->addWidget(closeFind);
    m_findBar->hide();editorLayout->addWidget(m_findBar);
    // Persistent until the conflict is resolved or another module is selected;
    // ordinary messages never clear it.
    m_conflict=new QLabel;m_conflict->setObjectName("workspaceConflict");m_conflict->setAccessibleName(tr("Conflict with stored code"));m_conflict->setWordWrap(true);
    m_conflict->setStyleSheet("QLabel{background:rgba(255,170,0,60);padding:4px;}");m_conflict->hide();editorLayout->addWidget(m_conflict);
    m_editor=new CodeEditor;m_editor->setObjectName("codeEditor");m_editor->setAccessibleName(tr("Module source"));editorLayout->addWidget(m_editor,1);
    new PythonHighlighter(m_editor->document());
    splitter->addWidget(editorColumn);

    m_side=new QStackedWidget;m_side->setObjectName("sidePanel");
    auto *macroPage=m_macroPage=new QWidget;macroPage->setObjectName("macroPage");auto *macroLayout=new QVBoxLayout(macroPage);macroLayout->setContentsMargins(4,4,4,4);
    macroLayout->addWidget(new QLabel(tr("Macros")));
    m_macros=new QListWidget;m_macros->setObjectName("macroList");m_macros->setAccessibleName(tr("Declared macros"));m_macros->setMaximumHeight(140);macroLayout->addWidget(m_macros);
    auto *macroButtons=new QHBoxLayout;macroLayout->addLayout(macroButtons);
    m_addMacro=new QPushButton(tr("Add declaration…"));m_addMacro->setObjectName("addMacroButton");macroButtons->addWidget(m_addMacro);
    m_removeMacro=new QPushButton(tr("Remove declaration"));m_removeMacro->setObjectName("removeMacroButton");macroButtons->addWidget(m_removeMacro);
    m_panel=new MacroDeclarationPanel;macroLayout->addWidget(m_panel,1);
    m_side->addWidget(macroPage);
    m_namesPage=new QWidget;m_namesPage->setObjectName("namesPage");auto *namesLayout=new QVBoxLayout(m_namesPage);namesLayout->setContentsMargins(4,4,4,4);
    namesLayout->addWidget(new QLabel(tr("Names (double-click to insert)")));
    m_names=new NamesPanel;namesLayout->addWidget(m_names,1);
    m_side->addWidget(m_namesPage);splitter->addWidget(m_side);
    splitter->setStretchFactor(0,1);splitter->setStretchFactor(1,4);splitter->setStretchFactor(2,2);

    m_status=new QLabel;m_status->setObjectName("workspaceStatus");m_status->setAccessibleName(tr("Draft or saved state"));
    m_approval=new QLabel;m_approval->setObjectName("workspaceApproval");m_approval->setAccessibleName(tr("Approval state"));
    m_message=new QLabel;m_message->setObjectName("workspaceMessage");m_message->setAccessibleName(tr("Workspace messages"));m_message->setWordWrap(true);
    m_message->setTextInteractionFlags(Qt::TextSelectableByMouse);
    statusBar()->addWidget(m_status);statusBar()->addWidget(m_message,1);statusBar()->addPermanentWidget(m_approval);

    auto showSide=[this]{
        const bool names=m_showNames->isChecked();m_side->setVisible(names || m_showMacro->isChecked());
        m_side->setCurrentWidget(names?m_namesPage:m_macroPage);};
    connect(m_showMacro,&QAction::toggled,this,showSide);connect(m_showNames,&QAction::toggled,this,showSide);
    connect(m_names,&NamesPanel::insertRequested,this,[this](const QString &name){
        // Inserting is an ordinary edit: it needs an editable module and makes a draft.
        if(m_editor->isReadOnly()){showMessage(tr("Names are inserted only into an editable module. %1").arg(blockedReason()));return;}
        m_editor->insertAtCursor(QStringLiteral("\"%1\"").arg(name));m_editor->setFocus();});
    // Picking stays armed for several picks until Escape, a second press, or the
    // owning window ends it.
    m_pickEscape=new QShortcut(QKeySequence(Qt::Key_Escape),this,nullptr,nullptr,Qt::WindowShortcut);m_pickEscape->setEnabled(false);
    connect(m_pickEscape,&QShortcut::activated,this,[this]{emit pickFromSheetRequested(false);});
    connect(m_pick,&QAction::triggered,this,[this](bool on){
        if(on){m_pickModule=m_current;showMessage(tr("Click a cell or drag a range in the workbook window to insert its reference. Esc or Pick from sheet again ends picking."));}
        emit pickFromSheetRequested(on);
        m_pickEscape->setEnabled(m_pick->isChecked());});
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
    connect(m_renameModule,&QPushButton::clicked,this,&PythonWorkspace::renameModule);
    connect(m_deleteModule,&QPushButton::clicked,this,&PythonWorkspace::deleteModule);
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
    if(m_pick->isChecked())emit pickFromSheetRequested(false);
    if(isVisible() && !m_viewStatePath.isEmpty()){QSettings settings(m_viewStatePath,QSettings::IniFormat);settings.setValue("pythonWorkspace/geometry",saveGeometry());settings.sync();}
    event->accept();
}


void PythonWorkspace::bind(const QJsonObject &info) {
    const auto workbook=info["workbook_id"].toString();
    if(!workbook.isEmpty() && !m_workbookId.isEmpty() && workbook!=m_workbookId) {
        // Drafts are never dropped here. They stay listed, marked as belonging
        // to the previous workbook and unsaveable, until the user discards them.
        m_catalog={};m_catalogReady=false;if(!m_drafts.contains(m_current))m_current.clear();
        if(!m_drafts.isEmpty())emit issue(tr("Python drafts from the previously open workbook are kept in the Python workspace. They cannot be saved into this workbook; discard them when you no longer need them."));
    }
    if(!workbook.isEmpty())m_workbookId=workbook;
    // A new handle (reopen or service restart) needs a fresh catalog: approval
    // belongs to the service process, so it is never carried across. An
    // operation still in flight on the old handle is reconciled by the owning
    // window before this point; nothing here may stay waiting for it.
    if(info["id"]!=m_info["id"]){m_catalogReady=false;resetPending();}
    m_info=info;
    if(info.isEmpty()){++m_readGeneration;m_catalogReady=false;updateState();return;}
    readCatalog();updateState();
}

void PythonWorkspace::resetPending() {
    m_pending=Pending::None;m_pendingOperation.clear();m_pendingKey.clear();m_pendingName.clear();m_pendingBase.clear();
    m_awaitingAdmission=false;m_reviewChanges=false;m_rebaseFrom.clear();m_rebaseTo.clear();
}

void PythonWorkspace::setLocked(bool locked){m_locked=locked;updateState();}

void PythonWorkspace::readCatalog() {
    const auto generation=++m_readGeneration;
    m_client->request("/v1/workbooks/"+m_info["id"].toString()+"/code",{{"expected_state",m_info["state"]}},[this,generation](QJsonObject result,QString error){
        if(generation!=m_readGeneration)return;
        if(!error.isEmpty()){m_reviewChanges=false;showMessage(tr("Could not read workbook code: %1. It is read again when the workbook refreshes.").arg(error));return;}
        applyCatalog(result);
    });
}

QJsonObject PythonWorkspace::baseModule(const Draft &draft) const {
    for(const auto &value:m_catalogs.value(draft.baseFingerprint))if(value.toObject()["id"].toString()==draft.moduleId)return value.toObject();
    return {};
}

void PythonWorkspace::applyCatalog(const QJsonObject &catalog) {
    m_catalog=catalog;m_catalogReady=true;const auto current=catalog["fingerprint"].toString();m_catalogs[current]=catalogModules();
    if(!m_rebaseTo.isEmpty()) {
        // Our own save moved the fingerprint; other drafts reviewed against the
        // previous catalog are still reviewed against everything but that save.
        if(m_rebaseTo==current)for(auto &draft:m_drafts)if(draft.baseFingerprint==m_rebaseFrom)draft.baseFingerprint=current;
        m_rebaseFrom.clear();m_rebaseTo.clear();
    }
    for(auto it=m_drafts.begin();it!=m_drafts.end();) {
        auto &draft=it.value();
        if(draft.workbookId!=m_workbookId || draft.moduleId.isEmpty()){++it;continue;}
        const auto stored=storedModule(draft.moduleId);
        // A draft that already matches the stored module (for example after a
        // reconciled save) is no longer a draft.
        if(!stored.isEmpty() && draft.name==stored["name"].toString() && draft.source==stored["source"].toString() && draft.macros==stored["macros"].toArray()){it=m_drafts.erase(it);continue;}
        // Its own module changed or was deleted since the draft's base: never
        // rebase silently; Save asks first.
        if(draft.baseFingerprint!=current && (!m_catalogs.contains(draft.baseFingerprint) || baseModule(draft)!=stored))draft.conflict=true;
        ++it;
    }
    if(m_reviewChanges) {
        m_reviewChanges=false;
        if(m_drafts.contains(m_pendingKey)) {
            auto &draft=m_drafts[m_pendingKey];const auto changes=describeChanges(m_catalogs.value(draft.baseFingerprint),catalogModules());
            if(draft.conflict)showMessage(tr("Workbook code changed since this draft began: %1. Save again to compare your draft with the stored module.").arg(changes));
            else {
                draft.baseFingerprint=current;
                showMessage(tr("Workbook code changed since this draft began: %1. This module is unchanged; your draft is kept and Save again stores it.").arg(changes));
            }
        }
        m_pendingKey.clear();
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

bool PythonWorkspace::foreign(const QString &key) const{return m_drafts.contains(key) && m_drafts[key].workbookId!=m_workbookId;}

PythonWorkspace::Draft PythonWorkspace::effective(const QString &key) const {
    if(m_drafts.contains(key))return m_drafts[key];
    const auto stored=storedModule(key);
    return {stored["id"].toString(),stored["name"].toString(),stored["source"].toString(),stored["macros"].toArray(),fingerprint(),m_workbookId};
}

void PythonWorkspace::refreshList() {
    {
        QScopedValueRollback<bool> loading(m_loading,true);
        m_modules->clear();QSet<QString> listed;
        auto add=[this,&listed](const QString &key,const QString &name,const QString &tip){
            const bool draft=m_drafts.contains(key);QString text=draft?name+QStringLiteral(" •"):name;
            if(foreign(key))text+=tr(" (previous workbook)");
            auto *item=new QListWidgetItem(text);
            item->setData(Qt::UserRole,key);item->setToolTip(draft?tip+"\n"+draftStatus():tip);m_modules->addItem(item);listed.insert(key);};
        for(const auto &value:catalogModules()){const auto module=value.toObject();add(module["id"].toString(),module["name"].toString(),tr("Stored module"));}
        for(auto it=m_drafts.begin();it!=m_drafts.end();++it)if(!listed.contains(it.key()))
            add(it.key(),it.value().name,foreign(it.key())?tr("Draft from the previously open workbook; it cannot be saved here"):
                it.key().startsWith(kNewPrefix)?tr("New module, not yet stored"):tr("No longer stored in the workbook"));
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
    return !m_info.isEmpty() && !m_info["readonly"].toBool() && !m_locked && m_catalogReady && m_pending==Pending::None && !m_awaitingAdmission;
}

QString PythonWorkspace::blockedReason() const {
    if(m_info["readonly"].toBool())return tr("Read-only workbook: reopen it for editing to change code.");
    if(foreign(m_current))return tr("This draft belongs to the previously open workbook and cannot be saved here. Discard it when you no longer need it.");
    if(m_info.isEmpty())return tr("Open a workbook first.");
    if(m_pending!=Pending::None || m_awaitingAdmission)return tr("Waiting for the workbook to confirm the last change…");
    if(editable() && m_current.isEmpty())return tr("Create or select a module first.");
    return tr("Wait for the current operation to finish.");
}

void PythonWorkspace::updateState() {
    const bool readonly=m_info["readonly"].toBool(),other=foreign(m_current),canEdit=editable() && !other,selected=!m_current.isEmpty(),draft=m_drafts.contains(m_current);
    const bool stored=!storedModule(m_current).isEmpty() && !other;
    const QString readOnlyReason=tr("Read-only workbook: reopen it for editing to change code.");
    const QString waitReason=m_info.isEmpty()?tr("Open a workbook first."):m_pending!=Pending::None || m_awaitingAdmission?tr("Waiting for the workbook to confirm the last change…"):tr("Wait for the current operation to finish.");
    const QString reason=blockedReason();
    m_editor->setReadOnly(!canEdit || !selected);
    m_save->setEnabled(canEdit && draft);
    m_save->setToolTip(!canEdit?reason:!draft?tr("No unsaved changes in this module."):tr("Save this module to the workbook"));
    m_newModule->setEnabled(editable() && m_modules->count()<16);
    m_newModule->setToolTip(!editable()?(readonly?readOnlyReason:waitReason):m_modules->count()>=16?tr("A workbook holds at most 16 modules."):tr("Create a module draft"));
    m_renameModule->setEnabled(canEdit && stored && !draft);
    m_renameModule->setToolTip(!canEdit?reason:!stored?tr("Save the new module before renaming it."):draft?tr("Save or discard this module's draft before renaming it."):tr("Rename this module in the workbook"));
    m_deleteModule->setEnabled(canEdit && stored);
    m_deleteModule->setToolTip(!canEdit?reason:!stored?tr("This module is not stored; use Discard draft instead."):tr("Delete this module from the workbook"));
    m_discard->setEnabled(draft && m_pending==Pending::None && !m_awaitingAdmission);
    m_addMacro->setEnabled(canEdit && selected);m_addMacro->setToolTip(!canEdit?reason:QString());
    m_removeMacro->setEnabled(canEdit && selected && m_currentMacro>=0);
    m_panel->setEnabled(canEdit && selected && m_currentMacro>=0);
    m_pick->setEnabled(canEdit && selected);
    m_pick->setToolTip(!canEdit || !selected?reason:tr("Click a cell or drag a range in the workbook window to insert its reference at the cursor. Addresses in code are not updated when rows, columns or sheets change; names keep code stable."));
    if(m_pick->isChecked() && !pickStillValid())emit pickFromSheetRequested(false);
    m_emptyHint->setVisible(m_modules->count()==0);
    const bool conflict=draft && m_drafts[m_current].conflict;
    m_conflict->setVisible(conflict);
    if(conflict)m_conflict->setText(storedModule(m_current).isEmpty()
        ?tr("%1 was deleted from the workbook since your draft began. Save asks whether to keep your draft or discard it.").arg(m_drafts[m_current].name)
        :tr("%1 changed in the workbook since your draft began. Save shows both versions before anything is replaced.").arg(m_drafts[m_current].name));
    m_status->setText(readonly?tr("Read-only workbook"):!selected?QString():draft?draftStatus():tr("Saved in workbook"));
    m_approval->setText(m_catalogReady && m_catalog["approved"].toBool()?tr("Approved for this session"):tr("Not approved to run"));
}

void PythonWorkspace::showMessage(const QString &text){m_message->setText(text);}

void PythonWorkspace::insertPickedReference(const QString &reference) {
    // A Python string literal; the reference never contains a newline.
    m_editor->insertAtCursor(QStringLiteral("\"%1\"").arg(QString(reference).replace("\\","\\\\").replace("\"","\\\"")));
}

bool PythonWorkspace::pickStillValid() const {
    return m_pick->isChecked() && !m_editor->isReadOnly() && !m_current.isEmpty() && m_current==m_pickModule;
}

void PythonWorkspace::pickFinished(const QString &problem) {
    m_pick->setChecked(false);m_pickEscape->setEnabled(false);m_pickModule.clear();
    showMessage(problem.isEmpty()?QString():tr("Picking could not start: %1").arg(problem));
}

void PythonWorkspace::updateDraft(const QString &source,const QJsonArray &macros) {
    m_discardConfirmed=false;
    const auto stored=storedModule(m_current);
    Draft draft=m_drafts.contains(m_current)?m_drafts[m_current]:Draft{stored["id"].toString(),stored["name"].toString(),{},{},fingerprint(),m_workbookId};
    draft.source=source;draft.macros=macros;
    const bool unchanged=!stored.isEmpty() && !draft.conflict && draft.workbookId==m_workbookId && draft.name==stored["name"].toString() && source==stored["source"].toString() && macros==stored["macros"].toArray();
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

bool PythonWorkspace::nameTaken(const QString &name,const QString &except) const {
    for(int row=0;row<m_modules->count();++row) {
        const auto key=m_modules->item(row)->data(Qt::UserRole).toString();
        if(key!=except && !foreign(key) && effective(key).name==name)return true;
    }
    return false;
}

void PythonWorkspace::newModule() {
    if(!editable())return;
    QInputDialog input(this);input.setObjectName("newModuleName");input.setWindowTitle(tr("New module"));input.setLabelText(tr("Module name"));
    input.setTextValue(QString("Module%1").arg(m_modules->count()+1));
    if(input.exec()!=QDialog::Accepted || !editable())return;
    const auto name=input.textValue().trimmed();QString problem;
    if(!validName(name,&problem)){showMessage(problem);return;}
    if(nameTaken(name,{})){showMessage(tr("A module named %1 already exists.").arg(name));return;}
    const auto key=QString(kNewPrefix)+QUuid::createUuid().toString(QUuid::WithoutBraces);
    m_discardConfirmed=false;
    m_drafts[key]=Draft{{},name,{},{},fingerprint(),m_workbookId};m_current=key;m_currentMacro=-1;showMessage({});
    refreshList();m_editor->setFocus();
}

void PythonWorkspace::renameModule() {
    const auto key=m_current;const auto stored=storedModule(key);
    if(!editable() || stored.isEmpty() || m_drafts.contains(key))return;
    QInputDialog input(this);input.setObjectName("renameModuleName");input.setWindowTitle(tr("Rename module"));input.setLabelText(tr("New name for %1").arg(stored["name"].toString()));
    input.setTextValue(stored["name"].toString());
    if(input.exec()!=QDialog::Accepted || !editable() || m_current!=key || m_drafts.contains(key))return;
    const auto name=input.textValue().trimmed();QString problem;
    if(name==stored["name"].toString())return;
    if(!validName(name,&problem)){showMessage(problem);return;}
    if(nameTaken(name,key)){showMessage(tr("A module named %1 already exists.").arg(name));return;}
    if(!confirmHistory())return;
    startOperation({{"action","module_save"},{"module",key},{"name",name},{"source",stored["source"]},{"macros",stored["macros"]},{"fingerprint",fingerprint()},{"label","Rename Python module"}},
        Pending::Rename,key,name,fingerprint());
}

void PythonWorkspace::deleteModule() {
    const auto key=m_current;const auto stored=storedModule(key);
    if(!editable() || stored.isEmpty())return;
    const auto name=stored["name"].toString();
    QMessageBox box(QMessageBox::Warning,tr("Delete module?"),tr("Delete module %1? This cannot be undone with Undo.").arg(name),QMessageBox::Yes|QMessageBox::Cancel,this);
    box.setObjectName("deleteModuleConfirm");box.setOption(QMessageBox::Option::DontUseNativeDialog);box.setDefaultButton(QMessageBox::Cancel);
    if(m_drafts.contains(key))box.setInformativeText(tr("Your unsaved draft of this module is discarded once the delete is stored."));
    if(box.exec()!=QMessageBox::Yes || !editable() || m_current!=key)return;
    if(!confirmHistory())return;
    startOperation({{"action","module_delete"},{"module",key},{"fingerprint",fingerprint()},{"label","Delete Python module"}},Pending::Delete,key,name,fingerprint());
}

void PythonWorkspace::discardDraft() {
    if(!m_drafts.contains(m_current))return;
    QMessageBox box(QMessageBox::Question,tr("Discard draft?"),tr("Discard your unsaved changes to %1? The stored workbook code is not changed.").arg(m_drafts[m_current].name),QMessageBox::Discard|QMessageBox::Cancel,this);
    box.setObjectName("discardModuleDraft");box.setOption(QMessageBox::Option::DontUseNativeDialog);box.setDefaultButton(QMessageBox::Cancel);
    if(box.exec()!=QMessageBox::Discard)return;
    m_drafts.remove(m_current);refreshList();
}

void PythonWorkspace::addMacro() {
    if(!editable() || m_current.isEmpty() || foreign(m_current))return;
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
    updateDraft(source,module.macros);m_currentMacro=int(module.macros.size())-1;loadCurrent();
}

void PythonWorkspace::removeMacro() {
    if(!editable() || m_currentMacro<0)return;
    auto macros=effective(m_current).macros;if(m_currentMacro>=macros.size())return;
    macros.removeAt(m_currentMacro);updateDraft(m_editor->toPlainText(),macros);loadCurrent();
}

bool PythonWorkspace::confirmHistoryClear(QWidget *parent,const QJsonObject &info,const QString &text,const QString &name) {
    if(s_skipHistoryPrompt || !(info["undo"].isString() || info["redo"].isString()))return true;
    QMessageBox box(QMessageBox::Question,tr("Clear Undo history?"),text,QMessageBox::Yes|QMessageBox::Cancel,parent);
    box.setObjectName(name+"Prompt");box.setOption(QMessageBox::Option::DontUseNativeDialog);box.setDefaultButton(QMessageBox::Cancel);
    auto *again=new QCheckBox(tr("Don't ask again this session"));again->setObjectName(name+"DontAsk");box.setCheckBox(again);
    if(box.exec()!=QMessageBox::Yes)return false;
    if(again->isChecked())s_skipHistoryPrompt=true;
    return true;
}

bool PythonWorkspace::confirmHistory() {
    return confirmHistoryClear(this,m_info,tr("Saving code clears Undo history for this workbook. Continue?"),QStringLiteral("codeSaveHistory")) && editable();
}

void PythonWorkspace::setNames(const QJsonArray &names) {
    m_names->setNames(names);
    QStringList identifiers;for(const auto &value:names)identifiers.append(value.toObject()["name"].toString());
    m_editor->setCompletionNames(identifiers);
}

// Shows the stored module beside the draft. True means "keep mine": the draft
// is rebased onto the current catalog and Save continues.
bool PythonWorkspace::resolveConflict() {
    const auto key=m_current;const Draft draft=m_drafts[key];const auto stored=storedModule(draft.moduleId);
    QDialog dialog(this);dialog.setObjectName("codeConflictDialog");dialog.setWindowTitle(tr("This module changed in the workbook"));dialog.resize(900,520);
    auto *layout=new QVBoxLayout(&dialog);
    auto *explanation=new QLabel(stored.isEmpty()
        ?tr("%1 was deleted from the workbook after your draft began. Keeping your draft stores it as a new module.").arg(draft.name)
        :tr("Someone else changed %1 after your draft began. Compare the versions before choosing.").arg(draft.name));
    explanation->setWordWrap(true);layout->addWidget(explanation);
    auto *panes=new QHBoxLayout;layout->addLayout(panes,1);
    auto pane=[&](const QString &title,const QString &name,const QString &text){
        auto *column=new QVBoxLayout;column->addWidget(new QLabel(title));
        auto *view=new QPlainTextEdit(text);view->setObjectName(name);view->setReadOnly(true);view->setAccessibleName(title);view->setLineWrapMode(QPlainTextEdit::NoWrap);
        view->setFont(QFontDatabase::systemFont(QFontDatabase::FixedFont));column->addWidget(view,1);panes->addLayout(column,1);};
    pane(stored.isEmpty()?tr("Stored version (deleted)"):tr("Stored version (%1)").arg(stored["name"].toString()),"conflictStoredSource",stored["source"].toString());
    pane(tr("Your draft"),"conflictDraftSource",draft.source);
    auto *buttons=new QDialogButtonBox;layout->addWidget(buttons);
    auto *keep=buttons->addButton(tr("Keep mine and replace"),QDialogButtonBox::AcceptRole);keep->setObjectName("conflictKeepMine");
    auto *take=buttons->addButton(tr("Discard mine and take the stored version"),QDialogButtonBox::DestructiveRole);take->setObjectName("conflictTakeStored");
    auto *cancel=buttons->addButton(QDialogButtonBox::Cancel);cancel->setObjectName("conflictCancel");cancel->setDefault(true);
    int choice=0;
    connect(keep,&QPushButton::clicked,&dialog,[&]{choice=1;dialog.accept();});
    connect(take,&QPushButton::clicked,&dialog,[&]{choice=2;dialog.accept();});
    connect(cancel,&QPushButton::clicked,&dialog,&QDialog::reject);
    dialog.exec();
    if(!m_drafts.contains(key) || m_current!=key)return false;
    if(choice==1) {
        auto &kept=m_drafts[key];kept.baseFingerprint=fingerprint();kept.conflict=false;
        if(stored.isEmpty())kept.moduleId.clear();
        return true;
    }
    if(choice==2){m_drafts.remove(key);showMessage(tr("Loaded the stored version of %1.").arg(stored["name"].toString()));refreshList();}
    return false;
}

void PythonWorkspace::save() {
    if(!editable() || !m_drafts.contains(m_current) || foreign(m_current))return;
    QString problem;
    if(!validName(m_drafts[m_current].name,&problem)){showMessage(problem);return;}
    const auto key=m_current;const Draft before=m_drafts[key];
    if(before.conflict && !resolveConflict())return;
    if(!confirmHistory() || !m_drafts.contains(m_current)) {
        // Declining the history prompt leaves a conflicted draft unresolved.
        if(before.conflict && m_drafts.contains(key))m_drafts[key]=before;
        updateState();return;
    }
    const auto draft=m_drafts[m_current];
    QJsonObject request{{"action","module_save"},{"name",draft.name},{"source",draft.source},{"macros",draft.macros},{"fingerprint",draft.baseFingerprint},{"label","Save Python module"}};
    if(!draft.moduleId.isEmpty())request["module"]=draft.moduleId;
    m_editor->clearMarks();
    startOperation(request,Pending::Save,m_current,draft.name,draft.baseFingerprint);
}

void PythonWorkspace::startOperation(QJsonObject request,Pending kind,const QString &key,const QString &name,const QString &base) {
    m_pendingKey=key;m_pendingName=name;m_pendingBase=base;
    m_awaitingAdmission=true;updateState();
    emit submitRequested(request);
    if(m_awaitingAdmission){m_awaitingAdmission=false;m_pendingKey.clear();showMessage(tr("The change did not start. Wait for the current operation to finish, then try again."));updateState();return;}
    m_pending=kind;updateState();
}

void PythonWorkspace::operationAdmitted(const QString &operation) {
    if(!m_awaitingAdmission)return;
    m_awaitingAdmission=false;m_pendingOperation=operation;showMessage(tr("Storing %1…").arg(m_pendingName));
}

void PythonWorkspace::operationReceipt(const QJsonObject &receipt) {
    if(m_pendingOperation.isEmpty() || receipt["operation_id"].toString()!=m_pendingOperation)return;
    const auto outcome=receipt["outcome"].toString();const auto name=m_pendingName;const auto key=m_pendingKey;const auto kind=m_pending;
    if(outcome=="unknown"){showMessage(tr("The outcome for %1 is unknown. Your draft is kept while the operation is reconciled.").arg(name));return;}
    m_pending=Pending::None;m_pendingOperation.clear();
    if(outcome=="committed") {
        m_pendingKey.clear();
        // The shown catalog is now stale: editing stays off until the owning
        // window's refresh binds the new state and the catalog is re-read
        // (a failed read is retried on the next bind or refresh).
        m_catalogReady=false;
        if(kind==Pending::Save) {
            m_drafts.remove(key);
            if(m_current==key && receipt["module_id"].isString())m_current=receipt["module_id"].toString();
            if(receipt["code_fingerprint"].isString()){m_rebaseFrom=m_pendingBase;m_rebaseTo=receipt["code_fingerprint"].toString();}
            showMessage(tr("Saved %1 in the workbook.").arg(name));
        } else if(kind==Pending::Rename) {
            showMessage(tr("Renamed the module to %1.").arg(name));
        } else {
            m_drafts.remove(key);showMessage(tr("Deleted %1 from the workbook.").arg(name));
        }
    } else {
        const auto code=receipt["code"].toString(),message=receipt["message"].toString();
        const auto what=kind==Pending::Rename?tr("renamed"):kind==Pending::Delete?tr("deleted"):tr("saved");
        showMessage(tr("%1 was not %2 (%3): %4").arg(name,what,code,message));
        emit issue(tr("Python module %1 was not %2 (%3): %4 Nothing was changed; drafts are kept.").arg(name,what,code,message));
        static const QRegularExpression line(QStringLiteral("line (\\d+)"));const auto match=line.match(message);
        if(kind==Pending::Save && code=="INVALID_CODE" && match.hasMatch() && m_current==key)m_editor->markLine(match.captured(1).toInt(),true);
        if(code=="CODE_CHANGED" && kind==Pending::Save){m_reviewChanges=true;readCatalog();}
        else{m_pendingKey.clear();if(code=="CODE_CHANGED")readCatalog();}
    }
    updateState();
}

bool PythonWorkspace::resolveDrafts() {
    if(m_drafts.isEmpty() || m_discardConfirmed)return true;
    QStringList names;for(auto it=m_drafts.cbegin();it!=m_drafts.cend();++it)names.append(foreign(it.key())?tr("%1 (previous workbook)").arg(it.value().name):it.value().name);
    QString saveable;
    if(m_drafts.contains(m_current) && !foreign(m_current))saveable=m_current;
    else for(auto it=m_drafts.cbegin();it!=m_drafts.cend();++it)if(!foreign(it.key())){saveable=it.key();break;}
    const bool canSave=editable() && !saveable.isEmpty();
    QMessageBox box(QMessageBox::Question,tr("Unsaved Python drafts"),
        tr("These Python modules have changes that are not saved to the workbook: %1").arg(names.join(", ")),
        (canSave?QMessageBox::Save:QMessageBox::NoButton)|QMessageBox::Discard|QMessageBox::Cancel,isVisible()?static_cast<QWidget *>(this):parentWidget());
    box.setObjectName("pythonDraftsPrompt");box.setOption(QMessageBox::Option::DontUseNativeDialog);box.setDefaultButton(QMessageBox::Cancel);
    box.setInformativeText(canSave?tr("Save stores one draft and keeps this window open. Discard drops every draft."):tr("Discard drops every draft."));
    const auto answer=box.exec();
    if(answer==QMessageBox::Discard){m_discardConfirmed=true;return true;}
    if(answer==QMessageBox::Save) {
        if(m_current!=saveable){m_current=saveable;m_currentMacro=-1;refreshList();}
        showWorkspace();save();
    }
    return false;
}

void PythonWorkspace::commitDiscard() {
    if(!m_discardConfirmed)return;
    m_discardConfirmed=false;m_drafts.clear();refreshList();
}

void PythonWorkspace::findNext() {
    const auto text=m_findField->text();if(text.isEmpty())return;
    if(!m_editor->find(text)) {
        auto cursor=m_editor->textCursor();cursor.movePosition(QTextCursor::Start);m_editor->setTextCursor(cursor);
        if(!m_editor->find(text)){showMessage(tr("No match for %1.").arg(text));return;}
    }
}
