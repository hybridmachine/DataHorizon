// SPDX-License-Identifier: GPL-3.0-only
#pragma once
#include "sheetmodel.h"
#include <QJsonArray>

inline bool validTraceTarget(const QJsonObject &target,const QJsonArray &sheets) {
    auto integer=[](QJsonValue value,int low,int high){return value.isDouble() && value.toDouble()==value.toInt(-1) && value.toInt()>=low && value.toInt()<=high;};
    if(!integer(target["r1"],1,1048576) || !integer(target["r2"],target["r1"].toInt(),1048576) || !integer(target["c1"],1,16384) || !integer(target["c2"],target["c1"].toInt(),16384))return false;
    bool known=false;for(const auto &value:sheets){const auto sheet=value.toObject();if(sheet["id"]==target["sheet"] && sheet["name"]==target["sheet_name"])known=true;}if(!known)return false;
    const auto first=SheetModel::columnName(target["c1"].toInt())+QString::number(target["r1"].toInt()),last=SheetModel::columnName(target["c2"].toInt())+QString::number(target["r2"].toInt());return target["address"].toString()==first+(first==last?QString():":"+last);
}
