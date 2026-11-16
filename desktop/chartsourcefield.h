// SPDX-License-Identifier: GPL-3.0-only
#pragma once

#include <QJsonArray>
#include <QJsonObject>
#include <QList>
#include <QString>
#include <QWidget>

class QComboBox;
class QLineEdit;
class QStackedWidget;
class QToolButton;

// One source field of the chart panel's Data group: a kind list, then a
// text field with a Pick button (Range, or a series name's Cell; Text has no
// Pick) or two lists (a table or saved query, then its column). It shows
// what the panel gives it and reports what the person does; it holds no
// chart and sends nothing.
//
// Inner object names, from the field's own name <field>: <field>Kind (the
// kind list), <field>Range (the text field, also for Text), <field>Pick,
// <field>Table and <field>TableColumn, <field>Query and <field>QueryColumn.
class ChartSourceField : public QWidget {
    Q_OBJECT
public:
    // label is the field's label in accessible names ("Categories",
    // "Series 1 Values"). A series name offers Text and Cell; any other
    // field Range, Table column and Query column.
    ChartSourceField(const QString &name,const QString &label,bool seriesName,QWidget *parent=nullptr);
    // "text", "cell", "range", "table" or "query".
    QString kind() const;
    QString text() const;
    // The text was typed since it was last shown.
    bool textModified() const;
    // The table or query chosen, and its column (a table column's id, a
    // query column's name); empty until chosen.
    QString chosenId() const;
    QString chosenColumn() const;
    // The controls that take input, in focus order.
    QList<QWidget *> inputs() const;
    // Shows a text, cell or range value.
    void showText(const QString &kind,const QString &text);
    // Shows a table or query column. The names given are shown even when
    // the catalog does not hold that table, query or column.
    void showChoice(const QString &kind,const QString &id,const QString &name,const QString &column,const QString &columnName);
    // Tables as [{id, name, columns: [{id, name}]}]; queries as
    // [{id, name}], with "shape": [{name}] once their columns are known.
    void setCatalog(const QJsonArray &tables,const QJsonArray &queries);
    bool queryColumnsKnown(const QString &id) const;
signals:
    // Enter or focus-out in the text field.
    void textFinished();
    void textEdited();
    // Both lists are chosen.
    void choiceMade();
    // Another kind was chosen; the field it shows is cleared.
    void kindChosen();
    void pickClicked();
    // A query was chosen, or a shown query's column list opened, and its
    // columns are not known yet.
    void queryColumnsWanted(const QString &queryId);
    // Focus left the field while a kind change was still incomplete.
    void abandoned();
private:
    bool eventFilter(QObject *object,QEvent *event) override;
    void showKind(const QString &kind);
    // Fills the object list of the page (table or query), keeping the shown choice.
    void fillObjects(const QString &kind);
    void fillColumns(const QString &kind);
    QComboBox *objects(const QString &kind) const;
    QComboBox *columns(const QString &kind) const;
    QComboBox *m_kind=nullptr,*m_table=nullptr,*m_tableColumn=nullptr,*m_query=nullptr,*m_queryColumn=nullptr;
    QLineEdit *m_edit=nullptr;
    QToolButton *m_pick=nullptr;
    QStackedWidget *m_pages=nullptr;
    QJsonArray m_tables,m_queries;
    // The table or query choice last shown, with its stored names.
    QString m_shownKind,m_shownId,m_shownName,m_shownColumn,m_shownColumnName;
    // Another kind was chosen and nothing has been shown or completed since.
    bool m_kindChanged=false;
};
