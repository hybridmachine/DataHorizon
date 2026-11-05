// SPDX-License-Identifier: GPL-3.0-only
#pragma once
#include <QAbstractTableModel>
#include <QHeaderView>
#include <QHBoxLayout>
#include <QKeyEvent>
#include <QApplication>
#include <QAbstractButton>
#include <QScrollBar>
#include <QSignalBlocker>
#include <QTableView>

// A single source row with no cell cache or independent REST requests.
class FrozenTopRowModel : public QAbstractTableModel {
public:
    explicit FrozenTopRowModel(QAbstractItemModel *source,QObject *parent=nullptr) : QAbstractTableModel(parent), m_source(source) {
        connect(source,&QAbstractItemModel::modelAboutToBeReset,this,[this]{beginResetModel();});
        connect(source,&QAbstractItemModel::modelReset,this,[this]{endResetModel();});
        connect(source,&QAbstractItemModel::rowsAboutToBeInserted,this,[this]{beginResetModel();});
        connect(source,&QAbstractItemModel::rowsInserted,this,[this]{endResetModel();});
        connect(source,&QAbstractItemModel::rowsAboutToBeRemoved,this,[this]{beginResetModel();});
        connect(source,&QAbstractItemModel::rowsRemoved,this,[this]{endResetModel();});
        connect(source,&QAbstractItemModel::columnsAboutToBeInserted,this,[this]{beginResetModel();});
        connect(source,&QAbstractItemModel::columnsInserted,this,[this]{endResetModel();});
        connect(source,&QAbstractItemModel::columnsAboutToBeRemoved,this,[this]{beginResetModel();});
        connect(source,&QAbstractItemModel::columnsRemoved,this,[this]{endResetModel();});
        connect(source,&QAbstractItemModel::dataChanged,this,[this](const QModelIndex &first,const QModelIndex &last,const QList<int> &roles){
            if(first.row()==0 && rowCount()) emit dataChanged(index(0,first.column()),index(0,last.column()),roles);
        });
        connect(source,&QAbstractItemModel::headerDataChanged,this,[this](Qt::Orientation orientation,int first,int last){
            if(orientation==Qt::Horizontal) emit headerDataChanged(orientation,first,last);
            else if(first==0 && rowCount()) emit headerDataChanged(orientation,0,0);
        });
    }
    int rowCount(const QModelIndex &parent={}) const override {return parent.isValid()?0:qMin(1,m_source->rowCount());}
    int columnCount(const QModelIndex &parent={}) const override {return parent.isValid()?0:m_source->columnCount();}
    QVariant data(const QModelIndex &cell,int role=Qt::DisplayRole) const override {return cell.isValid()?m_source->data(m_source->index(0,cell.column()),role):QVariant{};}
    QVariant headerData(int section,Qt::Orientation orientation,int role) const override {return m_source->headerData(section,orientation,role);}
    Qt::ItemFlags flags(const QModelIndex &cell) const override {return cell.isValid()?m_source->flags(m_source->index(0,cell.column())):Qt::NoItemFlags;}
    bool setData(const QModelIndex &cell,const QVariant &value,int role) override {return cell.isValid() && m_source->setData(m_source->index(0,cell.column()),value,role);}
private:
    QAbstractItemModel *m_source;
};

