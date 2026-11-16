// SPDX-License-Identifier: GPL-3.0-only
#include "chartpanel.h"
#include "chartspec.h"
#include "chartsourcefield.h"
#include <QAbstractItemView>
#include <QApplication>
#include <QComboBox>
#include <QFormLayout>
#include <QGridLayout>
#include <QGroupBox>
#include <QHBoxLayout>
#include <QLabel>
#include <QLineEdit>
#include <QLocale>
#include <QRegularExpression>
#include <QPushButton>
#include <QStackedWidget>
#include <QTimer>
#include <QToolButton>
#include <QVBoxLayout>
#include <algorithm>
#include <cmath>

namespace {
const QStringList Types{"column","bar","line","area","pie","scatter"};
const QStringList TextFields{"chartTitle","categoryAxisTitle","valueAxisTitle"};

QJsonObject axesOf(const QJsonObject &spec) {
    auto axes=spec["axes"].toObject();
    if(!axes["category"].isObject())axes["category"]=QJsonObject{{"title",QJsonValue::Null}};
    if(!axes["value"].isObject())axes["value"]=QJsonObject{{"title",QJsonValue::Null},{"min",QJsonValue::Null},{"max",QJsonValue::Null}};
    return axes;
}
// The spec value a text field shows: the title, or an axis title.
QJsonValue textValue(const QJsonObject &spec,const QString &field) {
    if(field=="chartTitle")return spec["title"].toObject()["text"];
    const auto axes=axesOf(spec);
    if(field=="categoryAxisTitle")return axes["category"].toObject()["title"];
    return axes["value"].toObject()["title"];
}
QString number(const QJsonValue &value) {
    if(!value.isDouble())return {};
    // A whole number is shown exactly; toInteger gives its default for any other.
    if(value.toInteger(1)==value.toInteger(0))return QString::number(value.toInteger());
    return QString::number(value.toDouble(),'g',QLocale::FloatingPointShortest);
}
QWidget *withMessage(QWidget *field,QLabel *message) {
    auto *box=new QWidget;auto *layout=new QVBoxLayout(box);layout->setContentsMargins(0,0,0,0);layout->setSpacing(2);
    layout->addWidget(field);layout->addWidget(message);box->setFocusProxy(field);
    return box;
}
}

