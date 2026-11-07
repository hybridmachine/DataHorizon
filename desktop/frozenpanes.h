// SPDX-License-Identifier: GPL-3.0-only
#pragma once
#include "paneselection.h"
#include <QAbstractTableModel>
#include <QAbstractButton>
#include <QHeaderView>
#include <QHBoxLayout>
#include <QVBoxLayout>
#include <QPointer>
#include <QScrollBar>
#include <QSignalBlocker>

class FrozenPrefixModel : public QAbstractTableModel {
public:
    explicit FrozenPrefixModel(QAbstractItemModel *source,int rows,int columns,QObject *parent=nullptr) : QAbstractTableModel(parent), m_source(source),m_rows(rows),m_columns(columns) {
        connect(source,&QAbstractItemModel::modelAboutToBeReset,this,[this]{beginResetModel();});
        connect(source,&QAbstractItemModel::modelReset,this,[this]{endResetModel();});
        connect(source,&QObject::destroyed,this,[this]{beginResetModel(); m_source=nullptr; endResetModel();});
        connect(source,&QAbstractItemModel::rowsAboutToBeInserted,this,[this]{beginResetModel();});
        connect(source,&QAbstractItemModel::rowsInserted,this,[this]{endResetModel();});
        connect(source,&QAbstractItemModel::rowsAboutToBeRemoved,this,[this]{beginResetModel();});
        connect(source,&QAbstractItemModel::rowsRemoved,this,[this]{endResetModel();});
        connect(source,&QAbstractItemModel::columnsAboutToBeInserted,this,[this]{beginResetModel();});
        connect(source,&QAbstractItemModel::columnsInserted,this,[this]{endResetModel();});
        connect(source,&QAbstractItemModel::columnsAboutToBeRemoved,this,[this]{beginResetModel();});
        connect(source,&QAbstractItemModel::columnsRemoved,this,[this]{endResetModel();});
        connect(source,&QAbstractItemModel::dataChanged,this,[this](const QModelIndex &first,const QModelIndex &last,const QList<int> &roles){
            const int bottom=qMin(last.row(),rowCount()-1),right=qMin(last.column(),columnCount()-1);
            if(first.row()<=bottom && first.column()<=right)emit dataChanged(index(first.row(),first.column()),index(bottom,right),roles);
        });
        connect(source,&QAbstractItemModel::headerDataChanged,this,[this](Qt::Orientation orientation,int first,int last){
            last=qMin(last,(orientation==Qt::Horizontal?columnCount():rowCount())-1);
            if(first<=last)emit headerDataChanged(orientation,first,last);
        });
    }
    void setLimits(int rows,int columns){
        if(rows==m_rows && columns==m_columns)return;
        beginResetModel();m_rows=rows;m_columns=columns;endResetModel();
    }
    int rowCount(const QModelIndex &parent={}) const override {return parent.isValid() || !m_source?0:(m_rows<0?m_source->rowCount():qMin(m_rows,m_source->rowCount()));}
    int columnCount(const QModelIndex &parent={}) const override {return parent.isValid() || !m_source?0:(m_columns<0?m_source->columnCount():qMin(m_columns,m_source->columnCount()));}
    QVariant data(const QModelIndex &cell,int role=Qt::DisplayRole) const override {return m_source && cell.isValid()?m_source->data(m_source->index(cell.row(),cell.column()),role):QVariant{};}
    QVariant headerData(int section,Qt::Orientation orientation,int role) const override {return m_source?m_source->headerData(section,orientation,role):QVariant{};}
    Qt::ItemFlags flags(const QModelIndex &cell) const override {return m_source && cell.isValid()?m_source->flags(m_source->index(cell.row(),cell.column())):Qt::NoItemFlags;}
    bool setData(const QModelIndex &cell,const QVariant &value,int role) override {return m_source && cell.isValid() && m_source->setData(m_source->index(cell.row(),cell.column()),value,role);}
private:
    QPointer<QAbstractItemModel> m_source;
    int m_rows,m_columns;
};

