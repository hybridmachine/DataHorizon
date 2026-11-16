// SPDX-License-Identifier: GPL-3.0-only
#include "chartlayer.h"
#include "chartspec.h"
#include "frozenpanes.h"
#include "restclient.h"
#include <QApplication>
#include <QCoreApplication>
#include <QEvent>
#include <QHeaderView>
#include <QKeyEvent>
#include <QMouseEvent>
#include <QJsonArray>
#include <QPainter>
#include <QScrollBar>
#include <QSvgRenderer>
#include <QTableView>
#include <QTimer>
#include <QUuid>
#include <QWheelEvent>
#include <algorithm>
#include <utility>

ChartOverlay::ChartOverlay(const QString &chartId,QWidget *parent):QWidget(parent),m_renderer(new QSvgRenderer(this)) {
    setObjectName("chartOverlay");setProperty("chartId",chartId);
    setFocusPolicy(Qt::ClickFocus);
    // Hovering a selected chart shows the move or resize cursor.
    setMouseTracking(true);
}

void ChartOverlay::setChart(const QJsonObject &chart) {
    m_chart=chart;setAccessibleName(tr("Chart: %1").arg(chart["name"].toString()));describe();update();
}

bool ChartOverlay::setSvg(const QByteArray &svg) {
    if(svg.isEmpty() || !QSvgRenderer(svg).isValid())return false;
    m_svg=svg;m_renderer->load(svg);describe();update();return true;
}

void ChartOverlay::setPlaceholder(const QString &reason) {
    m_svg.clear();m_reason=reason;describe();update();
}

void ChartOverlay::setSelected(bool selected) {
    if(m_selected==selected)return;
    m_selected=selected;update();
}

int ChartOverlay::handleAt(const QPoint &position) const {
    if(!m_selected)return -1;
    const int xs[]{0,(width()-HandleSize)/2,width()-HandleSize},ys[]{0,(height()-HandleSize)/2,height()-HandleSize};
    // Column and row of each handle, clockwise from the top-left.
    static constexpr int places[8][2]{{0,0},{1,0},{2,0},{2,1},{2,2},{1,2},{0,2},{0,1}};
    for(int handle=0;handle<8;++handle)
        if(QRect(xs[places[handle][0]],ys[places[handle][1]],HandleSize,HandleSize).contains(position))return handle;
    return -1;
}

// "<Type> chart, titled <title>, <n> series", or why the chart cannot be read
// or drawn: the catalog's reason, else the reason its placeholder shows.
void ChartOverlay::describe() {
    auto reason=ChartSpec::stateReason(m_chart);
    if(reason.isEmpty() && !hasImage())reason=m_reason;
    if(!reason.isEmpty()){setAccessibleDescription(reason);return;}
    const auto spec=m_chart["spec"].toObject();
    auto type=spec["type"].toString();
    if(!type.isEmpty())type[0]=type[0].toUpper();
    const auto title=spec["title"].toObject()["text"].toString();
    const auto count=spec["series"].toArray().size();
    const auto series=tr("%1 series").arg(count);  // "1 series", "2 series"
    setAccessibleDescription(title.isEmpty()?tr("%1 chart, %2").arg(type,series):tr("%1 chart, titled %2, %3").arg(type,title,series));
}

QString ChartOverlay::placeholder() const {
    return hasImage()?QString():m_chart["name"].toString()+"\n"+(m_reason.isEmpty()?tr("Loading"):m_reason);
}

void ChartOverlay::paintEvent(QPaintEvent *) {
    QPainter painter(this);
    painter.fillRect(rect(),palette().base());
    if(hasImage())m_renderer->render(&painter,QRectF(rect()));
    else {
        painter.setPen(palette().color(QPalette::Mid));painter.drawRect(rect().adjusted(0,0,-1,-1));
        painter.setPen(palette().color(QPalette::Text));
        painter.drawText(rect().adjusted(8,8,-8,-8),Qt::AlignCenter|Qt::TextWordWrap,placeholder());
    }
    if(!m_selected)return;
    // A 2 px frame and eight 8 px handles, inside the rectangle, so a selected chart covers no more cells.
    const auto highlight=palette().highlight();const int w=width(),h=height(),frame=2,handle=HandleSize;
    painter.fillRect(0,0,w,frame,highlight);painter.fillRect(0,h-frame,w,frame,highlight);
    painter.fillRect(0,0,frame,h,highlight);painter.fillRect(w-frame,0,frame,h,highlight);
    for(const int x:{0,(w-handle)/2,w-handle})for(const int y:{0,(h-handle)/2,h-handle})
        if(x!=(w-handle)/2 || y!=(h-handle)/2)painter.fillRect(x,y,handle,handle,highlight);
}