ChartPanel::ChartPanel(QWidget *parent) : QWidget(parent) {
    setObjectName("chartPanel");
    auto *outer=new QVBoxLayout(this);outer->setContentsMargins(0,0,0,0);
    m_pages=new QStackedWidget;outer->addWidget(m_pages);
    auto *none=new QLabel(tr("No chart selected"));none->setAlignment(Qt::AlignCenter);
    m_pages->addWidget(none);
    auto *page=new QWidget;auto *column=new QVBoxLayout(page);m_pages->addWidget(page);
    m_reasonLabel=new QLabel;m_reasonLabel->setObjectName("chartPanelReason");m_reasonLabel->setWordWrap(true);m_reasonLabel->hide();
    column->addWidget(m_reasonLabel);

    // Chart: Name, Bring to Front, Send to Back.
    m_chartGroup=new QGroupBox(tr("Chart"));auto *chartForm=new QFormLayout(m_chartGroup);
    m_name=new QLineEdit;m_name->setObjectName("chartName");m_name->setMaxLength(64);m_name->setAccessibleName(tr("Name"));
    chartForm->addRow(tr("Name"),withMessage(m_name,messageLabel("chartName")));
    m_front=new QPushButton(tr("Bring to Front"));m_front->setObjectName("chartToFront");
    m_back=new QPushButton(tr("Send to Back"));m_back->setObjectName("chartToBack");
    auto *order=new QHBoxLayout;order->addWidget(m_front);order->addWidget(m_back);order->addStretch();
    chartForm->addRow(order);chartForm->addRow(messageLabel("chartOrder"));
    column->addWidget(m_chartGroup);

    // Type: six buttons, exactly one pressed, and Grouping.
    m_typeGroup=new QGroupBox(tr("Type"));auto *typeForm=new QFormLayout(m_typeGroup);
    // Exactly one pressed, kept by the panel rather than by a QButtonGroup:
    // Qt's Tab passes over a group's unpressed buttons, and a group's arrow
    // keys would press (and so apply) each type in turn.
    auto *buttons=new QGridLayout;
    const QStringList labels{tr("Column"),tr("Bar"),tr("Line"),tr("Area"),tr("Pie"),tr("Scatter")};
    for(int i=0;i<Types.size();++i) {
        auto *button=new QToolButton;button->setObjectName("chartType_"+Types[i]);button->setText(labels[i]);button->setCheckable(true);
        button->setToolButtonStyle(Qt::ToolButtonTextOnly);button->setSizePolicy(QSizePolicy::Expanding,QSizePolicy::Fixed);
        m_typeButtons.insert(Types[i],button);buttons->addWidget(button,i/3,i%3);
    }
    typeForm->addRow(buttons);typeForm->addRow(messageLabel("chartType"));
    m_grouping=new QComboBox;m_grouping->setObjectName("chartGrouping");m_grouping->setAccessibleName(tr("Grouping"));
    m_grouping->addItem(tr("Clustered"),"clustered");m_grouping->addItem(tr("Stacked"),"stacked");
    typeForm->addRow(tr("Grouping"),withMessage(m_grouping,messageLabel("chartGrouping")));
    column->addWidget(m_typeGroup);

    // Title: one field, labelled by its group.
    m_titleGroup=new QGroupBox(tr("Title"));auto *titleForm=new QFormLayout(m_titleGroup);
    m_title=new QLineEdit;m_title->setObjectName("chartTitle");m_title->setMaxLength(255);m_title->setAccessibleName(tr("Title"));
    titleForm->addRow(withMessage(m_title,messageLabel("chartTitle")));
    column->addWidget(m_titleGroup);

    // Data: Categories, then the series list and Add Series.
    m_dataGroup=new QWidget;m_dataGroup->setObjectName("chartDataGroup");
    auto *data=new QVBoxLayout(m_dataGroup);data->setContentsMargins(0,0,0,0);
    auto *dataBox=new QGroupBox(tr("Data"));auto *dataColumn=new QVBoxLayout(dataBox);
    m_categoriesForm=new QFormLayout;
    m_categories=new ChartSourceField("categoriesSource",tr("Categories"),false);
    m_categoriesForm->addRow(tr("Categories"),withMarkers(m_categories));
    dataColumn->addLayout(m_categoriesForm);
    dataColumn->addWidget(new QLabel(tr("Series")));
    m_seriesLayout=new QVBoxLayout;m_seriesLayout->setContentsMargins(0,0,0,0);dataColumn->addLayout(m_seriesLayout);
    dataColumn->addWidget(messageLabel("series"));
    m_seriesMarker=new QLabel(tr("Not applied yet"));m_seriesMarker->setObjectName("seriesMarker");m_seriesMarker->hide();dataColumn->addWidget(m_seriesMarker);
    m_addSeries=new QPushButton(tr("Add Series"));m_addSeries->setObjectName("addSeries");
    auto *addRow=new QHBoxLayout;addRow->addWidget(m_addSeries);addRow->addStretch();dataColumn->addLayout(addRow);
    data->addWidget(dataBox);
    column->addWidget(m_dataGroup);

    // Axes: titles, Minimum and Maximum.
    m_axesGroup=new QGroupBox(tr("Axes"));auto *axesForm=new QFormLayout(m_axesGroup);
    auto line=[&](QLineEdit *&edit,const QString &name,const QString &label) {
        // No length cap: the service limits only the chart title (255) and the name.
        edit=new QLineEdit;edit->setObjectName(name);edit->setAccessibleName(label);
        axesForm->addRow(label,withMessage(edit,messageLabel(name)));
    };
    line(m_categoryTitle,"categoryAxisTitle",tr("Category title"));
    line(m_valueTitle,"valueAxisTitle",tr("Value title"));
    line(m_min,"valueAxisMin",tr("Minimum"));
    line(m_max,"valueAxisMax",tr("Maximum"));
    m_min->setPlaceholderText(tr("Automatic"));m_max->setPlaceholderText(tr("Automatic"));
    column->addWidget(m_axesGroup);

    // Legend: one list, labelled by its group.
    m_legendGroup=new QGroupBox(tr("Legend"));auto *legendForm=new QFormLayout(m_legendGroup);
    m_legend=new QComboBox;m_legend->setObjectName("legendPosition");m_legend->setAccessibleName(tr("Legend"));
    m_legend->addItem(tr("Right"),"right");m_legend->addItem(tr("Bottom"),"bottom");m_legend->addItem(tr("None"),"none");
    legendForm->addRow(withMessage(m_legend,messageLabel("legendPosition")));
    column->addWidget(m_legendGroup);
    column->addStretch();

    connect(m_name,&QLineEdit::editingFinished,this,&ChartPanel::applyName);
    connect(m_name,&QLineEdit::textEdited,this,[this]{clearMessage("chartName");});
    for(auto *edit:{m_title,m_categoryTitle,m_valueTitle}) {
        const auto field=edit->objectName();
        connect(edit,&QLineEdit::editingFinished,this,[this,field]{applyText(field);});
        connect(edit,&QLineEdit::textEdited,this,[this,field]{clearMessage(field);});
    }
    for(auto *edit:{m_min,m_max}) {
        const auto field=edit->objectName();
        connect(edit,&QLineEdit::editingFinished,this,[this,field]{applyBound(field);});
        connect(edit,&QLineEdit::textEdited,this,[this,field]{clearMessage(field);});
    }
    for(const auto &type:Types)
        connect(m_typeButtons.value(type),&QToolButton::clicked,this,[this,type]{
            for(auto button=m_typeButtons.cbegin();button!=m_typeButtons.cend();++button)button.value()->setChecked(button.key()==type);
            applyType(type);
        });
    for(const auto &[button,direction]:{std::pair{m_front,"front"},std::pair{m_back,"back"}})
        connect(button,&QPushButton::clicked,this,[this,direction=QString(direction)]{
            clearMessage("chartOrder");
            if(canRequest())request({{"order",direction}},"chartOrder");
        });
    for(auto *combo:{m_grouping,m_legend}) {
        const auto field=combo->objectName();
        connect(combo,&QComboBox::activated,this,[this,combo,field]{
            clearMessage(field);
            const auto value=combo->currentData().toString();
            const auto stored=field=="chartGrouping"?m_spec.value("grouping").toString():m_spec.value("legend").toObject().value("position").toString();
            if(!canRequest() || m_spec.isEmpty() || value==stored){loadField(field);return;}
            requestSpec(field,[field,value](QJsonObject &spec){
                if(field=="chartGrouping")spec["grouping"]=value;else spec["legend"]=QJsonObject{{"position",value}};
            });
        });
    }
    connect(m_addSeries,&QPushButton::clicked,this,&ChartPanel::addSeries);
    for(auto *button:{static_cast<QWidget *>(m_front),static_cast<QWidget *>(m_back),static_cast<QWidget *>(m_addSeries)})takeKeyboard(button);
    for(auto *button:m_typeButtons)takeKeyboard(button);
    for(auto *combo:{m_grouping,m_legend})takeKeyboard(combo);
    load();updateEnabled();
    orderFocus();
}

void ChartPanel::takeKeyboard(QWidget *control) {
    // A Tab stop whatever the platform's keyboard setting: macOS gives
    // buttons and lists Tab focus only, which its default setting skips.
    control->setFocusPolicy(Qt::StrongFocus);
    // A list takes no wheel: the wheel scrolls the panel (see eventFilter).
    if(qobject_cast<QComboBox *>(control))control->installEventFilter(this);
}

