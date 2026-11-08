// SPDX-License-Identifier: GPL-3.0-only
#pragma once
#include <QJsonArray>
#include <QJsonDocument>
#include <QJsonObject>
#include <QStringList>
#include <cmath>

inline bool validListViewName(const QString &name) {
    if(name.isEmpty() || name!=name.trimmed() || name.size()>64)return false;
    for(const auto ch:name)if(!ch.isPrint())return false;
    return true;
}

inline bool normalizedListView(const QJsonObject &input,QJsonObject &result) {
    const QStringList allowed{"r1","c1","r2","c2","header","sort","filters"};
    for(auto it=input.begin();it!=input.end();++it)if(!allowed.contains(it.key()))return false;
    auto integer=[](QJsonValue value,int low,int high){return value.isDouble() && value.toDouble()==value.toInt(-1) && value.toInt()>=low && value.toInt()<=high;};
    if(!integer(input["r1"],1,1048576) || !integer(input["r2"],input["r1"].toInt(),1048576) || !integer(input["c1"],1,16384) || !integer(input["c2"],input["c1"].toInt(),16384))return false;
    if(qint64(input["r2"].toInt()-input["r1"].toInt()+1)*(input["c2"].toInt()-input["c1"].toInt()+1)>10000)return false;
    if(input.contains("header") && !input["header"].isBool())return false;
    for(const auto &key:{QString("sort"),QString("filters")}) {
        if(input.contains(key) && !input[key].isArray())return false;
        const auto items=input[key].toArray();if(items.size()>8)return false;
        for(const auto &value:items) {
            if(!value.isObject())return false;const auto item=value.toObject();
            if(!integer(item["col"],input["c1"].toInt(),input["c2"].toInt()))return false;
            if(key=="sort") {if(item.contains("descending") && !item["descending"].isBool())return false;for(auto it=item.begin();it!=item.end();++it)if(it.key()!="col" && it.key()!="descending")return false;}
            else {
                if(item.contains("value_kind") && (!QStringList{"date","time","datetime"}.contains(item["value_kind"].toString()) || !item["value"].isString() || item["op"].toString()=="contains"))return false;const auto scalar=item.value("value");if(!QStringList{"=","<>",">","<",">=","<=","contains"}.contains(item["op"].toString()))return false;
                if(!scalar.isUndefined() && !scalar.isNull() && !scalar.isBool() && !scalar.isDouble() && !scalar.isString())return false;
                if(scalar.isDouble() && !std::isfinite(scalar.toDouble()))return false;
                if(scalar.isString() && scalar.toString().toUtf8().size()>65536)return false;
                if(item["op"].toString()=="contains" && !scalar.isString())return false;
                for(auto it=item.begin();it!=item.end();++it)if(it.key()!="col" && it.key()!="op" && it.key()!="value" && it.key()!="value_kind")return false;
            }
        }
    }
    result=input;result["header"]=input["header"].toBool();result["sort"]=input["sort"].toArray();result["filters"]=input["filters"].toArray();return true;
}
