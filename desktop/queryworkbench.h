// SPDX-License-Identifier: GPL-3.0-only
#pragma once
#include "restclient.h"
#include <QWidget>
#include <QJsonArray>
#include <QPointer>
#include <functional>
class QPlainTextEdit;class QTableWidget;class QTreeWidget;class QPushButton;class QLabel;class RecordInputDialog;
class QueryWorkbench : public QWidget {
    Q_OBJECT
public:
    explicit QueryWorkbench(RestClient *client,QWidget *parent=nullptr);
    void bind(const QJsonObject &info);
    void setCatalog(const QJsonObject &catalog);
    void setLocked(bool locked);
    void suspendView();
    void resumeView();
    bool hasActiveJob()const{return !m_job.isEmpty() || m_startPending || m_closing;}
    void closeJob(std::function<void(QString)> completion);
    QJsonObject cacheMetrics()const;
    QJsonObject definition()const;
    void setDefinition(const QJsonObject &definition);
    QString completedJob()const{return m_result.isEmpty()?QString{}:m_job;}
signals:
    void issue(QString message);
    void submitRequested(QJsonObject operation);
private:
    QString base()const;
    void addParameter(const QJsonObject &parameter={});
    void run();
    void start(QJsonObject request,QPointer<RecordInputDialog> editor={});
    void poll();
    void terminal(const QJsonObject &status);
    void page(int offset=0);
    void chooseColumns();
    void copyValues();
    void cancel();
    void releaseForClose();
    void buttons();
    void draftChanged();
    RestClient *m_client;QJsonObject m_info,m_catalog,m_result,m_page,m_values;
    QJsonArray m_projection;QString m_job;quint64 m_generation=0,m_pageGeneration=0;
    quint64 m_editorRevision=0,m_runRevision=0;
    QPointer<RecordInputDialog> m_parameterEditor;
    bool m_locked=true,m_running=false,m_startPending=false,m_closing=false;
    std::function<void(QString)> m_closeCompletion;
    QPlainTextEdit *m_sql;QTableWidget *m_parameters,*m_grid;QTreeWidget *m_schema;
    QPushButton *m_run,*m_cancel,*m_previous,*m_next,*m_columns,*m_copy;
    QLabel *m_status,*m_source;QList<int> m_previousPages;
};
