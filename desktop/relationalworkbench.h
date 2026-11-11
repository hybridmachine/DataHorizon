// SPDX-License-Identifier: GPL-3.0-only
#pragma once
#include "restclient.h"
#include <QWidget>
#include <QJsonArray>

class QTreeWidget;
class QTableWidget;
class QLabel;
class QPushButton;

class RelationalWorkbench : public QWidget {
    Q_OBJECT
public:
    explicit RelationalWorkbench(RestClient *client,QWidget *parent=nullptr);
    void bind(const QJsonObject &info);
    void setLocked(bool locked);
    void refreshCatalog();
    static bool editDefinition(QWidget *parent,QJsonObject &definition,const QString &title);
signals:
    void submitRequested(QJsonObject operation);
    void issue(QString message);
    void sheetsRequested();
private:
    void createTable();
    void dropTable();
    void showTable(const QString &id);
    QString base()const;
    RestClient *m_client;
    QJsonObject m_info,m_catalog,m_draftDefinition;
    QString m_table;
    quint64 m_generation=0;
    bool m_locked=true;
    QTreeWidget *m_objects;
    QTableWidget *m_schema;
    QLabel *m_status;
    QPushButton *m_create,*m_drop;
};
