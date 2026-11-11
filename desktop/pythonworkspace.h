// SPDX-License-Identifier: GPL-3.0-only
#pragma once
#include "restclient.h"
#include <QDeadlineTimer>
#include <QHash>
#include <QJsonArray>
#include <QJsonObject>
#include <QMainWindow>
#include <QMap>
#include <functional>

class CodeEditor;
class MacroDeclarationPanel;
class NamesPanel;
class QCheckBox;
class QComboBox;
class QFormLayout;
class QLabel;
class QLineEdit;
class QListWidget;
class QPlainTextEdit;
class QPushButton;
class QShortcut;
class QStackedWidget;
class QTabWidget;

// Separate top-level window for workbook Python. It reads the code catalog
// through REST and routes every write through submitRequested to the owning
// window's managed operation path; it never calls /operations itself.
// Editor contents are local drafts until Save. Nothing here imports or runs
// workbook code: Run asks the service to run approved, stored code through the
// same managed operation path, and the Output tab shows its receipt.
class PythonWorkspace : public QMainWindow {
    Q_OBJECT
public:
    explicit PythonWorkspace(RestClient *client,QWidget *owner);
    void bind(const QJsonObject &info);
    // Locked while the owning window is busy or an earlier operation's
    // outcome is unknown; unknown says which, so Run can explain itself.
    void setLocked(bool locked,bool unknown=false);
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
    // Asks before a change that clears the workbook's Undo history, honouring
    // the session's "Don't ask again" choice. The prompt is named
    // <name>Prompt and its check box <name>DontAsk.
    static bool confirmHistoryClear(QWidget *parent,const QJsonObject &info,const QString &text,const QString &name);
    // The Names page lists names and asks the owning window to change them.
    NamesPanel *namesPanel() const{return m_names;}
    void setNames(const QJsonArray &names);
    // Picking from the sheet: the owning window arms it on
    // pickFromSheetRequested(true), inserts through insertPickedReference while
    // pickStillValid holds, and reports the end through pickFinished.
    void insertPickedReference(const QString &reference);
    bool pickStillValid() const;
    void pickFinished(const QString &problem = {});
    // Review & approve: ownsService says whether the owning window started its
    // service; grant sends one exact activation key over its private channel
    // and returns false when nothing was sent.
    void setApprovalOwner(std::function<bool()> ownsService,std::function<bool(const QString &key)> grant){m_ownsService=std::move(ownsService);m_grant=std::move(grant);}
    // The service process stopped: its approvals are gone with it.
    void serviceStopped();
    // Checks one macro argument against its declared scalar type the way the
    // service does. Returns the problem, or an empty string with *value set.
    static QString argumentValue(const QJsonObject &parameter,const QString &text,QJsonValue *value);
    // A workbook-module frame of a traceback: index is its 0-based line in
    // the traceback text.
    struct Frame{int index=0;QString module;int line=0;};
    static QList<Frame> tracebackFrames(const QString &traceback);
    // The Output tab's text for a final run receipt; *tracebackLine is the
    // output line where the traceback starts, or -1 when there is none.
    static QString runOutputText(const QJsonObject &receipt,int *tracebackLine=nullptr);
signals:
    void submitRequested(QJsonObject operation);
    // A read found the workbook state stale; the owning window refreshes.
    void refreshRequested();
    void pickFromSheetRequested(bool on);
    void issue(QString message);
    // Asks the owning window to cancel the run this workspace has in flight.
    void cancelRunRequested(QString operation);
protected:
    void closeEvent(QCloseEvent *event) override;
    bool eventFilter(QObject *object,QEvent *event) override;
private:
    struct Draft {
        QString moduleId;  // empty for a module not yet stored
        QString name,source;
        QJsonArray macros;
        QString baseFingerprint;  // the reviewed catalog this draft is saved against
        QString workbookId;       // drafts never save into another workbook
        bool conflict=false;      // its own stored module changed since the base
    };
    enum class Pending {None,Save,Rename,Delete,Run};
    struct Argument {
        QJsonObject parameter;
        QLineEdit *field;
        QCheckBox *null;  // only for a nullable parameter
    };
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
    QString blockedReason() const;
    bool foreign(const QString &key) const;
    bool validName(const QString &name,QString *problem) const;
    bool nameTaken(const QString &name,const QString &except) const;
    QJsonObject storedModule(const QString &key) const;
    QJsonObject baseModule(const Draft &draft) const;
    Draft effective(const QString &key) const;
    QString describeChanges(const QJsonArray &before,const QJsonArray &after) const;
    bool hasWorkbookDrafts() const;
    QString approvalBlockedReason() const;
    void reviewApprove();
    void confirmAndGrant(const QJsonObject &shown);
    void pollApproval(const QJsonObject &shown,quint64 generation,QDeadlineTimer deadline,QString refreshedFor);
    void approvalFailed(const QString &text);
    QJsonArray catalogModules() const{return m_catalog["modules"].toArray();}
    void refreshMacroSelect();
    void loadArguments();
    void updateRunState();
    QString runBlockedReason() const;
    QJsonObject selectedMacro() const;
    void runMacro();
    void showRunReceipt(const QJsonObject &receipt);
    bool activateFrame(int outputLine);

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
    std::function<bool()> m_ownsService;
    std::function<bool(const QString &)> m_grant;
    // The last catalog this workspace approved per workbook in this
    // application session, only to show what changed since; never re-sent.
    QHash<QString,QJsonObject> m_approvedCatalogs;
    quint64 m_approvalGeneration=0;
    bool m_approvalInFlight=false;
    bool m_unknown=false;           // the owning window has an unresolved outcome
    bool m_runUnknown=false,m_cancelRequested=false;
    QJsonObject m_argumentsFor;     // the macro whose fields are shown
    QList<Argument> m_arguments;
    QHash<int,Frame> m_frames;      // runOutput line -> workbook frame of the shown traceback
    QString m_staleReviewFingerprint;  // a pre-approval read was stale; compared after the refresh
    inline static bool s_skipHistoryPrompt=false;

    QAction *m_save,*m_showMacro,*m_showNames,*m_find,*m_pick,*m_reviewApprove;
    QShortcut *m_pickEscape;
    QString m_pickModule;  // the module or draft that picking was armed for
    QListWidget *m_modules,*m_macros;
    QLabel *m_emptyHint,*m_status,*m_approval,*m_message,*m_conflict;
    QPushButton *m_newModule,*m_renameModule,*m_deleteModule,*m_discard,*m_addMacro,*m_removeMacro;
    CodeEditor *m_editor;
    QWidget *m_findBar;
    QLineEdit *m_findField;
    QStackedWidget *m_side;
    QWidget *m_macroPage,*m_namesPage;
    MacroDeclarationPanel *m_panel;
    NamesPanel *m_names;
    QTabWidget *m_tabs;
    QWidget *m_outputTab,*m_argumentsBox;
    QComboBox *m_macroSelect;
    QFormLayout *m_argumentsLayout;
    QPushButton *m_run,*m_cancelRun;
    QPlainTextEdit *m_output;
};
