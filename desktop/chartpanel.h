// SPDX-License-Identifier: GPL-3.0-only
#pragma once

#include <QHash>
#include <QJsonObject>
#include <QString>
#include <QWidget>
#include <functional>

class QButtonGroup;
class QComboBox;
class QGroupBox;
class QLabel;
class QLineEdit;
class QPushButton;
class QStackedWidget;
class QToolButton;

// The side panel that edits one chart: its name and stacking order, type and
// grouping, title, axes and legend (the Data group is filled by a later task,
// in chartDataGroup between Title and Axes). It takes the catalog's chart in
// and asks for changes out; it sends nothing itself and knows no window.
//
// A choice (a type button, Grouping, Legend, Bring to Front, Send to Back)
// asks for its change when it changes; a text field on Enter or when it loses
// focus, and only when its text differs from the chart's stored value. Each
// request is one chart_update: the name, the order, or the whole input-form
// specification built from the stored chart. While a request is answered the
// panel asks for nothing else; the next setChart or refused ends the wait.
//
// A chart whose specification cannot be built (unreadable, or with a broken
// source) shows its reason at the top, and only the Chart group is enabled.
class ChartPanel : public QWidget {
    Q_OBJECT
public:
    explicit ChartPanel(QWidget *parent=nullptr);
    // The chart to show; {} shows "No chart selected". The same chart again
    // changes nothing, so a field being edited keeps its text; any other
    // chart reloads every field and drops unsent text.
    void setChart(const QJsonObject &chart);
    QString chartId() const;
    // Busy or read-only: every control disabled, values kept.
    void setLocked(bool locked);
    // Puts the named field back to the chart's value and shows the message
    // beneath it until the chart changes or the field is edited again. An
    // empty message only puts the field back.
    void refused(const QString &field,const QString &message);
signals:
    // changes holds exactly the chart_update fields: name, order or spec.
    // field names the control (chartName, chartOrder, chartType,
    // chartGrouping, chartTitle, categoryAxisTitle, valueAxisTitle,
    // valueAxisMin, valueAxisMax, legendPosition).
    void changeRequested(const QString &chartId,const QJsonObject &changes,const QString &field);
    // A range pick for a source field (the Data group, later).
    void pickRequested(const QString &field);
private:
    void load();
    void loadField(const QString &field);
    void updateEnabled();
    void showMessage(const QString &field,const QString &message);
    void clearMessage(const QString &field);
    bool canRequest() const;
    void request(const QJsonObject &changes,const QString &field);
    // The stored spec with one change, sent whole in input form.
    void requestSpec(const QString &field,const std::function<void(QJsonObject &)> &change);
    void applyName();
    void applyText(const QString &field);
    void applyBound(const QString &field);
    void applyType(const QString &type);
    QLabel *messageLabel(const QString &field);
    QJsonObject m_chart;
    // The input-form spec; empty when it cannot be built (m_reason says why).
    QJsonObject m_spec;
    QString m_reason;
    bool m_locked=false,m_pending=false;
    QStackedWidget *m_pages=nullptr;
    QLabel *m_reasonLabel=nullptr;
    QGroupBox *m_chartGroup=nullptr,*m_typeGroup=nullptr,*m_titleGroup=nullptr,*m_axesGroup=nullptr,*m_legendGroup=nullptr;
    QWidget *m_dataGroup=nullptr;
    QLineEdit *m_name=nullptr,*m_title=nullptr,*m_categoryTitle=nullptr,*m_valueTitle=nullptr,*m_min=nullptr,*m_max=nullptr;
    QPushButton *m_front=nullptr,*m_back=nullptr;
    QButtonGroup *m_types=nullptr;
    QHash<QString,QToolButton *> m_typeButtons;
    QComboBox *m_grouping=nullptr,*m_legend=nullptr;
    QHash<QString,QLabel *> m_messages;
};
