// SPDX-License-Identifier: GPL-3.0-only
#pragma once
#include <QApplication>
#include <QKeyEvent>
#include <QMouseEvent>
#include <QPersistentModelIndex>
#include <QPointer>
#include <QScopedValueRollback>
#include <QSignalBlocker>
#include <QScrollBar>
#include <QTableView>
#include <QTimer>

// Prefix panes retain source row/column indices. The body owns the canonical
// current cell and rectangle; proxies mirror intersections without data caches.
class PaneSelection : public QObject {
public:
    explicit PaneSelection(QTableView *body,QObject *parent=nullptr):QObject(parent),m_body(body) {
        m_views.append(body);install(body);
        connect(body->selectionModel(),&QItemSelectionModel::currentChanged,this,[this](const QModelIndex &cell){
            if(!m_enabled || m_selecting)return;
            m_anchor=cell;sync();
        });
        connect(body->selectionModel(),&QItemSelectionModel::selectionChanged,this,[this]{sync();});
        connect(body->model(),&QAbstractItemModel::modelReset,this,[this]{m_anchor=QModelIndex();m_dragging=false;sync();});
    }
    void addPane(QTableView *pane) {
        m_views.append(pane);install(pane);
        connect(pane->selectionModel(),&QItemSelectionModel::currentChanged,this,[this,pane](const QModelIndex &cell){
            if(m_enabled && !m_selecting && pane->isVisible() && cell.isValid())select(source(cell),false,pane);
        });
        connect(pane->selectionModel(),&QItemSelectionModel::selectionChanged,this,[this,pane]{
            if(!m_enabled || m_selecting || !pane->isVisible())return;
            QItemSelection selection;
            for(const auto &r:pane->selectionModel()->selection())selection.select(source(r.topLeft()),source(r.bottomRight()));
            if(!selection.isEmpty()) {
                QScopedValueRollback<bool> selecting(m_selecting,true);
                m_body->selectionModel()->select(selection,QItemSelectionModel::ClearAndSelect);
                // Qt emits selectionChanged before currentChanged. Do not reset
                // the originating proxy's current cell inside that emission.
                QTimer::singleShot(0,this,[this]{sync();});
            }
        });
    }
    void setEnabled(bool enabled) {m_enabled=enabled;m_dragging=false;m_anchor=m_body->currentIndex();if(enabled)sync();}
    void sync() {
        if(!m_enabled || m_selecting)return;
        QScopedValueRollback<bool> selecting(m_selecting,true);
        for(auto *pane:m_views) {
            if(pane==m_body)continue;
            const auto current=m_body->currentIndex();QModelIndex local;
            if(current.isValid())local=pane->model()->index(current.row(),current.column());
            QSignalBlocker blocker(pane->selectionModel());
            pane->selectionModel()->setCurrentIndex(local,QItemSelectionModel::NoUpdate);
            QItemSelection intersection;
            for(const auto &r:m_body->selectionModel()->selection()) {
                const int bottom=qMin(r.bottom(),pane->model()->rowCount()-1),right=qMin(r.right(),pane->model()->columnCount()-1);
                if(r.top()<=bottom && r.left()<=right)intersection.select(pane->model()->index(r.top(),r.left()),pane->model()->index(bottom,right));
            }
            pane->selectionModel()->select(intersection,QItemSelectionModel::ClearAndSelect);
        }
    }
protected:
    bool eventFilter(QObject *object,QEvent *event) override {
        if(!m_enabled)return false;
        QTableView *view=nullptr;
        for(auto *candidate:m_views)if(object==candidate || object==candidate->viewport()){view=candidate;break;}
        if(!view || !view->isVisible())return false;
        if(event->type()==QEvent::MouseButtonPress && object==view->viewport()) {
            const auto *mouse=static_cast<QMouseEvent *>(event);
            if(mouse->button()!=Qt::LeftButton || mouse->modifiers()&~Qt::ShiftModifier)return false;
            const auto cell=view->indexAt(mouse->position().toPoint());if(!cell.isValid())return false;
            m_dragging=select(source(cell),mouse->modifiers().testFlag(Qt::ShiftModifier),view);return true;
        }
        if(event->type()==QEvent::MouseMove && m_dragging) {
            const auto *mouse=static_cast<QMouseEvent *>(event);
            if(!mouse->buttons().testFlag(Qt::LeftButton)){m_dragging=false;return false;}
            for(auto *pane:m_views) {
                if(!pane->isVisible())continue;
                const auto point=pane->viewport()->mapFromGlobal(mouse->globalPosition().toPoint());
                if(!pane->viewport()->rect().contains(point))continue;
                const auto cell=pane->indexAt(point);if(cell.isValid())select(source(cell),true,pane);break;
            }
            return true;
        }
        if(event->type()==QEvent::MouseButtonRelease && m_dragging) {m_dragging=false;return true;}
        if(event->type()==QEvent::KeyPress && object==view) {
            const auto *key=static_cast<QKeyEvent *>(event);
            if(key->modifiers()&~Qt::ShiftModifier)return false;
            int dr=0,dc=0;
            if(key->key()==Qt::Key_Down)dr=1;else if(key->key()==Qt::Key_Up)dr=-1;
            else if(key->key()==Qt::Key_Right)dc=1;else if(key->key()==Qt::Key_Left)dc=-1;else return false;
            auto cell=m_body->currentIndex();if(!cell.isValid())return false;
            do {cell=m_body->model()->index(cell.row()+dr,cell.column()+dc);}while(cell.isValid() && !paneFor(cell));
            if(cell.isValid())select(cell,key->modifiers().testFlag(Qt::ShiftModifier),paneFor(cell));
            return true;
        }
        return false;
    }
private:
    void install(QTableView *view){view->installEventFilter(this);view->viewport()->installEventFilter(this);}
    QModelIndex source(const QModelIndex &cell)const{return m_body->model()->index(cell.row(),cell.column());}
    QTableView *paneFor(const QModelIndex &cell)const {
        for(auto *view:m_views)if(view->isVisible() && cell.row()<view->model()->rowCount() && cell.column()<view->model()->columnCount()
            && !view->isRowHidden(cell.row()) && !view->isColumnHidden(cell.column()))return view;
        return nullptr;
    }
    bool select(const QModelIndex &cell,bool extend,QTableView *pane) {
        if(!cell.isValid())return false;
        const auto previous=m_body->selectionModel()->selection();
        const int vertical=m_body->verticalScrollBar()->value(),horizontal=m_body->horizontalScrollBar()->value();
        if(!m_anchor.isValid())m_anchor=m_body->currentIndex();
        QScopedValueRollback<bool> selecting(m_selecting,true);
        m_body->selectionModel()->setCurrentIndex(cell,QItemSelectionModel::NoUpdate);
        if(m_body->currentIndex()!=cell) {m_body->selectionModel()->select(previous,QItemSelectionModel::ClearAndSelect);m_selecting=false;sync();return false;}
        if(!extend || !m_anchor.isValid())m_anchor=cell;
        const int r1=qMin(cell.row(),m_anchor.row()),r2=qMax(cell.row(),m_anchor.row());
        const int c1=qMin(cell.column(),m_anchor.column()),c2=qMax(cell.column(),m_anchor.column());
        m_body->selectionModel()->select(QItemSelection(m_body->model()->index(r1,c1),m_body->model()->index(r2,c2)),QItemSelectionModel::ClearAndSelect);
        if(pane && pane!=m_body) {
            if(pane->model()->rowCount()<m_body->model()->rowCount())m_body->verticalScrollBar()->setValue(vertical);
            if(pane->model()->columnCount()<m_body->model()->columnCount())m_body->horizontalScrollBar()->setValue(horizontal);
        }
        m_selecting=false;sync();
        if(pane) {pane->scrollTo(pane->model()->index(cell.row(),cell.column()));pane->setFocus();}
        return true;
    }
    QTableView *m_body;
    QList<QTableView *> m_views;
    QPersistentModelIndex m_anchor;
    bool m_enabled=false,m_selecting=false,m_dragging=false;
};
