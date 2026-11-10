// SPDX-License-Identifier: GPL-3.0-only
#pragma once
#include "restclient.h"
#include "exportwriter.h"
#include <QFileDialog>
#include <QJsonArray>
#include <QMessageBox>
#include <QPointer>
#include <functional>

namespace RelationalExport {
inline QPair<QString,QString> destination(QWidget *parent){
    QString filter;const auto path=QFileDialog::getSaveFileName(parent,QObject::tr("Export complete relational values snapshot"),{},QObject::tr("CSV (*.csv);;Excel workbook (*.xlsx);;OpenDocument spreadsheet (*.ods)"),&filter);
    return {path,filter.contains("*.xlsx")?"xlsx":filter.contains("*.ods")?"ods":"csv"};
}
inline void run(QWidget *parent,RestClient *client,const QJsonObject &info,const QString &endpoint,const QJsonObject &scope,const QString &path,const QString &format,std::function<bool()> current,std::function<void(QString)> done){
    if(path.isEmpty()){done(QObject::tr("Export cancelled."));return;}
    const auto native=info["path"].toString();if(ExportWriter::sameFile(path,native)){done(QObject::tr("Export destination identifies the native workbook."));return;}
    auto request=scope;request["expected_state"]=info["state"];QPointer<QWidget> owner=parent;
    client->request(endpoint+"/"+format,request,[owner,client,info,native,path,current,done](QJsonObject snapshot,QString error){
        if(!owner)return;if(!error.isEmpty()){done(error);return;}if(!current() || snapshot["state"]!=info["state"]){done(QObject::tr("Export inputs changed. Review a fresh snapshot."));return;}
        QStringList report;for(const auto &line:snapshot["report"].toArray())report.append(line.toString());QMessageBox review(QMessageBox::Question,QObject::tr("Review complete values export"),QObject::tr("Export all %1 records and %2 columns from this frozen scope?").arg(snapshot["records"].toInt()).arg(snapshot["columns"].toInt()),QMessageBox::Yes|QMessageBox::Cancel,owner);review.setObjectName("relationalExportReview");review.setOption(QMessageBox::Option::DontUseNativeDialog);review.setTextFormat(Qt::PlainText);review.setInformativeText(report.join('\n'));review.setDefaultButton(QMessageBox::Cancel);
        if(review.exec()!=QMessageBox::Yes){done(QObject::tr("Export cancelled."));return;}if(!current()){done(QObject::tr("Export scope changed during review."));return;}
        client->request("/v1/workbooks/"+info["id"].toString(),{},[owner,info,native,path,current,done,snapshot](QJsonObject fresh,QString error){
            if(!owner)return;if(!error.isEmpty()){done(error);return;}if(!current() || fresh["id"]!=info["id"] || fresh["workbook_id"]!=info["workbook_id"] || fresh["state"]!=info["state"]){done(QObject::tr("Stored inputs changed during review. Refresh and export again."));return;}
            QString failure;if(!ExportWriter::save(path,native,snapshot,failure)){done(failure);return;}done(QObject::tr("Complete values snapshot exported."));
        },false);
    });
}
}