void ChartPanel::connectSource(ChartSourceField *source) {
    const auto field=source->objectName();
    connect(source,&ChartSourceField::textFinished,this,[this,field]{applySource(field);});
    connect(source,&ChartSourceField::choiceMade,this,[this,field]{clearMessage(field);applySource(field);});
    for(auto signal:{&ChartSourceField::textEdited,&ChartSourceField::kindChosen})connect(source,signal,this,[this,field]{clearMessage(field);});
    connect(source,&ChartSourceField::pickClicked,this,[this,field]{emit pickRequested(field);});
    // A kind changed and left incomplete shows the working source again.
    connect(source,&ChartSourceField::abandoned,this,[this,field]{showSource(field);});
    connect(source,&ChartSourceField::queryColumnsWanted,this,[this](const QString &id){
        if(id.isEmpty() || m_queryColumns.contains(id) || m_queriesWanted.contains(id))return;
        m_queriesWanted.insert(id);
        emit queryColumnsWanted(id);
    });
    source->setCatalog(m_tables,queriesWithColumns());
    for(auto *input:source->inputs())if(qobject_cast<QComboBox *>(input) || qobject_cast<QToolButton *>(input))takeKeyboard(input);
}

QWidget *ChartPanel::withMarkers(ChartSourceField *field) {
    const auto name=field->objectName();
    auto *marker=new QLabel;marker->setObjectName(name+"Marker");marker->setWordWrap(true);marker->hide();
    m_markers.insert(name,marker);
    auto *box=withMessage(field,messageLabel(name));
    box->layout()->addWidget(marker);
    connectSource(field);
    return box;
}

void ChartPanel::setChart(const QJsonObject &chart) {
    if(chart==m_chart)return;
    // The same chart renamed, moved or reordered keeps the Data group's
    // working copy (held repairs, added rows). The same chart with another
    // specification starts again from it, keeping only added rows not yet
    // complete; another chart drops everything. Held text stays, unshown
    // values kept in its field, only while the specification it was typed
    // against is unchanged (and, for Name, the name); otherwise its field
    // shows the chart as now stored.
    const bool same=!chart.isEmpty() && chart.value("id")==m_chart.value("id");
    const bool sameSpec=same && chart.value("spec")==m_chart.value("spec") && chart.value("issue")==m_chart.value("issue");
    const bool keepHeld=sameSpec && !m_deferred.isEmpty() && m_deferredChart==chartId()
        && (m_deferred!="chartName" || chart.value("name")==m_chart.value("name"));
    QJsonArray unsent;
    if(same && !sameSpec) {
        const auto list=m_working.value("series").toArray();
        for(int row=0;row<m_rows.size() && row<list.size();++row)
            if(m_rows[row].panelOnly && !complete(list[row]))unsent.append(list[row]);
    }
    m_chart=chart;if(!keepHeld)m_deferred.clear();
    load(sameSpec);
    if(!unsent.isEmpty() && !m_working.isEmpty()) {
        auto list=m_working["series"].toArray();
        for(const auto &item:unsent)if(list.size()<MaxSeries){list.append(item);m_rows.append(newRow(true));}
        m_working["series"]=list;
        showData();
    }
    updateEnabled();
}

void ChartPanel::settle() {
    m_pending=false;
    resume();
}

QString ChartPanel::chartId() const { return m_chart.value("id").toString(); }

void ChartPanel::setLocked(bool locked) {
    if(m_locked==locked)return;
    m_locked=locked;
    if(!locked){updateEnabled();resume();return;}
    // Disabling the focused field moves focus away; remember both ends.
    // A focus remembered by an earlier lock of the same wait is kept.
    auto *focused=window()->focusWidget();
    const bool inside=focused && isAncestorOf(focused);
    updateEnabled();
    if(!inside)return;
    m_focus=focused;m_focusChart=chartId();m_focusAfterLock=window()->focusWidget();
    // Any key or mouse press elsewhere during the wait means the person moved on.
    // Watched from the next turn of the event loop: the key that asked for the
    // change is still being delivered to the panel's parents now.
    m_userActed=false;
    QTimer::singleShot(0,this,[this]{if(m_focus)qApp->installEventFilter(this);});
}

bool ChartPanel::eventFilter(QObject *object,QEvent *event) {
    // The wheel over a closed list goes on to the scroll area holding the
    // panel: it never changes a value, which would apply at once.
    if(event->type()==QEvent::Wheel) {
        if(auto *combo=qobject_cast<QComboBox *>(object);combo && isAncestorOf(combo) && !combo->view()->isVisible()){event->ignore();return true;}
        return false;
    }
    if(event->type()==QEvent::KeyPress || event->type()==QEvent::MouseButtonPress) {
        // Counted where a widget receives it; a window passes it on to one.
        auto *widget=qobject_cast<QWidget *>(object);
        if(widget && widget!=this && !isAncestorOf(widget))m_userActed=true;
    }
    return false;
}

void ChartPanel::forgetFocus() {
    qApp->removeEventFilter(this);
    m_focus=nullptr;m_focusAfterLock=nullptr;m_userActed=false;
}

void ChartPanel::refused(const QString &field,const QString &message) {
    m_pending=false;
    loadField(field);
    if(message.isEmpty())clearMessage(field);else showMessage(field,message);
    resume();
}

void ChartPanel::resume() {
    if(m_locked || m_pending || m_chart.isEmpty())return;
    if(m_focus && !m_userActed && m_focusChart==chartId() && m_focus->isEnabled() && m_focus->isVisible()) {
        auto *now=window()->focusWidget();
        if(!now || now==m_focusAfterLock)m_focus->setFocus(Qt::OtherFocusReason);
    }
    forgetFocus();
    if(m_deferred.isEmpty())return;
    const auto field=std::exchange(m_deferred,{});
    if(m_deferredChart==chartId())apply(field);else loadField(field);
}