ChartLayer::ChartLayer(RestClient *client,QTableView *body,FrozenPanes *frozen,QWidget *gridArea,QObject *parent)
    :QObject(parent),m_client(client),m_body(body),m_frozen(frozen),m_gridArea(gridArea) {
    setObjectName("chartLayer");
    connect(body->horizontalScrollBar(),&QScrollBar::valueChanged,this,&ChartLayer::relayout);
    connect(body->verticalScrollBar(),&QScrollBar::valueChanged,this,&ChartLayer::relayout);
    QList<QTableView *> views{body};views+=frozen->views();
    for(auto *view:views) {
        for(auto *header:{view->horizontalHeader(),view->verticalHeader()}) {
            connect(header,&QHeaderView::sectionResized,this,&ChartLayer::relayout);
            connect(header,&QHeaderView::geometriesChanged,this,&ChartLayer::relayout);
        }
        view->installEventFilter(this);view->viewport()->installEventFilter(this);
        m_panes.append(view->viewport());
        for(auto *header:{view->horizontalHeader(),view->verticalHeader()}){header->viewport()->installEventFilter(this);m_panes.append(header->viewport());}
    }
    auto *model=body->model();
    connect(model,&QAbstractItemModel::modelReset,this,&ChartLayer::queueRelayout);
    connect(model,&QAbstractItemModel::layoutChanged,this,&ChartLayer::queueRelayout);
    connect(model,&QAbstractItemModel::rowsInserted,this,&ChartLayer::queueRelayout);
    connect(model,&QAbstractItemModel::rowsRemoved,this,&ChartLayer::queueRelayout);
    connect(model,&QAbstractItemModel::columnsInserted,this,&ChartLayer::queueRelayout);
    connect(model,&QAbstractItemModel::columnsRemoved,this,&ChartLayer::queueRelayout);
    for(QWidget *widget:{gridArea,static_cast<QWidget *>(frozen),frozen->leftWidget()})widget->installEventFilter(this);
    m_renderTimer.setSingleShot(true);m_renderTimer.setInterval(RelayoutRenderDelayMs);
    connect(&m_renderTimer,&QTimer::timeout,this,&ChartLayer::renderQueued);
    m_busyTimer.setSingleShot(true);
    connect(&m_busyTimer,&QTimer::timeout,this,[this]{m_busy=false;renderQueued();});
}

// A job still being started is retired by its start answer.
bool ChartLayer::attached() const { return m_body && m_frozen && m_gridArea; }

ChartLayer::~ChartLayer() {
    for(const auto &job:std::as_const(m_jobs))
        if(job.started && m_client && job.clientGeneration==m_client->generation())retire(m_client,job.base+"/charts/jobs/"+job.id,true);
}

QList<ChartOverlay *> ChartLayer::overlays() const {
    QList<ChartOverlay *> result;
    for(const auto &overlay:m_overlays)if(overlay)result.append(overlay);
    return result;
}

void ChartLayer::bind(const QString &workbookBase,const QString &sheet,const QString &state,bool projected) {
    if(m_listed && workbookBase==m_base && sheet==m_sheet && state==m_state) {
        // The window refreshed without a change: charts whose render failed
        // (a lost answer, say) are rendered again; one that fails because of
        // its own data simply fails again.
        bool retry=false;
        for(const auto &chartId:std::as_const(m_failed))
            if(auto *item=overlay(chartId)){item->setRenderKey({});retry=true;}
        m_failed.clear();
        if(projected!=m_projected || retry){m_projected=projected;if(m_projected){select({});refocusGrid();}relayout();}
        return;
    }
    const bool sameSheet=workbookBase==m_base && sheet==m_sheet;
    m_base=workbookBase;m_sheet=sheet;m_state=state;m_projected=projected;m_listed=true;m_current=false;
    // Another sheet's charts never stay on screen while this one's are read;
    // a new state of the same sheet keeps the overlays until the list lands.
    if(!sameSheet)clear();else ++m_generation;
    if(m_projected){select({});refocusGrid();}
    // Renders of another sheet, workbook or state are never shown.
    abandonUnwanted();
    if(!m_base.isEmpty() && !m_sheet.isEmpty() && !m_state.isEmpty())list();
    relayout();
}

void ChartLayer::list() {
    const auto generation=++m_generation;const auto clientGeneration=m_client->generation();
    const auto base=m_base,sheet=m_sheet,state=m_state;QPointer<ChartLayer> self(this);
    m_client->request(base+"/charts",{{"expected_state",state},{"sheet",sheet}},[self,generation,clientGeneration,base,sheet,state](QJsonObject result,QString error){
        if(!self || generation!=self->m_generation || clientGeneration!=self->m_client->generation()
           || base!=self->m_base || sheet!=self->m_sheet || state!=self->m_state)return;
        // After a failed or stale read the next bind of this key reads again;
        // a stale state refreshes the window, which binds again.
        const bool stale=error.isEmpty()?result["state"].toString()!=state:result["code"]=="STALE_STATE";
        if(stale || !error.isEmpty()){self->m_listed=false;self->m_current=false;}
        if(stale){emit self->stale();return;}
        if(!error.isEmpty()){emit self->failed(tr("The charts on this sheet could not be read: %1. They are read again when the workbook refreshes.").arg(error));return;}
        self->accept(result["charts"].toArray());
    });
}

void ChartLayer::accept(const QJsonArray &charts) {
    if(!attached())return;
    QHash<QString,ChartOverlay *> existing;
    for(const auto &overlay:m_overlays)if(overlay)existing.insert(overlay->chartId(),overlay);
    QList<QPointer<ChartOverlay>> kept;
    for(const auto &value:charts) {
        const auto chart=value.toObject();const auto id=chart["id"].toString(chart["chart_id"].toString());
        if(id.isEmpty())continue;
        auto *overlay=existing.take(id);
        if(!overlay){overlay=new ChartOverlay(id,m_gridArea);overlay->installEventFilter(this);applyPassThrough(overlay);}
        overlay->setChart(chart);
        const auto reason=ChartSpec::stateReason(chart);
        // A readable chart keeps its previous image until a new one arrives.
        if(!reason.isEmpty() || !overlay->hasImage()){overlay->setPlaceholder(reason);overlay->setRenderKey({});}
        kept.append(overlay);
    }
    // A chart that went takes the selection with it, and the grid takes the
    // focus such a chart had.
    bool refocus=false;
    for(auto *overlay:std::as_const(existing))refocus=refocus || overlay->hasFocus();
    const bool selectedGone=existing.contains(m_selected);
    // A drag whose chart went ends silently.
    if(m_drag.overlay && existing.contains(m_drag.overlay->chartId()))m_drag={};
    for(auto *overlay:existing)delete overlay;
    m_overlays=kept;m_current=true;
    if(selectedGone){m_selected.clear();emit selectionChanged({});}
    abandonUnwanted();restack();relayout();
    if(refocus && m_frozen)m_frozen->focusCurrent();
    emit listed(int(m_overlays.size()));
}

