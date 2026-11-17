// SPDX-License-Identifier: GPL-3.0-only
#include "chartspec.h"
#include <QCoreApplication>
#include <QJsonArray>
#include <QRegularExpression>
#include <algorithm>

namespace ChartSpec {
namespace {
constexpr int MinWidth=64,MinHeight=48,MaxSide=4096;

QString tr(const char *text) { return QCoreApplication::translate("ChartLayer",text); }

QString columnLetters(int column) {
    QString letters;
    for(;column>0;column=(column-1)/26)letters.prepend(QChar('A'+(column-1)%26));
    return letters;
}
QString cellName(int row,int column) { return columnLetters(column)+QString::number(row); }

// Rewrites {source:stored} to {source:input}; anything else is unchanged.
QJsonValue convertHolder(const QJsonValue &value) {
    if(!value.isObject() || !value.toObject().contains("source"))return value;
    auto holder=value.toObject();
    holder["source"]=sourceToInput(holder["source"].toObject());
    return holder;
}
}

bool hasBrokenSource(const QJsonObject &chart) {
    const auto spec=chart["spec"].toObject();
    QList<QJsonObject> sources;
    sources.append(spec["categories"].toObject()["source"].toObject());
    for(const auto &value:spec["series"].toArray())
        for(const auto &part:QStringList{"name","values","x"})sources.append(value.toObject()[part].toObject()["source"].toObject());
    for(const auto &source:sources)if(source["broken"].toBool())return true;
    return false;
}

QString stateReason(const QJsonObject &chart) {
    if(chart["issue"].isString())return tr("This chart cannot be read (%1)").arg(chart["issue"].toString());
    if(chart["over_limit"].isString())return tr("This chart's sources are over a size limit (%1)").arg(chart["over_limit"].toString());
    if(hasBrokenSource(chart))return tr("A source of this chart no longer exists");
    return {};
}

QJsonObject inputSpec(const QJsonObject &chart,QString *reason) {
    if(chart["issue"].isString() || hasBrokenSource(chart)) {
        if(reason)*reason=stateReason(chart);
        return {};
    }
    auto spec=chart["spec"].toObject();
    spec["categories"]=convertHolder(spec["categories"]);
    QJsonArray list;
    for(const auto &value:spec["series"].toArray()) {
        auto item=value.toObject();
        for(const auto &part:QStringList{"name","values","x"})if(item.contains(part))item[part]=convertHolder(item[part]);
        list.append(item);
    }
    spec["series"]=list;
    return spec;
}

QJsonObject workingSpec(const QJsonObject &chart) {
    if(chart["issue"].isString())return {};
    auto keep=[](const QJsonValue &value) {
        if(value.isObject() && value.toObject()["source"].toObject()["broken"].toBool())return value;
        return convertHolder(value);
    };
    auto spec=chart["spec"].toObject();
    spec["categories"]=keep(spec["categories"]);
    QJsonArray list;
    for(const auto &value:spec["series"].toArray()) {
        auto item=value.toObject();
        for(const auto &part:QStringList{"name","values","x"})if(item.contains(part))item[part]=keep(item[part]);
        list.append(item);
    }
    spec["series"]=list;
    return spec;
}

bool holdsBrokenSource(const QJsonObject &spec) {
    auto broken=[](const QJsonValue &holder){return holder.toObject()["source"].toObject()["broken"].toBool();};
    if(broken(spec["categories"]))return true;
    for(const auto &value:spec["series"].toArray())
        for(const auto &part:QStringList{"name","values","x"})if(broken(value.toObject()[part]))return true;
    return false;
}

bool takesGrouping(const QString &type) { return type=="column" || type=="bar" || type=="area"; }

QJsonObject withType(const QJsonObject &spec,const QString &type,QString *refusal) {
    auto out=spec;
    const auto oldType=spec["type"].toString();
    const auto oldGrouping=spec["grouping"];
    out["type"]=type;
    if(!takesGrouping(type))out["grouping"]=QJsonValue::Null;
    else if(!oldGrouping.isString())out["grouping"]="clustered";
    auto list=spec["series"].toArray();
    if(type=="scatter" && oldType!="scatter") {
        const auto categories=spec["categories"];
        if(!categories.isObject()) {
            if(refusal)*refusal=tr("Give an X source before changing this chart to a scatter chart.");
            return {};
        }
        QJsonArray rebuilt;
        for(const auto &item:list) {
            auto series=item.toObject();series["x"]=categories;rebuilt.append(series);
        }
        out["series"]=rebuilt;
        out["categories"]=QJsonValue::Null;
    } else if(oldType=="scatter" && type!="scatter") {
        QJsonArray rebuilt;
        QJsonValue first=QJsonValue::Null;
        for(const auto &item:list) {
            auto series=item.toObject();
            if(rebuilt.isEmpty() && series["x"].isObject())first=series["x"];
            series["x"]=QJsonValue::Null;rebuilt.append(series);
        }
        out["series"]=rebuilt;
        out["categories"]=first;
    }
    return out;
}

QJsonObject sourceToInput(const QJsonObject &s) {
    const auto kind=s["kind"].toString();
    if(kind=="range")return {{"kind","range"},{"sheet",s["sheet_id"]},{"ref",s["ref"]}};
    if(kind=="table")return {{"kind","table"},{"table",s["table_id"]},{"column",s["column_id"]}};
    if(kind=="query")return {{"kind","query"},{"query",s["query_id"]},{"column",s["column"]}};
    return s;
}

QString sourceText(const QJsonObject &s,const QHash<QString,QString> &sheetNames) {
    const auto kind=s["kind"].toString();
    if(kind=="table")return s["table"].toString()+"."+s["column"].toString();
    if(kind=="query")return s["query"].toString()+"."+s["column"].toString();
    if(kind!="range")return {};
    auto ref=s["ref"].toString();
    if(ref.isEmpty()) {
        ref=cellName(s["row1"].toInt(),s["col1"].toInt());
        if(s["row1"]!=s["row2"] || s["col1"]!=s["col2"])ref+=":"+cellName(s["row2"].toInt(),s["col2"].toInt());
    }
    const auto id=s["sheet_id"].toString();
    auto name=sheetNames.value(id,id);
    static const QRegularExpression plain("^[A-Za-z_][A-Za-z0-9_]*$");
    if(!plain.match(name).hasMatch())name="'"+name.replace("'","''")+"'";
    return name+"!"+ref;
}

QString rangeText(const QJsonObject &source,const QHash<QString,QString> &sheetNames,const QString &ownSheetId) {
    auto stored=source;
    if(!stored.contains("sheet_id"))stored["sheet_id"]=stored["sheet"];
    // A range broken by deleting its cells or sheet keeps no rectangle to show.
    if(stored["ref"].toString().isEmpty() && !stored["row1"].isDouble())return {};
    const auto text=sourceText(stored,sheetNames);
    if(stored["sheet_id"].toString()!=ownSheetId)return text;
    // A reference holds no "!", so the last one ends the sheet name.
    return text.mid(text.lastIndexOf('!')+1);
}

QJsonObject rangeSource(const QString &sheetId,int r1,int c1,int r2,int c2) {
    auto ref=cellName(r1,c1);
    if(r1!=r2 || c1!=c2)ref+=":"+cellName(r2,c2);
    return {{"kind","range"},{"sheet",sheetId},{"ref",ref}};
}

bool parseRange(const QString &input,const QHash<QString,QString> &sheetIdsByName,const QString &currentSheetId,QJsonObject *source) {
    const auto text=input.trimmed();
    QString sheetId=currentSheetId,cells=text;
    if(text.startsWith('\'')) {
        QString name;int i=1;bool closed=false;
        for(;i<text.size();++i) {
            if(text[i]=='\'') {
                if(i+1<text.size() && text[i+1]=='\''){name+='\'';++i;continue;}
                closed=true;break;
            }
            name+=text[i];
        }
        if(!closed || i+1>=text.size() || text[i+1]!='!' || !sheetIdsByName.contains(name))return false;
        sheetId=sheetIdsByName.value(name);cells=text.mid(i+2);
    } else if(const auto bang=text.indexOf('!');bang>=0) {
        const auto name=text.left(bang);
        if(!sheetIdsByName.contains(name))return false;
        sheetId=sheetIdsByName.value(name);cells=text.mid(bang+1);
    }
    static const QRegularExpression cell("^\\$?([A-Za-z]{1,3})\\$?([0-9]{1,7})$");
    const auto parts=cells.split(':');
    if(parts.size()>2)return false;
    int rows[2],cols[2];
    for(int k=0;k<parts.size();++k) {
        const auto match=cell.match(parts[k]);
        if(!match.hasMatch())return false;
        int column=0;
        for(const auto ch:match.captured(1).toUpper())column=column*26+(ch.unicode()-'A'+1);
        const int row=match.captured(2).toInt();
        if(row<1 || column<1 || column>16384)return false;
        rows[k]=row;cols[k]=column;
    }
    if(parts.size()==1){rows[1]=rows[0];cols[1]=cols[0];}
    if(source)*source=rangeSource(sheetId,std::min(rows[0],rows[1]),std::min(cols[0],cols[1]),std::max(rows[0],rows[1]),std::max(cols[0],cols[1]));
    return true;
}

QSize clampSize(QSize size) {
    return {std::clamp(size.width(),MinWidth,MaxSide),std::clamp(size.height(),MinHeight,MaxSide)};
}

QRect resized(QRect start,int handle,QPoint delta) {
    int left=start.left(),top=start.top(),right=start.left()+start.width(),bottom=start.top()+start.height();
    if(handle==0 || handle==6 || handle==7)left=std::clamp(left+delta.x(),right-MaxSide,right-MinWidth);
    if(handle==2 || handle==3 || handle==4)right=std::clamp(right+delta.x(),left+MinWidth,left+MaxSide);
    if(handle==0 || handle==1 || handle==2)top=std::clamp(top+delta.y(),bottom-MaxSide,bottom-MinHeight);
    if(handle==4 || handle==5 || handle==6)bottom=std::clamp(bottom+delta.y(),top+MinHeight,top+MaxSide);
    return {left,top,right-left,bottom-top};
}

}