void ChartPanel::apply(const QString &field) {
    if(field=="chartName")applyName();
    else if(field=="valueAxisMin" || field=="valueAxisMax")applyBound(field);
    else if(dataSlot(field,nullptr,nullptr))applySource(field);
    else applyText(field);
}

void ChartPanel::defer(const QString &field) {
    m_deferred=field;m_deferredChart=chartId();
}

QLabel *ChartPanel::messageLabel(const QString &field) {
    auto *label=new QLabel;label->setObjectName(field+"Message");label->setWordWrap(true);label->hide();
    auto palette=label->palette();palette.setColor(QPalette::WindowText,QColor("#b3261e"));label->setPalette(palette);
    m_messages.insert(field,label);
    return label;
}

void ChartPanel::load(bool keepData) {
    m_spec={};m_reason.clear();
    if(!m_chart.isEmpty()) {
        m_spec=ChartSpec::inputSpec(m_chart,&m_reason);
        if(m_reason.isEmpty())m_reason=ChartSpec::stateReason(m_chart);
    }
    m_pages->setCurrentIndex(m_chart.isEmpty()?0:1);
    m_reasonLabel->setText(m_reason);m_reasonLabel->setVisible(!m_reason.isEmpty());
    for(auto field=m_messages.keyBegin();field!=m_messages.keyEnd();++field)
        if(!keepData || (*field!="series" && !dataSlot(*field,nullptr,nullptr)))clearMessage(*field);
    // A held field keeps its text until it is applied.
    for(const auto &field:{"chartName","chartType","chartGrouping","chartTitle","categoryAxisTitle","valueAxisTitle","valueAxisMin","valueAxisMax","legendPosition"})
        if(field!=m_deferred)loadField(field);
    if(keepData)updateMarkers();else loadData();
}

void ChartPanel::loadField(const QString &field) {
    // A refused Data change undoes that change alone: the working copy as it
    // was before it, with earlier held edits and added rows, if the chart is
    // still the one it was made on; otherwise the chart as stored.
    if(field=="series" || dataSlot(field,nullptr,nullptr)) {
        if(m_before.valid && m_before.chart==m_chart) {
            m_working=m_before.working;m_rows=m_before.rows;m_categoriesEdited=m_before.categoriesEdited;m_seriesHeld=m_before.seriesHeld;
            m_before={};
            showData();
        } else loadData();
        return;
    }
    // An unreadable specification shows nothing; a broken source shows the stored values.
    const auto spec=m_chart.value("issue").isString()?QJsonObject():m_chart.value("spec").toObject();
    if(field=="chartName")m_name->setText(m_chart.value("name").toString());
    else if(field=="chartType") {
        for(auto *button:m_typeButtons)button->setChecked(false);
        if(auto *button=m_typeButtons.value(spec["type"].toString()))button->setChecked(true);
    } else if(field=="chartGrouping")m_grouping->setCurrentIndex(m_grouping->findData(spec["grouping"].toString()));
    else if(TextFields.contains(field))findChild<QLineEdit *>(field)->setText(textValue(spec,field).toString());
    else if(field=="valueAxisMin" || field=="valueAxisMax") {
        const QJsonValue value=axesOf(spec).value("value").toObject().value(field=="valueAxisMin"?"min":"max");
        (field=="valueAxisMin"?m_min:m_max)->setText(number(value));
    } else if(field=="legendPosition")m_legend->setCurrentIndex(spec.isEmpty()?-1:m_legend->findData(spec["legend"].toObject()["position"].toString()));
}

void ChartPanel::updateEnabled() {
    const bool open=!m_chart.isEmpty() && !m_locked,readable=open && !m_spec.isEmpty();
    m_chartGroup->setEnabled(open);
    // Data stays open for a broken source (its sources are edited there), not for an unreadable specification.
    m_dataGroup->setEnabled(readable || (open && !m_chart.value("issue").isString() && ChartSpec::hasBrokenSource(m_chart)));
    for(auto *group:{m_typeGroup,m_titleGroup,m_axesGroup,m_legendGroup})group->setEnabled(readable);
    m_grouping->setEnabled(readable && ChartSpec::takesGrouping(m_spec.value("type").toString()));
}

void ChartPanel::showMessage(const QString &field,const QString &message) {
    auto *label=m_messages.value(field);if(!label)return;
    label->setText(message);label->show();
    QList<QWidget *> widgets;
    if(field=="chartType")for(auto *button:m_typeButtons)widgets.append(button);
    else if(field=="chartOrder")widgets={m_front,m_back};
    else if(auto *source=sourceField(field))widgets=source->inputs();
    else if(auto *widget=findChild<QWidget *>(field))widgets={widget};
    for(auto *widget:widgets)widget->setAccessibleDescription(message);
}

void ChartPanel::clearMessage(const QString &field) {
    auto *label=m_messages.value(field);if(!label || label->isHidden())return;
    label->clear();label->hide();
    QList<QWidget *> widgets;
    if(field=="chartType")for(auto *button:m_typeButtons)widgets.append(button);
    else if(field=="chartOrder")widgets={m_front,m_back};
    else if(auto *source=sourceField(field))widgets=source->inputs();
    else if(auto *widget=findChild<QWidget *>(field))widgets={widget};
    for(auto *widget:widgets)widget->setAccessibleDescription({});
}

bool ChartPanel::canRequest() const { return !m_locked && !m_pending && !m_chart.isEmpty(); }

void ChartPanel::request(const QJsonObject &changes,const QString &field) {
    // Set before emitting: an unsent request is refused at once, which ends the wait.
    m_pending=true;
    emit changeRequested(chartId(),changes,field);
}

void ChartPanel::requestSpec(const QString &field,const std::function<void(QJsonObject &)> &change) {
    auto spec=m_spec;change(spec);
    request({{"spec",spec}},field);
}

void ChartPanel::applyName() {
    if(m_chart.isEmpty())return;
    if(m_locked || m_pending){defer("chartName");return;}
    // Only text the person typed applies, so passing through never changes a value.
    if(!m_name->isModified())return;
    const auto text=m_name->text();
    if(text==m_chart.value("name").toString())return;
    request({{"name",text}},"chartName");
}

