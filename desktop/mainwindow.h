// SPDX-License-Identifier: GPL-3.0-only
#pragma once

#include "restclient.h"
#include "sheetmodel.h"
#include <QMainWindow>
#include <QProcess>

class QTableView;
class QLineEdit;
class QTabBar;
class QLabel;
class QPlainTextEdit;
class QToolBar;

struct StartupOptions { QString url, token, handler, recoveryDirectory, initialWorkbook; bool automaticWorkbook = true; };

class MainWindow : public QMainWindow {
    Q_OBJECT
public:
    explicit MainWindow(const StartupOptions &options = {}, QWidget *parent = nullptr);
    ~MainWindow() override;
    void openBook(const QString &path, bool create = false, bool readonly = false);
    void submit(QJsonObject operation);
    void importCsvText(const QString &text);
    void exportCsvTo(const QString &path);
    void showList(QJsonObject scope);
    static bool isRecoveryPath(QString path);
    SheetModel *model() const { return m_model; }
signals:
    void serviceReady();
    void workbookReady();
    void operationFinished(bool committed);
protected:
    void closeEvent(QCloseEvent *event) override;
    bool eventFilter(QObject *object, QEvent *event) override;
private:
    void startService();
    void buildUi();
    void newBook();
    void refresh(const QString &selectSheet = {});
    void acceptInfo(const QJsonObject &info, const QString &selectSheet = {});
    void issue(const QString &message);
    void setBusy(bool busy, const QString &message = {});
    void reconcile();
    void saveCopy(bool firstSave = false);
    void save(bool copy);
    void copy(bool cut = false);
    void paste(bool valuesOnly = false, bool formatsOnly = false);
    void format(const QJsonObject &style);
    void importCsv();
    void exportCsv();
    void listDialog();
    void applyList();
    void findDialog(bool replace);
    void changeSheet(int index);
    void syncSource();
    bool resolveDraft();
    void submitDraft();
    void restartService();
    QString recoveryDirectory() const;
    bool preserveUnknownOperation();
    void clearRecoveryRecord();
    void restoreRecoveredDraft();
    void clearRecoveredDraft();
    QJsonObject selectedRange() const;
    QString base() const;
    StartupOptions m_options;
    RestClient *m_client;
    SheetModel *m_model;
    QProcess m_service;
    QByteArray m_startup;
    QTableView *m_grid = nullptr;
    QLineEdit *m_address = nullptr, *m_source = nullptr;
    QTabBar *m_tabs = nullptr;
    QLabel *m_status = nullptr;
    QPlainTextEdit *m_issues = nullptr;
    QToolBar *m_toolbar = nullptr;
    QAction *m_undo = nullptr, *m_redo = nullptr;
    QJsonObject m_info;
    QJsonObject m_listSpec;
    QJsonObject m_draftTarget;
    QJsonObject m_recoveredDraft;
    QString m_draftSubmissionId;
    bool m_restoreDraft = false, m_draftNeedsReview = false;
    QString m_pendingId;
    QString m_restartPath, m_recoveryRecord;
    bool m_busy = true, m_unknown = false, m_refreshing = false;
    int m_saveMode = 0;
    bool m_releasing = false, m_released = false;
    bool m_shuttingDown = false;
};
