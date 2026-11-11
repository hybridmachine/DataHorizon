// SPDX-License-Identifier: GPL-3.0-only
#pragma once
#include "restclient.h"
#include <QHash>
#include <QJsonArray>
#include <QJsonObject>
#include <QMainWindow>
#include <QMap>

class CodeEditor;
class MacroDeclarationPanel;
class QLabel;
class QLineEdit;
class QListWidget;
class QPushButton;
class QStackedWidget;

// Separate top-level window for workbook Python. It reads the code catalog
// through REST and routes every write through submitRequested to the owning
// window's managed operation path; it never calls /operations itself.
// Editor contents are local drafts until Save. Nothing here imports or runs
// workbook code.
class PythonWorkspace : public QMainWindow {
    Q_OBJECT
public:
    explicit PythonWorkspace(RestClient *client,QWidget *owner);
    void bind(const QJsonObject &info);
    void setLocked(bool locked);
    // Save/Discard/Cancel for unsaved drafts. False means the caller stays
    // open: Cancel, or Save started (its outcome arrives as a receipt).
    // True after Discard only records the decision: the caller applies it with
    // commitDiscard() at its point of no return, or keepDrafts() if it stops.
    bool resolveDrafts();
    void commitDiscard();
    void keepDrafts(){m_discardConfirmed=false;}
    bool hasDrafts() const{return !m_drafts.isEmpty();}
    void setViewStatePath(const QString &path){m_viewStatePath=path;}
    void showWorkspace();
    // The fingerprint of the catalog currently shown; empty until it is read
    // from the current service handle.
    QString fingerprint() const;
    void operationAdmitted(const QString &operation);
    void operationReceipt(const QJsonObject &receipt);
    // The "Don't ask again this session" choices last until the application
    // exits; tests reset them between cases.
    static void resetSessionChoices(){s_skipHistoryPrompt=false;}
signals:
    void submitRequested(QJsonObject operation);
    void issue(QString message);
protected:
    void closeEvent(QCloseEvent *event) override;
private:
    struct Draft {
        QString moduleId;  // empty for a module not yet stored
        QString name,source;
        QJsonArray macros;
        QString baseFingerprint;  // the reviewed catalog this draft is saved against
        QString workbookId;       // drafts never save into another workbook
        bool conflict=false;      // its own stored module changed since the base
    };
    enum class Pending {None,Save,Rename,Delete};
    void readCatalog();
    void applyCatalog(const QJsonObject &catalog);
    void refreshList();
    void loadCurrent();
    void loadMacros();
    void updateState();
    void updateDraft(const QString &source,const QJsonArray &macros);
    void newModule();
    void renameModule();
    void deleteModule();
    void discardDraft();
    void addMacro();
    void removeMacro();
    void save();
    bool resolveConflict();
    bool confirmHistory();
    void startOperation(QJsonObject request,Pending kind,const QString &key,const QString &name,const QString &base);
    void resetPending();
    void findNext();
    void showMessage(const QString &text);
    bool editable() const;
    bool foreign(const QString &key) const;
    bool validName(const QString &name,QString *problem) const;
    bool nameTaken(const QString &name,const QString &except) const;
    QJsonObject storedModule(const QString &key) const;
    QJsonObject baseModule(const Draft &draft) const;
    Draft effective(const QString &key) const;
    QString describeChanges(const QJsonArray &before,const QJsonArray &after) const;
    QJsonArray catalogModules() const{return m_catalog["modules"].toArray();}

    RestClient *m_client;
    QJsonObject m_info,m_catalog;
    bool m_catalogReady=false;
    QHash<QString,QJsonArray> m_catalogs;  // modules by fingerprint, for "what changed"
    QMap<QString,Draft> m_drafts;          // keyed by module id, or "new:<uuid>"
    QString m_workbookId,m_current;
    int m_currentMacro=-1;
    bool m_locked=true,m_loading=false,m_awaitingAdmission=false,m_reviewChanges=false,m_geometryRestored=false,m_discardConfirmed=false;
    Pending m_pending=Pending::None;
    QString m_pendingOperation,m_pendingKey,m_pendingName,m_pendingBase;
    QString m_rebaseFrom,m_rebaseTo;
    quint64 m_readGeneration=0;
    QString m_viewStatePath;
    inline static bool s_skipHistoryPrompt=false;

    QAction *m_save,*m_showMacro,*m_find;
    QListWidget *m_modules,*m_macros;
    QLabel *m_emptyHint,*m_status,*m_approval,*m_message,*m_conflict;
    QPushButton *m_newModule,*m_renameModule,*m_deleteModule,*m_discard,*m_addMacro,*m_removeMacro;
    CodeEditor *m_editor;
    QWidget *m_findBar;
    QLineEdit *m_findField;
    QStackedWidget *m_side;
    MacroDeclarationPanel *m_panel;
};