void ChartPanel::applyText(const QString &field) {
    if(m_chart.isEmpty() || m_spec.isEmpty())return;
    if(m_locked || m_pending){defer(field);return;}
    // Only text the person typed applies, so focus passing through a field
    // never sends what it shows.
    auto *edit=findChild<QLineEdit *>(field);
    if(!edit->isModified())return;
    const auto text=edit->text();
    const QJsonValue value=text.isEmpty()?QJsonValue(QJsonValue::Null):QJsonValue(text);
    if(value==textValue(m_spec,field))return;
    requestSpec(field,[field,value](QJsonObject &spec){
        if(field=="chartTitle"){spec["title"]=QJsonObject{{"text",value}};return;}
        auto axes=axesOf(spec);
        const auto axis=field=="categoryAxisTitle"?"category":"value";
        auto item=axes[axis].toObject();item["title"]=value;axes[axis]=item;spec["axes"]=axes;
    });
}

void ChartPanel::applyBound(const QString &field) {
    if(m_chart.isEmpty() || m_spec.isEmpty())return;
    if(m_locked || m_pending){defer(field);return;}
    const auto key=field=="valueAxisMin"?"min":"max";
    if(!(field=="valueAxisMin"?m_min:m_max)->isModified())return;
    const auto text=(field=="valueAxisMin"?m_min:m_max)->text().trimmed();
    QJsonValue value=QJsonValue::Null;
    if(!text.isEmpty()) {
        // A group separator is never taken: "1,500" is 1.5 where the comma
        // is the decimal mark, and refused where it would only group digits.
        QLocale c=QLocale::c(),local;
        c.setNumberOptions(c.numberOptions()|QLocale::RejectGroupSeparator);local.setNumberOptions(local.numberOptions()|QLocale::RejectGroupSeparator);
        // A whole number is sent as a JSON integer, never through a double.
        bool ok=false;const qint64 whole=c.toLongLong(text,&ok);
        if(ok)value=whole;
        else {
            double parsed=c.toDouble(text,&ok);
            if(!ok)parsed=local.toDouble(text,&ok);
            if(!ok || !std::isfinite(parsed)){refused(field,tr("Enter a number or leave empty for automatic"));return;}
            value=parsed;
        }
    }
    const QJsonValue stored=axesOf(m_spec).value("value").toObject().value(key);
    if(value.isNull()?stored.isNull():stored.isDouble() && (value.isDouble() && value.toInteger(1)==value.toInteger(0)
        ?stored.toInteger(~value.toInteger())==value.toInteger():stored.toDouble()==value.toDouble()))return;
    requestSpec(field,[key,value](QJsonObject &spec){
        auto axes=axesOf(spec);auto item=axes["value"].toObject();item[key]=value;axes["value"]=item;spec["axes"]=axes;
    });
}

void ChartPanel::applyType(const QString &type) {
    clearMessage("chartType");
    if(!canRequest() || m_spec.isEmpty() || type==m_spec.value("type").toString()){loadField("chartType");return;}
    QString refusal;
    const auto spec=ChartSpec::withType(m_spec,type,&refusal);
    if(spec.isEmpty()){refused("chartType",refusal);return;}
    request({{"spec",spec}},"chartType");
}

// The Data group.

namespace {
QString nameIn(const QJsonArray &items,const QString &id) {
    for(const auto &value:items)if(value.toObject()["id"].toString()==id)return value.toObject()["name"].toString();
    return {};
}
QString columnIn(const QJsonArray &tables,const QString &table,const QString &column) {
    for(const auto &value:tables) {
        if(value.toObject()["id"].toString()!=table)continue;
        return nameIn(value.toObject()["columns"].toArray(),column);
    }
    return {};
}
// The kind a field shows for a working holder: text or cell for a name,
// otherwise the source's kind (range when there is none yet).
QString holderKind(const QJsonObject &holder,bool name) {
    if(name)return holder.contains("source")?"cell":"text";
    const auto kind=holder["source"].toObject()["kind"].toString();
    return kind.isEmpty()?"range":kind;
}
}

void ChartPanel::setCatalog(const QJsonArray &sheets,const QJsonArray &tables,const QJsonArray &queries) {
    m_sheetNames.clear();m_sheetIds.clear();
    for(const auto &value:sheets) {
        const auto sheet=value.toObject();
        m_sheetNames.insert(sheet["id"].toString(),sheet["name"].toString());m_sheetIds.insert(sheet["name"].toString(),sheet["id"].toString());
    }
    // A saved query's columns stay known until the catalog describes it differently.
    QHash<QString,QJsonObject> summaries;
    for(const auto &value:queries)summaries.insert(value.toObject()["id"].toString(),value.toObject());
    for(auto known=m_queryColumns.begin();known!=m_queryColumns.end();)
        if(summaries.value(known.key())!=m_querySummaries.value(known.key()))known=m_queryColumns.erase(known);else ++known;
    m_querySummaries=summaries;
    m_tables=tables;m_queries=queries;
    // Columns asked for under the catalog before are asked for again.
    const auto wanted=std::exchange(m_queriesWanted,{});
    for(const auto &id:wanted)if(summaries.contains(id) && !m_queryColumns.contains(id)){m_queriesWanted.insert(id);emit queryColumnsWanted(id);}
    const auto withColumns=queriesWithColumns();
    QList<ChartSourceField *> fields{m_categories};
    for(const auto &box:std::as_const(m_series))fields<<box.name<<box.values<<box.x;
    for(auto *source:fields) {
        source->setCatalog(tables,withColumns);
        // A range shows the sheet's name as it is now, unless the person is
        // typing in it or chose another kind there.
        int row;QString part;dataSlot(source->objectName(),&row,&part);
        const auto kind=holderKind(holder(source->objectName()).toObject(),part=="name");
        if(!source->textModified() && source->kind()==kind && (kind=="range" || kind=="cell"))showSource(source->objectName());
    }
}