void ChartLayer::clear() {
    ++m_generation;m_drag={};
    if(!m_selected.isEmpty()){m_selected.clear();emit selectionChanged({});}
    // A deleted chart's focus goes to the grid, not the next control.
    bool refocus=false;
    for(const auto &overlay:m_overlays)refocus=refocus || (overlay && overlay->hasFocus());
    for(const auto &overlay:m_overlays)delete overlay.data();
    m_overlays.clear();m_staleRetried.clear();m_failed.clear();
    if(refocus && m_frozen)m_frozen->focusCurrent();
    abandonUnwanted();
}

// Overlays sit above every pane of the grid area, lowest z first; raise()
// moves each to the top of its siblings, so the highest z ends uppermost.
void ChartLayer::restack() {
    for(auto *overlay:zOrdered())overlay->raise();
}

void ChartLayer::queueRelayout() {
    if(m_queued)return;m_queued=true;
    QTimer::singleShot(0,this,[this]{if(m_queued)relayout();});
}

bool ChartLayer::eventFilter(QObject *object,QEvent *event) {
    const auto type=event->type();
    if(m_swallowRepeat && (type==QEvent::ShortcutOverride || type==QEvent::KeyPress || type==QEvent::KeyRelease)) {
        // The key that deleted a chart, still held: its repeats reach the
        // grid once the chart is gone and are swallowed there, before the
        // window's Clear Contents shortcut, until the key is released.
        auto *key=static_cast<QKeyEvent *>(event);const bool same=key->key()==m_swallowRepeat;
        if(same && key->isAutoRepeat() && type!=QEvent::KeyRelease && !qobject_cast<ChartOverlay *>(object)){key->accept();return true;}
        if(!key->isAutoRepeat() && (type==QEvent::KeyPress || (same && type==QEvent::KeyRelease)))m_swallowRepeat=0;
    }
    if(auto *item=qobject_cast<ChartOverlay *>(object))return overlayEvent(item,event);
    if(event->type()==QEvent::MouseButtonPress) {
        // A press on a grid pane or header deselects and takes the focus back
        // from a chart; the pane handles the press as usual. During a chart
        // range pick the chart keeps its selection.
        for(const auto &pane:std::as_const(m_panes))if(pane==object){if(!m_keepSelection)select({});refocusGrid();break;}
        return QObject::eventFilter(object,event);
    }
    if(attached())switch(event->type()) {
    case QEvent::Resize: case QEvent::Move: case QEvent::Show: case QEvent::Hide: case QEvent::LayoutRequest: queueRelayout();break;
    default: break;
    }
    return QObject::eventFilter(object,event);
}

// The overlay's mouse, wheel and key events. Presses and drags never reach the
// grid area beneath; the wheel goes to the body's viewport.
bool ChartLayer::overlayEvent(ChartOverlay *item,QEvent *event) {
    switch(event->type()) {
    case QEvent::MouseButtonPress: {
        auto *mouse=static_cast<QMouseEvent *>(event);
        // Another press during a drag cancels it and does nothing else.
        if(m_drag.active){cancelDrag();return true;}
        m_drag={};
        // Handles are drawn, and so taken, only on a chart already selected.
        const int handle=item->handleAt(mouse->position().toPoint());
        select(item->chartId());item->setFocus(Qt::MouseFocusReason);
        if(mouse->button()==Qt::RightButton){emit contextMenuRequested(item->chartId(),mouse->globalPosition().toPoint());return true;}
        QRect start;QTableView *pane=nullptr;
        if(mouse->button()==Qt::LeftButton && m_interactive && !m_passThrough && trueRect(item,&start,&pane))
            m_drag={item,handle,mouse->globalPosition().toPoint(),start,false};
        updateCursor(item,handle);
        return true;
    }
    case QEvent::MouseButtonDblClick:
        select(item->chartId());item->setFocus(Qt::MouseFocusReason);emit editRequested(item->chartId());return true;
    case QEvent::MouseMove: {
        auto *mouse=static_cast<QMouseEvent *>(event);const QPoint global=mouse->globalPosition().toPoint();
        if(m_drag.overlay==item) {
            // A move without the button (its release went elsewhere) ends the drag.
            if(!(mouse->buttons()&Qt::LeftButton))cancelDrag();
            else {
                // A plain click sends nothing: the drag starts past the drag distance.
                if(!m_drag.active && (global-m_drag.origin).manhattanLength()<QApplication::startDragDistance())return true;
                m_drag.active=true;dragTo(global);return true;
            }
        }
        if(mouse->buttons()==Qt::NoButton)updateCursor(item,item->handleAt(mouse->position().toPoint()));
        return true;
    }
    case QEvent::MouseButtonRelease: {
        auto *mouse=static_cast<QMouseEvent *>(event);
        if(mouse->button()==Qt::LeftButton && m_drag.overlay==item)finishDrag(mouse->globalPosition().toPoint());
        return true;
    }
    case QEvent::ContextMenu:
        // A right-click's menu is asked for on the press, the keyboard's on its key.
        return true;
    case QEvent::FocusOut: case QEvent::WindowDeactivate:
        // A chart that loses the keyboard or the window ends its drag.
        if(m_drag.overlay==item)cancelDrag();
        return false;
    case QEvent::Wheel: {
        if(!m_body)return false;
        auto *wheel=static_cast<QWheelEvent *>(event);auto *viewport=m_body->viewport();
        QWheelEvent forwarded(viewport->mapFromGlobal(wheel->globalPosition()),wheel->globalPosition(),wheel->pixelDelta(),wheel->angleDelta(),
                              wheel->buttons(),wheel->modifiers(),wheel->phase(),wheel->inverted(),wheel->source(),wheel->pointingDevice());
        QCoreApplication::sendEvent(viewport,&forwarded);
        return true;
    }
    case QEvent::ShortcutOverride: case QEvent::KeyPress:
        return keyEvent(item,static_cast<QKeyEvent *>(event));
    default:
        return false;
    }
}

