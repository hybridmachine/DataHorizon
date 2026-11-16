// SPDX-License-Identifier: GPL-3.0-only
#include "chartsourcefield.h"
#include <QApplication>
#include <QComboBox>
#include <QFocusEvent>
#include <QTimer>
#include <QHBoxLayout>
#include <QLineEdit>
#include <QStackedWidget>
#include <QToolButton>

namespace {
int pageOf(const QString &kind) { return kind=="table"?1:kind=="query"?2:0; }
}

ChartSourceField::ChartSourceField(const QString &name,const QString &label,bool seriesName,QWidget *parent) : QWidget(parent) {
    setObjectName(name);
    auto *row=new QHBoxLayout(this);row->setContentsMargins(0,0,0,0);
    m_kind=new QComboBox;m_kind->setObjectName(name+"Kind");m_kind->setAccessibleName(tr("%1 kind").arg(label));
    if(seriesName){m_kind->addItem(tr("Text"),"text");m_kind->addItem(tr("Cell"),"cell");}
    else{m_kind->addItem(tr("Range"),"range");m_kind->addItem(tr("Table column"),"table");m_kind->addItem(tr("Query column"),"query");}
    row->addWidget(m_kind);
    m_pages=new QStackedWidget;row->addWidget(m_pages,1);
    auto *text=new QWidget;auto *textRow=new QHBoxLayout(text);textRow->setContentsMargins(0,0,0,0);
    m_edit=new QLineEdit;m_edit->setObjectName(name+"Range");m_edit->setAccessibleName(label);
    // A name's text is at most 255 characters, as every text field of the panel.
    if(seriesName)m_edit->setMaxLength(255);
    m_pick=new QToolButton;m_pick->setObjectName(name+"Pick");m_pick->setText(tr("Pick"));m_pick->setAccessibleName(tr("Pick %1 on the grid").arg(label));
    textRow->addWidget(m_edit,1);textRow->addWidget(m_pick);
    m_pages->addWidget(text);
    auto lists=[&](QComboBox *&first,QComboBox *&second,const QString &suffix,const QString &what) {
        auto *page=new QWidget;auto *pageRow=new QHBoxLayout(page);pageRow->setContentsMargins(0,0,0,0);
        first=new QComboBox;first->setObjectName(name+suffix);first->setAccessibleName(tr("%1 %2").arg(label,what));
        second=new QComboBox;second->setObjectName(name+suffix+"Column");second->setAccessibleName(tr("%1 column").arg(label));
        for(auto *combo:{first,second}){combo->setSizeAdjustPolicy(QComboBox::AdjustToMinimumContentsLengthWithIcon);combo->setMinimumContentsLength(2);}
        pageRow->addWidget(first,1);pageRow->addWidget(second,1);m_pages->addWidget(page);
    };
    lists(m_table,m_tableColumn,"Table",tr("table"));
    lists(m_query,m_queryColumn,"Query",tr("query"));
    setFocusProxy(m_kind);
    showKind(m_kind->itemData(0).toString());

    connect(m_edit,&QLineEdit::editingFinished,this,&ChartSourceField::textFinished);
    connect(m_edit,&QLineEdit::textEdited,this,&ChartSourceField::textEdited);
    connect(m_pick,&QToolButton::clicked,this,&ChartSourceField::pickClicked);
    connect(m_kind,&QComboBox::activated,this,[this]{
        // A kind chosen again keeps what is shown.
        const auto kind=m_kind->currentData().toString();
        if(m_pages->property("kind").toString()==kind)return;
        showKind(kind);
        m_edit->clear();
        for(auto *combo:{m_table,m_query})combo->setCurrentIndex(-1);
        for(auto *combo:{m_tableColumn,m_queryColumn})combo->clear();
        m_kindChanged=true;
        emit kindChosen();
    });
    for(const auto &kind:{QString("table"),QString("query")}) {
        connect(objects(kind),&QComboBox::activated,this,[this,kind]{
            fillColumns(kind);columns(kind)->setCurrentIndex(-1);
            if(kind=="query" && !queryColumnsKnown(m_query->currentData().toString()))emit queryColumnsWanted(m_query->currentData().toString());
        });
        connect(columns(kind),&QComboBox::activated,this,[this,kind]{
            if(objects(kind)->currentIndex()>=0 && columns(kind)->currentIndex()>=0)emit choiceMade();
        });
    }
    // Focus leaving the field ends a kind change left incomplete; opening a
    // stored query's column list asks for its columns.
    for(auto *input:inputs())input->installEventFilter(this);
}

bool ChartSourceField::queryColumnsKnown(const QString &id) const {
    for(const auto &value:m_queries)if(value.toObject()["id"].toString()==id)return value.toObject().contains("shape");
    return false;
}

