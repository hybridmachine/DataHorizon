// SPDX-License-Identifier: GPL-3.0-only
// M1 read-only display probe, deliberately outside the installed desktop.
#pragma once
#include <QAbstractTableModel>
#include <QEvent>
#include <QHeaderView>
#include <QScrollBar>
#include <QTableView>

class FirstRowModel : public QAbstractTableModel {
public:
    explicit FirstRowModel(QAbstractItemModel *source) : m_source(source) {
        auto before=[this]{beginResetModel();};
        auto after=[this]{endResetModel();};
        connect(source,&QAbstractItemModel::modelAboutToBeReset,this,before);
        connect(source,&QAbstractItemModel::modelReset,this,after);
        connect(source,&QAbstractItemModel::rowsAboutToBeInserted,this,before);
        connect(source,&QAbstractItemModel::rowsInserted,this,after);
        connect(source,&QAbstractItemModel::rowsAboutToBeRemoved,this,before);
        connect(source,&QAbstractItemModel::rowsRemoved,this,after);
        connect(source,&QAbstractItemModel::columnsAboutToBeInserted,this,before);
        connect(source,&QAbstractItemModel::columnsInserted,this,after);
        connect(source,&QAbstractItemModel::columnsAboutToBeRemoved,this,before);
        connect(source,&QAbstractItemModel::columnsRemoved,this,after);
        connect(source,&QAbstractItemModel::dataChanged,this,[this](const QModelIndex &first,const QModelIndex &last,const QList<int> &roles){
            if(first.row()==0 && rowCount()>0)
                emit dataChanged(index(0,first.column()),index(0,last.column()),roles);
        });
    }
    int rowCount(const QModelIndex &parent={}) const override { return parent.isValid()?0:qMin(1,m_source->rowCount()); }
    int columnCount(const QModelIndex &parent={}) const override { return parent.isValid()?0:m_source->columnCount(); }
    QVariant data(const QModelIndex &cell,int role=Qt::DisplayRole) const override {
        return cell.isValid()?m_source->data(m_source->index(0,cell.column()),role):QVariant{};
    }
    Qt::ItemFlags flags(const QModelIndex &cell) const override {
        return cell.isValid()?(m_source->flags(m_source->index(0,cell.column())) & ~Qt::ItemIsEditable):Qt::NoItemFlags;
    }
private:
    QAbstractItemModel *m_source;
};

class FrozenRowProbe : public QObject {
public:
    explicit FrozenRowProbe(QTableView *grid) : QObject(grid), model(grid->model()), view(grid), m_grid(grid) {
        view.setModel(&model);
        view.setEditTriggers(QAbstractItemView::NoEditTriggers);
        view.setFocusPolicy(Qt::NoFocus);
        view.setAttribute(Qt::WA_TransparentForMouseEvents);
        view.setFrameShape(QFrame::NoFrame);
        view.horizontalHeader()->hide(); view.verticalHeader()->hide();
        view.setHorizontalScrollBarPolicy(Qt::ScrollBarAlwaysOff);
        view.setVerticalScrollBarPolicy(Qt::ScrollBarAlwaysOff);
        view.setHorizontalScrollMode(grid->horizontalScrollMode());
        view.setFont(grid->font());
        view.horizontalHeader()->setDefaultSectionSize(grid->horizontalHeader()->defaultSectionSize());
        view.setRowHeight(0,grid->rowHeight(0));
        // Column geometry is bounded by the 16,384-column GUI extent. No cell
        // reads or million-row hide/show loops are needed to make this view.
        for(int col=0;col<model.columnCount();++col)
            if(grid->columnWidth(col)!=view.columnWidth(col)) view.setColumnWidth(col,grid->columnWidth(col));
        connect(grid->horizontalHeader(),&QHeaderView::sectionResized,&view,[this](int col,int,int size){view.setColumnWidth(col,size);});
        connect(grid->verticalHeader(),&QHeaderView::sectionResized,&view,[this](int row,int,int size){
            if(row==0) { view.setRowHeight(0,size); geometry(); }
        });
        connect(grid->horizontalScrollBar(),&QScrollBar::valueChanged,&view,[this](int value){
            view.horizontalScrollBar()->setValue(value); geometry();
        });
        connect(grid->verticalScrollBar(),&QScrollBar::valueChanged,&view,[this]{geometry();});
        grid->viewport()->installEventFilter(this);
        geometry(); view.show(); view.raise();
    }
    void geometry() { view.setGeometry(QRect(m_grid->viewport()->pos(),QSize(m_grid->viewport()->width(),m_grid->rowHeight(0)))); }
    FirstRowModel model;
    QTableView view;
protected:
    bool eventFilter(QObject *object,QEvent *event) override {
        if(object==m_grid->viewport() && (event->type()==QEvent::Resize || event->type()==QEvent::Move)) geometry();
        return QObject::eventFilter(object,event);
    }
private:
    QTableView *m_grid;
};