namespace {
// Offsets are 0 to 4096 on each axis.
constexpr int MaxOffset=4096;
bool offsetAllowed(const QJsonObject &offset) { return offset["x"].toInt()<=MaxOffset && offset["y"].toInt()<=MaxOffset; }
// Where a menu opened from the keyboard appears: the middle of what is shown.
QPoint menuPosition(ChartOverlay *item) {
    const QRect shown=item->mask().isEmpty()?item->rect():item->mask().boundingRect()&item->rect();
    return item->mapToGlobal(shown.center());
}
}

// Before the window's shortcuts (ShortcutOverride) and as key presses, a
// chart takes Escape; when selected, the menu key and Shift+F10; when also
// interactive, Delete, Backspace, Enter, Return and the arrows with Shift and
// Alt. While the mouse passes through it takes no keys.
bool ChartLayer::keyEvent(ChartOverlay *item,QKeyEvent *key) {
    if(m_passThrough)return false;
    const bool press=key->type()==QEvent::KeyPress;const int code=key->key();
    const auto modifiers=key->modifiers()&~Qt::KeypadModifier;
    if(code==Qt::Key_Escape && modifiers==Qt::NoModifier) {
        key->accept();
        if(!press)return true;
        // During a drag Escape cancels the drag only; the chart stays selected.
        if(m_drag.active)cancelDrag();
        else {select({});if(m_frozen)m_frozen->focusCurrent();}
        return true;
    }
    if(!item->selected())return false;
    // The menu opens while the chart cannot change too; it then offers Edit Chart only.
    if((code==Qt::Key_Menu && modifiers==Qt::NoModifier) || (code==Qt::Key_F10 && modifiers==Qt::ShiftModifier)) {
        key->accept();
        if(press && !key->isAutoRepeat() && !m_drag.active)emit contextMenuRequested(item->chartId(),menuPosition(item));
        return true;
    }
    if(!m_interactive)return false;
    const bool enter=(code==Qt::Key_Return || code==Qt::Key_Enter) && modifiers==Qt::NoModifier;
    const bool remove=(code==Qt::Key_Delete || code==Qt::Key_Backspace) && modifiers==Qt::NoModifier;
    const bool arrow=(code==Qt::Key_Left || code==Qt::Key_Right || code==Qt::Key_Up || code==Qt::Key_Down) && !(modifiers&~(Qt::ShiftModifier|Qt::AltModifier));
    if(!enter && !remove && !arrow)return false;
    key->accept();
    // During a drag only Escape acts.
    if(!press || m_drag.active)return true;
    if(arrow)nudge(item,code,modifiers);
    else if(!key->isAutoRepeat()) {
        if(enter)emit editRequested(item->chartId());
        else{m_swallowRepeat=code;emit deleteRequested(item->chartId());}
    }
    return true;
}

// 8 px a press, or 1 px with Alt. A move places the moved rectangle as a
// release would; a corner scrolled out of its pane's view, or moved past it,
// is placed among that pane's cells. A resize moves the right and bottom edges.
void ChartLayer::nudge(ChartOverlay *item,int key,Qt::KeyboardModifiers modifiers) {
    const int step=modifiers&Qt::AltModifier?1:8;
    const QPoint delta(key==Qt::Key_Left?-step:key==Qt::Key_Right?step:0,key==Qt::Key_Up?-step:key==Qt::Key_Down?step:0);
    const auto chart=item->chart();
    if(modifiers&Qt::ShiftModifier) {
        const auto size=chart["size"].toObject();
        const QSize next(size["width"].toInt()+delta.x(),size["height"].toInt()+delta.y());
        if(ChartSpec::clampSize(next)!=next){emit refused(tr("The chart cannot resize further"));return;}
        emit resizeRequested(item->chartId(),chart["anchor"].toObject(),chart["offset"].toObject(),QJsonObject{{"width",next.width()},{"height",next.height()}});
        return;
    }
    QRect rect;QTableView *pane=nullptr;QJsonObject anchor,offset;bool placed=false;
    if(trueRect(item,&rect,&pane)) {
        const bool shown=paneArea(pane->viewport()).contains(rect.topLeft());
        placed=(shown && placement(rect.translated(delta),&anchor,&offset)) || placementIn(pane,rect.topLeft()+delta,&anchor,&offset);
    }
    // A move that would leave nothing of the chart in view is refused, so the
    // keys never stay with a chart out of sight.
    if(!placed || !offsetAllowed(offset) || (rect.translated(delta)&cellsArea()).isEmpty()){emit refused(tr("The chart cannot move further"));return;}
    emit moveRequested(item->chartId(),anchor,offset);
}