void ChartPanel::setQueryColumns(const QString &queryId,const QStringList &columns) {
    m_queriesWanted.remove(queryId);
    if(!m_querySummaries.contains(queryId))return;
    m_queryColumns.insert(queryId,columns);
    const auto withColumns=queriesWithColumns();
    m_categories->setCatalog(m_tables,withColumns);
    for(const auto &box:std::as_const(m_series))for(auto *source:{box.name,box.values,box.x})source->setCatalog(m_tables,withColumns);
}

void ChartPanel::queryColumnsFailed(const QString &queryId) {
    m_queriesWanted.remove(queryId);
}

QJsonArray ChartPanel::queriesWithColumns() const {
    QJsonArray out;
    for(const auto &value:m_queries) {
        auto query=value.toObject();
        if(const auto known=m_queryColumns.constFind(query["id"].toString());known!=m_queryColumns.constEnd()) {
            QJsonArray shape;for(const auto &column:*known)shape.append(QJsonObject{{"name",column}});
            query["shape"]=shape;
        }
        out.append(query);
    }
    return out;
}

bool ChartPanel::complete(const QJsonValue &series) const {
    const auto item=series.toObject();
    return item["values"].isObject() && (!scatter() || item["x"].isObject());
}

void ChartPanel::remember() {
    m_before={true,m_chart,m_working,m_rows,m_categoriesEdited,m_seriesHeld};
}

void ChartPanel::clearSeriesMessages() {
    for(auto field=m_messages.keyBegin();field!=m_messages.keyEnd();++field)
        if(field->startsWith("series") && dataSlot(*field,nullptr,nullptr))clearMessage(*field);
}

bool ChartPanel::dataSlot(const QString &field,int *row,QString *part) {
    if(field=="categoriesSource") {
        if(row)*row=-1;
        if(part)*part="categories";
        return true;
    }
    static const QRegularExpression pattern("^series([1-9][0-9]*)(Name|Values|X)$");
    const auto match=pattern.match(field);
    if(!match.hasMatch())return false;
    if(row)*row=match.captured(1).toInt()-1;
    if(part)*part=match.captured(2).toLower();
    return true;
}

ChartSourceField *ChartPanel::sourceField(const QString &field) const {
    int row;QString part;
    if(!dataSlot(field,&row,&part))return nullptr;
    if(row<0)return m_categories;
    if(row>=m_series.size())return nullptr;
    const auto &box=m_series[row];
    return part=="name"?box.name:part=="values"?box.values:box.x;
}

QJsonValue ChartPanel::holder(const QString &field) const {
    int row;QString part;
    if(!dataSlot(field,&row,&part))return {};
    if(row<0)return m_working.value("categories");
    return m_working.value("series").toArray().at(row).toObject().value(part);
}

void ChartPanel::setHolder(const QString &field,const QJsonValue &value) {
    int row;QString part;
    if(!dataSlot(field,&row,&part))return;
    if(row<0){m_working["categories"]=value;return;}
    auto list=m_working["series"].toArray();auto item=list[row].toObject();item[part]=value;list[row]=item;m_working["series"]=list;
}

bool ChartPanel::scatter() const { return m_working.value("type").toString()=="scatter"; }

void ChartPanel::loadData() {
    m_before={};m_picked={};
    m_working=m_chart.isEmpty()?QJsonObject():ChartSpec::workingSpec(m_chart);
    m_rows.clear();
    for(qsizetype row=0;row<m_working.value("series").toArray().size();++row)m_rows.append(newRow(false));
    m_categoriesEdited=m_seriesHeld=false;
    // The names the stored sources give, for a table, column or query the catalog lacks.
    m_storedNames.clear();
    const auto spec=m_chart.value("spec").toObject();
    QList<QJsonObject> sources{spec["categories"].toObject()["source"].toObject()};
    for(const auto &value:spec["series"].toArray())
        for(const auto &part:{"name","values","x"})sources.append(value.toObject()[part].toObject()["source"].toObject());
    for(const auto &source:sources) {
        const auto kind=source["kind"].toString();
        if(kind=="table") {
            if(source["table"].isString())m_storedNames.insert("table:"+source["table_id"].toString(),source["table"].toString());
            if(source["column"].isString())m_storedNames.insert("column:"+source["table_id"].toString()+"/"+source["column_id"].toString(),source["column"].toString());
        } else if(kind=="query" && source["query"].isString())m_storedNames.insert("query:"+source["query_id"].toString(),source["query"].toString());
    }
    showData();
}

void ChartPanel::showData() {
    // Fields shown afresh end a pick: the field picked may be another one now.
    if(!m_picking.isEmpty()) {
        if(auto *source=sourceField(std::exchange(m_picking,{})))source->setPicking(false);
        m_picked={};
        emit pickEnded();
    }
    ensureSeries(int(m_working.value("series").toArray().size()));
    const bool xs=scatter();
    // A scatter chart has no categories; each series has its X instead.
    m_categoriesForm->setRowVisible(0,!xs);
    showSource("categoriesSource");
    for(int row=0;row<m_series.size();++row) {
        const auto n=QString::number(row+1);
        for(const auto &part:{"Name","Values","X"})showSource("series"+n+part);
        m_series[row].form->setRowVisible(2,xs);
        m_series[row].up->setEnabled(row>0);m_series[row].down->setEnabled(row+1<m_series.size());
    }
    m_addSeries->setEnabled(m_series.size()<MaxSeries);
    updateMarkers();
}

