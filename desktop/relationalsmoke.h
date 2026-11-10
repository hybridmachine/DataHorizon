// SPDX-License-Identifier: GPL-3.0-only
#pragma once
#include "mainwindow.h"
#include <QApplication>
#include <QJsonArray>
#include <QTimer>
#include <QUuid>
#include <memory>

// Explicit development smoke mode uses the installed GUI's REST transport.
inline void relationalSmoke(MainWindow &window,QApplication &app,const QString &path){
    struct State{int step=0;QString query,job;QJsonObject info;};auto state=std::make_shared<State>();
    auto *client=window.findChild<RestClient *>();
    auto fail=[&app](const QString &error){qCritical("%s",qPrintable(error));app.exit(3);};
    auto base=[&window]{return "/v1/workbooks/"+window.model()->workbook();};
    const QJsonArray customerColumns{QJsonObject{{"name","CustomerID"},{"required",true},{"unique",true}},QJsonObject{{"name","Name"}}};
    const QJsonArray orderColumns{QJsonObject{{"name","OrderID"},{"required",true},{"unique",true}},QJsonObject{{"name","CustomerID"},{"required",true}},QJsonObject{{"name","Quantity"},{"type","integer"},{"required",true}},QJsonObject{{"name","UnitPrice"},{"type","number"},{"required",true}}};
    const QList<QJsonObject> operations{
        {{"action","table_create"},{"name","Customers"},{"columns",customerColumns}},
        {{"action","table_create"},{"name","Orders"},{"columns",orderColumns}},
        {{"action","table_insert"},{"table","Customers"},{"rows",QJsonArray{QJsonObject{{"CustomerID","C001"},{"Name","Ada"}},QJsonObject{{"CustomerID","C002"},{"Name","Grace"}}}}},
        {{"action","table_insert"},{"table","Orders"},{"rows",QJsonArray{QJsonObject{{"OrderID","O100"},{"CustomerID","C001"},{"Quantity",2},{"UnitPrice",12.5}},QJsonObject{{"OrderID","O101"},{"CustomerID","C001"},{"Quantity",1},{"UnitPrice",10}},QJsonObject{{"OrderID","O102"},{"CustomerID","C002"},{"Quantity",3},{"UnitPrice",7}}}}},
        {{"action","relationship_create"},{"name","CustomerOrders"},{"parent_table","Customers"},{"parent_column","CustomerID"},{"child_table","Orders"},{"child_column","CustomerID"}},
        {{"action","table_update"},{"table","Orders"},{"edits",QJsonArray{QJsonObject{{"row_id","1"},{"values",QJsonObject{{"Quantity",4}}}}}}},
        {{"action","undo"}}
    };
    auto *poll=new QTimer(&window);poll->setInterval(30);
    QObject::connect(poll,&QTimer::timeout,&window,[client,state,base,fail,&app,poll]{
        if(state->job.isEmpty())return;
        poll->stop();client->request(base()+"/queries/jobs/"+state->job,{},[client,state,base,fail,&app,poll](QJsonObject result,QString error){
            if(!error.isEmpty()){fail(error);return;}if(result["status"]=="running"){poll->start();return;}if(result["status"]!="completed"){fail("Relational smoke SQL did not complete");return;}
            const QJsonObject scope{{"expected_state",state->info["state"]}};
            client->request(base()+"/queries/jobs/"+state->job+"/page",scope,[client,state,base,scope,fail,&app](QJsonObject page,QString error){
                if(!error.isEmpty()){fail(error);return;}const auto rows=page["rows"].toArray();if(rows.size()!=2 || rows[0].toObject()["values"].toArray()!=QJsonArray{"C001","Ada",2,35} || rows[1].toObject()["values"].toArray()!=QJsonArray{"C002","Grace",1,21}){fail("Relational smoke totals mismatch");return;}
                client->request(base()+"/queries/jobs/"+state->job+"/export/csv",scope,[client,state,base,fail,&app](QJsonObject exported,QString error){
                    if(!error.isEmpty() || exported["records"].toInt()!=2 || !exported["csv"].toString().contains("C001,Ada,2,35")){fail(error.isEmpty()?"Complete query export mismatch":error);return;}
                    client->request(base()+"/queries/jobs/"+state->job+"/release",{},[fail,&app](QJsonObject result,QString error){if(!error.isEmpty() || !result["released"].toBool()){fail(error);return;}app.exit(0);});
                });
            });
        },false);
    });
    QObject::connect(&window,&MainWindow::serviceReady,&window,[&window,path]{window.openBook(path,true);},Qt::SingleShotConnection);
    QObject::connect(&window,&MainWindow::operationFinished,&window,[fail](bool ok){if(!ok)fail("Relational smoke managed operation failed");});
    QObject::connect(&window,&MainWindow::workbookReady,&window,[&window,client,state,operations,base,fail,poll]{
        if(state->step<operations.size()){window.submit(operations[state->step++]);return;}
        if(state->step==operations.size()){
            ++state->step;const QString sql="SELECT c.CustomerID,c.Name,count(o.OrderID) AS OrderCount,coalesce(sum(o.Quantity*o.UnitPrice),0) AS Total FROM Customers c LEFT JOIN Orders o ON o.CustomerID=c.CustomerID GROUP BY c.CustomerID,c.Name HAVING coalesce(sum(o.Quantity*o.UnitPrice),0)>=:minimum_total ORDER BY c.CustomerID";
            client->request(base()+"/queries/preview",{{"name","Customer totals"},{"sql",sql},{"parameters",QJsonArray{QJsonObject{{"name","minimum_total"},{"type","number"}}}},{"defaults",QJsonObject{{"minimum_total",0}}},{"expected_state",window.model()->state()}},[&window,fail,state](QJsonObject result,QString error){if(!error.isEmpty()){fail(error);return;}state->query=result["query"].toObject()["id"].toString();auto operation=result["source_request"].toObject();operation["action"]="query_save";operation["review_digest"]=result["review_digest"];window.submit(operation);});return;
        }
        if(state->step==operations.size()+1){++state->step;client->request(base(),{},[client,state,base,fail,poll](QJsonObject info,QString error){if(!error.isEmpty()){fail(error);return;}state->info=info;state->job=QUuid::createUuid().toString(QUuid::WithoutBraces);client->request(base()+"/queries/jobs",{{"query",state->query},{"job_id",state->job},{"expected_state",info["state"]},{"values",QJsonObject{{"minimum_total",0}}}},[fail,poll](QJsonObject,QString error){if(!error.isEmpty()){fail(error);return;}poll->start();});},false);}
    });
    QTimer::singleShot(25000,&app,[&app]{app.exit(2);});
}
