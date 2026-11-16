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
class QKeyEvent;
class QSvgRenderer;
class QTableView;

// One chart shown over the grid. It takes the mouse over its own rectangle
// and focus on a click; ChartLayer handles its events (selection, Escape,
// Enter, the wheel). Its accessible name is "Chart: <name>" and its
// description the chart's type, title and series count, or the reason it
// cannot be read or drawn. Until an image arrives it shows a placeholder with
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
    // A selected chart paints a 2 px frame in the palette's highlight colour
    // and eight 8 px handles at its corners and edge midpoints, all inside
    // its own rectangle, over the image or placeholder.
    void setSelected(bool selected);
    bool selected() const { return m_selected; }
    static constexpr int HandleSize=8;
    // The handle under a point of the widget, numbered as ChartSpec::resized
    // numbers them (0 top-left, clockwise); -1 elsewhere or when not selected.
    int handleAt(const QPoint &position) const;
protected:
    void paintEvent(QPaintEvent *event) override;
private:
    void describe();
    QJsonObject m_chart;
    QByteArray m_svg;
    QSvgRenderer *m_renderer;
    QString m_reason,m_renderKey;
    bool m_selected=false;
};

// Lists the charts of the bound sheet and keeps one overlay per chart placed
// over the cell where its anchor is shown: the frozen corner, top row or first
// column pane when the anchor row or column is frozen, otherwise the scrolling
// body. Overlays are hidden while the sheet is shown through a projected list
// view, and never cover the headers or scroll bars.
//
// At most one chart is selected. A press on an overlay selects it and focuses
// it; a press on any grid pane or header (except while the selection is
// kept), or Escape on the overlay, clears the selection, and the grid takes
// back any focus a chart had. The selection
// survives a relist while its chart exists, and clears when the chart goes,
// another sheet or workbook is bound, or the sheet is shown projected. The
// wheel over an overlay scrolls the body. A chart holds the keyboard only
// while its overlay is shown: when a focused overlay is hidden (scrolled or
// moved out of view) the chart is deselected, and when one is hidden or
// deleted the grid takes the focus, never another control.
//
// While interactive, a selected chart is moved by dragging it and resized by
// dragging one of its handles: the overlay follows the pointer and the
// release asks for one change (moveRequested, resizeRequested), or none when
// nothing moved or no cell is under the chart's top-left corner. Escape
// cancels a drag. Its keys, taken before the window's shortcuts so they never
// act on the cells beneath: the arrows move it, Shift with them
// resizes it, Delete or Backspace asks for its deletion, Enter or Return for
// its editing; the menu key or Shift+F10, and a right-click, ask for its
// context menu. The layer sends nothing itself: the window sends each request
// as one operation and the relist that follows places the chart. A keyboard
// move that would leave nothing of the chart in view is refused. After a
// chart is deleted with Delete or Backspace, auto-repeated presses of that
// key are swallowed until it is released, so a held key never clears the
// cells the grid shows once the chart is gone.
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
    // True when the charts shown are the catalog listed for this workbook,
    // sheet and state: false from a bind of another key, or a failed or
    // stale read, until that key's listing arrives.
    bool listedFor(const QString &workbookBase,const QString &sheet,const QString &state) const {
        return m_current && workbookBase==m_base && sheet==m_sheet && state==m_state;
    }
    void relayout();
    // Called as a model reset begins: the view shows hidden rows and default
    // sizes until it is restored, so nothing is placed or hidden until then.
    void holdLayout(){m_held=true;}
    ~ChartLayer() override;
    QList<ChartOverlay *> overlays() const;
    // The selected chart's id; empty when none is.
    QString selectedChart() const { return m_selected; }
    // Selects the chart; an empty id, or one not on the sheet, clears.
    void select(const QString &chartId);
    // Selects and focuses the next (+1) or previous (-1) chart in z order,
    // wrapping; with none selected, the lowest (+1) or highest (-1). A chart
    // scrolled out of view is scrolled into view first; one that cannot be
    // shown at all (its anchor past the last shown row or column) is
    // skipped, and when no chart can be shown nothing is selected and
    // refused() gives the status.
    void selectNext(int direction);
    // Scrolls the chart into view when it is out of view, then selects and
    // focuses it; false, selecting nothing, when it cannot be shown.
    bool focusChart(const QString &chartId);
    // The catalog's chart and its overlay; empty and null when not on the sheet.
    QJsonObject chart(const QString &chartId) const;
    ChartOverlay *overlay(const QString &chartId) const;
    // False while busy, read-only or projected: overlays still select on a
    // click but take no keys other than Escape.
    void setInteractive(bool interactive);
    // True while the window picks cells (a formula reference pick, the format
    // painter, a Python pick, a chart range pick): overlays let the mouse
    // through to the cells under them and take no focus, as the view-only
    // layer did.
    void setPassThrough(bool passThrough);
    // True during a chart range pick only: a press on the grid keeps the selection.
    void setKeepSelection(bool keep) { m_keepSelection=keep; }
    // The inverse of relayout(), so a chart stays where it was dropped: the cell under the rectangle's
    // top-left corner in the pane under it (frozen corner, frozen rows, frozen
    // columns or body), as 1-based {row, col}, and {x, y} of the corner inside
    // that cell. False when no cell is there: a header, outside the grid, past
    // the last row or column.
    bool placement(const QRect &rectInGridArea,QJsonObject *anchor,QJsonObject *offset) const;
