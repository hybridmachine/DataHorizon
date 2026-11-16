// SPDX-License-Identifier: GPL-3.0-only
#pragma once

#include <QHash>
#include <QJsonArray>
#include <QJsonObject>
#include <QList>
#include <QPointer>
#include <QSet>
#include <QString>
#include <QStringList>
#include <QWidget>
#include <functional>

class ChartSourceField;
class QComboBox;
class QFormLayout;
class QGroupBox;
class QLabel;
class QLineEdit;
class QPushButton;
class QStackedWidget;
class QToolButton;
class QVBoxLayout;

// The side panel that edits one chart: its name and stacking order, type and
// grouping, title, data (categories and series, in chartDataGroup), axes and
// legend. It takes the catalog's chart in and asks for changes out; it sends
// nothing itself and knows no window.
//
// The Data group edits a working copy of the specification. A completed
// source edit (a range applied with Enter or focus-out, both lists chosen, a
// name's text), a Remove or a Move asks for one change carrying the whole
// specification. Add Series adds a row to the panel only; it is sent with
// the first change after its values (and X, for a scatter chart) are given.
// While a source of the chart is broken, edits stay in the working copy,
// marked "Not applied yet", until none is broken; then one change carries
// them all. A refused change undoes that change alone. The same chart
// renamed, moved or reordered keeps the working copy; with another
// specification it starts again from it, keeping added rows not yet
// complete; another chart drops it.
//
// A choice (a type button, Grouping, Legend, Bring to Front, Send to Back)
// asks for its change when it changes; a text field on Enter or when it loses
// focus, and only when its text differs from the chart's stored value. Each
// request is one chart_update: the name, the order, or the whole input-form
// specification built from the stored chart. While a request is answered the
// panel asks for nothing else, until settle (a fresh catalog was shown) or
// refused ends the wait.
//
// Text left in a field while the panel was locked or waiting is applied when
// it can ask again, if the same chart is still shown with the specification
// it was typed against (and, for Name, the same name) and the text still
// differs; otherwise the field shows the chart as now stored. A field that had focus
// when the panel locked gets it back on unlock, unless the person pressed a
// key or a mouse button outside the panel meanwhile, or focus moved.
//
// A chart whose specification cannot be built (unreadable, or with a broken
// source) shows its reason at the top, and only the Chart group is enabled,
// with the Data group too when the only problem is a broken source.
class ChartPanel : public QWidget {
    Q_OBJECT
public:
    explicit ChartPanel(QWidget *parent=nullptr);
    // The chart to show; {} shows "No chart selected". The same chart again
    // changes nothing, so a field being edited keeps its text; any other
    // chart reloads every field and drops unsent text, except held text whose
    // specification is unchanged. A request still
    // awaited stays awaited.
    void setChart(const QJsonObject &chart);
    // The awaited request is answered and the catalog shown is fresh: the
    // panel may ask again, first for text held while it waited.
    void settle();
    QString chartId() const;
    // Busy or read-only: every control disabled, values kept.
    void setLocked(bool locked);
    // Puts the named field back to the chart's value and shows the message
    // beneath it until the chart changes or the field is edited again. An
    // empty message only puts the field back. Either ends the wait.
    void refused(const QString &field,const QString &message);
    // What the Data group's fields offer: sheets as [{id, name}], tables
    // and queries as POST /tables lists them (a query may carry its
    // result columns as "shape": [{name}]).
    // A saved query's columns are not listed by /tables: they are asked for
    // with queryColumnsWanted when a query is chosen (or a stored one's
    // column list is opened) and given here. They are kept until setCatalog
    // describes that query differently.
    void setCatalog(const QJsonArray &sheets,const QJsonArray &tables,const QJsonArray &queries);
    void setQueryColumns(const QString &queryId,const QStringList &columns);
    // The read of a saved query's columns failed: they are asked for again
    // the next time they are wanted.
    void queryColumnsFailed(const QString &queryId);
    // A range pick on the grid for a Range or Cell source field, so a source
    // is chosen by selecting cells as for a formula.
    // beginPick marks the field (highlighted, read-only, Pick pressed); it
    // does nothing unless the field shows a Range or Cell. setPicked shows a
    // picked range in the field as if typed, bare on the chart's sheet and
    // sheet-qualified otherwise; nothing is sent. endPick(true) applies the
    // field as a typed range is applied on Enter (one change, a held edit
    // while a source is broken, or completing an added row); endPick(false)
    // puts the field back. Either focuses the field when asked.
    void beginPick(const QString &field);
    void setPicked(const QString &field,const QJsonObject &rangeSource);
    void endPick(bool apply,bool focus);
    // The field being picked; empty when none.
    QString pickField() const { return m_picking; }
    // A source field's label: "Categories", "Series 1 Name", "Series 1 Values", "Series 1 X".
    static QString fieldLabel(const QString &field);
signals:
    // changes holds exactly the chart_update fields: name, order or spec.
    // field names the control (chartName, chartOrder, chartType,
    // chartGrouping, chartTitle, categoryAxisTitle, valueAxisTitle,
    // valueAxisMin, valueAxisMax, legendPosition; for the Data group
    // categoriesSource, series<n>Name, series<n>Values, series<n>X, or
    // "series" for a Remove or a Move).
    void changeRequested(const QString &chartId,const QJsonObject &changes,const QString &field);
    // Pick on a Range or Cell source field (categoriesSource, series<n>Name,
    // series<n>Values, series<n>X).
    void pickRequested(const QString &field);
    // The columns of a saved query are wanted (answered with setQueryColumns).
    void queryColumnsWanted(const QString &queryId);
    // The panel ended a pick itself: the Data group was shown afresh (a
    // series added, removed or moved, or another specification), so the
    // field may be another one now. Its content is the working copy's.
    void pickEnded();
private:
    // keepData: the same chart and specification; the Data group's working copy stays.
    void load(bool keepData=false);
    void loadField(const QString &field);
    void updateEnabled();
    void showMessage(const QString &field,const QString &message);
    void clearMessage(const QString &field);
    bool canRequest() const;
    void request(const QJsonObject &changes,const QString &field);
    // The stored spec with one change, sent whole in input form.
    void requestSpec(const QString &field,const std::function<void(QJsonObject &)> &change);
    void applyName();
    void applyText(const QString &field);
    void applyBound(const QString &field);
    void applyType(const QString &type);
    // A text field's apply, by field name.
    void apply(const QString &field);
    // Holds a text field whose apply could not ask now.
    void defer(const QString &field);
    // Once the panel may ask: focus back, then the held fields.
    void resume();
    QLabel *messageLabel(const QString &field);
    // While a focus is to be restored: notes a key or mouse press outside the panel.
    bool eventFilter(QObject *object,QEvent *event) override;
    void forgetFocus();
    // A button or list of the panel: a Tab stop on every platform, and a list ignores the wheel.
    void takeKeyboard(QWidget *control);

