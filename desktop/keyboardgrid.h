// SPDX-License-Identifier: GPL-3.0-only
#pragma once
#include <QAccessibleWidget>
#include <QTableView>

// A spreadsheet's conceptual extent is not a materialized accessibility table.
// Expose a bounded keyboard surface and its viewport/editor. Source/result
// inspection uses the named address/input controls and Read current cell action.
class KeyboardGridView : public QTableView {
public:
    explicit KeyboardGridView(QWidget *parent=nullptr);
};

class KeyboardGridAccessible : public QAccessibleWidget {
public:
    explicit KeyboardGridAccessible(KeyboardGridView *view)
        : QAccessibleWidget(view,QAccessible::Pane),m_view(view) {}
    int childCount() const override {return 1;}
    QAccessibleInterface *child(int index) const override {
        return index==0?QAccessible::queryAccessibleInterface(m_view->viewport()):nullptr;
    }
    int indexOfChild(const QAccessibleInterface *interface) const override {
        return interface && interface->object()==m_view->viewport()?0:-1;
    }
    QAccessibleInterface *childAt(int x,int y) const override {
        auto *viewport=child(0);if(!viewport || !viewport->rect().contains(x,y))return nullptr;
        auto *nested=viewport->childAt(x,y);return nested?nested:viewport;
    }
    QAccessibleInterface *focusChild() const override {
        auto *focus=m_view->focusWidget();
        return focus && m_view->viewport()->isAncestorOf(focus)?QAccessible::queryAccessibleInterface(focus):nullptr;
    }
    QString text(QAccessible::Text role) const override {
        if(role==QAccessible::Description) {
            const auto index=m_view->currentIndex();
            const auto cell=index.isValid()?index.data(Qt::AccessibleDescriptionRole).toString():QStringLiteral("No current cell");
            return cell+QStringLiteral(". Use Cell address to navigate, Cell input or formula to edit, or Read current cell to inspect the result.");
        }
        return QAccessibleWidget::text(role);
    }
private:
    KeyboardGridView *m_view;
};

inline KeyboardGridView::KeyboardGridView(QWidget *parent):QTableView(parent) {
    static const bool installed=[] {
        QAccessible::installFactory([](const QString &,QObject *object)->QAccessibleInterface * {
            auto *view=dynamic_cast<KeyboardGridView *>(object);
            return view?new KeyboardGridAccessible(view):nullptr;
        });
        return true;
    }();
    Q_UNUSED(installed);
}