QRect ChartLayer::dragRect(const QPoint &globalPosition) const {
    const QPoint delta=globalPosition-m_drag.origin;
    return m_drag.handle<0?m_drag.start.translated(delta):ChartSpec::resized(m_drag.start,m_drag.handle,delta);
}

// The dragged chart follows the pointer, drawn whole over the cells of every
// pane but never over a header or a scroll bar.
void ChartLayer::dragTo(const QPoint &globalPosition) {
    auto *item=m_drag.overlay.data();
    if(!item || !attached())return;
    const QRect rect=dragRect(globalPosition),shown=rect&cellsArea();
    item->setGeometry(rect);
    // An empty region would clear the mask; one outside the widget hides it all.
    item->setMask(shown.isEmpty()?QRegion(-1,-1,1,1):QRegion(shown.translated(-rect.topLeft())));
}

void ChartLayer::finishDrag(const QPoint &globalPosition) {
    const QRect rect=dragRect(globalPosition);const auto drag=std::exchange(m_drag,{});
    if(!drag.active || !drag.overlay)return;
    // The right, bottom-right and bottom handles leave the top-left corner
    // where it is stored; the others place the new corner, as relayout() would draw it.
    const bool cornerKept=drag.handle==3 || drag.handle==4 || drag.handle==5;
    const auto chart=drag.overlay->chart();
    QJsonObject anchor=chart["anchor"].toObject(),offset=chart["offset"].toObject();
    // Nothing moved, or no cell under the top-left corner: the chart goes back.
    if(rect==drag.start || (!cornerKept && (!placement(rect,&anchor,&offset) || !offsetAllowed(offset)))){relayout();return;}
    if(drag.handle<0)emit moveRequested(drag.overlay->chartId(),anchor,offset);
    else emit resizeRequested(drag.overlay->chartId(),anchor,offset,QJsonObject{{"width",rect.width()},{"height",rect.height()}});
}

void ChartLayer::cancelDrag() {
    const bool active=m_drag.active;m_drag={};
    if(active)relayout();
}

void ChartLayer::updateCursor(ChartOverlay *item,int handle) const {
    if(!item->selected() || !m_interactive || m_passThrough){item->unsetCursor();return;}
    static constexpr Qt::CursorShape shapes[8]{Qt::SizeFDiagCursor,Qt::SizeVerCursor,Qt::SizeBDiagCursor,Qt::SizeHorCursor,
                                               Qt::SizeFDiagCursor,Qt::SizeVerCursor,Qt::SizeBDiagCursor,Qt::SizeHorCursor};
    item->setCursor(handle<0?Qt::SizeAllCursor:shapes[handle]);
}

QTableView *ChartLayer::paneOf(int row,int col) const {
    const bool rowFrozen=row<m_frozen->frozenRows(),columnFrozen=col<m_frozen->frozenColumns();
    return rowFrozen && columnFrozen?m_frozen->cornerView():rowFrozen?m_frozen->view():columnFrozen?m_frozen->leftView():m_body.data();
}

QRect ChartLayer::paneArea(QWidget *viewport) const { return QRect(viewport->mapTo(m_gridArea,QPoint()),viewport->size()); }

QRect ChartLayer::cellsArea() const {
    const QRect body=paneArea(m_body->viewport());
    const int top=m_frozen->frozenRows()?paneArea(m_frozen->view()->viewport()).top():body.top();
    const int left=m_frozen->frozenColumns()?paneArea(m_frozen->leftView()->viewport()).left():body.left();
    return QRect(QPoint(left,top),body.bottomRight());
}

bool ChartLayer::anchorCell(ChartOverlay *overlay,int *row,int *col) const {
    if(!attached())return false;
    auto *model=m_body->model();const int rows=model?model->rowCount():0,columns=model?model->columnCount():0;
    const int frozenRows=m_frozen->frozenRows(),frozenColumns=m_frozen->frozenColumns();
    const auto anchor=overlay->chart()["anchor"].toObject();
    int r=anchor["row"].toInt(1)-1,c=anchor["col"].toInt(1)-1;
    if(r<0 || c<0)return false;
    // Hidden rows and columns move the anchor to the next shown one; a frozen
    // prefix is shown by the frozen panes, which the body hides.
    auto rowHidden=[&](int i){return i<frozenRows?m_frozen->view()->isRowHidden(i):m_body->isRowHidden(i);};
    auto columnHidden=[&](int i){return i<frozenColumns?m_frozen->leftView()->isColumnHidden(i):m_body->isColumnHidden(i);};
    while(r<rows && rowHidden(r))++r;
    while(c<columns && columnHidden(c))++c;
    if(r>=rows || c>=columns)return false;
    *row=r;*col=c;
    return true;
}

bool ChartLayer::trueRect(ChartOverlay *overlay,QRect *rect,QTableView **pane) const {
    int row=0,col=0;
    if(!anchorCell(overlay,&row,&col))return false;
    const auto chart=overlay->chart();const auto offset=chart["offset"].toObject(),size=chart["size"].toObject();
    auto *view=paneOf(row,col);
    const QPoint cell=view->viewport()->mapTo(m_gridArea,QPoint(view->columnViewportPosition(col),view->rowViewportPosition(row)));
    *rect=QRect(cell+QPoint(offset["x"].toInt(),offset["y"].toInt()),QSize(size["width"].toInt(),size["height"].toInt()));
    *pane=view;
    return true;
}