void ChartPanel::showSource(const QString &field) {
    auto *source=sourceField(field);if(!source)return;
    int row;QString part;dataSlot(field,&row,&part);
    const auto value=holder(field).toObject();
    const auto own=m_chart.value("sheet_id").toString();
    const auto stored=value["source"].toObject();
    const auto kind=holderKind(value,part=="name");
    if(kind=="text")source->showText(kind,value["text"].toString());
    else if(kind=="table") {
        // Input form names the table and column by id; a stored (broken) source in its own fields.
        const bool input=!stored.contains("table_id");
        const auto table=stored[input?"table":"table_id"].toString(),column=stored[input?"column":"column_id"].toString();
        auto name=nameIn(m_tables,table);if(name.isEmpty())name=m_storedNames.value("table:"+table);
        auto columnName=columnIn(m_tables,table,column);if(columnName.isEmpty())columnName=m_storedNames.value("column:"+table+"/"+column);
        source->showChoice(kind,table,name,column,columnName);
    } else if(kind=="query") {
        const auto query=stored[stored.contains("query_id")?"query_id":"query"].toString();
        auto name=nameIn(m_queries,query);if(name.isEmpty())name=m_storedNames.value("query:"+query);
        source->showChoice(kind,query,name,stored["column"].toString(),stored["column"].toString());
    } else source->showText(kind,stored.isEmpty()?QString():ChartSpec::rangeText(stored,m_sheetNames,own));
}

void ChartPanel::ensureSeries(int count) {
    while(m_series.size()>count) {
        const auto box=m_series.takeLast();
        const auto n=QString::number(m_series.size()+1);
        for(const auto &part:{"Name","Values","X"}){m_messages.remove("series"+n+part);m_markers.remove("series"+n+part);}
        // Gone at once for lookups by name; deleted once a signal of its own has returned.
        box.box->hide();m_seriesLayout->removeWidget(box.box);
        for(auto *child:box.box->findChildren<QObject *>())child->setObjectName({});
        box.box->deleteLater();
    }
    while(m_series.size()<count) {
        const int row=int(m_series.size());
        const auto n=QString::number(row+1);
        SeriesBox box;
        box.box=new QGroupBox(tr("Series %1").arg(n));box.form=new QFormLayout(box.box);
        auto field=[&](ChartSourceField *&source,const QString &part,const QString &label,bool name) {
            source=new ChartSourceField("series"+n+part,tr("Series %1 %2").arg(n,label),name);
            box.form->addRow(label,withMarkers(source));
        };
        field(box.name,"Name",tr("Name"),true);
        field(box.values,"Values",tr("Values"),false);
        field(box.x,"X",tr("X"),false);
        box.remove=new QPushButton(tr("Remove"));box.remove->setObjectName("removeSeries"+n);
        box.up=new QPushButton(tr("Move Up"));box.up->setObjectName("seriesUp"+n);
        box.down=new QPushButton(tr("Move Down"));box.down->setObjectName("seriesDown"+n);
        auto *buttons=new QHBoxLayout;
        for(auto *button:{box.remove,box.up,box.down})buttons->addWidget(button);
        buttons->addStretch();box.form->addRow(buttons);
        connect(box.remove,&QPushButton::clicked,this,[this,row]{removeSeries(row);});
        connect(box.up,&QPushButton::clicked,this,[this,row]{moveSeries(row,-1);});
        connect(box.down,&QPushButton::clicked,this,[this,row]{moveSeries(row,1);});
        for(auto *button:{box.remove,box.up,box.down})takeKeyboard(button);
        // Shown now, not when the layout gets round to it.
        m_seriesLayout->addWidget(box.box);box.box->show();
        m_series.append(box);
    }
    orderFocus();
}

void ChartPanel::orderFocus() {
    // The panel's focus order, as the wireframes give it: series rows are
    // made after Axes and Legend, so the order is set rather than left to creation.
    QList<QWidget *> chain{m_name,m_front,m_back};
    for(const auto &type:Types)chain<<m_typeButtons.value(type);
    chain<<m_grouping<<m_title;
    chain+=m_categories->inputs();
    for(const auto &box:std::as_const(m_series)) {
        chain+=box.name->inputs();chain+=box.values->inputs();chain+=box.x->inputs();
        chain<<box.remove<<box.up<<box.down;
    }
    chain<<m_addSeries<<m_categoryTitle<<m_valueTitle<<m_min<<m_max<<m_legend;
    for(int i=1;i<chain.size();++i)setTabOrder(chain[i-1],chain[i]);
}

void ChartPanel::updateMarkers() {
    const bool held=ChartSpec::holdsBrokenSource(m_working);
    auto mark=[&](const QString &field,bool edited,bool unsent) {
        auto *label=m_markers.value(field);if(!label)return;
        const bool broken=holder(field).toObject()["source"].toObject()["broken"].toBool();
        const auto text=broken?tr("This source no longer exists"):(edited && held) || unsent?tr("Not applied yet"):QString();
        auto palette=label->palette();
        palette.setColor(QPalette::WindowText,broken?QColor("#b3261e"):this->palette().color(QPalette::WindowText));
        label->setPalette(palette);label->setText(text);label->setVisible(!text.isEmpty());
    };
    mark("categoriesSource",m_categoriesEdited,false);
    for(int row=0;row<m_rows.size();++row) {
        const auto n=QString::number(row+1);const auto &state=m_rows[row];
        mark("series"+n+"Name",state.edited.contains("name"),false);
        // A series added in the panel is not applied until its values (and X) are given.
        mark("series"+n+"Values",state.edited.contains("values"),state.panelOnly);
        mark("series"+n+"X",state.edited.contains("x"),state.panelOnly && scatter());
    }
    m_seriesMarker->setVisible(held && m_seriesHeld);
}

QJsonObject ChartPanel::sendable() const {
    auto spec=m_working;
    QJsonArray list;
    for(const auto &value:m_working.value("series").toArray()) {
        const auto item=value.toObject();
        if(!item["values"].isObject() || (scatter() && !item["x"].isObject()))continue;
        list.append(item);
    }
    spec["series"]=list;
    return spec;
}