// Three prefix proxies and the body share source indices, cache and selection.
// Widgets occupy layout cells rather than covering a scrolling viewport.
class FrozenPanes : public QWidget {
public:
    explicit FrozenPanes(QTableView *body,QWidget *parent=nullptr):QWidget(parent),m_body(body),
        m_topModel(body->model(),1,-1,this),m_leftModel(body->model(),-1,1,this),m_cornerModel(body->model(),1,1,this),m_selection(body,this) {
        auto make=[this,body](QAbstractItemModel *model,QWidget *parent,const QString &name,const QString &label){
            auto *view=new QTableView(parent);view->setObjectName(name);view->setAccessibleName(label);view->setModel(model);
            view->setSelectionMode(QAbstractItemView::ContiguousSelection);
            view->setHorizontalScrollBarPolicy(Qt::ScrollBarAlwaysOff);view->setVerticalScrollBarPolicy(Qt::ScrollBarAlwaysOff);
            view->setHorizontalScrollMode(body->horizontalScrollMode());view->setVerticalScrollMode(body->verticalScrollMode());
            view->horizontalHeader()->setDefaultSectionSize(body->horizontalHeader()->defaultSectionSize());
            view->verticalHeader()->setDefaultSectionSize(body->verticalHeader()->defaultSectionSize());
            view->viewport()->installEventFilter(this);view->installEventFilter(this);
            connect(view->horizontalHeader(),&QHeaderView::sectionResized,this,[this](int section,int,int size){resize(true,section,size);});
            connect(view->verticalHeader(),&QHeaderView::sectionResized,this,[this](int section,int,int size){resize(false,section,size);});
            connect(view->horizontalHeader(),&QHeaderView::sectionClicked,body,&QTableView::selectColumn);
            connect(view->verticalHeader(),&QHeaderView::sectionClicked,body,&QTableView::selectRow);
            if(auto *button=view->findChild<QAbstractButton *>())connect(button,&QAbstractButton::clicked,body,&QTableView::selectAll);
            m_selection.addPane(view);return view;
        };
        m_top=make(&m_topModel,this,"frozenTopRow",tr("Frozen top row"));
        auto *topLayout=new QHBoxLayout(this);topLayout->setContentsMargins(0,0,0,0);topLayout->setSpacing(0);topLayout->addWidget(m_top);topLayout->addStretch();
        m_leftWidget=new QWidget(parent);auto *leftLayout=new QVBoxLayout(m_leftWidget);leftLayout->setContentsMargins(0,0,0,0);leftLayout->setSpacing(0);
        m_left=make(&m_leftModel,m_leftWidget,"frozenFirstColumn",tr("Frozen first column"));leftLayout->addWidget(m_left);leftLayout->addStretch();
        m_corner=make(&m_cornerModel,parent,"frozenCorner",tr("Frozen row and column intersection"));
        hide();m_leftWidget->hide();m_corner->hide();
        body->viewport()->installEventFilter(this);body->verticalHeader()->installEventFilter(this);body->horizontalHeader()->installEventFilter(this);
        connect(body->horizontalHeader(),&QHeaderView::sectionResized,this,[this](int col,int,int size){resize(true,col,size);});
        connect(body->verticalHeader(),&QHeaderView::sectionResized,this,[this](int row,int,int size){resize(false,row,size);});
        connect(body->horizontalScrollBar(),&QScrollBar::valueChanged,m_top->horizontalScrollBar(),&QScrollBar::setValue);
        connect(m_top->horizontalScrollBar(),&QScrollBar::valueChanged,body->horizontalScrollBar(),&QScrollBar::setValue);
        connect(body->verticalScrollBar(),&QScrollBar::valueChanged,m_left->verticalScrollBar(),&QScrollBar::setValue);
        connect(m_left->verticalScrollBar(),&QScrollBar::valueChanged,body->verticalScrollBar(),&QScrollBar::setValue);
    }
    ~FrozenPanes()override {
        m_selection.setEnabled(false);
        m_body->viewport()->removeEventFilter(this);m_body->verticalHeader()->removeEventFilter(this);m_body->horizontalHeader()->removeEventFilter(this);
        m_body->horizontalHeader()->disconnect(this);m_body->verticalHeader()->disconnect(this);
        for(auto *view:views()) {
            view->removeEventFilter(this);view->viewport()->removeEventFilter(this);
            view->horizontalHeader()->disconnect(this);view->verticalHeader()->disconnect(this);view->setModel(nullptr);
        }
        delete m_leftWidget;delete m_corner;
    }
    QTableView *view()const{return m_top;}
    QTableView *leftView()const{return m_left;}
    QTableView *cornerView()const{return m_corner;}
    QWidget *leftWidget()const{return m_leftWidget;}
    QList<QTableView *> views()const{return {m_top,m_left,m_corner};}
    bool active()const{return m_active;}
    bool columnActive()const{return m_columnActive;}
    int frozenRows()const{return m_active?m_rows:0;}
    int frozenColumns()const{return m_columnActive?m_columns:0;}
    void setFrozen(bool active,int height=26){setFrozenCounts(active?1:0,frozenColumns());setRowHeight(height);}
    void setFirstColumnFrozen(bool active){setFrozenCounts(frozenRows(),active?1:0);}
    void setFrozenCounts(int rows,int columns){
        rows=qBound(0,rows,16);columns=qBound(0,columns,8);
        if(rows==frozenRows() && columns==frozenColumns()){apply();return;}
        QList<int> heights,widths;
        for(int row=0;row<qMax(1,rows);++row)heights.append(sectionSize(false,row));
        for(int col=0;col<qMax(1,columns);++col)widths.append(sectionSize(true,col));
        m_selection.setEnabled(false);m_active=rows>0;m_columnActive=columns>0;m_rows=qMax(1,rows);m_columns=qMax(1,columns);
        m_topModel.setLimits(m_rows,-1);m_leftModel.setLimits(-1,m_columns);m_cornerModel.setLimits(m_rows,m_columns);
        for(int row=0;row<heights.size();++row)resize(false,row,heights[row]);
        for(int col=0;col<widths.size();++col)resize(true,col,widths[col]);
        apply();
    }
    void setRowHeight(int height){resize(false,0,qBound(18,height,1000));}
    void setHiddenRows(const QSet<int> &rows){for(int row:m_hiddenRows)m_left->setRowHidden(row,false);m_hiddenRows=rows;apply();}
    void setHiddenColumns(const QSet<int> &columns){for(int col:m_hiddenColumns)m_top->setColumnHidden(col,false);m_hiddenColumns=columns;apply();}
    void resizeSection(bool columns,int section,int size){resize(columns,section,size);}
    int sectionSize(bool columns,int section)const {
        int size=columns?m_body->columnWidth(section):m_body->rowHeight(section);
        if(size>0)return size;
        for(auto *view:views())if(section<(columns?view->model()->columnCount():view->model()->rowCount())) {
            size=columns?view->columnWidth(section):view->rowHeight(section);if(size>0)return size;
        }
        return columns?110:26;
    }
    void syncSelection(){m_selection.sync();}
    PaneSelection *selectionController(){return &m_selection;}
    void focusCurrent(){m_selection.focusCurrent();}
protected:
    bool eventFilter(QObject *object,QEvent *event)override {
        for(auto *view:views())if(view && object==view->viewport() && view->isVisible() && event->type()==QEvent::Wheel){QApplication::sendEvent(m_body->viewport(),event);return true;}
        if(event->type()==QEvent::Resize || event->type()==QEvent::Move || event->type()==QEvent::Show || event->type()==QEvent::Hide)syncGeometry();
        return QWidget::eventFilter(object,event);
    }
private:
    void resize(bool columns,int section,int size){
        if(m_sizing || size<=0)return;QScopedValueRollback<bool> sizing(m_sizing,true);
        size=columns?qBound(30,size,2000):qBound(18,size,1000);
        if(columns)m_body->setColumnWidth(section,size);else m_body->setRowHeight(section,size);
        // Qt reports zero for hidden source sections; retain their real size
        // while the prefix is displayed in another pane.
        const int actual=columns?m_body->columnWidth(section):m_body->rowHeight(section);
        if(actual>0)size=actual;
        for(auto *view:views())if(view && section<(columns?view->model()->columnCount():view->model()->rowCount())) {
            QSignalBlocker blocker(columns?view->horizontalHeader():view->verticalHeader());
            if(columns)view->setColumnWidth(section,size);else view->setRowHeight(section,size);
        }
        syncGeometry();
    }
    void apply(){
        m_body->horizontalHeader()->setVisible(!m_active);m_body->verticalHeader()->setVisible(!m_columnActive);
        m_top->verticalHeader()->setVisible(!m_columnActive);m_left->horizontalHeader()->setVisible(!m_active);
        for(int row=0;row<qMax(m_maskRows,frozenRows());++row){
            const bool hidden=row<frozenRows() || m_hiddenRows.contains(row);m_body->setRowHidden(row,hidden);m_left->setRowHidden(row,hidden);
        }
        for(int col=0;col<qMax(m_maskColumns,frozenColumns());++col){
            const bool hidden=col<frozenColumns() || m_hiddenColumns.contains(col);m_body->setColumnHidden(col,hidden);m_top->setColumnHidden(col,hidden);
        }
        m_maskRows=frozenRows();m_maskColumns=frozenColumns();
        for(int row:m_hiddenRows)m_left->setRowHidden(row,true);
        for(int col:m_hiddenColumns)m_top->setColumnHidden(col,true);
        setVisible(m_active);m_leftWidget->setVisible(m_columnActive);m_corner->setVisible(m_active && m_columnActive);
        m_selection.setEnabled(m_active || m_columnActive);syncGeometry();
    }
    void syncGeometry(){
        if(m_syncing || !m_top || !m_left || !m_corner)return;QScopedValueRollback<bool> syncing(m_syncing,true);
        const int rowHeader=m_columnActive?m_left->verticalHeader()->width():m_body->verticalHeader()->width();
        if(!m_columnActive)m_top->verticalHeader()->setFixedWidth(rowHeader);
        int rowHeight=0,columnWidth=0;
        for(int row=0;row<m_rows;++row)rowHeight+=m_top->rowHeight(row);
        for(int col=0;col<m_columns;++col)columnWidth+=m_left->columnWidth(col);
        const int height=m_top->horizontalHeader()->height()+rowHeight+2*m_top->frameWidth();
        m_top->setFixedWidth(m_body->viewport()->width()+(m_columnActive?0:rowHeader)+2*m_body->frameWidth());setFixedHeight(height);
        const int width=rowHeader+columnWidth+2*m_left->frameWidth();m_leftWidget->setFixedWidth(width);
        m_left->setFixedHeight(m_body->viewport()->height()+(m_active?0:m_body->horizontalHeader()->height())+2*m_body->frameWidth());
        m_corner->verticalHeader()->setFixedWidth(rowHeader);m_corner->setFixedSize(width,height);
        m_top->horizontalScrollBar()->setValue(m_body->horizontalScrollBar()->value());m_left->verticalScrollBar()->setValue(m_body->verticalScrollBar()->value());
    }
    QTableView *m_body,*m_top=nullptr,*m_left=nullptr,*m_corner=nullptr;QWidget *m_leftWidget=nullptr;
    FrozenPrefixModel m_topModel,m_leftModel,m_cornerModel;
    PaneSelection m_selection;
    QSet<int> m_hiddenRows,m_hiddenColumns;
    int m_rows=1,m_columns=1,m_maskRows=0,m_maskColumns=0;
    bool m_active=false,m_columnActive=false,m_syncing=false,m_sizing=false;
};
