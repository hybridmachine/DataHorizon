// SPDX-License-Identifier: GPL-3.0-only
#pragma once
#include "restclient.h"
#include <QPointer>
#include <QTimer>

// Owns only the asynchronous cancel/status/release exchange for one captured handle.
class JobCloser : public QObject {
public:
    using Completion=std::function<void(QJsonObject,QString)>;
    JobCloser(RestClient *client,QString base,QString job,QJsonObject status,Completion completion,QObject *parent):
        QObject(parent),m_client(client),m_base(std::move(base)),m_job(std::move(job)),
        m_session(client->generation()),m_completion(std::move(completion)),m_last(std::move(status)) {
        connect(&m_timeout,&QTimer::timeout,this,[this]{finish("Job cancellation or handle release could not be confirmed before the close deadline.");});
        m_timeout.setSingleShot(true);
        connect(client,&RestClient::configured,this,[this]{finish("Service session changed during job close; inspect the retained journal.");});
    }
    void start(){m_timeout.start(10000);request(m_base+"/operation-jobs/"+m_job+"/cancel",false);}
private:
    RestClient *m_client;QString m_base,m_job;quint64 m_session;Completion m_completion;
    QTimer m_timeout;QJsonObject m_last;bool m_done=false;
    void finish(const QString &error={}){
        if(m_done)return;m_done=true;m_timeout.stop();auto completion=m_completion;deleteLater();completion(m_last,error);
    }
    void request(const QString &path,bool releasing,bool post=true){
        QPointer<JobCloser> self=this;
        QJsonObject body;
        if(path.endsWith("/cancel") && m_last.contains("execution_id"))body["execution_id"]=m_last["execution_id"];
        m_client->request(path,body,[self,releasing](QJsonObject result,QString error){
            if(!self || self->m_done)return;
            if(self->m_client->generation()!=self->m_session){self->finish("Service session changed during close.");return;}
            if(!error.isEmpty()){self->finish(error);return;}
            if(releasing){if(result["closed"]!=true)self->finish("Workbook handle release was not acknowledged.");else self->finish();return;}
            if(result["job_id"].toString()!=self->m_job){self->finish("Job close received an unrelated status.");return;}
            if(self->m_last.contains("execution_id") && result["execution_id"]!=self->m_last["execution_id"]){self->finish("Job execution identity changed during close.");return;}
            self->m_last=result;const auto status=result["status"].toString();
            if(QStringList{"completed","cancelled","failed","prepared"}.contains(status))self->request(self->m_base+"/close",true);
            else if(status=="running" || status=="staging")QTimer::singleShot(100,self,[self]{if(self && !self->m_done)self->request(self->m_base+"/operation-jobs/"+self->m_job,false,false);});
            else self->finish("Job outcome remains unresolved; retain the journal and inspect before resuming.");
        },post);
    }
};