// A layout sibling of the body, so no scrolling body cells are obscured.
class FrozenTopRow : public QWidget {
public:
    explicit FrozenTopRow(QTableView *body,QWidget *parent=nullptr) : QWidget(parent), m_body(body), m_model(body->model(),this) {
        auto *layout=new QHBoxLayout(this); layout->setContentsMargins(0,0,0,0); layout->setSpacing(0);
        m_view=new QTableView(this); m_view->setObjectName("frozenTopRow"); m_view->setAccessibleName(tr("Frozen top row"));
        m_view->setModel(&m_model); m_view->setSelectionMode(QAbstractItemView::ContiguousSelection);
        m_view->setHorizontalScrollBarPolicy(Qt::ScrollBarAlwaysOff); m_view->setVerticalScrollBarPolicy(Qt::ScrollBarAlwaysOff);
        m_view->setHorizontalScrollMode(body->horizontalScrollMode());
        m_view->horizontalHeader()->setDefaultSectionSize(body->horizontalHeader()->defaultSectionSize());
        layout->addWidget(m_view); layout->addStretch(); hide();
        body->viewport()->installEventFilter(this); body->verticalHeader()->installEventFilter(this);
        body->installEventFilter(this); m_view->installEventFilter(this); m_view->viewport()->installEventFilter(this);
        connect(body->horizontalHeader(),&QHeaderView::sectionResized,this,[this](int col,int,int size){
            QSignalBlocker blocker(m_view->horizontalHeader()); m_view->setColumnWidth(col,size);
        });
        connect(m_view->horizontalHeader(),&QHeaderView::sectionResized,this,[this](int col,int,int size){m_body->setColumnWidth(col,size);});
        connect(m_view->horizontalHeader(),&QHeaderView::sectionClicked,this,[this](int col){m_body->selectColumn(col);});
        if(auto *corner=m_view->findChild<QAbstractButton *>()) connect(corner,&QAbstractButton::clicked,m_body,&QTableView::selectAll);
        connect(body->horizontalScrollBar(),&QScrollBar::valueChanged,m_view->horizontalScrollBar(),&QScrollBar::setValue);
        connect(m_view->horizontalScrollBar(),&QScrollBar::valueChanged,body->horizontalScrollBar(),&QScrollBar::setValue);
        connect(m_view->selectionModel(),&QItemSelectionModel::currentChanged,this,[this](const QModelIndex &current){
            if(!m_active || !current.isValid()) return;
            const int scroll=m_body->verticalScrollBar()->value();
            m_selecting=true;
            m_body->setCurrentIndex(m_body->model()->index(0,current.column()));
            m_body->verticalScrollBar()->setValue(scroll);
            m_selecting=false;
            if(m_body->currentIndex()!=m_body->model()->index(0,current.column())) syncSelection();
        });
        connect(m_view->selectionModel(),&QItemSelectionModel::selectionChanged,this,[this]{
            if(!m_active || m_selecting) return;
            QItemSelection selection;
            for(const auto &range:m_view->selectionModel()->selection())
                selection.select(m_body->model()->index(0,range.left()),m_body->model()->index(0,range.right()));
            m_body->selectionModel()->select(selection,QItemSelectionModel::ClearAndSelect);
        });
        connect(body->selectionModel(),&QItemSelectionModel::currentChanged,this,[this]{syncSelection();});
    }
    QTableView *view() const {return m_view;}
    bool active() const {return m_active;}
    void setFrozen(bool active,int height=26) {
        if(active==m_active) {if(active) setRowHeight(height); return;}
        m_active=active;
        m_body->horizontalHeader()->setVisible(!active);
        if(active) {
            for(int col=0;col<m_model.columnCount();++col) if(m_view->columnWidth(col)!=m_body->columnWidth(col)) m_view->setColumnWidth(col,m_body->columnWidth(col));
            m_body->setRowHidden(0,true); setRowHeight(height); show(); syncSelection();
        } else {hide(); m_body->setRowHidden(0,false);}
        syncGeometry();
    }
    void setRowHeight(int height) {m_view->setRowHeight(0,qBound(18,height,1000)); syncGeometry();}
    void setHiddenColumns(const QSet<int> &columns) {
        QSignalBlocker blocker(m_view->horizontalHeader());
        const auto previous=m_hiddenColumns;
        for(int col:previous) m_view->setColumnHidden(col,false);
        for(int col:columns) m_view->setColumnHidden(col,true);
        m_hiddenColumns=columns; syncGeometry();
    }
    void syncSelection() {
        if(m_selecting) return;
        QSignalBlocker blocker(m_view->selectionModel());
        const auto current=m_body->currentIndex();
        if(current.isValid() && current.row()==0) {
            if(m_view->currentIndex()!=m_model.index(0,current.column())) m_view->setCurrentIndex(m_model.index(0,current.column()));
        }
        else {m_view->clearSelection(); m_view->setCurrentIndex({});}
    }
protected:
    bool eventFilter(QObject *object,QEvent *event) override {
        if(m_active && object==m_view->viewport() && event->type()==QEvent::Wheel) {
            QApplication::sendEvent(m_body->viewport(),event); return true;
        }
        if(m_active && event->type()==QEvent::KeyPress) {
            const auto *key=static_cast<QKeyEvent *>(event);
            if(object==m_view && key->key()==Qt::Key_Down && key->modifiers()==Qt::NoModifier) {
                const int col=qMax(0,m_view->currentIndex().column());
                int row=1; while(row<m_body->model()->rowCount() && m_body->isRowHidden(row)) ++row;
                m_body->setCurrentIndex(m_body->model()->index(row,col)); m_body->setFocus(); return true;
            }
            if(object==m_body && key->key()==Qt::Key_Up && key->modifiers()==Qt::NoModifier && m_body->currentIndex().row()>0) {
                int row=m_body->currentIndex().row()-1;
                while(row>0 && m_body->isRowHidden(row)) --row;
                if(row==0) {m_body->setCurrentIndex(m_body->model()->index(0,m_body->currentIndex().column())); m_view->setFocus(); return true;}
            }
        }
        if((object==m_body->viewport() || object==m_body->verticalHeader()) && (event->type()==QEvent::Resize || event->type()==QEvent::Move)) syncGeometry();
        return QWidget::eventFilter(object,event);
    }
private:
    void syncGeometry() {
        if(!m_active || m_syncing) return;
        m_syncing=true;
        m_view->verticalHeader()->setFixedWidth(m_body->verticalHeader()->width());
        m_view->setFixedWidth(m_body->viewport()->width()+m_body->verticalHeader()->width()+2*m_body->frameWidth());
        setFixedHeight(m_view->horizontalHeader()->height()+m_view->rowHeight(0)+2*m_view->frameWidth());
        m_view->horizontalScrollBar()->setValue(m_body->horizontalScrollBar()->value());
        m_syncing=false;
    }
    QTableView *m_body, *m_view;
    FrozenTopRowModel m_model;
    QSet<int> m_hiddenColumns;
    bool m_active=false, m_syncing=false, m_selecting=false;
};
