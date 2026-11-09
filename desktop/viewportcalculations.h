// SPDX-License-Identifier: GPL-3.0-only
#pragma once
#include "restclient.h"
#include <QElapsedTimer>
#include <QPointer>
#include <QSet>
#include <QTimer>
#include <QUuid>
#include <functional>
#include <optional>

// One read-only worker per model, at most sixteen queued cache tiles. Requests
// keep their original handle/state even when selection or the current sheet changes.
class ViewportCalculations : public QObject {
public:
    struct Work { quint64 key; int generation; QString workbook; QJsonObject request; };
    using Wanted=std::function<bool(const Work &)>;
    using Finished=std::function<void(const Work &,QJsonObject,QString)>;
    ViewportCalculations(RestClient *client,QObject *parent,Wanted wanted,Finished finished)
        :QObject(parent),m_client(client),m_wanted(std::move(wanted)),m_finished(std::move(finished)) {
        m_timer.setSingleShot(true);
        connect(&m_timer,&QTimer::timeout,this,[this]{if(m_active)poll();else start();});
        connect(client,&RestClient::configured,this,[this]{
            // Replies for a previous service are suppressed by RestClient. Its
            // workbook-handle lifecycle owns cleanup on that former connection.
            ++m_serial;m_timer.stop();m_queue.clear();m_active.reset();
        });
    }
    ~ViewportCalculations() override {
        if(m_active && m_connection==m_client->generation()) {
            const auto path=m_resource;QPointer<RestClient> client=m_client;
            client->request(path+"/cancel",{},[client,path](QJsonObject reply,QString error){
                if(client && error.isEmpty() && reply["status"]!="running")client->request(path+"/release",{},[](QJsonObject,QString){});
            });
        }
    }
    void enqueue(Work work) {
        if(!m_enabled || !wanted(work))return;
        if(m_active && m_active->key==work.key && m_active->generation==work.generation && !m_drop)return;
        for(int i=m_queue.size()-1;i>=0;--i)if(m_queue[i].key==work.key)m_queue.removeAt(i);
        if(m_queue.size()==16)m_queue.removeFirst();
        m_queue.append(std::move(work));
        if(!m_active)m_timer.start(30);
    }
    void retain(const QSet<quint64> &keys) {
        m_visible=keys;m_visibilityKnown=true;
        for(int i=m_queue.size()-1;i>=0;--i)if(!wanted(m_queue[i]))m_queue.removeAt(i);
        if(m_active && !wanted(*m_active))cancel();
    }
    void invalidate() {m_queue.clear();if(m_active)cancel();}
    void setEnabled(bool enabled) {m_enabled=enabled;if(!enabled)invalidate();}
    int queued() const {return m_queue.size();}
    bool active() const {return m_active.has_value();}
private:
    bool wanted(const Work &work) const {return m_enabled && (!m_visibilityKnown || m_visible.contains(work.key)) && m_wanted(work);}
    void start() {
        if(m_active || !m_enabled)return;
        while(!m_queue.isEmpty()) {
            const auto work=m_queue.takeLast();if(!wanted(work))continue;
            m_active=work;m_drop=false;m_cancelSent=false;m_admitted=false;m_recovering=false;m_connection=m_client->generation();
            m_job=QUuid::createUuid().toString(QUuid::WithoutBraces);
            m_resource="/v1/workbooks/"+work.workbook+"/calculation/jobs/"+m_job;
            auto body=work.request;body["job_id"]=m_job;body["stream_id"]=m_stream;body["preview"]=true;
            const auto serial=++m_serial;QPointer<ViewportCalculations> self=this;m_elapsed.start();
            m_client->request("/v1/workbooks/"+work.workbook+"/calculation/jobs",body,[self,serial](QJsonObject reply,QString error){if(self && serial==self->m_serial)self->handle(reply,error);});
            return;
        }
    }
    void cancel() {
        if(!m_active)return;
        m_drop=true;if(m_cancelSent || !m_admitted)return;m_cancelSent=true;
        const auto serial=m_serial;QPointer<ViewportCalculations> self=this;
        m_client->request(m_resource+"/cancel",{},[self,serial](QJsonObject reply,QString error){if(self && serial==self->m_serial)self->handle(reply,error);});
    }
    void poll() {
        if(!m_active)return;
        if(m_elapsed.elapsed()>20000){finish({},"Background calculation status timed out; refresh to retry.");return;}
        const auto serial=m_serial;QPointer<ViewportCalculations> self=this;
        m_client->request(m_resource,{},[self,serial](QJsonObject reply,QString error){if(self && serial==self->m_serial)self->handle(reply,error);},false);
    }
    void handle(const QJsonObject &reply,const QString &error) {
        if(!m_active)return;
        if(!error.isEmpty()){
            if(reply["code"]=="STALE_STATE"){finish({{"state_changed",true}},{});return;}
            // A lost acknowledgement may have admitted this read-only job.
            // Inspect its captured ID once; never submit another create request.
            if(!reply.contains("code") && !m_recovering) {
                m_recovering=true;const auto serial=m_serial;QPointer<ViewportCalculations> self=this;
                m_client->request(m_resource,{},[self,serial](QJsonObject status,QString failure){if(self && serial==self->m_serial)self->handle(status,failure);},false);
            } else finish({},error);
            return;
        }
        if(reply["job_id"].toString()!=m_job){finish({},"Invalid background calculation acknowledgement.");return;}
        m_admitted=true;
        const auto status=reply["status"].toString();
        if(status=="running"){if(m_drop && !m_cancelSent)cancel();else m_timer.start(50);return;}
        if(status=="stale"){finish({{"state_changed",true}},{});return;}
        if(status=="completed") {
            const auto result=reply["result"].toObject();
            if(reply["stream_id"].toString()!=m_stream || result["state"]!=m_active->request["expected_state"]
                || result["sheet"]!=m_active->request["sheet"] || !reply["generation"].isDouble() || result["generation"]!=reply["generation"]) {
                finish({},"Background calculation does not match its reviewed input state.");return;
            }
            finish(result,{});return;
        }
        finish({},reply["error"].toObject()["message"].toString("Background calculation stopped or became stale; refresh to retry."));
    }
    void finish(QJsonObject snapshot,QString error) {
        if(!m_active)return;
        const auto work=*m_active;const bool publish=!m_drop && wanted(work);
        const auto resource=m_resource;
        ++m_serial;m_timer.stop();m_active.reset();
        m_client->request(resource+"/release",{},[](QJsonObject,QString){});
        if(publish && (!snapshot.isEmpty() || !error.isEmpty()))m_finished(work,snapshot,error);
        if(!m_queue.isEmpty())m_timer.start(30);
    }
    RestClient *m_client;
    Wanted m_wanted;Finished m_finished;
    QTimer m_timer;QElapsedTimer m_elapsed;
    QList<Work> m_queue;std::optional<Work> m_active;
    QSet<quint64> m_visible;
    QString m_resource,m_job,m_stream=QUuid::createUuid().toString(QUuid::WithoutBraces);
    quint64 m_serial=0,m_connection=0;
    bool m_visibilityKnown=false,m_enabled=true,m_drop=false,m_cancelSent=false,m_admitted=false,m_recovering=false;
};