void ChartPanel::applySource(const QString &field) {
    auto *source=sourceField(field);
    if(!source || m_chart.isEmpty() || m_working.isEmpty())return;
    // While picked the field shows the grid's selection; endPick applies it.
    if(field==m_picking)return;
    const auto kind=source->kind();
    const bool typed=kind=="text" || kind=="cell" || kind=="range";
    if(m_locked || m_pending){if(typed)defer(field);else showSource(field);return;}
    QJsonValue value;
    if(typed) {
        // Only typed text applies; a field left as it was shown sends nothing.
        if(!source->textModified())return;
        if(kind=="text")value=QJsonObject{{"text",source->text()}};
        else {
            const auto text=source->text().trimmed();
            if(text.isEmpty())return;
            QJsonObject range;
            // A picked range is taken as picked; typed text is read.
            if(m_picked.field==field && m_picked.text==text)range=m_picked.source;
            else if(!ChartSpec::parseRange(text,m_sheetIds,m_chart.value("sheet_id").toString(),&range)){refuseLocally(field,tr("Enter a cell range such as A2:A13"));return;}
            if(kind=="cell" && range["ref"].toString().contains(':')){refuseLocally(field,tr("A series name takes one cell"));return;}
            value=QJsonObject{{"source",range}};
        }
    } else {
        const auto id=source->chosenId(),column=source->chosenColumn();
        if(id.isEmpty() || column.isEmpty())return;
        value=QJsonObject{{"source",QJsonObject{{"kind",kind},{kind,id},{"column",column}}}};
    }
    if(value==holder(field)){showSource(field);return;}
    remember();
    setHolder(field,value);
    int row;QString part;dataSlot(field,&row,&part);
    if(row<0)m_categoriesEdited=true;else m_rows[row].edited.insert(part);
    showSource(field);
    commitData(field);
}

void ChartPanel::commitData(const QString &field) {
    updateMarkers();
    // Held while a source is broken; nothing to send when it is what is stored.
    const auto spec=sendable();
    if(ChartSpec::holdsBrokenSource(spec) || spec==m_spec || spec.value("series").toArray().isEmpty())return;
    request({{"spec",spec}},field);
}

void ChartPanel::removeSeries(int row) {
    clearMessage("series");
    if(m_locked || m_chart.isEmpty() || row>=m_rows.size())return;
    auto list=m_working["series"].toArray();
    // An added row not yet complete was never sent: it goes even while a change is awaited.
    const bool stored=complete(list[row]);
    if(stored && !canRequest())return;
    const auto completeCount=std::count_if(list.begin(),list.end(),[this](const QJsonValue &value){return complete(value);});
    if(stored && completeCount==1){refuseLocally("series",tr("A chart needs at least one series"));return;}
    if(stored)remember();
    // An added row removed while a change is awaited (or edits are held) is
    // gone from what a refusal goes back to as well, found there by its id.
    else if(m_before.valid) {
        const int id=m_rows[row].id;
        for(qsizetype at=0;at<m_before.rows.size();++at) {
            if(m_before.rows[at].id!=id)continue;
            auto before=m_before.working["series"].toArray();before.removeAt(at);m_before.working["series"]=before;m_before.rows.removeAt(at);
            break;
        }
    }
    list.removeAt(row);m_working["series"]=list;m_rows.removeAt(row);
    if(stored)m_seriesHeld=true;
    // Messages stay with no shifted series.
    clearSeriesMessages();
    showData();
    if(stored)commitData("series");
}

void ChartPanel::moveSeries(int row,int by) {
    clearMessage("series");
    const int to=row+by;
    if(!canRequest() || to<0 || to>=m_rows.size())return;
    remember();
    auto list=m_working["series"].toArray();
    const QJsonValue item=list[row];list[row]=list[to];list[to]=item;
    m_working["series"]=list;m_rows.swapItemsAt(row,to);
    m_seriesHeld=true;
    clearSeriesMessages();
    showData();
    commitData("series");
}

void ChartPanel::addSeries() {
    clearMessage("series");
    if(!canRequest() || m_working.isEmpty() || m_rows.size()>=MaxSeries)return;
    auto list=m_working["series"].toArray();
    list.append(QJsonObject{{"name",QJsonObject{{"text",""}}},{"values",QJsonValue::Null},{"x",QJsonValue::Null}});
    m_working["series"]=list;m_rows.append(newRow(true));
    showData();
    m_series.last().values->setFocus(Qt::OtherFocusReason);
}

void ChartPanel::refuseLocally(const QString &field,const QString &message) {
    showSource(field);
    showMessage(field,message);
}

// Picking a range on the grid.

QString ChartPanel::fieldLabel(const QString &field) {
    int row;QString part;
    if(!dataSlot(field,&row,&part))return {};
    if(row<0)return tr("Categories");
    const auto label=part=="name"?tr("Name"):part=="values"?tr("Values"):tr("X");
    return tr("Series %1 %2").arg(row+1).arg(label);
}

void ChartPanel::beginPick(const QString &field) {
    if(!m_picking.isEmpty())endPick(false,false);
    auto *source=sourceField(field);
    if(!source || m_chart.isEmpty() || m_working.isEmpty() || !(source->kind()=="range" || source->kind()=="cell"))return;
    m_picking=field;m_picked={};
    source->setPicking(true);
}

void ChartPanel::setPicked(const QString &field,const QJsonObject &rangeSource) {
    auto *source=sourceField(field);
    if(field.isEmpty() || field!=m_picking || !source)return;
    const auto text=ChartSpec::rangeText(rangeSource,m_sheetNames,m_chart.value("sheet_id").toString());
    m_picked={field,text,rangeSource};
    clearMessage(field);
    source->showPicked(text);
}

void ChartPanel::endPick(bool apply,bool focus) {
    const auto field=std::exchange(m_picking,{});
    auto *source=sourceField(field);if(!source)return;
    source->setPicking(false,!apply);
    if(!apply)m_picked={};
    // Focus first: a change sent now locks the panel, which gives it back on unlock.
    if(focus)source->focusText();
    if(apply)applySource(field);
}
