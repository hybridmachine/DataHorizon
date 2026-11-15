// SPDX-License-Identifier: GPL-3.0-only
#include "chartlayer.h"
#include "frozenpanes.h"
#include "restclient.h"
#include <QEvent>
#include <QHeaderView>
#include <QJsonArray>
#include <QPainter>
#include <QScrollBar>
#include <QSvgRenderer>
#include <QTableView>
#include <QTimer>
#include <algorithm>

ChartOverlay::ChartOverlay(const QString &chartId,QWidget *parent):QWidget(parent) {
    setObjectName("chartOverlay");setProperty("chartId",chartId);
    setAttribute(Qt::WA_TransparentForMouseEvents);setFocusPolicy(Qt::NoFocus);
    m_reason=tr("Loading");
}

void ChartOverlay::setChart(const QJsonObject &chart) {
    m_chart=chart;setAccessibleName(chart["name"].toString());update();
}

void ChartOverlay::setSvg(const QByteArray &svg) {
    if(svg.isEmpty())return;
    m_svg=svg;update();
}

void ChartOverlay::setPlaceholder(const QString &reason) {
    m_svg.clear();m_reason=reason.isEmpty()?tr("Loading"):reason;update();
}

QString ChartOverlay::placeholder() const {
    return hasImage()?QString():m_chart["name"].toString()+"\n"+m_reason;
}

void ChartOverlay::paintEvent(QPaintEvent *) {
    QPainter painter(this);
    painter.fillRect(rect(),palette().base());
    if(hasImage()) {
        QSvgRenderer renderer(m_svg);
        if(renderer.isValid()){renderer.render(&painter,rect());return;}
    }
    painter.setPen(palette().color(QPalette::Mid));painter.drawRect(rect().adjusted(0,0,-1,-1));
    painter.setPen(palette().color(QPalette::Text));
    painter.drawText(rect().adjusted(8,8,-8,-8),Qt::AlignCenter|Qt::TextWordWrap,placeholder());
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
    }
    auto *model=body->model();
    connect(model,&QAbstractItemModel::modelReset,this,&ChartLayer::queueRelayout);
    connect(model,&QAbstractItemModel::layoutChanged,this,&ChartLayer::queueRelayout);
    connect(model,&QAbstractItemModel::rowsInserted,this,&ChartLayer::queueRelayout);
    connect(model,&QAbstractItemModel::rowsRemoved,this,&ChartLayer::queueRelayout);
    connect(model,&QAbstractItemModel::columnsInserted,this,&ChartLayer::queueRelayout);
    connect(model,&QAbstractItemModel::columnsRemoved,this,&ChartLayer::queueRelayout);
    for(QWidget *widget:{gridArea,static_cast<QWidget *>(frozen),frozen->leftWidget()})widget->installEventFilter(this);
}

QList<ChartOverlay *> ChartLayer::overlays() const {
    QList<ChartOverlay *> result;
    for(const auto &overlay:m_overlays)if(overlay)result.append(overlay);
    return result;
}

void ChartLayer::bind(const QString &workbookBase,const QString &sheet,const QString &state,bool projected) {
    if(workbookBase==m_base && sheet==m_sheet && state==m_state) {
        if(projected!=m_projected){m_projected=projected;relayout();}
        return;
    }
    const bool sameSheet=workbookBase==m_base && sheet==m_sheet;
    m_base=workbookBase;m_sheet=sheet;m_state=state;m_projected=projected;
    // Another sheet's charts never stay on screen while this one's are read;
    // a new state of the same sheet keeps the overlays until the list lands.
    if(!sameSheet)clear();else ++m_generation;
    if(!m_base.isEmpty() && !m_sheet.isEmpty() && !m_state.isEmpty())list();
    relayout();
}

void ChartLayer::list() {
    const auto generation=++m_generation;const auto clientGeneration=m_client->generation();
    const auto base=m_base,sheet=m_sheet,state=m_state;QPointer<ChartLayer> self(this);
    m_client->request(base+"/charts",{{"expected_state",state},{"sheet",sheet}},[self,generation,clientGeneration,base,sheet,state](QJsonObject result,QString error){
        if(!self || generation!=self->m_generation || clientGeneration!=self->m_client->generation()
           || base!=self->m_base || sheet!=self->m_sheet || state!=self->m_state)return;
        // A stale or failed read is not shown: the window refreshes on a stale
        // state, and each later bind lists again.
        if(!error.isEmpty() || result["state"].toString()!=state)return;
        self->accept(result["charts"].toArray());
    });
}

QString ChartLayer::reasonFor(const QJsonObject &chart) {
    if(chart["issue"].isString())return tr("This chart cannot be read (%1)").arg(chart["issue"].toString());
    if(chart["over_limit"].isString())return tr("This chart's sources are over a size limit (%1)").arg(chart["over_limit"].toString());
    const auto spec=chart["spec"].toObject();
    QList<QJsonObject> sources;
    sources.append(spec["categories"].toObject()["source"].toObject());
    for(const auto &value:spec["series"].toArray())
        for(const auto &part:QStringList{"name","values","x"})sources.append(value.toObject()[part].toObject()["source"].toObject());
    for(const auto &source:sources)if(source["broken"].toBool())return tr("A source of this chart no longer exists");
    return tr("Loading");
}