bool ChartLayer::placement(const QRect &rectInGridArea,QJsonObject *anchor,QJsonObject *offset) const {
    if(!attached())return false;
    const QPoint corner=rectInGridArea.topLeft();
    QList<QTableView *> panes{m_body.data()};
    if(m_frozen->frozenRows())panes.append(m_frozen->view());
    if(m_frozen->frozenColumns())panes.append(m_frozen->leftView());
    if(m_frozen->frozenRows() && m_frozen->frozenColumns())panes.append(m_frozen->cornerView());
    for(auto *view:std::as_const(panes))
        if(paneArea(view->viewport()).contains(corner))return placementIn(view,corner,anchor,offset);
    return false;
}

bool ChartLayer::placementIn(QTableView *view,const QPoint &point,QJsonObject *anchor,QJsonObject *offset) const {
    const QPoint local=view->viewport()->mapFrom(m_gridArea,point);
    const int row=view->rowAt(local.y()),col=view->columnAt(local.x());
    // Past the last row or column, or a cell another pane shows.
    if(row<0 || col<0 || paneOf(row,col)!=view)return false;
    *anchor=QJsonObject{{"row",row+1},{"col",col+1}};
    *offset=QJsonObject{{"x",local.x()-view->columnViewportPosition(col)},{"y",local.y()-view->rowViewportPosition(row)}};
    return true;
}

// Each chart is placed from the view that shows its anchor cell and clipped
// to the cells of that pane and the scrolling body beyond it: a chart anchored
// in a frozen row stays put vertically and may extend over the body below,
// while a body chart passes under the frozen rows and columns. The clip never
// includes a header or a scroll bar.
void ChartLayer::relayout() {
    m_queued=false;
    const auto items=overlays();
    // Restarted on each relayout, so scrolling renders once it settles.
    if(!items.isEmpty() || !m_jobs.isEmpty())m_renderTimer.start();
    if(items.isEmpty() || !attached())return;
    const QRect body=paneArea(m_body->viewport()),cells=cellsArea();
    // A focused chart hidden here (scrolled or moved out of view) gives up
    // its selection, and the grid takes the focus Qt would hand to the next control.
    bool lost=false;
    auto hide=[&](ChartOverlay *overlay) {
        if(overlay->isHidden())return;
        if(overlay->hasFocus()){lost=true;if(overlay->chartId()==m_selected)select({});}
        overlay->hide();
    };
    for(auto *overlay:items) {
        // A dragged chart follows the pointer, not its stored place.
        if(dragging(overlay))continue;
        QRect chartRect;QTableView *view=nullptr;
        if(m_projected || !trueRect(overlay,&chartRect,&view)){hide(overlay);continue;}
        const bool rowFrozen=view==m_frozen->view() || view==m_frozen->cornerView(),columnFrozen=view==m_frozen->leftView() || view==m_frozen->cornerView();
        const QRect clip(QPoint(columnFrozen?cells.left():body.left(),rowFrozen?cells.top():body.top()),body.bottomRight());
        const QRect shown=chartRect&clip;
        if(shown.isEmpty()){hide(overlay);continue;}
        overlay->setGeometry(chartRect);overlay->setMask(QRegion(shown.translated(-chartRect.topLeft())));overlay->show();
    }
    // The pane holding the grid cursor takes it, without scrolling back to the cursor.
    if(lost) {
        const auto current=m_body->currentIndex();
        (current.isValid()?paneOf(current.row(),current.column()):m_body.data())->setFocus(Qt::OtherFocusReason);
    }
}

// A chart is rendered while it is shown, readable and not projected away.
bool ChartLayer::wanted(ChartOverlay *overlay) const {
    return overlay && attached() && !overlay->isHidden() && !m_projected && !m_base.isEmpty() && !m_sheet.isEmpty() && !m_state.isEmpty()
        && ChartSpec::stateReason(overlay->chart()).isEmpty();
}

QString ChartLayer::renderKey(ChartOverlay *overlay) const {
    const auto size=overlay->chart()["size"].toObject();
    return m_state+"|"+QString::number(size["width"].toInt())+"x"+QString::number(size["height"].toInt());
}

ChartOverlay *ChartLayer::overlay(const QString &chartId) const {
    if(chartId.isEmpty())return nullptr;
    for(const auto &item:m_overlays)if(item && item->chartId()==chartId)return item;
    return nullptr;
}

QJsonObject ChartLayer::chart(const QString &chartId) const {
    auto *item=overlay(chartId);
    return item?item->chart():QJsonObject();
}

void ChartLayer::select(const QString &chartId) {
    auto *next=overlay(chartId);const auto id=next?chartId:QString();
    if(id==m_selected)return;
    cancelDrag();
    if(auto *previous=overlay(m_selected)){previous->setSelected(false);previous->unsetCursor();}
    m_selected=id;
    if(next)next->setSelected(true);
    emit selectionChanged(m_selected);
}

void ChartLayer::selectNext(int direction) {
    const auto ordered=zOrdered();
    if(ordered.isEmpty())return;
    const int n=int(ordered.size());int index=-1;
    for(int i=0;i<n;++i)if(ordered[i]->chartId()==m_selected)index=i;
    const int step=direction<0?-1:1;
    int next=index<0?(step>0?0:n-1):(index+step+n)%n;
    // A chart that cannot be shown is passed over.
    for(int tried=0;tried<n;++tried,next=(next+step+n)%n)
        if(focusChart(ordered[next]->chartId()))return;
    emit refused(tr("The chart is not shown • Show its hidden rows or columns"));
}