    // The Data group.
    struct SeriesBox {
        QGroupBox *box=nullptr;QFormLayout *form=nullptr;
        ChartSourceField *name=nullptr,*values=nullptr,*x=nullptr;
        QPushButton *remove=nullptr,*up=nullptr,*down=nullptr;
    };
    // What the panel knows of a working series beyond its specification.
    // id names the row for as long as the panel shows it, whatever its place.
    struct SeriesState { bool panelOnly=false;QSet<QString> edited;int id=0; };
    SeriesState newRow(bool panelOnly) { return SeriesState{panelOnly,{},++m_lastRow}; }
    int m_lastRow=0;
    // A source field (categoriesSource, series<n>Name, series<n>Values,
    // series<n>X): its 0-based series (-1 for the categories) and part.
    static bool dataSlot(const QString &field,int *row,QString *part);
    ChartSourceField *sourceField(const QString &field) const;
    QJsonValue holder(const QString &field) const;
    void setHolder(const QString &field,const QJsonValue &value);
    // The working copy afresh from the chart.
    void loadData();
    // Every Data field from the working copy.
    void showData();
    void showSource(const QString &field);
    void ensureSeries(int count);
    void updateMarkers();
    void orderFocus();
    bool scatter() const;
    // The working copy as it would be sent: series not yet complete left out.
    QJsonObject sendable() const;
    void applySource(const QString &field);
    // Sends the working copy when nothing is broken and it changed.
    void commitData(const QString &field);
    void removeSeries(int row);
    void moveSeries(int row,int by);
    void addSeries();
    // A change refused in the panel: the field shows the working copy again,
    // with the message beneath it.
    void refuseLocally(const QString &field,const QString &message);
    QWidget *withMarkers(ChartSourceField *field);
    void connectSource(ChartSourceField *source);
    static constexpr int MaxSeries=64;
    // Values (and X, for a scatter chart) given.
    bool complete(const QJsonValue &series) const;
    // Notes the working copy before an edit that may be sent, so a refusal undoes that edit alone.
    void remember();
    void clearSeriesMessages();
    QJsonArray queriesWithColumns() const;
    QJsonObject m_working;
    QList<SeriesState> m_rows;
    bool m_categoriesEdited=false,m_seriesHeld=false;
    struct Before {
        bool valid=false;
        QJsonObject chart,working;
        QList<SeriesState> rows;
        bool categoriesEdited=false,seriesHeld=false;
    } m_before;
    QHash<QString,QStringList> m_queryColumns;
    QHash<QString,QJsonObject> m_querySummaries;
    QSet<QString> m_queriesWanted;
    QHash<QString,QString> m_sheetNames,m_sheetIds;
    QJsonArray m_tables,m_queries;
    // Names of the tables, columns and queries the chart's stored sources give.
    QHash<QString,QString> m_storedNames;
    ChartSourceField *m_categories=nullptr;
    QFormLayout *m_categoriesForm=nullptr;
    QVBoxLayout *m_seriesLayout=nullptr;
    QList<SeriesBox> m_series;
    QPushButton *m_addSeries=nullptr;
    QLabel *m_seriesMarker=nullptr;
    QHash<QString,QLabel *> m_markers;
    QJsonObject m_chart;
    // The input-form spec; empty when it cannot be built (m_reason says why).
    QJsonObject m_spec;
    QString m_reason;
    bool m_locked=false,m_pending=false;
    // At most one held field: it is the one that had focus, and only the
    // focused field finishes editing, so a second one cannot arise.
    QString m_deferred,m_deferredChart,m_focusChart;
    bool m_userActed=false;
    // The panel widget that had focus when the panel locked, and where focus went then.
    QPointer<QWidget> m_focus,m_focusAfterLock;
    QStackedWidget *m_pages=nullptr;
    QLabel *m_reasonLabel=nullptr;
    QGroupBox *m_chartGroup=nullptr,*m_typeGroup=nullptr,*m_titleGroup=nullptr,*m_axesGroup=nullptr,*m_legendGroup=nullptr;
    QWidget *m_dataGroup=nullptr;
    QLineEdit *m_name=nullptr,*m_title=nullptr,*m_categoryTitle=nullptr,*m_valueTitle=nullptr,*m_min=nullptr,*m_max=nullptr;
    QPushButton *m_front=nullptr,*m_back=nullptr;
    QHash<QString,QToolButton *> m_typeButtons;
    QComboBox *m_grouping=nullptr,*m_legend=nullptr;
    QHash<QString,QLabel *> m_messages;
    // The field being picked, and the range last picked into a field with
    // the text it shows: that text applies as that range.
    QString m_picking;
    struct Picked { QString field,text;QJsonObject source; } m_picked;
};
