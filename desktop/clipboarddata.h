// SPDX-License-Identifier: GPL-3.0-only
#pragma once
#include <QJsonArray>
#include <QJsonDocument>
#include <QJsonObject>
#include <QMimeData>

namespace ClipboardData {
constexpr int maxBytes=4*1024*1024,maxCells=10000;
inline bool decode(const QMimeData *mime,QJsonObject &payload,QString &error,int textLimit=maxBytes){
    payload={};
    if(!mime){error="No clipboard data.";return false;}
    if(mime->hasFormat("application/x-datahorizon-cells")){
        const auto bytes=mime->data("application/x-datahorizon-cells");
        if(bytes.size()>maxBytes){error="Clipboard exceeds the 4 MiB limit. Copy a smaller selection.";return false;}
        QJsonParseError parse;const auto document=QJsonDocument::fromJson(bytes,&parse);
        if(parse.error!=QJsonParseError::NoError || !document.isObject()){error="Invalid native clipboard data.";return false;}
        payload=document.object();
    }else{
        auto text=mime->text();
        if(text.size()>textLimit || text.toUtf8().size()>textLimit){error=QString("Clipboard exceeds the %1 limit. Copy a smaller selection.").arg(textLimit>=1024*1024?QString::number(textLimit/(1024*1024))+" MiB":QString::number(textLimit)+" byte");return false;}
        text.replace("\r\n","\n");if(text.endsWith('\n'))text.chop(1);
        if(text.count('\t')+text.count('\n')+1>maxCells){error="Clipboard exceeds the 10,000-cell limit. Copy a smaller selection.";return false;}
        QJsonArray rows;
        for(const auto &line:text.split('\n')){
            QJsonArray row;
            for(const auto &value:line.split('\t'))row.append(QJsonObject{{"source",value},{"input_kind","auto"},{"value",value},{"calculation","current"}});
            rows.append(row);
        }
        payload["rows"]=rows;
    }
    if(payload.contains("rows") || !payload["cut"].toBool()){
        if(!payload["rows"].isArray()){error="Invalid native clipboard rows.";return false;}
        const auto rows=payload["rows"].toArray();int cells=0;
        if(rows.size()>maxCells){error="Clipboard exceeds the 10,000-cell limit.";return false;}
        for(const auto &value:rows){
            if(!value.isArray()){error="Invalid native clipboard rows.";return false;}
            const auto row=value.toArray();cells+=row.size();
            if(cells>maxCells){error="Clipboard exceeds the 10,000-cell limit.";return false;}
            for(const auto &cell:row)if(!cell.isObject()){error="Invalid native clipboard cell.";return false;}
        }
    }
    return true;
}
}