bool ChartLayer::focusChart(const QString &chartId) {
    auto *item=overlay(chartId);
    if(!item || m_projected || !reveal(item))return false;
    select(chartId);
    item->setFocus(Qt::OtherFocusReason);
    return true;
}

bool ChartLayer::reveal(ChartOverlay *item) {
    int row=0,col=0;
    if(!anchorCell(item,&row,&col))return false;
    relayout();
    if(!item->isHidden())return true;
    // The body scrolls to the anchor; a frozen row or column keeps its axis.
    const bool rowFrozen=row<m_frozen->frozenRows(),columnFrozen=col<m_frozen->frozenColumns();
    const int r=rowFrozen?m_body->rowAt(0):row,c=columnFrozen?m_body->columnAt(0):col;
    if(r>=0 && c>=0)m_body->scrollTo(m_body->model()->index(r,c),rowFrozen?QAbstractItemView::EnsureVisible:QAbstractItemView::PositionAtTop);
    relayout();
    return !item->isHidden();
}

void ChartLayer::setInteractive(bool interactive) {
    if(m_interactive==interactive)return;
    m_interactive=interactive;
    if(m_interactive)return;
    cancelDrag();
    for(auto *item:overlays())item->unsetCursor();
}

void ChartLayer::requestEdit() {
    if(!m_selected.isEmpty())emit editRequested(m_selected);
}

void ChartLayer::requestOrder(const QString &order) {
    if(!m_selected.isEmpty() && m_interactive)emit orderRequested(m_selected,order);
}

void ChartLayer::requestDelete() {
    if(!m_selected.isEmpty() && m_interactive)emit deleteRequested(m_selected);
}

void ChartLayer::setPassThrough(bool passThrough) {
    if(m_passThrough==passThrough)return;
    m_passThrough=passThrough;
    if(m_passThrough)cancelDrag();
    for(auto *item:overlays())applyPassThrough(item);
    // A chart keeps its selection through a pick but not the keyboard.
    if(m_passThrough)refocusGrid();
}

void ChartLayer::applyPassThrough(ChartOverlay *item) const {
    item->setAttribute(Qt::WA_TransparentForMouseEvents,m_passThrough);
    item->setFocusPolicy(m_passThrough?Qt::NoFocus:Qt::ClickFocus);
}

void ChartLayer::refocusGrid() {
    if(!m_frozen)return;
    for(auto *item:overlays())if(item->hasFocus()){m_frozen->focusCurrent();return;}
}

QList<ChartOverlay *> ChartLayer::zOrdered() const {
    auto ordered=overlays();
    std::stable_sort(ordered.begin(),ordered.end(),[](ChartOverlay *a,ChartOverlay *b){return a->chart()["z"].toInt()<b->chart()["z"].toInt();});
    return ordered;
}

int ChartLayer::jobIndex(const QString &jobId) const {
    for(int i=0;i<m_jobs.size();++i)if(m_jobs[i].id==jobId)return i;
    return -1;
}

int ChartLayer::jobForChart(const QString &chartId) const {
    for(int i=0;i<m_jobs.size();++i)if(m_jobs[i].chartId==chartId && !m_jobs[i].abandoned)return i;
    return -1;
}

// Cancels the jobs whose result would no longer be shown: their chart went,
// left view or changed size, or the sheet, state or workbook changed. Jobs of
// a previous service session get no answers and are dropped.
void ChartLayer::abandonUnwanted() {
    const auto generation=m_client?m_client->generation():0;
    for(int i=int(m_jobs.size())-1;i>=0;--i) {
        const auto &job=m_jobs[i];
        if(!m_client || job.clientGeneration!=generation){m_jobs.removeAt(i);continue;}
        if(job.abandoned)continue;
        auto *overlay=this->overlay(job.chartId);
        if(job.base!=m_base || job.sheet!=m_sheet || job.state!=m_state || !wanted(overlay) || renderKey(overlay)!=job.key)abandon(i);
    }
}

void ChartLayer::abandon(int index) {
    if(!m_jobs[index].started){m_jobs[index].abandoned=true;return;}
    const auto job=m_jobs.takeAt(index);
    retire(m_client,job.base+"/charts/jobs/"+job.id,true);
}

// Starts the renders still needed, lowest z first, while slots are free.
void ChartLayer::renderQueued() {
    if(!attached() || !m_client)return;
    abandonUnwanted();
    if(m_busy)return;
    for(auto *overlay:zOrdered()) {
        if(m_jobs.size()>=MaxConcurrentRenders)break;
        if(wanted(overlay) && overlay->renderKey()!=renderKey(overlay) && jobForChart(overlay->chartId())<0)start(overlay);
    }
}

