// SPDX-License-Identifier: GPL-3.0-only
#pragma once

#include <QByteArray>
#include <QHash>
#include <QJsonObject>
#include <QList>
#include <QPointer>
#include <QRect>
#include <QSet>
#include <QTimer>
#include <QWidget>

class RestClient;
class FrozenPanes;
class QSvgRenderer;
class QTableView;

// One chart shown over the grid. View-only: mouse and keyboard pass through
// to the cells under it. Until an image arrives it shows a placeholder with
// the chart's name and why it is not drawn ("Loading" while one is awaited).
// The image is the service's SVG scaled to the widget, so a previous image
// stays, stretched, while one of a new size is rendered.
class ChartOverlay : public QWidget {
    Q_OBJECT
public:
    explicit ChartOverlay(const QString &chartId,QWidget *parent=nullptr);
    // False, keeping what is shown, when svg is empty or not a readable image.
    bool setSvg(const QByteArray &svg);
    void setPlaceholder(const QString &reason);
    bool hasImage() const { return !m_svg.isEmpty(); }
    // The text shown while there is no image: the name and the reason, or
    // "Loading" while no reason is set.
    QString placeholder() const;
    QString chartId() const { return property("chartId").toString(); }
    void setChart(const QJsonObject &chart);
    QJsonObject chart() const { return m_chart; }
    // The (state, width, height) whose render has settled: an image, a
    // failure, or a stale answer that was already retried. Empty when none.
    QString renderKey() const { return m_renderKey; }
    void setRenderKey(const QString &key) { m_renderKey=key; }
protected:
    void paintEvent(QPaintEvent *event) override;
private:
    QJsonObject m_chart;
    QByteArray m_svg;
    QSvgRenderer *m_renderer;
    QString m_reason,m_renderKey;
};

// Lists the charts of the bound sheet and keeps one overlay per chart placed
// over the cell where its anchor is shown: the frozen corner, top row or first
// column pane when the anchor row or column is frozen, otherwise the scrolling
// body. Overlays are hidden while the sheet is shown through a projected list
// view, and never cover the headers or scroll bars.
//
// Charts in view are rendered by service render jobs, one at a time (the
// service runs one chart job per workbook), lowest z first, after relayouts
// settle. A state change re-renders them; the previous image stays until the
// new one arrives. A render that failed is tried again when the window binds
// the same state again (Refresh). Every job started is released: when it
// ends, or after a cancel when its chart leaves view, the sheet, state or
// workbook changes, or the layer is destroyed.
class ChartLayer : public QObject {
    Q_OBJECT
public:
    // At most this many render jobs of the layer are open at once: the
    // service refuses a second chart job on a workbook (JOB_BUSY), so one
    // more would only cost a refused start. Another client's chart job still
    // takes the slot at times, so a refused start backs off and tries again.
    // A relayout checks the queue this long after the last one; a running
    // job is polled at this interval.
    static constexpr int MaxConcurrentRenders=1,RelayoutRenderDelayMs=150,PollIntervalMs=50;
    ChartLayer(RestClient *client,QTableView *body,FrozenPanes *frozen,QWidget *gridArea,QObject *parent);
    // workbookBase is "/v1/workbooks/{id}", empty when no workbook is open.
    // Binding the state already bound keeps the overlays and renders again
    // the charts whose render failed.
    void bind(const QString &workbookBase,const QString &sheet,const QString &state,bool projected);
    void relayout();
    ~ChartLayer() override;
    QList<ChartOverlay *> overlays() const;
signals:
    void listed(int count);
    // A chart's overlay now shows the service's image for the current state and size.
    void rendered(const QString &chartId);
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
    // One render job of this layer. A job still being started is marked
    // abandoned rather than dropped, so its answer can retire it.
    struct Job { QString id,chartId,base,sheet,state,key; quint64 clientGeneration=0; bool started=false,abandoned=false; };
    void renderQueued();
    void abandonUnwanted();
    void abandon(int index);
    void start(ChartOverlay *overlay);
    void poll(const QString &jobId);
    void answer(const QString &jobId,const QJsonObject &response,const QString &error);
    void fail(ChartOverlay *overlay,const QString &key,const QJsonObject &error,const QString &message);
    bool wanted(ChartOverlay *overlay) const;
    QString renderKey(ChartOverlay *overlay) const;
    ChartOverlay *overlayFor(const QString &chartId) const;
    int jobIndex(const QString &jobId) const;
    int jobForChart(const QString &chartId) const;
    // Cancels (when asked) and releases a job that is no longer the layer's;
    // a running job is polled until it stops, since it cannot be released.
    static void retire(QPointer<RestClient> client,const QString &path,bool cancel);
    bool attached() const { return m_body && m_frozen && m_gridArea; }
    // The client goes before the layer when the window is destroyed.
    QPointer<RestClient> m_client;
    // The grid widgets go before the layer when the window is destroyed.
    QPointer<QTableView> m_body;
    QPointer<FrozenPanes> m_frozen;
    QPointer<QWidget> m_gridArea;
    QString m_base,m_sheet,m_state;
    bool m_projected=false,m_queued=false,m_listed=false;
    quint64 m_generation=0;
    QList<QPointer<ChartOverlay>> m_overlays;
    QList<Job> m_jobs;
    // m_renderTimer debounces relayouts; m_busyTimer retries after the
    // service refused a start because its chart job slots were taken.
    QTimer m_renderTimer,m_busyTimer;
    bool m_busy=false;int m_busyDelay=PollIntervalMs;
    // The key a chart was already retried for after a stale answer.
    QHash<QString,QString> m_staleRetried;
    // Charts whose render settled as a failure; a bind of the same state
    // renders them again.
    QSet<QString> m_failed;
};
