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
    // The text shown while there is no image: the name and the reason, or
    // "Loading" while no reason is set.
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
    // The catalog read failed; the next bind of the same sheet and state reads again.
    void failed(const QString &message);
    // The workbook changed while the catalog was read.
    void stale();
protected:
    bool eventFilter(QObject *object,QEvent *event) override;
private:
    void list();
    void accept(const QJsonArray &charts);
    void clear();
    void restack();
    void queueRelayout();
    // Why a chart cannot be drawn; empty for a readable chart.
    static QString reasonFor(const QJsonObject &chart);
    bool attached() const { return m_body && m_frozen && m_gridArea; }
    RestClient *m_client;
    // The grid widgets go before the layer when the window is destroyed.
    QPointer<QTableView> m_body;
    QPointer<FrozenPanes> m_frozen;
    QPointer<QWidget> m_gridArea;
    QString m_base,m_sheet,m_state;
    bool m_projected=false,m_queued=false,m_listed=false;
    quint64 m_generation=0;
    QList<QPointer<ChartOverlay>> m_overlays;
};