void ChartLayer::start(ChartOverlay *overlay) {
    const auto size=overlay->chart()["size"].toObject();
    Job job{QUuid::createUuid().toString(QUuid::WithoutBraces),overlay->chartId(),m_base,m_sheet,m_state,renderKey(overlay),m_client->generation()};
    m_jobs.append(job);
    const auto path=job.base+"/charts/jobs/"+job.id;QPointer<ChartLayer> self(this);QPointer<RestClient> client(m_client);
    m_client->request(m_base+"/charts/jobs",{{"job_id",job.id},{"expected_state",job.state},{"chart_id",job.chartId},{"kind","render"},
                                            {"width",size["width"].toInt()},{"height",size["height"].toInt()}},
        [self,client,id=job.id,path](QJsonObject result,QString error){
        // A refusal names its code; an answer lost in transit may still have started the job.
        const auto code=result["code"].toString();const bool created=error.isEmpty() || code.isEmpty();
        const int index=self?self->jobIndex(id):-1;
        if(index<0 || self->m_jobs[index].abandoned) {
            if(created)retire(client,path,true);
            if(index>=0){self->m_jobs.removeAt(index);self->renderQueued();}
            return;
        }
        if(error.isEmpty()){self->m_jobs[index].started=true;self->m_busyDelay=PollIntervalMs;self->answer(id,result,error);return;}
        const auto job=self->m_jobs.takeAt(index);auto *overlay=self->overlay(job.chartId);
        if(code=="JOB_BUSY" || code=="JOB_LIMIT") {
            // The service's chart job slots are taken: try again shortly, backing
            // off, or as soon as one of this layer's jobs ends.
            self->m_busy=true;self->m_busyTimer.start(self->m_busyDelay);self->m_busyDelay=qMin(self->m_busyDelay*2,1000);
            return;
        }
        if(created)retire(client,path,true);
        if(code=="STALE_STATE") {
            // The window is behind the workbook, as for a stale catalog read: its
            // refresh binds the new state, which renders again.
            if(overlay)overlay->setRenderKey(job.key);
            emit self->stale();
        }
        else if(overlay)self->fail(overlay,job.key,result,error);
        self->renderQueued();
    });
}

void ChartLayer::poll(const QString &jobId) {
    const int index=jobIndex(jobId);if(index<0 || !m_client)return;
    QPointer<ChartLayer> self(this);
    m_client->request(m_jobs[index].base+"/charts/jobs/"+jobId,{},[self,jobId](QJsonObject result,QString error){if(self)self->answer(jobId,result,error);},false);
}

// A job's answer: polled again while running; otherwise released, and its
// result shown only while its chart, sheet, state and size are current.
void ChartLayer::answer(const QString &jobId,const QJsonObject &response,const QString &error) {
    const int index=jobIndex(jobId);if(index<0)return;
    const auto status=error.isEmpty()?response["status"].toString():QString();
    if(status=="running"){QTimer::singleShot(PollIntervalMs,this,[this,jobId]{poll(jobId);});return;}
    const auto job=m_jobs.takeAt(index);m_busy=false;m_busyTimer.stop();
    // An unreadable answer leaves the job's fate unknown, so it is cancelled first.
    retire(m_client,job.base+"/charts/jobs/"+job.id,!error.isEmpty());
    auto *overlay=this->overlay(job.chartId);
    if(overlay && job.base==m_base && job.sheet==m_sheet && job.state==m_state && renderKey(overlay)==job.key) {
        const auto result=response["result"].toObject();
        if(status=="completed" && result["state"].toString()==job.state) {
            if(overlay->setSvg(result["svg"].toString().toUtf8())){overlay->setRenderKey(job.key);m_failed.remove(job.chartId);emit rendered(job.chartId);}
            else fail(overlay,job.key,{{"code","INVALID_IMAGE"}},tr("The service's chart image could not be read"));
        }
        else if(status=="stale" || status=="completed") {
            // The workbook moved on while the job ran: start once more for this
            // state. That start is normally refused with STALE_STATE (see
            // start), since the workbook already moved on; a second stale
            // answer needs the workbook to change again between the retry's
            // start and its run, a race only a test proxy produces on demand.
            // The chart then waits, keeping what it shows, for the window to
            // bind the new state.
            if(m_staleRetried.value(job.chartId)!=job.key)m_staleRetried.insert(job.chartId,job.key);
            else overlay->setRenderKey(job.key);
        }
        else if(!error.isEmpty())fail(overlay,job.key,response,error);
        else {
            const auto failure=response["error"].toObject();
            fail(overlay,job.key,failure.isEmpty()?QJsonObject{{"code","CHART_JOB_"+status.toUpper()}}:failure,failure["message"].toString(tr("Chart job %1").arg(status)));
        }
    }
    renderQueued();
}

// A failure is never an empty space: the placeholder names the code and message.
void ChartLayer::fail(ChartOverlay *overlay,const QString &key,const QJsonObject &error,const QString &message) {
    const auto code=error["code"].toString();
    overlay->setPlaceholder(code.isEmpty()?tr("This chart could not be drawn: %1").arg(message):tr("This chart could not be drawn (%1): %2").arg(code,message));
    overlay->setRenderKey(key);m_failed.insert(overlay->chartId());
}

namespace {
// Polls a cancelled job for up to ten seconds before releasing it regardless.
constexpr int RetireAttempts=200;
void release(QPointer<RestClient> client,const QString &path){if(client)client->request(path+"/release",{},[](QJsonObject,QString){});}
void releaseWhenStopped(QPointer<RestClient> client,const QString &path,const QJsonObject &job,const QString &error,int attempts) {
    if(!client)return;
    if(!error.isEmpty() || job["status"]!="running" || attempts<=0){release(client,path);return;}
    QTimer::singleShot(ChartLayer::PollIntervalMs,client.data(),[client,path,attempts]{
        if(client)client->request(path,{},[client,path,attempts](QJsonObject job,QString error){releaseWhenStopped(client,path,job,error,attempts-1);},false);
    });
}
}

void ChartLayer::retire(QPointer<RestClient> client,const QString &path,bool cancel) {
    if(!client)return;
    if(!cancel){release(client,path);return;}
    client->request(path+"/cancel",{},[client,path](QJsonObject job,QString error){releaseWhenStopped(client,path,job,error,RetireAttempts);});
}
