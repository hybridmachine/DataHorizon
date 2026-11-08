// SPDX-License-Identifier: GPL-3.0-only
#pragma once
#include <QJsonArray>
#include <QJsonObject>
#include <QJsonDocument>

inline bool validStylePattern(const QJsonArray &rows,int height,int width) {
    if(height<1 || width<1 || qint64(height)*width>10000 || rows.size()!=height)return false;
    for(const auto &row:rows){if(!row.isArray() || row.toArray().size()!=width)return false;for(const auto &style:row.toArray())if(!style.isObject())return false;}
    return QJsonDocument(rows).toJson(QJsonDocument::Compact).size()<=4*1024*1024;
}
inline QJsonArray formatPainterEdits(const QJsonArray &rows,int r1,int c1,int r2,int c2) {
    const int height=rows.size(),width=height?rows.first().toArray().size():0;
    if(!validStylePattern(rows,height,width) || r1<1 || c1<1 || r2<r1 || c2<c1 || r2>1048576 || c2>16384 || qint64(r2-r1+1)*(c2-c1+1)>10000 || (r2-r1+1)%height || (c2-c1+1)%width)return {};
    QJsonArray edits;
    for(int row=r1;row<=r2;++row)for(int col=c1;col<=c2;++col)edits.append(QJsonObject{{"row",row},{"col",col},{"style",rows[(row-r1)%height].toArray()[(col-c1)%width].toObject()},{"replace_style",true}});
    return edits;
}