public slots:
    // Requests for the selected chart, as its keys make them; nothing without
    // a selection, and no order or delete while not interactive.
    void requestEdit();
    void requestOrder(const QString &order);
    void requestDelete();
signals:
    void listed(int count);
    // A chart's overlay now shows the service's image for the current state and size.
    void rendered(const QString &chartId);
    // The catalog read failed; the next bind of the same sheet and state reads again.
    void failed(const QString &message);
    // The workbook changed while the catalog was read.
    void stale();
    // The selection changed; empty when it was cleared.
    void selectionChanged(const QString &chartId);
    // A double-click or Enter on a chart.
    void editRequested(const QString &chartId);
    void moveRequested(const QString &chartId,const QJsonObject &anchor,const QJsonObject &offset);
    void resizeRequested(const QString &chartId,const QJsonObject &anchor,const QJsonObject &offset,const QJsonObject &size);
    void deleteRequested(const QString &chartId);
    // order is "front" or "back".
    void orderRequested(const QString &chartId,const QString &order);
    void contextMenuRequested(const QString &chartId,const QPoint &globalPosition);
    // A keyboard move or resize refused in the window; status is the text to show.
    void refused(const QString &status);
protected:
    bool eventFilter(QObject *object,QEvent *event) override;
private:
    void list();
    void accept(const QJsonArray &charts);
    void clear();
    void restack();
    void queueRelayout();
    bool m_held=false;
    bool overlayEvent(ChartOverlay *overlay,QEvent *event);
    bool keyEvent(ChartOverlay *overlay,QKeyEvent *key);
    // One arrow key on a selected chart: a move, or with Shift a resize.
    void nudge(ChartOverlay *overlay,int key,Qt::KeyboardModifiers modifiers);
    // The pane showing a 0-based cell, and a viewport's rectangle in the grid area.
    QTableView *paneOf(int row,int col) const;
    QRect paneArea(QWidget *viewport) const;
    // The cells of every pane together: no header and no scroll bar.
    QRect cellsArea() const;
    // The chart's whole rectangle in the grid area (its anchor cell's position
    // plus its offset, however much of it is clipped) and the pane showing
    // the anchor; false when the anchor is not shown.
    bool trueRect(ChartOverlay *overlay,QRect *rect,QTableView **pane) const;
    // The 0-based cell showing the chart's anchor: its own, or the next shown
    // row and column after hidden ones; false when there is none.
    bool anchorCell(ChartOverlay *overlay,int *row,int *col) const;
    // Scrolls the body so a chart out of view shows its anchor; false when it
    // still shows nothing.
    bool reveal(ChartOverlay *overlay);
    // placement() within one pane's cells, including those scrolled out of view.
    bool placementIn(QTableView *view,const QPoint &point,QJsonObject *anchor,QJsonObject *offset) const;
    // A mouse drag: pending until the pointer moves past the drag distance,
    // then active. handle is -1 for a move.
    struct Drag { QPointer<ChartOverlay> overlay; int handle=-1; QPoint origin; QRect start; bool active=false; };
    bool dragging(ChartOverlay *overlay) const { return m_drag.active && m_drag.overlay && m_drag.overlay==overlay; }
    QRect dragRect(const QPoint &globalPosition) const;
    void dragTo(const QPoint &globalPosition);
    void finishDrag(const QPoint &globalPosition);
    // Ends any drag; an active one's chart goes back to its stored place.
    void cancelDrag();
    void updateCursor(ChartOverlay *overlay,int handle) const;
    void applyPassThrough(ChartOverlay *overlay) const;
    // Gives the grid the focus when a chart holds it.
    void refocusGrid();
    QList<ChartOverlay *> zOrdered() const;
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
    int jobIndex(const QString &jobId) const;
    int jobForChart(const QString &chartId) const;
    // Cancels (when asked) and releases a job that is no longer the layer's;
    // a running job is polled until it stops, since it cannot be released.
    static void retire(QPointer<RestClient> client,const QString &path,bool cancel);
    // Defined in the source file: testing m_frozen needs the complete FrozenPanes type.
    bool attached() const;
    // The client goes before the layer when the window is destroyed.
    QPointer<RestClient> m_client;
    // The grid widgets go before the layer when the window is destroyed.
    QPointer<QTableView> m_body;
    QPointer<FrozenPanes> m_frozen;
    QPointer<QWidget> m_gridArea;
    QString m_base,m_sheet,m_state;
    bool m_projected=false,m_queued=false,m_listed=false,m_current=false,m_interactive=true,m_passThrough=false,m_keepSelection=false;
    QString m_selected;
    Drag m_drag;
    // The viewports of every grid pane and header: a press there deselects.
    QList<QPointer<QWidget>> m_panes;
    quint64 m_generation=0;
    QList<QPointer<ChartOverlay>> m_overlays;
    QList<Job> m_jobs;
    // m_renderTimer debounces relayouts; m_busyTimer retries after the
    // service refused a start because its chart job slots were taken.
    QTimer m_renderTimer,m_busyTimer;
    bool m_busy=false;int m_busyDelay=PollIntervalMs;
    // A chart was deleted with this key (Delete or Backspace) and it is still held.
    int m_swallowRepeat=0;
    // The key a chart was already retried for after a stale answer.
    QHash<QString,QString> m_staleRetried;
    // Charts whose render settled as a failure; a bind of the same state
    // renders them again.
    QSet<QString> m_failed;
};
