// SPDX-License-Identifier: GPL-3.0-only
#include "chartpanel.h"
#include "chartspec.h"
#include <QButtonGroup>
#include <QComboBox>
#include <QFormLayout>
#include <QGridLayout>
#include <QGroupBox>
#include <QHBoxLayout>
#include <QLabel>
#include <QLineEdit>
#include <QLocale>
#include <QPushButton>
#include <QStackedWidget>
#include <QToolButton>
#include <QVBoxLayout>
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
    return value.isDouble()?QString::number(value.toDouble(),'g',QLocale::FloatingPointShortest):QString();
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
    m_types=new QButtonGroup(this);m_types->setExclusive(true);
    auto *buttons=new QGridLayout;
    const QStringList labels{tr("Column"),tr("Bar"),tr("Line"),tr("Area"),tr("Pie"),tr("Scatter")};
    for(int i=0;i<Types.size();++i) {
        auto *button=new QToolButton;button->setObjectName("chartType_"+Types[i]);button->setText(labels[i]);button->setCheckable(true);
        button->setToolButtonStyle(Qt::ToolButtonTextOnly);button->setSizePolicy(QSizePolicy::Expanding,QSizePolicy::Fixed);
        m_types->addButton(button,i);m_typeButtons.insert(Types[i],button);buttons->addWidget(button,i/3,i%3);
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

    // Data: sources and series (filled by a later task).
    m_dataGroup=new QWidget;m_dataGroup->setObjectName("chartDataGroup");
    auto *data=new QVBoxLayout(m_dataGroup);data->setContentsMargins(0,0,0,0);
    column->addWidget(m_dataGroup);

    // Axes: titles, Minimum and Maximum.
    m_axesGroup=new QGroupBox(tr("Axes"));auto *axesForm=new QFormLayout(m_axesGroup);
    auto line=[&](QLineEdit *&edit,const QString &name,const QString &label) {
        edit=new QLineEdit;edit->setObjectName(name);edit->setAccessibleName(label);edit->setMaxLength(255);
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
    connect(m_types,&QButtonGroup::idClicked,this,[this](int id){applyType(Types.value(id));});
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
    load();updateEnabled();
}

void ChartPanel::setChart(const QJsonObject &chart) {
    if(chart==m_chart)return;
    // Another chart, or a changed one: held text is dropped with the reload.
    m_chart=chart;m_deferred.clear();
    load();updateEnabled();
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
    auto *focused=window()->focusWidget();
    m_focus=focused && isAncestorOf(focused)?focused:nullptr;m_focusChart=chartId();
    updateEnabled();
    m_focusAfterLock=m_focus?window()->focusWidget():nullptr;
}

void ChartPanel::refused(const QString &field,const QString &message) {
    m_pending=false;
    loadField(field);
    if(message.isEmpty())clearMessage(field);else showMessage(field,message);
    resume();
}

void ChartPanel::resume() {
    if(m_locked || m_pending || m_chart.isEmpty())return;
    if(m_focus && m_focusChart==chartId() && m_focus->isEnabled() && m_focus->isVisible()) {
        auto *now=window()->focusWidget();
        if(!now || now==m_focusAfterLock)m_focus->setFocus(Qt::OtherFocusReason);
    }
    m_focus=nullptr;m_focusAfterLock=nullptr;
    if(m_deferredChart!=chartId()){for(const auto &field:std::exchange(m_deferred,{}))loadField(field);return;}
    while(!m_deferred.isEmpty() && !m_pending && !m_locked)apply(m_deferred.takeFirst());
}

void ChartPanel::apply(const QString &field) {
    if(field=="chartName")applyName();
    else if(field=="valueAxisMin" || field=="valueAxisMax")applyBound(field);
    else applyText(field);
}

void ChartPanel::defer(const QString &field) {
    if(!m_deferred.contains(field))m_deferred.append(field);
    m_deferredChart=chartId();
}

QLabel *ChartPanel::messageLabel(const QString &field) {
    auto *label=new QLabel;label->setObjectName(field+"Message");label->setWordWrap(true);label->hide();
    auto palette=label->palette();palette.setColor(QPalette::WindowText,QColor("#b3261e"));label->setPalette(palette);
    m_messages.insert(field,label);
    return label;
}

void ChartPanel::load() {
    m_spec={};m_reason.clear();
    if(!m_chart.isEmpty()) {
        m_spec=ChartSpec::inputSpec(m_chart,&m_reason);
        if(m_reason.isEmpty())m_reason=ChartSpec::stateReason(m_chart);
    }
    m_pages->setCurrentIndex(m_chart.isEmpty()?0:1);
    m_reasonLabel->setText(m_reason);m_reasonLabel->setVisible(!m_reason.isEmpty());
    for(auto field=m_messages.keyBegin();field!=m_messages.keyEnd();++field)clearMessage(*field);
    for(const auto &field:{"chartName","chartType","chartGrouping","chartTitle","categoryAxisTitle","valueAxisTitle","valueAxisMin","valueAxisMax","legendPosition"})loadField(field);
}

void ChartPanel::loadField(const QString &field) {
    // An unreadable specification shows nothing; a broken source shows the stored values.
    const auto spec=m_chart.value("issue").isString()?QJsonObject():m_chart.value("spec").toObject();
    if(field=="chartName")m_name->setText(m_chart.value("name").toString());
    else if(field=="chartType") {
        m_types->setExclusive(false);
        for(auto *button:m_typeButtons)button->setChecked(false);
        if(auto *button=m_typeButtons.value(spec["type"].toString()))button->setChecked(true);
        m_types->setExclusive(true);
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
    else if(auto *widget=findChild<QWidget *>(field))widgets={widget};
    for(auto *widget:widgets)widget->setAccessibleDescription(message);
}

void ChartPanel::clearMessage(const QString &field) {
    auto *label=m_messages.value(field);if(!label || label->isHidden())return;
    label->clear();label->hide();
    QList<QWidget *> widgets;
    if(field=="chartType")for(auto *button:m_typeButtons)widgets.append(button);
    else if(field=="chartOrder")widgets={m_front,m_back};
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
    const auto text=m_name->text();
    if(text==m_chart.value("name").toString())return;
    request({{"name",text}},"chartName");
}

void ChartPanel::applyText(const QString &field) {
    if(m_chart.isEmpty() || m_spec.isEmpty())return;
    if(m_locked || m_pending){defer(field);return;}
    const auto text=findChild<QLineEdit *>(field)->text();
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
    const auto text=(field=="valueAxisMin"?m_min:m_max)->text().trimmed();
    QJsonValue value=QJsonValue::Null;
    if(!text.isEmpty()) {
        // A group separator is never taken: "1,500" is 1.5 where the comma
        // is the decimal mark, and refused where it would only group digits.
        QLocale c=QLocale::c(),local;
        c.setNumberOptions(c.numberOptions()|QLocale::RejectGroupSeparator);local.setNumberOptions(local.numberOptions()|QLocale::RejectGroupSeparator);
        bool ok=false;double parsed=c.toDouble(text,&ok);
        if(!ok)parsed=local.toDouble(text,&ok);
        if(!ok || !std::isfinite(parsed)){refused(field,tr("Enter a number or leave empty for automatic"));return;}
        value=parsed;
    }
    const QJsonValue stored=axesOf(m_spec).value("value").toObject().value(key);
    if(value.isNull()?stored.isNull():stored.isDouble() && stored.toDouble()==value.toDouble())return;
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