bool ChartSourceField::eventFilter(QObject *object,QEvent *event) {
    if(object==m_queryColumn && (event->type()==QEvent::FocusIn || event->type()==QEvent::MouseButtonPress)) {
        const auto id=m_query->currentData().toString();
        if(m_query->currentIndex()>=0 && !id.isEmpty() && !queryColumnsKnown(id))emit queryColumnsWanted(id);
    }
    if(event->type()==QEvent::FocusOut && m_kindChanged && static_cast<QFocusEvent *>(event)->reason()!=Qt::PopupFocusReason)
        // Decided once focus has landed: a move within the field keeps the change.
        QTimer::singleShot(0,this,[this]{
            if(!m_kindChanged || QApplication::activePopupWidget())return;
            auto *focus=QApplication::focusWidget();
            if(focus && isAncestorOf(focus))return;
            m_kindChanged=false;
            emit abandoned();
        });
    return QWidget::eventFilter(object,event);
}

QString ChartSourceField::kind() const { return m_kind->currentData().toString(); }
QString ChartSourceField::text() const { return m_edit->text(); }
bool ChartSourceField::textModified() const { return m_edit->isModified(); }
QString ChartSourceField::chosenId() const { return objects(kind())?objects(kind())->currentData().toString():QString(); }
QString ChartSourceField::chosenColumn() const { return columns(kind())?columns(kind())->currentData().toString():QString(); }
QList<QWidget *> ChartSourceField::inputs() const { return {m_kind,m_edit,m_pick,m_table,m_tableColumn,m_query,m_queryColumn}; }

QComboBox *ChartSourceField::objects(const QString &kind) const { return kind=="table"?m_table:kind=="query"?m_query:nullptr; }
QComboBox *ChartSourceField::columns(const QString &kind) const { return kind=="table"?m_tableColumn:kind=="query"?m_queryColumn:nullptr; }

void ChartSourceField::showKind(const QString &kind) {
    m_kind->setCurrentIndex(m_kind->findData(kind));
    m_pages->setProperty("kind",kind);
    m_pages->setCurrentIndex(pageOf(kind));
    m_pick->setVisible(kind=="cell" || kind=="range");
}

void ChartSourceField::showText(const QString &kind,const QString &text) {
    showKind(kind);
    m_kindChanged=false;
    m_edit->setText(text);
    m_shownKind.clear();
}

void ChartSourceField::showChoice(const QString &kind,const QString &id,const QString &name,const QString &column,const QString &columnName) {
    showKind(kind);
    m_kindChanged=false;
    m_edit->clear();
    m_shownKind=kind;m_shownId=id;m_shownName=name;m_shownColumn=column;m_shownColumnName=columnName;
    fillObjects(kind);
    objects(kind)->setCurrentIndex(objects(kind)->findData(id));
    fillColumns(kind);
    columns(kind)->setCurrentIndex(columns(kind)->findData(column));
}

void ChartSourceField::setCatalog(const QJsonArray &tables,const QJsonArray &queries) {
    m_tables=tables;m_queries=queries;
    for(const auto &kind:{QString("table"),QString("query")}) {
        const auto id=objects(kind)->currentData().toString(),column=columns(kind)->currentData().toString();
        const bool chosen=objects(kind)->currentIndex()>=0,columnChosen=columns(kind)->currentIndex()>=0;
        fillObjects(kind);
        objects(kind)->setCurrentIndex(chosen?objects(kind)->findData(id):-1);
        if(chosen)fillColumns(kind);else columns(kind)->clear();
        columns(kind)->setCurrentIndex(columnChosen?columns(kind)->findData(column):-1);
    }
}

void ChartSourceField::fillObjects(const QString &kind) {
    auto *combo=objects(kind);
    combo->clear();
    bool shown=false;
    for(const auto &value:kind=="table"?m_tables:m_queries) {
        const auto item=value.toObject();
        combo->addItem(item["name"].toString(),item["id"].toString());
        shown=shown || item["id"].toString()==m_shownId;
    }
    // A table or query the chart refers to that the catalog lacks, by its
    // stored name (none for one that no longer exists).
    if(m_shownKind==kind && !shown)combo->addItem(m_shownName,m_shownId);
}

void ChartSourceField::fillColumns(const QString &kind) {
    auto *combo=columns(kind);
    combo->clear();
    const auto id=objects(kind)->currentData().toString();
    if(objects(kind)->currentIndex()<0)return;
    bool shown=false;
    for(const auto &value:kind=="table"?m_tables:m_queries) {
        const auto item=value.toObject();
        if(item["id"].toString()!=id)continue;
        if(kind=="table") {
            for(const auto &column:item["columns"].toArray()) {
                combo->addItem(column.toObject()["name"].toString(),column.toObject()["id"].toString());
                shown=shown || column.toObject()["id"].toString()==m_shownColumn;
            }
        } else {
            for(const auto &column:item["shape"].toArray()) {
                combo->addItem(column.toObject()["name"].toString(),column.toObject()["name"].toString());
                shown=shown || column.toObject()["name"].toString()==m_shownColumn;
            }
        }
    }
    if(m_shownKind==kind && m_shownId==id && !shown)combo->addItem(m_shownColumnName,m_shownColumn);
    // A query's columns are read when wanted; until then only its stored column, if any.
    combo->setEnabled(kind=="table" || combo->count()>0);
}