void ChartLayer::accept(const QJsonArray &charts) {
    QHash<QString,ChartOverlay *> existing;
    for(const auto &overlay:m_overlays)if(overlay)existing.insert(overlay->chartId(),overlay);
    QList<QPointer<ChartOverlay>> kept;
    for(const auto &value:charts) {
        const auto chart=value.toObject();const auto id=chart["id"].toString(chart["chart_id"].toString());
        if(id.isEmpty())continue;
        auto *overlay=existing.take(id);
        if(!overlay)overlay=new ChartOverlay(id,m_gridArea);
        overlay->setChart(chart);
        const auto reason=reasonFor(chart);
        // A readable chart keeps its previous image until a new one arrives.
        if(reason!=tr("Loading") || !overlay->hasImage())overlay->setPlaceholder(reason);
        kept.append(overlay);
    }
    for(auto *overlay:existing)delete overlay;
    m_overlays=kept;restack();relayout();
    emit listed(int(m_overlays.size()));
}

void ChartLayer::clear() {
    ++m_generation;
    for(const auto &overlay:m_overlays)delete overlay.data();
    m_overlays.clear();
}

// Overlays sit above every pane of the grid area, lowest z first; raise()
// moves each to the top of its siblings, so the highest z ends uppermost.
void ChartLayer::restack() {
    auto ordered=overlays();
    std::stable_sort(ordered.begin(),ordered.end(),[](ChartOverlay *a,ChartOverlay *b){return a->chart()["z"].toInt()<b->chart()["z"].toInt();});
    for(auto *overlay:ordered)overlay->raise();
}

void ChartLayer::queueRelayout() {
    if(m_queued)return;m_queued=true;
    QTimer::singleShot(0,this,[this]{if(m_queued)relayout();});
}

bool ChartLayer::eventFilter(QObject *object,QEvent *event) {
    switch(event->type()) {
    case QEvent::Resize: case QEvent::Move: case QEvent::Show: case QEvent::Hide: case QEvent::LayoutRequest: queueRelayout();break;
    default: break;
    }
    return QObject::eventFilter(object,event);
}

// Each chart is placed from the view that shows its anchor cell and clipped
// to the cells of that pane and the scrolling body beyond it: a chart anchored
// in a frozen row stays put vertically and may extend over the body below,
// while a body chart passes under the frozen rows and columns. The clip never
// includes a header or a scroll bar.
void ChartLayer::relayout() {
    m_queued=false;
    const auto items=overlays();if(items.isEmpty())return;
    auto *model=m_body->model();const int rows=model?model->rowCount():0,columns=model?model->columnCount():0;
    const int frozenRows=m_frozen->frozenRows(),frozenColumns=m_frozen->frozenColumns();
    auto area=[this](QWidget *viewport){return QRect(viewport->mapTo(m_gridArea,QPoint()),viewport->size());};
    const QRect body=area(m_body->viewport());
    const int top=frozenRows?area(m_frozen->view()->viewport()).top():body.top();
    const int left=frozenColumns?area(m_frozen->leftView()->viewport()).left():body.left();
    // Hidden rows and columns move the anchor to the next shown one; a frozen
    // prefix is shown by the frozen panes, which the body hides.
    auto rowHidden=[&](int row){return row<frozenRows?m_frozen->view()->isRowHidden(row):m_body->isRowHidden(row);};
    auto columnHidden=[&](int col){return col<frozenColumns?m_frozen->leftView()->isColumnHidden(col):m_body->isColumnHidden(col);};
    for(auto *overlay:items) {
        const auto chart=overlay->chart();const auto anchor=chart["anchor"].toObject(),offset=chart["offset"].toObject(),size=chart["size"].toObject();
        int row=anchor["row"].toInt(1)-1,col=anchor["col"].toInt(1)-1;
        if(m_projected || row<0 || col<0){overlay->hide();continue;}
        while(row<rows && rowHidden(row))++row;
        while(col<columns && columnHidden(col))++col;
        if(row>=rows || col>=columns){overlay->hide();continue;}
        const bool rowFrozen=row<frozenRows,columnFrozen=col<frozenColumns;
        QTableView *view=rowFrozen && columnFrozen?m_frozen->cornerView():rowFrozen?m_frozen->view():columnFrozen?m_frozen->leftView():m_body;
        const QPoint cell=view->viewport()->mapTo(m_gridArea,QPoint(view->columnViewportPosition(col),view->rowViewportPosition(row)));
        const QRect chartRect(cell+QPoint(offset["x"].toInt(),offset["y"].toInt()),QSize(size["width"].toInt(),size["height"].toInt()));
        const QRect clip(QPoint(columnFrozen?left:body.left(),rowFrozen?top:body.top()),body.bottomRight());
        const QRect shown=chartRect&clip;
        if(shown.isEmpty()){overlay->hide();continue;}
        overlay->setGeometry(chartRect);overlay->setMask(QRegion(shown.translated(-chartRect.topLeft())));overlay->show();
    }
}
