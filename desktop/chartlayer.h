// SPDX-License-Identifier: GPL-3.0-only
#pragma once

#include <QByteArray>
#include <QHash>
#include <QJsonObject>
#include <QList>
#include <QPointer>
#include <QRect>
#include <QWidget>

class RestClient;
class FrozenPanes;
class QTableView;

// One chart shown over the grid. View-only: mouse and keyboard pass through
// to the cells under it. Until an image arrives it shows a placeholder with
// the chart's name and why it is not drawn ("Loading" while one is awaited).
class ChartOverlay : public QWidget {
    Q_OBJECT
public:
    explicit ChartOverlay(const QString &chartId,QWidget *parent=nullptr);
    void setSvg(const QByteArray &svg);
    void setPlaceholder(const QString &reason);
    bool hasImage() const { return !m_svg.isEmpty(); }
    // The text shown while there is no image: the name and the reason.
    QString placeholder() const;
    QString chartId() const { return property("chartId").toString(); }
    void setChart(const QJsonObject &chart);
    QJsonObject chart() const { return m_chart; }
protected:
    void paintEvent(QPaintEvent *event) override;
private:
    QJsonObject m_chart;
    QByteArray m_svg;
    QString m_reason;
};

// Lists the charts of the bound sheet and keeps one overlay per chart placed
// over the cell where its anchor is shown: the frozen corner, top row or first
// column pane when the anchor row or column is frozen, otherwise the scrolling
// body. Overlays are hidden while the sheet is shown through a projected list
// view, and never cover the headers or scroll bars.
class ChartLayer : public QObject {
    Q_OBJECT
public:
    ChartLayer(RestClient *client,QTableView *body,FrozenPanes *frozen,QWidget *gridArea,QObject *parent);
    // workbookBase is "/v1/workbooks/{id}", empty when no workbook is open.
    void bind(const QString &workbookBase,const QString &sheet,const QString &state,bool projected);
    void relayout();
    QList<ChartOverlay *> overlays() const;
signals:
    void listed(int count);
protected:
    bool eventFilter(QObject *object,QEvent *event) override;
private:
    void list();
    void accept(const QJsonArray &charts);
    void clear();
    void restack();
    void queueRelayout();
    static QString reasonFor(const QJsonObject &chart);
    RestClient *m_client;
    QTableView *m_body;
    FrozenPanes *m_frozen;
    QWidget *m_gridArea;
    QString m_base,m_sheet,m_state;
    bool m_projected=false,m_queued=false;
    quint64 m_generation=0;
    QList<QPointer<ChartOverlay>> m_overlays;
};
