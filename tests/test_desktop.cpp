// SPDX-License-Identifier: GPL-3.0-only
#include "mainwindow.h"
#include "keyboardgrid.h"
#include <limits>
#include "tracenavigation.h"
#include "formatcellsdialog.h"
#include "cellborders.h"
#include "formatpainter.h"
#include "servicepaths.h"
#include "exportwriter.h"
#include "clipboarddata.h"
#include "relationalworkbench.h"
#include "queryworkbench.h"
#include "conversionreportview.h"
#include "restclient.h"
#include <QEventLoop>
#include <QTextDocument>
#include <QToolButton>
#include <QTabWidget>
#include <QTableWidget>
#include <QTreeWidget>
#include "frozen_row_probe.h"
#include "frozentoprow.h"
#include <QAbstractItemModelTester>
#include <QApplication>
#include <QAction>
#include <QJsonArray>
#include <QLineEdit>
#include <QItemSelectionModel>
#include <QRegularExpression>
#include <QLabel>
#include <QSignalSpy>
#include <QStandardItemModel>
#include <QScopeGuard>
#include <QTableView>
#include <QPlainTextEdit>
#include <QDockWidget>
#include <QPointer>
#include <QPainter>
#include <QInputDialog>
#include "listcriteriadialog.h"
#include "namedlistviews.h"
#include "functionhelp.h"
#include "formulaeditor.h"
#include <QSettings>
#include <QAbstractButton>
#include <QPushButton>
#include <QProgressDialog>
#include <QAbstractItemDelegate>
#include <QClipboard>
#include <QCryptographicHash>
#include <QMenu>
#include <QToolButton>
#include <QComboBox>
#include <QCheckBox>
#include <QSpinBox>
#include <QFile>
#include <QFileInfo>
#include <QFileDialog>
#include <QDir>
#include <QDialogButtonBox>
#include <QElapsedTimer>
#include <QJsonDocument>
#include <QMessageBox>
#include <QMimeData>
#include <QTcpServer>
#include <QTcpSocket>
#include <QNetworkReply>
#include <QNetworkProxy>
#include <QTabBar>
#include <QTimer>
#include <QStyleOptionViewItem>
#include <QTemporaryDir>
#include <QTest>
#include <memory>

class ViewportCalculationServer : public QTcpServer {
public:
    struct Job {QJsonObject request;QString status="running";double value=2;bool wrongState=false;};
    QList<Job> jobs;QList<QPointer<QTcpSocket>> admissions;QJsonArray ranges;int cancellations=0,releases=0;bool completeOnCancel=false,holdAdmissions=false,loseAdmissionReply=false;
    ViewportCalculationServer() {
        listen(QHostAddress::LocalHost);
        connect(this,&QTcpServer::newConnection,this,[this]{
            auto *socket=nextPendingConnection();connect(socket,&QTcpSocket::disconnected,socket,&QObject::deleteLater);
            connect(socket,&QTcpSocket::readyRead,socket,[this,socket]{
                const auto bytes=socket->property("request").toByteArray()+socket->readAll();socket->setProperty("request",bytes);
                const int end=bytes.indexOf("\r\n\r\n");if(end<0 || socket->property("handled").toBool())return;
                const int length=QRegularExpression("Content-Length: (\\d+)",QRegularExpression::CaseInsensitiveOption).match(QString::fromLatin1(bytes.left(end))).captured(1).toInt();
                if(bytes.size()<end+4+length)return;socket->setProperty("handled",true);
                const auto path=QString::fromLatin1(bytes.left(bytes.indexOf("\r\n")).split(' ')[1]);
                const auto body=QJsonDocument::fromJson(bytes.mid(end+4)).object();
                if(path.endsWith("/range")) {
                    ranges.append(body);auto cell=input(body);cell["calculation"]="pending";cell["display"]="…";cell["value"]=QJsonValue::Null;
                    send(socket,{{"state",body.value("expected_state").toString("state")},{"sheet",body["sheet"]},{"cells",QJsonArray{cell}}});return;
                }
                if(path.endsWith("/calculation/jobs")){
                    // Qt 6.8 may retransmit a buffered POST after the peer closes
                    // before a reply. The real service deduplicates this job ID.
                    for(int i=0;i<jobs.size();++i)if(jobs[i].request["job_id"]==body["job_id"]){send(socket,reply(i));return;}
                    jobs.append({body});admissions.append(socket);if(loseAdmissionReply)socket->disconnectFromHost();else if(!holdAdmissions)send(socket,reply(jobs.size()-1));return;
                }
                for(int i=0;i<jobs.size();++i) {
                    const auto resource="/v1/workbooks/book/calculation/jobs/"+jobs[i].request["job_id"].toString();
                    if(path==resource+"/cancel"){++cancellations;jobs[i].status=completeOnCancel?"completed":"cancelled";jobs[i].value=99;send(socket,reply(i));return;}
                    if(path==resource+"/release"){++releases;send(socket,{{"released",true}});return;}
                    if(path==resource){send(socket,reply(i));return;}
                }
                send(socket,{{"message","Unknown fake resource"}});
            });
        });
    }
    QString url() const {return "http://127.0.0.1:"+QString::number(serverPort());}
    void complete(int i,double value) {jobs[i].status="completed";jobs[i].value=value;}
    void acknowledge(int i) {if(admissions[i])send(admissions[i],reply(i));}
private:
    static QJsonObject input(const QJsonObject &request) {
        const auto rows=request["rows"].toArray();const int row=rows.isEmpty()?request["r1"].toInt(1):rows.first().toInt();
        return {{"row",row},{"col",request["c1"].toInt(1)},{"source","=1+1"},{"kind","formula"},{"input_kind","formula"},{"style",QJsonObject{}}};
    }
    QJsonObject reply(int i) const {
        const auto &job=jobs[i];const auto &body=job.request;
        QJsonObject result{{"job_id",body["job_id"]},{"status",job.status},{"state",body["expected_state"]},{"generation",i+1},{"stream_id",body["stream_id"]}};
        if(job.status=="completed") {
            auto cell=input(body);cell["value"]=job.value;cell["display"]=QString::number(job.value);cell["calculation"]="current";
            result["result"]=QJsonObject{{"state",job.wrongState?QJsonValue("other"):body["expected_state"]},{"sheet",body["sheet"]},{"generation",i+1},{"cells",QJsonArray{cell}}};
        }
        if(job.status=="failed")result["error"]=QJsonObject{{"message","Required background snapshot failed"}};
        return result;
    }
    static void send(QTcpSocket *socket,const QJsonObject &result) {
        const auto body=QJsonDocument(result).toJson(QJsonDocument::Compact);
        socket->write("HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nConnection: close\r\nContent-Length: "+QByteArray::number(body.size())+"\r\n\r\n"+body);socket->disconnectFromHost();
    }
};

class DelayedSummaryServer : public QTcpServer {
public:
    struct Pending {QPointer<QTcpSocket> socket;QJsonObject range;};
    QList<Pending> requests,navigationRequests;
    QString state,sheet;
    DelayedSummaryServer(QString version,QString source):state(version),sheet(source) {
        connect(this,&QTcpServer::newConnection,this,[this]{
            auto *socket=nextPendingConnection();connect(socket,&QTcpSocket::disconnected,socket,&QObject::deleteLater);
            connect(socket,&QTcpSocket::readyRead,socket,[this,socket]{
                const auto bytes=socket->property("request").toByteArray()+socket->readAll();socket->setProperty("request",bytes);
                const int end=bytes.indexOf("\r\n\r\n");if(end<0 || socket->property("handled").toBool())return;
                const int length=QRegularExpression("Content-Length: (\\d+)",QRegularExpression::CaseInsensitiveOption).match(QString::fromLatin1(bytes.left(end))).captured(1).toInt();
                if(bytes.size()<end+4+length)return;socket->setProperty("handled",true);
                if(bytes.contains("/navigate HTTP/"))navigationRequests.append({socket,QJsonDocument::fromJson(bytes.mid(end+4)).object()});
                else if(bytes.contains("/summary HTTP/"))requests.append({socket,QJsonDocument::fromJson(bytes.mid(end+4)).object()});
                else send(socket,{{"state",state},{"sheet",sheet},{"cells",QJsonArray{}}});
            });
        });
    }
    void respond(int index,double sum) {
        const auto &pending=requests.at(index);const auto &r=pending.range;
        send(pending.socket,{{"state",state},{"sheet",sheet},{"range",QJsonArray{r["r1"],r["c1"],r["r2"],r["c2"]}},
            {"count",1},{"numeric_count",1},{"selected_cells",1},{"sum",sum},{"average",sum},{"error_count",0},{"numeric_error",QJsonValue::Null}});
    }
    void respondNavigation(int index,int row,int col) {send(navigationRequests.at(index).socket,{{"state",state},{"sheet",sheet},{"row",row},{"col",col},{"basis","stored_inputs"}});}
    void respondInvalid(int index) {send(requests.at(index).socket,{{"state",state},{"sheet",sheet}});}
private:
    static void send(QTcpSocket *socket,QJsonObject result) {
        const auto body=QJsonDocument(result).toJson(QJsonDocument::Compact);
        socket->write("HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nConnection: close\r\nContent-Length: "+QByteArray::number(body.size())+"\r\n\r\n"+body);socket->disconnectFromHost();
    }
};

class DelayedClipboardServer : public QTcpServer {
public:
    struct Pending { QPointer<QTcpSocket> socket; QJsonObject range; };
    QList<Pending> requests;bool holdRanges=false,holdStyles=false;
    DelayedClipboardServer(const QString &state, const QString &sheet) : m_state(state), m_sheet(sheet) {
        connect(this,&QTcpServer::newConnection,this,[this] {
            auto *socket=nextPendingConnection();
            connect(socket,&QTcpSocket::disconnected,socket,&QObject::deleteLater);
            connect(socket,&QTcpSocket::readyRead,socket,[this,socket] {
                const auto request=socket->property("request").toByteArray()+socket->readAll(); socket->setProperty("request",request);
                const auto end=request.indexOf("\r\n\r\n"); if(end<0 || socket->property("handled").toBool()) return;
                const auto length=QRegularExpression("Content-Length: (\\d+)",QRegularExpression::CaseInsensitiveOption)
                    .match(QString::fromLatin1(request.left(end))).captured(1).toInt();
                if(request.size()<end+4+length) return;
                socket->setProperty("handled",true);
                if(request.startsWith("POST ") && (request.contains("/clipboard HTTP/") || request.contains("/list/values HTTP/") || (holdRanges && request.contains("/range HTTP/")) || (holdStyles && request.contains("/styles HTTP/"))))
                    requests.append({socket,QJsonDocument::fromJson(request.mid(end+4)).object()});
                else send(socket,{{"state",m_state},{"sheet",m_sheet},{"cells",QJsonArray{}}});
            });
        });
    }
    void respond(int index, const QString &source, bool rejected = false) {
        const auto &pending=requests.at(index);
        if(rejected) send(pending.socket,{{"code","READ_FAILED"},{"message","Superseded clipboard read failed"}},true);
        else send(pending.socket,{{"state",m_state},{"sheet",m_sheet},{"cells",QJsonArray{QJsonObject{
            {"row",pending.range["r1"]},{"col",pending.range["c1"]},{"source",source},{"display",source},
            {"value",source},{"input_kind","text"},{"kind","text"},{"style",QJsonObject{}}}}}});
    }
    void respondStyles(int index,const QJsonArray &rows){const auto &r=requests.at(index).range;send(requests.at(index).socket,{{"state",m_state},{"sheet",m_sheet},{"range",QJsonArray{r["r1"],r["c1"],r["r2"],r["c2"]}},{"rows",rows}});}
    void respondList(int index,const QString &text) {send(requests.at(index).socket,{{"state",m_state},{"rows",QJsonArray{QJsonArray{QJsonObject{{"source",text},{"display",text},{"input_kind","text"}}}}}});}
private:
    static void send(QTcpSocket *socket, const QJsonObject &result, bool rejected = false) {
        const auto body=QJsonDocument(result).toJson(QJsonDocument::Compact);
        socket->write(QByteArray("HTTP/1.1 ")+(rejected?"409 Conflict":"200 OK")
            +"\r\nContent-Type: application/json\r\nConnection: close\r\nContent-Length: "+QByteArray::number(body.size())+"\r\n\r\n"+body);
        socket->disconnectFromHost();
    }
    QString m_state, m_sheet;
};

class DelayedFindServer : public QTcpServer {
public:
    struct Pending{QPointer<QTcpSocket> socket;QJsonObject input;};QList<Pending> requests;QString state,sheet;
    DelayedFindServer(QString version,QString source):state(version),sheet(source){connect(this,&QTcpServer::newConnection,this,[this]{auto *socket=nextPendingConnection();connect(socket,&QTcpSocket::disconnected,socket,&QObject::deleteLater);connect(socket,&QTcpSocket::readyRead,socket,[this,socket]{
        const auto bytes=socket->property("input").toByteArray()+socket->readAll();socket->setProperty("input",bytes);const int end=bytes.indexOf("\r\n\r\n");if(end<0 || socket->property("handled").toBool())return;
        const int length=QRegularExpression("Content-Length: (\\d+)",QRegularExpression::CaseInsensitiveOption).match(QString::fromLatin1(bytes.left(end))).captured(1).toInt();if(bytes.size()<end+4+length)return;socket->setProperty("handled",true);
        if(bytes.contains("/find HTTP/"))requests.append({socket,QJsonDocument::fromJson(bytes.mid(end+4)).object()});else send(socket,{{"state",state},{"sheet",sheet},{"cells",QJsonArray{}}});
    });});}
    void respond(int index,int count=1200,int singleRow=1){const auto &pending=requests.at(index);const int offset=pending.input["offset"].toInt();QJsonArray matches;for(int i=offset;i<qMin(count,offset+500);++i){const int row=count==1?singleRow:i+1;matches.append(QJsonObject{{"row",row},{"col",1},{"address","A"+QString::number(row)},{"text","hit"}});}send(pending.socket,{{"state",state},{"count",count},{"offset",offset},{"limit",500},{"matches",matches},{"truncated",count>matches.size()}});}
private:
    static void send(QTcpSocket *socket,QJsonObject result){const auto bytes=QJsonDocument(result).toJson(QJsonDocument::Compact);socket->write("HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nConnection: close\r\nContent-Length: "+QByteArray::number(bytes.size())+"\r\n\r\n"+bytes);socket->disconnectFromHost();}
};

class DelayedTraceServer : public QTcpServer {
public:
    QList<QPointer<QTcpSocket>> traces,metadata;QList<QJsonObject> inputs;QString state,sheet,workbook;
    DelayedTraceServer(QString version,QString source,QString book):state(version),sheet(source),workbook(book){connect(this,&QTcpServer::newConnection,this,[this]{auto *socket=nextPendingConnection();connect(socket,&QTcpSocket::disconnected,socket,&QObject::deleteLater);connect(socket,&QTcpSocket::readyRead,socket,[this,socket]{const auto bytes=socket->property("input").toByteArray()+socket->readAll();socket->setProperty("input",bytes);const int end=bytes.indexOf("\r\n\r\n");if(end<0 || socket->property("handled").toBool())return;const int length=QRegularExpression("Content-Length: (\\d+)",QRegularExpression::CaseInsensitiveOption).match(QString::fromLatin1(bytes.left(end))).captured(1).toInt();if(bytes.size()<end+4+length)return;socket->setProperty("handled",true);if(bytes.contains("/trace HTTP/")){traces.append(socket);inputs.append(QJsonDocument::fromJson(bytes.mid(end+4)).object());}else if(bytes.startsWith("GET /v1/workbooks/"))metadata.append(socket);else send(socket,{{"state",state},{"sheet",sheet},{"cells",QJsonArray{}}});});});}
    void respondTrace(QJsonObject target){const auto input=inputs.first();send(traces.first(),{{"state",state},{"sheet",sheet},{"row",input["row"]},{"col",input["col"]},{"direction",input["direction"]},{"semantics","direct_syntactic"},{"targets",QJsonArray{target}},{"issues",QJsonArray{}}});}
    void respondInfo(){send(metadata.first(),{{"id",workbook},{"state",state},{"sheets",QJsonArray{QJsonObject{{"id",sheet},{"name","Sheet1"}}}}});}
private:
    static void send(QTcpSocket *socket,QJsonObject result){const auto bytes=QJsonDocument(result).toJson(QJsonDocument::Compact);socket->write("HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nConnection: close\r\nContent-Length: "+QByteArray::number(bytes.size())+"\r\n\r\n"+bytes);socket->disconnectFromHost();}
};

class DelayedCloseProxy : public QTcpServer {
public:
    struct Pending { QPointer<QTcpSocket> socket; QJsonObject result; QString error; };
    Pending closeReply, metadataReply, rangeReply, openReply;
    bool holdMetadata = false, holdRange = false, holdOpen = false;
    QString closeFailure, closedHandle;
    int mutations = 0, opens = 0, closes = 0, metadataReads = 0;
    DelayedCloseProxy(const QJsonObject &service) : m_upstream(this) {
        m_upstream.configure(service["url"].toString(),service["token"].toString());
        connect(this,&QTcpServer::newConnection,this,[this] {
            auto *socket=nextPendingConnection(); connect(socket,&QTcpSocket::disconnected,socket,&QObject::deleteLater);
            connect(socket,&QTcpSocket::readyRead,socket,[this,socket] {
                const auto request=socket->property("request").toByteArray()+socket->readAll(); socket->setProperty("request",request);
                const auto end=request.indexOf("\r\n\r\n"); if(end<0 || socket->property("handled").toBool()) return;
                const auto length=QRegularExpression("Content-Length: (\\d+)",QRegularExpression::CaseInsensitiveOption)
                    .match(QString::fromLatin1(request.left(end))).captured(1).toInt();
                if(request.size()<end+4+length) return;
                socket->setProperty("handled",true);
                const auto path=QString::fromLatin1(request.left(request.indexOf("\r\n")).split(' ').at(1));
                const auto parts=path.split('/',Qt::SkipEmptyParts);
                const bool post=request.startsWith("POST "), metadata=!post && parts.size()==3 && parts[1]=="workbooks";
                if(path=="/v1/open") ++opens;
                if(path.endsWith("/operations")) ++mutations;
                if(metadata) ++metadataReads;
                if(path.endsWith("/close")) {
                    ++closes; closedHandle=parts[2];
                    if(!closeFailure.isEmpty()) {
                        Pending failed{socket,{},"Close service unavailable"};
                        if(m_closeFailureReleased) failClose(failed); else closeReply=failed;
                        return;
                    }
                }
                const QPointer<QTcpSocket> guarded=socket;
                m_upstream.request(path,QJsonDocument::fromJson(request.mid(end+4)).object(),
                    [this,guarded,path,metadata](QJsonObject result,QString error) {
                        if(!guarded) return;
                        Pending response{guarded,result,error};
                        if(path.endsWith("/close") && error.isEmpty() && result["closed"].toBool()) closeReply=response;
                        else if(path=="/v1/open" && holdOpen) openReply=response;
                        else if(metadata && holdMetadata) metadataReply=response;
                        else if(path.endsWith("/range") && holdRange) rangeReply=response;
                        else send(response);
                    },post);
            });
        });
    }
    void releaseClose() {
        if(!closeFailure.isEmpty()) { m_closeFailureReleased=true; failClose(closeReply); }
        else send(closeReply);
        closeReply={};
    }
    void releaseMetadata() { holdMetadata=false; send(metadataReply); }
    void releaseRange() { holdRange=false; send(rangeReply); }
    void releaseOpen() { holdOpen=false; send(openReply); }
private:
    void failClose(const Pending &response) {
        if(closeFailure=="disconnect") response.socket->abort(); else send(response);
    }
    static void send(const Pending &response) {
        const auto body=QJsonDocument(response.error.isEmpty()?response.result:QJsonObject{{"code","UNAVAILABLE"},{"message",response.error}})
            .toJson(QJsonDocument::Compact);
        response.socket->write(QByteArray("HTTP/1.1 ")+(response.error.isEmpty()?"200 OK":"503 Service Unavailable")
            +"\r\nContent-Type: application/json\r\nConnection: close\r\nContent-Length: "+QByteArray::number(body.size())+"\r\n\r\n"+body);
        response.socket->disconnectFromHost();
    }
    RestClient m_upstream;
    bool m_closeFailureReleased = false;
};


// Forwards JSON calls to a real service, but reports a different SHA-256 for
// a completed export job and serves a short substitute body for its download.
class ExportDigestProxy : public QTcpServer {
public:
    bool patched=false, served=false;
    explicit ExportDigestProxy(const QJsonObject &service) : m_upstream(this) {
        m_upstream.configure(service["url"].toString(),service["token"].toString());
        connect(this,&QTcpServer::newConnection,this,[this] {
            auto *socket=nextPendingConnection(); connect(socket,&QTcpSocket::disconnected,socket,&QObject::deleteLater);
            connect(socket,&QTcpSocket::readyRead,socket,[this,socket] {
                const auto request=socket->property("request").toByteArray()+socket->readAll(); socket->setProperty("request",request);
                const auto end=request.indexOf("\r\n\r\n"); if(end<0 || socket->property("handled").toBool()) return;
                const auto length=QRegularExpression("Content-Length: (\\d+)",QRegularExpression::CaseInsensitiveOption)
                    .match(QString::fromLatin1(request.left(end))).captured(1).toInt();
                if(request.size()<end+4+length) return;
                socket->setProperty("handled",true);
                const auto path=QString::fromLatin1(request.left(request.indexOf("\r\n")).split(' ').at(1));
                const bool post=request.startsWith("POST ");
                if(!post && path.startsWith("/v1/exports/")) {
                    served=true;
                    socket->write("HTTP/1.1 200 OK\r\nContent-Type: application/octet-stream\r\nConnection: close\r\nContent-Length: "+QByteArray::number(m_body.size())+"\r\n\r\n"+m_body);
                    socket->disconnectFromHost(); return;
                }
                const QPointer<QTcpSocket> guarded=socket;
                m_upstream.request(path,QJsonDocument::fromJson(request.mid(end+4)).object(),[this,guarded,path](QJsonObject result,QString error) {
                    if(!guarded) return;
                    if(error.isEmpty() && path.startsWith("/v1/jobs/") && result["result"].toObject().contains("export_id")) {
                        auto value=result["result"].toObject(); value["bytes"]=double(m_body.size()); value["sha256"]=QString(64,'0');
                        result["result"]=value; patched=true;
                    }
                    const auto body=QJsonDocument(error.isEmpty() || result.contains("code")?result:QJsonObject{{"code","UNAVAILABLE"},{"message",error}}).toJson(QJsonDocument::Compact);
                    guarded->write(QByteArray("HTTP/1.1 ")+(error.isEmpty()?"200 OK":"409 Conflict")
                        +"\r\nContent-Type: application/json\r\nConnection: close\r\nContent-Length: "+QByteArray::number(body.size())+"\r\n\r\n"+body);
                    guarded->disconnectFromHost();
                },post);
            });
        });
    }
private:
    RestClient m_upstream;
    const QByteArray m_body="PK substitute export body";
};

class JobLifecycleServer : public QTcpServer {
public:
    QString failure;QStringList requests;int cancellations=0,releases=0,polls=0;bool running=false,cancelled=false,committed=false;
    QJsonObject info,job;
    explicit JobLifecycleServer(const QString &path){
        info={{"id","handle"},{"workbook_id","workbook"},{"path",path},{"state","epoch:0"},{"readonly",false},{"sheets",QJsonArray{QJsonObject{{"id","sheet"},{"name","Sheet1"}}}}};
        job={{"job_id","retained"},{"execution_id","0123456789abcdef0123456789abcdef"},{"status","prepared"},{"expected_state","epoch:0"},{"plan_digest","reviewed"},{"total",3},{"next",0},{"committed_batches",0},{"mode","batches"},{"action","edit"}};
        connect(this,&QTcpServer::newConnection,this,[this]{auto *socket=nextPendingConnection();connect(socket,&QTcpSocket::disconnected,socket,&QObject::deleteLater);connect(socket,&QTcpSocket::readyRead,socket,[this,socket]{
            auto bytes=socket->property("request").toByteArray()+socket->readAll();socket->setProperty("request",bytes);const auto end=bytes.indexOf("\r\n\r\n");if(end<0 || socket->property("handled").toBool())return;
            const auto length=QRegularExpression("Content-Length: (\\d+)",QRegularExpression::CaseInsensitiveOption).match(QString::fromLatin1(bytes.left(end))).captured(1).toInt();if(bytes.size()<end+4+length)return;socket->setProperty("handled",true);
            const auto path=QString::fromLatin1(bytes.left(bytes.indexOf("\r\n")).split(' ').at(1));requests.append(path);QJsonObject result;bool failed=false;
            if(path=="/v1/capabilities")result={{"api_version",1}};
            else if(path=="/v1/open" || path=="/v1/workbooks/handle")result=info;
            else if(path.endsWith("/close")){++releases;failed=failure=="close";result={{"closed",true}};}
            else if(path.endsWith("/start") || path.endsWith("/resume")){running=true;job["status"]="running";result=job;}
            else if(path.endsWith("/cancel")){++cancellations;failed=failure=="cancel";cancelled=true;result=job;}
            else if(path.contains("/operation-jobs/")){
                if(cancelled){++polls;failed=failure=="status";job["status"]=committed?"completed":"cancelled";if(committed){job["next"]=3;job["committed_batches"]=1;}}
                result=job;
            }
            else result={{"state","epoch:0"},{"sheet","sheet"},{"cells",QJsonArray{}},{"functions",QJsonArray{}}};
            if(failed)result={{"message","injected lost acknowledgement"},{"code","UNAVAILABLE"},{"outcome","unknown"}};
            const auto body=QJsonDocument(result).toJson(QJsonDocument::Compact);socket->write(QByteArray("HTTP/1.1 ")+(failed?"503 Unavailable":"200 OK")+"\r\nContent-Type: application/json\r\nConnection: close\r\nContent-Length: "+QByteArray::number(body.size())+"\r\n\r\n"+body);socket->disconnectFromHost();
        });});
    }
};

class DesktopTests : public QObject {
    Q_OBJECT
private slots:
    void savedSqlViewsReopenWithoutExecutionAndExportCompleteResults() {
        QTemporaryDir temp;const auto path=temp.filePath("views.dhwb");StartupOptions options;options.automaticWorkbook=false;MainWindow window(options);window.show();QSignalSpy ready(&window,&MainWindow::serviceReady);QTRY_COMPARE_WITH_TIMEOUT(ready.count(),1,10000);QSignalSpy opened(&window,&MainWindow::workbookReady);window.openBook(path,true);QTRY_COMPARE_WITH_TIMEOUT(opened.count(),1,10000);
        window.submit({{"action","table_create"},{"name","Data"},{"columns",QJsonArray{QJsonObject{{"name","Value"},{"type","integer"}}}}});QTRY_COMPARE_WITH_TIMEOUT(opened.count(),2,10000);QJsonArray rows;for(int i=0;i<300;++i)rows.append(QJsonObject{{"Value",i}});window.submit({{"action","table_insert"},{"table","Data"},{"rows",rows}});QTRY_COMPARE_WITH_TIMEOUT(opened.count(),3,10000);
        auto *pages=window.findChild<QTabWidget *>("relationalPages");pages->setCurrentIndex(1);window.findChild<QPlainTextEdit *>("querySql")->setPlainText("SELECT Value FROM Data ORDER BY Value");int phase=0;QTimer timer;timer.setInterval(10);connect(&timer,&QTimer::timeout,&window,[&]{auto *dialog=qobject_cast<QDialog *>(QApplication::activeModalWidget());if(!dialog)return;
            if(phase==0 && dialog->objectName()=="saveQueryOptions"){phase=1;dialog->findChild<QLineEdit *>("savedQueryNameInput")->setText("Values view");dialog->findChild<QDialogButtonBox *>()->button(QDialogButtonBox::Ok)->click();}
            else if(phase==1 && dialog->objectName()=="queryDefinitionReview"){phase=2;dialog->findChild<QDialogButtonBox *>()->button(QDialogButtonBox::Apply)->click();}
            else if((phase==2 || phase==5) && dialog->objectName()=="relationalExportReview"){QVERIFY(qobject_cast<QMessageBox *>(dialog)->text().contains("300"));++phase;qobject_cast<QMessageBox *>(dialog)->button(QMessageBox::Yes)->click();}
            else if(phase==3 && dialog->objectName()=="discardSqlOnOpen"){phase=4;qobject_cast<QMessageBox *>(dialog)->button(QMessageBox::Yes)->click();}
        });timer.start();window.findChild<QPushButton *>("saveQueryDefinition")->click();QTRY_COMPARE_WITH_TIMEOUT(opened.count(),4,10000);QVERIFY(!window.findChild<QueryWorkbench *>()->hasActiveJob());QCOMPARE(window.findChild<QTableWidget *>("queryResults")->rowCount(),0);
        window.findChild<QPushButton *>("runQuery")->click();auto *grid=window.findChild<QTableWidget *>("queryResults");QTRY_COMPARE_WITH_TIMEOUT(grid->rowCount(),128,10000);const auto output=temp.filePath("query.csv");window.findChild<QueryWorkbench *>()->exportTo(output,"csv");QTRY_VERIFY_WITH_TIMEOUT(QFileInfo::exists(output),5000);QFile result(output);QVERIFY(result.open(QIODevice::ReadOnly));const auto bytes=result.readAll();QCOMPARE(bytes.split('\n').size(),302);QVERIFY(bytes.endsWith("299\r\n"));
        window.openBook(path);QTRY_COMPARE_WITH_TIMEOUT(opened.count(),5,10000);window.findChild<QAction *>("relationalWorkbenchAction")->trigger();auto *objects=window.findChild<QTreeWidget *>("relationalObjects");QTRY_VERIFY_WITH_TIMEOUT(objects->topLevelItemCount()==3 && objects->topLevelItem(2)->childCount()==1,5000);objects->setCurrentItem(objects->topLevelItem(2)->child(0));QTRY_COMPARE_WITH_TIMEOUT(window.findChild<QLabel *>("savedQueryName")->text(),QString("Values view"),5000);QVERIFY(!window.findChild<QueryWorkbench *>()->hasActiveJob());QCOMPARE(grid->rowCount(),0);
        phase=5;pages->setCurrentIndex(0);objects->setCurrentItem(objects->topLevelItem(0)->child(0));const auto tableOutput=temp.filePath("table.csv");auto *workbench=window.findChild<RelationalWorkbench *>();workbench->exportTableTo(tableOutput,"csv");QTRY_VERIFY_WITH_TIMEOUT(QFileInfo::exists(tableOutput),5000);timer.stop();QCOMPARE(phase,6);
        const auto alias=temp.filePath("native-alias.csv");QVERIFY(QFile::link(path,alias));QFile native(path);QVERIFY(native.open(QIODevice::ReadOnly));const auto before=QCryptographicHash::hash(native.readAll(),QCryptographicHash::Sha256);native.close();workbench->exportTableTo(alias,"csv");QVERIFY(native.open(QIODevice::ReadOnly));QCOMPARE(QCryptographicHash::hash(native.readAll(),QCryptographicHash::Sha256),before);
    }
    void readonlySqlWorkbenchBindsParametersPagesResultsAndMarksChangesStale() {
        QTemporaryDir temp;StartupOptions options;options.automaticWorkbook=false;MainWindow window(options);window.show();QSignalSpy ready(&window,&MainWindow::serviceReady);QTRY_COMPARE_WITH_TIMEOUT(ready.count(),1,10000);QSignalSpy opened(&window,&MainWindow::workbookReady);window.openBook(temp.filePath("query.dhwb"),true);QTRY_COMPARE_WITH_TIMEOUT(opened.count(),1,10000);
        window.submit({{"action","table_create"},{"name","Data"},{"columns",QJsonArray{QJsonObject{{"name","Value"},{"type","integer"}}}}});QTRY_COMPARE_WITH_TIMEOUT(opened.count(),2,10000);QJsonArray rows;for(int i=0;i<300;++i)rows.append(QJsonObject{{"Value",i}});window.submit({{"action","table_insert"},{"table","Data"},{"rows",rows}});QTRY_COMPARE_WITH_TIMEOUT(opened.count(),3,10000);
        window.findChild<QTabWidget *>("relationalPages")->setCurrentIndex(1);auto *sql=window.findChild<QPlainTextEdit *>("querySql");sql->setPlainText("SELECT Value, :label AS Label FROM Data WHERE Value >= :minimum ORDER BY Value DESC");auto *parameters=window.findChild<QTableWidget *>("queryParameters");window.findChild<QPushButton *>("addQueryParameter")->click();parameters->item(0,0)->setText("minimum");qobject_cast<QComboBox *>(parameters->cellWidget(0,1))->setCurrentText("integer");window.findChild<QPushButton *>("addQueryParameter")->click();parameters->item(1,0)->setText("label");
        QTimer timer;timer.setInterval(10);connect(&timer,&QTimer::timeout,&window,[&]{auto *dialog=qobject_cast<QDialog *>(QApplication::activeModalWidget());if(!dialog || dialog->objectName()!="queryParameterValues")return;auto *button=dialog->findChild<QDialogButtonBox *>()->button(QDialogButtonBox::Save);if(!button->isEnabled())return;dialog->findChild<QLineEdit *>("recordInput_0")->setText("40");dialog->findChild<QLineEdit *>("recordInput_1")->setText("=literal");button->click();});timer.start();window.findChild<QPushButton *>("runQuery")->click();timer.stop();
        auto *grid=window.findChild<QTableWidget *>("queryResults");QTRY_VERIFY_WITH_TIMEOUT(grid->item(0,0) && grid->item(0,0)->text()=="299",10000);QCOMPARE(grid->item(0,1)->text(),QString("=literal"));QVERIFY(!(grid->model()->flags(grid->model()->index(0,0))&Qt::ItemIsEditable));
        window.findChild<QPushButton *>("nextQueryPage")->click();QTRY_VERIFY_WITH_TIMEOUT(grid->item(0,0) && grid->item(0,0)->text()=="171",5000);window.findChild<QPushButton *>("previousQueryPage")->click();QTRY_VERIFY_WITH_TIMEOUT(grid->item(0,0) && grid->item(0,0)->text()=="299",5000);
        grid->setRangeSelected(QTableWidgetSelectionRange(0,0,0,1),true);window.findChild<QPushButton *>("copyQueryValues")->click();QTRY_COMPARE_WITH_TIMEOUT(QApplication::clipboard()->text(),QString("299\t=literal"),5000);
        QCOMPARE(window.findChild<QTableWidget *>("tableRecords")->rowCount(),0);QVERIFY(window.findChild<RelationalWorkbench *>()->cacheMetrics()["estimated_bytes"].toInteger()<16*1024*1024);
        window.submit({{"action","table_update"},{"table","Data"},{"edits",QJsonArray{QJsonObject{{"row_id","1"},{"values",QJsonObject{{"Value",500}}}}}}});QTRY_COMPARE_WITH_TIMEOUT(opened.count(),4,10000);QTRY_COMPARE_WITH_TIMEOUT(grid->rowCount(),0,5000);QVERIFY(sql->toPlainText().contains(":minimum"));
        timer.start();window.findChild<QPushButton *>("runQuery")->click();timer.stop();QTRY_VERIFY_WITH_TIMEOUT(grid->item(0,0) && grid->item(0,0)->text()=="500",10000);
        parameters->setRowCount(0);sql->setPlainText("DELETE FROM Data");window.findChild<QPushButton *>("runQuery")->click();QTRY_VERIFY_WITH_TIMEOUT(window.findChild<QLabel *>("queryStatus")->text().contains("accepts one read-only"),5000);QCOMPARE(grid->rowCount(),0);
    }
    void closingSqlWorkbenchCancelsPendingSnapshotAndReleasesItsJob() {
        QTemporaryDir temp;StartupOptions options;options.automaticWorkbook=false;MainWindow window(options);window.show();QSignalSpy ready(&window,&MainWindow::serviceReady);QTRY_COMPARE_WITH_TIMEOUT(ready.count(),1,10000);QSignalSpy opened(&window,&MainWindow::workbookReady);window.openBook(temp.filePath("cancel-query.dhwb"),true);QTRY_COMPARE_WITH_TIMEOUT(opened.count(),1,10000);
        window.findChild<QAction *>("relationalWorkbenchAction")->trigger();window.findChild<QTabWidget *>("relationalPages")->setCurrentIndex(1);window.findChild<QPlainTextEdit *>("querySql")->setPlainText("WITH RECURSIVE n(x) AS (VALUES(1) UNION ALL SELECT x+1 FROM n WHERE x<1000000000) SELECT sum(x) FROM n");window.findChild<QPushButton *>("runQuery")->click();QVERIFY(window.findChild<QueryWorkbench *>()->hasActiveJob());QTimer discard;discard.setInterval(10);connect(&discard,&QTimer::timeout,&window,[]{auto *review=qobject_cast<QMessageBox *>(QApplication::activeModalWidget());if(review && review->objectName()=="discardQueryDraft")review->button(QMessageBox::Yes)->click();});discard.start();window.close();QTRY_VERIFY_WITH_TIMEOUT(!window.isVisible(),10000);QVERIFY(!window.findChild<QueryWorkbench *>()->hasActiveJob());
    }
    void relationshipsAndOrphanRepairUseReviewedRestOperations() {
        QTemporaryDir temp;const auto path=temp.filePath("relations.dhwb");StartupOptions options;options.automaticWorkbook=false;MainWindow window(options);window.show();QSignalSpy ready(&window,&MainWindow::serviceReady);QTRY_COMPARE_WITH_TIMEOUT(ready.count(),1,10000);
        QSignalSpy opened(&window,&MainWindow::workbookReady);window.openBook(path,true);QTRY_COMPARE_WITH_TIMEOUT(opened.count(),1,10000);
        window.submit({{"action","table_create"},{"name","Customers"},{"columns",QJsonArray{QJsonObject{{"name","ID"},{"required",true},{"unique",true}}}}});QTRY_COMPARE_WITH_TIMEOUT(opened.count(),2,10000);
        window.submit({{"action","table_create"},{"name","Orders"},{"columns",QJsonArray{QJsonObject{{"name","Customer"}}}}});QTRY_COMPARE_WITH_TIMEOUT(opened.count(),3,10000);
        window.submit({{"action","table_insert"},{"table","Customers"},{"rows",QJsonArray{QJsonObject{{"ID","C001"}}}}});QTRY_COMPARE_WITH_TIMEOUT(opened.count(),4,10000);
        QTRY_VERIFY_WITH_TIMEOUT(window.findChild<QTreeWidget *>("relationalObjects")->topLevelItemCount()>=1 && window.findChild<QTreeWidget *>("relationalObjects")->topLevelItem(0)->childCount()==2,5000);
        int phase=0;QTimer timer;timer.setInterval(10);connect(&timer,&QTimer::timeout,&window,[&]{auto *dialog=qobject_cast<QDialog *>(QApplication::activeModalWidget());if(!dialog)return;
            if(phase==0 && dialog->objectName()=="relationshipsDialog"){phase=1;auto *button=dialog->findChild<QPushButton *>("createRelationship");QTimer::singleShot(0,button,&QPushButton::click);}
            else if(phase==1 && dialog->objectName()=="createRelationshipDialog"){phase=2;dialog->findChild<QLineEdit *>("relationshipName")->setText("CustomerOrders");auto *child=dialog->findChild<QComboBox *>("relationshipChild");child->setCurrentIndex(child->findText("Orders.Customer (text)"));dialog->findChild<QDialogButtonBox *>()->button(QDialogButtonBox::Apply)->click();}
            else if(phase==3 && dialog->objectName()=="relationshipViolationInspector"){phase=4;auto *orphans=dialog->findChild<QTableWidget *>("relationshipOrphans");QCOMPARE(orphans->rowCount(),1);orphans->setCurrentCell(0,0);auto *button=dialog->findChild<QPushButton *>("repairRelationshipKeys");QTimer::singleShot(0,button,&QPushButton::click);}
            else if(phase==4 && dialog->objectName()=="recordInputDialog"){phase=5;dialog->findChild<QCheckBox *>("recordNull_0")->setChecked(false);dialog->findChild<QLineEdit *>("recordInput_0")->setText("C001");dialog->findChild<QDialogButtonBox *>()->button(QDialogButtonBox::Save)->click();}
        });timer.start();window.findChild<QPushButton *>("tableRelationships")->click();QTRY_COMPARE_WITH_TIMEOUT(opened.count(),5,10000);timer.stop();QCOMPARE(phase,2);
        QProcess direct;direct.start(qEnvironmentVariable("DATAHORIZON_PYTHON","python3"),{"-c","import sqlite3,sys; db=sqlite3.connect(sys.argv[1]); db.execute(\"INSERT INTO Orders(Customer) VALUES('missing')\"); db.commit(); db.close()",path});QVERIFY(direct.waitForFinished(10000));QCOMPARE(direct.exitCode(),0);
        window.openBook(path,false,false,true);QTRY_COMPARE_WITH_TIMEOUT(opened.count(),6,10000);QTRY_VERIFY_WITH_TIMEOUT(!window.findChild<QPushButton *>("addTableRecord")->isEnabled(),5000);
        QTRY_VERIFY_WITH_TIMEOUT(window.findChild<QTreeWidget *>("relationalObjects")->topLevelItemCount()>=1 && window.findChild<QTreeWidget *>("relationalObjects")->topLevelItem(0)->childCount()==2,5000);
        phase=3;timer.start();window.findChild<QPushButton *>("relationshipViolations")->click();QTRY_COMPARE_WITH_TIMEOUT(opened.count(),7,10000);timer.stop();QCOMPARE(phase,5);
        window.openBook(path);QTRY_COMPARE_WITH_TIMEOUT(opened.count(),8,10000);
    }
    void tableFullScopeBrowseUsesKeysetPagesAndReleasesHiddenSheetCache() {
        QTemporaryDir temp;StartupOptions options;options.automaticWorkbook=false;MainWindow window(options);window.show();QSignalSpy ready(&window,&MainWindow::serviceReady);QTRY_COMPARE_WITH_TIMEOUT(ready.count(),1,10000);
        QSignalSpy opened(&window,&MainWindow::workbookReady);window.openBook(temp.filePath("browse.dhwb"),true);QTRY_COMPARE_WITH_TIMEOUT(opened.count(),1,10000);
        window.submit({{"action","table_create"},{"name","Data"},{"columns",QJsonArray{QJsonObject{{"name","Qty"},{"type","integer"}}}}});QTRY_COMPARE_WITH_TIMEOUT(opened.count(),2,10000);
        QJsonArray rows;for(int i=0;i<300;++i)rows.append(QJsonObject{{"Qty",i}});window.submit({{"action","table_insert"},{"table","Data"},{"rows",rows}});QTRY_COMPARE_WITH_TIMEOUT(opened.count(),3,10000);
        auto *grid=window.findChild<QTableWidget *>("tableRecords");QTRY_COMPARE_WITH_TIMEOUT(grid->rowCount(),128,5000);
        int phase=0;QTimer timer;timer.setInterval(10);connect(&timer,&QTimer::timeout,&window,[&]{auto *dialog=qobject_cast<QDialog *>(QApplication::activeModalWidget());if(phase || !dialog || dialog->objectName()!="listCriteriaDialog")return;phase=1;
            dialog->findChild<QPushButton *>("addSort")->click();auto *sort=dialog->findChild<QTableWidget *>("sortCriteria");qobject_cast<QComboBox *>(sort->cellWidget(0,1))->setCurrentIndex(1);
            dialog->findChild<QPushButton *>("addFilter")->click();auto *filters=dialog->findChild<QTableWidget *>("filterCriteria");qobject_cast<QComboBox *>(filters->cellWidget(0,1))->setCurrentText(">");qobject_cast<QComboBox *>(filters->cellWidget(0,2))->setCurrentIndex(1);qobject_cast<QLineEdit *>(filters->cellWidget(0,3))->setText("50");dialog->findChild<QDialogButtonBox *>()->button(QDialogButtonBox::Ok)->click();});timer.start();window.findChild<QPushButton *>("tableBrowseOptions")->click();timer.stop();
        QTRY_VERIFY_WITH_TIMEOUT(grid->item(0,0) && grid->item(0,0)->text()=="299",5000);window.findChild<QPushButton *>("nextTablePage")->click();QTRY_VERIFY_WITH_TIMEOUT(grid->item(0,0) && grid->item(0,0)->text()=="171",5000);
        window.findChild<QPushButton *>("previousTablePage")->click();QTRY_VERIFY_WITH_TIMEOUT(grid->item(0,0) && grid->item(0,0)->text()=="299",5000);
        auto *workbench=window.findChild<RelationalWorkbench *>();QVERIFY(workbench->cacheMetrics()["estimated_bytes"].toInteger()<16*1024*1024);QCOMPARE(window.model()->cacheMetrics()["tile_charge_bytes"].toInt(),0);
        window.findChild<QPushButton *>("returnToSheets")->click();QCOMPARE(workbench->cacheMetrics()["loaded_values"].toInt(),0);
    }
    void typedTableRecordEditorRetainsInvalidInputAndUsesManagedHistory() {
        QTemporaryDir temp;StartupOptions options;options.automaticWorkbook=false;MainWindow window(options);window.show();
        QSignalSpy ready(&window,&MainWindow::serviceReady);QTRY_COMPARE_WITH_TIMEOUT(ready.count(),1,10000);
        QSignalSpy opened(&window,&MainWindow::workbookReady);window.openBook(temp.filePath("records.dhwb"),true);QTRY_COMPARE_WITH_TIMEOUT(opened.count(),1,10000);
        window.submit({{"action","table_create"},{"name","Data"},{"columns",QJsonArray{QJsonObject{{"name","ID"},{"required",true}},QJsonObject{{"name","Qty"},{"type","integer"},{"required",true}}}}});
        QTRY_COMPARE_WITH_TIMEOUT(opened.count(),2,10000);auto *grid=window.findChild<QTableWidget *>("tableRecords");
        QTRY_COMPARE_WITH_TIMEOUT(window.findChild<QTableWidget *>("tableSchema")->rowCount(),2,5000);
        QTRY_VERIFY_WITH_TIMEOUT(window.findChild<QPushButton *>("addTableRecord")->isEnabled(),5000);
        int phase=0;QTimer timer;timer.setInterval(10);connect(&timer,&QTimer::timeout,&window,[&]{
            auto *dialog=qobject_cast<QDialog *>(QApplication::activeModalWidget());if(!dialog || dialog->objectName()!="recordInputDialog")return;
            if(phase==0){phase=1;dialog->findChild<QLineEdit *>("recordInput_0")->setText("=literal");dialog->findChild<QLineEdit *>("recordInput_1")->setText("3");dialog->findChild<QDialogButtonBox *>()->button(QDialogButtonBox::Save)->click();}
            else if(phase==2){phase=3;dialog->findChild<QLineEdit *>("recordInput_0")->setText("bad");dialog->findChild<QDialogButtonBox *>()->button(QDialogButtonBox::Save)->click();}
            else if(phase==3 && dialog->findChild<QLabel *>("recordEditStatus")->text().contains("Record 1")){phase=4;QCOMPARE(dialog->findChild<QLineEdit *>("recordInput_0")->text(),QString("bad"));dialog->findChild<QLineEdit *>("recordInput_0")->setText("7");dialog->findChild<QDialogButtonBox *>()->button(QDialogButtonBox::Save)->click();}
        });timer.start();window.findChild<QPushButton *>("addTableRecord")->click();QTRY_COMPARE_WITH_TIMEOUT(opened.count(),3,10000);QTRY_COMPARE_WITH_TIMEOUT(grid->rowCount(),1,5000);
        QCOMPARE(grid->item(0,0)->text(),QString("=literal"));QCOMPARE(grid->item(0,1)->text(),QString("3"));const auto identity=grid->item(0,1)->data(Qt::UserRole);
        phase=2;grid->setCurrentCell(0,1);grid->setFocus();QTest::keyClick(grid,Qt::Key_F2);QTRY_COMPARE_WITH_TIMEOUT(opened.count(),4,10000);timer.stop();QCOMPARE(phase,4);
        QTRY_VERIFY_WITH_TIMEOUT(grid->item(0,1) && grid->item(0,1)->text()=="7",5000);QCOMPARE(grid->item(0,1)->data(Qt::UserRole),identity);
        window.findChild<QPushButton *>("undoTableRecords")->click();QTRY_COMPARE_WITH_TIMEOUT(opened.count(),5,10000);QTRY_VERIFY_WITH_TIMEOUT(grid->item(0,1) && grid->item(0,1)->text()=="3",5000);
    }
    void tableSchemaReviewPreservesObjectIdentityAndAppliesConstraints() {
        QTemporaryDir temp;StartupOptions options;options.automaticWorkbook=false;MainWindow window(options);window.show();
        QSignalSpy ready(&window,&MainWindow::serviceReady);QTRY_COMPARE_WITH_TIMEOUT(ready.count(),1,10000);
        QSignalSpy opened(&window,&MainWindow::workbookReady);window.openBook(temp.filePath("schema.dhwb"),true);QTRY_COMPARE_WITH_TIMEOUT(opened.count(),1,10000);
        window.submit({{"action","table_create"},{"name","Data"},{"columns",QJsonArray{QJsonObject{{"name","ID"}},QJsonObject{{"name","Value"},{"type","number"}}}}});
        QTRY_COMPARE_WITH_TIMEOUT(opened.count(),2,10000);auto *schema=window.findChild<QTableWidget *>("tableSchema");QTRY_COMPARE_WITH_TIMEOUT(schema->rowCount(),2,5000);
        auto *objects=window.findChild<QTreeWidget *>("relationalObjects");const auto id=objects->currentItem()->data(0,Qt::UserRole);
        int phase=0;QTimer timer;timer.setInterval(10);connect(&timer,&QTimer::timeout,&window,[&]{
            auto *dialog=qobject_cast<QDialog *>(QApplication::activeModalWidget());if(!dialog)return;
            if(phase==0 && dialog->objectName()=="tableDefinitionDialog") {
                phase=1;dialog->findChild<QLineEdit *>("tableName")->setText("Renamed");auto *columns=dialog->findChild<QTableWidget *>("columnDefinitions");
                columns->item(0,0)->setText("Code");qobject_cast<QCheckBox *>(columns->cellWidget(0,2))->setChecked(true);qobject_cast<QCheckBox *>(columns->cellWidget(0,3))->setChecked(true);
                qobject_cast<QComboBox *>(columns->cellWidget(1,1))->setCurrentText("integer");dialog->findChild<QDialogButtonBox *>()->button(QDialogButtonBox::Ok)->click();
            } else if(phase==1 && qobject_cast<QInputDialog *>(dialog)){phase=2;dialog->accept();}
            else if(phase==2 && dialog->objectName()=="tableSchemaReview"){phase=3;dialog->findChild<QDialogButtonBox *>()->button(QDialogButtonBox::Apply)->click();}
        });timer.start();window.findChild<QPushButton *>("editTableSchema")->click();QTRY_COMPARE_WITH_TIMEOUT(opened.count(),3,10000);timer.stop();QCOMPARE(phase,3);
        QTRY_COMPARE_WITH_TIMEOUT(schema->item(0,0)->text(),QString("Code"),5000);QCOMPARE(schema->item(0,2)->text(),QString("Yes"));QCOMPARE(schema->item(1,1)->text(),QString("integer"));
        QCOMPARE(objects->currentItem()->data(0,Qt::UserRole),id);QCOMPARE(objects->currentItem()->text(0),QString("Renamed"));
    }
    void tableSelectionReviewCreatesIndependentValuesThroughRest() {
        QTemporaryDir temp;StartupOptions options;options.automaticWorkbook=false;MainWindow window(options);window.show();
        QSignalSpy ready(&window,&MainWindow::serviceReady);QTRY_COMPARE_WITH_TIMEOUT(ready.count(),1,10000);
        QSignalSpy opened(&window,&MainWindow::workbookReady);window.openBook(temp.filePath("promote.dhwb"),true);QTRY_COMPARE_WITH_TIMEOUT(opened.count(),1,10000);
        window.submit({{"action","edit"},{"cells",QJsonArray{QJsonObject{{"row",1},{"col",1},{"source","ID"}},QJsonObject{{"row",1},{"col",2},{"source","Amount"}},
            QJsonObject{{"row",2},{"col",1},{"source","00123"},{"kind","text"}},QJsonObject{{"row",2},{"col",2},{"source","=1+2"},{"kind","formula"}},
            QJsonObject{{"row",3},{"col",1},{"source","=literal"},{"kind","text"}},QJsonObject{{"row",3},{"col",2},{"source","2.5"}}}}});
        QTRY_COMPARE_WITH_TIMEOUT(opened.count(),2,10000);
        auto *address=window.findChild<QLineEdit *>("address");address->setText("A1:B3");address->returnPressed();
        int phase=0;QTimer timer;timer.setInterval(10);connect(&timer,&QTimer::timeout,&window,[&]{
            auto *dialog=qobject_cast<QDialog *>(QApplication::activeModalWidget());if(!dialog)return;
            if(phase==0 && dialog->objectName()=="tableSelectionOptions"){phase=1;dialog->findChild<QLineEdit *>("selectionTableName")->setText("Items");dialog->findChild<QDialogButtonBox *>()->button(QDialogButtonBox::Ok)->click();}
            else if(phase==1 && dialog->objectName()=="tableDefinitionDialog"){phase=2;dialog->findChild<QDialogButtonBox *>()->button(QDialogButtonBox::Ok)->click();}
            else if(phase==2 && dialog->objectName()=="tablePromotionReview"){phase=3;dialog->findChild<QDialogButtonBox *>()->button(QDialogButtonBox::Apply)->click();}
        });timer.start();window.findChild<QAction *>("tableFromSelectionAction")->trigger();
        QTRY_COMPARE_WITH_TIMEOUT(opened.count(),3,10000);timer.stop();QCOMPARE(phase,3);
        auto *records=window.findChild<QTableWidget *>("tableRecords");QTRY_COMPARE_WITH_TIMEOUT(records->rowCount(),2,5000);
        QCOMPARE(records->item(0,0)->text(),QString("00123"));QCOMPARE(records->item(0,1)->text(),QString("3"));QCOMPARE(records->item(1,0)->text(),QString("=literal"));
        QTest::mouseClick(window.findChild<QPushButton *>("returnToSheets"),Qt::LeftButton);
        QTRY_VERIFY_WITH_TIMEOUT(window.model()->loaded(window.model()->index(1,1)),5000);QCOMPARE(window.model()->cell(window.model()->index(1,1))["source"].toString(),QString("=1+2"));
    }
    void relationalTablesUseRestAndKeepSheetActionsSeparate() {
        QTemporaryDir temp;StartupOptions options;options.automaticWorkbook=false;MainWindow window(options);window.show();
        QSignalSpy ready(&window,&MainWindow::serviceReady);QTRY_COMPARE_WITH_TIMEOUT(ready.count(),1,10000);
        QSignalSpy opened(&window,&MainWindow::workbookReady);window.openBook(temp.filePath("tables.dhwb"),true);QTRY_COMPARE_WITH_TIMEOUT(opened.count(),1,10000);
        window.submit({{"action","table_create"},{"name","Customers"},{"columns",QJsonArray{QJsonObject{{"name","CustomerID"},{"required",true},{"unique",true}},QJsonObject{{"name","Name"}}}}});
        QTRY_COMPARE_WITH_TIMEOUT(opened.count(),2,10000);
        auto *action=window.findChild<QAction *>("relationalWorkbenchAction");QVERIFY(action);action->trigger();
        auto *objects=window.findChild<QTreeWidget *>("relationalObjects");QVERIFY(objects);
        QTRY_VERIFY_WITH_TIMEOUT(objects->topLevelItemCount()>=1 && objects->topLevelItem(0)->childCount()==1,5000);
        objects->setCurrentItem(objects->topLevelItem(0)->child(0));
        auto *schema=window.findChild<QTableWidget *>("tableSchema");QVERIFY(schema);QCOMPARE(schema->rowCount(),2);
        QCOMPARE(schema->item(0,0)->text(),QString("CustomerID"));
        QVERIFY(!window.findChild<QAction *>("importAction")->isEnabled());
        const auto state=window.model()->state();window.submit({{"action","edit"},{"cells",QJsonArray{QJsonObject{{"row",1},{"col",1},{"source","hidden write"}}}}});
        QCOMPARE(window.model()->state(),state);
        QTest::mouseClick(window.findChild<QPushButton *>("returnToSheets"),Qt::LeftButton);
        QVERIFY(window.findChild<QAction *>("importAction")->isEnabled());
    }
    void keyboardGridAccessibilityNeverEnumeratesConceptualCells() {
        RestClient client;SheetModel model(&client);KeyboardGridView view;view.setModel(&model);view.setAccessibleName("Spreadsheet");
        auto *surface=QAccessible::queryAccessibleInterface(&view);QVERIFY(surface);QCOMPARE(surface->role(),QAccessible::Pane);
        QCOMPARE(surface->childCount(),1);QVERIFY(!surface->tableInterface());QVERIFY(!surface->child(1));
        QVERIFY(qint64(model.rowCount())*model.columnCount()>std::numeric_limits<int>::max());
        QCOMPARE(surface->child(0)->object(),view.viewport());QCOMPARE(surface->indexOfChild(surface->child(0)),0);
    }
    void accessibleCellDescriptionsDistinguishPendingAndProjectedResults() {
        ViewportCalculationServer server;RestClient client;client.configure(server.url(),"test");SheetModel model(&client,nullptr,128,32);
        model.bind("book","sheet","state",true);model.setProjection({9,19});const auto cell=model.index(0,0);
        QCOMPARE(model.data(cell,Qt::AccessibleTextRole).toString(),QString("A10: Loading cell"));
        QTRY_COMPARE_WITH_TIMEOUT(server.jobs.size(),1,3000);
        QCOMPARE(model.data(cell,Qt::AccessibleTextRole).toString(),QString("A10: Calculating stored formula; input retained"));
        server.complete(0,2);QTRY_COMPARE_WITH_TIMEOUT(model.data(cell,Qt::AccessibleTextRole).toString(),QString("A10: 2"),3000);
        QCOMPARE(model.data(cell,Qt::AccessibleDescriptionRole),model.data(cell,Qt::AccessibleTextRole));
    }
    void accessibleCellFailureAndReadDialogRetainInputs() {
        ViewportCalculationServer server;RestClient client;client.configure(server.url(),"test");SheetModel model(&client,nullptr,128,32);
        model.bind("book","sheet","state",false);model.data(model.index(0,0));QTRY_COMPARE_WITH_TIMEOUT(server.jobs.size(),1,3000);server.jobs[0].status="failed";
        QTRY_VERIFY_WITH_TIMEOUT(model.data(model.index(0,0),Qt::AccessibleTextRole).toString().contains("Unavailable"),3000);
        QVERIFY(model.data(model.index(0,0),Qt::AccessibleTextRole).toString().contains("Input retained"));
        QTemporaryDir temp;StartupOptions options;options.automaticWorkbook=false;MainWindow window(options);window.show();QSignalSpy ready(&window,&MainWindow::serviceReady);QTRY_COMPARE_WITH_TIMEOUT(ready.count(),1,10000);
        QSignalSpy opened(&window,&MainWindow::workbookReady);window.openBook(temp.filePath("accessible.dhwb"),true);QTRY_COMPARE_WITH_TIMEOUT(opened.count(),1,10000);
        window.submit({{"action","edit"},{"cells",QJsonArray{QJsonObject{{"row",1},{"col",1},{"source","=1+1"}}}}});QTRY_COMPARE_WITH_TIMEOUT(opened.count(),2,10000);
        QTRY_COMPARE_WITH_TIMEOUT(window.model()->data(window.model()->index(0,0)).toString(),QString("2"),5000);const auto state=window.model()->state();
        bool seen=false;QTimer::singleShot(0,&window,[&]{auto *dialog=qobject_cast<QMessageBox *>(QApplication::activeModalWidget());if(dialog){seen=true;QCOMPARE(dialog->text(),QString("A1: 2"));QCOMPARE(dialog->textFormat(),Qt::PlainText);dialog->accept();}});
        window.findChild<QAction *>("readCurrentCellAction")->trigger();QVERIFY(seen);QCOMPARE(window.model()->state(),state);QCOMPARE(window.model()->cell(window.model()->index(0,0))["source"].toString(),QString("=1+1"));
    }
    void exportDestinationPermissionFailureRetainsNativeData() {
#ifndef Q_OS_UNIX
        QTest::qSkip("This case requires POSIX directory permissions; native platform qualification is separate.",__FILE__,__LINE__);return;
#else
        QTemporaryDir temp;StartupOptions options;options.automaticWorkbook=false;options.handler=QString(DATAHORIZON_FIXTURES)+"/execution_hook.py";
        MainWindow window(options);window.show();QSignalSpy ready(&window,&MainWindow::serviceReady);QTRY_COMPARE_WITH_TIMEOUT(ready.count(),1,10000);
        const auto native=temp.filePath("native.dhwb");QSignalSpy opened(&window,&MainWindow::workbookReady);window.openBook(native,true);QTRY_COMPARE_WITH_TIMEOUT(opened.count(),1,10000);
        window.submit({{"action","edit"},{"cells",QJsonArray{QJsonObject{{"row",1},{"col",1},{"source","7"}}}}});QTRY_COMPARE_WITH_TIMEOUT(opened.count(),2,10000);
        const auto state=window.model()->state();QFile source(native);QVERIFY(source.open(QIODevice::ReadOnly));const auto original=source.readAll();source.close();
        const auto denied=temp.filePath("denied");QVERIFY(QDir().mkdir(denied));const auto permissions=QFileInfo(denied).permissions();
        const auto restore=qScopeGuard([&]{QFile::setPermissions(denied,permissions);});QVERIFY(QFile::setPermissions(denied,QFile::ReadOwner|QFile::ExeOwner));
        QFile permissionCheck(denied+"/probe");if(permissionCheck.open(QIODevice::WriteOnly)){permissionCheck.close();QFile::remove(permissionCheck.fileName());QTest::qSkip("Host does not enforce unprivileged directory permission denial.",__FILE__,__LINE__);return;}
        QString error;const QJsonObject snapshot{{"file_base64",QString::fromLatin1(QByteArray("bounded export fixture").toBase64())}};
        QVERIFY(!ExportWriter::save(denied+"/export.xlsx",native,snapshot,error));QVERIFY(!error.isEmpty());QVERIFY(!QFile::exists(denied+"/export.xlsx"));
        QVERIFY(source.open(QIODevice::ReadOnly));QCOMPARE(source.readAll(),original);QCOMPARE(window.model()->state(),state);
        QTRY_COMPARE_WITH_TIMEOUT(window.model()->data(window.model()->index(0,0)).toString(),QString("7"),5000);
        QTRY_COMPARE_WITH_TIMEOUT(window.model()->data(window.model()->index(0,1)).toString(),QString("14"),5000);
#endif
    }
    void backgroundDisplayChangesPreserveInCellDraftAndCaret_data() {
        QTest::addColumn<int>("pane");QTest::newRow("main")<<0;QTest::newRow("frozen-row")<<1;QTest::newRow("frozen-column")<<2;QTest::newRow("corner")<<3;
    }
    void backgroundDisplayChangesPreserveInCellDraftAndCaret() {
        QFETCH(int,pane);QTemporaryDir temp;StartupOptions options;options.automaticWorkbook=false;options.viewStatePath=temp.filePath("views.ini");
        MainWindow window(options);window.show();QSignalSpy ready(&window,&MainWindow::serviceReady);QTRY_COMPARE_WITH_TIMEOUT(ready.count(),1,10000);
        QSignalSpy opened(&window,&MainWindow::workbookReady);window.openBook(temp.filePath("draft.dhwb"),true);QTRY_COMPARE_WITH_TIMEOUT(opened.count(),1,10000);
        window.submit({{"action","edit"},{"cells",QJsonArray{QJsonObject{{"row",1},{"col",1},{"source","=1+1"}}}}});QTRY_COMPARE_WITH_TIMEOUT(opened.count(),2,10000);
        auto *model=window.model();auto *grid=window.findChild<QTableView *>("grid");QTRY_COMPARE_WITH_TIMEOUT(model->data(model->index(0,0)).toString(),QString("2"),5000);
        QTableView *view=grid;
        if(pane==1){window.findChild<QAction *>("freezeTopRowAction")->trigger();view=window.findChild<QTableView *>("frozenTopRow");}
        if(pane==2){window.findChild<QAction *>("freezeFirstColumnAction")->trigger();view=window.findChild<QTableView *>("frozenFirstColumn");}
        if(pane==3){grid->setCurrentIndex(model->index(1,1));window.findChild<QAction *>("freezePanesAction")->trigger();view=window.findChild<QTableView *>("frozenCorner");}
        QVERIFY(view && view->isVisible());view->setCurrentIndex(view->model()->index(0,0));view->edit(view->currentIndex());
        QPointer<QLineEdit> editor=view->findChild<QLineEdit *>();QVERIFY(editor);QCOMPARE(editor->text(),QString("=1+1"));
        editor->setText("=SUM(7,8)");editor->setModified(true);editor->setSelection(2,3);const auto state=model->state();QSignalSpy writes(&window,&MainWindow::operationFinished);
        // This is the same notification emitted by a derived tile publication.
        // Qt 6.8 updates active editors even when the supplied roles omit EditRole.
        emit model->dataChanged(model->index(0,0),model->index(31,15),{Qt::DisplayRole});QTest::qWait(20);
        QVERIFY(editor);QCOMPARE(editor->text(),QString("=SUM(7,8)"));QCOMPARE(editor->selectionStart(),2);QCOMPARE(editor->selectedText(),QString("UM("));
        QCOMPARE(model->state(),state);QCOMPARE(model->cell(model->index(0,0))["source"].toString(),QString("=1+1"));QVERIFY(writes.isEmpty());
        QTest::keyClick(editor,Qt::Key_Escape);
    }
    void viewportCalculationsPublishCurrentResultsWithoutChangingSources() {
        ViewportCalculationServer server;RestClient client;client.configure(server.url(),"test");SheetModel model(&client,nullptr,128,32);
        model.bind("book","sheet","state",false);const auto cell=model.index(0,0);model.data(cell);
        QTRY_COMPARE_WITH_TIMEOUT(server.jobs.size(),1,3000);QCOMPARE(model.data(cell).toString(),QString("…"));
        QVERIFY(model.flags(cell).testFlag(Qt::ItemIsEditable));QCOMPARE(server.ranges.first().toObject()["calculation"].toString(),QString("cached"));
        server.complete(0,2);QTRY_COMPARE_WITH_TIMEOUT(model.data(cell).toString(),QString("2"),3000);
        QCOMPARE(model.cell(cell)["source"].toString(),QString("=1+1"));QTRY_COMPARE_WITH_TIMEOUT(server.releases,1,3000);
    }
    void viewportCalculationsDiscardCompletionAfterRebind() {
        ViewportCalculationServer server;server.completeOnCancel=true;RestClient client;client.configure(server.url(),"test");SheetModel model(&client,nullptr,128,32);
        model.bind("book","sheet","state",false);model.data(model.index(0,0));QTRY_COMPARE_WITH_TIMEOUT(server.jobs.size(),1,3000);
        model.bind("book","sheet","state",false);model.data(model.index(0,0));QTRY_COMPARE_WITH_TIMEOUT(server.jobs.size(),2,3000);
        QCOMPARE(server.cancellations,1);QCOMPARE(model.data(model.index(0,0)).toString(),QString("…"));
        server.complete(1,8);QTRY_COMPARE_WITH_TIMEOUT(model.data(model.index(0,0)).toString(),QString("8"),3000);
    }
    void viewportCancellationWaitsForTheCapturedAdmission() {
        ViewportCalculationServer server;server.holdAdmissions=true;RestClient client;client.configure(server.url(),"test");SheetModel model(&client,nullptr,128,32);
        model.bind("book","sheet","state",false);model.data(model.index(0,0));QTRY_COMPARE_WITH_TIMEOUT(server.jobs.size(),1,3000);
        model.bind("book","sheet","state",false);model.data(model.index(0,0));QTest::qWait(50);QCOMPARE(server.cancellations,0);
        server.acknowledge(0);QTRY_COMPARE_WITH_TIMEOUT(server.jobs.size(),2,3000);QCOMPARE(server.cancellations,1);
        server.complete(1,8);server.acknowledge(1);QTRY_COMPARE_WITH_TIMEOUT(model.data(model.index(0,0)).toString(),QString("8"),3000);
    }
    void staleViewportSnapshotRequestsAnExternalRefresh() {
        ViewportCalculationServer server;RestClient client;client.configure(server.url(),"test");SheetModel model(&client,nullptr,128,32);QSignalSpy refresh(&model,&SheetModel::externalChange);
        model.bind("book","sheet","state",false);model.data(model.index(0,0));QTRY_COMPARE_WITH_TIMEOUT(server.jobs.size(),1,3000);server.jobs[0].status="stale";
        QTRY_COMPARE_WITH_TIMEOUT(refresh.count(),1,3000);QVERIFY(model.data(model.index(0,0)).toString()!=QString("99"));
    }
    void viewportCalculationReconcilesLostStartReplyWithoutCreatingAgain() {
        ViewportCalculationServer server;server.loseAdmissionReply=true;RestClient client;client.configure(server.url(),"test");SheetModel model(&client,nullptr,128,32);
        model.bind("book","sheet","state",false);model.data(model.index(0,0));QTRY_COMPARE_WITH_TIMEOUT(server.jobs.size(),1,3000);server.complete(0,2);
        QTRY_COMPARE_WITH_TIMEOUT(model.data(model.index(0,0)).toString(),QString("2"),3000);QCOMPARE(server.jobs.size(),1);QTRY_COMPARE_WITH_TIMEOUT(server.releases,1,3000);
    }
    void viewportCalculationsCancelTilesThatLeaveVisiblePanes() {
        ViewportCalculationServer server;server.completeOnCancel=true;RestClient client;client.configure(server.url(),"test");SheetModel model(&client,nullptr,128,32);
        model.bind("book","sheet","state",false);model.setVisibleRanges({QRect(0,0,1,1)});model.data(model.index(0,0));QTRY_COMPARE_WITH_TIMEOUT(server.jobs.size(),1,3000);
        model.setVisibleRanges({QRect(0,64,1,1)});model.data(model.index(64,0));QTRY_COMPARE_WITH_TIMEOUT(server.jobs.size(),2,3000);
        QCOMPARE(server.cancellations,1);QVERIFY(model.data(model.index(0,0)).toString()!=QString("99"));
        server.complete(1,8);QTRY_COMPARE_WITH_TIMEOUT(model.data(model.index(64,0)).toString(),QString("8"),3000);
    }
    void viewportCalculationsKeepProjectedSourceCoordinates() {
        ViewportCalculationServer server;RestClient client;client.configure(server.url(),"test");SheetModel model(&client,nullptr,128,32);
        model.bind("book","sheet","state",true);model.setProjection({9,19});model.data(model.index(0,0));QTRY_COMPARE_WITH_TIMEOUT(server.jobs.size(),1,3000);
        QCOMPARE(server.jobs[0].request["rows"].toArray(),QJsonArray({10,20}));server.complete(0,2);
        QTRY_COMPARE_WITH_TIMEOUT(model.data(model.index(0,0)).toString(),QString("2"),3000);QCOMPARE(model.sourceRow(0),9);
    }
    void viewportCalculationFailureIsVisibleWithoutFreshReadFallback() {
        ViewportCalculationServer server;RestClient client;client.configure(server.url(),"test");SheetModel model(&client,nullptr,128,32);QSignalSpy issues(&model,&SheetModel::issue);
        model.bind("book","sheet","state",false);model.data(model.index(0,0));QTRY_COMPARE_WITH_TIMEOUT(server.jobs.size(),1,3000);server.jobs[0].status="failed";
        QTRY_COMPARE_WITH_TIMEOUT(model.data(model.index(0,0)).toString(),QString("Unavailable"),3000);QCOMPARE(issues.count(),1);QCOMPARE(server.ranges.size(),1);
        QCOMPARE(model.cell(model.index(0,0))["source"].toString(),QString("=1+1"));
    }
    void viewportCalculationRejectsWrongStateAndServiceReplies() {
        ViewportCalculationServer oldServer,newServer;RestClient client;client.configure(oldServer.url(),"test");SheetModel model(&client,nullptr,128,32);
        model.bind("book","sheet","state",false);model.data(model.index(0,0));QTRY_COMPARE_WITH_TIMEOUT(oldServer.jobs.size(),1,3000);
        client.configure(newServer.url(),"new");model.bind("book","sheet","state",false);model.data(model.index(0,0));QTRY_COMPARE_WITH_TIMEOUT(newServer.jobs.size(),1,3000);
        oldServer.complete(0,99);newServer.jobs[0].wrongState=true;newServer.complete(0,2);
        QTRY_COMPARE_WITH_TIMEOUT(model.data(model.index(0,0)).toString(),QString("Unavailable"),3000);QTRY_COMPARE_WITH_TIMEOUT(newServer.releases,1,3000);
    }
    void viewportCalculationQueueIsBoundedAndPrunedBeforeAdmission() {
        ViewportCalculationServer server;RestClient client;client.configure(server.url(),"test");int publications=0;
        ViewportCalculations jobs(&client,nullptr,[](const ViewportCalculations::Work &){return true;},[&](const ViewportCalculations::Work &work,QJsonObject,QString){QCOMPARE(work.key,quint64(63));++publications;});
        auto work=[](int key){return ViewportCalculations::Work{quint64(key),1,"book",{{"sheet","sheet"},{"expected_state","state"},{"r1",key*32+1},{"r2",key*32+32},{"c1",1},{"c2",16}}};};
        jobs.enqueue(work(0));QTRY_COMPARE_WITH_TIMEOUT(server.jobs.size(),1,3000);for(int key=1;key<64;++key)jobs.enqueue(work(key));
        QCOMPARE(jobs.queued(),16);jobs.retain({63});QTRY_COMPARE_WITH_TIMEOUT(server.jobs.size(),2,3000);QCOMPARE(server.cancellations,1);
        server.complete(1,2);QTRY_COMPARE_WITH_TIMEOUT(publications,1,3000);QCOMPARE(server.jobs.size(),2);
    }
    void initTestCase(){
        m_originalDialogAttribute=QCoreApplication::testAttribute(Qt::AA_DontUseNativeDialogs);
        // QTest timers cannot reliably drive AppKit's separate NSAlert modal loop.
        // Native OS dialogs remain a manual gate; these tests exercise Qt widgets.
        QCoreApplication::setAttribute(Qt::AA_DontUseNativeDialogs);
    }
    void cleanupTestCase(){QCoreApplication::setAttribute(Qt::AA_DontUseNativeDialogs,m_originalDialogAttribute);}
    void clipboardParserPreservesFieldsAndRejectsMalformedData(){
        QMimeData mime;QJsonObject payload;QString error;
        mime.setText("10\r\n\r\n20\t\r\n");QVERIFY(ClipboardData::decode(&mime,payload,error));const auto rows=payload["rows"].toArray();QCOMPARE(rows.size(),3);QCOMPARE(rows[1].toArray()[0].toObject()["source"].toString(),QString());QCOMPARE(rows[2].toArray().size(),2);
        mime.setText(QString(10001,'\n'));QVERIFY(!ClipboardData::decode(&mime,payload,error));QVERIFY(error.contains("10,000"));
        mime.setText(QString(100,'x'));QVERIFY(!ClipboardData::decode(&mime,payload,error,99));
        mime.setData("application/x-datahorizon-cells","invalid");QVERIFY(!ClipboardData::decode(&mime,payload,error));
        mime.setData("application/x-datahorizon-cells","{\"rows\":[[3]]}");QVERIFY(!ClipboardData::decode(&mime,payload,error));
    }
    void recordPasteNormalizesSpreadsheetLineEndings_data(){
        QTest::addColumn<QString>("text");QTest::addColumn<bool>("records");
        for(bool records:{false,true})for(const auto &ending:{QString("\n"),QString("\r\n")})for(bool terminal:{false,true}){
            const auto name=QString("%1-%2-%3").arg(records?"records":"grid",ending=="\n"?"lf":"crlf",terminal?"terminal":"no-terminal");
            QTest::newRow(qPrintable(name))<<QString("10"+ending+"20"+(terminal?ending:QString()))<<records;
        }
    }
    void recordPasteNormalizesSpreadsheetLineEndings(){
        QFETCH(QString,text);QFETCH(bool,records);QTemporaryDir temp;StartupOptions options;options.automaticWorkbook=false;options.viewStatePath=temp.filePath("views.ini");
        MainWindow window(options);window.show();QSignalSpy ready(&window,&MainWindow::serviceReady);QTRY_COMPARE_WITH_TIMEOUT(ready.count(),1,10000);QSignalSpy opened(&window,&MainWindow::workbookReady);window.openBook(temp.filePath("paste.dhwb"),true);QTRY_COMPARE_WITH_TIMEOUT(opened.count(),1,10000);
        window.submit({{"action","list_declare"},{"id","Records"},{"spec",QJsonObject{{"r2",2},{"c2",1}}}});QTRY_COMPARE_WITH_TIMEOUT(opened.count(),2,10000);
        auto *grid=window.findChild<QTableView *>("grid");grid->setCurrentIndex(window.model()->index(0,0));QTRY_VERIFY_WITH_TIMEOUT(window.model()->loaded(grid->currentIndex()),5000);
        auto saved=std::make_unique<QMimeData>();auto *clipboard=QApplication::clipboard();
        if(const auto *mime=clipboard->mimeData()){for(const auto &format:mime->formats())saved->setData(format,mime->data(format));if(mime->hasImage())saved->setImageData(mime->imageData());}
        const auto marker=QUuid::createUuid().toByteArray();const auto restore=qScopeGuard([&]{if(clipboard->mimeData()->data("application/x-datahorizon-test-marker")==marker)clipboard->setMimeData(saved.release());});
        auto *input=new QMimeData;input->setText(text);input->setData("application/x-datahorizon-test-marker",marker);clipboard->setMimeData(input);
        QSignalSpy edits(&window,&MainWindow::operationFinished);QTimer timer;timer.setInterval(10);connect(&timer,&QTimer::timeout,&window,[&]{
            if(auto *dialog=window.findChild<QDialog *>("recordCommandDialog");dialog && !dialog->property("checked").toBool()){dialog->setProperty("checked",true);auto *mode=dialog->findChild<QComboBox *>("recordCommandMode");mode->setCurrentIndex(mode->findData("all"));dialog->accept();}
            if(auto *box=qobject_cast<QMessageBox *>(QApplication::activeModalWidget()))if(auto *yes=box->button(QMessageBox::Yes))yes->click();
        });timer.start();
        if(records)window.findChild<QAction *>("recordCommandAction")->trigger();
        else{grid->setFocus();QAction *paste=nullptr;for(auto *action:window.findChildren<QAction *>())if(action->shortcut().matches(QKeySequence::Paste)==QKeySequence::ExactMatch)paste=action;QVERIFY(paste);paste->trigger();}
        QTRY_COMPARE_WITH_TIMEOUT(edits.count(),1,5000);timer.stop();QVERIFY(edits.first().first().toBool());QTRY_COMPARE_WITH_TIMEOUT(opened.count(),3,5000);
        auto *client=window.findChild<RestClient *>();const auto base="/v1/workbooks/"+window.model()->workbook();const auto cells=syncRequest(client,base+"/range",{{"r2",2}})["cells"].toArray();QCOMPARE(cells.size(),2);QCOMPARE(cells[0].toObject()["value"].toInt(),10);QCOMPARE(cells[1].toObject()["value"].toInt(),20);QCOMPARE(cells[0].toObject()["input_kind"].toString(),QString("auto"));
        window.submit({{"action","undo"}});QTRY_COMPARE_WITH_TIMEOUT(edits.count(),2,5000);QTRY_COMPARE_WITH_TIMEOUT(opened.count(),4,5000);QVERIFY(syncRequest(client,base+"/range",{{"r2",2}})["cells"].toArray().isEmpty());
    }
    void ownedJobCloseRetainsCommittedBatchesAndReleasesHandle(){
        QTemporaryDir temp;const auto handler=temp.filePath("handler.py");QFile code(handler);QVERIFY(code.open(QIODevice::WriteOnly));code.write("import time\ndef on_change(context, changes):\n    time.sleep(0.15)\n");code.close();
        StartupOptions options;options.automaticWorkbook=false;options.handler=handler;options.recoveryDirectory=temp.filePath("recovery");options.viewStatePath=temp.filePath("views.ini");
        MainWindow window(options);window.show();QSignalSpy ready(&window,&MainWindow::serviceReady);QTRY_COMPARE_WITH_TIMEOUT(ready.count(),1,10000);const auto path=temp.filePath("owned.dhwb");QSignalSpy opened(&window,&MainWindow::workbookReady);window.openBook(path,true);QTRY_COMPARE_WITH_TIMEOUT(opened.count(),1,10000);
        auto *client=window.findChild<RestClient *>();const auto base="/v1/workbooks/"+window.model()->workbook();
        auto job=syncRequest(client,base+"/operation-jobs",{{"job_id","retained"},{"expected_state",window.model()->state()},{"mode","batches"},{"batch_size",1}});QVERIFY(!job.contains("_error"));
        QJsonArray cells;for(int row=1;row<=10;++row)cells.append(QJsonObject{{"row",row},{"col",1},{"source","owned"}});
        QVERIFY(!syncRequest(client,base+"/operation-jobs/retained/append",{{"cells",cells}}).contains("_error"));QCOMPARE(syncRequest(client,base+"/operation-jobs/retained/seal",{})["status"].toString(),QString("prepared"));
        int phase=0;QTimer timer;timer.setInterval(10);connect(&timer,&QTimer::timeout,&window,[&]{
            if(auto *box=qobject_cast<QMessageBox *>(QApplication::activeModalWidget())){if(auto *yes=box->button(QMessageBox::Yes))yes->click();return;}
            auto *dialog=window.findChild<QDialog *>("operationJobsDialog");if(!dialog)return;const auto value=QJsonDocument::fromJson(dialog->findChild<QPlainTextEdit *>("operationJobReport")->toPlainText().toUtf8()).object();
            if(phase==0){phase=1;dialog->findChild<QLineEdit *>("operationJobId")->setText("retained");dialog->findChild<QPushButton *>("inspectOperationJob")->click();}
            else if(phase==1 && value["status"]=="prepared"){phase=2;QTimer::singleShot(0,dialog->findChild<QPushButton *>("startOperationJob"),&QPushButton::click);}
            else if(phase==2 && value["status"]=="running" && value["committed_batches"].toInt()>0){phase=3;dialog->accept();}
        });timer.start();window.findChild<QAction *>("operationJobsAction")->trigger();QCOMPARE(phase,3);QVERIFY(!window.close());QTRY_VERIFY_WITH_TIMEOUT(!window.isVisible(),5000);timer.stop();
        QVERIFY(syncRequest(client,base,{},false).contains("_error"));
        const auto reopened=syncRequest(client,"/v1/open",{{"path",path}});QVERIFY(!reopened.contains("_error"));const auto fresh="/v1/workbooks/"+reopened["id"].toString();
        job=syncRequest(client,fresh+"/operation-jobs/retained/status",{});QCOMPARE(job["status"].toString(),QString("cancelled"));QVERIFY(job["next"].toInt()>0);QVERIFY(job["next"].toInt()<10);
        const auto stored=syncRequest(client,fresh+"/range",{{"r2",10}})["cells"].toArray();QCOMPARE(stored.size(),job["next"].toInt());
        syncRequest(client,fresh+"/close",{});
    }
    void closingActiveJobCancelsThenReleases_data(){
        QTest::addColumn<QString>("failure");QTest::addColumn<bool>("committed");
        QTest::newRow("cancelled")<<QString()<<false;QTest::newRow("last-commit")<<QString()<<true;
        QTest::newRow("lost-cancel")<<QString("cancel")<<false;QTest::newRow("lost-status")<<QString("status")<<false;QTest::newRow("lost-close")<<QString("close")<<false;
    }
    void closingActiveJobCancelsThenReleases(){
        QFETCH(QString,failure);QFETCH(bool,committed);QTemporaryDir temp;JobLifecycleServer server(temp.filePath("native.dhwb"));server.failure=failure;server.committed=committed;QVERIFY(server.listen(QHostAddress::LocalHost,0));
        StartupOptions options;options.automaticWorkbook=false;options.url="http://127.0.0.1:"+QString::number(server.serverPort());options.token="token";options.recoveryDirectory=temp.filePath("recovery");options.viewStatePath=temp.filePath("views.ini");
        MainWindow window(options);window.show();QSignalSpy ready(&window,&MainWindow::serviceReady);QTRY_COMPARE_WITH_TIMEOUT(ready.count(),1,5000);QSignalSpy opened(&window,&MainWindow::workbookReady);window.openBook(temp.filePath("native.dhwb"));QTRY_COMPARE_WITH_TIMEOUT(opened.count(),1,5000);
        int phase=0;QTimer timer;timer.setInterval(10);connect(&timer,&QTimer::timeout,&window,[&]{
            if(auto *box=qobject_cast<QMessageBox *>(QApplication::activeModalWidget())){if(auto *yes=box->button(QMessageBox::Yes))yes->click();return;}
            auto *dialog=window.findChild<QDialog *>("operationJobsDialog");if(!dialog)return;const auto value=QJsonDocument::fromJson(dialog->findChild<QPlainTextEdit *>("operationJobReport")->toPlainText().toUtf8()).object();
            if(phase==0){phase=1;dialog->findChild<QLineEdit *>("operationJobId")->setText("retained");dialog->findChild<QPushButton *>("inspectOperationJob")->click();}
            else if(phase==1 && value["status"]=="prepared"){phase=2;QTimer::singleShot(0,dialog->findChild<QPushButton *>("startOperationJob"),&QPushButton::click);}
            else if(phase==2 && value["status"]=="running"){phase=3;dialog->accept();}
        });timer.start();window.findChild<QAction *>("operationJobsAction")->trigger();QCOMPARE(phase,3);
        QVERIFY(!window.close());QVERIFY(!window.close());QTRY_VERIFY_WITH_TIMEOUT(!window.isVisible(),5000);timer.stop();
        QCOMPARE(server.cancellations,1);QCOMPARE(server.releases,failure=="cancel" || failure=="status"?0:1);
        if(failure.isEmpty()){
            QVERIFY(server.polls>=1);QVERIFY(server.requests.indexOf("/v1/workbooks/handle/operation-jobs/retained/cancel")<server.requests.indexOf("/v1/workbooks/handle/close"));
            QCOMPARE(QDir(options.recoveryDirectory).entryList({"job-*.json"},QDir::Files).size(),0);QCOMPARE(server.job["status"].toString(),committed?QString("completed"):QString("cancelled"));
        }else{
            QDir recovery(options.recoveryDirectory);const auto files=recovery.entryList({"job-*.json"},QDir::Files);QCOMPARE(files.size(),1);QFile file(recovery.filePath(files.first()));QVERIFY(file.open(QIODevice::ReadOnly));const auto saved=QJsonDocument::fromJson(file.readAll()).object();QCOMPARE(saved["job_id"].toString(),QString("retained"));QCOMPARE(saved["outcome"].toString(),QString("unknown"));QVERIFY(saved["status"].toObject().contains("execution_id"));
        }
        // The independent service remains usable after this window closes.
        RestClient other;other.configure(options.url,options.token);QCOMPARE(syncRequest(&other,"/v1/capabilities",{},false)["api_version"].toInt(),1);
    }
    void preparedJobsRequireExplicitResumeReview_data() {
        QTest::addColumn<bool>("reopen");QTest::addColumn<bool>("accept");QTest::addColumn<bool>("race");
        QTest::newRow("reopen")<<true<<true<<false;
        QTest::newRow("intervening-edit")<<false<<true<<false;
        QTest::newRow("decline")<<false<<false<<false;
        QTest::newRow("changed-during-review")<<false<<true<<true;
    }
    void preparedJobsRequireExplicitResumeReview() {
        QFETCH(bool,reopen);QFETCH(bool,accept);QFETCH(bool,race);
        QTemporaryDir temp;StartupOptions options;options.automaticWorkbook=false;options.viewStatePath=temp.filePath("views.ini");
        MainWindow window(options);window.show();QSignalSpy ready(&window,&MainWindow::serviceReady);QTRY_COMPARE_WITH_TIMEOUT(ready.count(),1,10000);
        QSignalSpy opened(&window,&MainWindow::workbookReady);const auto path=temp.filePath("resume.dhwb");window.openBook(path,true);QTRY_COMPARE_WITH_TIMEOUT(opened.count(),1,10000);
        auto *client=window.findChild<RestClient *>();QVERIFY(client);
        auto base="/v1/workbooks/"+window.model()->workbook();const auto old=window.model()->state();
        auto job=syncRequest(client,base+"/operation-jobs",{{"job_id","retained"},{"expected_state",old}});QVERIFY(!job.contains("_error"));
        QVERIFY(!syncRequest(client,base+"/operation-jobs/retained/append",{{"cells",QJsonArray{QJsonObject{{"row",1},{"col",1},{"source","reviewed"}}}}}).contains("_error"));
        job=syncRequest(client,base+"/operation-jobs/retained/seal",{});QCOMPARE(job["status"].toString(),QString("prepared"));
        if(reopen){QVERIFY(!syncRequest(client,base+"/close",{}).contains("_error"));window.openBook(path);QTRY_COMPARE_WITH_TIMEOUT(opened.count(),2,10000);base="/v1/workbooks/"+window.model()->workbook();}
        else{window.submit({{"action","edit"},{"cells",QJsonArray{QJsonObject{{"row",1},{"col",2},{"source","intervening"}}}}});QTRY_COMPARE_WITH_TIMEOUT(opened.count(),2,10000);}
        QVERIFY(window.model()->state()!=old);int phase=0;bool explicitReview=false;bool completed=false;QTimer timer;timer.setInterval(10);
        connect(&timer,&QTimer::timeout,&window,[&]{
            auto *dialog=window.findChild<QDialog *>("operationJobsDialog");if(!dialog)return;
            const auto report=dialog->findChild<QPlainTextEdit *>("operationJobReport")->toPlainText();const auto status=QJsonDocument::fromJson(report.toUtf8()).object()["status"].toString();
            if(auto *box=qobject_cast<QMessageBox *>(QApplication::activeModalWidget());box && box->objectName()=="operationJobExecutionReview" && phase==2){
                explicitReview=box->text().contains("Explicitly resume") && box->text().contains("Original state:");phase=3;
                if(race){const auto info=syncRequest(client,base,{},false);syncRequest(client,base+"/operations",{{"action","edit"},{"operation_id","race"},{"expected_state",info["state"]},{"cells",QJsonArray{QJsonObject{{"row",2},{"col",2},{"source","race"}}}}});}
                box->button(accept?QMessageBox::Yes:QMessageBox::Cancel)->click();return;
            }
            if(phase==0){phase=1;dialog->findChild<QLineEdit *>("operationJobId")->setText("retained");QTimer::singleShot(0,dialog->findChild<QPushButton *>("inspectOperationJob"),&QPushButton::click);}
            else if(phase==1 && status=="prepared"){phase=2;QTimer::singleShot(0,dialog->findChild<QPushButton *>("startOperationJob"),&QPushButton::click);}
            else if(phase==3 && (!accept || status=="completed" || report.startsWith("Status unavailable"))){completed=status=="completed";phase=4;dialog->accept();}
        });timer.start();window.findChild<QAction *>("operationJobsAction")->trigger();timer.stop();QCOMPARE(phase,4);QVERIFY(explicitReview);QCOMPARE(completed,accept && !race);
        const auto result=syncRequest(client,base+"/operation-jobs/retained/status",{});
        QCOMPARE(result["batches"].toArray().size(),accept && !race?1:0);
        // An execution refusal remains inspectable; clear its GUI lock before teardown.
        if(race){QTimer close;connect(&close,&QTimer::timeout,&window,[&]{if(auto *dialog=window.findChild<QDialog *>("operationJobsDialog"))if(dialog->findChild<QPlainTextEdit *>("operationJobReport")->toPlainText().contains("prepared"))dialog->accept();});close.start(10);window.findChild<QAction *>("operationJobsAction")->trigger();}
    }
    void exportWriterReportsFailuresWithoutSuccess() {
        struct Sink {
            bool opened=true,committed=true;int writes=0,commits=0;bool shortWrite=false;
            bool open(QIODevice::OpenMode){return opened;}
            qint64 write(const QByteArray &bytes){++writes;return bytes.size()-int(shortWrite);}
            bool commit(){++commits;return committed;}
            QString errorString()const{return "injected failure";}
        };
        QTemporaryDir temp;QString error;
        for(int mode=0;mode<3;++mode){
            Sink sink;sink.opened=mode!=0;sink.shortWrite=mode==1;sink.committed=mode!=2;
            QVERIFY(!ExportWriter::write(temp.filePath("out"),temp.filePath("native"),"bytes",sink,error));
            QVERIFY(error.contains("Spreadsheet destination failed"));
            QCOMPARE(sink.commits,mode==2?1:0);
        }
        Sink sink;QVERIFY(!ExportWriter::write(temp.filePath("out"),temp.filePath("native"),{},sink,error));QCOMPARE(sink.writes,0);
    }
    void exportWriterRejectsNativeAliasesAndInvalidSnapshots() {
        QTemporaryDir temp;const auto native=temp.filePath("native.dhwb"),alias=temp.filePath("alias.xlsx");
        QFile file(native);QVERIFY(file.open(QIODevice::WriteOnly));file.write("retained native inputs");file.close();QVERIFY(QFile::link(native,alias));
        const QJsonObject snapshot{{"file_base64",QString::fromLatin1(QByteArray("export").toBase64())}};QString error;
        for(const auto &path:{native,alias}){QVERIFY(!ExportWriter::save(path,native,snapshot,error));QVERIFY(error.contains("native workbook"));}
        std::error_code failure;const auto hard=temp.filePath("hard.xlsx");std::filesystem::create_hard_link(QFileInfo(native).filesystemFilePath(),QFileInfo(hard).filesystemFilePath(),failure);
        if(!failure)QVERIFY(!ExportWriter::save(hard,native,snapshot,error));
        const auto output=temp.filePath("out.xlsx");QVERIFY(!ExportWriter::save(output,native,{{"file_base64","invalid!"}},error));QVERIFY(!QFile::exists(output));
        QVERIFY(ExportWriter::save(output,native,snapshot,error));QFile exported(output);QVERIFY(exported.open(QIODevice::ReadOnly));QCOMPARE(exported.readAll(),QByteArray("export"));
        QVERIFY(file.open(QIODevice::ReadOnly));QCOMPARE(file.readAll(),QByteArray("retained native inputs"));
    }
    void recordExportRechecksNativeAliasAfterReview_data() {
        QTest::addColumn<QString>("format");QTest::newRow("xlsx")<<QString("xlsx");QTest::newRow("ods")<<QString("ods");
    }
    void recordExportRechecksNativeAliasAfterReview() {
        const bool nativeDialogs=QCoreApplication::testAttribute(Qt::AA_DontUseNativeDialogs);
        QCoreApplication::setAttribute(Qt::AA_DontUseNativeDialogs);
        const auto restoreDialogs=qScopeGuard([nativeDialogs]{QCoreApplication::setAttribute(Qt::AA_DontUseNativeDialogs,nativeDialogs);});
        QFETCH(QString,format);QTemporaryDir temp;const auto native=temp.filePath("records.dhwb"),destination=temp.filePath("export."+format);
        StartupOptions options;options.automaticWorkbook=false;options.viewStatePath=temp.filePath("views.ini");
        MainWindow window(options);window.show();QSignalSpy ready(&window,&MainWindow::serviceReady);QTRY_COMPARE_WITH_TIMEOUT(ready.count(),1,10000);
        QSignalSpy opened(&window,&MainWindow::workbookReady);window.openBook(native,true);QTRY_COMPARE_WITH_TIMEOUT(opened.count(),1,10000);
        window.submit({{"action","edit"},{"cells",QJsonArray{QJsonObject{{"row",1},{"col",1},{"source","7"}}}}});QTRY_COMPARE_WITH_TIMEOUT(opened.count(),2,10000);
        window.submit({{"action","list_declare"},{"id","Records"},{"spec",QJsonObject{{"r2",1},{"c2",1}}}});QTRY_COMPARE_WITH_TIMEOUT(opened.count(),3,10000);
        auto *grid=window.findChild<QTableView *>("grid");grid->setCurrentIndex(window.model()->index(0,0));QTRY_VERIFY_WITH_TIMEOUT(window.model()->loaded(grid->currentIndex()),5000);
        QFile file(native);QVERIFY(file.open(QIODevice::ReadOnly));const auto original=file.readAll();file.close();
        bool reviewed=false,linked=false;QTimer timer;timer.setInterval(10);
        connect(&timer,&QTimer::timeout,&window,[&]{
            auto *modal=QApplication::activeModalWidget();
            if(auto *dialog=window.findChild<QDialog *>("recordCommandDialog");dialog && !dialog->property("checked").toBool()){
                dialog->setProperty("checked",true);auto *mode=dialog->findChild<QComboBox *>("recordCommandMode");mode->setCurrentIndex(mode->findData(format+"_values"));dialog->accept();
            }
            if(auto *picker=qobject_cast<QFileDialog *>(modal);picker && !picker->property("checked").toBool()){
                picker->setProperty("checked",true);picker->setDirectory(temp.path());picker->findChild<QLineEdit *>("fileNameEdit")->setText(destination);QMetaObject::invokeMethod(picker,"accept",Qt::QueuedConnection);
            }
            if(auto *box=qobject_cast<QMessageBox *>(modal)){
                if(box->objectName()=="recordExportReview" && !reviewed){reviewed=true;linked=QFile::link(native,destination);}
                if(auto *yes=box->button(QMessageBox::Yes))yes->click();
            }
        });timer.start();window.findChild<QAction *>("recordCommandAction")->trigger();
        QTRY_VERIFY_WITH_TIMEOUT(window.findChild<QPlainTextEdit *>("operationIssues")->toPlainText().contains("destination resolves to the native workbook"),5000);timer.stop();
        QVERIFY(reviewed);QVERIFY(linked);QVERIFY(file.open(QIODevice::ReadOnly));QCOMPARE(file.readAll(),original);QCOMPARE(opened.count(),3);
    }
    void frozenTopRowModelForwardsEditsAndTracksSourceResets() {
        QStandardItemModel source(3,4); source.setData(source.index(0,0),"Header");
        FrozenTopRowModel first(&source); QAbstractItemModelTester tester(&first,QAbstractItemModelTester::FailureReportingMode::QtTest);
        QCOMPARE(first.rowCount(),1); QVERIFY(first.flags(first.index(0,0))&Qt::ItemIsEditable);
        QVERIFY(first.setData(first.index(0,0),"Edited",Qt::EditRole)); QCOMPARE(source.data(source.index(0,0)).toString(),QString("Edited"));
        source.insertRow(0); source.setData(source.index(0,0),"Inserted"); QCOMPARE(first.data(first.index(0,0)).toString(),QString("Inserted"));
        source.removeRows(0,source.rowCount()); QCOMPARE(first.rowCount(),0);
        auto *shortLived=new QStandardItemModel(1,4);
        FrozenTopRowModel detached(shortLived); QAbstractItemModelTester detachedTester(&detached,QAbstractItemModelTester::FailureReportingMode::QtTest);
        const auto stale=detached.index(0,0); delete shortLived;
        QCOMPARE(detached.rowCount(),0); QCOMPARE(detached.columnCount(),0);
        QVERIFY(!detached.data(stale).isValid()); QVERIFY(!detached.setData(stale,"ignored",Qt::EditRole));
    }
    void frozenTopRowEditsAlignsAndPersistsWithoutCoveringBody() {
        QTemporaryDir temp; StartupOptions options; options.automaticWorkbook=false;
        options.viewStatePath=temp.filePath("views.ini"); const auto path=temp.filePath("frozen.dhwb");
        {
            MainWindow window(options); window.show();
            QSignalSpy ready(&window,&MainWindow::serviceReady); QTRY_COMPARE_WITH_TIMEOUT(ready.count(),1,10000);
            QSignalSpy opened(&window,&MainWindow::workbookReady); window.openBook(path,true);
            QTRY_COMPARE_WITH_TIMEOUT(opened.count(),1,10000);
            window.submit({{"action","edit"},{"cells",QJsonArray{QJsonObject{{"row",1},{"col",1},{"source","=SUM(7,13.5)"},{"style",QJsonObject{{"border","box"}}}}}}});
            QTRY_COMPARE_WITH_TIMEOUT(opened.count(),2,10000);
            auto *grid=window.findChild<QTableView *>("grid"); auto *model=window.model();
            QTRY_VERIFY_WITH_TIMEOUT(model->sourceReady(model->index(0,0)),5000);
            auto *freeze=window.findChild<QAction *>("freezeTopRowAction"); freeze->trigger();
            auto *top=window.findChild<QTableView *>("frozenTopRow"); QVERIFY(top->isVisible()); QVERIFY(grid->isRowHidden(0));
            QVERIFY(top->viewport()->mapToGlobal(QPoint(0,top->rowHeight(0))).y()<=grid->viewport()->mapToGlobal(QPoint()).y());
            grid->scrollTo(model->index(10000,7),QAbstractItemView::PositionAtTop);
            QTRY_VERIFY_WITH_TIMEOUT(model->loaded(model->index(10000,7)),5000);
            grid->setColumnWidth(7,188); QCOMPARE(top->columnWidth(7),188);
            QCOMPARE(top->viewport()->mapToGlobal(top->visualRect(top->model()->index(0,7)).topLeft()).x(),
                     grid->viewport()->mapToGlobal(grid->visualRect(model->index(10000,7)).topLeft()).x());
            const int scroll=grid->verticalScrollBar()->value(); top->setCurrentIndex(top->model()->index(0,7));
            QCOMPARE(grid->currentIndex(),model->index(0,7)); QCOMPARE(grid->verticalScrollBar()->value(),scroll);
            top->setFocus(); QTest::keyClick(top,Qt::Key_Right,Qt::ShiftModifier);
            QCOMPARE(grid->selectionModel()->selectedIndexes().size(),2);
            top->setCurrentIndex(top->model()->index(0,7));
            QSignalSpy edits(model,&SheetModel::editRequested);
            top->edit(top->model()->index(0,7)); QPointer<QLineEdit> editor=top->findChild<QLineEdit *>(); QVERIFY(editor);
            editor->setText("999"); QTest::keyClick(editor,Qt::Key_Escape); QTRY_VERIFY_WITH_TIMEOUT(editor.isNull(),3000); QVERIFY(edits.isEmpty());
            top->edit(top->model()->index(0,7)); editor=top->findChild<QLineEdit *>(); QVERIFY(editor);
            QCOMPARE(editor->accessibleName(),QString("Cell H1 input")); editor->setText("123"); QTest::keyClick(editor,Qt::Key_Return);
            QTRY_COMPARE_WITH_TIMEOUT(opened.count(),3,10000); QTRY_COMPARE_WITH_TIMEOUT(edits.count(),1,5000);
            QTRY_COMPARE_WITH_TIMEOUT(top->model()->data(top->model()->index(0,7)).toString(),QString("123"),5000);
            QTRY_COMPARE_WITH_TIMEOUT(grid->currentIndex(),model->index(1,7),5000);
            QTest::keyClick(grid,Qt::Key_Up); QCOMPARE(grid->currentIndex(),model->index(0,7));
            QTest::keyClick(top,Qt::Key_Down); QCOMPARE(grid->currentIndex(),model->index(1,7));
            QTest::mouseClick(top->horizontalHeader()->viewport(),Qt::LeftButton,Qt::NoModifier,
                QPoint(top->horizontalHeader()->sectionViewportPosition(7)+10,5));
            QCOMPARE(grid->selectionModel()->selection().first().height(),model->rowCount());
            top->setRowHeight(0,77); QCOMPARE(top->rowHeight(0),77);
            grid->selectColumn(2); window.findChild<QAction *>("hideColumnsAction")->trigger(); QVERIFY(top->isColumnHidden(2));
            window.showList({{"r1",1},{"r2",3},{"c1",1},{"c2",8},{"header",false}});
            QTRY_COMPARE_WITH_TIMEOUT(opened.count(),4,10000); QVERIFY(!top->isVisible());
            window.findChild<QAction *>("clearListAction")->trigger();
            QTRY_COMPARE_WITH_TIMEOUT(opened.count(),5,10000); QVERIFY(top->isVisible());
            QVERIFY(model->cacheMetrics()["tiles"].toInt()<=16);
        }
        {
            MainWindow window(options); window.show();
            QSignalSpy ready(&window,&MainWindow::serviceReady); QTRY_COMPARE_WITH_TIMEOUT(ready.count(),1,10000);
            QSignalSpy opened(&window,&MainWindow::workbookReady); window.openBook(path,false,true);
            QTRY_COMPARE_WITH_TIMEOUT(opened.count(),1,10000);
            auto *top=window.findChild<QTableView *>("frozenTopRow"); auto *grid=window.findChild<QTableView *>("grid");
            QVERIFY(top->isVisible()); QVERIFY(top->isColumnHidden(2));
            QCOMPARE(top->rowHeight(0),77);
            QVERIFY(!(top->model()->flags(top->model()->index(0,7))&Qt::ItemIsEditable));
            QTRY_COMPARE_WITH_TIMEOUT(top->model()->data(top->model()->index(0,7)).toString(),QString("123"),5000);
            window.findChild<QAction *>("freezeTopRowAction")->trigger(); QVERIFY(!top->isVisible()); QVERIFY(!grid->isRowHidden(0));
            QCOMPARE(grid->rowHeight(0),77);
        }
    }
    void frozenRowProbeKeepsOneSourceRowAcrossChangesAndReset() {
        QStandardItemModel source(3,8);
        source.setData(source.index(0,0),"Header");
        FirstRowModel first(&source);
        QAbstractItemModelTester tester(&first,QAbstractItemModelTester::FailureReportingMode::QtTest);
        QCOMPARE(first.rowCount(),1); QCOMPARE(first.columnCount(),8);
        QCOMPARE(first.data(first.index(0,0)).toString(),QString("Header"));
        QVERIFY(!(first.flags(first.index(0,0)) & Qt::ItemIsEditable));
        QSignalSpy changed(&first,&QAbstractItemModel::dataChanged);
        source.setData(source.index(1,0),"Body"); QCOMPARE(changed.count(),0);
        source.setData(source.index(0,0),"Updated"); QCOMPARE(changed.count(),1);
        QCOMPARE(first.data(first.index(0,0)).toString(),QString("Updated"));
        source.insertRow(0);
        source.setData(source.index(0,0),"New header");
        QCOMPARE(first.data(first.index(0,0)).toString(),QString("New header"));
        source.removeRow(0);
        QCOMPARE(first.data(first.index(0,0)).toString(),QString("Updated"));
        source.clear(); QCOMPARE(first.rowCount(),0); QCOMPARE(first.columnCount(),0);
    }
    void frozenRowProbeRemainsVisibleDuringViewportScrolling() {
        QStandardItemModel source(200,20);
        source.setData(source.index(0,0),"Header");
        QTableView grid; grid.setModel(&source); grid.resize(500,300); grid.show();
        FrozenRowProbe frozen(&grid);
        QCoreApplication::processEvents();
        grid.scrollTo(source.index(100,10),QAbstractItemView::PositionAtTop);
        QCoreApplication::processEvents();
        QCOMPARE(frozen.view.pos(),grid.viewport()->pos());
        QVERIFY(frozen.view.isVisible());
        QCOMPARE(frozen.view.width(),grid.viewport()->width());
    }
    void viewportRepliesPreserveAnAddressBeingTyped() {
        QTemporaryDir temp; StartupOptions options; options.automaticWorkbook=false;
        MainWindow window(options); window.show(); QSignalSpy ready(&window,&MainWindow::serviceReady);
        QTRY_COMPARE_WITH_TIMEOUT(ready.count(),1,10000);
        QSignalSpy opened(&window,&MainWindow::workbookReady); window.openBook(temp.filePath("address-draft.dhwb"),true);
        QTRY_COMPARE_WITH_TIMEOUT(opened.count(),1,10000);
        auto *model=window.model(); auto *grid=window.findChild<QTableView *>("grid");
        auto *address=window.findChild<QLineEdit *>("address");
        QTRY_VERIFY_WITH_TIMEOUT(model->sourceReady(model->index(0,0)),3000);
        QSignalSpy loaded(model,&SheetModel::tileLoaded);
        model->data(model->index(64000,0));
        address->setFocus(); address->selectAll(); QTest::keyClicks(address,"B42");
        QTRY_VERIFY_WITH_TIMEOUT(loaded.count()>0,3000);
        QCOMPARE(address->text(),QString("B42"));
        QTest::keyClick(address,Qt::Key_Return);
        QTRY_COMPARE(grid->currentIndex(),model->index(41,1));
        QCOMPARE(address->text(),QString("B42"));
    }
    void scrollingPastTileLimitKeepsTheVisibleViewportLoaded() {
        QTemporaryDir temp; StartupOptions options; options.automaticWorkbook=false;
        MainWindow window(options); window.show(); QSignalSpy ready(&window,&MainWindow::serviceReady);
        QTRY_COMPARE_WITH_TIMEOUT(ready.count(),1,10000);
        QSignalSpy opened(&window,&MainWindow::workbookReady); window.openBook(temp.filePath("scroll.dhwb"),true);
        QTRY_COMPARE_WITH_TIMEOUT(opened.count(),1,10000);
        auto *grid=window.findChild<QTableView *>("grid"); auto *model=window.model();
        auto visibleReady=[&] {
            bool loaded=true;
            for(int row=grid->rowAt(0);row<=grid->rowAt(grid->viewport()->height()-1);++row)
                for(int col=grid->columnAt(0);col<=grid->columnAt(grid->viewport()->width()-1);++col) {
                    const auto index=model->index(row,col); model->data(index);
                    loaded=loaded && model->loaded(index);
                }
            return loaded;
        };
        for (int sample=0;sample<40;++sample) {
            grid->scrollTo(model->index(40+sample*64,0),QAbstractItemView::PositionAtCenter);
            QCoreApplication::processEvents();
            QTRY_VERIFY_WITH_TIMEOUT(visibleReady(),3000);
            const auto cache=model->cacheMetrics();
            QVERIFY(cache["tiles"].toInt()<=16);
            QVERIFY(cache["tile_charge_bytes"].toInt()<=cache["tile_budget_bytes"].toInt());
        }
    }
    void wholeRowColumnCommandsAdjustReferencesAndWarnOnDeletion() {
        QTemporaryDir temp; StartupOptions options; options.automaticWorkbook=false;
        MainWindow window(options); window.show(); QSignalSpy ready(&window,&MainWindow::serviceReady);
        QTRY_COMPARE_WITH_TIMEOUT(ready.count(),1,10000);
        QSignalSpy opened(&window,&MainWindow::workbookReady); window.openBook(temp.filePath("structure.dhwb"),true);
        QTRY_COMPARE_WITH_TIMEOUT(opened.count(),1,10000);
        window.submit({{"action","edit"},{"cells",QJsonArray{QJsonObject{{"row",2},{"col",1},{"source","7"}},
            QJsonObject{{"row",4},{"col",2},{"source","=$A$2"}}}}});
        QTRY_COMPARE_WITH_TIMEOUT(opened.count(),2,10000);
        auto *grid=window.findChild<QTableView *>("grid"); auto *model=window.model();
        const auto original=model->state(); grid->selectRow(1);
        answerNextDialog(QMessageBox::Cancel); window.findChild<QAction *>("insertRowsAction")->trigger();
        QCOMPARE(model->state(),original);
        answerNextDialog(QMessageBox::Yes); window.findChild<QAction *>("insertRowsAction")->trigger();
        QTRY_COMPARE_WITH_TIMEOUT(opened.count(),3,10000);
        QTRY_COMPARE_WITH_TIMEOUT(model->data(model->index(4,1),Qt::EditRole).toString(),QString("=$A$3"),5000);
        QTRY_COMPARE_WITH_TIMEOUT(model->data(model->index(4,1)).toString(),QString("7"),5000);
        grid->selectColumn(0); answerNextDialog(QMessageBox::Yes);
        window.findChild<QAction *>("insertColumnsAction")->trigger();
        QTRY_COMPARE_WITH_TIMEOUT(opened.count(),4,10000);
        QTRY_COMPARE_WITH_TIMEOUT(model->data(model->index(4,2),Qt::EditRole).toString(),QString("=$B$3"),5000);
        grid->selectRow(2); answerNextDialog(QMessageBox::Yes);
        window.findChild<QAction *>("deleteRowsAction")->trigger();
        QTRY_COMPARE_WITH_TIMEOUT(opened.count(),5,10000);
        QTRY_COMPARE_WITH_TIMEOUT(model->data(model->index(3,2)).toString(),QString("#REF!"),5000);
        QVERIFY(window.findChild<QPlainTextEdit *>("operationIssues")->toPlainText().contains("1 reference expressions became #REF!"));
        grid->selectColumn(1); answerNextDialog(QMessageBox::Yes);
        window.findChild<QAction *>("deleteColumnsAction")->trigger();
        QTRY_COMPARE_WITH_TIMEOUT(opened.count(),6,10000);
        QTRY_COMPARE_WITH_TIMEOUT(model->data(model->index(3,1),Qt::EditRole).toString(),QString("=#REF!"),5000);
        model->setProjection({0,2});
        window.findChild<QAction *>("insertRowsAction")->trigger();
        QVERIFY(window.findChild<QPlainTextEdit *>("operationIssues")->toPlainText().contains("Clear the list view before changing rows or columns"));
        QCOMPARE(opened.count(),6);
    }
    void rowDeletionConfirmationRetainsReviewedStateAfterExternalRefresh() {
        QTemporaryDir temp; StartupOptions options; options.automaticWorkbook=false;
        MainWindow window(options); window.show(); QSignalSpy ready(&window,&MainWindow::serviceReady);
        QTRY_COMPARE_WITH_TIMEOUT(ready.count(),1,10000);
        const auto path=temp.filePath("structure-race.dhwb");
        QSignalSpy opened(&window,&MainWindow::workbookReady); window.openBook(path,true);
        QTRY_COMPARE_WITH_TIMEOUT(opened.count(),1,10000);
        window.submit({{"action","edit"},{"cells",QJsonArray{QJsonObject{{"row",2},{"col",1},{"source","7"}}}}});
        QTRY_COMPARE_WITH_TIMEOUT(opened.count(),2,10000);
        MainWindow writer(options); QSignalSpy writerReady(&writer,&MainWindow::serviceReady);
        QTRY_COMPARE_WITH_TIMEOUT(writerReady.count(),1,10000);
        QSignalSpy writerOpened(&writer,&MainWindow::workbookReady); writer.openBook(path);
        QTRY_COMPARE_WITH_TIMEOUT(writerOpened.count(),1,10000);
        connect(&writer,&MainWindow::workbookReady,&window,[&]{window.model()->externalChange();});
        bool confirmed=false;
        connect(&window,&MainWindow::workbookReady,&window,[&]{
            if (auto *box=qobject_cast<QMessageBox *>(QApplication::activeModalWidget())) {
                confirmed=true; box->button(QMessageBox::Yes)->click();
            }
        });
        window.findChild<QTableView *>("grid")->selectRow(1);
        QSignalSpy changed(&window,&MainWindow::operationFinished);
        QTimer::singleShot(0,&writer,[&]{writer.submit({{"action","edit"},{"cells",QJsonArray{QJsonObject{{"row",2},{"col",1},{"source","9"}}}}});});
        QTimer::singleShot(10000,&window,[]{
            if (auto *box=qobject_cast<QMessageBox *>(QApplication::activeModalWidget())) box->button(QMessageBox::Cancel)->click();
        });
        window.findChild<QAction *>("deleteRowsAction")->trigger();
        QVERIFY(confirmed); QCOMPARE(changed.count(),0);
        QVERIFY(window.findChild<QPlainTextEdit *>("operationIssues")->toPlainText().contains("Reviewed inputs changed"));
        QTRY_COMPARE_WITH_TIMEOUT(window.model()->data(window.model()->index(1,0)).toString(),QString("9"),5000);
    }
    void listSaveRetainsItsSourceCellAfterResorting() {
        QTemporaryDir temp; StartupOptions options; options.automaticWorkbook=false;
        MainWindow window(options); window.show(); QSignalSpy ready(&window,&MainWindow::serviceReady);
        QTRY_COMPARE_WITH_TIMEOUT(ready.count(),1,10000);
        QSignalSpy opened(&window,&MainWindow::workbookReady); window.openBook(temp.filePath("list-save.dhwb"),true);
        QTRY_COMPARE_WITH_TIMEOUT(opened.count(),1,10000);
        window.submit({{"action","edit"},{"cells",QJsonArray{
            QJsonObject{{"row",1},{"col",1},{"source","10"}},QJsonObject{{"row",2},{"col",1},{"source","20"}},
            QJsonObject{{"row",3},{"col",1},{"source","30"}},QJsonObject{{"row",2},{"col",2},{"source","original"}}}}});
        QTRY_COMPARE_WITH_TIMEOUT(opened.count(),2,10000);
        window.showList({{"r1",1},{"r2",3},{"c1",1},{"c2",2},{"sort",QJsonArray{QJsonObject{{"col",1},{"descending",true}}}}});
        QTRY_COMPARE_WITH_TIMEOUT(opened.count(),3,10000);
        auto *grid=window.findChild<QTableView *>("grid"); auto *source=window.findChild<QLineEdit *>("source"); auto *model=window.model();
        grid->setCurrentIndex(model->index(1,1)); QTRY_VERIFY_WITH_TIMEOUT(source->isEnabled(),5000);
        source->setFocus(); source->selectAll(); QTest::keyClicks(source,"updated");
        window.findChild<QAction *>("saveAction")->trigger(); QTRY_COMPARE_WITH_TIMEOUT(opened.count(),4,10000);
        QCOMPARE(model->sourceRow(grid->currentIndex().row()),1); QCOMPARE(grid->currentIndex().column(),1);
        QTRY_COMPARE_WITH_TIMEOUT(model->data(grid->currentIndex()).toString(),QString("updated"),5000);
        grid->setCurrentIndex(model->index(1,0)); QTRY_VERIFY_WITH_TIMEOUT(source->isEnabled(),5000);
        source->setFocus(); source->selectAll(); QTest::keyClicks(source,"5");
        window.findChild<QAction *>("saveAction")->trigger(); QTRY_COMPARE_WITH_TIMEOUT(opened.count(),5,10000);
        QCOMPARE(model->sourceRow(grid->currentIndex().row()),1); QCOMPARE(grid->currentIndex(),model->index(2,0));
        QTRY_COMPARE_WITH_TIMEOUT(model->data(grid->currentIndex()).toString(),QString("5"),5000);
    }
    void modelInvariants() {
        RestClient client;
        SheetModel model(&client, nullptr, 64, 32);
        QAbstractItemModelTester tester(&model, QAbstractItemModelTester::FailureReportingMode::QtTest);
        QCOMPARE(model.headerData(26, Qt::Horizontal, Qt::DisplayRole).toString(), QString("AA"));
    }
    void managedSupplies() {
        QTemporaryDir temp;
        StartupOptions options; options.automaticWorkbook = false;
        MainWindow window(options); window.show();
        QSignalSpy service(&window, &MainWindow::serviceReady);
        QTRY_COMPARE_WITH_TIMEOUT(service.count(), 1, 10000);
        QSignalSpy opened(&window, &MainWindow::workbookReady);
        window.openBook(temp.filePath("supplies.dhwb"), true);
        QTRY_COMPARE_WITH_TIMEOUT(opened.count(), 1, 10000);
        auto *model = window.model();
        auto *grid = window.findChild<QTableView *>("grid");
        auto *source = window.findChild<QLineEdit *>("source");
        QTRY_VERIFY_WITH_TIMEOUT(model->loaded(model->index(0,0)), 5000);
        QSignalSpy changed(&window, &MainWindow::operationFinished);
        QJsonArray cells;
        auto cell = [&cells](int row, int col, const QString &text) { cells.append(QJsonObject{{"row", row}, {"col", col}, {"source", text}}); };
        cell(1,1,"Item"); cell(1,2,"Quantity"); cell(1,3,"Unit price"); cell(1,4,"Amount");
        cell(2,1,"Notebook"); cell(2,2,"2"); cell(2,3,"3.5"); cell(2,4,"=B2*C2");
        cell(3,1,"Pen"); cell(3,2,"4"); cell(3,3,"1.25"); cell(3,4,"=B3*C3");
        cell(4,1,"Folder"); cell(4,2,"3"); cell(4,3,"2"); cell(4,4,"=B4*C4");
        cell(6,3,"Total"); cell(6,4,"=SUM(D2:D4)");
        window.submit({{"action","edit"},{"cells",cells}});
        QTRY_COMPARE_WITH_TIMEOUT(changed.count(), 1, 10000);
        QTRY_VERIFY_WITH_TIMEOUT(opened.count() >= 2, 10000);
        QTRY_COMPARE_WITH_TIMEOUT(model->data(model->index(5,3)).toString(), QString("18"), 5000);
        grid->setCurrentIndex(model->index(2,1)); source->setFocus(); source->selectAll();
        QTest::keyClicks(source,"6"); QTest::keyClick(source,Qt::Key_Return);
        QTRY_COMPARE_WITH_TIMEOUT(changed.count(),2,10000);
        QTRY_VERIFY_WITH_TIMEOUT(opened.count() >= 3,10000);
        QTRY_COMPARE_WITH_TIMEOUT(model->data(model->index(5,3)).toString(), QString("20.5"),5000);
        window.submit({{"action","undo"}});
        QTRY_COMPARE_WITH_TIMEOUT(changed.count(),3,10000);
        QTRY_VERIFY_WITH_TIMEOUT(opened.count() >= 4,10000);
        QTRY_COMPARE_WITH_TIMEOUT(model->data(model->index(5,3)).toString(),QString("18"),5000);
        // A projected edit must use the source row, even after reordering.
        model->setProjection({0, 3, 2});
        QTRY_COMPARE_WITH_TIMEOUT(model->data(model->index(1,0)).toString(),QString("Folder"),5000);
        QCOMPARE(model->headerData(1,Qt::Vertical,Qt::DisplayRole).toInt(),4);
        grid->setCurrentIndex(model->index(1,1));
        source->setFocus(); source->selectAll(); QTest::keyClicks(source,"5"); QTest::keyClick(source,Qt::Key_Escape);
        QCOMPARE(source->text(),QString("3"));
        source->setFocus(); source->selectAll(); QTest::keyClicks(source,"4"); QTest::keyClick(source,Qt::Key_Return);
        QTRY_COMPARE_WITH_TIMEOUT(changed.count(),4,10000);
        QTRY_VERIFY_WITH_TIMEOUT(opened.count() >= 5,10000);
        QTRY_COMPARE_WITH_TIMEOUT(model->data(model->index(3,1)).toString(),QString("4"),5000);
        window.submit({{"action","undo"}});
        QTRY_COMPARE_WITH_TIMEOUT(changed.count(),5,10000);
        QTRY_VERIFY_WITH_TIMEOUT(opened.count() >= 6,10000);
        QTRY_COMPARE_WITH_TIMEOUT(model->data(model->index(5,3)).toString(),QString("18"),5000);
        grid->setCurrentIndex(model->index(2,1)); source->setFocus(); source->selectAll(); QTest::keyClicks(source,"6");
        window.findChild<QAction *>("saveAction")->trigger();
        QTRY_COMPARE_WITH_TIMEOUT(changed.count(),6,10000);
        QTRY_VERIFY_WITH_TIMEOUT(opened.count() >= 7,10000);
        QTRY_COMPARE_WITH_TIMEOUT(model->data(model->index(5,3)).toString(),QString("20.5"),5000);
        QTRY_COMPARE_WITH_TIMEOUT(window.findChild<QLabel *>("storageStatus")->text(),QString("Stored automatically • Save confirmed"),5000);
        QJsonArray formats;
        for(int row=2;row<=6;++row) formats.append(QJsonObject{{"row",row},{"col",4},{"style",QJsonObject{{"format","fixed2"}}}});
        window.submit({{"action","edit"},{"cells",formats}});
        QTRY_COMPARE_WITH_TIMEOUT(changed.count(),7,10000);
        QTRY_VERIFY_WITH_TIMEOUT(opened.count() >= 8,10000);
        QTRY_COMPARE_WITH_TIMEOUT(model->data(model->index(5,3)).toString(),QString("20.50"),5000);
        auto *address = window.findChild<QLineEdit *>("address");
        address->setText("XFD1048576"); QTest::keyClick(address,Qt::Key_Return);
        QCOMPARE(grid->currentIndex(),model->index(1048575,16383));
        address->setText("A1"); QTest::keyClick(address,Qt::Key_Return);
        auto screenshot = qEnvironmentVariable("DATAHORIZON_SCREENSHOT");
        if (!screenshot.isEmpty()) { QTest::qWait(300); QVERIFY(window.grab().save(screenshot)); }
    }
    void managedRequiredHook() {
        QTemporaryDir temp;
        StartupOptions options; options.automaticWorkbook=false; options.handler=QString(DATAHORIZON_FIXTURES)+"/validation_hook.py";
        MainWindow window(options); window.show();
        QSignalSpy ready(&window,&MainWindow::serviceReady);
        QTRY_COMPARE_WITH_TIMEOUT(ready.count(),1,10000);
        QSignalSpy opened(&window,&MainWindow::workbookReady);
        window.openBook(temp.filePath("hooks.dhwb"),true);
        QTRY_COMPARE_WITH_TIMEOUT(opened.count(),1,10000);
        auto *model=window.model();
        QTRY_VERIFY_WITH_TIMEOUT(model->loaded(model->index(0,0)),5000);
        QSignalSpy changed(&window,&MainWindow::operationFinished);
        QVERIFY(model->setData(model->index(0,0),"8",Qt::EditRole));
        QTRY_COMPARE_WITH_TIMEOUT(changed.count(),1,10000);
        QVERIFY(changed[0][0].toBool());
        QTRY_VERIFY_WITH_TIMEOUT(opened.count()>=2,10000);
        QTRY_COMPARE_WITH_TIMEOUT(model->data(model->index(0,1)).toString(),QString("16"),5000);
        QVERIFY(model->setData(model->index(0,0),"-1",Qt::EditRole));
        QTRY_COMPARE_WITH_TIMEOUT(changed.count(),2,10000);
        QVERIFY(!changed[1][0].toBool());
        QTRY_VERIFY_WITH_TIMEOUT(opened.count()>=3,10000);
        auto *source=window.findChild<QLineEdit *>("source");
        QTRY_COMPARE_WITH_TIMEOUT(source->text(),QString("-1"),5000);
        QTRY_VERIFY_WITH_TIMEOUT(source->isEnabled(),5000);
        QVERIFY(source->isModified());
        QCOMPARE(model->data(model->index(0,0)).toString(),QString("8"));
        QCOMPARE(model->data(model->index(0,1)).toString(),QString("16"));
        QVERIFY(window.findChild<QPlainTextEdit *>("operationIssues")->toPlainText().contains("nonnegative"));
    }
    void closingWindowRequiresExplicitDiscardOfAnActiveCellDraft() {
        QTemporaryDir temp; StartupOptions options; options.automaticWorkbook=false;
        MainWindow window(options); window.show(); QSignalSpy ready(&window,&MainWindow::serviceReady);
        QTRY_COMPARE_WITH_TIMEOUT(ready.count(),1,10000);
        QSignalSpy opened(&window,&MainWindow::workbookReady); window.openBook(temp.filePath("close-draft.dhwb"),true);
        QTRY_COMPARE_WITH_TIMEOUT(opened.count(),1,10000);
        auto *grid=window.findChild<QTableView *>("grid"); auto *source=window.findChild<QLineEdit *>("source");
        QTRY_VERIFY_WITH_TIMEOUT(window.model()->sourceReady(window.model()->index(0,0)),5000);
        grid->edit(window.model()->index(0,0)); QPointer<QLineEdit> editor=grid->findChild<QLineEdit *>(); QVERIFY(editor);
        QTest::keyClicks(editor,"unsubmitted input");
        QSignalSpy changed(&window,&MainWindow::operationFinished);
        answerNextDialog(QMessageBox::No);
        QVERIFY(!window.close());
        QCOMPARE(source->text(),QString("unsubmitted input")); QVERIFY(source->isModified());
        QCOMPARE(changed.count(),0); QTRY_VERIFY_WITH_TIMEOUT(editor.isNull(),3000);
        answerNextDialog(QMessageBox::Yes); QVERIFY(window.close());
    }
    void openingWorkbookRequiresExplicitDiscardOfAnActiveCellDraft() {
        QTemporaryDir temp; StartupOptions options; options.automaticWorkbook=false;
        MainWindow window(options); window.show(); QSignalSpy ready(&window,&MainWindow::serviceReady);
        QTRY_COMPARE_WITH_TIMEOUT(ready.count(),1,10000);
        QSignalSpy opened(&window,&MainWindow::workbookReady); window.openBook(temp.filePath("first.dhwb"),true);
        QTRY_COMPARE_WITH_TIMEOUT(opened.count(),1,10000);
        auto *grid=window.findChild<QTableView *>("grid"); auto *source=window.findChild<QLineEdit *>("source");
        QTRY_VERIFY_WITH_TIMEOUT(window.model()->sourceReady(window.model()->index(0,0)),5000);
        const auto original=window.model()->workbook();
        grid->edit(window.model()->index(0,0)); QPointer<QLineEdit> editor=grid->findChild<QLineEdit *>(); QVERIFY(editor);
        QTest::keyClicks(editor,"unsubmitted input");
        const auto second=temp.filePath("second.dhwb");
        answerNextDialog(QMessageBox::No); window.openBook(second,true);
        QCOMPARE(window.model()->workbook(),original); QCOMPARE(opened.count(),1); QVERIFY(!QFile::exists(second));
        QCOMPARE(source->text(),QString("unsubmitted input")); QVERIFY(source->isModified());
        QTRY_VERIFY_WITH_TIMEOUT(editor.isNull(),3000);
        answerNextDialog(QMessageBox::Yes); window.openBook(second,true);
        QTRY_COMPARE_WITH_TIMEOUT(opened.count(),2,10000);
        QVERIFY(window.model()->workbook()!=original);
    }
    void attachedHandleReleaseKeepsInteractionLocked_data() {
        QTest::addColumn<QString>("pendingRead");
        QTest::newRow("no-pending-read") << QString();
        QTest::newRow("newer-metadata") << QString("metadata");
        QTest::newRow("pending-range") << QString("range");
    }
    void attachedHandleReleaseKeepsInteractionLocked() {
        QFETCH(QString,pendingRead);
        QTemporaryDir temp; QJsonObject service; StartupOptions ownerOptions; ownerOptions.automaticWorkbook=false;
        MainWindow owner(ownerOptions); observeOwnedServiceStartup(owner,service); QSignalSpy serviceReady(&owner,&MainWindow::serviceReady);
        QTRY_COMPARE_WITH_TIMEOUT(serviceReady.count(),1,10000); QVERIFY(!service["token"].toString().isEmpty());
        const auto path=temp.filePath("attached-release.dhwb");
        QSignalSpy ownerOpened(&owner,&MainWindow::workbookReady); owner.openBook(path,true);
        QTRY_COMPARE_WITH_TIMEOUT(ownerOpened.count(),1,10000);
        owner.submit({{"action","sheet_add"},{"name","Other"}}); QTRY_COMPARE_WITH_TIMEOUT(ownerOpened.count(),2,10000);
        DelayedCloseProxy proxy(service); QVERIFY(proxy.listen(QHostAddress::LocalHost,0));
        StartupOptions options; options.automaticWorkbook=false; options.url="http://127.0.0.1:"+QString::number(proxy.serverPort());
        options.token=service["token"].toString(); options.recoveryDirectory=temp.filePath("recovery");
        MainWindow window(options); window.show(); QSignalSpy ready(&window,&MainWindow::serviceReady);
        QTRY_COMPARE_WITH_TIMEOUT(ready.count(),1,10000);
        QSignalSpy opened(&window,&MainWindow::workbookReady); window.openBook(path); QTRY_COMPARE_WITH_TIMEOUT(opened.count(),1,10000);
        auto *model=window.model(); auto *source=window.findChild<QLineEdit *>("source"); auto *tabs=window.findChild<QTabBar *>("sheets");
        QTRY_VERIFY_WITH_TIMEOUT(source->isEnabled(),5000);
        const auto handle=model->workbook(), sheet=model->sheet(), state=model->state();
        if(pendingRead=="metadata") {
            owner.submit({{"action","edit"},{"cells",QJsonArray{QJsonObject{{"row",1},{"col",1},{"source","independent change"}}}}});
            QTRY_COMPARE_WITH_TIMEOUT(ownerOpened.count(),3,10000);
            proxy.holdMetadata=true;
            QAction *refresh=nullptr; for(auto *action:window.findChildren<QAction *>()) if(action->text()=="Refresh stored data") refresh=action;
            QVERIFY(refresh); refresh->trigger(); QTRY_VERIFY_WITH_TIMEOUT(proxy.metadataReply.socket,3000);
            QVERIFY(proxy.metadataReply.result["state"].toString()!=state);
        } else if(pendingRead=="range") {
            proxy.holdRange=true; model->data(model->index(64,0)); QTRY_VERIFY_WITH_TIMEOUT(proxy.rangeReply.socket,3000);
        }
        QVERIFY(!window.close()); QTRY_VERIFY_WITH_TIMEOUT(proxy.closeReply.socket,3000);
        QVERIFY(proxy.closeReply.error.isEmpty()); QVERIFY(proxy.closeReply.result["closed"].toBool()); QCOMPARE(proxy.closedHandle,handle);
        if(pendingRead=="metadata") {
            proxy.releaseMetadata(); QTRY_COMPARE_WITH_TIMEOUT(opened.count(),2,3000); QVERIFY(model->state()!=state);
        } else if(pendingRead=="range") {
            proxy.releaseRange(); QTRY_VERIFY_WITH_TIMEOUT(model->loaded(model->index(64,0)),3000);
        }
        QVERIFY(window.isVisible()); QVERIFY(!source->isEnabled()); QVERIFY(!tabs->isEnabled());
        QVERIFY(!(model->flags(model->index(0,0))&Qt::ItemIsEditable));
        QSignalSpy changed(&window,&MainWindow::operationFinished);
        source->returnPressed();
        window.submit({{"action","edit"},{"cells",QJsonArray{QJsonObject{{"row",1},{"col",1},{"source","must not submit"}}}}});
        const auto second=temp.filePath("must-not-open.dhwb"); window.openBook(second,true); tabs->setCurrentIndex(1);
        QTest::qWait(50); QCOMPARE(proxy.mutations,0); QCOMPARE(changed.count(),0); QCOMPARE(proxy.opens,1);
        QCOMPARE(model->sheet(),sheet); QVERIFY(!QFile::exists(second));
        proxy.releaseClose(); QTRY_VERIFY_WITH_TIMEOUT(!window.isVisible(),3000); QCOMPARE(proxy.closes,1);
        QCOMPARE(owner.findChild<QProcess *>("ownedService")->state(),QProcess::Running);
        QVERIFY(QDir(options.recoveryDirectory).entryList({"operation-*.json"},QDir::Files).isEmpty());
    }
    void failedAttachedHandleCleanupStillCloses_data() {
        QTest::addColumn<QString>("failure");
        QTest::newRow("http-error") << QString("http");
        QTest::newRow("disconnected-service") << QString("disconnect");
    }
    void failedAttachedHandleCleanupStillCloses() {
        QFETCH(QString,failure);
        QTemporaryDir temp; QJsonObject service; StartupOptions ownerOptions; ownerOptions.automaticWorkbook=false;
        MainWindow owner(ownerOptions); observeOwnedServiceStartup(owner,service); QSignalSpy serviceReady(&owner,&MainWindow::serviceReady);
        QTRY_COMPARE_WITH_TIMEOUT(serviceReady.count(),1,10000); QVERIFY(!service["token"].toString().isEmpty());
        const auto path=temp.filePath("failed-close.dhwb");
        QSignalSpy ownerOpened(&owner,&MainWindow::workbookReady); owner.openBook(path,true);
        QTRY_COMPARE_WITH_TIMEOUT(ownerOpened.count(),1,10000);
        DelayedCloseProxy proxy(service); proxy.closeFailure=failure; QVERIFY(proxy.listen(QHostAddress::LocalHost,0));
        StartupOptions options; options.automaticWorkbook=false; options.url="http://127.0.0.1:"+QString::number(proxy.serverPort());
        options.token=service["token"].toString(); options.recoveryDirectory=temp.filePath("recovery");
        MainWindow window(options); window.show(); QSignalSpy ready(&window,&MainWindow::serviceReady);
        QTRY_COMPARE_WITH_TIMEOUT(ready.count(),1,10000);
        QSignalSpy opened(&window,&MainWindow::workbookReady); window.openBook(path); QTRY_COMPARE_WITH_TIMEOUT(opened.count(),1,10000);
        auto *source=window.findChild<QLineEdit *>("source"); QTRY_VERIFY_WITH_TIMEOUT(source->isEnabled(),5000);
        QVERIFY(!window.close()); QTRY_VERIFY_WITH_TIMEOUT(proxy.closeReply.socket,3000); QVERIFY(!source->isEnabled());
        int completedCloses=0;
        connect(window.findChild<RestClient *>()->findChild<QNetworkAccessManager *>(),&QNetworkAccessManager::finished,&window,[&](QNetworkReply *reply) {
            if(reply->url().path().endsWith("/close")) ++completedCloses;
        });
        window.submit({{"action","edit"},{"cells",QJsonArray{QJsonObject{{"row",1},{"col",1},{"source","must not submit"}}}}});
        proxy.releaseClose(); QTRY_VERIFY_WITH_TIMEOUT(!window.isVisible(),3000);
        QCOMPARE(proxy.mutations,0); QCOMPARE(completedCloses,1); QVERIFY(proxy.closes>=1); QCOMPARE(proxy.metadataReads,0);
        if(failure=="disconnect") {
            const auto attempts=proxy.closes;
            QTcpSocket retry; retry.setProxy(QNetworkProxy::NoProxy); retry.connectToHost(QHostAddress::LocalHost,proxy.serverPort());
            QTRY_COMPARE_WITH_TIMEOUT(retry.state(),QAbstractSocket::ConnectedState,3000);
            retry.write("POST /v1/workbooks/retried/close HTTP/1.1\r\nHost: 127.0.0.1\r\nContent-Length: 2\r\nConnection: close\r\n\r\n{}");
            QTRY_VERIFY_WITH_TIMEOUT(proxy.closes>attempts,3000);
            QTRY_COMPARE_WITH_TIMEOUT(retry.state(),QAbstractSocket::UnconnectedState,3000);
            QVERIFY(!proxy.closeReply.socket); QCOMPARE(completedCloses,1);
        }
        QCOMPARE(owner.findChild<QProcess *>("ownedService")->state(),QProcess::Running);
        QVERIFY(QDir(options.recoveryDirectory).entryList({"operation-*.json"},QDir::Files).isEmpty());
    }
    void releasedHandleCompletionCannotCloseANewerWorkbook() {
        QTemporaryDir temp; QJsonObject service; StartupOptions ownerOptions; ownerOptions.automaticWorkbook=false;
        MainWindow owner(ownerOptions); observeOwnedServiceStartup(owner,service); QSignalSpy serviceReady(&owner,&MainWindow::serviceReady);
        QTRY_COMPARE_WITH_TIMEOUT(serviceReady.count(),1,10000); QVERIFY(!service["token"].toString().isEmpty());
        DelayedCloseProxy proxy(service); QVERIFY(proxy.listen(QHostAddress::LocalHost,0));
        StartupOptions options; options.automaticWorkbook=false; options.url="http://127.0.0.1:"+QString::number(proxy.serverPort());
        options.token=service["token"].toString();
        MainWindow window(options); window.show(); QSignalSpy ready(&window,&MainWindow::serviceReady);
        QTRY_COMPARE_WITH_TIMEOUT(ready.count(),1,10000);
        QSignalSpy opened(&window,&MainWindow::workbookReady); window.openBook(temp.filePath("first-close-context.dhwb"),true);
        QTRY_COMPARE_WITH_TIMEOUT(opened.count(),1,10000);
        auto *source=window.findChild<QLineEdit *>("source"); QTRY_VERIFY_WITH_TIMEOUT(source->isEnabled(),5000);
        const auto first=window.model()->workbook(); proxy.holdOpen=true;
        window.openBook(temp.filePath("second-close-context.dhwb"),true); QTRY_VERIFY_WITH_TIMEOUT(proxy.openReply.socket,3000);
        QVERIFY(!window.close()); QTRY_VERIFY_WITH_TIMEOUT(proxy.closeReply.socket,3000); QCOMPARE(proxy.closedHandle,first);
        proxy.releaseOpen(); QTRY_COMPARE_WITH_TIMEOUT(opened.count(),2,3000);
        const auto second=window.model()->workbook(); QVERIFY(second!=first); QVERIFY(!source->isEnabled());
        proxy.releaseClose(); QTRY_VERIFY_WITH_TIMEOUT(source->isEnabled(),5000); QVERIFY(window.isVisible());
        QCOMPARE(window.model()->workbook(),second);
        QVERIFY(!window.close()); QTRY_VERIFY_WITH_TIMEOUT(proxy.closeReply.socket,3000); QCOMPARE(proxy.closedHandle,second);
        proxy.releaseClose(); QTRY_VERIFY_WITH_TIMEOUT(!window.isVisible(),3000); QCOMPARE(proxy.mutations,0);
    }
    void enterAdvancesAfterCommitAndSaveKeepsTheCurrentCell() {
        QTemporaryDir temp; StartupOptions options; options.automaticWorkbook=false;
        MainWindow window(options); window.show(); QSignalSpy ready(&window,&MainWindow::serviceReady);
        QTRY_COMPARE_WITH_TIMEOUT(ready.count(),1,10000);
        QSignalSpy opened(&window,&MainWindow::workbookReady); window.openBook(temp.filePath("enter.dhwb"),true);
        QTRY_COMPARE_WITH_TIMEOUT(opened.count(),1,10000);
        auto *model=window.model(); auto *grid=window.findChild<QTableView *>("grid"); auto *source=window.findChild<QLineEdit *>("source");
        QTRY_VERIFY_WITH_TIMEOUT(source->isEnabled(),5000);
        source->setFocus(); QTest::keyClicks(source,"7"); QTest::keyClick(source,Qt::Key_Return);
        QTRY_COMPARE_WITH_TIMEOUT(opened.count(),2,10000);
        QCOMPARE(grid->currentIndex(),model->index(1,0));
        QTRY_VERIFY_WITH_TIMEOUT(model->sourceReady(model->index(1,0)),5000);
        grid->edit(model->index(1,0)); QPointer<QLineEdit> editor=grid->findChild<QLineEdit *>(); QVERIFY(editor);
        QTest::keyClicks(editor,"8"); QTest::keyClick(editor,Qt::Key_Return);
        QTRY_COMPARE_WITH_TIMEOUT(opened.count(),3,10000);
        QCOMPARE(grid->currentIndex(),model->index(2,0)); QTRY_VERIFY_WITH_TIMEOUT(editor.isNull(),3000);
        QTRY_VERIFY_WITH_TIMEOUT(source->isEnabled(),5000);
        source->setFocus(); QTest::keyClicks(source,"9"); QTest::keyClick(source,Qt::Key_Return,Qt::ShiftModifier);
        QTRY_COMPARE_WITH_TIMEOUT(opened.count(),4,10000);
        QCOMPARE(grid->currentIndex(),model->index(1,0));
        QTRY_VERIFY_WITH_TIMEOUT(source->isEnabled(),5000);
        source->setFocus(); source->selectAll(); QTest::keyClicks(source,"10");
        window.findChild<QAction *>("saveAction")->trigger();
        QTRY_COMPARE_WITH_TIMEOUT(opened.count(),5,10000);
        QCOMPARE(grid->currentIndex(),model->index(1,0));
        QTRY_COMPARE_WITH_TIMEOUT(model->data(model->index(1,0)).toString(),QString("10"),5000);
        grid->setCurrentIndex(model->index(1048575,0)); QTRY_VERIFY_WITH_TIMEOUT(source->isEnabled(),5000);
        source->setFocus(); QTest::keyClicks(source,"11"); QTest::keyClick(source,Qt::Key_Return);
        QTRY_COMPARE_WITH_TIMEOUT(opened.count(),6,10000);
        QCOMPARE(grid->currentIndex(),model->index(1048575,0));
        const QJsonArray sort{QJsonObject{{"col",1},{"descending",true}}};
        window.showList({{"r1",1},{"r2",3},{"c1",1},{"c2",1},{"sort",sort}});
        QTRY_COMPARE_WITH_TIMEOUT(opened.count(),7,10000);
        QTRY_VERIFY_WITH_TIMEOUT(source->isEnabled(),5000); QCOMPARE(model->sourceRow(0),1);
        source->setFocus(); source->selectAll(); QTest::keyClicks(source,"6"); QTest::keyClick(source,Qt::Key_Return);
        QTRY_COMPARE_WITH_TIMEOUT(opened.count(),8,10000);
        QCOMPARE(model->sourceRow(grid->currentIndex().row()),2);
        QTRY_COMPARE_WITH_TIMEOUT(model->data(grid->currentIndex()).toString(),QString("9"),5000);
    }
    void rejectedEnterKeepsItsCellAndDraft() {
        QTemporaryDir temp; StartupOptions options; options.automaticWorkbook=false;
        options.handler=QString(DATAHORIZON_FIXTURES)+"/validation_hook.py";
        MainWindow window(options); window.show(); QSignalSpy ready(&window,&MainWindow::serviceReady);
        QTRY_COMPARE_WITH_TIMEOUT(ready.count(),1,10000);
        QSignalSpy opened(&window,&MainWindow::workbookReady); window.openBook(temp.filePath("enter-rejected.dhwb"),true);
        QTRY_COMPARE_WITH_TIMEOUT(opened.count(),1,10000);
        auto *model=window.model(); auto *grid=window.findChild<QTableView *>("grid"); auto *source=window.findChild<QLineEdit *>("source");
        QTRY_VERIFY_WITH_TIMEOUT(source->isEnabled(),5000);
        source->setFocus(); QTest::keyClicks(source,"-1"); QTest::keyClick(source,Qt::Key_Return);
        QTRY_COMPARE_WITH_TIMEOUT(opened.count(),2,10000);
        QCOMPARE(grid->currentIndex(),model->index(0,0)); QCOMPARE(source->text(),QString("-1")); QVERIFY(source->isModified());
        QTest::keyClick(source,Qt::Key_Escape);
        QTRY_VERIFY_WITH_TIMEOUT(model->sourceReady(model->index(0,0)),5000);
        grid->edit(model->index(0,0)); QPointer<QLineEdit> editor=grid->findChild<QLineEdit *>(); QVERIFY(editor);
        QTest::keyClicks(editor,"-2"); QTest::keyClick(editor,Qt::Key_Return);
        QTRY_COMPARE_WITH_TIMEOUT(opened.count(),3,10000);
        QCOMPARE(grid->currentIndex(),model->index(0,0)); QCOMPARE(source->text(),QString("-2")); QVERIFY(source->isModified());
    }
    void cancellingARangeActionPreservesTheActiveCellDraftBeforeMutation() {
        QTemporaryDir temp; StartupOptions options; options.automaticWorkbook=false;
        MainWindow window(options); window.show(); QSignalSpy ready(&window,&MainWindow::serviceReady);
        QTRY_COMPARE_WITH_TIMEOUT(ready.count(),1,10000);
        QSignalSpy opened(&window,&MainWindow::workbookReady); window.openBook(temp.filePath("range-draft.dhwb"),true);
        QTRY_COMPARE_WITH_TIMEOUT(opened.count(),1,10000);
        auto *model=window.model(); auto *grid=window.findChild<QTableView *>("grid"); auto *source=window.findChild<QLineEdit *>("source");
        QTRY_VERIFY_WITH_TIMEOUT(model->sourceReady(model->index(0,0)),5000);
        grid->edit(model->index(0,0)); QPointer<QLineEdit> editor=grid->findChild<QLineEdit *>(); QVERIFY(editor);
        QTest::keyClicks(editor,"retained input");
        QAction *bold=nullptr; for(auto *action:window.findChildren<QAction *>()) if(action->text()=="Bold") bold=action;
        QVERIFY(bold); QSignalSpy changed(&window,&MainWindow::operationFinished); QString question;
        answerNextDialog(QMessageBox::Cancel,&question); bold->trigger();
        QTRY_VERIFY_WITH_TIMEOUT(!question.isEmpty(),5000);
        QCOMPARE(changed.count(),0); QCOMPARE(opened.count(),1);
        QCOMPARE(source->text(),QString("retained input")); QVERIFY(source->isModified()); QVERIFY(source->isEnabled());
        QTRY_VERIFY_WITH_TIMEOUT(editor.isNull(),3000);
        QTest::keyClick(source,Qt::Key_Escape); bold->trigger();
        QTRY_COMPARE_WITH_TIMEOUT(changed.count(),1,10000); QTRY_COMPARE_WITH_TIMEOUT(opened.count(),2,10000);
        QTRY_VERIFY_WITH_TIMEOUT(model->loaded(model->index(0,0)),5000);
        QVERIFY(model->cell(model->index(0,0))["style"].toObject()["bold"].toBool());
        QCOMPARE(model->cell(model->index(0,0))["source"].toString(),QString());
    }
    void ownedServiceIgnoresAmbientPythonModules() {
        QTemporaryDir temp;
        const auto previous=QDir::currentPath(); const auto pythonpath=qgetenv("PYTHONPATH");
        const auto restore=qScopeGuard([previous,pythonpath]{
            QDir::setCurrent(previous);
            if(pythonpath.isNull()) qunsetenv("PYTHONPATH"); else qputenv("PYTHONPATH",pythonpath);
        });
        QVERIFY(QDir().mkpath(temp.filePath("datahorizon")));
        QFile package(temp.filePath("datahorizon/__init__.py")); QVERIFY(package.open(QIODevice::WriteOnly));
        package.write("from pathlib import Path\nPath(__file__).with_name('executed').write_text('untrusted import')\nraise RuntimeError('untrusted import')\n"); package.close();
        QFile module(temp.filePath("json.py")); QVERIFY(module.open(QIODevice::WriteOnly));
        module.write("from pathlib import Path\nPath(__file__).with_name('json-executed').write_text('untrusted import')\nraise RuntimeError('untrusted import')\n"); module.close();
        QVERIFY(QDir::setCurrent(temp.path())); qputenv("PYTHONPATH",(temp.path()+QDir::listSeparator()).toUtf8());
        StartupOptions options; options.automaticWorkbook=false; options.handler=QString(DATAHORIZON_FIXTURES)+"/validation_hook.py";
        MainWindow window(options); window.show(); QSignalSpy ready(&window,&MainWindow::serviceReady);
        QTRY_VERIFY_WITH_TIMEOUT(ready.count()==1 || QFile::exists(temp.filePath("datahorizon/executed")) || QFile::exists(temp.filePath("json-executed")),10000);
        QVERIFY(!QFile::exists(temp.filePath("datahorizon/executed"))); QVERIFY(!QFile::exists(temp.filePath("json-executed")));
        QCOMPARE(ready.count(),1);
        QSignalSpy opened(&window,&MainWindow::workbookReady); window.openBook(temp.filePath("trusted.dhwb"),true);
        QTRY_COMPARE_WITH_TIMEOUT(opened.count(),1,10000);
        window.submit({{"action","edit"},{"cells",QJsonArray{QJsonObject{{"row",1},{"col",1},{"source","7"}}}}});
        QTRY_COMPARE_WITH_TIMEOUT(opened.count(),2,10000);
        QTRY_COMPARE_WITH_TIMEOUT(window.model()->data(window.model()->index(0,1)).toString(),QString("14"),5000);
        QVERIFY(!QFile::exists(temp.filePath("datahorizon/executed"))); QVERIFY(!QFile::exists(temp.filePath("json-executed")));
    }
    void failedStartupCanClose() {
        const auto original=qgetenv("DATAHORIZON_PYTHON");
        qputenv("DATAHORIZON_PYTHON","/nonexistent/datahorizon-python");
        StartupOptions options; options.automaticWorkbook=false;
        MainWindow window(options); window.show();
        QTRY_VERIFY_WITH_TIMEOUT(window.findChild<QPlainTextEdit *>("operationIssues")->toPlainText().contains("startup failed"),5000);
        QVERIFY(window.close());
        if(original.isNull()) qunsetenv("DATAHORIZON_PYTHON"); else qputenv("DATAHORIZON_PYTHON",original);
    }
    void hiddenSectionsPersistMarkGapsAndGuardPaste() {
        QTemporaryDir temp; StartupOptions options; options.automaticWorkbook=false;
        options.viewStatePath=temp.filePath("views.ini"); const auto path=temp.filePath("hidden.dhwb");
        {
            MainWindow window(options); window.show();
            QSignalSpy ready(&window,&MainWindow::serviceReady); QTRY_COMPARE_WITH_TIMEOUT(ready.count(),1,10000);
            QSignalSpy opened(&window,&MainWindow::workbookReady); window.openBook(path,true);
            QTRY_COMPARE_WITH_TIMEOUT(opened.count(),1,10000);
            window.submit({{"action","edit"},{"cells",QJsonArray{
                QJsonObject{{"row",1},{"col",1},{"source","3"}},QJsonObject{{"row",2},{"col",1},{"source","1"}},
                QJsonObject{{"row",3},{"col",1},{"source","2"}}}}});
            QTRY_COMPARE_WITH_TIMEOUT(opened.count(),2,10000);
            auto *grid=window.findChild<QTableView *>("grid"); grid->setRowHeight(1,66); grid->setColumnWidth(1,234);
            QSignalSpy mutations(&window,&MainWindow::operationFinished);
            grid->selectRow(1); window.findChild<QAction *>("hideRowsAction")->trigger(); QVERIFY(grid->isRowHidden(1));
            grid->selectColumn(1); window.findChild<QAction *>("hideColumnsAction")->trigger(); QVERIFY(grid->isColumnHidden(1));
            QVERIFY(window.model()->headerData(0,Qt::Horizontal,Qt::DisplayRole).toString().contains("⋯"));
            QVERIFY(window.model()->headerData(0,Qt::Vertical,Qt::DisplayRole).toString().contains("⋯"));
            QVERIFY(!grid->isColumnHidden(grid->currentIndex().column()));
            auto *show=window.findChild<QPushButton *>("showHiddenButton"); QVERIFY(show->isVisible());
            grid->setCurrentIndex(window.model()->index(0,0));
            auto *saved=new QMimeData;
            if(const auto *mime=QApplication::clipboard()->mimeData()) for(const auto &format:mime->formats()) saved->setData(format,mime->data(format));
            QApplication::clipboard()->setText("7\n8\n9");
            window.findChild<QAction *>("pasteAction")->trigger(); QApplication::clipboard()->setMimeData(saved);
            QVERIFY(mutations.isEmpty());
            window.exportCsvTo(temp.filePath("blocked.csv"),{{"r1",2},{"c1",1}});
            QVERIFY(window.findChild<QPlainTextEdit *>("operationIssues")->toPlainText().contains("export rectangle"));
            QVERIFY(!QFile::exists(temp.filePath("blocked.csv")));
            QTimer cancelUnexpectedDialog; cancelUnexpectedDialog.setInterval(10);
            connect(&cancelUnexpectedDialog,&QTimer::timeout,[]{
                if(auto *box=qobject_cast<QMessageBox *>(QApplication::activeModalWidget())) if(auto *cancel=box->button(QMessageBox::Cancel)) cancel->click();
            });
            cancelUnexpectedDialog.start(); window.findChild<QAction *>("deleteRowsAction")->trigger(); cancelUnexpectedDialog.stop();
            QVERIFY(window.findChild<QPlainTextEdit *>("operationIssues")->toPlainText().contains("structural edits"));
            auto *address=window.findChild<QLineEdit *>("address"); address->setText("B2"); QTest::keyClick(address,Qt::Key_Return);
            QCOMPARE(grid->currentIndex(),window.model()->index(0,0));
            window.showList({{"r1",1},{"r2",3},{"c1",1},{"c2",1},{"header",false},
                {"sort",QJsonArray{QJsonObject{{"col",1},{"descending",false}}}}});
            QTRY_COMPARE_WITH_TIMEOUT(opened.count(),3,10000);
            QCOMPARE(window.model()->viewRow(1),0); QVERIFY(grid->isRowHidden(0));
            grid->selectionModel()->select(QItemSelection(window.model()->index(0,0),window.model()->index(2,0)),QItemSelectionModel::ClearAndSelect);
            window.findChild<QAction *>("hideRowsAction")->trigger(); QVERIFY(!grid->isRowHidden(1)); QVERIFY(mutations.isEmpty());
        }
        {
            MainWindow window(options); window.show();
            QSignalSpy ready(&window,&MainWindow::serviceReady); QTRY_COMPARE_WITH_TIMEOUT(ready.count(),1,10000);
            QSignalSpy opened(&window,&MainWindow::workbookReady); window.openBook(path,false,true);
            QTRY_COMPARE_WITH_TIMEOUT(opened.count(),1,10000);
            auto *grid=window.findChild<QTableView *>("grid"); QVERIFY(grid->isRowHidden(1)); QVERIFY(grid->isColumnHidden(1));
            window.findChild<QPushButton *>("showHiddenButton")->click();
            QVERIFY(!grid->isRowHidden(1)); QVERIFY(!grid->isColumnHidden(1));
            QCOMPARE(grid->rowHeight(1),66); QCOMPARE(grid->columnWidth(1),234);
            QTRY_COMPARE_WITH_TIMEOUT(window.model()->data(window.model()->index(1,0)).toString(),QString("1"),5000);
        }
    }
    void sheetDimensionsPersistAndFollowProjectedSourceRows() {
        QTemporaryDir temp; StartupOptions options; options.automaticWorkbook=false;
        options.viewStatePath=temp.filePath("views.ini"); const auto path=temp.filePath("sizes.dhwb");
        {
            MainWindow window(options); window.show();
            QSignalSpy ready(&window,&MainWindow::serviceReady); QTRY_COMPARE_WITH_TIMEOUT(ready.count(),1,10000);
            QSignalSpy opened(&window,&MainWindow::workbookReady); window.openBook(path,true);
            QTRY_COMPARE_WITH_TIMEOUT(opened.count(),1,10000);
            window.submit({{"action","edit"},{"cells",QJsonArray{
                QJsonObject{{"row",1},{"col",1},{"source","3"}},QJsonObject{{"row",2},{"col",1},{"source","1"}},
                QJsonObject{{"row",3},{"col",1},{"source","2"}}}}});
            QTRY_COMPARE_WITH_TIMEOUT(opened.count(),2,10000);
            auto *grid=window.findChild<QTableView *>("grid"); grid->setColumnWidth(0,234); grid->setRowHeight(2,86);
            window.showList({{"r1",1},{"r2",3},{"c1",1},{"c2",1},{"header",false},
                {"sort",QJsonArray{QJsonObject{{"col",1},{"descending",false}}}}});
            QTRY_COMPARE_WITH_TIMEOUT(opened.count(),3,10000);
            QCOMPARE(window.model()->sourceRow(1),2); QCOMPARE(grid->rowHeight(1),86); QCOMPARE(grid->columnWidth(0),234);
            QTRY_VERIFY_WITH_TIMEOUT(window.model()->loaded(window.model()->index(0,0)),5000);
            QSignalSpy mutations(&window,&MainWindow::operationFinished);
            grid->selectColumn(0); window.findChild<QAction *>("fitColumnsAction")->trigger();
            QVERIFY(grid->columnWidth(0)<234); QVERIFY(mutations.isEmpty());
            grid->setColumnWidth(0,234);
            window.submit({{"action","sheet_add"},{"name","Other"}});
            QTRY_COMPARE_WITH_TIMEOUT(opened.count(),4,10000);
            auto *tabs=window.findChild<QTabBar *>("sheets"); tabs->setCurrentIndex(1);
            QCOMPARE(grid->columnWidth(0),110); QCOMPARE(grid->rowHeight(2),26);
            tabs->setCurrentIndex(0);
            QCOMPARE(grid->columnWidth(0),234); QCOMPARE(grid->rowHeight(2),86);
        }
        {
            QSettings preferences(options.viewStatePath,QSettings::IniFormat);
            const auto key=preferences.allKeys().first();
            auto state=QJsonDocument::fromJson(preferences.value(key).toByteArray()).object();
            auto rows=state["rows"].toObject(); rows["-1"]=50; rows["1048576"]=50; rows["4"]=32.5;
            state["rows"]=rows; preferences.setValue(key,QJsonDocument(state).toJson(QJsonDocument::Compact));
        }
        {
            MainWindow window(options); window.show();
            QSignalSpy ready(&window,&MainWindow::serviceReady); QTRY_COMPARE_WITH_TIMEOUT(ready.count(),1,10000);
            QSignalSpy opened(&window,&MainWindow::workbookReady); window.openBook(path,false,true);
            QTRY_COMPARE_WITH_TIMEOUT(opened.count(),1,10000);
            auto *grid=window.findChild<QTableView *>("grid");
            QCOMPARE(grid->rowHeight(2),86); QCOMPARE(grid->rowHeight(1),26); QCOMPARE(grid->columnWidth(0),234);
            QCOMPARE(grid->rowHeight(4),26);
            grid->selectRow(2);
            QTimer::singleShot(20,[] { if(auto *dialog=qobject_cast<QInputDialog *>(QApplication::activeModalWidget())) {dialog->setIntValue(77); dialog->accept();} });
            window.findChild<QAction *>("rowHeightAction")->trigger(); QCOMPARE(grid->rowHeight(2),77);
            window.findChild<QAction *>("resetDimensionsAction")->trigger();
            QCOMPARE(grid->rowHeight(2),26); QCOMPARE(grid->columnWidth(0),110);
        }
    }
    void cellBordersPaintAndClearWithoutChangingInput() {
        QTemporaryDir temp; StartupOptions options; options.automaticWorkbook=false;
        MainWindow window(options); window.show();
        QSignalSpy ready(&window,&MainWindow::serviceReady); QTRY_COMPARE_WITH_TIMEOUT(ready.count(),1,10000);
        QSignalSpy opened(&window,&MainWindow::workbookReady); window.openBook(temp.filePath("border.dhwb"),true);
        QTRY_COMPARE_WITH_TIMEOUT(opened.count(),1,10000);
        auto *grid=window.findChild<QTableView *>("grid"); auto *model=window.model();
        const auto index=model->index(0,0); QTRY_VERIFY_WITH_TIMEOUT(model->loaded(index),5000);
        auto render=[&]{
            QImage image(120,52,QImage::Format_ARGB32); image.fill(Qt::white); QPainter painter(&image);
            QStyleOptionViewItem option; option.initFrom(grid); option.rect=image.rect(); option.state=QStyle::State_Enabled;
            grid->itemDelegate()->paint(&painter,option,index); return image;
        };
        const auto before=render();
        window.findChild<QAction *>("allBordersAction")->trigger();
        QTRY_COMPARE_WITH_TIMEOUT(opened.count(),2,10000); QTRY_VERIFY_WITH_TIMEOUT(model->loaded(index),5000);
        const auto bordered=render(); QVERIFY(bordered.pixelColor(1,20)!=before.pixelColor(1,20));
        QCOMPARE(model->data(index,Qt::EditRole).toString(),QString());
        window.findChild<QAction *>("clearBordersAction")->trigger();
        QTRY_COMPARE_WITH_TIMEOUT(opened.count(),3,10000); QTRY_VERIFY_WITH_TIMEOUT(model->loaded(index),5000);
        QCOMPARE(render().pixelColor(1,20),before.pixelColor(1,20));
    }
    void borderDialogPreservesOtherEdgesAndPresetsClearOverrides() {
        QTemporaryDir temp;StartupOptions options;options.automaticWorkbook=false;MainWindow window(options);window.show();QSignalSpy ready(&window,&MainWindow::serviceReady);QTRY_COMPARE_WITH_TIMEOUT(ready.count(),1,10000);QSignalSpy opened(&window,&MainWindow::workbookReady);window.openBook(temp.filePath("edges.dhwb"),true);QTRY_COMPARE_WITH_TIMEOUT(opened.count(),1,10000);window.submit({{"action","edit"},{"cells",QJsonArray{QJsonObject{{"row",1},{"col",1},{"source","00123"},{"kind","text"},{"style",QJsonObject{{"border","box"},{"border_top","dotted"}}}}}}});QTRY_COMPARE_WITH_TIMEOUT(opened.count(),2,10000);auto *model=window.model();auto *grid=window.findChild<QTableView *>("grid");grid->setCurrentIndex(model->index(0,0));QTimer timer;connect(&timer,&QTimer::timeout,&window,[&]{if(auto *d=window.findChild<QDialog *>("formatCellsDialog"))if(!d->property("checked").toBool()){d->setProperty("checked",true);d->findChild<QTabWidget *>("formatTabs")->setCurrentIndex(4);auto *left=d->findChild<QComboBox *>("border_left");left->setCurrentIndex(left->findData("medium"));auto *bottom=d->findChild<QComboBox *>("border_bottom");bottom->setCurrentIndex(bottom->findData("dashed"));d->grab().save("build/border-cells-"+QGuiApplication::platformName()+".png");d->accept();}});timer.start(10);window.findChild<QAction *>("formatCellsAction")->trigger();QTRY_COMPARE_WITH_TIMEOUT(opened.count(),3,10000);QTRY_VERIFY_WITH_TIMEOUT(model->loaded(model->index(0,0)),5000);auto style=model->cell(model->index(0,0))["style"].toObject();QCOMPARE(style["border_top"].toString(),QString("dotted"));QCOMPARE(style["border_left"].toString(),QString("medium"));QCOMPARE(model->data(model->index(0,0)).toString(),QString("00123"));window.findChild<QAction *>("clearBordersAction")->trigger();QTRY_COMPARE_WITH_TIMEOUT(opened.count(),4,10000);QTRY_VERIFY_WITH_TIMEOUT(model->loaded(model->index(0,0)),5000);style=model->cell(model->index(0,0))["style"].toObject();QCOMPARE(style["border"].toString(),QString("none"));QVERIFY(!style.contains("border_top"));QVERIFY(!style.contains("border_left"));window.submit({{"action","undo"}});QTRY_COMPARE_WITH_TIMEOUT(opened.count(),5,10000);QTRY_VERIFY_WITH_TIMEOUT(model->loaded(model->index(0,0)),5000);QCOMPARE(model->cell(model->index(0,0))["style"].toObject()["border_left"].toString(),QString("medium"));
    }
    void borderPaintingUsesIndividualEdgesWidthsAndPatterns() {
        auto render=[](const QJsonObject &style){QImage image(120,52,QImage::Format_ARGB32);image.fill(Qt::white);QPainter p(&image);paintCellBorders(&p,image.rect(),style,Qt::black);return image;};const auto left=render({{"border_left","thin"}});QCOMPARE(left.pixelColor(1,20),QColor(Qt::black));QCOMPARE(left.pixelColor(118,20),QColor(Qt::white));const auto thick=render({{"border_left","thick"}});QCOMPARE(thick.pixelColor(3,20),QColor(Qt::black));QCOMPARE(left.pixelColor(3,20),QColor(Qt::white));const auto dash=render({{"border_top","dashed"}}),dot=render({{"border_top","dotted"}});QVERIFY(dash!=dot);QVERIFY(dash!=render({{"border_top","thin"}}));
    }
    void wrappedTextRetainsInputAndFitsVisibleRows() {
        QTemporaryDir temp; StartupOptions options; options.automaticWorkbook=false;
        MainWindow window(options); window.show();
        QSignalSpy ready(&window,&MainWindow::serviceReady); QTRY_COMPARE_WITH_TIMEOUT(ready.count(),1,10000);
        QSignalSpy opened(&window,&MainWindow::workbookReady); window.openBook(temp.filePath("wrap.dhwb"),true);
        QTRY_COMPARE_WITH_TIMEOUT(opened.count(),1,10000);
        const QString text="A long text value that should occupy several lines in a narrow spreadsheet column";
        window.submit({{"action","edit"},{"cells",QJsonArray{QJsonObject{{"row",1},{"col",1},{"source",text},{"kind","text"}}}}});
        QTRY_COMPARE_WITH_TIMEOUT(opened.count(),2,10000);
        auto *grid=window.findChild<QTableView *>("grid"); auto *model=window.model();
        QTRY_VERIFY_WITH_TIMEOUT(model->loaded(model->index(0,0)),5000);
        auto *wrap=window.findChild<QAction *>("wrapTextAction"); QVERIFY(wrap); wrap->trigger();
        QTRY_COMPARE_WITH_TIMEOUT(opened.count(),3,10000);
        QTRY_VERIFY_WITH_TIMEOUT(grid->rowHeight(0)>26,5000);
        QCOMPARE(model->data(model->index(0,0),Qt::EditRole).toString(),text);
        grid->scrollTo(model->index(10000,0),QAbstractItemView::PositionAtTop);
        QTRY_COMPARE_WITH_TIMEOUT(grid->rowHeight(0),26,5000);
        grid->scrollTo(model->index(0,0),QAbstractItemView::PositionAtTop);
        QTRY_VERIFY_WITH_TIMEOUT(grid->rowHeight(0)>26,5000);
        const int narrowHeight=grid->rowHeight(0); grid->setColumnWidth(0,500);
        QTRY_VERIFY_WITH_TIMEOUT(grid->rowHeight(0)<narrowHeight,5000);
        grid->setRowHeight(0,91); grid->setColumnWidth(0,110); QTest::qWait(30);
        QCOMPARE(grid->rowHeight(0),91);
        wrap->trigger(); QTRY_COMPARE_WITH_TIMEOUT(opened.count(),4,10000);
        QCOMPARE(grid->rowHeight(0),91);
        window.findChild<QAction *>("resetDimensionsAction")->trigger();
        QTRY_COMPARE_WITH_TIMEOUT(grid->rowHeight(0),26,5000);
        QTRY_COMPARE_WITH_TIMEOUT(model->data(model->index(0,0),Qt::EditRole).toString(),text,5000);
    }
    void sourceEditorsPreserveLongInputs() {
        QTemporaryDir temp; StartupOptions options; options.automaticWorkbook=false;
        MainWindow window(options); window.show();
        QSignalSpy ready(&window,&MainWindow::serviceReady); QTRY_COMPARE_WITH_TIMEOUT(ready.count(),1,10000);
        QSignalSpy opened(&window,&MainWindow::workbookReady); window.openBook(temp.filePath("long.dhwb"),true);
        QTRY_COMPARE_WITH_TIMEOUT(opened.count(),1,10000);
        const QString original(40000,'x');
        QSignalSpy changed(&window,&MainWindow::operationFinished);
        window.submit({{"action","edit"},{"cells",QJsonArray{QJsonObject{{"row",1},{"col",1},{"source",original}}}}});
        QTRY_COMPARE_WITH_TIMEOUT(opened.count(),2,10000);
        auto *model=window.model(); auto *grid=window.findChild<QTableView *>("grid"); auto *source=window.findChild<QLineEdit *>("source");
        QTRY_COMPARE_WITH_TIMEOUT(model->data(model->index(0,0),Qt::EditRole).toString(),original,5000);
        QTRY_COMPARE_WITH_TIMEOUT(source->text(),original,5000);
        grid->edit(model->index(0,0)); QPointer<QLineEdit> editor=grid->findChild<QLineEdit *>(); QVERIFY(editor);
        QCOMPARE(editor->text(),original);
        QCOMPARE(editor->accessibleName(),QString("Cell A1 input"));
        editor->setCursorPosition(editor->text().size()); editor->insert("z"); QTest::keyClick(editor,Qt::Key_Return);
        QTRY_COMPARE_WITH_TIMEOUT(opened.count(),3,10000);
        QTRY_VERIFY_WITH_TIMEOUT(editor.isNull(),3000); grid->setCurrentIndex(model->index(0,0));
        QTRY_COMPARE_WITH_TIMEOUT(source->text(),original+"z",5000);
        source->setModified(true); QTest::keyClick(source,Qt::Key_Return);
        QTRY_COMPARE_WITH_TIMEOUT(changed.count(),3,10000);
        QTRY_COMPARE_WITH_TIMEOUT(opened.count(),4,10000);
        QTRY_COMPARE_WITH_TIMEOUT(model->data(model->index(0,0),Qt::EditRole).toString(),original+"z",5000);
    }
    void draftsRemainBoundToTheirOriginalCellAndSheet() {
        QTemporaryDir temp; StartupOptions options; options.automaticWorkbook=false;
        MainWindow window(options); window.show();
        QSignalSpy ready(&window,&MainWindow::serviceReady); QTRY_COMPARE_WITH_TIMEOUT(ready.count(),1,10000);
        QSignalSpy opened(&window,&MainWindow::workbookReady); window.openBook(temp.filePath("draft.dhwb"),true);
        QTRY_COMPARE_WITH_TIMEOUT(opened.count(),1,10000);
        window.submit({{"action","sheet_add"},{"name","Other"}}); QTRY_COMPARE_WITH_TIMEOUT(opened.count(),2,10000);
        auto *model=window.model(); auto *grid=window.findChild<QTableView *>("grid"); auto *source=window.findChild<QLineEdit *>("source");
        auto *tabs=window.findChild<QTabBar *>("sheets");
        QTRY_VERIFY_WITH_TIMEOUT(model->loaded(model->index(0,0)),5000);
        source->setFocus(); QTest::keyClicks(source,"draft"); grid->setFocus(); model->tileLoaded();
        QCOMPARE(source->text(),QString("draft")); QVERIFY(source->isModified());
        answerNextDialog(QMessageBox::Cancel); grid->setCurrentIndex(model->index(0,1));
        QCOMPARE(grid->currentIndex(),model->index(0,0)); QCOMPARE(source->text(),QString("draft"));
        answerNextDialog(QMessageBox::Cancel); tabs->setCurrentIndex(1);
        QCOMPARE(tabs->currentIndex(),0); QCOMPARE(source->text(),QString("draft"));
        answerNextDialog(QMessageBox::Cancel);
        for (auto *action:window.findChildren<QAction *>()) if (action->text().startsWith("Sort / filter selected list")) action->trigger();
        QCOMPARE(source->text(),QString("draft")); QVERIFY(source->isModified());
        answerNextDialog(QMessageBox::Save); grid->setCurrentIndex(model->index(0,1));
        QTRY_COMPARE_WITH_TIMEOUT(opened.count(),3,10000);
        QTRY_COMPARE_WITH_TIMEOUT(model->data(model->index(0,0)).toString(),QString("draft"),5000);
        QCOMPARE(model->data(model->index(0,1)).toString(),QString());
    }
    void sheetRenameDialogRetainsIdentityAndUpdatesFormulaReferences() {
        QTemporaryDir temp; StartupOptions options; options.automaticWorkbook=false;
        MainWindow window(options); window.show(); QSignalSpy ready(&window,&MainWindow::serviceReady);
        QTRY_COMPARE_WITH_TIMEOUT(ready.count(),1,10000);
        QSignalSpy opened(&window,&MainWindow::workbookReady); window.openBook(temp.filePath("rename.dhwb"),true);
        QTRY_COMPARE_WITH_TIMEOUT(opened.count(),1,10000);
        const auto id=window.model()->sheet();
        window.submit({{"action","edit"},{"cells",QJsonArray{
            QJsonObject{{"row",1},{"col",1},{"source","=Sheet1!A2"}},
            QJsonObject{{"row",2},{"col",1},{"source","7"}}}}});
        QTRY_COMPARE_WITH_TIMEOUT(opened.count(),2,10000);
        QTimer answer; answer.setInterval(10);
        connect(&answer,&QTimer::timeout,&window,[&]{
            if(auto *dialog=qobject_cast<QInputDialog *>(QApplication::activeModalWidget())) {
                dialog->setTextValue("O'Brien"); dialog->accept();
            }
        });
        answer.start(); window.findChild<QAction *>("renameSheetAction")->trigger(); answer.stop();
        QTRY_COMPARE_WITH_TIMEOUT(opened.count(),3,10000);
        QCOMPARE(window.model()->sheet(),id);
        QTRY_COMPARE_WITH_TIMEOUT(window.model()->cell(window.model()->index(0,0))["source"].toString(),QString("='O''Brien'!A2"),5000);
        QTRY_COMPARE_WITH_TIMEOUT(window.model()->data(window.model()->index(0,0)).toString(),QString("7"),5000);
    }
    void sheetDuplicateDialogCopiesSelfReferencesWithoutChangingSource() {
        QTemporaryDir temp; StartupOptions options; options.automaticWorkbook=false;
        MainWindow window(options); window.show(); QSignalSpy ready(&window,&MainWindow::serviceReady);
        QTRY_COMPARE_WITH_TIMEOUT(ready.count(),1,10000);
        QSignalSpy opened(&window,&MainWindow::workbookReady); window.openBook(temp.filePath("duplicate.dhwb"),true);
        QTRY_COMPARE_WITH_TIMEOUT(opened.count(),1,10000);
        const auto original=window.model()->sheet();
        window.submit({{"action","edit"},{"cells",QJsonArray{
            QJsonObject{{"row",1},{"col",1},{"source","=Sheet1!A2"}},
            QJsonObject{{"row",2},{"col",1},{"source","9"}}}}});
        QTRY_COMPARE_WITH_TIMEOUT(opened.count(),2,10000);
        QTimer answer; answer.setInterval(10);
        connect(&answer,&QTimer::timeout,&window,[&]{
            if(auto *dialog=qobject_cast<QInputDialog *>(QApplication::activeModalWidget())) {
                dialog->setTextValue("Copy"); dialog->accept();
            }
        });
        answer.start(); window.findChild<QAction *>("duplicateSheetAction")->trigger(); answer.stop();
        QTRY_COMPARE_WITH_TIMEOUT(opened.count(),3,10000);
        auto *tabs=window.findChild<QTabBar *>("sheets"); QCOMPARE(tabs->count(),2); tabs->setCurrentIndex(1);
        QVERIFY(window.model()->sheet()!=original);
        QTRY_COMPARE_WITH_TIMEOUT(window.model()->cell(window.model()->index(0,0))["source"].toString(),QString("='Copy'!A2"),5000);
        QTRY_COMPARE_WITH_TIMEOUT(window.model()->data(window.model()->index(0,0)).toString(),QString("9"),5000);
    }
    void sheetDeleteDialogWarnsAndReportsBrokenReferences() {
        QTemporaryDir temp; StartupOptions options; options.automaticWorkbook=false;
        MainWindow window(options); window.show(); QSignalSpy ready(&window,&MainWindow::serviceReady);
        QTRY_COMPARE_WITH_TIMEOUT(ready.count(),1,10000);
        QSignalSpy opened(&window,&MainWindow::workbookReady); window.openBook(temp.filePath("delete.dhwb"),true);
        QTRY_COMPARE_WITH_TIMEOUT(opened.count(),1,10000);
        window.submit({{"action","sheet_add"},{"name","Other"}});
        QTRY_COMPARE_WITH_TIMEOUT(opened.count(),2,10000);
        auto *tabs=window.findChild<QTabBar *>("sheets"); tabs->setCurrentIndex(1);
        window.submit({{"action","edit"},{"cells",QJsonArray{QJsonObject{{"row",1},{"col",1},{"source","=Sheet1!A2"}}}}});
        QTRY_COMPARE_WITH_TIMEOUT(opened.count(),3,10000);
        tabs->setCurrentIndex(0);
        bool warned=false; QTimer answer; answer.setInterval(10);
        connect(&answer,&QTimer::timeout,&window,[&]{
            if(auto *box=qobject_cast<QMessageBox *>(QApplication::activeModalWidget())) {
                warned=box->text().contains("#REF!") && box->text().contains("Undo");
                box->button(QMessageBox::Yes)->click();
            }
        });
        answer.start(); window.findChild<QAction *>("deleteSheetAction")->trigger(); answer.stop();
        QVERIFY(warned); QTRY_COMPARE_WITH_TIMEOUT(opened.count(),4,10000);
        QCOMPARE(tabs->count(),1);
        QTRY_COMPARE_WITH_TIMEOUT(window.model()->data(window.model()->index(0,0)).toString(),QString("#REF!"),5000);
        QVERIFY(window.findChild<QPlainTextEdit *>("operationIssues")->toPlainText().contains("Other!A1"));
    }
    void formatCellsReadOnlyGuardDoesNotOpenAnEditor() {
        QTemporaryDir temp;StartupOptions options;options.automaticWorkbook=false;const auto path=temp.filePath("readonly-format.dhwb");
        {MainWindow window(options);window.show();QSignalSpy ready(&window,&MainWindow::serviceReady);QTRY_COMPARE_WITH_TIMEOUT(ready.count(),1,10000);QSignalSpy opened(&window,&MainWindow::workbookReady);window.openBook(path,true);QTRY_COMPARE_WITH_TIMEOUT(opened.count(),1,10000);}
        MainWindow window(options);window.show();QSignalSpy ready(&window,&MainWindow::serviceReady);QTRY_COMPARE_WITH_TIMEOUT(ready.count(),1,10000);QSignalSpy opened(&window,&MainWindow::workbookReady);window.openBook(path,false,true);QTRY_COMPARE_WITH_TIMEOUT(opened.count(),1,10000);QSignalSpy edits(&window,&MainWindow::operationFinished);window.findChild<QAction *>("formatPainterAction")->trigger();QVERIFY(!window.findChild<QAction *>("formatPainterAction")->isChecked());window.findChild<QAction *>("formatCellsAction")->trigger();QVERIFY(!window.findChild<QDialog *>("formatCellsDialog"));QVERIFY(window.findChild<QPlainTextEdit *>("operationIssues")->toPlainText().contains("read-only"));QVERIFY(edits.isEmpty());
    }
    void formatCellsDialogPatchesOnlyChosenPropertiesAndValidatesColors() {
        FormatCellsDialog dialog("A1:B2");QVERIFY(dialog.patch().isEmpty());QVERIFY(!dialog.replaceStyle());auto *fill=dialog.findChild<QLineEdit *>("cellBackground");fill->setText("red");QVERIFY(!dialog.findChild<QDialogButtonBox *>()->button(QDialogButtonBox::Ok)->isEnabled());fill->setText("#AbCdEf");QVERIFY(dialog.findChild<QDialogButtonBox *>()->button(QDialogButtonBox::Ok)->isEnabled());dialog.findChild<QCheckBox *>("fontBold")->setChecked(true);dialog.findChild<QCheckBox *>("fontItalic")->setChecked(false);auto *number=dialog.findChild<QComboBox *>("numberFormat");number->setCurrentIndex(number->findData("number"));QCOMPARE(dialog.patch(),(QJsonObject{{"background","#abcdef"},{"bold",true},{"italic",false},{"format","number"}}));dialog.findChild<QCheckBox *>("resetAllFormats")->setChecked(true);QVERIFY(dialog.replaceStyle());
    }
    void operationJobReviewStagesBeforeWritingAndReportsCommittedBatches() {
        QTemporaryDir temp;QFile csv(temp.filePath("inputs.csv"));QVERIFY(csv.open(QIODevice::WriteOnly));csv.write("00123\n2024-02-29\n");csv.close();StartupOptions options;options.automaticWorkbook=false;MainWindow window(options);window.show();QSignalSpy ready(&window,&MainWindow::serviceReady);QTRY_COMPARE_WITH_TIMEOUT(ready.count(),1,10000);QSignalSpy opened(&window,&MainWindow::workbookReady);window.openBook(temp.filePath("jobs.dhwb"),true);QTRY_COMPARE_WITH_TIMEOUT(opened.count(),1,10000);auto *grid=window.findChild<QTableView *>("grid");grid->setCurrentIndex(window.model()->index(0,0));QTRY_VERIFY_WITH_TIMEOUT(window.model()->loaded(window.model()->index(0,0)),5000);
        int phase=0;bool stagedWithoutWrites=false,committed=false;QTimer timer;connect(&timer,&QTimer::timeout,&window,[&]{
            if(auto *box=qobject_cast<QMessageBox *>(QApplication::activeModalWidget())){if(auto *yes=box->button(QMessageBox::Yes))yes->click();return;}
            auto *dialog=window.findChild<QDialog *>("operationJobsDialog");if(!dialog)return;
            auto *report=dialog->findChild<QPlainTextEdit *>("operationJobReport");const auto value=QJsonDocument::fromJson(report->toPlainText().toUtf8()).object();
            if(phase==0){dialog->findChild<QLineEdit *>("operationJobSource")->setText(csv.fileName());phase=1;dialog->findChild<QPushButton *>("prepareOperationJob")->click();}
            else if(phase==1 && value["status"]=="prepared"){stagedWithoutWrites=window.model()->data(window.model()->index(0,0)).toString().isEmpty();dialog->grab().save("build/operation-jobs-"+QGuiApplication::platformName()+".png");phase=2;auto *button=dialog->findChild<QPushButton *>("startOperationJob");QTimer::singleShot(0,button,&QPushButton::click);}
            else if(phase==2 && value["status"]=="completed"){committed=value["committed_batches"].toInt()==1;phase=3;dialog->accept();}
            else if(value["status"]=="failed" || report->toPlainText().startsWith("Status unavailable")){phase=-1;dialog->accept();}
        });timer.start(10);window.findChild<QAction *>("operationJobsAction")->trigger();timer.stop();QCOMPARE(phase,3);QVERIFY(stagedWithoutWrites);QVERIFY(committed);QTRY_COMPARE_WITH_TIMEOUT(opened.count(),2,10000);QTRY_COMPARE_WITH_TIMEOUT(window.model()->data(window.model()->index(0,0)).toString(),QString("00123"),5000);QCOMPARE(window.model()->cell(window.model()->index(0,0))["input_kind"].toString(),QString("text"));
    }
    void localizedEntryReviewsConversionAndKeepsNativeNumber() {
        QTemporaryDir temp;StartupOptions options;options.automaticWorkbook=false;MainWindow window(options);window.show();QSignalSpy ready(&window,&MainWindow::serviceReady);QTRY_COMPARE_WITH_TIMEOUT(ready.count(),1,10000);QSignalSpy opened(&window,&MainWindow::workbookReady);window.openBook(temp.filePath("localized.dhwb"),true);QTRY_COMPARE_WITH_TIMEOUT(opened.count(),1,10000);auto *grid=window.findChild<QTableView *>("grid");grid->setCurrentIndex(window.model()->index(0,0));QTRY_VERIFY_WITH_TIMEOUT(window.model()->loaded(window.model()->index(0,0)),5000);QTimer timer;connect(&timer,&QTimer::timeout,&window,[&]{if(auto *dialog=window.findChild<QDialog *>("localizedEntryDialog");dialog && !dialog->property("checked").toBool()){dialog->setProperty("checked",true);dialog->findChild<QLineEdit *>("localizedSource")->setText("1.234,50");auto *profile=dialog->findChild<QComboBox *>("localizedProfile");profile->setCurrentIndex(profile->findData("de_DE"));dialog->accept();}if(auto *box=qobject_cast<QMessageBox *>(QApplication::activeModalWidget()))if(auto *yes=box->button(QMessageBox::Yes))yes->click();});timer.start(10);window.findChild<QAction *>("enterLocalizedAction")->trigger();QTRY_COMPARE_WITH_TIMEOUT(opened.count(),2,10000);timer.stop();QTRY_COMPARE_WITH_TIMEOUT(window.model()->data(window.model()->index(0,0)).toString(),QString("1234.5"),5000);QCOMPARE(window.model()->cell(window.model()->index(0,0))["input_kind"].toString(),QString("number"));
    }
    void declaredRecordClearUsesVisibleIdsAndRetainsFilteredOutInputs() {
        QTemporaryDir temp;StartupOptions options;options.automaticWorkbook=false;MainWindow window(options);window.show();QSignalSpy ready(&window,&MainWindow::serviceReady);QTRY_COMPARE_WITH_TIMEOUT(ready.count(),1,10000);QSignalSpy opened(&window,&MainWindow::workbookReady);window.openBook(temp.filePath("record-edit.dhwb"),true);QTRY_COMPARE_WITH_TIMEOUT(opened.count(),1,10000);QJsonArray cells;for(int row=1;row<=3;++row)cells.append(QJsonObject{{"row",row},{"col",1},{"source",row==2?"1":"4"}});window.submit({{"action","edit"},{"cells",cells}});QTRY_COMPARE_WITH_TIMEOUT(opened.count(),2,10000);const QJsonObject scope{{"r1",1},{"c1",1},{"r2",3},{"c2",1},{"header",false}};window.submit({{"action","list_declare"},{"id","Records"},{"spec",scope}});QTRY_COMPARE_WITH_TIMEOUT(opened.count(),3,10000);auto filtered=scope;filtered["filters"]=QJsonArray{QJsonObject{{"col",1},{"op",">"},{"value",1}}};window.showList(filtered);QTRY_COMPARE_WITH_TIMEOUT(window.model()->rowCount(),2,5000);QSignalSpy edits(&window,&MainWindow::operationFinished);
        QTimer timer;connect(&timer,&QTimer::timeout,&window,[&]{if(auto *dialog=window.findChild<QDialog *>("recordCommandDialog");dialog && !dialog->property("checked").toBool()){dialog->setProperty("checked",true);auto *mode=dialog->findChild<QComboBox *>("recordCommandMode");mode->setCurrentIndex(mode->findData("clear_contents"));dialog->findChild<QSpinBox *>("recordFirstColumn")->setValue(1);dialog->findChild<QSpinBox *>("recordLastColumn")->setValue(1);dialog->accept();}if(auto *box=qobject_cast<QMessageBox *>(QApplication::activeModalWidget()))if(auto *yes=box->button(QMessageBox::Yes))yes->click();});timer.start(10);window.findChild<QAction *>("recordCommandAction")->trigger();QTRY_COMPARE_WITH_TIMEOUT(edits.count(),1,10000);timer.stop();QTRY_COMPARE_WITH_TIMEOUT(window.model()->rowCount(),0,5000);window.showList(scope);QTRY_COMPARE_WITH_TIMEOUT(window.model()->rowCount(),3,5000);QTRY_COMPARE_WITH_TIMEOUT(window.model()->data(window.model()->index(1,0)).toString(),QString("1"),5000);
    }
    void portableViewSaveLoadUsesNativeMetadataAndRetainsInputs() {
        QTemporaryDir temp;StartupOptions options;options.automaticWorkbook=false;options.viewStatePath=temp.filePath("views.ini");MainWindow window(options);window.show();QSignalSpy ready(&window,&MainWindow::serviceReady);QTRY_COMPARE_WITH_TIMEOUT(ready.count(),1,10000);QSignalSpy opened(&window,&MainWindow::workbookReady);window.openBook(temp.filePath("portable.dhwb"),true);QTRY_COMPARE_WITH_TIMEOUT(opened.count(),1,10000);auto *grid=window.findChild<QTableView *>("grid");grid->setCurrentIndex(window.model()->index(0,0));QTRY_VERIFY_WITH_TIMEOUT(window.model()->loaded(window.model()->index(0,0)),5000);grid->setColumnWidth(0,180);window.findChild<QAction *>("savePortableViewAction")->trigger();QTRY_COMPARE_WITH_TIMEOUT(opened.count(),2,10000);grid->setColumnWidth(0,90);const auto state=window.model()->state();window.findChild<QAction *>("loadPortableViewAction")->trigger();QTRY_COMPARE_WITH_TIMEOUT(grid->columnWidth(0),180,5000);QCOMPARE(window.model()->state(),state);
    }
    void backgroundCalculationReportsSnapshotWithoutEdits() {
        QTemporaryDir temp;StartupOptions options;options.automaticWorkbook=false;MainWindow window(options);window.show();QSignalSpy ready(&window,&MainWindow::serviceReady);QTRY_COMPARE_WITH_TIMEOUT(ready.count(),1,10000);QSignalSpy opened(&window,&MainWindow::workbookReady);window.openBook(temp.filePath("calculation.dhwb"),true);QTRY_COMPARE_WITH_TIMEOUT(opened.count(),1,10000);window.submit({{"action","edit"},{"cells",QJsonArray{QJsonObject{{"row",1},{"col",1},{"source","=DATE(2024,2,29)"}}}}});QTRY_COMPARE_WITH_TIMEOUT(opened.count(),2,10000);auto *grid=window.findChild<QTableView *>("grid");grid->setCurrentIndex(window.model()->index(0,0));QTRY_VERIFY_WITH_TIMEOUT(window.model()->loaded(window.model()->index(0,0)),5000);const auto state=window.model()->state();QSignalSpy edits(&window,&MainWindow::operationFinished);window.findChild<QAction *>("calculateSelectionAction")->trigger();QTRY_VERIFY2_WITH_TIMEOUT(window.findChild<QLabel *>("storageStatus")->text().contains("Background snapshot"),qPrintable(window.findChild<QLabel *>("storageStatus")->text()+" | "+window.findChild<QPlainTextEdit *>("operationIssues")->toPlainText()),5000);QCOMPARE(window.model()->state(),state);QCOMPARE(edits.count(),0);
    }
    void temporalCsvCompanionPreviewRestoresMixedTypes() {
        QTemporaryDir temp;StartupOptions options;options.automaticWorkbook=false;MainWindow window(options);window.show();QSignalSpy ready(&window,&MainWindow::serviceReady);QTRY_COMPARE_WITH_TIMEOUT(ready.count(),1,10000);QSignalSpy opened(&window,&MainWindow::workbookReady);window.openBook(temp.filePath("csv-temporal.dhwb"),true);QTRY_COMPARE_WITH_TIMEOUT(opened.count(),1,10000);auto *grid=window.findChild<QTableView *>("grid");grid->setCurrentIndex(window.model()->index(0,0));QTRY_VERIFY_WITH_TIMEOUT(window.model()->loaded(window.model()->index(0,0)),5000);
        const QByteArray csv="2024-02-29\r\n2024-02-29\r\n";const QJsonObject entry{{"row",1},{"col",1},{"kind","date"}};const QJsonObject schema{{"version",1},{"sha256",QString::fromLatin1(QCryptographicHash::hash(csv,QCryptographicHash::Sha256).toHex())},{"cells",QJsonArray{entry}}};
        answerNextDialog(QMessageBox::Yes);QTimer::singleShot(5000,&window,[&]{for(auto *box:window.findChildren<QMessageBox *>())if(box->isVisible())box->reject();});window.importCsvBytes(csv,"utf-8",",",{},schema);QTRY_COMPARE_WITH_TIMEOUT(opened.count(),2,10000);QTRY_COMPARE_WITH_TIMEOUT(window.model()->cell(window.model()->index(0,0))["input_kind"].toString(),QString("date"),5000);QTRY_COMPARE_WITH_TIMEOUT(window.model()->cell(window.model()->index(1,0))["input_kind"].toString(),QString("text"),5000);
    }
    void temporalFilterDialogRetainsExplicitKind() {
        const QJsonObject filter{{"col",1},{"op",">="},{"value","2024-02-29"},{"value_kind","date"}};
        ListCriteriaDialog dialog(QJsonObject{{"r1",1},{"c1",1},{"r2",10},{"c2",1},{"filters",QJsonArray{filter}}});
        auto *table=dialog.findChild<QTableWidget *>("filterCriteria");QCOMPARE(qobject_cast<QComboBox *>(table->cellWidget(0,2))->currentIndex(),4);dialog.findChild<QDialogButtonBox *>()->button(QDialogButtonBox::Ok)->click();QCOMPARE(dialog.result(),int(QDialog::Accepted));QCOMPARE(dialog.scope()["filters"].toArray()[0].toObject()["value_kind"].toString(),QString("date"));QJsonObject normalized;QVERIFY(normalizedListView(dialog.scope(),normalized));
    }
    void dateFormatsDisableNumericControlsAndPatchOnlyPresentation() {
        FormatCellsDialog dialog("A1");auto *category=dialog.findChild<QComboBox *>("numberFormat");
        for(const auto &mode:QStringList{"date_iso","date_mdy","date_dmy","time24","datetime_iso"}){QVERIFY(category->findData(mode)>=0);category->setCurrentIndex(category->findData(mode));QVERIFY(!dialog.findChild<QSpinBox *>("decimalPlaces")->isEnabled());QVERIFY(!dialog.findChild<QCheckBox *>("numberGrouping")->isEnabled());QCOMPARE(dialog.patch(),(QJsonObject{{"format",mode}}));}
        dialog.show();QTest::qWait(20);dialog.grab().save("build/date-formats-"+QGuiApplication::platformName()+".png");
    }
    void temporalEntryUsesManagedTypesAndRejectsChangedTargets() {
        QTemporaryDir temp;StartupOptions options;options.automaticWorkbook=false;MainWindow window(options);window.show();QSignalSpy ready(&window,&MainWindow::serviceReady);QTRY_COMPARE_WITH_TIMEOUT(ready.count(),1,10000);QSignalSpy opened(&window,&MainWindow::workbookReady);window.openBook(temp.filePath("temporal.dhwb"),true);QTRY_COMPARE_WITH_TIMEOUT(opened.count(),1,10000);
        auto *grid=window.findChild<QTableView *>("grid");auto *model=window.model();grid->setCurrentIndex(model->index(0,0));QTRY_VERIFY_WITH_TIMEOUT(model->loaded(model->index(0,0)),5000);bool change=false;
        QTimer timer;connect(&timer,&QTimer::timeout,&window,[&]{auto *d=window.findChild<QDialog *>("temporalEntryDialog");if(!d || d->property("checked").toBool())return;d->setProperty("checked",true);d->findChild<QLineEdit *>("temporalSource")->setText("2024-02-29");if(change)grid->setCurrentIndex(model->index(1,1));d->accept();});timer.start(10);
        auto *action=window.findChild<QAction *>("enterTemporalAction");QVERIFY(action);action->trigger();QTRY_COMPARE_WITH_TIMEOUT(opened.count(),2,10000);QTRY_COMPARE_WITH_TIMEOUT(model->cell(model->index(0,0))["input_kind"].toString(),QString("date"),5000);
        model->setData(model->index(0,0),"2024-03-01",Qt::EditRole);QTRY_COMPARE_WITH_TIMEOUT(opened.count(),3,10000);QTRY_COMPARE_WITH_TIMEOUT(model->data(model->index(0,0)).toString(),QString("2024-03-01"),5000);QCOMPARE(model->cell(model->index(0,0))["input_kind"].toString(),QString("date"));
        const auto exported=temp.filePath("dates.csv");window.exportCsvTo(exported);QTRY_VERIFY_WITH_TIMEOUT(QFileInfo::exists(exported+".dh-types.json"),5000);QFile companion(exported+".dh-types.json");QVERIFY(companion.open(QIODevice::ReadOnly));QCOMPARE(QJsonDocument::fromJson(companion.readAll()).object()["cells"].toArray()[0].toObject()["kind"].toString(),QString("date"));
        const auto state=model->state();change=true;action->trigger();QCOMPARE(model->state(),state);QVERIFY(window.findChild<QPlainTextEdit *>("operationIssues")->toPlainText().contains("Date/time target changed"));
    }
    void formatCellsAppliesOneManagedEditKeepsInputsAndRejectsChangedScopes() {
        QTemporaryDir temp;StartupOptions options;options.automaticWorkbook=false;MainWindow window(options);window.show();QSignalSpy ready(&window,&MainWindow::serviceReady);QTRY_COMPARE_WITH_TIMEOUT(ready.count(),1,10000);QSignalSpy opened(&window,&MainWindow::workbookReady);window.openBook(temp.filePath("format-dialog.dhwb"),true);QTRY_COMPARE_WITH_TIMEOUT(opened.count(),1,10000);window.submit({{"action","edit"},{"cells",QJsonArray{QJsonObject{{"row",1},{"col",1},{"source","00123"},{"kind","text"},{"style",QJsonObject{{"italic",true},{"background","#ccddaa"}}}},QJsonObject{{"row",1},{"col",2},{"source","1.234"},{"style",QJsonObject{{"bold",false}}}},QJsonObject{{"row",2},{"col",1},{"source","=B1*2"}}}}});QTRY_COMPARE_WITH_TIMEOUT(opened.count(),2,10000);auto *grid=window.findChild<QTableView *>("grid");auto *model=window.model();grid->setCurrentIndex(model->index(0,0));grid->selectionModel()->select(QItemSelection(model->index(0,0),model->index(1,1)),QItemSelectionModel::ClearAndSelect);QSignalSpy edits(&window,&MainWindow::operationFinished);int mode=0,dialogs=0;
        QTimer timer;connect(&timer,&QTimer::timeout,&window,[&]{auto *dialog=window.findChild<QDialog *>("formatCellsDialog");if(!dialog || dialog->property("checked").toBool())return;dialog->setProperty("checked",true);++dialogs;if(mode==0){dialog->findChild<QCheckBox *>("fontBold")->setChecked(true);auto *number=dialog->findChild<QComboBox *>("numberFormat");number->setCurrentIndex(number->findData("number"));auto *tabs=dialog->findChild<QTabWidget *>("formatTabs");tabs->setCurrentIndex(3);dialog->findChild<QLineEdit *>("cellBackground")->setText("#ddeeff");dialog->grab().save("build/format-cells-"+QGuiApplication::platformName()+".png");}if(mode==1)dialog->reject();else {if(mode==2)dialog->findChild<QCheckBox *>("resetAllFormats")->setChecked(true);if(mode==3){grid->setCurrentIndex(model->index(2,2));grid->selectionModel()->select(model->index(2,2),QItemSelectionModel::ClearAndSelect);dialog->findChild<QCheckBox *>("fontBold")->setChecked(true);}dialog->findChild<QDialogButtonBox *>()->button(QDialogButtonBox::Ok)->click();}});timer.start(10);
        window.findChild<QAction *>("formatCellsAction")->trigger();QTRY_COMPARE_WITH_TIMEOUT(opened.count(),3,10000);QTRY_COMPARE_WITH_TIMEOUT(model->data(model->index(0,1)).toString(),QString("1.23"),5000);QCOMPARE(model->data(model->index(0,0)).toString(),QString("00123"));QVERIFY(model->cell(model->index(0,0))["style"].toObject()["italic"].toBool());QCOMPARE(model->cell(model->index(0,1))["source"].toString(),QString("1.234"));QCOMPARE(edits.count(),1);
        window.submit({{"action","undo"}});QTRY_COMPARE_WITH_TIMEOUT(opened.count(),4,10000);QTRY_COMPARE_WITH_TIMEOUT(model->cell(model->index(0,0))["style"].toObject()["background"].toString(),QString("#ccddaa"),5000);window.submit({{"action","redo"}});QTRY_COMPARE_WITH_TIMEOUT(opened.count(),5,10000);const auto state=model->state();mode=1;window.findChild<QAction *>("formatCellsAction")->trigger();QCOMPARE(model->state(),state);QCOMPARE(dialogs,2);
        grid->selectionModel()->select(QItemSelection(model->index(0,0),model->index(1,1)),QItemSelectionModel::ClearAndSelect);mode=2;window.findChild<QAction *>("formatCellsAction")->trigger();QTRY_COMPARE_WITH_TIMEOUT(opened.count(),6,10000);QTRY_VERIFY_WITH_TIMEOUT(model->loaded(model->index(0,1)),5000);QTRY_VERIFY_WITH_TIMEOUT(model->cell(model->index(0,1))["style"].toObject().isEmpty(),5000);QCOMPARE(model->cell(model->index(0,0))["source"].toString(),QString("00123"));QCOMPARE(model->cell(model->index(1,0))["source"].toString(),QString("=B1*2"));const int before=edits.count();mode=3;window.findChild<QAction *>("formatCellsAction")->trigger();QTest::qWait(50);QCOMPARE(edits.count(),before);QVERIFY(window.findChild<QPlainTextEdit *>("operationIssues")->toPlainText().contains("Formatting target changed"));
    }
    void formatPainterPatternValidatesBoundsAndRepeatsExactStyles() {
        const QJsonArray rows{QJsonArray{QJsonObject{{"font_size",16}},QJsonObject{}},QJsonArray{QJsonObject{{"format","text"}},QJsonObject{{"border_bottom","dashed"}}}};QVERIFY(validStylePattern(rows,2,2));QVERIFY(!validStylePattern(QJsonArray{QJsonArray{true}},1,1));QVERIFY(!validStylePattern(rows,1,2));const auto edits=formatPainterEdits(rows,3,3,6,6);QCOMPARE(edits.size(),16);QCOMPARE(edits[2].toObject()["style"],edits[0].toObject()["style"]);for(const auto &v:edits){const auto e=v.toObject();QVERIFY(e["replace_style"].toBool());QVERIFY(!e.contains("source"));QVERIFY(!e.contains("kind"));}QVERIFY(formatPainterEdits(rows,3,3,5,5).isEmpty());QVERIFY(formatPainterEdits(rows,1048576,16384,1048577,16385).isEmpty());QVERIFY(formatPainterEdits(rows,1,1,102,100).isEmpty());QJsonArray single;single.append(QJsonArray{QJsonValue(QJsonObject{})});QCOMPARE(formatPainterEdits(single,1048576,16384,1048576,16384).size(),1);
    }
    void formatPainterCopiesPatternsKeepsInputsClearsStylesAndUndoes() {
        QTemporaryDir temp;StartupOptions options;options.automaticWorkbook=false;MainWindow window(options);window.show();QSignalSpy ready(&window,&MainWindow::serviceReady);QTRY_COMPARE_WITH_TIMEOUT(ready.count(),1,10000);QSignalSpy opened(&window,&MainWindow::workbookReady);window.openBook(temp.filePath("painter.dhwb"),true);QTRY_COMPARE_WITH_TIMEOUT(opened.count(),1,10000);const QJsonObject a{{"format","text"},{"font_size",16},{"text_color","#123abc"}},b{{"border_bottom","dashed"},{"background","#ddeeff"}},c{{"bold",true}};window.submit({{"action","edit"},{"cells",QJsonArray{QJsonObject{{"row",1},{"col",1},{"source","00123"},{"kind","text"},{"style",a}},QJsonObject{{"row",1},{"col",2},{"source","3"},{"style",b}},QJsonObject{{"row",2},{"col",1},{"source","=1/0"},{"style",c}},QJsonObject{{"row",4},{"col",4},{"source","=2+3"},{"kind","formula"},{"style",QJsonObject{{"italic",true}}}},QJsonObject{{"row",4},{"col",5},{"source","00123"},{"kind","text"},{"style",QJsonObject{{"bold",true}}}},QJsonObject{{"row",5},{"col",4},{"source","7"}},QJsonObject{{"row",5},{"col",5},{"source","last"},{"kind","text"},{"style",QJsonObject{{"border","box"}}}}}}});QTRY_COMPARE_WITH_TIMEOUT(opened.count(),2,10000);auto *grid=window.findChild<QTableView *>("grid");auto *model=window.model();auto *painter=window.findChild<QAction *>("formatPainterAction");auto select=[&]{grid->setCurrentIndex(model->index(0,0));grid->selectionModel()->select(QItemSelection(model->index(0,0),model->index(1,1)),QItemSelectionModel::ClearAndSelect);};auto point=[](QTableView *v,int row,int col){return v->visualRect(v->model()->index(row,col)).center();};select();QSignalSpy edits(&window,&MainWindow::operationFinished);QApplication::clipboard()->setText("keep clipboard");painter->trigger();QTRY_VERIFY_WITH_TIMEOUT(painter->property("ready").toBool(),5000);window.grab().save("build/format-painter-"+QGuiApplication::platformName()+".png");QTest::mouseClick(grid->viewport(),Qt::LeftButton,Qt::NoModifier,point(grid,3,3));QTRY_COMPARE_WITH_TIMEOUT(opened.count(),3,10000);QTRY_VERIFY_WITH_TIMEOUT(model->loaded(model->index(4,4)),5000);QCOMPARE(model->cell(model->index(3,3))["style"].toObject(),a);QCOMPARE(model->cell(model->index(3,4))["style"].toObject(),b);QCOMPARE(model->cell(model->index(4,3))["style"].toObject(),c);QVERIFY(model->cell(model->index(4,4))["style"].toObject().isEmpty());QCOMPARE(model->cell(model->index(3,3))["source"].toString(),QString("=2+3"));QCOMPARE(model->cell(model->index(3,3))["input_kind"].toString(),QString("formula"));QCOMPARE(model->data(model->index(3,4)).toString(),QString("00123"));QCOMPARE(edits.count(),1);QCOMPARE(QApplication::clipboard()->text(),QString("keep clipboard"));QVERIFY(!painter->isChecked());window.submit({{"action","undo"}});QTRY_COMPARE_WITH_TIMEOUT(opened.count(),4,10000);QTRY_VERIFY_WITH_TIMEOUT(model->loaded(model->index(4,4)),5000);QCOMPARE(model->cell(model->index(3,3))["style"].toObject(),(QJsonObject{{"italic",true}}));QCOMPARE(model->cell(model->index(4,4))["style"].toObject(),(QJsonObject{{"border","box"}}));
        select();painter->trigger();QTRY_VERIFY_WITH_TIMEOUT(painter->property("ready").toBool(),5000);QTest::mousePress(grid->viewport(),Qt::LeftButton,Qt::NoModifier,point(grid,3,3));QTest::mouseRelease(grid->viewport(),Qt::LeftButton,Qt::NoModifier,point(grid,6,6));QTRY_COMPARE_WITH_TIMEOUT(opened.count(),5,10000);QTRY_VERIFY_WITH_TIMEOUT(model->loaded(model->index(6,6)),5000);QCOMPARE(model->cell(model->index(5,5))["style"].toObject(),a);QCOMPARE(model->cell(model->index(6,6))["style"].toObject(),QJsonObject{});
        select();painter->trigger();QTRY_VERIFY_WITH_TIMEOUT(painter->property("ready").toBool(),5000);const int before=edits.count();QTest::mousePress(grid->viewport(),Qt::LeftButton,Qt::NoModifier,point(grid,3,3));QTest::mouseRelease(grid->viewport(),Qt::LeftButton,Qt::NoModifier,point(grid,5,5));QTest::qWait(50);QCOMPARE(edits.count(),before);QVERIFY(window.findChild<QPlainTextEdit *>("operationIssues")->toPlainText().contains("exact multiples"));
        select();painter->trigger();QTRY_VERIFY_WITH_TIMEOUT(painter->property("ready").toBool(),5000);QTest::keyClick(grid,Qt::Key_Escape);QVERIFY(!painter->isChecked());QCOMPARE(edits.count(),before);
        if(auto *dock=qobject_cast<QDockWidget *>(window.findChild<QPlainTextEdit *>("operationIssues")->parentWidget()))dock->hide();grid->setCurrentIndex(model->index(1,1));QTRY_VERIFY_WITH_TIMEOUT(model->sourceReady(grid->currentIndex()),5000);QTest::qWait(30);window.findChild<QAction *>("freezePanesAction")->trigger();auto *left=window.findChild<QTableView *>("frozenFirstColumn");QTRY_VERIFY_WITH_TIMEOUT(left->isVisible(),2000);select();painter->trigger();QTRY_VERIFY_WITH_TIMEOUT(painter->property("ready").toBool(),5000);QTest::mouseClick(left->viewport(),Qt::LeftButton,Qt::NoModifier,point(left,3,0));QTRY_COMPARE_WITH_TIMEOUT(opened.count(),6,10000);QTRY_VERIFY_WITH_TIMEOUT(model->loaded(model->index(3,0)),5000);QCOMPARE(model->cell(model->index(3,0))["style"].toObject(),a);
        window.submit({{"action","sheet_add"},{"name","Target"}});QTRY_COMPARE_WITH_TIMEOUT(opened.count(),7,10000);select();painter->trigger();QTRY_VERIFY_WITH_TIMEOUT(painter->property("ready").toBool(),5000);window.findChild<QTabBar *>("sheets")->setCurrentIndex(1);QVERIFY(painter->property("ready").toBool());QTest::mouseClick(grid->viewport(),Qt::LeftButton,Qt::NoModifier,point(grid,3,3));QTRY_COMPARE_WITH_TIMEOUT(opened.count(),8,10000);QTRY_VERIFY_WITH_TIMEOUT(model->loaded(model->index(4,4)),5000);QCOMPARE(model->cell(model->index(3,3))["style"].toObject(),a);QCOMPARE(model->cell(model->index(3,4))["style"].toObject(),b);
        window.findChild<QTabBar *>("sheets")->setCurrentIndex(0);grid->setCurrentIndex(model->index(6,3));grid->selectionModel()->select(model->index(6,3),QItemSelectionModel::ClearAndSelect);window.findChild<QAction *>("hideRowsAction")->trigger();QVERIFY(grid->isRowHidden(6));select();painter->trigger();QTRY_VERIFY_WITH_TIMEOUT(painter->property("ready").toBool(),5000);const int hiddenBefore=edits.count();QTest::mouseClick(grid->viewport(),Qt::LeftButton,Qt::NoModifier,point(grid,5,3));QTest::qWait(50);QCOMPARE(edits.count(),hiddenBefore);QVERIFY(!painter->isChecked());window.findChild<QAction *>("showRowsAction")->trigger();

    }
    void formatPainterDropsLateSnapshotsAndRejectsMalformedReplies() {
        QTemporaryDir temp;StartupOptions options;options.automaticWorkbook=false;MainWindow window(options);window.show();QSignalSpy ready(&window,&MainWindow::serviceReady);QTRY_COMPARE_WITH_TIMEOUT(ready.count(),1,10000);QSignalSpy opened(&window,&MainWindow::workbookReady);window.openBook(temp.filePath("painter-late.dhwb"),true);QTRY_COMPARE_WITH_TIMEOUT(opened.count(),1,10000);auto *model=window.model();auto *grid=window.findChild<QTableView *>("grid");grid->setCurrentIndex(model->index(0,0));QTRY_VERIFY_WITH_TIMEOUT(model->sourceReady(grid->currentIndex()),5000);DelayedClipboardServer server(model->state(),model->sheet());server.holdStyles=true;QVERIFY(server.listen(QHostAddress::LocalHost,0));window.findChild<RestClient *>()->configure("http://127.0.0.1:"+QString::number(server.serverPort()),"token");auto *action=window.findChild<QAction *>("formatPainterAction");QSignalSpy edits(&window,&MainWindow::operationFinished);action->trigger();QTRY_COMPARE_WITH_TIMEOUT(server.requests.size(),1,5000);grid->setCurrentIndex(model->index(2,2));server.respondStyles(0,QJsonArray{QJsonArray{QJsonObject{{"bold",true}}}});QTest::qWait(100);QVERIFY(!action->property("ready").toBool());QVERIFY(!action->isChecked());action->trigger();QTRY_COMPARE_WITH_TIMEOUT(server.requests.size(),2,5000);server.respondStyles(1,QJsonArray{QJsonArray{true}});QTest::qWait(100);QVERIFY(!action->isChecked());QVERIFY(edits.isEmpty());
        QTRY_VERIFY_WITH_TIMEOUT(model->sourceReady(grid->currentIndex()),5000);action->trigger();QTRY_COMPARE_WITH_TIMEOUT(server.requests.size(),3,5000);grid->edit(grid->currentIndex());auto *editor=grid->findChild<QLineEdit *>();QVERIFY(editor);editor->setFocus();QTest::keyClicks(editor,"retained draft");QJsonArray styles;styles.append(QJsonArray{QJsonValue(QJsonObject{{"bold",true}})});server.respondStyles(2,styles);QTest::qWait(100);QVERIFY(!action->isChecked());QCOMPARE(editor->text(),QString("retained draft"));QVERIFY(edits.isEmpty());QTest::keyClick(editor,Qt::Key_Escape);

    }
    void fontControlsValidateRenderAndUndoWithoutRetyping() {
        FormatCellsDialog dialog("A1");QVERIFY(dialog.patch().isEmpty());auto *family=dialog.findChild<QLineEdit *>("fontFamily");family->setText("Arial,serif");QVERIFY(!dialog.findChild<QDialogButtonBox *>()->button(QDialogButtonBox::Ok)->isEnabled());family->setText("DejaVu Sans");dialog.findChild<QSpinBox *>("fontSize")->setValue(16);dialog.findChild<QLineEdit *>("textColor")->setText("#12AB34");QCOMPARE(dialog.patch(),(QJsonObject{{"font_family","DejaVu Sans"},{"font_size",16},{"text_color","#12ab34"}}));
        QTemporaryDir temp;StartupOptions options;options.automaticWorkbook=false;MainWindow window(options);window.show();QSignalSpy ready(&window,&MainWindow::serviceReady);QTRY_COMPARE_WITH_TIMEOUT(ready.count(),1,10000);QSignalSpy opened(&window,&MainWindow::workbookReady);window.openBook(temp.filePath("fonts.dhwb"),true);QTRY_COMPARE_WITH_TIMEOUT(opened.count(),1,10000);window.submit({{"action","edit"},{"cells",QJsonArray{QJsonObject{{"row",1},{"col",1},{"source","00123"},{"kind","text"}}}}});QTRY_COMPARE_WITH_TIMEOUT(opened.count(),2,10000);auto *model=window.model();auto *grid=window.findChild<QTableView *>("grid");grid->setCurrentIndex(model->index(0,0));QTimer timer;connect(&timer,&QTimer::timeout,&window,[&]{if(auto *d=window.findChild<QDialog *>("formatCellsDialog"))if(!d->property("checked").toBool()){d->setProperty("checked",true);d->findChild<QTabWidget *>("formatTabs")->setCurrentIndex(1);d->findChild<QLineEdit *>("fontFamily")->setText("DejaVu Sans");d->findChild<QSpinBox *>("fontSize")->setValue(16);d->findChild<QLineEdit *>("textColor")->setText("#12ab34");d->grab().save("build/font-cells-"+QGuiApplication::platformName()+".png");d->accept();}});timer.start(10);window.findChild<QAction *>("formatCellsAction")->trigger();QTRY_COMPARE_WITH_TIMEOUT(opened.count(),3,10000);QTRY_VERIFY_WITH_TIMEOUT(model->loaded(model->index(0,0)),5000);QCOMPARE(qvariant_cast<QFont>(model->data(model->index(0,0),Qt::FontRole)).family(),QString("DejaVu Sans"));QCOMPARE(qvariant_cast<QFont>(model->data(model->index(0,0),Qt::FontRole)).pointSize(),16);QCOMPARE(qvariant_cast<QBrush>(model->data(model->index(0,0),Qt::ForegroundRole)).color(),QColor("#12ab34"));QCOMPARE(model->data(model->index(0,0)).toString(),QString("00123"));window.submit({{"action","undo"}});QTRY_COMPARE_WITH_TIMEOUT(opened.count(),4,10000);QTRY_VERIFY_WITH_TIMEOUT(model->loaded(model->index(0,0)),5000);QVERIFY(!model->data(model->index(0,0),Qt::ForegroundRole).isValid());
    }
    void currencyDialogValidatesExplicitLabelsAndKeepsMagnitude() {
        FormatCellsDialog dialog("A1");auto *category=dialog.findChild<QComboBox *>("numberFormat");category->setCurrentIndex(category->findData("currency"));auto *code=dialog.findChild<QLineEdit *>("currencyCode");QVERIFY(code->isEnabled());QCOMPARE(code->text(),QString("USD"));code->setText("usd");QVERIFY(!dialog.findChild<QDialogButtonBox *>()->button(QDialogButtonBox::Ok)->isEnabled());code->setText("EUR");QVERIFY(dialog.findChild<QDialogButtonBox *>()->button(QDialogButtonBox::Ok)->isEnabled());QCOMPARE(dialog.patch(),(QJsonObject{{"format","currency"},{"currency","EUR"}}));category->setCurrentIndex(category->findData("general"));QVERIFY(!dialog.patch().contains("currency"));
        QTemporaryDir temp;StartupOptions options;options.automaticWorkbook=false;MainWindow window(options);window.show();QSignalSpy ready(&window,&MainWindow::serviceReady);QTRY_COMPARE_WITH_TIMEOUT(ready.count(),1,10000);QSignalSpy opened(&window,&MainWindow::workbookReady);window.openBook(temp.filePath("currency.dhwb"),true);QTRY_COMPARE_WITH_TIMEOUT(opened.count(),1,10000);window.submit({{"action","edit"},{"cells",QJsonArray{QJsonObject{{"row",1},{"col",1},{"source","-1234.5"}}}}});QTRY_COMPARE_WITH_TIMEOUT(opened.count(),2,10000);auto *grid=window.findChild<QTableView *>("grid");grid->setCurrentIndex(window.model()->index(0,0));
        QTimer timer;connect(&timer,&QTimer::timeout,&window,[&]{if(auto *d=window.findChild<QDialog *>("formatCellsDialog"))if(!d->property("checked").toBool()){d->setProperty("checked",true);auto *c=d->findChild<QComboBox *>("numberFormat");c->setCurrentIndex(c->findData("currency"));d->findChild<QLineEdit *>("currencyCode")->setText("EUR");d->findChild<QCheckBox *>("numberGrouping")->setChecked(true);d->accept();}});timer.start(10);window.findChild<QAction *>("formatCellsAction")->trigger();QTRY_COMPARE_WITH_TIMEOUT(opened.count(),3,10000);QTRY_COMPARE_WITH_TIMEOUT(window.model()->data(window.model()->index(0,0)).toString(),QString("-1,234.50 EUR"),5000);QCOMPARE(window.model()->cell(window.model()->index(0,0))["source"].toString(),QString("-1234.5"));window.findChild<QAction *>("moreDecimalsAction")->trigger();QTRY_COMPARE_WITH_TIMEOUT(opened.count(),4,10000);QTRY_COMPARE_WITH_TIMEOUT(window.model()->data(window.model()->index(0,0)).toString(),QString("-1,234.500 EUR"),5000);
    }
    void groupingDialogKeepsTriStateAndChangesOnlyNumericPresentation() {
        FormatCellsDialog dialog("A1");auto *grouping=dialog.findChild<QCheckBox *>("numberGrouping");QCOMPARE(grouping->checkState(),Qt::PartiallyChecked);QVERIFY(dialog.patch().isEmpty());grouping->setChecked(true);QCOMPARE(dialog.patch(),(QJsonObject{{"grouping",true}}));auto *category=dialog.findChild<QComboBox *>("numberFormat");category->setCurrentIndex(category->findData("text"));QVERIFY(!grouping->isEnabled());QVERIFY(!dialog.patch().contains("grouping"));category->setCurrentIndex(category->findData("percent"));QVERIFY(grouping->isEnabled());QVERIFY(dialog.patch()["grouping"].toBool());
    }
    void groupingFormatsSelectionThroughManagedDialogAndUndo() {
        QTemporaryDir temp;StartupOptions options;options.automaticWorkbook=false;MainWindow window(options);window.show();QSignalSpy ready(&window,&MainWindow::serviceReady);QTRY_COMPARE_WITH_TIMEOUT(ready.count(),1,10000);QSignalSpy opened(&window,&MainWindow::workbookReady);window.openBook(temp.filePath("grouping.dhwb"),true);QTRY_COMPARE_WITH_TIMEOUT(opened.count(),1,10000);window.submit({{"action","edit"},{"cells",QJsonArray{QJsonObject{{"row",1},{"col",1},{"source","12345.625"}},QJsonObject{{"row",1},{"col",2},{"source","0012345"},{"kind","text"}}}}});QTRY_COMPARE_WITH_TIMEOUT(opened.count(),2,10000);auto *grid=window.findChild<QTableView *>("grid");auto *model=window.model();grid->setCurrentIndex(model->index(0,0));grid->selectionModel()->select(QItemSelection(model->index(0,0),model->index(0,1)),QItemSelectionModel::ClearAndSelect);
        QTimer timer;connect(&timer,&QTimer::timeout,&window,[&]{if(auto *dialog=window.findChild<QDialog *>("formatCellsDialog"))if(!dialog->property("checked").toBool()){dialog->setProperty("checked",true);dialog->findChild<QCheckBox *>("numberGrouping")->setChecked(true);dialog->findChild<QSpinBox *>("decimalPlaces")->setValue(3);dialog->accept();}});timer.start(10);window.findChild<QAction *>("formatCellsAction")->trigger();QTRY_COMPARE_WITH_TIMEOUT(opened.count(),3,10000);QTRY_COMPARE_WITH_TIMEOUT(model->data(model->index(0,0)).toString(),QString("12,345.625"),5000);QTRY_COMPARE_WITH_TIMEOUT(model->data(model->index(0,1)).toString(),QString("0012345"),5000);QCOMPARE(model->cell(model->index(0,0))["source"].toString(),QString("12345.625"));window.submit({{"action","undo"}});QTRY_COMPARE_WITH_TIMEOUT(opened.count(),4,10000);QTRY_COMPARE_WITH_TIMEOUT(model->data(model->index(0,0)).toString(),QString("12345.625"),5000);
    }
    void decimalControlsPreserveMixedFormatsAndOneUndo() {
        FormatCellsDialog dialog("A1:B1");auto *places=dialog.findChild<QSpinBox *>("decimalPlaces");places->setValue(4);QCOMPARE(dialog.patch(),(QJsonObject{{"decimals",4}}));auto *category=dialog.findChild<QComboBox *>("numberFormat");category->setCurrentIndex(category->findData("general"));QVERIFY(!places->isEnabled());QVERIFY(!dialog.patch().contains("decimals"));
        QTemporaryDir temp;StartupOptions options;options.automaticWorkbook=false;MainWindow window(options);window.show();QSignalSpy ready(&window,&MainWindow::serviceReady);QTRY_COMPARE_WITH_TIMEOUT(ready.count(),1,10000);QSignalSpy opened(&window,&MainWindow::workbookReady);window.openBook(temp.filePath("precision.dhwb"),true);QTRY_COMPARE_WITH_TIMEOUT(opened.count(),1,10000);window.submit({{"action","edit"},{"cells",QJsonArray{QJsonObject{{"row",1},{"col",1},{"source",".125"},{"style",QJsonObject{{"format","percent2"}}}},QJsonObject{{"row",1},{"col",2},{"source","=A1*2"}},QJsonObject{{"row",1},{"col",3},{"source","1.25"},{"style",QJsonObject{{"format","general"},{"decimals",14}}}}}}});QTRY_COMPARE_WITH_TIMEOUT(opened.count(),2,10000);auto *grid=window.findChild<QTableView *>("grid");auto *model=window.model();grid->setCurrentIndex(model->index(0,0));QTRY_VERIFY_WITH_TIMEOUT(model->loaded(grid->currentIndex()),5000);grid->selectionModel()->select(QItemSelection(model->index(0,0),model->index(0,1)),QItemSelectionModel::ClearAndSelect);QSignalSpy edits(&window,&MainWindow::operationFinished);window.findChild<QAction *>("moreDecimalsAction")->trigger();QTRY_COMPARE_WITH_TIMEOUT(opened.count(),3,10000);QTRY_COMPARE_WITH_TIMEOUT(model->data(model->index(0,0)).toString(),QString("12.500%"),5000);QTRY_COMPARE_WITH_TIMEOUT(model->data(model->index(0,1)).toString(),QString("0.250"),5000);QCOMPARE(model->cell(model->index(0,1))["source"].toString(),QString("=A1*2"));QCOMPARE(edits.count(),1);window.submit({{"action","undo"}});QTRY_COMPARE_WITH_TIMEOUT(opened.count(),4,10000);QTRY_COMPARE_WITH_TIMEOUT(model->data(model->index(0,0)).toString(),QString("12.50%"),5000);QTRY_COMPARE_WITH_TIMEOUT(model->data(model->index(0,1)).toString(),QString("0.25"),5000);grid->setCurrentIndex(model->index(0,2));QTRY_VERIFY_WITH_TIMEOUT(model->loaded(grid->currentIndex()),5000);window.findChild<QAction *>("moreDecimalsAction")->trigger();QTRY_COMPARE_WITH_TIMEOUT(opened.count(),5,10000);QTRY_COMPARE_WITH_TIMEOUT(model->data(model->index(0,2)).toString(),QString("1.250"),5000);
    }
    void traceTargetValidationRejectsMalformedLocations() {
        const QJsonArray sheets{QJsonObject{{"id","s"},{"name","Sheet1"}}};const QJsonObject good{{"sheet","s"},{"sheet_name","Sheet1"},{"r1",2},{"c1",2},{"r2",1048576},{"c2",2},{"address","B2:B1048576"}};QVERIFY(validTraceTarget(good,sheets));for(const auto &key:QStringList{"r1","r2","c1","c2"}){auto bad=good;bad[key]=true;QVERIFY(!validTraceTarget(bad,sheets));}auto bad=good;bad["address"]="A1";QVERIFY(!validTraceTarget(bad,sheets));bad=good;bad["sheet"]="missing";QVERIFY(!validTraceTarget(bad,sheets));bad=good;bad["r2"]=1048577;QVERIFY(!validTraceTarget(bad,sheets));
    }
    void traceNavigationCrossesSheetsRevealsTargetsAndMakesNoEdits() {
        QTemporaryDir temp;StartupOptions options;options.automaticWorkbook=false;MainWindow window(options);window.show();QSignalSpy ready(&window,&MainWindow::serviceReady);QTRY_COMPARE_WITH_TIMEOUT(ready.count(),1,10000);QSignalSpy opened(&window,&MainWindow::workbookReady);window.openBook(temp.filePath("trace-nav.dhwb"),true);QTRY_COMPARE_WITH_TIMEOUT(opened.count(),1,10000);window.submit({{"action","sheet_add"},{"name","Other"}});QTRY_COMPARE_WITH_TIMEOUT(opened.count(),2,10000);auto *tabs=window.findChild<QTabBar *>("sheets");tabs->setCurrentIndex(0);window.submit({{"action","edit"},{"cells",QJsonArray{QJsonObject{{"row",4},{"col",4},{"source","=SUM(Other!B2:C3)"}}}}});QTRY_COMPARE_WITH_TIMEOUT(opened.count(),3,10000);auto *grid=window.findChild<QTableView *>("grid");auto *model=window.model();grid->setCurrentIndex(model->index(3,3));QSignalSpy edits(&window,&MainWindow::operationFinished);int dialogs=0;
        QTimer timer;connect(&timer,&QTimer::timeout,&window,[&]{auto *dialog=window.findChild<QDialog *>("formulaTraceDialog");if(!dialog || dialog->property("checked").toBool())return;dialog->setProperty("checked",true);++dialogs;QVERIFY(dialog->findChild<QListWidget *>("traceTargets")->count()>0);dialog->findChild<QPushButton *>("traceGoButton")->click();});timer.start(10);
        window.findChild<QAction *>("tracePrecedentsAction")->trigger();QTRY_COMPARE_WITH_TIMEOUT(dialogs,1,5000);QTRY_COMPARE_WITH_TIMEOUT(tabs->currentIndex(),1,5000);QTRY_COMPARE_WITH_TIMEOUT(grid->currentIndex(),model->index(1,1),5000);
        window.findChild<QAction *>("traceDependentsAction")->trigger();QTRY_COMPARE_WITH_TIMEOUT(dialogs,2,5000);QTRY_COMPARE_WITH_TIMEOUT(tabs->currentIndex(),0,5000);QTRY_COMPARE_WITH_TIMEOUT(grid->currentIndex(),model->index(3,3),5000);QVERIFY(edits.isEmpty());
        timer.stop();window.submit({{"action","edit"},{"cells",QJsonArray{QJsonObject{{"row",4},{"col",4},{"source","=A1"}}}}});QTRY_COMPARE_WITH_TIMEOUT(opened.count(),4,10000);grid->setCurrentIndex(model->index(0,0));grid->selectionModel()->select(model->index(0,0),QItemSelectionModel::ClearAndSelect);window.findChild<QAction *>("hideRowsAction")->trigger();QVERIFY(grid->isRowHidden(0));grid->setCurrentIndex(model->index(3,3));timer.start(10);const int before=edits.count();window.findChild<QAction *>("tracePrecedentsAction")->trigger();QTRY_COMPARE_WITH_TIMEOUT(dialogs,3,5000);QTRY_COMPARE_WITH_TIMEOUT(grid->currentIndex(),model->index(0,0),5000);QVERIFY(!grid->isRowHidden(0));QCOMPARE(edits.count(),before);
    }
    void traceNavigationDropsMetadataReplyAfterSelectionChanges() {
        QTemporaryDir temp;StartupOptions options;options.automaticWorkbook=false;MainWindow window(options);window.show();QSignalSpy ready(&window,&MainWindow::serviceReady);QTRY_COMPARE_WITH_TIMEOUT(ready.count(),1,10000);QSignalSpy opened(&window,&MainWindow::workbookReady);window.openBook(temp.filePath("trace-late.dhwb"),true);QTRY_COMPARE_WITH_TIMEOUT(opened.count(),1,10000);window.submit({{"action","edit"},{"cells",QJsonArray{QJsonObject{{"row",1},{"col",1},{"source","=B2"}}}}});QTRY_COMPARE_WITH_TIMEOUT(opened.count(),2,10000);auto *model=window.model();auto *grid=window.findChild<QTableView *>("grid");grid->setCurrentIndex(model->index(0,0));QTRY_VERIFY_WITH_TIMEOUT(model->sourceReady(grid->currentIndex()),5000);DelayedTraceServer server(model->state(),model->sheet(),model->workbook());QVERIFY(server.listen(QHostAddress::LocalHost,0));window.findChild<RestClient *>()->configure("http://127.0.0.1:"+QString::number(server.serverPort()),"token");QSignalSpy edits(&window,&MainWindow::operationFinished);
        QTimer timer;connect(&timer,&QTimer::timeout,&window,[&]{if(auto *dialog=window.findChild<QDialog *>("formulaTraceDialog"))if(!dialog->property("checked").toBool()){dialog->setProperty("checked",true);dialog->findChild<QPushButton *>("traceGoButton")->click();}});timer.start(10);window.findChild<QAction *>("tracePrecedentsAction")->trigger();QTRY_COMPARE_WITH_TIMEOUT(server.traces.size(),1,5000);server.respondTrace({{"sheet",model->sheet()},{"sheet_name","Sheet1"},{"r1",2},{"c1",2},{"r2",2},{"c2",2},{"address","B2"}});QTRY_COMPARE_WITH_TIMEOUT(server.metadata.size(),1,5000);grid->setCurrentIndex(model->index(5,5));server.respondInfo();QTest::qWait(100);QCOMPARE(grid->currentIndex(),model->index(5,5));QVERIFY(edits.isEmpty());
    }
    void shortLivedPreviewModelIgnoresLateRangeReplies() {
        DelayedClipboardServer server("state","sheet");server.holdRanges=true;QVERIFY(server.listen(QHostAddress::LocalHost,0));RestClient client;client.configure("http://127.0.0.1:"+QString::number(server.serverPort()),"token");auto *model=new SheetModel(&client,nullptr,32,16);QPointer<SheetModel> guarded=model;model->bind("book","sheet","state",true);model->data(model->index(0,0));QTRY_COMPARE_WITH_TIMEOUT(server.requests.size(),1,5000);delete model;QVERIFY(guarded.isNull());server.respond(0,"late");QTest::qWait(100);QVERIFY(guarded.isNull());
    }
    void crossSheetReferencePickerRetainsOriginalDraftAndQuotesNames() {
        QTemporaryDir temp;StartupOptions options;options.automaticWorkbook=false;MainWindow window(options);window.show();QSignalSpy ready(&window,&MainWindow::serviceReady);QTRY_COMPARE_WITH_TIMEOUT(ready.count(),1,10000);QSignalSpy opened(&window,&MainWindow::workbookReady);window.openBook(temp.filePath("cross-pick.dhwb"),true);QTRY_COMPARE_WITH_TIMEOUT(opened.count(),1,10000);window.submit({{"action","sheet_add"},{"name","Costs O'Brien"}});QTRY_COMPARE_WITH_TIMEOUT(opened.count(),2,10000);window.findChild<QTabBar *>("sheets")->setCurrentIndex(1);window.submit({{"action","edit"},{"cells",QJsonArray{QJsonObject{{"row",2},{"col",2},{"source","4"}},QJsonObject{{"row",3},{"col",2},{"source","6"}}}}});QTRY_COMPARE_WITH_TIMEOUT(opened.count(),3,10000);
        auto *grid=window.findChild<QTableView *>("grid");auto *model=window.model();auto *source=window.findChild<QLineEdit *>("source");auto *tabs=window.findChild<QTabBar *>("sheets");tabs->setCurrentIndex(0);grid->setCurrentIndex(model->index(3,3));QTRY_VERIFY_WITH_TIMEOUT(source->isEnabled(),5000);const auto originalSheet=model->sheet();QSignalSpy edits(&window,&MainWindow::operationFinished);QString address="B2:B3";bool cancel=false,expire=false;int dialogs=0;
        QTimer timer;connect(&timer,&QTimer::timeout,&window,[&]{auto *dialog=window.findChild<QDialog *>("referencePickerDialog");if(!dialog || dialog->property("checked").toBool())return;dialog->setProperty("checked",true);++dialogs;auto *preview=dialog->findChild<QTableView *>("referencePickerGrid");QCOMPARE(preview->editTriggers(),QAbstractItemView::NoEditTriggers);QCOMPARE(model->sheet(),originalSheet);QCOMPARE(grid->currentIndex(),model->index(3,3));auto *line=dialog->findChild<QLineEdit *>("referenceAddress");auto *ok=dialog->findChild<QDialogButtonBox *>()->button(QDialogButtonBox::Ok);line->setText("A0");QTest::keyClick(line,Qt::Key_Return);QVERIFY(!ok->isEnabled());line->setText(address);QTest::keyClick(line,Qt::Key_Return);if(expire){QMetaObject::invokeMethod(preview->model(),"externalChange");QVERIFY(!ok->isEnabled());dialog->reject();}else if(cancel)dialog->reject();else ok->click();});timer.start(10);
        source->setFocus();QTest::keyClicks(source,"=SUM(");QTest::mouseClick(window.findChild<QToolButton *>("pickOtherSheetReferenceButton"),Qt::LeftButton);QTRY_COMPARE_WITH_TIMEOUT(dialogs,1,5000);QTRY_COMPARE_WITH_TIMEOUT(source->text(),QString("=SUM('Costs O''Brien'!B2:B3"),5000);QVERIFY(edits.isEmpty());QCOMPARE(model->sheet(),originalSheet);source->undo();QCOMPARE(source->text(),QString("=SUM("));source->redo();source->setFocus();QTest::keyClicks(source,")");QTest::keyClick(source,Qt::Key_Return);QTRY_COMPARE_WITH_TIMEOUT(opened.count(),4,10000);QTRY_COMPARE_WITH_TIMEOUT(model->data(model->index(3,3)).toString(),QString("10"),5000);
        grid->setCurrentIndex(model->index(3,3));QTRY_VERIFY_WITH_TIMEOUT(source->isEnabled(),5000);source->setFocus();source->selectAll();QTest::keyClicks(source,"=1+");cancel=true;window.findChild<QAction *>("pickOtherSheetReferenceAction")->trigger();QCOMPARE(dialogs,2);QCOMPARE(source->text(),QString("=1+"));QCOMPARE(edits.count(),1);QTest::keyClick(source,Qt::Key_Escape);
        cancel=false;address="B2";source->setFocus();source->selectAll();QTest::keyClicks(source,"=1+");window.findChild<QAction *>("pickOtherSheetReferenceAction")->trigger();QCOMPARE(dialogs,3);source->setFocus();QTest::keyClicks(source,"9");QTest::qWait(100);QCOMPARE(source->text(),QString("=1+9"));QTest::keyClick(source,Qt::Key_Escape);
        expire=true;source->setFocus();source->selectAll();QTest::keyClicks(source,"=1+");window.findChild<QAction *>("pickOtherSheetReferenceAction")->trigger();QCOMPARE(dialogs,4);QCOMPARE(source->text(),QString("=1+"));QTest::keyClick(source,Qt::Key_Escape);
        expire=false;address="B2";QTRY_VERIFY_WITH_TIMEOUT(model->sourceReady(grid->currentIndex()),5000);grid->edit(grid->currentIndex());auto *editor=grid->findChild<QLineEdit *>();QVERIFY(editor);editor->setFocus();editor->selectAll();QTest::keyClicks(editor,"=SUM(");QTest::mouseClick(window.findChild<QToolButton *>("pickOtherSheetReferenceButton"),Qt::LeftButton);QTRY_COMPARE_WITH_TIMEOUT(dialogs,5,5000);QTRY_COMPARE_WITH_TIMEOUT(source->text(),QString("=SUM('Costs O''Brien'!B2"),5000);QCOMPARE(edits.count(),1);QTest::keyClick(source,Qt::Key_Escape);
    }
    void referenceCycleTargetsEndpointsSelectionsAndIgnoresQuotes() {
        auto cycle=[](QString text,int cursor,int start=-1,int length=0){const auto change=cycleFormulaReference(text,cursor,start,length);if(change.start>=0)text.replace(change.start,change.length,change.replacement);return text;};
        QString text="=a1";for(const auto &expected:{"=$a$1","=a$1","=$a1","=a1"}){text=cycle(text,text.size());QCOMPARE(text,QString(expected));}
        QCOMPARE(cycle("='A1 name'!B2:C3",13),QString("='A1 name'!$B$2:C3"));QCOMPARE(cycle("=A1:B2",6,1,5),QString("=$A$1:$B$2"));
        QCOMPARE(cycle("=A1+B2",2,2,3),QString("=A1+B2"));QCOMPARE(cycle("=\"A1\"+B2",3),QString("=\"A1\"+B2"));QCOMPARE(cycle("='A1'!B2",3),QString("='A1'!B2"));QCOMPARE(cycle("=XFE1",5),QString("=XFE1"));
        text="=";for(int i=0;i<70;++i)text+="A1+";text+="B2";QVERIFY(cycle(text,text.size()).endsWith("$B$2"));
    }
    void f4ReferenceCyclesStayDraftsInSourceCellAndFrozenEditors() {
        QTemporaryDir temp;StartupOptions options;options.automaticWorkbook=false;MainWindow window(options);window.show();QSignalSpy ready(&window,&MainWindow::serviceReady);QTRY_COMPARE_WITH_TIMEOUT(ready.count(),1,10000);QSignalSpy opened(&window,&MainWindow::workbookReady);window.openBook(temp.filePath("f4.dhwb"),true);QTRY_COMPARE_WITH_TIMEOUT(opened.count(),1,10000);auto *grid=window.findChild<QTableView *>("grid");auto *model=window.model();auto *source=window.findChild<QLineEdit *>("source");QSignalSpy edits(&window,&MainWindow::operationFinished);
        grid->setCurrentIndex(model->index(1,1));QTRY_VERIFY_WITH_TIMEOUT(source->isEnabled(),5000);source->setFocus();QTest::keyClicks(source,"=A1");QTest::keyClick(source,Qt::Key_F4);QCOMPARE(source->text(),QString("=$A$1"));source->undo();QCOMPARE(source->text(),QString("=A1"));source->redo();QCOMPARE(source->text(),QString("=$A$1"));QVERIFY(edits.isEmpty());QTest::keyClick(source,Qt::Key_Return);QTRY_COMPARE_WITH_TIMEOUT(opened.count(),2,10000);QTRY_COMPARE_WITH_TIMEOUT(model->data(model->index(1,1),Qt::EditRole).toString(),QString("=$A$1"),5000);
        grid->setCurrentIndex(model->index(2,1));QTRY_VERIFY_WITH_TIMEOUT(model->sourceReady(grid->currentIndex()),5000);grid->edit(grid->currentIndex());auto *editor=grid->findChild<QLineEdit *>();QVERIFY(editor);editor->setFocus();QTest::keyClicks(editor,"=A1:B2");editor->setSelection(1,5);QTest::keyClick(editor,Qt::Key_F4);QCOMPARE(editor->text(),QString("=$A$1:$B$2"));QCOMPARE(edits.count(),1);QTest::keyClick(editor,Qt::Key_Escape);
        window.findChild<QAction *>("freezeFirstColumnAction")->trigger();auto *left=window.findChild<QTableView *>("frozenFirstColumn");grid->setCurrentIndex(model->index(3,0));left->setCurrentIndex(left->model()->index(3,0));QTRY_VERIFY_WITH_TIMEOUT(model->sourceReady(model->index(3,0)),5000);left->edit(left->currentIndex());editor=left->findChild<QLineEdit *>();QVERIFY(editor);editor->setFocus();QTest::keyClicks(editor,"=A$1");QTest::keyClick(editor,Qt::Key_F4);QCOMPARE(editor->text(),QString("=$A1"));QTest::keyClick(editor,Qt::Key_Escape);QCOMPARE(edits.count(),1);
        FormulaLineEdit literal;literal.show();literal.setFocus();literal.setText("=A1");literal.setFormulaAllowed(false);QTest::keyClick(&literal,Qt::Key_F4);QCOMPARE(literal.text(),QString("=A1"));literal.setFormulaAllowed(true);literal.setReadOnly(true);QTest::keyClick(&literal,Qt::Key_F4);QCOMPARE(literal.text(),QString("=A1"));
    }
    void catalogExamplesCalculateThroughNativeSourceEditor() {
        QTemporaryDir temp;StartupOptions options;options.automaticWorkbook=false;MainWindow window(options);window.show();QSignalSpy ready(&window,&MainWindow::serviceReady);QTRY_COMPARE_WITH_TIMEOUT(ready.count(),1,10000);QSignalSpy opened(&window,&MainWindow::workbookReady);window.openBook(temp.filePath("catalog-editor.dhwb"),true);QTRY_COMPARE_WITH_TIMEOUT(opened.count(),1,10000);QJsonObject catalog;QString error;bool loaded=false;window.findChild<RestClient *>()->request("/v1/functions",{},[&](QJsonObject result,QString issue){catalog=result;error=issue;loaded=true;},false);QTRY_VERIFY_WITH_TIMEOUT(loaded,5000);QVERIFY(error.isEmpty());QVERIFY(validFunctionCatalog(catalog));
        auto *grid=window.findChild<QTableView *>("grid");auto *model=window.model();auto *source=dynamic_cast<FormulaLineEdit *>(window.findChild<QLineEdit *>("source"));int changes=1;
        for(const auto &item:catalog["functions"].toArray())for(const auto &value:item.toObject()["examples"].toArray()){
            const auto example=value.toObject();QJsonArray cells;for(int row=1;row<=4;++row)for(int col=1;col<=4;++col){QJsonObject cell{{"row",row},{"col",col},{"source",""}};for(const auto &input:example["inputs"].toArray())if(input.toObject()["row"].toInt()==row && input.toObject()["col"].toInt()==col)cell=input.toObject();cells.append(cell);}window.submit({{"action","edit"},{"cells",cells}});++changes;QTRY_COMPARE_WITH_TIMEOUT(opened.count(),changes,10000);grid->setCurrentIndex(model->index(0,0));QTRY_VERIFY_WITH_TIMEOUT(source->isEnabled(),5000);source->setFocus();source->selectAll();QTest::keyClicks(source,example["formula"].toString());QTest::keyClick(source,Qt::Key_Return);++changes;QTRY_COMPARE_WITH_TIMEOUT(opened.count(),changes,10000);QTRY_VERIFY_WITH_TIMEOUT(model->loaded(model->index(0,0)),5000);QTRY_VERIFY_WITH_TIMEOUT(QStringList({"current","error"}).contains(model->cell(model->index(0,0))["calculation"].toString()),5000);if(example["result"].isString())QCOMPARE(model->data(model->index(0,0)).toString(),example["result"].toString());else QCOMPARE(model->cell(model->index(0,0))["value"],example["result"]);
        }
    }
    void formulaTracingReadsStoredReferencesWithoutMutation() {
        QTemporaryDir temp;StartupOptions options;options.automaticWorkbook=false;MainWindow window(options);window.show();QSignalSpy ready(&window,&MainWindow::serviceReady);QTRY_COMPARE_WITH_TIMEOUT(ready.count(),1,10000);QSignalSpy opened(&window,&MainWindow::workbookReady);window.openBook(temp.filePath("trace.dhwb"),true);QTRY_COMPARE_WITH_TIMEOUT(opened.count(),1,10000);
        window.submit({{"action","edit"},{"cells",QJsonArray{QJsonObject{{"row",1},{"col",1},{"source","=IF(FALSE,SUM(B1:B10),C2)+#REF!"}},QJsonObject{{"row",2},{"col",1},{"source","=A1+A1"}}}}});QTRY_COMPARE_WITH_TIMEOUT(opened.count(),2,10000);auto *grid=window.findChild<QTableView *>("grid");grid->setCurrentIndex(window.model()->index(0,0));QSignalSpy edits(&window,&MainWindow::operationFinished);const auto state=window.model()->state();int shown=0;QString text;
        QTimer timer;connect(&timer,&QTimer::timeout,&window,[&]{if(auto *dialog=window.findChild<QDialog *>("formulaTraceDialog");dialog && !dialog->property("checked").toBool()){dialog->setProperty("checked",true);text=dialog->findChild<QPlainTextEdit *>("formulaTraceDetails")->toPlainText();++shown;dialog->reject();}});timer.start(10);
        window.findChild<QAction *>("tracePrecedentsAction")->trigger();QTRY_COMPARE_WITH_TIMEOUT(shown,1,5000);QVERIFY(text.contains("Sheet1!B1:B10"));QVERIFY(text.contains("Sheet1!C2"));QVERIFY(text.contains("Broken reference at Sheet1!A1"));
        window.findChild<QAction *>("traceDependentsAction")->trigger();QTRY_COMPARE_WITH_TIMEOUT(shown,2,5000);QVERIFY(text.startsWith("Sheet1!A2\n"));QVERIFY(text.contains("Broken reference at Sheet1!A1"));QVERIFY(edits.isEmpty());QCOMPARE(window.model()->state(),state);QCOMPARE(grid->currentIndex(),window.model()->index(0,0));
    }
    void exactLookupCalculatesIdentifierKeysAndLazyFallbackInTheNativeEditor() {
        QTemporaryDir temp;StartupOptions options;options.automaticWorkbook=false;MainWindow window(options);window.show();QSignalSpy ready(&window,&MainWindow::serviceReady);QTRY_COMPARE_WITH_TIMEOUT(ready.count(),1,10000);QSignalSpy opened(&window,&MainWindow::workbookReady);window.openBook(temp.filePath("lookup.dhwb"),true);QTRY_COMPARE_WITH_TIMEOUT(opened.count(),1,10000);window.submit({{"action","edit"},{"cells",QJsonArray{QJsonObject{{"row",1},{"col",1},{"source","00123"},{"kind","text"}},QJsonObject{{"row",2},{"col",1},{"source","123"}},QJsonObject{{"row",1},{"col",2},{"source","Pen"},{"kind","text"}},QJsonObject{{"row",2},{"col",2},{"source","Book"},{"kind","text"}}}}});QTRY_COMPARE_WITH_TIMEOUT(opened.count(),2,10000);auto *grid=window.findChild<QTableView *>("grid");auto *model=window.model();auto *source=dynamic_cast<FormulaLineEdit *>(window.findChild<QLineEdit *>("source"));grid->setCurrentIndex(model->index(0,3));QTRY_VERIFY_WITH_TIMEOUT(source->isEnabled(),5000);QTRY_VERIFY_WITH_TIMEOUT(source->functionNames().contains("XLOOKUP"),5000);source->setFocus();QTest::keyClicks(source,"=XLOOKUP(\"00123\",A1:A2,B1:B2,1/0)");QTest::keyClick(source,Qt::Key_Return);QTRY_COMPARE_WITH_TIMEOUT(opened.count(),3,10000);QTRY_COMPARE_WITH_TIMEOUT(model->data(model->index(0,3)).toString(),QString("Pen"),5000);window.submit({{"action","edit"},{"cells",QJsonArray{QJsonObject{{"row",2},{"col",4},{"source","=XLOOKUP(123,A1:A2,B1:B2)"}},QJsonObject{{"row",3},{"col",4},{"source","=XLOOKUP(7,A1:A2,B1:B2,\"missing\")"}}}}});QTRY_COMPARE_WITH_TIMEOUT(opened.count(),4,10000);QTRY_COMPARE_WITH_TIMEOUT(model->data(model->index(1,3)).toString(),QString("Book"),5000);QTRY_COMPARE_WITH_TIMEOUT(model->data(model->index(2,3)).toString(),QString("missing"),5000);
    }
    void conditionalFunctionsCalculateAlignedTotalsThroughTheNativeEditor() {
        QTemporaryDir temp;StartupOptions options;options.automaticWorkbook=false;MainWindow window(options);window.show();QSignalSpy ready(&window,&MainWindow::serviceReady);QTRY_COMPARE_WITH_TIMEOUT(ready.count(),1,10000);QSignalSpy opened(&window,&MainWindow::workbookReady);window.openBook(temp.filePath("conditional.dhwb"),true);QTRY_COMPARE_WITH_TIMEOUT(opened.count(),1,10000);window.submit({{"action","edit"},{"cells",QJsonArray{QJsonObject{{"row",1},{"col",1},{"source","Pen"},{"kind","text"}},QJsonObject{{"row",2},{"col",1},{"source","Book"},{"kind","text"}},QJsonObject{{"row",3},{"col",1},{"source","pen"},{"kind","text"}},QJsonObject{{"row",1},{"col",2},{"source","2"}},QJsonObject{{"row",2},{"col",2},{"source","5"}},QJsonObject{{"row",3},{"col",2},{"source","3"}}}}});QTRY_COMPARE_WITH_TIMEOUT(opened.count(),2,10000);auto *grid=window.findChild<QTableView *>("grid");auto *model=window.model();auto *source=dynamic_cast<FormulaLineEdit *>(window.findChild<QLineEdit *>("source"));grid->setCurrentIndex(model->index(0,3));QTRY_VERIFY_WITH_TIMEOUT(source->isEnabled(),5000);QTRY_VERIFY_WITH_TIMEOUT(source->functionNames().contains("SUMIF"),5000);source->setFocus();QTest::keyClicks(source,"=SUMIF(A1:A3,\"Pen\",B1:B3)");QTest::keyClick(source,Qt::Key_Return);QTRY_COMPARE_WITH_TIMEOUT(opened.count(),3,10000);QTRY_COMPARE_WITH_TIMEOUT(model->data(model->index(0,3)).toString(),QString("5"),5000);window.submit({{"action","edit"},{"cells",QJsonArray{QJsonObject{{"row",2},{"col",4},{"source","=COUNTIF(B1:B3,\">2\")"}}}}});QTRY_COMPARE_WITH_TIMEOUT(opened.count(),4,10000);QTRY_COMPARE_WITH_TIMEOUT(model->data(model->index(1,3)).toString(),QString("2"),5000);
    }
    void concatenationCalculatesThroughSourceEditorAndPreservesRawValues() {
        QTemporaryDir temp;StartupOptions options;options.automaticWorkbook=false;MainWindow window(options);window.show();QSignalSpy ready(&window,&MainWindow::serviceReady);QTRY_COMPARE_WITH_TIMEOUT(ready.count(),1,10000);QSignalSpy opened(&window,&MainWindow::workbookReady);window.openBook(temp.filePath("concat.dhwb"),true);QTRY_COMPARE_WITH_TIMEOUT(opened.count(),1,10000);window.submit({{"action","edit"},{"cells",QJsonArray{QJsonObject{{"row",1},{"col",1},{"source","00123"},{"kind","text"}},QJsonObject{{"row",2},{"col",1},{"source","0.25"},{"style",QJsonObject{{"format","percent2"}}}}}}});QTRY_COMPARE_WITH_TIMEOUT(opened.count(),2,10000);auto *grid=window.findChild<QTableView *>("grid");auto *model=window.model();auto *source=dynamic_cast<FormulaLineEdit *>(window.findChild<QLineEdit *>("source"));grid->setCurrentIndex(model->index(0,1));QTRY_VERIFY_WITH_TIMEOUT(source->isEnabled(),5000);QTRY_VERIFY_WITH_TIMEOUT(source->functionNames().contains("CONCAT"),5000);source->setFocus();QTest::keyClicks(source,"=A1&\"X\"");QTest::keyClick(source,Qt::Key_Return);QTRY_COMPARE_WITH_TIMEOUT(opened.count(),3,10000);QTRY_COMPARE_WITH_TIMEOUT(model->data(model->index(0,1)).toString(),QString("00123X"),5000);window.submit({{"action","edit"},{"cells",QJsonArray{QJsonObject{{"row",2},{"col",2},{"source","=CONCAT(A1,TRUE,A2)"}}}}});QTRY_COMPARE_WITH_TIMEOUT(opened.count(),4,10000);QTRY_COMPARE_WITH_TIMEOUT(model->data(model->index(1,1)).toString(),QString("00123TRUE0.25"),5000);
    }
    void substringFunctionsCalculateIdentifiersThroughTheNativeEditor() {
        QTemporaryDir temp;StartupOptions options;options.automaticWorkbook=false;MainWindow window(options);window.show();QSignalSpy ready(&window,&MainWindow::serviceReady);QTRY_COMPARE_WITH_TIMEOUT(ready.count(),1,10000);QSignalSpy opened(&window,&MainWindow::workbookReady);window.openBook(temp.filePath("substrings.dhwb"),true);QTRY_COMPARE_WITH_TIMEOUT(opened.count(),1,10000);window.submit({{"action","edit"},{"cells",QJsonArray{QJsonObject{{"row",1},{"col",1},{"source","00123"},{"kind","text"}}}}});QTRY_COMPARE_WITH_TIMEOUT(opened.count(),2,10000);auto *grid=window.findChild<QTableView *>("grid");auto *model=window.model();auto *source=dynamic_cast<FormulaLineEdit *>(window.findChild<QLineEdit *>("source"));grid->setCurrentIndex(model->index(0,1));QTRY_VERIFY_WITH_TIMEOUT(source->isEnabled(),5000);QTRY_VERIFY_WITH_TIMEOUT(source->functionNames().contains("MID"),5000);source->setFocus();QTest::keyClicks(source,"=MID(A1,2,3)");QTest::keyClick(source,Qt::Key_Return);QTRY_COMPARE_WITH_TIMEOUT(opened.count(),3,10000);QTRY_COMPARE_WITH_TIMEOUT(model->data(model->index(0,1)).toString(),QString("012"),5000);window.submit({{"action","edit"},{"cells",QJsonArray{QJsonObject{{"row",2},{"col",2},{"source","=LEFT(A1,2)"}},QJsonObject{{"row",3},{"col",2},{"source","=RIGHT(A1,2)"}}}}});QTRY_COMPARE_WITH_TIMEOUT(opened.count(),4,10000);QTRY_COMPARE_WITH_TIMEOUT(model->data(model->index(1,1)).toString(),QString("00"),5000);QTRY_COMPARE_WITH_TIMEOUT(model->data(model->index(2,1)).toString(),QString("23"),5000);
    }
    void textCleanupFunctionsCalculateThroughSourceEditorAndKeepIdentifiers() {
        QTemporaryDir temp;StartupOptions options;options.automaticWorkbook=false;MainWindow window(options);window.show();QSignalSpy ready(&window,&MainWindow::serviceReady);QTRY_COMPARE_WITH_TIMEOUT(ready.count(),1,10000);QSignalSpy opened(&window,&MainWindow::workbookReady);window.openBook(temp.filePath("text-functions.dhwb"),true);QTRY_COMPARE_WITH_TIMEOUT(opened.count(),1,10000);window.submit({{"action","edit"},{"cells",QJsonArray{QJsonObject{{"row",1},{"col",1},{"source","  Data   Horizon  "},{"kind","text"}},QJsonObject{{"row",2},{"col",1},{"source","00123"},{"kind","text"}}}}});QTRY_COMPARE_WITH_TIMEOUT(opened.count(),2,10000);auto *model=window.model();auto *grid=window.findChild<QTableView *>("grid");auto *source=dynamic_cast<FormulaLineEdit *>(window.findChild<QLineEdit *>("source"));QTRY_VERIFY_WITH_TIMEOUT(source->functionNames().contains("TRIM"),5000);grid->setCurrentIndex(model->index(0,1));QTRY_VERIFY_WITH_TIMEOUT(source->isEnabled(),5000);source->setFocus();QTest::keyClicks(source,"=UPPER(TRIM(A1))");QTest::keyClick(source,Qt::Key_Return);QTRY_COMPARE_WITH_TIMEOUT(opened.count(),3,10000);QTRY_COMPARE_WITH_TIMEOUT(model->data(model->index(0,1)).toString(),QString("DATA HORIZON"),5000);window.submit({{"action","edit"},{"cells",QJsonArray{QJsonObject{{"row",2},{"col",2},{"source","=LEN(A2)"}},QJsonObject{{"row",2},{"col",3},{"source","=LOWER(A2)"}}}}});QTRY_COMPARE_WITH_TIMEOUT(opened.count(),4,10000);QTRY_COMPARE_WITH_TIMEOUT(model->data(model->index(1,1)).toString(),QString("5"),5000);QTRY_COMPARE_WITH_TIMEOUT(model->data(model->index(1,2)).toString(),QString("00123"),5000);QCOMPARE(model->cell(model->index(1,0))["source"].toString(),QString("00123"));
    }
    void formulaSyntaxCuesIgnoreQuotedTextBoundReferencesAndMatchParentheses() {
        auto cues=formulaSyntaxCues("=SUM(A1:B2,A1,\"C9)\",'O''Brien (D4)'!$C$3)+A1",5);QCOMPARE(cues.references.size(),4);QCOMPARE(cues.references[0].r2,2);QCOMPARE(cues.references[0].c2,2);QCOMPARE(cues.references[2].sheet,QString("O'Brien (D4)"));QCOMPARE(cues.references[1].color,cues.references[3].color);QVERIFY(cues.unmatched.isEmpty());QCOMPARE(cues.matchFirst,4);QVERIFY(cues.matchLast>4);
        cues=formulaSyntaxCues("=SUM(A1",7);QCOMPARE(cues.unmatched,QList<int>{4});QVERIFY(cues.parentheses.contains("unmatched"));QCOMPARE(formulaSyntaxCues("=\"A1\"",5).references.size(),0);QCOMPARE(formulaSyntaxCues("=XFE1+A1048577",8).references.size(),0);
        QString many="=";for(int i=0;i<70;++i)many+=(i?"+A1":"A1");cues=formulaSyntaxCues(many,many.size());QCOMPARE(cues.references.size(),64);QVERIFY(cues.truncated);QCOMPARE(formulaSyntaxCues("literal A1",5).references.size(),0);
    }
    void formulaHighlightingTracksSourceAndCellDraftsWithoutMutationOrSourceChanges() {
        QTemporaryDir temp;StartupOptions options;options.automaticWorkbook=false;MainWindow window(options);window.show();QSignalSpy ready(&window,&MainWindow::serviceReady);QTRY_COMPARE_WITH_TIMEOUT(ready.count(),1,10000);QSignalSpy opened(&window,&MainWindow::workbookReady);window.openBook(temp.filePath("cues.dhwb"),true);QTRY_COMPARE_WITH_TIMEOUT(opened.count(),1,10000);auto *model=window.model();auto *grid=window.findChild<QTableView *>("grid");auto *source=dynamic_cast<FormulaLineEdit *>(window.findChild<QLineEdit *>("source"));auto *legend=window.findChild<QLabel *>("formulaSyntaxCues");QSignalSpy edits(&window,&MainWindow::operationFinished);grid->setCurrentIndex(model->index(3,3));QTRY_VERIFY_WITH_TIMEOUT(source->isEnabled(),5000);source->setFocus();QTest::keyClicks(source,"=SUM(A1:B2)+A1");source->setCursorPosition(5);QCOMPARE(source->syntaxCues().references.size(),2);QCOMPARE(grid->property("formulaReferenceCount").toInt(),2);QVERIFY(legend->isVisible());QVERIFY(legend->text().contains("A1:B2"));QVERIFY(source->property("formulaParentheses").toString().contains("Matching"));const auto original=source->text();const auto cursor=source->cursorPosition();QCOMPARE(grid->currentIndex(),model->index(3,3));QCOMPARE(window.findChild<QLineEdit *>("address")->text(),QString("D4"));QTest::qWait(30);source->repaint();QCOMPARE(source->text(),original);QCOMPARE(source->cursorPosition(),cursor);QVERIFY(edits.isEmpty());
        if(qEnvironmentVariableIsSet("DATAHORIZON_FORMULA_CUES_SCREENSHOT"))QVERIFY(window.grab().save(qEnvironmentVariable("DATAHORIZON_FORMULA_CUES_SCREENSHOT")));
        QTest::keyClick(source,Qt::Key_Escape);QVERIFY(!legend->isVisible());grid->setCurrentIndex(model->index(4,3));QTRY_VERIFY_WITH_TIMEOUT(model->sourceReady(grid->currentIndex()),5000);grid->edit(grid->currentIndex());auto *editor=dynamic_cast<FormulaLineEdit *>(grid->findChild<QLineEdit *>());QVERIFY(editor);editor->setFocus();QTest::keyClicks(editor,"=SUM(B1");QCOMPARE(editor->syntaxCues().references.size(),1);QVERIFY(legend->text().contains("B1"));QVERIFY(editor->property("formulaParentheses").toString().contains("unmatched"));QTest::keyClick(editor,Qt::Key_Escape);QTRY_VERIFY_WITH_TIMEOUT(!legend->isVisible(),3000);QVERIFY(edits.isEmpty());
        source->setText("=A1");source->setFormulaAllowed(false);QCOMPARE(source->syntaxCues().references.size(),0);QCOMPARE(grid->property("formulaReferenceCount").toInt(),0);source->setText(QString(65000,'x'));source->setFormulaAllowed(true);QCOMPARE(source->syntaxCues().references.size(),0);
    }
    void referencePickingKeepsDraftTargetAcrossBodyFrozenCornerCancelAndHiddenRanges() {
        QTemporaryDir temp;StartupOptions options;options.automaticWorkbook=false;MainWindow window(options);window.show();QSignalSpy ready(&window,&MainWindow::serviceReady);QTRY_COMPARE_WITH_TIMEOUT(ready.count(),1,10000);QSignalSpy opened(&window,&MainWindow::workbookReady);window.openBook(temp.filePath("pick.dhwb"),true);QTRY_COMPARE_WITH_TIMEOUT(opened.count(),1,10000);
        window.submit({{"action","edit"},{"cells",QJsonArray{QJsonObject{{"row",1},{"col",1},{"source","1"}},QJsonObject{{"row",1},{"col",2},{"source","2"}},QJsonObject{{"row",2},{"col",1},{"source","3"}},QJsonObject{{"row",2},{"col",2},{"source","4"}}}}});QTRY_COMPARE_WITH_TIMEOUT(opened.count(),2,10000);auto *grid=window.findChild<QTableView *>("grid");auto *model=window.model();auto *source=window.findChild<QLineEdit *>("source");auto *pick=window.findChild<QAction *>("pickReferenceAction");QSignalSpy edits(&window,&MainWindow::operationFinished);
        auto drag=[&](QTableView *from,int r1,int c1,QTableView *to,int r2,int c2){const auto a=from->visualRect(from->model()->index(r1,c1)).center(),b=to->visualRect(to->model()->index(r2,c2)).center();const auto ga=from->viewport()->mapToGlobal(a),gb=to->viewport()->mapToGlobal(b);QMouseEvent press(QEvent::MouseButtonPress,a,ga,Qt::LeftButton,Qt::LeftButton,Qt::NoModifier);QApplication::sendEvent(from->viewport(),&press);QMouseEvent move(QEvent::MouseMove,from->viewport()->mapFromGlobal(gb),gb,Qt::NoButton,Qt::LeftButton,Qt::NoModifier);QApplication::sendEvent(from->viewport(),&move);QMouseEvent release(QEvent::MouseButtonRelease,from->viewport()->mapFromGlobal(gb),gb,Qt::LeftButton,Qt::NoButton,Qt::NoModifier);QApplication::sendEvent(from->viewport(),&release);};
        grid->setCurrentIndex(model->index(3,3));QTRY_VERIFY_WITH_TIMEOUT(source->isEnabled(),5000);source->setFocus();QTest::keyClicks(source,"=SUM(");pick->trigger();QVERIFY(pick->isChecked());drag(grid,0,0,grid,1,1);QCOMPARE(grid->currentIndex(),model->index(3,3));QCOMPARE(source->text(),QString("=SUM(A1:B2"));QVERIFY(edits.isEmpty());source->undo();QCOMPARE(source->text(),QString("=SUM("));source->redo();QTest::keyClicks(source,")");QTest::keyClick(source,Qt::Key_Return);QTRY_COMPARE_WITH_TIMEOUT(opened.count(),3,10000);QTRY_COMPARE_WITH_TIMEOUT(model->data(model->index(3,3)).toString(),QString("10"),5000);
        grid->setCurrentIndex(model->index(2,2));window.findChild<QAction *>("freezePanesAction")->trigger();auto *corner=window.findChild<QTableView *>("frozenCorner");QVERIFY(corner->isVisible());grid->setCurrentIndex(model->index(3,3));QTRY_VERIFY_WITH_TIMEOUT(source->isEnabled(),5000);source->setFocus();source->selectAll();QTest::keyClicks(source,"=SUM(");pick->trigger();drag(corner,0,0,grid,2,2);QCOMPARE(source->text(),QString("=SUM(A1:C3"));QCOMPARE(grid->currentIndex(),model->index(3,3));QCOMPARE(edits.count(),1);QTest::keyClick(source,Qt::Key_Escape);
        source->setFocus();QTest::keyClicks(source,"+(");const auto draft=source->text();pick->trigger();QVERIFY(pick->isChecked());QTest::keyClick(source,Qt::Key_Escape);QVERIFY(!pick->isChecked());QCOMPARE(source->text(),draft);QTest::keyClick(source,Qt::Key_Escape);window.findChild<QAction *>("unfreezePanesAction")->trigger();
        grid->setCurrentIndex(model->index(2,0));window.findChild<QAction *>("hideRowsAction")->trigger();QVERIFY(grid->isRowHidden(2));grid->setCurrentIndex(model->index(3,3));QTRY_VERIFY_WITH_TIMEOUT(source->isEnabled(),5000);source->setFocus();source->selectAll();QTest::keyClicks(source,"=SUM(");pick->trigger();drag(grid,1,0,grid,3,1);QCOMPARE(source->text(),QString("=SUM("));QVERIFY(window.findChild<QPlainTextEdit *>("operationIssues")->toPlainText().contains("Show hidden cells before picking"));QTest::keyClick(source,Qt::Key_Escape);QCOMPARE(edits.count(),1);
    }
    void referencePickingTransfersCellDraftWithoutCommitAndRespectsTextAndReadOnly() {
        QTemporaryDir temp;StartupOptions options;options.automaticWorkbook=false;MainWindow window(options);window.show();QSignalSpy ready(&window,&MainWindow::serviceReady);QTRY_COMPARE_WITH_TIMEOUT(ready.count(),1,10000);QSignalSpy opened(&window,&MainWindow::workbookReady);const auto path=temp.filePath("pick-cell.dhwb");window.openBook(path,true);QTRY_COMPARE_WITH_TIMEOUT(opened.count(),1,10000);auto *model=window.model();auto *grid=window.findChild<QTableView *>("grid");auto *source=window.findChild<QLineEdit *>("source");auto *pick=window.findChild<QAction *>("pickReferenceAction");QSignalSpy edits(&window,&MainWindow::operationFinished);
        grid->setCurrentIndex(model->index(4,4));QTRY_VERIFY_WITH_TIMEOUT(model->sourceReady(grid->currentIndex()),5000);grid->edit(grid->currentIndex());auto *editor=grid->findChild<QLineEdit *>();QVERIFY(editor);editor->setFocus();QTest::keyClicks(editor,"=SUM(");QTest::mouseClick(window.findChild<QToolButton *>("pickReferenceButton"),Qt::LeftButton);QVERIFY(pick->isChecked());QCOMPARE(source->text(),QString("=SUM("));QCOMPARE(grid->currentIndex(),model->index(4,4));QTest::mouseClick(grid->viewport(),Qt::LeftButton,Qt::NoModifier,grid->visualRect(model->index(0,0)).center());QCOMPARE(source->text(),QString("=SUM(A1"));QVERIFY(edits.isEmpty());QTest::keyClicks(source,")");QTest::keyClick(source,Qt::Key_Return);QTRY_COMPARE_WITH_TIMEOUT(opened.count(),2,10000);QTRY_COMPARE_WITH_TIMEOUT(model->data(model->index(4,4)).toString(),QString("0"),5000);
        window.submit({{"action","edit"},{"cells",QJsonArray{QJsonObject{{"row",1},{"col",1},{"source","=SUM("},{"kind","text"}}}}});QTRY_COMPARE_WITH_TIMEOUT(opened.count(),3,10000);grid->setCurrentIndex(model->index(0,0));QTRY_VERIFY_WITH_TIMEOUT(source->isEnabled(),5000);source->setFocus();pick->trigger();QVERIFY(!pick->isChecked());QCOMPARE(source->text(),QString("=SUM("));MainWindow readonly(options);readonly.show();QSignalSpy readReady(&readonly,&MainWindow::serviceReady);QTRY_COMPARE_WITH_TIMEOUT(readReady.count(),1,10000);QSignalSpy readOpened(&readonly,&MainWindow::workbookReady);readonly.openBook(path,false,true);QTRY_COMPARE_WITH_TIMEOUT(readOpened.count(),1,10000);auto *readPick=readonly.findChild<QAction *>("pickReferenceAction");readPick->trigger();QVERIFY(!readPick->isChecked());QCOMPARE(edits.count(),2);
    }
    void formulaCursorContextUnderstandsNestedCallsQuotesAndReplacementSpans() {
        auto c=formulaCursorContext("=SUM(1,IF(TRUE,RO",17);QCOMPARE(c.function,QString("IF"));QCOMPARE(c.argument,2);QCOMPARE(c.prefix,QString("RO"));
        c=formulaCursorContext("=SUM(\"a,b(\",1)",13);QCOMPARE(c.function,QString("SUM"));QCOMPARE(c.argument,2);
        c=formulaCursorContext("='Sheet, (name)'!A1+ROund(1,2)",22);QVERIFY(!c.quoted);QVERIFY(c.start>=0);
        c=formulaCursorContext("=SUM(\"IF",8);QVERIFY(c.quoted);QCOMPARE(c.start,-1);QCOMPARE(formulaCursorContext("plain SUM",9).start,-1);
    }
    void formulaAutocompleteAndHintsStayDraftsInSourceCellAndFrozenEditors() {
        QTemporaryDir temp;StartupOptions options;options.automaticWorkbook=false;MainWindow window(options);window.show();QSignalSpy ready(&window,&MainWindow::serviceReady);QTRY_COMPARE_WITH_TIMEOUT(ready.count(),1,10000);QSignalSpy opened(&window,&MainWindow::workbookReady);window.openBook(temp.filePath("assist.dhwb"),true);QTRY_COMPARE_WITH_TIMEOUT(opened.count(),1,10000);
        auto *source=dynamic_cast<FormulaLineEdit *>(window.findChild<QLineEdit *>("source"));QVERIFY(source);QTRY_VERIFY_WITH_TIMEOUT(source->functionNames().contains("SUM"),5000);auto *grid=window.findChild<QTableView *>("grid");auto *model=window.model();QSignalSpy edits(&window,&MainWindow::operationFinished);QTRY_VERIFY_WITH_TIMEOUT(source->isEnabled(),5000);
        source->setFocus();QTest::keyClicks(source,"=SUM");QTRY_VERIFY_WITH_TIMEOUT(source->completionVisible(),3000);QTest::keyClick(source,Qt::Key_Tab);QCOMPARE(source->text(),QString("=SUM("));QVERIFY(source->isModified());QVERIFY(edits.isEmpty());QVERIFY(source->argumentHint().contains("Argument 1"));QTest::keyClicks(source,"1,2)");QVERIFY(source->argumentHint().isEmpty());QTest::keyClick(source,Qt::Key_Return);QTRY_COMPARE_WITH_TIMEOUT(opened.count(),2,10000);QTRY_COMPARE_WITH_TIMEOUT(model->data(model->index(0,0)).toString(),QString("3"),5000);
        grid->setCurrentIndex(model->index(1,0));QTRY_VERIFY_WITH_TIMEOUT(source->isEnabled(),5000);source->setFocus();QTest::keyClicks(source,"=RO");QTRY_VERIFY_WITH_TIMEOUT(source->completionVisible(),3000);QTest::keyClick(source,Qt::Key_Escape);QCOMPARE(source->text(),QString("=RO"));QTest::keyClick(source,Qt::Key_Escape);QCOMPARE(source->text(),QString());
        grid->setCurrentIndex(model->index(2,0));QTRY_VERIFY_WITH_TIMEOUT(model->sourceReady(grid->currentIndex()),5000);grid->edit(grid->currentIndex());auto *editor=dynamic_cast<FormulaLineEdit *>(grid->findChild<QLineEdit *>());QVERIFY(editor);editor->setFocus();QTest::keyClicks(editor,"=AV");QTRY_VERIFY_WITH_TIMEOUT(editor->completionVisible(),3000);QTest::keyClick(editor,Qt::Key_Return);QCOMPARE(editor->text(),QString("=AVERAGE("));QCOMPARE(edits.count(),1);QVERIFY(editor->argumentHint().contains("AVERAGE"));QTest::keyClicks(editor,"2,4)");QTest::keyClick(editor,Qt::Key_Return);QTRY_COMPARE_WITH_TIMEOUT(opened.count(),3,10000);QTRY_COMPARE_WITH_TIMEOUT(model->data(model->index(2,0)).toString(),QString("3"),5000);
        window.findChild<QAction *>("freezeFirstColumnAction")->trigger();auto *left=window.findChild<QTableView *>("frozenFirstColumn");grid->setCurrentIndex(model->index(3,0));left->setCurrentIndex(left->model()->index(3,0));QTRY_VERIFY_WITH_TIMEOUT(model->sourceReady(model->index(3,0)),5000);left->edit(left->currentIndex());editor=dynamic_cast<FormulaLineEdit *>(left->findChild<QLineEdit *>());QVERIFY(editor);editor->setFocus();QTest::keyClicks(editor,"=AB");QTRY_VERIFY_WITH_TIMEOUT(editor->completionVisible(),3000);QTest::keyClick(editor,Qt::Key_Tab);QCOMPARE(editor->text(),QString("=ABS("));QTest::keyClicks(editor,"-9)");QTest::keyClick(editor,Qt::Key_Return);QTRY_COMPARE_WITH_TIMEOUT(opened.count(),4,10000);QTRY_COMPARE_WITH_TIMEOUT(model->data(model->index(3,0)).toString(),QString("9"),5000);
        window.submit({{"action","edit"},{"cells",QJsonArray{QJsonObject{{"row",5},{"col",1},{"source","=SU"},{"kind","text"}}}}});QTRY_COMPARE_WITH_TIMEOUT(opened.count(),5,10000);grid->setCurrentIndex(model->index(4,0));QTRY_VERIFY_WITH_TIMEOUT(source->isEnabled(),5000);source->setFocus();QVERIFY(!source->formulaAllowed());QTest::keyClick(source,Qt::Key_End);QTest::keyClicks(source,"M");QVERIFY(!source->completionVisible());QTest::keyClick(source,Qt::Key_Escape);QCOMPARE(edits.count(),4);
    }
    void functionHelpReadsServiceCatalogSearchesExamplesWithoutEditing() {
        StartupOptions options;options.automaticWorkbook=false;MainWindow window(options);window.show();QSignalSpy ready(&window,&MainWindow::serviceReady);QTRY_COMPARE_WITH_TIMEOUT(ready.count(),1,10000);QSignalSpy edits(&window,&MainWindow::operationFinished);
        bool reviewed=false;QTimer timer;connect(&timer,&QTimer::timeout,&window,[&]{if(auto *dialog=qobject_cast<QDialog *>(QApplication::activeModalWidget()))if(dialog->objectName()=="functionHelpDialog"){
            auto *search=dialog->findChild<QLineEdit *>("functionSearch");auto *list=dialog->findChild<QListWidget *>("functionList");auto *details=dialog->findChild<QPlainTextEdit *>("functionDetails");QVERIFY(list->count()>=15);search->setText("iferror");QVERIFY(list->currentItem());QCOMPARE(list->currentItem()->text(),QString("IFERROR"));QVERIFY(details->toPlainText().contains("IFERROR(value, fallback)"));QVERIFY(details->toPlainText().contains("=IFERROR(1/0,9)"));search->setText("countif");QVERIFY(details->toPlainText().contains("Input B1: Pen"));search->setText("missing-function");QCOMPARE(list->currentRow(),-1);QVERIFY(details->toPlainText().isEmpty());reviewed=true;dialog->reject();}});timer.start(10);window.findChild<QAction *>("functionHelpAction")->trigger();QTRY_VERIFY_WITH_TIMEOUT(reviewed,5000);QVERIFY(edits.isEmpty());
    }
    void namedListCriteriaValidationRejectsMalformedPreferencesWithoutClamping() {
        QJsonObject output;const QJsonObject good{{"r1",1},{"r2",4},{"c1",1},{"c2",2},{"header",false},{"sort",QJsonArray{QJsonObject{{"col",1},{"descending",true}}}},{"filters",QJsonArray{QJsonObject{{"col",2},{"op","="},{"value",QJsonValue::Null}},QJsonObject{{"col",1},{"op","="},{"value",""}}}}};
        QVERIFY(normalizedListView(good,output));QVERIFY(output["filters"].toArray()[0].toObject()["value"].isNull());QVERIFY(output["filters"].toArray()[1].toObject()["value"].isString());
        for(const auto &change:{QJsonObject{{"r2",1048576}},QJsonObject{{"c1",1.5}},QJsonObject{{"sort",QJsonValue::Null}},QJsonObject{{"header",1}},QJsonObject{{"workbook","old-handle"}},QJsonObject{{"filters",QJsonArray{QJsonObject{{"col",3},{"op","="},{"value",1}}}}},QJsonObject{{"filters",QJsonArray{QJsonObject{{"col",1},{"op","contains"},{"value",false}}}}}}){auto invalid=good;for(auto it=change.begin();it!=change.end();++it)invalid[it.key()]=it.value();QVERIFY(!normalizedListView(invalid,output));}
        QVERIFY(validListViewName("High quantity"));QVERIFY(!validListViewName(" High"));QVERIFY(!validListViewName(QString(65,'x')));QVERIFY(!validListViewName("line\nbreak"));
    }
    void namedListViewsRejectOversizeWithoutReplacingSavedPreferences() {
        QTemporaryDir temp;StartupOptions options;options.automaticWorkbook=false;options.viewStatePath=temp.filePath("views.ini");MainWindow window(options);window.show();QSignalSpy ready(&window,&MainWindow::serviceReady);QTRY_COMPARE_WITH_TIMEOUT(ready.count(),1,10000);QSignalSpy opened(&window,&MainWindow::workbookReady);window.openBook(temp.filePath("bytes.dhwb"),true);QTRY_COMPARE_WITH_TIMEOUT(opened.count(),1,10000);
        window.showList({{"r1",1},{"r2",1},{"c1",1},{"c2",1}});QTRY_COMPARE_WITH_TIMEOUT(opened.count(),2,10000);window.saveNamedListView("Small");QJsonArray filters;for(int i=0;i<3;++i)filters.append(QJsonObject{{"col",1},{"op","="},{"value",QString(50000,'x')}});
        window.showList({{"r1",1},{"r2",1},{"c1",1},{"c2",1},{"filters",filters}});QTRY_COMPARE_WITH_TIMEOUT(opened.count(),3,10000);window.saveNamedListView("Too large");QVERIFY(window.findChild<QPlainTextEdit *>("operationIssues")->toPlainText().contains("128 KiB"));QSettings settings(options.viewStatePath,QSettings::IniFormat);const auto saved=QJsonDocument::fromJson(settings.value(settings.allKeys().first()).toByteArray()).object()["named_views"].toObject();QCOMPARE(saved.size(),1);QVERIFY(saved.contains("Small"));
    }
    void namedListViewsPersistReopenReviewAndReevaluateCurrentInputsReadOnly() {
        QTemporaryDir temp;StartupOptions options;options.automaticWorkbook=false;options.viewStatePath=temp.filePath("views.ini");const auto path=temp.filePath("named.dhwb");QString key;
        {
            MainWindow window(options);window.show();QSignalSpy ready(&window,&MainWindow::serviceReady);QTRY_COMPARE_WITH_TIMEOUT(ready.count(),1,10000);QSignalSpy opened(&window,&MainWindow::workbookReady);window.openBook(path,true);QTRY_COMPARE_WITH_TIMEOUT(opened.count(),1,10000);
            window.submit({{"action","edit"},{"cells",QJsonArray{QJsonObject{{"row",1},{"col",1},{"source","ID"}},QJsonObject{{"row",1},{"col",2},{"source","Qty"}},QJsonObject{{"row",2},{"col",1},{"source","00123"},{"kind","text"}},QJsonObject{{"row",2},{"col",2},{"source","2"}},QJsonObject{{"row",3},{"col",1},{"source","Z"}},QJsonObject{{"row",3},{"col",2},{"source","3"}}}}});QTRY_COMPARE_WITH_TIMEOUT(opened.count(),2,10000);
            window.showList({{"r1",1},{"r2",3},{"c1",1},{"c2",2},{"header",true},{"sort",QJsonArray{QJsonObject{{"col",2},{"descending",true}}}},{"filters",QJsonArray{QJsonObject{{"col",2},{"op",">"},{"value",1}}}}});QTRY_COMPARE_WITH_TIMEOUT(opened.count(),3,10000);QSignalSpy changes(&window,&MainWindow::operationFinished);
            window.saveNamedListView("High qty");for(int i=0;i<15;++i)window.saveNamedListView("View "+QString::number(i));window.saveNamedListView("Overflow");QVERIFY(window.findChild<QPlainTextEdit *>("operationIssues")->toPlainText().contains("16 names"));window.saveNamedListView("High qty");QVERIFY(window.findChild<QPlainTextEdit *>("operationIssues")->toPlainText().contains("already exists"));window.removeNamedListView("View 0");window.saveNamedListView("Replacement");QVERIFY(changes.isEmpty());
            QSettings settings(options.viewStatePath,QSettings::IniFormat);key=settings.allKeys().first();const auto saved=QJsonDocument::fromJson(settings.value(key).toByteArray()).object()["named_views"].toObject();QCOMPARE(saved.size(),16);QVERIFY(!saved.contains("Overflow"));QVERIFY(saved.contains("Replacement"));const auto criteria=saved["High qty"].toObject();QVERIFY(!criteria.contains("expected_state"));QVERIFY(!criteria.contains("workbook"));QCOMPARE(criteria["r2"].toInt(),3);
            window.submit({{"action","edit"},{"cells",QJsonArray{QJsonObject{{"row",2},{"col",2},{"source","0"}}}}});QTRY_COMPARE_WITH_TIMEOUT(opened.count(),4,10000);window.findChild<QAction *>("clearListAction")->trigger();QTRY_COMPARE_WITH_TIMEOUT(opened.count(),5,10000);window.findChild<QAction *>("freezeFirstColumnAction")->trigger();
        }
        {
            MainWindow window(options);window.show();QSignalSpy ready(&window,&MainWindow::serviceReady);QTRY_COMPARE_WITH_TIMEOUT(ready.count(),1,10000);QSignalSpy opened(&window,&MainWindow::workbookReady);window.openBook(path,false,true);QTRY_COMPARE_WITH_TIMEOUT(opened.count(),1,10000);QSignalSpy changes(&window,&MainWindow::operationFinished);
            QTimer::singleShot(0,&window,[&]{auto *dialog=qobject_cast<QDialog *>(QApplication::activeModalWidget());QVERIFY(dialog);QCOMPARE(dialog->objectName(),QString("listCriteriaDialog"));QVERIFY(dialog->windowTitle().contains("review source coordinates"));dialog->reject();});window.applyNamedListView("High qty");QVERIFY(!window.model()->projected());
            QTimer::singleShot(0,&window,[&]{auto *dialog=qobject_cast<QDialog *>(QApplication::activeModalWidget());QVERIFY(dialog);QVERIFY(dialog->findChild<QCheckBox *>("listHeader")->isChecked());QCOMPARE(dialog->findChild<QTableWidget *>("filterCriteria")->rowCount(),1);dialog->findChild<QDialogButtonBox *>()->button(QDialogButtonBox::Ok)->click();});window.applyNamedListView("High qty");QTRY_COMPARE_WITH_TIMEOUT(opened.count(),2,10000);QVERIFY(window.model()->projected());QCOMPARE(window.model()->rowCount(),2);QCOMPARE(window.model()->sourceRow(1),2);QVERIFY(changes.isEmpty());
            window.removeNamedListView("High qty");QVERIFY(window.model()->projected());QSettings settings(options.viewStatePath,QSettings::IniFormat);QVERIFY(!QJsonDocument::fromJson(settings.value(key).toByteArray()).object()["named_views"].toObject().contains("High qty"));
        }
        {QSettings settings(options.viewStatePath,QSettings::IniFormat);auto state=QJsonDocument::fromJson(settings.value(key).toByteArray()).object();auto views=state["named_views"].toObject();views["Broken"]=QJsonObject{{"r1",1},{"r2",1000000},{"c1",1},{"c2",10}};state["named_views"]=views;settings.setValue(key,QJsonDocument(state).toJson(QJsonDocument::Compact));settings.sync();}
        {MainWindow window(options);window.show();QSignalSpy ready(&window,&MainWindow::serviceReady);QTRY_COMPARE_WITH_TIMEOUT(ready.count(),1,10000);QSignalSpy opened(&window,&MainWindow::workbookReady);window.openBook(path,false,true);QTRY_COMPARE_WITH_TIMEOUT(opened.count(),1,10000);QVERIFY(window.findChild<QPlainTextEdit *>("operationIssues")->toPlainText().contains("Invalid saved list view"));window.applyNamedListView("Broken");QVERIFY(window.findChild<QPlainTextEdit *>("operationIssues")->toPlainText().contains("unavailable or invalid"));}
    }
    void filteredExportWritesOnlyReviewedVisibleSnapshotAndRetainsDestinationOnCancelError() {
        QTemporaryDir temp;StartupOptions options;options.automaticWorkbook=false;MainWindow window(options);window.show();QSignalSpy ready(&window,&MainWindow::serviceReady);QTRY_COMPARE_WITH_TIMEOUT(ready.count(),1,10000);QSignalSpy opened(&window,&MainWindow::workbookReady);
        const auto native=temp.filePath("list-export.dhwb");window.openBook(native,true);QTRY_COMPARE_WITH_TIMEOUT(opened.count(),1,10000);window.submit({{"action","edit"},{"cells",QJsonArray{
            QJsonObject{{"row",1},{"col",1},{"source","ID"}},QJsonObject{{"row",1},{"col",2},{"source","Value"}},QJsonObject{{"row",2},{"col",1},{"source","00123"},{"kind","text"}},QJsonObject{{"row",2},{"col",2},{"source","=1/4"},{"style",QJsonObject{{"format","percent2"}}}},QJsonObject{{"row",3},{"col",1},{"source","Z"}},QJsonObject{{"row",3},{"col",2},{"source","TRUE"}}}}});QTRY_COMPARE_WITH_TIMEOUT(opened.count(),2,10000);
        window.showList({{"r1",1},{"r2",3},{"c1",1},{"c2",2},{"header",true},{"sort",QJsonArray{QJsonObject{{"col",1},{"descending",true}}}}});QTRY_COMPARE_WITH_TIMEOUT(opened.count(),3,10000);QSignalSpy edits(&window,&MainWindow::operationFinished);
        bool accept=true;int reviews=0;QTimer timer;connect(&timer,&QTimer::timeout,&window,[&]{if(auto *box=qobject_cast<QMessageBox *>(QApplication::activeModalWidget()))if(box->objectName()=="listExportReview"){QVERIFY(box->informativeText().contains("formula inputs converted"));++reviews;box->button(accept?QMessageBox::Yes:QMessageBox::Cancel)->click();}});timer.start(10);
        for(const auto &format:{QString("csv"),QString("xlsx"),QString("ods")}){const auto path=temp.filePath("result."+format);window.exportListTo(path,format);QTRY_VERIFY_WITH_TIMEOUT(QFile::exists(path),5000);QFile file(path);QVERIFY(file.open(QIODevice::ReadOnly));const auto bytes=file.readAll();if(format=="csv")QCOMPARE(bytes,QByteArray("ID,Value\r\nZ,TRUE\r\n00123,0.25\r\n"));else QVERIFY(bytes.startsWith("PK"));}
        QCOMPARE(reviews,3);QVERIFY(edits.isEmpty());const auto keep=temp.filePath("keep.csv");{QFile file(keep);QVERIFY(file.open(QIODevice::WriteOnly));file.write("keep");}
        accept=false;window.exportListTo(keep);QTRY_COMPARE_WITH_TIMEOUT(reviews,4,5000);QTest::qWait(20);{QFile file(keep);QVERIFY(file.open(QIODevice::ReadOnly));QCOMPARE(file.readAll(),QByteArray("keep"));}
        window.exportListTo(native);QVERIFY(window.findChild<QPlainTextEdit *>("operationIssues")->toPlainText().contains("destination is the native workbook"));
        window.submit({{"action","edit"},{"cells",QJsonArray{QJsonObject{{"row",2},{"col",2},{"source","=1/0"}}}}});QTRY_COMPARE_WITH_TIMEOUT(opened.count(),4,10000);window.exportListTo(keep);QTRY_VERIFY_WITH_TIMEOUT(window.findChild<QPlainTextEdit *>("operationIssues")->toPlainText().contains("List values blocked"),5000);QCOMPARE(reviews,4);QFile file(keep);QVERIFY(file.open(QIODevice::ReadOnly));QCOMPARE(file.readAll(),QByteArray("keep"));
    }
    void filteredCopyUsesDisplayedValuesAndStylesAndPreservesClipboardOnEmpty() {
        QTemporaryDir temp;StartupOptions options;options.automaticWorkbook=false;MainWindow window(options);window.show();QSignalSpy ready(&window,&MainWindow::serviceReady);QTRY_COMPARE_WITH_TIMEOUT(ready.count(),1,10000);
        QSignalSpy opened(&window,&MainWindow::workbookReady);window.openBook(temp.filePath("list-copy.dhwb"),true);QTRY_COMPARE_WITH_TIMEOUT(opened.count(),1,10000);
        window.submit({{"action","edit"},{"cells",QJsonArray{QJsonObject{{"row",1},{"col",1},{"source","ID"}},QJsonObject{{"row",1},{"col",2},{"source","Value"}},
            QJsonObject{{"row",2},{"col",1},{"source","00123"},{"kind","text"}},QJsonObject{{"row",2},{"col",2},{"source","=1/4"},{"style",QJsonObject{{"format","percent2"}}}},
            QJsonObject{{"row",3},{"col",1},{"source","Z"}},QJsonObject{{"row",3},{"col",2},{"source","TRUE"}}}}});QTRY_COMPARE_WITH_TIMEOUT(opened.count(),2,10000);
        auto *grid=window.findChild<QTableView *>("grid");auto *model=window.model();QSignalSpy edits(&window,&MainWindow::operationFinished);
        window.showList({{"r1",1},{"r2",3},{"c1",1},{"c2",2},{"header",true},{"sort",QJsonArray{QJsonObject{{"col",1},{"descending",true}}}}});QTRY_COMPARE_WITH_TIMEOUT(opened.count(),3,10000);
        QApplication::clipboard()->setText("before");window.copyListValues();QTRY_COMPARE_WITH_TIMEOUT(QApplication::clipboard()->text(),QString("ID\tValue\nZ\tTRUE\n00123\t25.00%"),5000);QVERIFY(edits.isEmpty());
        const auto payload=QJsonDocument::fromJson(QApplication::clipboard()->mimeData()->data("application/x-datahorizon-cells")).object();auto copied=payload["rows"].toArray();QCOMPARE(copied[2].toArray()[1].toObject()["input_kind"].toString(),QString("number"));QCOMPARE(copied[2].toArray()[1].toObject()["value"].toDouble(),.25);
        grid->selectionModel()->select(QItemSelection(model->index(1,0),model->index(2,1)),QItemSelectionModel::ClearAndSelect);grid->setFocus();QAction *copy=nullptr;for(auto *action:window.findChildren<QAction *>())if(action->shortcut().matches(QKeySequence::Copy)==QKeySequence::ExactMatch)copy=action;QVERIFY(copy);copy->trigger();QTRY_COMPARE_WITH_TIMEOUT(QApplication::clipboard()->text(),QString("Z\tTRUE\n00123\t25.00%"),5000);
        window.findChild<QAction *>("clearListAction")->trigger();QTRY_COMPARE_WITH_TIMEOUT(opened.count(),4,10000);grid->setCurrentIndex(model->index(9,0));grid->setFocus();window.findChild<QAction *>("pasteAction")->trigger();QTRY_COMPARE_WITH_TIMEOUT(opened.count(),5,10000);
        QTRY_VERIFY_WITH_TIMEOUT(model->sourceReady(model->index(10,1)),5000);QCOMPARE(model->cell(model->index(10,0))["input_kind"].toString(),QString("text"));QCOMPARE(model->cell(model->index(10,0))["source"].toString(),QString("00123"));QCOMPARE(model->cell(model->index(10,1))["source"].toString(),QString("0.25"));QCOMPARE(model->cell(model->index(10,1))["style"].toObject()["format"].toString(),QString("percent2"));
        grid->setCurrentIndex(model->index(1,0));grid->selectionModel()->select(QItemSelection(model->index(1,0),model->index(1,1)),QItemSelectionModel::ClearAndSelect);QVERIFY(!grid->selectionModel()->selection().isEmpty());window.findChild<QAction *>("hideRowsAction")->trigger();QVERIFY2(grid->isRowHidden(1),qPrintable(window.findChild<QPlainTextEdit *>("operationIssues")->toPlainText()));window.showList({{"r1",1},{"r2",3},{"c1",1},{"c2",2},{"header",true}});QTRY_COMPARE_WITH_TIMEOUT(opened.count(),6,10000);window.copyListValues();QTRY_COMPARE_WITH_TIMEOUT(QApplication::clipboard()->text(),QString("ID\tValue\nZ\tTRUE"),5000);
        window.showList({{"r1",1},{"r2",3},{"c1",1},{"c2",2},{"filters",QJsonArray{QJsonObject{{"col",1},{"op","="},{"value","missing"}}}}});QTRY_COMPARE_WITH_TIMEOUT(opened.count(),7,10000);const auto prior=QApplication::clipboard()->text();window.copyListValues();QTRY_VERIFY_WITH_TIMEOUT(window.findChild<QPlainTextEdit *>("operationIssues")->toPlainText().contains("No visible list values"),5000);QCOMPARE(QApplication::clipboard()->text(),prior);
    }
    void filteredCopyIgnoresSupersededClipboardAndServiceReplies() {
        QTemporaryDir temp;StartupOptions options;options.automaticWorkbook=false;MainWindow window(options);window.show();QSignalSpy ready(&window,&MainWindow::serviceReady);QTRY_COMPARE_WITH_TIMEOUT(ready.count(),1,10000);
        QSignalSpy opened(&window,&MainWindow::workbookReady);window.openBook(temp.filePath("late-list.dhwb"),true);QTRY_COMPARE_WITH_TIMEOUT(opened.count(),1,10000);window.showList({{"r1",1},{"r2",2},{"c1",1},{"c2",1}});QTRY_COMPARE_WITH_TIMEOUT(opened.count(),2,10000);
        auto *model=window.model();DelayedClipboardServer server(model->state(),model->sheet());QVERIFY(server.listen(QHostAddress::LocalHost,0));auto *client=window.findChild<RestClient *>();client->configure("http://127.0.0.1:"+QString::number(server.serverPort()),"token");
        window.copyListValues();QTRY_COMPARE_WITH_TIMEOUT(server.requests.size(),1,3000);QApplication::clipboard()->setText("new external clipboard");server.respondList(0,"stale");QTest::qWait(100);QCOMPARE(QApplication::clipboard()->text(),QString("new external clipboard"));
        window.copyListValues();QTRY_COMPARE_WITH_TIMEOUT(server.requests.size(),2,3000);client->configure("http://127.0.0.1:1","other");server.respondList(1,"wrong service");QTest::qWait(100);QCOMPARE(QApplication::clipboard()->text(),QString("new external clipboard"));
    }
    void multiCriteriaEditorPreservesScalarTypesOrderAndLimits() {
        QJsonArray keys;for(int col=1;col<=8;++col)keys.append(QJsonObject{{"col",col},{"descending",col==8}});
        const QJsonArray filters{QJsonObject{{"col",1},{"op","="},{"value","00123"}},QJsonObject{{"col",2},{"op",">"},{"value",1.25}},
            QJsonObject{{"col",3},{"op","="},{"value",false}},QJsonObject{{"col",4},{"op","="}},QJsonObject{{"col",5},{"op","="},{"value",""}}};
        ListCriteriaDialog dialog({{"r1",1},{"c1",1},{"r2",10},{"c2",8},{"header",false},{"sort",keys},{"filters",filters}});
        auto *sort=dialog.findChild<QTableWidget *>("sortCriteria"),*table=dialog.findChild<QTableWidget *>("filterCriteria");
        QCOMPARE(sort->rowCount(),8);QVERIFY(!dialog.findChild<QPushButton *>("addSort")->isEnabled());
        sort->setCurrentCell(7,0);dialog.findChild<QPushButton *>("sortUp")->click();QCOMPARE(qobject_cast<QSpinBox *>(sort->cellWidget(6,0))->value(),8);
        QCOMPARE(qobject_cast<QComboBox *>(sort->cellWidget(6,1))->currentIndex(),1);
        for(int i=0;i<4;++i)dialog.findChild<QPushButton *>("addFilter")->click();QCOMPARE(table->rowCount(),8);QVERIFY(!dialog.findChild<QPushButton *>("addFilter")->isEnabled());
        table->setCurrentCell(7,0);dialog.findChild<QPushButton *>("removeFilter")->click();QVERIFY(dialog.findChild<QPushButton *>("addFilter")->isEnabled());
        auto *number=qobject_cast<QLineEdit *>(table->cellWidget(1,3));number->setText("not a number");
        auto *ok=dialog.findChild<QDialogButtonBox *>()->button(QDialogButtonBox::Ok);ok->click();QVERIFY(dialog.result()!=QDialog::Accepted);QVERIFY(dialog.findChild<QLabel *>("criteriaError")->text().contains("finite"));
        number->setText("2.5");ok->click();QCOMPARE(dialog.result(),int(QDialog::Accepted));const auto result=dialog.scope();
        QCOMPARE(result["filters"].toArray()[0].toObject()["value"].toString(),QString("00123"));QCOMPARE(result["filters"].toArray()[1].toObject()["value"].toDouble(),2.5);
        QVERIFY(result["filters"].toArray()[2].toObject()["value"].isBool());QVERIFY(!result["filters"].toArray()[2].toObject()["value"].toBool());QVERIFY(result["filters"].toArray()[3].toObject()["value"].isNull());
        QCOMPARE(result["filters"].toArray()[4].toObject()["value"].toString(),QString());QCOMPARE(result["sort"].toArray()[6].toObject()["col"].toInt(),8);
    }
    void multiCriteriaDialogAppliesEditsAndCancelsAnExistingProjection() {
        QTemporaryDir temp;StartupOptions options;options.automaticWorkbook=false;
        MainWindow window(options);window.show();QSignalSpy ready(&window,&MainWindow::serviceReady);QTRY_COMPARE_WITH_TIMEOUT(ready.count(),1,10000);
        QSignalSpy opened(&window,&MainWindow::workbookReady);window.openBook(temp.filePath("criteria.dhwb"),true);QTRY_COMPARE_WITH_TIMEOUT(opened.count(),1,10000);
        const QList<QStringList> rows{{"ID","Group","Qty","Flag"},{"b","X","2","TRUE"},{"a","X","3","TRUE"},{"c","Y","4","FALSE"},{"d","X","1","TRUE"}};
        QJsonArray cells;for(int row=0;row<rows.size();++row)for(int col=0;col<4;++col)cells.append(QJsonObject{{"row",row+1},{"col",col+1},{"source",rows[row][col]}});
        window.submit({{"action","edit"},{"cells",cells}});QTRY_COMPARE_WITH_TIMEOUT(opened.count(),2,10000);QSignalSpy changed(&window,&MainWindow::operationFinished);
        auto *model=window.model();auto *grid=window.findChild<QTableView *>("grid");grid->selectionModel()->select(QItemSelection(model->index(0,0),model->index(4,3)),QItemSelectionModel::ClearAndSelect);
        QTimer answer;answer.setInterval(10);bool filled=false;
        connect(&answer,&QTimer::timeout,&window,[&]{if(auto *dialog=qobject_cast<QDialog *>(QApplication::activeModalWidget());dialog && dialog->objectName()=="listCriteriaDialog" && !filled){
            filled=true;auto *sort=dialog->findChild<QTableWidget *>("sortCriteria"),*filters=dialog->findChild<QTableWidget *>("filterCriteria");
            for(int i=0;i<3;++i){dialog->findChild<QPushButton *>("addSort")->click();dialog->findChild<QPushButton *>("addFilter")->click();}
            for(int i=0;i<3;++i)qobject_cast<QSpinBox *>(sort->cellWidget(i,0))->setValue(QList<int>{2,3,1}[i]);qobject_cast<QComboBox *>(sort->cellWidget(1,1))->setCurrentIndex(1);
            for(int i=0;i<3;++i){qobject_cast<QSpinBox *>(filters->cellWidget(i,0))->setValue(i+2);qobject_cast<QComboBox *>(filters->cellWidget(i,2))->setCurrentIndex(i);}
            qobject_cast<QLineEdit *>(filters->cellWidget(0,3))->setText("X");qobject_cast<QComboBox *>(filters->cellWidget(1,1))->setCurrentText(">");qobject_cast<QLineEdit *>(filters->cellWidget(1,3))->setText("1");qobject_cast<QLineEdit *>(filters->cellWidget(2,3))->setText("TRUE");
            if(qEnvironmentVariableIsSet("DATAHORIZON_CRITERIA_SCREENSHOT"))dialog->grab().save(qEnvironmentVariable("DATAHORIZON_CRITERIA_SCREENSHOT"));
            dialog->findChild<QDialogButtonBox *>()->button(QDialogButtonBox::Ok)->click();
        }});answer.start();window.findChild<QAction *>("listCriteriaAction")->trigger();answer.stop();QVERIFY(filled);
        QTRY_COMPARE_WITH_TIMEOUT(opened.count(),3,10000);QVERIFY(model->projected());QCOMPARE(model->rowCount(),3);QCOMPARE(model->sourceRow(1),2);QCOMPARE(model->sourceRow(2),1);
        auto *summary=window.findChild<QLabel *>("listCriteriaSummary");QVERIFY(summary->isVisible());QVERIFY(summary->text().contains("B↑, C↓, A↑"));QVERIFY(summary->toolTip().contains("Boolean"));QVERIFY(changed.isEmpty());
        if(qEnvironmentVariableIsSet("DATAHORIZON_CRITERIA_VIEW_SCREENSHOT"))window.grab().save(qEnvironmentVariable("DATAHORIZON_CRITERIA_VIEW_SCREENSHOT"));
        QTimer cancel;cancel.setInterval(10);bool inspected=false;connect(&cancel,&QTimer::timeout,&window,[&]{if(auto *dialog=qobject_cast<QDialog *>(QApplication::activeModalWidget());dialog && dialog->objectName()=="listCriteriaDialog"){
            inspected=dialog->findChild<QTableWidget *>("sortCriteria")->rowCount()==3 && dialog->findChild<QTableWidget *>("filterCriteria")->rowCount()==3;dialog->reject();
        }});cancel.start();window.findChild<QAction *>("listCriteriaAction")->trigger();cancel.stop();QVERIFY(inspected);QCOMPARE(opened.count(),3);QCOMPARE(model->rowCount(),3);
        QTimer edit;edit.setInterval(10);connect(&edit,&QTimer::timeout,&window,[&]{if(auto *dialog=qobject_cast<QDialog *>(QApplication::activeModalWidget());dialog && dialog->objectName()=="listCriteriaDialog"){
            auto *filters=dialog->findChild<QTableWidget *>("filterCriteria");qobject_cast<QLineEdit *>(filters->cellWidget(1,3))->setText("2");dialog->findChild<QDialogButtonBox *>()->button(QDialogButtonBox::Ok)->click();
        }});edit.start();window.findChild<QAction *>("listCriteriaAction")->trigger();edit.stop();QTRY_COMPARE_WITH_TIMEOUT(opened.count(),4,10000);QCOMPARE(model->rowCount(),2);QCOMPARE(model->sourceRow(1),2);QVERIFY(changed.isEmpty());
        window.submit({{"action","edit"},{"cells",QJsonArray{QJsonObject{{"row",3},{"col",3},{"source","=1/0"}}}}});QTRY_COMPARE_WITH_TIMEOUT(opened.count(),5,10000);QVERIFY(!model->projected());QVERIFY(!summary->isVisible());QVERIFY(window.findChild<QPlainTextEdit *>("operationIssues")->toPlainText().contains("List evaluation blocked"));
        window.submit({{"action","undo"}});QTRY_COMPARE_WITH_TIMEOUT(opened.count(),6,10000);QTRY_COMPARE_WITH_TIMEOUT(model->data(model->index(2,2)).toString(),QString("3"),5000);
    }
    void dataBoundaryNavigationExtendsFromSequenceAnchorAndProtectsDrafts() {
        QTemporaryDir temp;StartupOptions options;options.automaticWorkbook=false;options.viewStatePath=temp.filePath("views.ini");
        MainWindow window(options);window.show();QSignalSpy ready(&window,&MainWindow::serviceReady);QTRY_COMPARE_WITH_TIMEOUT(ready.count(),1,10000);
        QSignalSpy opened(&window,&MainWindow::workbookReady);window.openBook(temp.filePath("data-nav.dhwb"),true);QTRY_COMPARE_WITH_TIMEOUT(opened.count(),1,10000);
        QJsonArray cells;for(int row:{1,2,3,10})cells.append(QJsonObject{{"row",row},{"col",1},{"source",QString::number(row)}});
        cells.append(QJsonObject{{"row",10},{"col",2},{"source","20"}});cells.append(QJsonObject{{"row",10},{"col",3},{"source","30"}});
        window.submit({{"action","edit"},{"cells",cells}});QTRY_COMPARE_WITH_TIMEOUT(opened.count(),2,10000);
        auto *model=window.model();auto *grid=window.findChild<QTableView *>("grid");auto *source=window.findChild<QLineEdit *>("source");
        QSignalSpy changed(&window,&MainWindow::operationFinished);
        auto action=[&](QString name){return window.findChild<QAction *>(name);};
        grid->setCurrentIndex(model->index(0,0));grid->setFocus();QTest::keyClick(grid,Qt::Key_Down,Qt::ControlModifier);QTRY_COMPARE_WITH_TIMEOUT(grid->currentIndex(),model->index(2,0),5000);
        action("dataJumpUpAction")->trigger();QTRY_COMPARE_WITH_TIMEOUT(grid->currentIndex(),model->index(0,0),5000);
        action("dataExtendDownAction")->trigger();QTRY_COMPARE_WITH_TIMEOUT(grid->currentIndex(),model->index(2,0),5000);
        action("dataExtendDownAction")->trigger();QTRY_COMPARE_WITH_TIMEOUT(grid->currentIndex(),model->index(9,0),5000);
        action("dataExtendRightAction")->trigger();QTRY_COMPARE_WITH_TIMEOUT(grid->currentIndex(),model->index(9,2),5000);
        QCOMPARE(grid->selectionModel()->selection().first().top(),0);QCOMPARE(grid->selectionModel()->selection().first().left(),0);QCOMPARE(grid->selectionModel()->selection().first().width(),3);
        QTRY_VERIFY_WITH_TIMEOUT(model->sourceReady(model->index(9,2)),5000);source->setText("retained draft");source->setModified(true);
        answerNextDialog(QMessageBox::Cancel);action("dataJumpDownAction")->trigger();QCOMPARE(grid->currentIndex(),model->index(9,2));QCOMPARE(source->text(),QString("retained draft"));
        QTest::keyClick(source,Qt::Key_Escape);grid->selectRow(2);window.findChild<QAction *>("hideRowsAction")->trigger();grid->setCurrentIndex(model->index(0,0));
        action("dataJumpDownAction")->trigger();QTRY_VERIFY_WITH_TIMEOUT(window.findChild<QPlainTextEdit *>("operationIssues")->toPlainText().contains("data boundary is hidden"),5000);QCOMPARE(grid->currentIndex(),model->index(0,0));
        window.findChild<QPushButton *>("showHiddenButton")->click();
        QCOMPARE(action("dataJumpDownAction")->shortcutContext(),Qt::WidgetShortcut);
        QTRY_VERIFY_WITH_TIMEOUT(model->sourceReady(model->index(0,0)),5000);source->setFocus();source->setText("draft words");source->setModified(true);
        bool unexpectedDialog=false;QTimer cancel;cancel.setInterval(10);connect(&cancel,&QTimer::timeout,&window,[&]{if(auto *box=qobject_cast<QMessageBox *>(QApplication::activeModalWidget())){unexpectedDialog=true;if(auto *button=box->button(QMessageBox::Cancel))button->click();}});
        cancel.start();QTest::keyClick(source,Qt::Key_Left,Qt::ControlModifier);QTest::qWait(50);cancel.stop();QVERIFY(!unexpectedDialog);QCOMPARE(source->text(),QString("draft words"));
        QTest::keyClick(source,Qt::Key_Escape);QVERIFY(changed.isEmpty());
        window.showList({{"r1",1},{"r2",10},{"c1",1},{"c2",3},{"header",false}});QTRY_COMPARE_WITH_TIMEOUT(opened.count(),3,10000);QVERIFY(!action("dataJumpDownAction")->isEnabled());
    }
    void dataBoundaryNavigationDiscardsMovedSelectionEditorAndConnectionReplies() {
        QTemporaryDir temp;StartupOptions options;options.automaticWorkbook=false;
        MainWindow window(options);window.show();QSignalSpy ready(&window,&MainWindow::serviceReady);QTRY_COMPARE_WITH_TIMEOUT(ready.count(),1,10000);
        QSignalSpy opened(&window,&MainWindow::workbookReady);window.openBook(temp.filePath("nav-late.dhwb"),true);QTRY_COMPARE_WITH_TIMEOUT(opened.count(),1,10000);
        auto *model=window.model();auto *grid=window.findChild<QTableView *>("grid");QTRY_VERIFY_WITH_TIMEOUT(model->sourceReady(model->index(0,0)),5000);
        DelayedSummaryServer server(model->state(),model->sheet());QVERIFY(server.listen(QHostAddress::LocalHost,0));auto *client=window.findChild<RestClient *>();
        client->configure("http://127.0.0.1:"+QString::number(server.serverPort()),"token");int completed=0;
        connect(client->findChild<QNetworkAccessManager *>(),&QNetworkAccessManager::finished,&window,[&](QNetworkReply *reply){if(reply->url().path().endsWith("/navigate"))++completed;});
        auto *down=window.findChild<QAction *>("dataJumpDownAction");down->trigger();QTRY_COMPARE_WITH_TIMEOUT(server.navigationRequests.size(),1,3000);
        grid->setCurrentIndex(model->index(0,1));server.respondNavigation(0,100,1);QTRY_COMPARE_WITH_TIMEOUT(completed,1,3000);QCOMPARE(grid->currentIndex(),model->index(0,1));
        down->trigger();QTRY_COMPARE_WITH_TIMEOUT(server.navigationRequests.size(),2,3000);QTRY_VERIFY_WITH_TIMEOUT(model->sourceReady(model->index(0,1)),5000);
        grid->edit(model->index(0,1));QPointer<QLineEdit> editor=grid->findChild<QLineEdit *>();QVERIFY(editor);editor->setText("new editor draft");
        server.respondNavigation(1,100,2);QTRY_COMPARE_WITH_TIMEOUT(completed,2,3000);QCOMPARE(grid->currentIndex(),model->index(0,1));QCOMPARE(editor->text(),QString("new editor draft"));
        QTest::keyClick(editor,Qt::Key_Escape);QTRY_VERIFY_WITH_TIMEOUT(editor.isNull(),3000);
        down->trigger();QTRY_COMPARE_WITH_TIMEOUT(server.navigationRequests.size(),3,3000);
        DelayedSummaryServer replacement(model->state(),model->sheet());QVERIFY(replacement.listen(QHostAddress::LocalHost,0));client->configure("http://127.0.0.1:"+QString::number(replacement.serverPort()),"token");
        down->trigger();QTRY_COMPARE_WITH_TIMEOUT(replacement.navigationRequests.size(),1,3000);replacement.respondNavigation(0,5,2);QTRY_COMPARE_WITH_TIMEOUT(grid->currentIndex(),model->index(4,1),3000);
        server.respondNavigation(2,9,2);QTest::qWait(50);QCOMPARE(grid->currentIndex(),model->index(4,1));
    }
    void frozenPaneTabAndEditorTabAdvanceAcrossSourceColumns() {
        QTemporaryDir temp;StartupOptions options;options.automaticWorkbook=false;options.viewStatePath=temp.filePath("views.ini");
        MainWindow window(options);window.show();QSignalSpy ready(&window,&MainWindow::serviceReady);QTRY_COMPARE_WITH_TIMEOUT(ready.count(),1,10000);
        QSignalSpy opened(&window,&MainWindow::workbookReady);window.openBook(temp.filePath("pane-tab.dhwb"),true);QTRY_COMPARE_WITH_TIMEOUT(opened.count(),1,10000);
        auto *model=window.model();auto *grid=window.findChild<QTableView *>("grid");
        window.findChild<QAction *>("freezeTopRowAction")->trigger();window.findChild<QAction *>("freezeFirstColumnAction")->trigger();
        auto *corner=window.findChild<QTableView *>("frozenCorner"),*top=window.findChild<QTableView *>("frozenTopRow"),*left=window.findChild<QTableView *>("frozenFirstColumn");
        QTRY_VERIFY_WITH_TIMEOUT(model->sourceReady(model->index(0,0)),5000);corner->edit(corner->model()->index(0,0));auto *editor=corner->findChild<QLineEdit *>();QVERIFY(editor);editor->setText("=2+3");QTest::keyClick(editor,Qt::Key_Tab);
        QTRY_COMPARE_WITH_TIMEOUT(opened.count(),2,10000);QTRY_COMPARE_WITH_TIMEOUT(grid->currentIndex(),model->index(0,1),5000);QCOMPARE(window.focusWidget(),top);
        QTest::keyClick(top,Qt::Key_Backtab);QCOMPARE(grid->currentIndex(),model->index(0,0));QCOMPARE(window.focusWidget(),corner);
        QTest::keyClick(corner,Qt::Key_Down);QCOMPARE(grid->currentIndex(),model->index(1,0));QTRY_VERIFY_WITH_TIMEOUT(model->sourceReady(model->index(1,0)),5000);
        left->edit(left->model()->index(1,0));editor=left->findChild<QLineEdit *>();QVERIFY(editor);editor->setText("left entry");QTest::keyClick(editor,Qt::Key_Tab);
        QTRY_COMPARE_WITH_TIMEOUT(opened.count(),3,10000);QTRY_COMPARE_WITH_TIMEOUT(grid->currentIndex(),model->index(1,1),5000);QCOMPARE(window.focusWidget(),grid);
        QTRY_COMPARE_WITH_TIMEOUT(model->data(model->index(1,0)).toString(),QString("left entry"),5000);
    }
    void frozenPrefixModelsResizeAndForwardSourceIndices() {
        QStandardItemModel source(5,6);FrozenPrefixModel prefix(&source,2,3);
        QAbstractItemModelTester tester(&prefix,QAbstractItemModelTester::FailureReportingMode::QtTest);
        QCOMPARE(prefix.rowCount(),2);QCOMPARE(prefix.columnCount(),3);
        QVERIFY(prefix.setData(prefix.index(1,2),"value",Qt::EditRole));QCOMPARE(source.data(source.index(1,2)).toString(),QString("value"));
        prefix.setLimits(4,2);QCOMPARE(prefix.rowCount(),4);QCOMPARE(prefix.columnCount(),2);
        source.removeRows(2,3);QCOMPARE(prefix.rowCount(),2);source.insertColumn(0);QCOMPARE(prefix.columnCount(),2);
        prefix.setLimits(0,0);QCOMPARE(prefix.rowCount(),0);QCOMPARE(prefix.columnCount(),0);
    }
    void freezeSelectedPanesPreservesSizesSelectionsAndReopenWithBounds() {
        QTemporaryDir temp;StartupOptions options;options.automaticWorkbook=false;options.viewStatePath=temp.filePath("views.ini");const auto path=temp.filePath("multi-pane.dhwb");
        {
            MainWindow window(options);window.show();QSignalSpy ready(&window,&MainWindow::serviceReady);QTRY_COMPARE_WITH_TIMEOUT(ready.count(),1,10000);
            QSignalSpy opened(&window,&MainWindow::workbookReady);window.openBook(path,true);QTRY_COMPARE_WITH_TIMEOUT(opened.count(),1,10000);
            auto *grid=window.findChild<QTableView *>("grid");auto *model=window.model();
            grid->setRowHeight(1,49);grid->setColumnWidth(1,137);grid->setCurrentIndex(model->index(2,2));
            QSignalSpy changed(&window,&MainWindow::operationFinished);auto *freeze=window.findChild<QAction *>("freezePanesAction");freeze->trigger();
            auto *top=window.findChild<QTableView *>("frozenTopRow"),*left=window.findChild<QTableView *>("frozenFirstColumn"),*corner=window.findChild<QTableView *>("frozenCorner");
            QCOMPARE(top->model()->rowCount(),2);QCOMPARE(left->model()->columnCount(),2);QCOMPARE(corner->model()->rowCount(),2);QCOMPARE(corner->model()->columnCount(),2);
            QVERIFY(grid->isRowHidden(0));QVERIFY(grid->isRowHidden(1));QVERIFY(grid->isColumnHidden(1));QCOMPARE(top->rowHeight(1),49);QCOMPARE(left->columnWidth(1),137);
            auto *address=window.findChild<QLineEdit *>("address");address->setText("A1:D4");QTest::keyClick(address,Qt::Key_Return);
            QCOMPARE(grid->selectionModel()->selection().first().height(),4);QCOMPARE(corner->selectionModel()->selection().first().height(),2);
            address->setText("B2");QTest::keyClick(address,Qt::Key_Return);QCOMPARE(window.focusWidget(),corner);
            QTest::keyClick(corner,Qt::Key_Down,Qt::ShiftModifier);QCOMPARE(grid->currentIndex(),model->index(2,1));QCOMPARE(window.focusWidget(),left);
            QTest::keyClick(left,Qt::Key_Right,Qt::ShiftModifier);QCOMPARE(grid->currentIndex(),model->index(2,2));QCOMPARE(window.focusWidget(),grid);
            grid->setCurrentIndex(model->index(17,9));freeze->trigger();QCOMPARE(top->model()->rowCount(),2);QVERIFY(window.findChild<QPlainTextEdit *>("operationIssues")->toPlainText().contains("16 rows and 8 columns"));
            grid->selectRow(1);window.findChild<QAction *>("hideRowsAction")->trigger();QVERIFY(!top->isVisible());QVERIFY(left->isVisible());
            window.findChild<QPushButton *>("showHiddenButton")->click();QVERIFY(top->isVisible());
            grid->setCurrentIndex(model->index(2,2));QTRY_VERIFY_WITH_TIMEOUT(model->sourceReady(model->index(2,2)),5000);
            auto *source=window.findChild<QLineEdit *>("source");source->setText("draft");source->setModified(true);
            answerNextDialog(QMessageBox::Cancel);window.findChild<QAction *>("unfreezePanesAction")->trigger();QVERIFY(corner->isVisible());QCOMPARE(source->text(),QString("draft"));
            QTest::keyClick(source,Qt::Key_Escape);QVERIFY(changed.isEmpty());
            window.findChild<QAction *>("unfreezePanesAction")->trigger();QVERIFY(!corner->isVisible());
            grid->setColumnWidth(0,2000);grid->setCurrentIndex(model->index(0,1));freeze->trigger();QVERIFY(!left->isVisible());
            QVERIFY(window.findChild<QPlainTextEdit *>("operationIssues")->toPlainText().contains("too little scrolling space"));
            grid->setColumnWidth(0,110);grid->setCurrentIndex(model->index(2,2));freeze->trigger();QVERIFY(corner->isVisible());QVERIFY(changed.isEmpty());
            if(qEnvironmentVariableIsSet("DATAHORIZON_PANES_SCREENSHOT"))window.grab().save(qEnvironmentVariable("DATAHORIZON_PANES_SCREENSHOT"));
        }
        {
            MainWindow window(options);window.show();QSignalSpy ready(&window,&MainWindow::serviceReady);QTRY_COMPARE_WITH_TIMEOUT(ready.count(),1,10000);
            QSignalSpy opened(&window,&MainWindow::workbookReady);window.openBook(path,false,true);QTRY_COMPARE_WITH_TIMEOUT(opened.count(),1,10000);
            auto *grid=window.findChild<QTableView *>("grid"),*corner=window.findChild<QTableView *>("frozenCorner");
            QVERIFY(corner->isVisible());QCOMPARE(corner->model()->rowCount(),2);QCOMPARE(corner->model()->columnCount(),2);
            QCOMPARE(corner->rowHeight(1),49);QCOMPARE(corner->columnWidth(1),137);
            window.findChild<QAction *>("unfreezePanesAction")->trigger();QVERIFY(!corner->isVisible());QVERIFY(!grid->isRowHidden(1));QVERIFY(!grid->isColumnHidden(1));
            QCOMPARE(grid->rowHeight(1),49);QCOMPARE(grid->columnWidth(1),137);
        }
        QSettings settings(options.viewStatePath,QSettings::IniFormat);QVERIFY(!settings.allKeys().isEmpty());
        const auto key=settings.allKeys().first();auto legacy=QJsonDocument::fromJson(settings.value(key).toByteArray()).object();
        legacy["frozen_rows"]=1000000000;legacy["frozen_columns"]="bad";legacy["freeze_top_row"]=true;legacy["freeze_first_column"]=true;
        settings.setValue(key,QJsonDocument(legacy).toJson(QJsonDocument::Compact));settings.sync();
        MainWindow legacyWindow(options);legacyWindow.show();QSignalSpy ready(&legacyWindow,&MainWindow::serviceReady);QTRY_COMPARE_WITH_TIMEOUT(ready.count(),1,10000);
        QSignalSpy opened(&legacyWindow,&MainWindow::workbookReady);legacyWindow.openBook(path,false,true);QTRY_COMPARE_WITH_TIMEOUT(opened.count(),1,10000);
        auto *corner=legacyWindow.findChild<QTableView *>("frozenCorner");QVERIFY(corner->isVisible());QCOMPARE(corner->model()->rowCount(),1);QCOMPARE(corner->model()->columnCount(),1);
    }
    void frozenFirstColumnWrapUsesBoundedSharedRowMeasurement() {
        QTemporaryDir temp;StartupOptions options;options.automaticWorkbook=false;options.viewStatePath=temp.filePath("views.ini");
        MainWindow window(options);window.show();QSignalSpy ready(&window,&MainWindow::serviceReady);QTRY_COMPARE_WITH_TIMEOUT(ready.count(),1,10000);
        QSignalSpy opened(&window,&MainWindow::workbookReady);window.openBook(temp.filePath("frozen-wrap.dhwb"),true);QTRY_COMPARE_WITH_TIMEOUT(opened.count(),1,10000);
        const QString text="A long label in the frozen first column should wrap and share its row height with the scrolling body";
        window.submit({{"action","edit"},{"cells",QJsonArray{QJsonObject{{"row",2},{"col",1},{"source",text},{"kind","text"},{"style",QJsonObject{{"wrap",true}}}}}}});
        QTRY_COMPARE_WITH_TIMEOUT(opened.count(),2,10000);window.findChild<QAction *>("freezeFirstColumnAction")->trigger();
        auto *left=window.findChild<QTableView *>("frozenFirstColumn"),*grid=window.findChild<QTableView *>("grid");
        left->setColumnWidth(0,90);QTRY_VERIFY_WITH_TIMEOUT(grid->rowHeight(1)>26,5000);QCOMPARE(left->rowHeight(1),grid->rowHeight(1));
        const int narrow=grid->rowHeight(1);left->setColumnWidth(0,500);QTRY_VERIFY_WITH_TIMEOUT(grid->rowHeight(1)<narrow,5000);
        QCOMPARE(left->rowHeight(1),grid->rowHeight(1));QCOMPARE(window.model()->data(window.model()->index(1,0),Qt::EditRole).toString(),text);
    }
    void frozenFirstColumnAlignsEditsSelectsAndPersistsWithCorner() {
        QTemporaryDir temp;StartupOptions options;options.automaticWorkbook=false;options.viewStatePath=temp.filePath("views.ini");
        const auto path=temp.filePath("first-column.dhwb");
        {
            MainWindow window(options);window.show();QSignalSpy ready(&window,&MainWindow::serviceReady);QTRY_COMPARE_WITH_TIMEOUT(ready.count(),1,10000);
            QSignalSpy opened(&window,&MainWindow::workbookReady);window.openBook(path,true);QTRY_COMPARE_WITH_TIMEOUT(opened.count(),1,10000);
            auto *grid=window.findChild<QTableView *>("grid");auto *model=window.model();
            window.findChild<QAction *>("freezeFirstColumnAction")->trigger();
            auto *left=window.findChild<QTableView *>("frozenFirstColumn"),*top=window.findChild<QTableView *>("frozenTopRow"),*corner=window.findChild<QTableView *>("frozenCorner");
            QVERIFY(left->isVisible());QVERIFY(grid->isColumnHidden(0));QVERIFY(!grid->verticalHeader()->isVisible());
            grid->scrollTo(model->index(10000,40),QAbstractItemView::PositionAtTop);QTest::qWait(50);
            grid->setRowHeight(10000,63);QCOMPARE(left->rowHeight(10000),63);
            QCOMPARE(left->viewport()->mapToGlobal(left->visualRect(left->model()->index(10000,0)).topLeft()).y(),grid->viewport()->mapToGlobal(grid->visualRect(model->index(10000,40)).topLeft()).y());
            const int horizontal=grid->horizontalScrollBar()->value();left->setCurrentIndex(left->model()->index(10000,0));
            QCOMPARE(grid->currentIndex(),model->index(10000,0));QCOMPARE(grid->horizontalScrollBar()->value(),horizontal);
            QTest::keyClick(left,Qt::Key_Right,Qt::ShiftModifier);QCOMPARE(grid->selectionModel()->selection().first().width(),2);
            left->setColumnWidth(0,150);QCOMPARE(left->columnWidth(0),150);
            grid->selectColumn(0);QTimer::singleShot(20,[]{if(auto *dialog=qobject_cast<QInputDialog *>(QApplication::activeModalWidget())){dialog->setIntValue(151);dialog->accept();}});
            window.findChild<QAction *>("columnWidthAction")->trigger();QCOMPARE(left->columnWidth(0),151);
            auto *address=window.findChild<QLineEdit *>("address");address->setText("A1");QTest::keyClick(address,Qt::Key_Return);QCOMPARE(window.focusWidget(),left);
            window.findChild<QAction *>("freezeTopRowAction")->trigger();QVERIFY(top->isVisible());QVERIFY(corner->isVisible());
            address->setText("A1:C3");QTest::keyClick(address,Qt::Key_Return);QCOMPARE(window.focusWidget(),corner);
            QCOMPARE(grid->selectionModel()->selection().first().width(),3);QCOMPARE(grid->selectionModel()->selection().first().height(),3);
            QTRY_VERIFY_WITH_TIMEOUT(model->sourceReady(model->index(0,0)),5000);
            corner->edit(corner->model()->index(0,0));auto *editor=corner->findChild<QLineEdit *>();QVERIFY(editor);
            editor->setText("=3+4");QTest::keyClick(editor,Qt::Key_Return);QTRY_COMPARE_WITH_TIMEOUT(opened.count(),2,10000);
            QTRY_COMPARE_WITH_TIMEOUT(left->model()->data(left->model()->index(1,0)).toString(),QString(),5000);
            QTRY_COMPARE_WITH_TIMEOUT(corner->model()->data(corner->model()->index(0,0)).toString(),QString("7"),5000);
            QCOMPARE(grid->currentIndex(),model->index(1,0));QCOMPARE(window.focusWidget(),left);
            address->setText("A1");QTest::keyClick(address,Qt::Key_Return);
            QTRY_VERIFY_WITH_TIMEOUT(model->sourceReady(model->index(0,0)),5000);
            corner->edit(corner->model()->index(0,0));editor=corner->findChild<QLineEdit *>();QVERIFY(editor);editor->setText("draft");
            QPointer<QLineEdit> cancelledEditor=editor;
            answerNextDialog(QMessageBox::Cancel);window.findChild<QAction *>("freezeFirstColumnAction")->trigger();QVERIFY(corner->isVisible());
            QTRY_VERIFY_WITH_TIMEOUT(cancelledEditor.isNull(),3000);
            auto *source=window.findChild<QLineEdit *>("source");QCOMPARE(source->text(),QString("draft"));QVERIFY(source->isModified());
            QTest::keyClick(source,Qt::Key_Escape);QCOMPARE(window.focusWidget(),corner);
            corner->edit(corner->model()->index(0,0));editor=corner->findChild<QLineEdit *>();QVERIFY(editor);editor->setText("=4+4");
            window.findChild<QAction *>("saveAction")->trigger();QTRY_COMPARE_WITH_TIMEOUT(opened.count(),3,10000);
            QTRY_COMPARE_WITH_TIMEOUT(corner->model()->data(corner->model()->index(0,0)).toString(),QString("8"),5000);
            grid->selectColumn(0);window.findChild<QAction *>("hideColumnsAction")->trigger();QVERIFY(!left->isVisible());QVERIFY(!corner->isVisible());
            window.findChild<QPushButton *>("showHiddenButton")->click();QVERIFY(left->isVisible());QVERIFY(corner->isVisible());
            window.showList({{"r1",1},{"r2",3},{"c1",1},{"c2",2},{"header",false}});QTRY_COMPARE_WITH_TIMEOUT(opened.count(),4,10000);
            QVERIFY(!left->isVisible());QVERIFY(!corner->isVisible());
            window.findChild<QAction *>("clearListAction")->trigger();QTRY_COMPARE_WITH_TIMEOUT(opened.count(),5,10000);QVERIFY(left->isVisible());
            QVERIFY(model->cacheMetrics()["tiles"].toInt()<=16);
            if(qEnvironmentVariableIsSet("DATAHORIZON_COLUMN_SCREENSHOT"))window.grab().save(qEnvironmentVariable("DATAHORIZON_COLUMN_SCREENSHOT"));
        }
        {
            MainWindow window(options);window.show();QSignalSpy ready(&window,&MainWindow::serviceReady);QTRY_COMPARE_WITH_TIMEOUT(ready.count(),1,10000);
            QSignalSpy opened(&window,&MainWindow::workbookReady);window.openBook(path,false,true);QTRY_COMPARE_WITH_TIMEOUT(opened.count(),1,10000);
            auto *left=window.findChild<QTableView *>("frozenFirstColumn"),*corner=window.findChild<QTableView *>("frozenCorner");
            QVERIFY(left->isVisible());QVERIFY(corner->isVisible());QCOMPARE(left->columnWidth(0),151);
            QVERIFY(!(corner->model()->flags(corner->model()->index(0,0))&Qt::ItemIsEditable));
            window.findChild<QAction *>("freezeFirstColumnAction")->trigger();QVERIFY(!left->isVisible());QVERIFY(!window.findChild<QTableView *>("grid")->isColumnHidden(0));
        }
    }
    void frozenPaneSelectionCrossesByKeyboardMouseAndAddressWithoutEdits() {
        QTemporaryDir temp;StartupOptions options;options.automaticWorkbook=false;options.viewStatePath=temp.filePath("views.ini");
        MainWindow window(options);window.show();QSignalSpy ready(&window,&MainWindow::serviceReady);
        QTRY_COMPARE_WITH_TIMEOUT(ready.count(),1,10000);QSignalSpy opened(&window,&MainWindow::workbookReady);
        window.openBook(temp.filePath("cross-pane.dhwb"),true);QTRY_COMPARE_WITH_TIMEOUT(opened.count(),1,10000);
        auto *grid=window.findChild<QTableView *>("grid");auto *model=window.model();
        window.findChild<QAction *>("freezeTopRowAction")->trigger();auto *top=window.findChild<QTableView *>("frozenTopRow");
        QSignalSpy changed(&window,&MainWindow::operationFinished);QSignalSpy edits(model,&SheetModel::editRequested);
        auto point=[](QTableView *view,int row,int col){return view->visualRect(view->model()->index(row,col)).center();};
        QTest::mouseClick(grid->viewport(),Qt::LeftButton,Qt::NoModifier,point(grid,1,1));
        QTest::keyClick(grid,Qt::Key_Up,Qt::ShiftModifier);QCOMPARE(grid->currentIndex(),model->index(0,1));
        QCOMPARE(grid->selectionModel()->selection().first().height(),2);QCOMPARE(window.focusWidget(),top);
        QTest::keyClick(top,Qt::Key_Down,Qt::ShiftModifier);QCOMPARE(grid->currentIndex(),model->index(1,1));
        QCOMPARE(grid->selectionModel()->selection().first().height(),1);
        auto drag=[&](QTableView *from,int r1,int c1,QTableView *to,int r2,int c2){
            QTest::mousePress(from->viewport(),Qt::LeftButton,Qt::NoModifier,point(from,r1,c1));
            const auto global=to->viewport()->mapToGlobal(point(to,r2,c2));
            QMouseEvent move(QEvent::MouseMove,QPointF(from->viewport()->mapFromGlobal(global)),QPointF(global),Qt::NoButton,Qt::LeftButton,Qt::NoModifier);
            QApplication::sendEvent(from->viewport(),&move);
            QTest::mouseRelease(from->viewport(),Qt::LeftButton,Qt::NoModifier,from->viewport()->mapFromGlobal(global));
        };
        drag(top,0,0,grid,2,2);auto range=grid->selectionModel()->selection().first();
        QCOMPARE(range.top(),0);QCOMPARE(range.left(),0);QCOMPARE(range.bottom(),2);QCOMPARE(range.right(),2);
        QCOMPARE(top->selectionModel()->selection().first().width(),3);
        drag(grid,2,2,top,0,0);QCOMPARE(grid->selectionModel()->selection().first(),range);
        auto *address=window.findChild<QLineEdit *>("address");address->setText("A1:C3");QTest::keyClick(address,Qt::Key_Return);
        QCOMPARE(grid->selectionModel()->selection().first(),range);
        QTRY_VERIFY_WITH_TIMEOUT(model->sourceReady(model->index(0,0)),5000);
        auto *source=window.findChild<QLineEdit *>("source");source->setText("draft");source->setModified(true);
        answerNextDialog(QMessageBox::Cancel);QTest::mouseClick(grid->viewport(),Qt::LeftButton,Qt::ShiftModifier,point(grid,3,3));
        QCOMPARE(grid->selectionModel()->selection().first(),range);QCOMPARE(source->text(),QString("draft"));
        QTest::keyClick(source,Qt::Key_Escape);QVERIFY(edits.isEmpty());QVERIFY(changed.isEmpty());
        QAction *bold=nullptr;for(auto *action:window.findChildren<QAction *>())if(action->shortcut().matches(QKeySequence::Bold)==QKeySequence::ExactMatch)bold=action;
        QVERIFY(bold);bold->trigger();QTRY_COMPARE_WITH_TIMEOUT(opened.count(),2,10000);
        QTRY_VERIFY_WITH_TIMEOUT(model->loaded(model->index(2,2)),5000);QVERIFY(model->cell(model->index(2,2))["style"].toObject()["bold"].toBool());
        QTRY_VERIFY_WITH_TIMEOUT(model->loaded(model->index(0,0)),5000);QVERIFY(model->cell(model->index(0,0))["style"].toObject()["bold"].toBool());
    }
    void formulaAwareCutRetargetsDependentsAndSupportsUndo() {
        QTemporaryDir temp;StartupOptions options;options.automaticWorkbook=false;
        MainWindow window(options);window.show();QSignalSpy ready(&window,&MainWindow::serviceReady);
        QTRY_COMPARE_WITH_TIMEOUT(ready.count(),1,10000);
        QSignalSpy opened(&window,&MainWindow::workbookReady);window.openBook(temp.filePath("formula-cut.dhwb"),true);
        QTRY_COMPARE_WITH_TIMEOUT(opened.count(),1,10000);
        window.submit({{"action","edit"},{"cells",QJsonArray{QJsonObject{{"row",1},{"col",1},{"source","7"}},
            QJsonObject{{"row",1},{"col",2},{"source","=$A$1+1"}}}}});
        QTRY_COMPARE_WITH_TIMEOUT(opened.count(),2,10000);
        auto *model=window.model();auto *grid=window.findChild<QTableView *>("grid");
        QTRY_COMPARE_WITH_TIMEOUT(model->data(model->index(0,1)).toString(),QString("8"),5000);
        QAction *cut=nullptr;for(auto *action:window.findChildren<QAction *>())if(action->shortcut().matches(QKeySequence::Cut)==QKeySequence::ExactMatch)cut=action;
        QVERIFY(cut);grid->setCurrentIndex(model->index(0,0));grid->setFocus();cut->trigger();
        QTRY_VERIFY_WITH_TIMEOUT(QApplication::clipboard()->mimeData()->hasFormat("application/x-datahorizon-cells"),5000);
        grid->setCurrentIndex(model->index(2,0));window.findChild<QAction *>("pasteAction")->trigger();
        QTRY_COMPARE_WITH_TIMEOUT(opened.count(),3,10000);
        QTRY_COMPARE_WITH_TIMEOUT(model->data(model->index(0,1),Qt::EditRole).toString(),QString("=$A$3+1"),5000);
        QTRY_COMPARE_WITH_TIMEOUT(model->data(model->index(2,0)).toString(),QString("7"),5000);
        window.submit({{"action","undo"}});QTRY_COMPARE_WITH_TIMEOUT(opened.count(),4,10000);
        QTRY_COMPARE_WITH_TIMEOUT(model->data(model->index(0,1),Qt::EditRole).toString(),QString("=$A$1+1"),5000);
    }
    void selectionSummaryReadsWholeStoredRectangleAndHandlesErrorsAndViewGuards() {
        QTemporaryDir temp;StartupOptions options;options.automaticWorkbook=false;
        MainWindow window(options);window.show();QSignalSpy ready(&window,&MainWindow::serviceReady);
        QTRY_COMPARE_WITH_TIMEOUT(ready.count(),1,10000);
        QSignalSpy opened(&window,&MainWindow::workbookReady);window.openBook(temp.filePath("summary.dhwb"),true);
        QTRY_COMPARE_WITH_TIMEOUT(opened.count(),1,10000);
        window.submit({{"action","edit"},{"cells",QJsonArray{
            QJsonObject{{"row",1},{"col",1},{"source","0.125"},{"style",QJsonObject{{"format","percent2"}}}},
            QJsonObject{{"row",2},{"col",1},{"source","=2+2"}},QJsonObject{{"row",3},{"col",1},{"source","12"},{"kind","text"}},
            QJsonObject{{"row",4},{"col",1},{"source","TRUE"}},QJsonObject{{"row",5},{"col",1},{"source",""},{"kind","text"}},
            QJsonObject{{"row",6},{"col",1},{"source","NULL"},{"kind","null"}},QJsonObject{{"row",1000},{"col",1},{"source","3"}}}}});
        QTRY_COMPARE_WITH_TIMEOUT(opened.count(),2,10000);
        auto *grid=window.findChild<QTableView *>("grid");auto *model=window.model();auto *label=window.findChild<QLabel *>("selectionSummary");
        QSignalSpy changed(&window,&MainWindow::operationFinished);
        grid->selectionModel()->select(QItemSelection(model->index(0,0),model->index(999,0)),QItemSelectionModel::ClearAndSelect);
        QTRY_COMPARE_WITH_TIMEOUT(label->text(),QString("Count 6 • Sum 7.125 • Average 2.375"),5000);
        QVERIFY(label->toolTip().contains("1000 selected cells; 3 numeric"));QVERIFY(changed.isEmpty());
        window.resize(700,500);QTest::qWait(30);QVERIFY(label->width()<window.width());QCOMPARE(label->accessibleName(),QString("Selection summary"));
        if(qEnvironmentVariableIsSet("DATAHORIZON_SUMMARY_SCREENSHOT"))window.grab().save(qEnvironmentVariable("DATAHORIZON_SUMMARY_SCREENSHOT"));
        grid->selectionModel()->select(QItemSelection(model->index(0,0),model->index(10000,0)),QItemSelectionModel::ClearAndSelect);
        QCOMPARE(label->text(),QString("Summary unavailable"));QVERIFY(label->toolTip().contains("10,000"));
        grid->setCurrentIndex(model->index(1,0));window.findChild<QAction *>("hideRowsAction")->trigger();
        grid->selectionModel()->select(QItemSelection(model->index(0,0),model->index(2,0)),QItemSelectionModel::ClearAndSelect);
        QVERIFY(label->toolTip().contains("hidden"));window.findChild<QPushButton *>("showHiddenButton")->click();
        QTRY_COMPARE_WITH_TIMEOUT(label->text(),QString("Count 3 • Sum 4.125 • Average 2.0625"),5000);
        window.submit({{"action","edit"},{"cells",QJsonArray{QJsonObject{{"row",2},{"col",1},{"source","=1/0"}}}}});
        QTRY_COMPARE_WITH_TIMEOUT(opened.count(),3,10000);
        grid->selectionModel()->select(QItemSelection(model->index(0,0),model->index(2,0)),QItemSelectionModel::ClearAndSelect);
        QTRY_COMPARE_WITH_TIMEOUT(label->text(),QString("Count 3 • Sum/average unavailable"),5000);
        QVERIFY(label->toolTip().contains("1 formula errors"));
        window.showList({{"r1",1},{"r2",3},{"c1",1},{"c2",1},{"header",false}});
        QTRY_COMPARE_WITH_TIMEOUT(opened.count(),4,10000);QVERIFY(label->toolTip().contains("list view"));
    }
    void selectionSummaryCoalescesAndDiscardsSupersededRepliesAndConnections() {
        QTemporaryDir temp;StartupOptions options;options.automaticWorkbook=false;
        MainWindow window(options);window.show();QSignalSpy ready(&window,&MainWindow::serviceReady);
        QTRY_COMPARE_WITH_TIMEOUT(ready.count(),1,10000);
        QSignalSpy opened(&window,&MainWindow::workbookReady);window.openBook(temp.filePath("summary-late.dhwb"),true);
        QTRY_COMPARE_WITH_TIMEOUT(opened.count(),1,10000);
        auto *model=window.model();auto *grid=window.findChild<QTableView *>("grid");auto *label=window.findChild<QLabel *>("selectionSummary");
        DelayedSummaryServer server(model->state(),model->sheet());QVERIFY(server.listen(QHostAddress::LocalHost,0));
        auto *client=window.findChild<RestClient *>();client->configure("http://127.0.0.1:"+QString::number(server.serverPort()),"token");
        grid->setCurrentIndex(model->index(0,0));
        QTRY_COMPARE_WITH_TIMEOUT(server.requests.size(),1,3000);
        for(int col=1;col<=8;++col)grid->setCurrentIndex(model->index(0,col));
        QTest::qWait(350);QCOMPARE(server.requests.size(),1);QCOMPARE(label->text(),QString("Summary…"));
        server.respond(0,100);QTRY_COMPARE_WITH_TIMEOUT(server.requests.size(),2,3000);
        QCOMPARE(server.requests[1].range["c1"].toInt(),9);QCOMPARE(label->text(),QString("Summary…"));
        server.respond(1,9);QTRY_COMPARE_WITH_TIMEOUT(label->text(),QString("Count 1 • Sum 9 • Average 9"),3000);
        grid->setCurrentIndex(model->index(0,9));QTRY_COMPARE_WITH_TIMEOUT(server.requests.size(),3,3000);
        DelayedSummaryServer replacement(model->state(),model->sheet());QVERIFY(replacement.listen(QHostAddress::LocalHost,0));
        client->configure("http://127.0.0.1:"+QString::number(replacement.serverPort()),"token");
        QTRY_COMPARE_WITH_TIMEOUT(replacement.requests.size(),1,3000);replacement.respond(0,10);
        QTRY_COMPARE_WITH_TIMEOUT(label->text(),QString("Count 1 • Sum 10 • Average 10"),3000);
        server.respond(2,200);QTest::qWait(50);QCOMPARE(label->text(),QString("Count 1 • Sum 10 • Average 10"));
        const auto issues=window.findChild<QPlainTextEdit *>("operationIssues")->toPlainText();
        grid->setCurrentIndex(model->index(0,10));QTRY_COMPARE_WITH_TIMEOUT(replacement.requests.size(),2,3000);
        replacement.respondInvalid(1);QTRY_COMPARE_WITH_TIMEOUT(label->text(),QString("Summary unavailable"),3000);
        QVERIFY(label->toolTip().contains("Invalid selection summary response"));QCOMPARE(window.findChild<QPlainTextEdit *>("operationIssues")->toPlainText(),issues);
    }
    void scopedFindNavigatesWrapsAndInvalidatesAfterEdits() {
        QTemporaryDir temp;StartupOptions options;options.automaticWorkbook=false;
        MainWindow window(options);window.show();QSignalSpy ready(&window,&MainWindow::serviceReady);
        QTRY_COMPARE_WITH_TIMEOUT(ready.count(),1,10000);
        QSignalSpy opened(&window,&MainWindow::workbookReady);window.openBook(temp.filePath("find.dhwb"),true);
        QTRY_COMPARE_WITH_TIMEOUT(opened.count(),1,10000);
        window.submit({{"action","edit"},{"cells",QJsonArray{QJsonObject{{"row",1},{"col",1},{"source","hit"}},
            QJsonObject{{"row",3},{"col",2},{"source","hit"}},QJsonObject{{"row",8},{"col",1},{"source","hit"}}}}});
        QTRY_COMPARE_WITH_TIMEOUT(opened.count(),2,10000);
        auto *grid=window.findChild<QTableView *>("grid");auto *model=window.model();
        auto *next=window.findChild<QAction *>("findNextAction"),*previous=window.findChild<QAction *>("findPreviousAction");
        QVERIFY(!next->isEnabled());
        grid->selectionModel()->select(QItemSelection(model->index(0,0),model->index(4,1)),QItemSelectionModel::ClearAndSelect);
        auto search=[&](const QString &needle){
            QTimer answer;answer.setInterval(10);connect(&answer,&QTimer::timeout,&window,[&]{
                if(auto *dialog=qobject_cast<QInputDialog *>(QApplication::activeModalWidget())) {
                    dialog->setTextValue(dialog->windowTitle()=="Search field"?dialog->comboBoxItems().first():needle);dialog->accept();
                }
            });answer.start();window.findChild<QAction *>("findAction")->trigger();
        };
        QSignalSpy changed(&window,&MainWindow::operationFinished);search("hit");
        QTRY_VERIFY_WITH_TIMEOUT(next->isEnabled(),5000);QCOMPARE(grid->currentIndex(),model->index(0,0));
        next->trigger();QCOMPARE(grid->currentIndex(),model->index(2,1));
        next->trigger();QCOMPARE(grid->currentIndex(),model->index(0,0));
        previous->trigger();QCOMPARE(grid->currentIndex(),model->index(2,1));
        QVERIFY(window.findChild<QLabel *>("storageStatus")->text().contains("2 of 2"));
        QTRY_VERIFY_WITH_TIMEOUT(model->sourceReady(model->index(2,1)),5000);
        auto *source=window.findChild<QLineEdit *>("source");source->setText("draft");source->setModified(true);
        answerNextDialog(QMessageBox::Cancel);next->trigger();QCOMPARE(grid->currentIndex(),model->index(2,1));QCOMPARE(source->text(),QString("draft"));
        answerNextDialog(QMessageBox::Discard);next->trigger();QCOMPARE(grid->currentIndex(),model->index(0,0));
        previous->trigger();QCOMPARE(grid->currentIndex(),model->index(2,1));
        window.findChild<QAction *>("hideRowsAction")->trigger();
        previous->trigger();QCOMPARE(grid->currentIndex(),model->index(0,0));
        next->trigger();QCOMPARE(grid->currentIndex(),model->index(0,0));
        QVERIFY(window.findChild<QPlainTextEdit *>("operationIssues")->toPlainText().contains("next match is hidden"));
        window.findChild<QPushButton *>("showHiddenButton")->click();next->trigger();QCOMPARE(grid->currentIndex(),model->index(2,1));
        QVERIFY(changed.isEmpty());
        window.submit({{"action","edit"},{"cells",QJsonArray{QJsonObject{{"row",3},{"col",2},{"source","changed"}}}}});
        QTRY_COMPARE_WITH_TIMEOUT(opened.count(),3,10000);QVERIFY(!next->isEnabled());QVERIFY(!previous->isEnabled());
        search("absent");QTRY_VERIFY_WITH_TIMEOUT(window.findChild<QPlainTextEdit *>("operationIssues")->toPlainText().contains("0 matching cells"),5000);
        QVERIFY(!next->isEnabled());
    }
    void findPagesDiscardMovedSelectionsDraftsInvalidCountsAndSupersededSearches() {
        QTemporaryDir temp;StartupOptions options;options.automaticWorkbook=false;MainWindow window(options);window.show();QSignalSpy ready(&window,&MainWindow::serviceReady);QTRY_COMPARE_WITH_TIMEOUT(ready.count(),1,10000);QSignalSpy opened(&window,&MainWindow::workbookReady);window.openBook(temp.filePath("late-find.dhwb"),true);QTRY_COMPARE_WITH_TIMEOUT(opened.count(),1,10000);
        auto *grid=window.findChild<QTableView *>("grid");auto *model=window.model();auto *source=window.findChild<QLineEdit *>("source");auto *next=window.findChild<QAction *>("findNextAction"),*previous=window.findChild<QAction *>("findPreviousAction");auto *client=window.findChild<RestClient *>();DelayedFindServer server(model->state(),model->sheet());QVERIFY(server.listen(QHostAddress::LocalHost,0));QTRY_VERIFY_WITH_TIMEOUT(model->sourceReady(model->index(8,0)),5000);client->configure("http://127.0.0.1:"+QString::number(server.serverPort()),"token");QSignalSpy replies(client->findChild<QNetworkAccessManager *>(),&QNetworkAccessManager::finished);
        auto search=[&]{grid->selectionModel()->select(QItemSelection(model->index(0,0),model->index(1199,0)),QItemSelectionModel::ClearAndSelect);QTimer timer;connect(&timer,&QTimer::timeout,&window,[&]{if(auto *dialog=qobject_cast<QInputDialog *>(QApplication::activeModalWidget())){dialog->setTextValue(dialog->windowTitle()=="Search field"?dialog->comboBoxItems().first():QString("hit"));dialog->accept();}});timer.start(10);window.findChild<QAction *>("findAction")->trigger();};
        search();QTRY_COMPARE_WITH_TIMEOUT(server.requests.size(),1,3000);server.respond(0);QTRY_VERIFY_WITH_TIMEOUT(next->isEnabled(),3000);QCOMPARE(grid->currentIndex(),model->index(0,0));
        previous->trigger();QTRY_COMPARE_WITH_TIMEOUT(server.requests.size(),2,3000);QCOMPARE(server.requests[1].input["offset"].toInt(),1000);QCOMPARE(server.requests[1].input["expected_state"].toString(),model->state());grid->setCurrentIndex(model->index(8,0));server.respond(1);QTRY_VERIFY_WITH_TIMEOUT(next->isEnabled(),3000);QCOMPARE(grid->currentIndex(),model->index(8,0));
        previous->trigger();QTRY_COMPARE_WITH_TIMEOUT(server.requests.size(),3,3000);QTRY_VERIFY_WITH_TIMEOUT(source->isEnabled(),3000);source->setFocus();source->setText("new draft");source->setModified(true);server.respond(2);QTRY_VERIFY_WITH_TIMEOUT(next->isEnabled(),3000);QCOMPARE(source->text(),QString("new draft"));QCOMPARE(grid->currentIndex(),model->index(8,0));QTest::keyClick(source,Qt::Key_Escape);
        previous->trigger();QTRY_COMPARE_WITH_TIMEOUT(server.requests.size(),4,3000);server.respond(3,1199);QTRY_VERIFY_WITH_TIMEOUT(window.findChild<QPlainTextEdit *>("operationIssues")->toPlainText().contains("page is invalid"),3000);QVERIFY(!next->isEnabled());
        search();QTRY_COMPARE_WITH_TIMEOUT(server.requests.size(),5,3000);server.respond(4);QTRY_VERIFY_WITH_TIMEOUT(next->isEnabled(),3000);previous->trigger();QTRY_COMPARE_WITH_TIMEOUT(server.requests.size(),6,3000);
        search();QTRY_COMPARE_WITH_TIMEOUT(server.requests.size(),7,3000);const auto before=grid->currentIndex();const int completed=replies.count();server.respond(5);QTRY_VERIFY_WITH_TIMEOUT(replies.count()>completed,3000);QCOMPARE(grid->currentIndex(),before);QVERIFY(!next->isEnabled());server.respond(6,1,4);QTRY_VERIFY_WITH_TIMEOUT(next->isEnabled(),3000);QCOMPARE(grid->currentIndex(),model->index(3,0));
        client->configure("http://127.0.0.1:1","other");QVERIFY(!next->isEnabled());QVERIFY(!previous->isEnabled());
    }
    void scopedFindPagesPastFiveHundredAndWrapsAcrossAllDisplayMatches() {
        QTemporaryDir temp;StartupOptions options;options.automaticWorkbook=false;
        MainWindow window(options);window.show();QSignalSpy ready(&window,&MainWindow::serviceReady);
        QTRY_COMPARE_WITH_TIMEOUT(ready.count(),1,10000);
        QSignalSpy opened(&window,&MainWindow::workbookReady);window.openBook(temp.filePath("find-limit.dhwb"),true);
        QTRY_COMPARE_WITH_TIMEOUT(opened.count(),1,10000);
        QJsonArray cells;for(int r=1;r<=501;++r)cells.append(QJsonObject{{"row",r},{"col",1},{"source","=1/8"},{"style",QJsonObject{{"format","percent2"}}}});
        window.submit({{"action","edit"},{"cells",cells}});QTRY_COMPARE_WITH_TIMEOUT(opened.count(),2,10000);
        auto *model=window.model();auto *grid=window.findChild<QTableView *>("grid");
        grid->selectionModel()->select(QItemSelection(model->index(0,0),model->index(500,0)),QItemSelectionModel::ClearAndSelect);
        QTimer answer;answer.setInterval(10);connect(&answer,&QTimer::timeout,&window,[&]{
            if(auto *dialog=qobject_cast<QInputDialog *>(QApplication::activeModalWidget())) {
                dialog->setTextValue(dialog->windowTitle()=="Search field"?dialog->comboBoxItems().at(1):QString("12.50%"));dialog->accept();
            }
        });answer.start();window.findChild<QAction *>("findAction")->trigger();answer.stop();
        auto *next=window.findChild<QAction *>("findNextAction");QTRY_VERIFY_WITH_TIMEOUT(next->isEnabled(),5000);
        QVERIFY(window.findChild<QLabel *>("storageStatus")->text().contains("1 of 501"));
        window.findChild<QAction *>("findPreviousAction")->trigger();QTRY_COMPARE_WITH_TIMEOUT(grid->currentIndex(),model->index(500,0),5000);
        QVERIFY(window.findChild<QLabel *>("storageStatus")->text().contains("501 of 501"));
        next->trigger();QTRY_COMPARE_WITH_TIMEOUT(grid->currentIndex(),model->index(0,0),5000);
        for(int i=1;i<500;++i)next->trigger();QCOMPARE(grid->currentIndex(),model->index(499,0));next->trigger();QTRY_COMPARE_WITH_TIMEOUT(grid->currentIndex(),model->index(500,0),5000);
        window.showList({{"r1",1},{"r2",3},{"c1",1},{"c2",1},{"header",false}});
        QTRY_COMPARE_WITH_TIMEOUT(opened.count(),3,10000);QVERIFY(!next->isEnabled());
    }
    void addressRangesNormalizeBoundsAndProtectDraftsAndViewGuards() {
        QTemporaryDir temp;StartupOptions options;options.automaticWorkbook=false;
        MainWindow window(options);window.show();QSignalSpy ready(&window,&MainWindow::serviceReady);
        QTRY_COMPARE_WITH_TIMEOUT(ready.count(),1,10000);
        QSignalSpy opened(&window,&MainWindow::workbookReady);window.openBook(temp.filePath("ranges.dhwb"),true);
        QTRY_COMPARE_WITH_TIMEOUT(opened.count(),1,10000);
        auto *grid=window.findChild<QTableView *>("grid");auto *model=window.model();
        auto *address=window.findChild<QLineEdit *>("address");auto *source=window.findChild<QLineEdit *>("source");
        QSignalSpy changed(&window,&MainWindow::operationFinished);
        auto go=[&](QString value){address->setText(value);QTest::keyClick(address,Qt::Key_Return);};
        go(" $d$5:$b$2 ");QCOMPARE(grid->currentIndex(),model->index(4,3));
        auto rect=grid->selectionModel()->selection().first();QCOMPARE(rect.top(),1);QCOMPARE(rect.left(),1);QCOMPARE(rect.bottom(),4);QCOMPARE(rect.right(),3);
        const auto saved=grid->selectionModel()->selection();
        for(const auto &bad:QStringList{"A0","XFE1","A1048577","A1:XFD1048576","Sheet1!A1","A1,B2"}) {go(bad);QCOMPARE(grid->selectionModel()->selection(),saved);}
        go("XFD1048576");QCOMPARE(grid->currentIndex(),model->index(1048575,16383));
        go("B2:C3");QTRY_VERIFY_WITH_TIMEOUT(model->sourceReady(model->index(1,1)),5000);
        source->setFocus();source->setText("draft");source->setModified(true);
        answerNextDialog(QMessageBox::Cancel);go("E5:F6");QCOMPARE(grid->currentIndex(),model->index(1,1));QCOMPARE(source->text(),QString("draft"));
        answerNextDialog(QMessageBox::Discard);go("E5:F6");QCOMPARE(grid->currentIndex(),model->index(4,4));
        go("B2");window.findChild<QAction *>("hideRowsAction")->trigger();
        const auto visible=grid->currentIndex();go("A1:B3");QCOMPARE(grid->currentIndex(),visible);
        window.findChild<QPushButton *>("showHiddenButton")->click();
        window.findChild<QAction *>("freezeTopRowAction")->trigger();
        go("C1");QCOMPARE(grid->currentIndex(),model->index(0,2));
        QCOMPARE(window.focusWidget(),window.findChild<QTableView *>("frozenTopRow"));
        go("A1:B2");QCOMPARE(grid->currentIndex(),model->index(0,0));
        QCOMPARE(grid->selectionModel()->selection().first().bottom(),1);
        window.findChild<QAction *>("freezeTopRowAction")->trigger();
        window.showList({{"r1",1},{"r2",3},{"c1",1},{"c2",1},{"header",false}});
        QTRY_COMPARE_WITH_TIMEOUT(opened.count(),2,10000);
        const auto projected=grid->currentIndex();go("B2:C3");QCOMPARE(grid->currentIndex(),projected);
        QTest::qWait(50);QVERIFY(changed.isEmpty());
    }
    void directionalFillUsesIndependentSeedsAndUndo() {
        QTemporaryDir temp; StartupOptions options; options.automaticWorkbook=false;
        MainWindow window(options);window.show();QSignalSpy ready(&window,&MainWindow::serviceReady);
        QTRY_COMPARE_WITH_TIMEOUT(ready.count(),1,10000);
        QSignalSpy opened(&window,&MainWindow::workbookReady);window.openBook(temp.filePath("fill.dhwb"),true);
        QTRY_COMPARE_WITH_TIMEOUT(opened.count(),1,10000);
        window.submit({{"action","edit"},{"cells",QJsonArray{QJsonObject{{"row",1},{"col",1},{"source","1"}},
            QJsonObject{{"row",1},{"col",2},{"source","=A1+1"},{"style",QJsonObject{{"bold",true}}}}}}});
        QTRY_COMPARE_WITH_TIMEOUT(opened.count(),2,10000);
        auto *model=window.model();auto *grid=window.findChild<QTableView *>("grid");
        grid->selectionModel()->select(QItemSelection(model->index(0,0),model->index(2,1)),QItemSelectionModel::ClearAndSelect);
        window.findChild<QAction *>("fillDownAction")->trigger();QTRY_COMPARE_WITH_TIMEOUT(opened.count(),3,10000);
        QTRY_COMPARE_WITH_TIMEOUT(model->data(model->index(2,1)).toString(),QString("2"),5000);
        QCOMPARE(model->data(model->index(2,1),Qt::EditRole).toString(),QString("=A3+1"));
        QVERIFY(model->cell(model->index(2,1))["style"].toObject()["bold"].toBool());
        grid->selectionModel()->select(QItemSelection(model->index(0,0),model->index(2,2)),QItemSelectionModel::ClearAndSelect);
        window.findChild<QAction *>("fillRightAction")->trigger();QTRY_COMPARE_WITH_TIMEOUT(opened.count(),4,10000);
        QTRY_COMPARE_WITH_TIMEOUT(model->data(model->index(2,2)).toString(),QString("1"),5000);
        window.submit({{"action","undo"}});QTRY_COMPARE_WITH_TIMEOUT(opened.count(),5,10000);
        QTRY_COMPARE_WITH_TIMEOUT(model->data(model->index(2,1)).toString(),QString("2"),5000);
        QCOMPARE(model->data(model->index(2,1),Qt::EditRole).toString(),QString("=A3+1"));
    }
    void clearCommandsDistinguishInputsFormatsAndAllWithUndo() {
        QTemporaryDir temp; StartupOptions options; options.automaticWorkbook=false;
        MainWindow window(options); window.show(); QSignalSpy ready(&window,&MainWindow::serviceReady);
        QTRY_COMPARE_WITH_TIMEOUT(ready.count(),1,10000);
        QSignalSpy opened(&window,&MainWindow::workbookReady); window.openBook(temp.filePath("clear.dhwb"),true);
        QTRY_COMPARE_WITH_TIMEOUT(opened.count(),1,10000);
        window.submit({{"action","edit"},{"cells",QJsonArray{
            QJsonObject{{"row",1},{"col",1},{"source","=1/8"},{"style",QJsonObject{{"format","percent2"},{"bold",true}}}},
            QJsonObject{{"row",1},{"col",2},{"source","00123"},{"kind","text"},{"style",QJsonObject{{"background","#12ab34"}}}}}}});
        QTRY_COMPARE_WITH_TIMEOUT(opened.count(),2,10000);
        auto *grid=window.findChild<QTableView *>("grid"); auto *model=window.model();
        auto select=[&]{grid->selectionModel()->select(QItemSelection(model->index(0,0),model->index(0,1)),QItemSelectionModel::ClearAndSelect);};
        select(); window.findChild<QAction *>("clearFormatsAction")->trigger(); QTRY_COMPARE_WITH_TIMEOUT(opened.count(),3,10000);
        QTRY_COMPARE_WITH_TIMEOUT(model->data(model->index(0,0)).toString(),QString("0.125"),5000);
        QTRY_COMPARE_WITH_TIMEOUT(model->data(model->index(0,1)).toString(),QString("00123"),5000);
        QVERIFY(model->cell(model->index(0,0))["style"].toObject().isEmpty());
        window.submit({{"action","undo"}}); QTRY_COMPARE_WITH_TIMEOUT(opened.count(),4,10000);
        QTRY_COMPARE_WITH_TIMEOUT(model->data(model->index(0,0)).toString(),QString("12.50%"),5000);
        select(); window.findChild<QAction *>("clearAllAction")->trigger(); QTRY_COMPARE_WITH_TIMEOUT(opened.count(),5,10000);
        QTRY_COMPARE_WITH_TIMEOUT(model->data(model->index(0,0)).toString(),QString(),5000);
        QTRY_COMPARE_WITH_TIMEOUT(model->data(model->index(0,1)).toString(),QString(),5000);
        QVERIFY(model->cell(model->index(0,0))["style"].toObject().isEmpty());
        window.submit({{"action","undo"}}); QTRY_COMPARE_WITH_TIMEOUT(opened.count(),6,10000);
        QTRY_COMPARE_WITH_TIMEOUT(model->data(model->index(0,0)).toString(),QString("12.50%"),5000);
        QTRY_COMPARE_WITH_TIMEOUT(model->data(model->index(0,1)).toString(),QString("00123"),5000);
        select(); window.findChild<QAction *>("clearContentsAction")->trigger(); QTRY_COMPARE_WITH_TIMEOUT(opened.count(),7,10000);
        QTRY_COMPARE_WITH_TIMEOUT(model->data(model->index(0,0)).toString(),QString(),5000);
        QCOMPARE(model->cell(model->index(0,0))["style"].toObject()["format"].toString(),QString("percent2"));
    }
    void rejectedClearAllRetainsInputsAndHandlerGeneratedCells() {
        QTemporaryDir temp; StartupOptions options; options.automaticWorkbook=false; options.handler=QString(DATAHORIZON_FIXTURES)+"/validation_hook.py";
        MainWindow window(options); window.show(); QSignalSpy ready(&window,&MainWindow::serviceReady);
        QTRY_COMPARE_WITH_TIMEOUT(ready.count(),1,10000);
        QSignalSpy opened(&window,&MainWindow::workbookReady); window.openBook(temp.filePath("reject-clear.dhwb"),true);
        QTRY_COMPARE_WITH_TIMEOUT(opened.count(),1,10000);
        window.submit({{"action","edit"},{"cells",QJsonArray{QJsonObject{{"row",1},{"col",1},{"source","7"},{"style",QJsonObject{{"bold",true}}}}}}});
        QTRY_COMPARE_WITH_TIMEOUT(opened.count(),2,10000);
        auto *model=window.model(); auto *grid=window.findChild<QTableView *>("grid");
        QTRY_COMPARE_WITH_TIMEOUT(model->data(model->index(0,1)).toString(),QString("14"),5000);
        grid->selectionModel()->select(QItemSelection(model->index(0,0),model->index(0,1)),QItemSelectionModel::ClearAndSelect);
        QSignalSpy changed(&window,&MainWindow::operationFinished); window.findChild<QAction *>("clearAllAction")->trigger();
        QTRY_COMPARE_WITH_TIMEOUT(changed.count(),1,10000); QVERIFY(!changed[0][0].toBool());
        QCOMPARE(model->data(model->index(0,0)).toString(),QString("7")); QCOMPARE(model->data(model->index(0,1)).toString(),QString("14"));
    }
    void percentageActionPreservesFormulaAndSupportsUndo() {
        QTemporaryDir temp; StartupOptions options; options.automaticWorkbook=false;
        MainWindow window(options); window.show(); QSignalSpy ready(&window,&MainWindow::serviceReady);
        QTRY_COMPARE_WITH_TIMEOUT(ready.count(),1,10000);
        QSignalSpy opened(&window,&MainWindow::workbookReady); window.openBook(temp.filePath("percentage.dhwb"),true);
        QTRY_COMPARE_WITH_TIMEOUT(opened.count(),1,10000);
        window.submit({{"action","edit"},{"cells",QJsonArray{QJsonObject{{"row",1},{"col",1},{"source","=1/8"}}}}});
        QTRY_COMPARE_WITH_TIMEOUT(opened.count(),2,10000);
        auto *grid=window.findChild<QTableView *>("grid"); grid->setCurrentIndex(window.model()->index(0,0));
        auto *percent=window.findChild<QAction *>("percentFormatAction"); QVERIFY(percent); percent->trigger();
        QTRY_COMPARE_WITH_TIMEOUT(opened.count(),3,10000);
        QTRY_COMPARE_WITH_TIMEOUT(window.model()->data(window.model()->index(0,0)).toString(),QString("12.50%"),5000);
        window.submit({{"action","undo"}}); QTRY_COMPARE_WITH_TIMEOUT(opened.count(),4,10000);
        QTRY_COMPARE_WITH_TIMEOUT(window.model()->data(window.model()->index(0,0)).toString(),QString("0.125"),5000);
    }
    void numericFunctionsRenderDecimalRoundingRules() {
        QTemporaryDir temp; StartupOptions options; options.automaticWorkbook=false;
        MainWindow window(options); window.show(); QSignalSpy ready(&window,&MainWindow::serviceReady);
        QTRY_COMPARE_WITH_TIMEOUT(ready.count(),1,10000);
        QSignalSpy opened(&window,&MainWindow::workbookReady); window.openBook(temp.filePath("rounding.dhwb"),true);
        QTRY_COMPARE_WITH_TIMEOUT(opened.count(),1,10000);
        QJsonArray cells; const QStringList formulas{"=ABS(-7)","=ROUND(-2.5,0)","=ROUNDUP(-1.21,1)","=ROUNDDOWN(-1.29,1)"};
        for(int i=0;i<formulas.size();++i) cells.append(QJsonObject{{"row",1},{"col",i+1},{"source",formulas[i]}});
        window.submit({{"action","edit"},{"cells",cells}}); QTRY_COMPARE_WITH_TIMEOUT(opened.count(),2,10000);
        const QStringList expected{"7","-3","-1.3","-1.2"};
        for(int i=0;i<expected.size();++i) QTRY_COMPARE_WITH_TIMEOUT(window.model()->data(window.model()->index(0,i)).toString(),expected[i],5000);
    }
    void iferrorRendersFallbackWithoutHidingCircularReferences() {
        QTemporaryDir temp; StartupOptions options; options.automaticWorkbook=false;
        MainWindow window(options); window.show(); QSignalSpy ready(&window,&MainWindow::serviceReady);
        QTRY_COMPARE_WITH_TIMEOUT(ready.count(),1,10000);
        QSignalSpy opened(&window,&MainWindow::workbookReady); window.openBook(temp.filePath("iferror.dhwb"),true);
        QTRY_COMPARE_WITH_TIMEOUT(opened.count(),1,10000);
        window.submit({{"action","edit"},{"cells",QJsonArray{
            QJsonObject{{"row",1},{"col",1},{"source","=IFERROR(1/0,42)"}},
            QJsonObject{{"row",1},{"col",2},{"source","=IFERROR(7,1/0)"}},
            QJsonObject{{"row",1},{"col",3},{"source","=IFERROR(C1,0)"}}}}});
        QTRY_COMPARE_WITH_TIMEOUT(opened.count(),2,10000);
        const QStringList expected{"42","7","#CYCLE!"};
        for(int i=0;i<expected.size();++i) QTRY_COMPARE_WITH_TIMEOUT(window.model()->data(window.model()->index(0,i)).toString(),expected[i],5000);
    }
    void logicalFunctionsRenderThroughTheManagedGrid() {
        QTemporaryDir temp; StartupOptions options; options.automaticWorkbook=false;
        MainWindow window(options); window.show(); QSignalSpy ready(&window,&MainWindow::serviceReady);
        QTRY_COMPARE_WITH_TIMEOUT(ready.count(),1,10000);
        QSignalSpy opened(&window,&MainWindow::workbookReady); window.openBook(temp.filePath("logical.dhwb"),true);
        QTRY_COMPARE_WITH_TIMEOUT(opened.count(),1,10000);
        QJsonArray cells;
        const QStringList formulas{"=AND(TRUE,1)","=OR(FALSE,0)","=NOT(FALSE)","=AND(FALSE,1/0)"};
        for(int i=0;i<formulas.size();++i) cells.append(QJsonObject{{"row",1},{"col",i+1},{"source",formulas[i]}});
        window.submit({{"action","edit"},{"cells",cells}}); QTRY_COMPARE_WITH_TIMEOUT(opened.count(),2,10000);
        const QStringList expected{"TRUE","FALSE","TRUE","#DIV/0!"};
        for(int i=0;i<expected.size();++i) QTRY_COMPARE_WITH_TIMEOUT(window.model()->data(window.model()->index(0,i)).toString(),expected[i],5000);
    }
    void aggregateFunctionsRenderThroughTheManagedGrid() {
        QTemporaryDir temp; StartupOptions options; options.automaticWorkbook=false;
        MainWindow window(options); window.show(); QSignalSpy ready(&window,&MainWindow::serviceReady);
        QTRY_COMPARE_WITH_TIMEOUT(ready.count(),1,10000);
        QSignalSpy opened(&window,&MainWindow::workbookReady); window.openBook(temp.filePath("functions.dhwb"),true);
        QTRY_COMPARE_WITH_TIMEOUT(opened.count(),1,10000);
        QJsonArray cells{QJsonObject{{"row",1},{"col",1},{"source","2"}},QJsonObject{{"row",2},{"col",1},{"source","4"}}};
        const QStringList functions{"AVERAGE","MIN","MAX","COUNT","COUNTA"};
        for(int i=0;i<functions.size();++i) cells.append(QJsonObject{{"row",1},{"col",i+2},{"source","="+functions[i]+"(A1:A2)"}});
        window.submit({{"action","edit"},{"cells",cells}});
        QTRY_COMPARE_WITH_TIMEOUT(opened.count(),2,10000);
        const QStringList expected{"3","2","4","2","2"};
        for(int i=0;i<expected.size();++i) QTRY_COMPARE_WITH_TIMEOUT(window.model()->data(window.model()->index(0,i+1)).toString(),expected[i],5000);
    }
    void installedRuntimeResolutionNeverFallsBackToDevelopmentSources() {
        QTemporaryDir temp; const auto bin=temp.filePath("bin"); QVERIFY(QDir().mkpath(bin));
        auto missing=resolveServicePaths(bin); QVERIFY(!missing.error.isEmpty()); QVERIFY(!missing.developmentFallback);
        const auto service=temp.filePath("share/datahorizon/service/datahorizon"); QVERIFY(QDir().mkpath(service));
        QFile launcher(service+"/launch.py"); QVERIFY(launcher.open(QIODevice::WriteOnly)); launcher.write("# fixture"); launcher.close();
        const auto selected=resolveServicePaths(bin,"/trusted/python"); QCOMPARE(selected.python,QString("/trusted/python")); QCOMPARE(selected.runtimeSource,QString("override"));
        QVERIFY(selected.error.isEmpty()); QVERIFY(!selected.developmentFallback);
        const auto runtime=temp.filePath("share/datahorizon/runtime"); QVERIFY(QDir().mkpath(runtime));
        const auto incomplete=resolveServicePaths(bin); QCOMPARE(incomplete.runtimeSource,QString("bundled")); QVERIFY(!incomplete.error.isEmpty());
        const auto overrideIncomplete=resolveServicePaths(bin,"/trusted/python"); QVERIFY(overrideIncomplete.error.isEmpty());
    }
    void interruptedAtomicExportRetainsOldOrCommittedDestination() {
        QTemporaryDir temp; const auto path=temp.filePath("export.xlsx");
        QFile original(path); QVERIFY(original.open(QIODevice::WriteOnly)); original.write("retained"); original.close();
        auto helper=QCoreApplication::applicationDirPath()+"/savefile_crash";
#ifdef Q_OS_WIN
        helper+=".exe";
#endif
        for(const auto &phase:{QString("before"),QString("after")}) {
            QProcess process; process.start(helper,{path,phase}); QVERIFY(process.waitForStarted()); QVERIFY(process.waitForFinished(10000));
            QCOMPARE(process.exitCode(),73);
            QFile result(path); QVERIFY(result.open(QIODevice::ReadOnly));
            QCOMPARE(result.readAll(),phase=="before"?QByteArray("retained"):QByteArray("replacement"));
        }
    }
    void nativeInspectionPreservesTheOpenWorkbookAndEditorDraft() {
        QTemporaryDir temp; StartupOptions options; options.automaticWorkbook=false;
        MainWindow window(options); window.show(); QSignalSpy ready(&window,&MainWindow::serviceReady);
        QTRY_COMPARE_WITH_TIMEOUT(ready.count(),1,10000);
        const auto path=temp.filePath("inspect.dhwb");
        QSignalSpy opened(&window,&MainWindow::workbookReady); window.openBook(path,true);
        QTRY_COMPARE_WITH_TIMEOUT(opened.count(),1,10000);
        const auto sheet=window.model()->sheet();
        auto *source=window.findChild<QLineEdit *>("source"); QVERIFY(source);
        source->setText("retained draft"); source->setModified(true);
        bool inspected=false; QTimer answer; answer.setInterval(10);
        connect(&answer,&QTimer::timeout,&window,[&]{
            if(auto *box=qobject_cast<QMessageBox *>(QApplication::activeModalWidget())) {
                if(box->objectName()=="nativeInspectionReview") { inspected=true; box->button(QMessageBox::Ok)->click(); }
            }
        });
        QTimer::singleShot(4000,&window,[]{
            for(auto *widget:QApplication::topLevelWidgets()) if(auto *box=qobject_cast<QMessageBox *>(widget)) {
                qWarning()<<"Inspection fixture unexpected modal"<<box->objectName()<<box->text();
                if(auto *button=box->button(QMessageBox::Ok)) button->click(); else box->reject();
            }
        });
        answer.start(); window.inspectNativeFile(path);
        QTRY_VERIFY_WITH_TIMEOUT(inspected,5000); answer.stop();
        QCOMPARE(opened.count(),1); QCOMPARE(window.model()->sheet(),sheet);
        QCOMPARE(source->text(),QString("retained draft")); QVERIFY(source->isModified());
        source->setModified(false);
    }
    void spreadsheetExportFailuresPreserveExistingFilesAndNativeAliases() {
        QTemporaryDir temp; StartupOptions options; options.automaticWorkbook=false;
        MainWindow window(options); window.show(); QSignalSpy ready(&window,&MainWindow::serviceReady);
        QTRY_COMPARE_WITH_TIMEOUT(ready.count(),1,10000);
        const auto native=temp.filePath("native.dhwb");
        QSignalSpy opened(&window,&MainWindow::workbookReady); window.openBook(native,true);
        QTRY_COMPARE_WITH_TIMEOUT(opened.count(),1,10000);
        window.submit({{"action","edit"},{"cells",QJsonArray{QJsonObject{{"row",1},{"col",1},{"source","7"}}}}});
        QTRY_COMPARE_WITH_TIMEOUT(opened.count(),2,10000);
        QFile source(native); QVERIFY(source.open(QIODevice::ReadOnly)); const auto original=source.readAll(); source.close();
        const auto alias=temp.filePath("native-alias.xlsx"); QVERIFY(QFile::link(native,alias));
        auto *issues=window.findChild<QPlainTextEdit *>("operationIssues"); QVERIFY(issues);
        for(const auto &format:{QString("xlsx"),QString("ods")}) {
            window.exportSpreadsheetTo(alias,format); QVERIFY(issues->toPlainText().contains("native workbook"));
            const auto existing=temp.filePath("retained."+format); QFile file(existing); QVERIFY(file.open(QIODevice::WriteOnly)); file.write("retained"); file.close();
            bool reviewed=false; QTimer answer; answer.setInterval(10);
            connect(&answer,&QTimer::timeout,&window,[&]{
                if(auto *box=qobject_cast<QMessageBox *>(QApplication::activeModalWidget())) {
                    if(box->objectName()=="spreadsheetExportReview") { reviewed=true; box->button(QMessageBox::Cancel)->click(); }
                }
            });
            QTimer::singleShot(5000,&answer,[]{
                for(auto *widget:QApplication::topLevelWidgets()) if(auto *box=qobject_cast<QMessageBox *>(widget)) {
                    if(auto *button=box->button(QMessageBox::Cancel)) button->click(); else if(auto *button=box->button(QMessageBox::Ok)) button->click();
                }
            });
            answer.start(); window.exportSpreadsheetTo(existing,format);
            QTRY_VERIFY_WITH_TIMEOUT(reviewed,5000); answer.stop();
            QVERIFY(file.open(QIODevice::ReadOnly)); QCOMPARE(file.readAll(),QByteArray("retained")); file.close();
            const auto directory=temp.filePath("directory-"+format); QVERIFY(QDir().mkdir(directory));
            answerNextDialog(QMessageBox::Yes); window.exportSpreadsheetTo(directory,format);
            QTRY_VERIFY_WITH_TIMEOUT(issues->toPlainText().contains("Spreadsheet destination failed"),5000);
            issues->clear();
        }
        QVERIFY(source.open(QIODevice::ReadOnly)); QCOMPARE(source.readAll(),original);
    }
    void odsImportAndExportPreserveFormattedFormulaThroughTheManagedGrid() {
        QTemporaryDir temp; StartupOptions options; options.automaticWorkbook=false;
        MainWindow window(options); window.show(); QSignalSpy ready(&window,&MainWindow::serviceReady);
        QTRY_COMPARE_WITH_TIMEOUT(ready.count(),1,10000);
        QSignalSpy opened(&window,&MainWindow::workbookReady); window.openBook(temp.filePath("ods.dhwb"),true);
        QTRY_COMPARE_WITH_TIMEOUT(opened.count(),1,10000);
        QFile fixture(QStringLiteral(DATAHORIZON_FIXTURES "/inputs.ods")); QVERIFY(fixture.open(QIODevice::ReadOnly));
        QSignalSpy changed(&window,&MainWindow::operationFinished);
        QString conversionReport;answerNextDialog(QMessageBox::Yes,nullptr,&conversionReport); window.importSpreadsheetBytes(fixture.readAll(),"ods");
        QTRY_COMPARE_WITH_TIMEOUT(changed.count(),1,10000);
        QVERIFY(conversionReport.contains("native ISO types"));QVERIFY(!conversionReport.contains("ISO Text"));
        QTRY_COMPARE_WITH_TIMEOUT(window.model()->data(window.model()->index(0,1)).toString(),QString("14.00"),5000);
        QVERIFY(window.model()->data(window.model()->index(0,1),Qt::FontRole).value<QFont>().bold());
        auto *grid=window.findChild<QTableView *>("grid"); QVERIFY(grid);
        grid->selectionModel()->select(QItemSelection(window.model()->index(0,0),window.model()->index(0,2)),QItemSelectionModel::ClearAndSelect);
        const auto output=temp.filePath("copy.ods");
        answerNextDialog(QMessageBox::Yes); window.exportSpreadsheetTo(output,"ods");
        QTRY_VERIFY_WITH_TIMEOUT(QFile::exists(output),5000);
        QFile exported(output); QVERIFY(exported.open(QIODevice::ReadOnly)); QVERIFY(exported.readAll().startsWith("PK"));
    }
    void xlsxImportReviewAndExportUseTheManagedGrid() {
        QTemporaryDir temp; StartupOptions options; options.automaticWorkbook=false;
        MainWindow window(options); window.show(); QSignalSpy ready(&window,&MainWindow::serviceReady);
        QTRY_COMPARE_WITH_TIMEOUT(ready.count(),1,10000);
        QSignalSpy opened(&window,&MainWindow::workbookReady); window.openBook(temp.filePath("xlsx.dhwb"),true);
        QTRY_COMPARE_WITH_TIMEOUT(opened.count(),1,10000);
        QFile fixture(QStringLiteral(DATAHORIZON_FIXTURES "/inputs.xlsx")); QVERIFY(fixture.open(QIODevice::ReadOnly));
        const auto data=fixture.readAll();
        answerNextDialog(QMessageBox::Cancel); window.importSpreadsheetBytes(data);
        auto *status=window.findChild<QLabel *>("status"); Q_UNUSED(status);
        QTRY_VERIFY_WITH_TIMEOUT(!window.findChild<QProgressDialog *>("xlsxPreparationProgress"),5000);
        QTRY_VERIFY_WITH_TIMEOUT(!QApplication::activeModalWidget(),5000);
        QCOMPARE(window.model()->data(window.model()->index(0,0)).toString(),QString());
        QSignalSpy changed(&window,&MainWindow::operationFinished);
        answerNextDialog(QMessageBox::Yes); window.importSpreadsheetBytes(data);
        QTRY_COMPARE_WITH_TIMEOUT(changed.count(),1,10000);
        QTRY_COMPARE_WITH_TIMEOUT(window.model()->data(window.model()->index(0,1)).toString(),QString("14.00"),5000);
        QVERIFY(window.model()->data(window.model()->index(0,1),Qt::FontRole).value<QFont>().bold());
        if(qEnvironmentVariableIsSet("DATAHORIZON_XLSX_SCREENSHOT")) window.grab().save(qEnvironmentVariable("DATAHORIZON_XLSX_SCREENSHOT"));
        auto *grid=window.findChild<QTableView *>("grid"); QVERIFY(grid);
        grid->selectionModel()->select(QItemSelection(window.model()->index(0,0),window.model()->index(0,2)),QItemSelectionModel::ClearAndSelect);
        const auto output=temp.filePath("copy.xlsx");
        answerNextDialog(QMessageBox::Yes); window.exportSpreadsheetTo(output);
        QTRY_VERIFY_WITH_TIMEOUT(QFile::exists(output),5000);
        QFile exported(output); QVERIFY(exported.open(QIODevice::ReadOnly)); QVERIFY(exported.readAll().startsWith("PK"));
    }
    void wholeWorkbookImportReplacesEmptyCatalogAndExportsSnapshot_data() {
        QTest::addColumn<QString>("format");
        QTest::newRow("xlsx")<<QString("xlsx"); QTest::newRow("ods")<<QString("ods");
    }
    void wholeWorkbookImportReplacesEmptyCatalogAndExportsSnapshot() {
        QFETCH(QString,format);
        QTemporaryDir temp; StartupOptions options; options.automaticWorkbook=false;
        MainWindow window(options); window.show(); QSignalSpy ready(&window,&MainWindow::serviceReady);
        QTRY_COMPARE_WITH_TIMEOUT(ready.count(),1,10000);
        QSignalSpy opened(&window,&MainWindow::workbookReady); window.openBook(temp.filePath("whole.dhwb"),true);
        QTRY_COMPARE_WITH_TIMEOUT(opened.count(),1,10000);
        QFile fixture(QStringLiteral(DATAHORIZON_FIXTURES "/inputs.")+format); QVERIFY(fixture.open(QIODevice::ReadOnly));
        // All sheets take the large path: upload, staged preparation, grouped review, Apply.
        importAndApply(window,fixture.fileName(),format,"formulas","replace_empty");
        QTRY_COMPARE_WITH_TIMEOUT(window.model()->data(window.model()->index(0,1)).toString(),QString("14.00"),5000);
        // The public export helper captures the full reviewed context itself.
        const auto output=temp.filePath("whole."+format);
        answerNextDialog(QMessageBox::Yes); window.exportSpreadsheetTo(output,format);
        QTRY_VERIFY_WITH_TIMEOUT(QFile::exists(output),5000);
    }
    void cancellingCsvPreparationCannotOpenReviewOrWriteCells() {
        QTemporaryDir temp; StartupOptions options; options.automaticWorkbook=false;
        MainWindow window(options); window.show(); QSignalSpy ready(&window,&MainWindow::serviceReady);
        QTRY_COMPARE_WITH_TIMEOUT(ready.count(),1,10000);
        QSignalSpy opened(&window,&MainWindow::workbookReady); window.openBook(temp.filePath("cancel.dhwb"),true);
        QTRY_COMPARE_WITH_TIMEOUT(opened.count(),1,10000);
        QSignalSpy changed(&window,&MainWindow::operationFinished);
        window.importCsvText("must not be written");
        auto *progress=window.findChild<QProgressDialog *>("csvPreparationProgress"); QVERIFY(progress);
        auto *cancel=progress->findChild<QPushButton *>(); QVERIFY(cancel); cancel->click();
        auto *issues=window.findChild<QPlainTextEdit *>("operationIssues"); QVERIFY(issues);
        QTRY_VERIFY_WITH_TIMEOUT(issues->toPlainText().contains("preparation cancelled"),5000);
        QCOMPARE(changed.count(),0); QVERIFY(!qobject_cast<QMessageBox *>(QApplication::activeModalWidget()));
        QCOMPARE(window.model()->data(window.model()->index(0,0)).toString(),QString());
    }
    void unifiedImportDetectsExtensions_data() {
        QTest::addColumn<QString>("extension");
        QTest::addColumn<QString>("expectedFirst");
        QTest::addColumn<QString>("expectedSecond");
        QTest::newRow("tsv")<<QString("TSV")<<QString("00123")<<QString("7");
        QTest::newRow("text")<<QString("txt")<<QString("00123")<<QString("7");
        QTest::newRow("xlsx")<<QString("XLSX")<<QString("7")<<QString("14.00");
        QTest::newRow("ods")<<QString("ods")<<QString("7")<<QString("14.00");
    }
    void unifiedImportDetectsExtensions() {
        QFETCH(QString,extension); QFETCH(QString,expectedFirst); QFETCH(QString,expectedSecond);
        const bool nativeDialogs=QApplication::testAttribute(Qt::AA_DontUseNativeDialogs);
        QApplication::setAttribute(Qt::AA_DontUseNativeDialogs,true);
        const auto restore=qScopeGuard([nativeDialogs]{QApplication::setAttribute(Qt::AA_DontUseNativeDialogs,nativeDialogs);});
        QTemporaryDir temp; const auto path=temp.filePath("input."+extension);
        const bool tabular=extension=="TSV" || extension=="txt";
        if(tabular) { QFile file(path); QVERIFY(file.open(QIODevice::WriteOnly)); file.write(extension=="TSV"?"00123\t7":"00123,7"); }
        else QVERIFY(QFile::copy(QStringLiteral(DATAHORIZON_FIXTURES "/inputs.")+extension.toLower(),path));
        StartupOptions options; options.automaticWorkbook=false; MainWindow window(options); window.show();
        QSignalSpy ready(&window,&MainWindow::serviceReady); QTRY_COMPARE_WITH_TIMEOUT(ready.count(),1,10000);
        QSignalSpy opened(&window,&MainWindow::workbookReady); window.openBook(temp.filePath("native.dhwb"),true);
        QTRY_COMPARE_WITH_TIMEOUT(opened.count(),1,10000);
        auto *import=window.findChild<QAction *>("importAction"); QVERIFY(import);
        int imports=0,exports=0;
        auto *fileMenu=qobject_cast<QMenu *>(import->parent());QVERIFY(fileMenu);
        for(auto *action:fileMenu->actions()) {
            imports+=action->text().startsWith("Import"); exports+=action->text().startsWith("Export");
        }
        QCOMPARE(imports,1); QCOMPARE(exports,1);
        bool pickerSeen=false,optionsSeen=false,correctDelimiter=true,timedOut=false;
        QElapsedTimer elapsed; elapsed.start(); QTimer answer; answer.setInterval(10);
        connect(&answer,&QTimer::timeout,&window,[&]{
            auto *modal=QApplication::activeModalWidget();
            if(elapsed.elapsed()>10000) { timedOut=true; if(auto *dialog=qobject_cast<QDialog *>(modal)) dialog->reject(); return; }
            if(auto *picker=qobject_cast<QFileDialog *>(modal)) {
                if(!pickerSeen) {
                    pickerSeen=true;
                    QCOMPARE(picker->nameFilters().size(),1);
                    QVERIFY(picker->nameFilters()[0].contains("*.ods")); QVERIFY(!picker->nameFilters()[0].contains("(*)"));
                    picker->setDirectory(temp.path());
                    auto *name=picker->findChild<QLineEdit *>("fileNameEdit"); QVERIFY(name); name->setText(path);
                }
                if(auto *buttons=picker->findChild<QDialogButtonBox *>())
                    if(auto *button=buttons->button(QDialogButtonBox::Open);button && button->isEnabled()) QTimer::singleShot(0,button,[button]{button->click();});
            } else if(auto *dialog=qobject_cast<QDialog *>(modal);dialog && dialog->objectName()==(tabular?"csvOptionsDialog":"spreadsheetOptionsDialog")) {
                optionsSeen=true;
                if(tabular) correctDelimiter=dialog->findChild<QComboBox *>("csvDelimiter")->currentData().toString()==(extension=="TSV"?"\t":",");
                dialog->accept();
            } else if(auto *box=qobject_cast<QMessageBox *>(modal)) {
                if(auto *button=box->button(QMessageBox::Yes)) button->click();
            }
        });
        QSignalSpy changed(&window,&MainWindow::operationFinished);
        answer.start(); import->trigger(); QTRY_COMPARE_WITH_TIMEOUT(changed.count(),1,10000); answer.stop();
        QVERIFY(!timedOut); QVERIFY(pickerSeen); QVERIFY(optionsSeen); QVERIFY(correctDelimiter); QVERIFY(changed[0][0].toBool());
        QTRY_COMPARE_WITH_TIMEOUT(window.model()->data(window.model()->index(0,0)).toString(),expectedFirst,5000);
        QTRY_COMPARE_WITH_TIMEOUT(window.model()->data(window.model()->index(0,1)).toString(),expectedSecond,5000);
    }
    // A synchronous call to the window's own service, for checking what the
    // desktop left behind there (released uploads, jobs and exports).
    static QPair<QJsonObject,QString> serviceCall(MainWindow &window,const QString &path,bool post=true) {
        auto *client=window.findChild<RestClient *>(); if(!client) return {{},"no client"};
        QPair<QJsonObject,QString> answer; bool done=false; QEventLoop loop;
        client->request(path,{},[&](QJsonObject result,QString error){answer={result,error};done=true;loop.quit();},post);
        QTimer::singleShot(10000,&loop,&QEventLoop::quit); if(!done) loop.exec();
        return answer;
    }
    static QDialog *visibleDialog(MainWindow &window,const QString &name) {
        for(auto *dialog:window.findChildren<QDialog *>(name)) if(dialog->isVisible()) return dialog;
        return nullptr;
    }
    static QStringList tabTexts(MainWindow &window) {
        QStringList texts; auto *tabs=window.findChild<QTabBar *>("sheets"); if(!tabs) return texts;
        for(int i=0;i<tabs->count();++i) texts.append(tabs->tabText(i));
        return texts;
    }
    static void openFresh(MainWindow &window,const QString &path) {
        QSignalSpy ready(&window,&MainWindow::serviceReady); QTRY_COMPARE_WITH_TIMEOUT(ready.count(),1,10000);
        QSignalSpy opened(&window,&MainWindow::workbookReady); window.openBook(path,true);
        QTRY_COMPARE_WITH_TIMEOUT(opened.count(),1,10000);
    }
    // Starts a large import, waits for its review and clicks Apply.
    static void importAndApply(MainWindow &window,const QString &path,const QString &format,const QString &mode,const QString &target) {
        QSignalSpy changed(&window,&MainWindow::operationFinished);
        window.importWorkbookFile(path,format,mode,target);
        QDialog *review=nullptr; QTRY_VERIFY_WITH_TIMEOUT((review=visibleDialog(window,"largeImportReview")),20000);
        auto *apply=review->findChild<QPushButton *>("importApply"); QVERIFY(apply); QVERIFY(apply->isEnabled());
        QVERIFY(!apply->isDefault()); apply->click();
        QTRY_COMPARE_WITH_TIMEOUT(changed.count(),1,20000); QVERIFY(changed[0][0].toBool());
    }
    void largeWorkbookImportUploadsReviewsAndApplies() {
        QTemporaryDir temp; StartupOptions options; options.automaticWorkbook=false;
        MainWindow window(options); window.show(); openFresh(window,temp.filePath("large.dhwb"));
        QSignalSpy changed(&window,&MainWindow::operationFinished);
        window.importWorkbookFile(QStringLiteral(DATAHORIZON_FIXTURES "/fidelity.xlsx"),"xlsx","mixed","replace_empty");
        QPointer<QProgressDialog> progress=window.findChild<QProgressDialog *>("largeImportProgress"); QVERIFY(progress);
        const auto upload=progress->property("uploadId").toString(), job=progress->property("jobId").toString();
        QCOMPARE(progress->findChild<QLabel *>()->textFormat(),Qt::PlainText);
        QDialog *review=nullptr; QTRY_VERIFY_WITH_TIMEOUT((review=visibleDialog(window,"largeImportReview")),20000);
        QTRY_VERIFY_WITH_TIMEOUT(!progress,5000);
        // The service's scope statements sit in a collapsed Details section.
        auto *scopeLines=review->findChild<QLabel *>("importReviewScope"); QVERIFY(!scopeLines->isVisibleTo(review));
        review->findChild<QToolButton *>("importReviewDetailsToggle")->click(); QVERIFY(scopeLines->isVisibleTo(review));
        QVERIFY(review->findChild<QLabel *>("importReviewSummary")->text().contains("cannot be undone"));
        auto *sheets=review->findChild<QTreeWidget *>("importSheets"); QVERIFY(sheets); QCOMPARE(sheets->topLevelItemCount(),3);
        QCOMPARE(sheets->topLevelItem(0)->text(0),QString("Inputs")); QCOMPARE(sheets->topLevelItem(0)->text(1),QString("48"));
        QVERIFY(review->findChild<ConversionReportView *>("conversionReport"));
        QVERIFY(!review->findChild<QLabel *>("importReviewScope")->text().isEmpty());
        // The upload is released once the stage is prepared; the job holds the stage until Apply.
        QTRY_COMPARE_WITH_TIMEOUT(serviceCall(window,"/v1/uploads/"+upload+"/release").first["code"].toString(),QString("JOB_NOT_FOUND"),5000);
        QCOMPARE(serviceCall(window,"/v1/jobs/"+job,false).first["status"].toString(),QString("completed"));
        review->findChild<QPushButton *>("importApply")->click();
        QTRY_COMPARE_WITH_TIMEOUT(changed.count(),1,20000); QVERIFY(changed[0][0].toBool());
        QTRY_COMPARE_WITH_TIMEOUT(tabTexts(window),QStringList({"Inputs","Summary","Empty"}),10000);
        QTRY_COMPARE_WITH_TIMEOUT(window.findChild<QTabBar *>("sheets")->tabText(window.findChild<QTabBar *>("sheets")->currentIndex()),QString("Inputs"),5000);
        QTRY_COMPARE_WITH_TIMEOUT(window.model()->data(window.model()->index(0,1)).toString(),QString("14.00"),5000);
        QTRY_VERIFY_WITH_TIMEOUT(!visibleDialog(window,"largeImportReview"),5000);
        QTRY_COMPARE_WITH_TIMEOUT(serviceCall(window,"/v1/jobs/"+job,false).first["code"].toString(),QString("JOB_NOT_FOUND"),5000);
    }
    void importReportGroupsBySeverity() {
        QTemporaryDir temp; StartupOptions options; options.automaticWorkbook=false;
        MainWindow window(options); window.show(); openFresh(window,temp.filePath("report.dhwb"));
        // Keep every formula: INFO has no cached result to fall back on, so it is kept uncalculated.
        window.importWorkbookFile(QStringLiteral(DATAHORIZON_FIXTURES "/unsupported.xlsx"),"xlsx","formulas","replace_empty");
        QDialog *review=nullptr; QTRY_VERIFY_WITH_TIMEOUT((review=visibleDialog(window,"largeImportReview")),20000);
        auto *report=review->findChild<ConversionReportView *>("conversionReport"); QVERIFY(report);
        // The producer's theme font colour is omitted too; groups follow severity order.
        auto *tree=report->tree(); QCOMPARE(tree->topLevelItemCount(),2);
        QCOMPARE(tree->topLevelItem(1)->data(0,Qt::UserRole).toString(),QString("omitted"));
        QCOMPARE(tree->topLevelItem(1)->text(0),QString("Left out"));
        auto *group=tree->topLevelItem(0);
        QCOMPARE(group->data(0,Qt::UserRole).toString(),QString("unevaluable"));
        QCOMPARE(group->text(0),QString("Formulas kept but not calculated")); QCOMPARE(group->text(2),QString("1"));
        QCOMPARE(group->childCount(),1);
        auto *row=group->child(0);
        QCOMPARE(row->data(0,Qt::UserRole).toString(),QString("formula.unsupported_function"));
        QCOMPARE(row->text(0),QString("Unsupported function")); QCOMPARE(row->text(2),QString("1"));
        QCOMPARE(row->childCount(),1); QCOMPARE(row->child(0)->text(0),QString("Data!B1"));
        auto *totals=report->findChild<QLabel *>("conversionTotals"); QVERIFY(totals);
        QVERIFY(totals->text().contains("Formulas kept but not calculated 1"));
        if(qEnvironmentVariableIsSet("DATAHORIZON_IMPORT_REVIEW_SCREENSHOT")) QVERIFY(review->grab().save(qEnvironmentVariable("DATAHORIZON_IMPORT_REVIEW_SCREENSHOT")));
        auto *cancel=review->findChild<QPushButton *>("importCancel"); QVERIFY(cancel); QVERIFY(cancel->isDefault());
        const auto job=review->property("jobId").toString();
        cancel->click();
        QTRY_COMPARE_WITH_TIMEOUT(serviceCall(window,"/v1/jobs/"+job,false).first["code"].toString(),QString("JOB_NOT_FOUND"),5000);
        QCOMPARE(tabTexts(window),QStringList({"Sheet1"}));
    }
    void importAddsSheetsToNonEmptyWorkbook() {
        QTemporaryDir temp; StartupOptions options; options.automaticWorkbook=false;
        MainWindow window(options); window.show(); openFresh(window,temp.filePath("add.dhwb"));
        QSignalSpy opened(&window,&MainWindow::workbookReady);
        window.submit({{"action","edit"},{"cells",QJsonArray{QJsonObject{{"row",1},{"col",1},{"source","kept"}}}}});
        QTRY_COMPARE_WITH_TIMEOUT(opened.count(),1,10000);
        importAndApply(window,QStringLiteral(DATAHORIZON_FIXTURES "/fidelity.xlsx"),"xlsx","mixed","add_sheets");
        QTRY_COMPARE_WITH_TIMEOUT(tabTexts(window),QStringList({"Sheet1","Inputs","Summary","Empty"}),10000);
        auto *tabs=window.findChild<QTabBar *>("sheets"); QTRY_COMPARE_WITH_TIMEOUT(tabs->currentIndex(),1,5000);
        QTRY_COMPARE_WITH_TIMEOUT(window.model()->data(window.model()->index(0,1)).toString(),QString("14.00"),5000);
        tabs->setCurrentIndex(0);
        QTRY_COMPARE_WITH_TIMEOUT(window.model()->data(window.model()->index(0,0)).toString(),QString("kept"),5000);
    }
    void importNameConflictLeavesWorkbookUnchanged() {
        QTemporaryDir temp; StartupOptions options; options.automaticWorkbook=false;
        MainWindow window(options); window.show(); openFresh(window,temp.filePath("conflict.dhwb"));
        QSignalSpy opened(&window,&MainWindow::workbookReady);
        window.submit({{"action","sheet_add"},{"name","summary"}}); QTRY_COMPARE_WITH_TIMEOUT(opened.count(),1,10000);
        QSignalSpy changed(&window,&MainWindow::operationFinished);
        window.importWorkbookFile(QStringLiteral(DATAHORIZON_FIXTURES "/fidelity.xlsx"),"xlsx","mixed","add_sheets");
        QDialog *review=nullptr; QTRY_VERIFY_WITH_TIMEOUT((review=visibleDialog(window,"largeImportReview")),20000);
        const auto job=review->property("jobId").toString();
        auto *apply=review->findChild<QPushButton *>("importApply"); apply->click();
        QTRY_COMPARE_WITH_TIMEOUT(changed.count(),1,20000); QVERIFY(!changed[0][0].toBool());
        auto *message=review->findChild<QLabel *>("importReviewMessage"); QVERIFY(message);
        QTRY_VERIFY_WITH_TIMEOUT(message->isVisible(),5000);
        QVERIFY(message->text().contains("Summary")); QCOMPARE(message->textFormat(),Qt::PlainText);
        QVERIFY(review->isVisible()); QVERIFY(!apply->isEnabled());
        QCOMPARE(tabTexts(window),QStringList({"Sheet1","summary"}));
        review->findChild<QPushButton *>("importCancel")->click();
        QTRY_COMPARE_WITH_TIMEOUT(serviceCall(window,"/v1/jobs/"+job,false).first["code"].toString(),QString("JOB_NOT_FOUND"),5000);
        QCOMPARE(tabTexts(window),QStringList({"Sheet1","summary"}));
    }
    void cancelDuringUploadLeavesNoStage() {
        QTemporaryDir temp; StartupOptions options; options.automaticWorkbook=false;
        const auto path=temp.filePath("big.xlsx");
        { QFile big(path); QVERIFY(big.open(QIODevice::WriteOnly)); const QByteArray block(1024*1024,'x'); for(int i=0;i<48;++i) big.write(block); }
        MainWindow window(options); window.show(); openFresh(window,temp.filePath("upload.dhwb"));
        QSignalSpy changed(&window,&MainWindow::operationFinished);
        window.importWorkbookFile(path,"xlsx","mixed","replace_empty");
        QPointer<QProgressDialog> progress=window.findChild<QProgressDialog *>("largeImportProgress"); QVERIFY(progress);
        const auto upload=progress->property("uploadId").toString(), job=progress->property("jobId").toString();
        bool cancelledDuringUpload=false; QTimer watch; watch.setInterval(0);
        connect(&watch,&QTimer::timeout,&window,[&]{
            if(progress && progress->labelText().startsWith("Uploading") && progress->value()<progress->maximum()) {
                cancelledDuringUpload=true; watch.stop(); progress->findChild<QPushButton *>()->click();
            }
        });
        watch.start();
        auto *issues=window.findChild<QPlainTextEdit *>("operationIssues"); QVERIFY(issues);
        QTRY_VERIFY_WITH_TIMEOUT(issues->toPlainText().contains("Import cancelled"),20000); watch.stop();
        QVERIFY(cancelledDuringUpload); QVERIFY(!visibleDialog(window,"largeImportReview"));
        QTRY_COMPARE_WITH_TIMEOUT(serviceCall(window,"/v1/uploads/"+upload+"/release").first["code"].toString(),QString("JOB_NOT_FOUND"),5000);
        QTRY_COMPARE_WITH_TIMEOUT(serviceCall(window,"/v1/jobs/"+job,false).first["code"].toString(),QString("JOB_NOT_FOUND"),5000);
        QCOMPARE(changed.count(),0); QCOMPARE(tabTexts(window),QStringList({"Sheet1"}));
    }
    void wholeWorkbookExportDownloadsThroughJob() {
        QTemporaryDir temp; StartupOptions options; options.automaticWorkbook=false;
        MainWindow window(options); window.show(); openFresh(window,temp.filePath("native.dhwb"));
        QSignalSpy opened(&window,&MainWindow::workbookReady);
        window.submit({{"action","edit"},{"cells",QJsonArray{QJsonObject{{"row",1},{"col",1},{"source","7"}},QJsonObject{{"row",1},{"col",2},{"source","=A1*2"}}}}});
        QTRY_COMPARE_WITH_TIMEOUT(opened.count(),1,10000);
        const auto output=temp.filePath("out/whole.xlsx"); QVERIFY(QDir().mkpath(temp.filePath("out")));
        window.exportWorkbookFile(output,"xlsx");
        QPointer<QProgressDialog> progress=window.findChild<QProgressDialog *>("largeExportProgress"); QVERIFY(progress);
        const auto job=progress->property("jobId").toString();
        QTRY_VERIFY_WITH_TIMEOUT(QFile::exists(output),20000); QTRY_VERIFY_WITH_TIMEOUT(!progress,5000);
        QFile file(output); QVERIFY(file.open(QIODevice::ReadOnly)); const auto data=file.readAll();
        QVERIFY(data.startsWith("PK")); QVERIFY(data.contains("xl/workbook.xml"));
        QCOMPARE(QDir(temp.filePath("out")).entryList(QDir::Files|QDir::Hidden|QDir::NoDotAndDotDot),QStringList({"whole.xlsx"}));
        QTRY_COMPARE_WITH_TIMEOUT(serviceCall(window,"/v1/exports/"+job,false).first["code"].toString(),QString("JOB_NOT_FOUND"),5000);
        QTRY_COMPARE_WITH_TIMEOUT(serviceCall(window,"/v1/jobs/"+job,false).first["code"].toString(),QString("JOB_NOT_FOUND"),5000);
        QCOMPARE(opened.count(),1);
    }
    void importExpiredStageOffersToPrepareAgain() {
        QTemporaryDir temp; StartupOptions options; options.automaticWorkbook=false;
        MainWindow window(options); window.show(); openFresh(window,temp.filePath("expired.dhwb"));
        window.importWorkbookFile(QStringLiteral(DATAHORIZON_FIXTURES "/fidelity.xlsx"),"xlsx","mixed","replace_empty");
        QDialog *review=nullptr; QTRY_VERIFY_WITH_TIMEOUT((review=visibleDialog(window,"largeImportReview")),20000);
        const auto job=review->property("jobId").toString();
        QVERIFY(serviceCall(window,"/v1/jobs/"+job+"/release").second.isEmpty());
        bool offered=false,plainOffer=false; QString offer; QTimer answer; answer.setInterval(10);
        connect(&answer,&QTimer::timeout,&window,[&]{
            if(auto *box=qobject_cast<QMessageBox *>(QApplication::activeModalWidget()); box && box->objectName()=="importExpiredReview") {
                offered=true; offer=box->text(); plainOffer=box->textFormat()==Qt::PlainText; answer.stop(); box->button(QMessageBox::Yes)->click();
            }
        });
        answer.start();
        QSignalSpy changed(&window,&MainWindow::operationFinished);
        review->findChild<QPushButton *>("importApply")->click();
        QTRY_VERIFY_WITH_TIMEOUT(offered,20000); QVERIFY(offer.contains("expired")); QVERIFY(plainOffer);
        QCOMPARE(changed.count(),1); QVERIFY(!changed[0][0].toBool());
        QDialog *again=nullptr;
        QTRY_VERIFY_WITH_TIMEOUT((again=visibleDialog(window,"largeImportReview")) && again->property("jobId").toString()!=job,20000);
        again->reject();
        QTRY_VERIFY_WITH_TIMEOUT(!visibleDialog(window,"largeImportReview"),5000);
        QCOMPARE(tabTexts(window),QStringList({"Sheet1"}));
    }
    // Drives File → Import… with the given file up to the options dialog and
    // hands that dialog to inspect; inspect accepts or rejects it.
    static bool driveImportOptions(MainWindow &window,const QString &path,const std::function<void(QDialog *)> &inspect) {
        const bool nativeDialogs=QApplication::testAttribute(Qt::AA_DontUseNativeDialogs);
        QApplication::setAttribute(Qt::AA_DontUseNativeDialogs,true);
        const auto restore=qScopeGuard([nativeDialogs]{QApplication::setAttribute(Qt::AA_DontUseNativeDialogs,nativeDialogs);});
        bool pickerSeen=false,optionsSeen=false; QElapsedTimer elapsed; elapsed.start(); QTimer answer; answer.setInterval(10);
        QObject::connect(&answer,&QTimer::timeout,&window,[&]{
            auto *modal=QApplication::activeModalWidget();
            if(elapsed.elapsed()>15000) { if(auto *dialog=qobject_cast<QDialog *>(modal)) dialog->reject(); return; }
            if(auto *picker=qobject_cast<QFileDialog *>(modal)) {
                if(!pickerSeen) { pickerSeen=true; picker->setDirectory(QFileInfo(path).absolutePath()); picker->findChild<QLineEdit *>("fileNameEdit")->setText(path); }
                if(auto *buttons=picker->findChild<QDialogButtonBox *>())
                    if(auto *button=buttons->button(QDialogButtonBox::Open);button && button->isEnabled()) QTimer::singleShot(0,button,[button]{button->click();});
            } else if(auto *dialog=qobject_cast<QDialog *>(modal);dialog && dialog->objectName()=="spreadsheetOptionsDialog" && !optionsSeen) {
                optionsSeen=true; answer.stop(); inspect(dialog);
            }
        });
        answer.start(); window.findChild<QAction *>("importAction")->trigger();
        QElapsedTimer wait; wait.start();
        while(!optionsSeen && wait.elapsed()<16000) QTest::qWait(20);
        return optionsSeen;
    }
    void importModeChooserPreselectsMixed() {
        QTemporaryDir temp; StartupOptions options; options.automaticWorkbook=false;
        MainWindow window(options); window.show(); openFresh(window,temp.filePath("mode.dhwb"));
        const auto path=temp.filePath("input.xlsx"); QVERIFY(QFile::copy(QStringLiteral(DATAHORIZON_FIXTURES "/inputs.xlsx"),path));
        QStringList labels,modes; bool replaceEnabled=false,hintVisible=true; QString target;
        QVERIFY(driveImportOptions(window,path,[&](QDialog *dialog){
            auto *mode=dialog->findChild<QComboBox *>("importMode");
            for(int i=0;i<mode->count();++i){labels.append(mode->itemText(i));modes.append(mode->itemData(i).toString());}
            modes.prepend(mode->currentData().toString());
            auto *destination=dialog->findChild<QComboBox *>("importTarget");
            replaceEnabled=qobject_cast<QStandardItemModel *>(destination->model())->item(0)->isEnabled();
            target=destination->currentData().toString();
            hintVisible=dialog->findChild<QLabel *>("importTargetHint")->isVisibleTo(dialog);
            dialog->reject();
        }));
        QCOMPARE(labels,QStringList({"Formulas where possible, values otherwise","Keep every formula","Values only"}));
        QCOMPARE(modes,QStringList({"mixed","mixed","formulas","values"}));
        QVERIFY(replaceEnabled); QCOMPARE(target,QString("replace_empty")); QVERIFY(!hintVisible);
    }
    void replaceTargetDisabledForNonEmptyWorkbook_data() {
        QTest::addColumn<bool>("reopened");
        // "edited": the window's undo history already rules Replace out.
        // "stored": a reopened file has no history, so only the cell read finds the stored cell.
        QTest::newRow("edited")<<false; QTest::newRow("stored")<<true;
    }
    void replaceTargetDisabledForNonEmptyWorkbook() {
        QFETCH(bool,reopened);
        QTemporaryDir temp; StartupOptions options; options.automaticWorkbook=false;
        const auto native=temp.filePath("full.dhwb");
        if(reopened) {
            MainWindow writer(options); writer.show(); openFresh(writer,native);
            QSignalSpy written(&writer,&MainWindow::workbookReady);
            writer.submit({{"action","edit"},{"cells",QJsonArray{QJsonObject{{"row",3},{"col",2},{"source","7"}}}}});
            QTRY_COMPARE_WITH_TIMEOUT(written.count(),1,10000);
        }
        MainWindow window(options); window.show();
        if(reopened) {
            QSignalSpy ready(&window,&MainWindow::serviceReady); QTRY_COMPARE_WITH_TIMEOUT(ready.count(),1,10000);
            QSignalSpy opened(&window,&MainWindow::workbookReady); window.openBook(native); QTRY_COMPARE_WITH_TIMEOUT(opened.count(),1,10000);
            QAction *undo=nullptr; for(auto *action:window.findChildren<QAction *>()) if(action->text().startsWith("Undo")) undo=action;
            QVERIFY(undo); QCOMPARE(undo->text(),QString("Undo"));
            QCOMPARE(tabTexts(window),QStringList({"Sheet1"}));
        } else {
            openFresh(window,native);
            QSignalSpy opened(&window,&MainWindow::workbookReady);
            window.submit({{"action","edit"},{"cells",QJsonArray{QJsonObject{{"row",1},{"col",1},{"source","7"}}}}});
            QTRY_COMPARE_WITH_TIMEOUT(opened.count(),1,10000);
        }
        const auto path=temp.filePath("input.xlsx"); QVERIFY(QFile::copy(QStringLiteral(DATAHORIZON_FIXTURES "/inputs.xlsx"),path));
        bool replaceEnabled=true,hintVisible=false; QString target,hint;
        QVERIFY(driveImportOptions(window,path,[&](QDialog *dialog){
            auto *scope=dialog->findChild<QComboBox *>("importScope"); scope->setCurrentIndex(scope->findData("workbook"));
            auto *destination=dialog->findChild<QComboBox *>("importTarget");
            replaceEnabled=qobject_cast<QStandardItemModel *>(destination->model())->item(0)->isEnabled();
            target=destination->currentData().toString();
            auto *label=dialog->findChild<QLabel *>("importTargetHint"); hintVisible=label->isVisibleTo(dialog); hint=label->text();
            dialog->reject();
        }));
        QVERIFY(!replaceEnabled); QCOMPARE(target,QString("add_sheets")); QVERIFY(hintVisible); QVERIFY(hint.contains("Replace is available only"));
    }
    void odsWholeWorkbookImportUsesLargePath() {
        QTemporaryDir temp; StartupOptions options; options.automaticWorkbook=false;
        MainWindow window(options); window.show(); openFresh(window,temp.filePath("ods.dhwb"));
        const auto path=temp.filePath("fidelity.ods"); QVERIFY(QFile::copy(QStringLiteral(DATAHORIZON_FIXTURES "/fidelity.ods"),path));
        QVERIFY(driveImportOptions(window,path,[&](QDialog *dialog){
            auto *scope=dialog->findChild<QComboBox *>("importScope"); scope->setCurrentIndex(scope->findData("workbook"));
            dialog->accept();
        }));
        QSignalSpy changed(&window,&MainWindow::operationFinished);
        QDialog *review=nullptr; QTRY_VERIFY_WITH_TIMEOUT((review=visibleDialog(window,"largeImportReview")),20000);
        QCOMPARE(review->findChild<QTreeWidget *>("importSheets")->topLevelItemCount(),3);
        review->findChild<QPushButton *>("importApply")->click();
        QTRY_COMPARE_WITH_TIMEOUT(changed.count(),1,20000); QVERIFY(changed[0][0].toBool());
        QTRY_COMPARE_WITH_TIMEOUT(tabTexts(window),QStringList({"Inputs","Summary","Empty"}),10000);
        QTRY_COMPARE_WITH_TIMEOUT(window.model()->data(window.model()->index(0,1)).toString(),QString("14.00"),5000);
    }
    void exportCancelReleasesExportAndWritesNoFile() {
        QTemporaryDir temp; StartupOptions options; options.automaticWorkbook=false;
        MainWindow window(options); window.show(); openFresh(window,temp.filePath("native.dhwb"));
        QSignalSpy opened(&window,&MainWindow::workbookReady);
        window.submit({{"action","edit"},{"cells",QJsonArray{QJsonObject{{"row",1},{"col",1},{"source","7"}}}}});
        QTRY_COMPARE_WITH_TIMEOUT(opened.count(),1,10000);
        QVERIFY(QDir().mkpath(temp.filePath("out"))); const auto output=temp.filePath("out/cancelled.ods");
        window.exportWorkbookFile(output,"ods");
        QPointer<QProgressDialog> progress=window.findChild<QProgressDialog *>("largeExportProgress"); QVERIFY(progress);
        const auto job=progress->property("jobId").toString();
        progress->findChild<QPushButton *>()->click();
        auto *issues=window.findChild<QPlainTextEdit *>("operationIssues");
        QTRY_VERIFY_WITH_TIMEOUT(issues->toPlainText().contains("Export cancelled"),5000);
        // The job may be created after the cancel arrives; it is discarded then too.
        QTest::qWait(1000);
        QTRY_COMPARE_WITH_TIMEOUT(serviceCall(window,"/v1/jobs/"+job,false).first["code"].toString(),QString("JOB_NOT_FOUND"),5000);
        QCOMPARE(serviceCall(window,"/v1/exports/"+job,false).first["code"].toString(),QString("JOB_NOT_FOUND"));
        QCOMPARE(QDir(temp.filePath("out")).entryList(QDir::Files|QDir::Hidden|QDir::NoDotAndDotDot),QStringList());
        QCOMPARE(opened.count(),1);
    }
    void reportViewShowsPlainTextForMarkupInSheetNames() {
        ConversionReportView view;
        QCOMPARE(view.objectName(),QString("conversionReport"));
        view.setReport({});
        QCOMPARE(view.findChild<QLabel *>("conversionTotals")->text(),QString("Nothing was changed or left out."));
        const QJsonObject entry{{"category","formula.unsupported_function"},{"severity","unevaluable"},{"detail","<i>INFO</i>"},{"count",2},
                                {"samples",QJsonArray{QJsonObject{{"sheet","<b>x</b>"},{"cell","B1"}},QJsonObject{{"sheet","<b>x</b>"},{"cell","B2"}}}}};
        const QJsonObject other{{"category","vendor.custom"},{"severity","converted"},{"detail","kept"},{"count",1},{"samples",QJsonArray{}}};
        view.setReport({{"report_entries",QJsonArray{other,entry}},{"totals",QJsonObject{{"unevaluable",2},{"omitted",0},{"approximated",0},{"converted",1}}}});
        auto *tree=view.tree(); QCOMPARE(tree->topLevelItemCount(),2);
        QCOMPARE(tree->topLevelItem(0)->text(0),QString("Formulas kept but not calculated"));
        QCOMPARE(tree->topLevelItem(1)->text(0),QString("Converted"));
        QCOMPARE(tree->topLevelItem(1)->child(0)->text(0),QString("vendor.custom"));
        auto *row=tree->topLevelItem(0)->child(0);
        QCOMPARE(row->text(1),QString("<i>INFO</i>")); QCOMPARE(row->childCount(),2);
        QCOMPARE(row->child(0)->text(0),QString("<b>x</b>!B1"));
        auto *totals=view.findChild<QLabel *>("conversionTotals"); QCOMPARE(totals->textFormat(),Qt::PlainText);
        QVERIFY(totals->text().contains("Converted 1"));
        QVERIFY(!tree->wordWrap()); QCOMPARE(tree->textElideMode(),Qt::ElideMiddle);
        // Tooltips interpret markup they detect: the escaped tip renders literally.
        auto rendered=[](const QString &tip){QTextDocument document; if(Qt::mightBeRichText(tip)) document.setHtml(tip); else document.setPlainText(tip); return document.toPlainText();};
        QCOMPARE(rendered(row->toolTip(1)),QString("<i>INFO</i>"));
        QCOMPARE(rendered(row->child(0)->toolTip(0)),QString("<b>x</b>!B1"));
        QVERIFY(!row->toolTip(1).contains("<i>"));
        // Through the import: the sheet list, its tooltip and a conflict message
        // show a markup sheet name literally. Sheet names cannot contain "/".
        QTemporaryDir temp; StartupOptions options; options.automaticWorkbook=false;
        MainWindow window(options); window.show(); openFresh(window,temp.filePath("markup.dhwb"));
        QSignalSpy opened(&window,&MainWindow::workbookReady);
        window.submit({{"action","sheet_add"},{"name","<b>x<b>"}}); QTRY_COMPARE_WITH_TIMEOUT(opened.count(),1,10000);
        QSignalSpy changed(&window,&MainWindow::operationFinished);
        window.importWorkbookFile(QStringLiteral(DATAHORIZON_FIXTURES "/markup.xlsx"),"xlsx","mixed","add_sheets");
        QDialog *review=nullptr; QTRY_VERIFY_WITH_TIMEOUT((review=visibleDialog(window,"largeImportReview")),20000);
        auto *sheets=review->findChild<QTreeWidget *>("importSheets");
        QCOMPARE(sheets->topLevelItem(0)->text(0),QString("<b>x<b>"));
        QCOMPARE(rendered(sheets->topLevelItem(0)->toolTip(0)),QString("<b>x<b>"));
        for(auto *label:review->findChildren<QLabel *>()) QCOMPARE(label->textFormat(),Qt::PlainText);
        review->findChild<QPushButton *>("importApply")->click();
        QTRY_COMPARE_WITH_TIMEOUT(changed.count(),1,20000);
        auto *message=review->findChild<QLabel *>("importReviewMessage"); QTRY_VERIFY_WITH_TIMEOUT(message->isVisible(),5000);
        QVERIFY(message->text().contains("<b>x<b>")); QCOMPARE(message->textFormat(),Qt::PlainText);
        review->reject();
    }

    void cancelAfterJobStartLeavesNoUploadOrJob() {
        QTemporaryDir temp; StartupOptions options; options.automaticWorkbook=false;
        MainWindow window(options); window.show(); openFresh(window,temp.filePath("race.dhwb"));
        window.importWorkbookFile(QStringLiteral(DATAHORIZON_FIXTURES "/fidelity.xlsx"),"xlsx","mixed","replace_empty");
        QPointer<QProgressDialog> progress=window.findChild<QProgressDialog *>("largeImportProgress"); QVERIFY(progress);
        const auto upload=progress->property("uploadId").toString(), job=progress->property("jobId").toString();
        // Cancel as soon as the job-creating request has been sent.
        bool cancelled=false; QTimer watch; watch.setInterval(0);
        connect(&watch,&QTimer::timeout,&window,[&]{
            if(progress && progress->labelText().startsWith("Reading cells")) { cancelled=true; watch.stop(); progress->findChild<QPushButton *>()->click(); }
        });
        watch.start();
        auto *issues=window.findChild<QPlainTextEdit *>("operationIssues");
        QTRY_VERIFY_WITH_TIMEOUT(issues->toPlainText().contains("Import cancelled"),20000); watch.stop(); QVERIFY(cancelled);
        QTest::qWait(1000);
        QTRY_COMPARE_WITH_TIMEOUT(serviceCall(window,"/v1/jobs/"+job,false).first["code"].toString(),QString("JOB_NOT_FOUND"),5000);
        QTRY_COMPARE_WITH_TIMEOUT(serviceCall(window,"/v1/uploads/"+upload+"/release").first["code"].toString(),QString("JOB_NOT_FOUND"),5000);
        QVERIFY(!visibleDialog(window,"largeImportReview")); QCOMPARE(tabTexts(window),QStringList({"Sheet1"}));
    }
    void closingWindowDuringReviewReleasesJobAndUpload() {
        QTemporaryDir temp; StartupOptions options; options.automaticWorkbook=false;
        MainWindow window(options); window.show(); openFresh(window,temp.filePath("close.dhwb"));
        window.importWorkbookFile(QStringLiteral(DATAHORIZON_FIXTURES "/fidelity.xlsx"),"xlsx","mixed","replace_empty");
        auto *progress=window.findChild<QProgressDialog *>("largeImportProgress"); QVERIFY(progress);
        const auto upload=progress->property("uploadId").toString(), job=progress->property("jobId").toString();
        QTRY_VERIFY_WITH_TIMEOUT(visibleDialog(window,"largeImportReview"),20000);
        QCOMPARE(serviceCall(window,"/v1/jobs/"+job,false).first["status"].toString(),QString("completed"));
        window.close();
        QTRY_VERIFY_WITH_TIMEOUT(!window.isVisible(),10000);
        QCOMPARE(serviceCall(window,"/v1/jobs/"+job,false).first["code"].toString(),QString("JOB_NOT_FOUND"));
        QCOMPARE(serviceCall(window,"/v1/uploads/"+upload+"/release").first["code"].toString(),QString("JOB_NOT_FOUND"));
    }
    void declinedApplyKeepsReviewAndBusyState() {
        QTemporaryDir temp; StartupOptions options; options.automaticWorkbook=false;
        MainWindow window(options); window.show(); openFresh(window,temp.filePath("declined.dhwb"));
        window.importWorkbookFile(QStringLiteral(DATAHORIZON_FIXTURES "/fidelity.xlsx"),"xlsx","mixed","replace_empty");
        QDialog *review=nullptr; QTRY_VERIFY_WITH_TIMEOUT((review=visibleDialog(window,"largeImportReview")),20000);
        auto *tabs=window.findChild<QTabBar *>("sheets"); QVERIFY(!tabs->isEnabled());
        // A formula-bar draft the user keeps makes submit decline before sending.
        auto *source=window.findChild<QLineEdit *>("source"); source->setText("kept draft"); source->setModified(true);
        QSignalSpy changed(&window,&MainWindow::operationFinished);
        answerNextDialog(QMessageBox::Cancel);
        auto *apply=review->findChild<QPushButton *>("importApply"); apply->click();
        auto *message=review->findChild<QLabel *>("importReviewMessage");
        QTRY_VERIFY_WITH_TIMEOUT(message->isVisible(),5000);
        QVERIFY(review->isVisible()); QVERIFY(apply->isEnabled()); QVERIFY(!tabs->isEnabled()); QCOMPARE(changed.count(),0);
        QVERIFY(!window.findChild<QProgressDialog *>("importApplyProgress"));
        source->setModified(false);
        review->findChild<QPushButton *>("importCancel")->click();
        QTRY_VERIFY_WITH_TIMEOUT(tabs->isEnabled(),5000); QCOMPARE(tabTexts(window),QStringList({"Sheet1"}));
    }
    void exportCancelDuringDownloadWritesNoFile() {
        QTemporaryDir temp; StartupOptions options; options.automaticWorkbook=false;
        MainWindow window(options); window.show(); openFresh(window,temp.filePath("download.dhwb"));
        QSignalSpy opened(&window,&MainWindow::workbookReady);
        QJsonArray cells; for(int i=0;i<10000;++i) cells.append(QJsonObject{{"row",i/20+1},{"col",i%20+1},{"source",QString::fromLatin1(QCryptographicHash::hash(QByteArray::number(i),QCryptographicHash::Sha256).toHex())}});
        window.submit({{"action","edit"},{"cells",cells}}); QTRY_COMPARE_WITH_TIMEOUT(opened.count(),1,20000);
        QVERIFY(QDir().mkpath(temp.filePath("out"))); const auto output=temp.filePath("out/download.xlsx");
        window.exportWorkbookFile(output,"xlsx");
        QPointer<QProgressDialog> progress=window.findChild<QProgressDialog *>("largeExportProgress"); QVERIFY(progress);
        const auto job=progress->property("jobId").toString();
        bool cancelledDuringDownload=false; QTimer watch; watch.setInterval(0);
        connect(&watch,&QTimer::timeout,&window,[&]{
            if(progress && progress->labelText().startsWith("Downloading")) { cancelledDuringDownload=true; watch.stop(); progress->findChild<QPushButton *>()->click(); }
        });
        watch.start();
        auto *issues=window.findChild<QPlainTextEdit *>("operationIssues");
        QTRY_VERIFY_WITH_TIMEOUT(issues->toPlainText().contains("Export cancelled"),30000); watch.stop();
        QVERIFY(cancelledDuringDownload);
        QTRY_COMPARE_WITH_TIMEOUT(serviceCall(window,"/v1/exports/"+job,false).first["code"].toString(),QString("JOB_NOT_FOUND"),10000);
        QCOMPARE(QDir(temp.filePath("out")).entryList(QDir::Files|QDir::Hidden|QDir::NoDotAndDotDot),QStringList());
    }
    void exportHashMismatchWritesNoFile() {
        QTemporaryDir temp; QJsonObject service; StartupOptions ownerOptions; ownerOptions.automaticWorkbook=false;
        MainWindow owner(ownerOptions); observeOwnedServiceStartup(owner,service); QSignalSpy serviceReady(&owner,&MainWindow::serviceReady);
        QTRY_COMPARE_WITH_TIMEOUT(serviceReady.count(),1,10000); QVERIFY(!service["token"].toString().isEmpty());
        const auto path=temp.filePath("hash.dhwb");
        QSignalSpy ownerOpened(&owner,&MainWindow::workbookReady); owner.openBook(path,true); QTRY_COMPARE_WITH_TIMEOUT(ownerOpened.count(),1,10000);
        owner.submit({{"action","edit"},{"cells",QJsonArray{QJsonObject{{"row",1},{"col",1},{"source","7"}}}}}); QTRY_COMPARE_WITH_TIMEOUT(ownerOpened.count(),2,10000);
        ExportDigestProxy proxy(service); QVERIFY(proxy.listen(QHostAddress::LocalHost,0));
        StartupOptions options; options.automaticWorkbook=false; options.url="http://127.0.0.1:"+QString::number(proxy.serverPort());
        options.token=service["token"].toString(); options.recoveryDirectory=temp.filePath("recovery");
        MainWindow window(options); window.show(); QSignalSpy ready(&window,&MainWindow::serviceReady); QTRY_COMPARE_WITH_TIMEOUT(ready.count(),1,10000);
        QSignalSpy opened(&window,&MainWindow::workbookReady); window.openBook(path); QTRY_COMPARE_WITH_TIMEOUT(opened.count(),1,10000);
        QVERIFY(QDir().mkpath(temp.filePath("out"))); const auto output=temp.filePath("out/hash.xlsx");
        window.exportWorkbookFile(output,"xlsx");
        const auto job=window.findChild<QProgressDialog *>("largeExportProgress")->property("jobId").toString();
        auto *issues=window.findChild<QPlainTextEdit *>("operationIssues");
        QTRY_VERIFY_WITH_TIMEOUT(issues->toPlainText().contains("does not match"),20000);
        QVERIFY(proxy.patched); QVERIFY(proxy.served);
        QCOMPARE(QDir(temp.filePath("out")).entryList(QDir::Files|QDir::Hidden|QDir::NoDotAndDotDot),QStringList());
        // The real export is released on the service behind the proxy.
        RestClient direct; direct.configure(service["url"].toString(),service["token"].toString());
        auto lookup=[&]{QString code;bool done=false;direct.request("/v1/exports/"+job,{},[&](QJsonObject result,QString){code=result["code"].toString();done=true;},false);for(int i=0;i<250 && !done;++i)QTest::qWait(20);return code;};
        QTRY_COMPARE_WITH_TIMEOUT(lookup(),QString("JOB_NOT_FOUND"),5000);
    }
    void unifiedExportUsesTheFormatDropdown_data() {
        QTest::addColumn<int>("formatIndex"); QTest::addColumn<QString>("extension"); QTest::addColumn<bool>("whole");
        QTest::newRow("csv")<<0<<QString("csv")<<false; QTest::newRow("xlsx")<<1<<QString("xlsx")<<false; QTest::newRow("ods")<<2<<QString("ods")<<false;
        QTest::newRow("xlsx-workbook")<<1<<QString("xlsx")<<true; QTest::newRow("ods-workbook")<<2<<QString("ods")<<true;
    }
    void unifiedExportUsesTheFormatDropdown() {
        QFETCH(int,formatIndex); QFETCH(QString,extension); QFETCH(bool,whole);
        QTemporaryDir temp; StartupOptions options; options.automaticWorkbook=false;
        MainWindow window(options); window.show(); QSignalSpy ready(&window,&MainWindow::serviceReady);
        QTRY_COMPARE_WITH_TIMEOUT(ready.count(),1,10000);
        QSignalSpy opened(&window,&MainWindow::workbookReady); window.openBook(temp.filePath("native.dhwb"),true);
        QTRY_COMPARE_WITH_TIMEOUT(opened.count(),1,10000);
        window.submit({{"action","edit"},{"cells",QJsonArray{QJsonObject{{"row",1},{"col",1},{"source","7"}},QJsonObject{{"row",1},{"col",2},{"source","=A1*2"}}}}});
        QTRY_COMPARE_WITH_TIMEOUT(opened.count(),2,10000);
        auto *grid=window.findChild<QTableView *>("grid"); QVERIFY(grid);
        grid->selectionModel()->select(QItemSelection(window.model()->index(0,whole?1:0),window.model()->index(0,1)),QItemSelectionModel::ClearAndSelect);
        auto *action=window.findChild<QAction *>("exportAction"); QVERIFY(action);
        bool pickerSeen=false,timedOut=false; QElapsedTimer elapsed; elapsed.start(); QTimer answer; answer.setInterval(10);
        connect(&answer,&QTimer::timeout,&window,[&]{
            auto *modal=QApplication::activeModalWidget();
            if(elapsed.elapsed()>10000) { timedOut=true; if(auto *dialog=qobject_cast<QDialog *>(modal)) dialog->reject(); return; }
            if(auto *picker=qobject_cast<QFileDialog *>(modal)) {
                if(!pickerSeen) {
                    pickerSeen=true; QCOMPARE(picker->nameFilters().size(),3);
                    picker->setDirectory(temp.path());
                    auto *name=picker->findChild<QLineEdit *>("fileNameEdit"); QVERIFY(name); name->setText(temp.filePath("result.csv"));
                    auto *formats=picker->findChild<QComboBox *>("fileTypeCombo"); QVERIFY(formats);
                    formats->setFocus(); QTest::keyClick(formats,Qt::Key_Home);
                    for(int i=0;i<formatIndex;++i) QTest::keyClick(formats,Qt::Key_Down);
                    QCOMPARE(picker->selectedNameFilter(),picker->nameFilters()[formatIndex]);
                    if(qEnvironmentVariableIsSet("DATAHORIZON_EXPORT_DIALOG_SCREENSHOT")) picker->grab().save(qEnvironmentVariable("DATAHORIZON_EXPORT_DIALOG_SCREENSHOT"));
                }
                if(auto *buttons=picker->findChild<QDialogButtonBox *>())
                    if(auto *button=buttons->button(QDialogButtonBox::Save);button && button->isEnabled()) QTimer::singleShot(0,button,[button]{button->click();});
            } else if(auto *input=qobject_cast<QInputDialog *>(modal)) {
                if(whole) input->setTextValue(input->comboBoxItems().value(2));
                input->accept();
            }
            else if(auto *box=qobject_cast<QMessageBox *>(modal)) {
                if(auto *button=box->button(QMessageBox::Yes)) button->click();
            }
        });
        answer.start(); action->trigger();
        const auto path=temp.filePath("result."+extension);
        QTRY_VERIFY_WITH_TIMEOUT(QFile::exists(path),10000); answer.stop(); QVERIFY(pickerSeen); QVERIFY(!timedOut);
        QFile output(path); QVERIFY(output.open(QIODevice::ReadOnly)); const auto data=output.readAll();
        if(extension=="csv") QCOMPARE(data,QByteArray("7,14\r\n"));
        else if(extension=="xlsx") { QVERIFY(data.startsWith("PK")); QVERIFY(data.contains("xl/workbook.xml")); }
        else { QVERIFY(data.startsWith("PK")); QVERIFY(data.contains("application/vnd.oasis.opendocument.spreadsheet")); }
        QCOMPARE(opened.count(),2); QVERIFY(extension=="csv" || !QFile::exists(temp.filePath("result.csv")));
    }
    void unifiedExportConfirmsTheNormalizedFilenameBeforeOverwriting() {
        QTemporaryDir temp; StartupOptions options; options.automaticWorkbook=false;
        MainWindow window(options); window.show(); QSignalSpy ready(&window,&MainWindow::serviceReady);
        QTRY_COMPARE_WITH_TIMEOUT(ready.count(),1,10000);
        QSignalSpy opened(&window,&MainWindow::workbookReady); window.openBook(temp.filePath("native.dhwb"),true);
        QTRY_COMPARE_WITH_TIMEOUT(opened.count(),1,10000);
        const auto path=temp.filePath("retained.xlsx"); QFile original(path); QVERIFY(original.open(QIODevice::WriteOnly)); original.write("retained"); original.close();
        bool pickerSeen=false,declined=false,confirmedExactPath=false,timedOut=false;
        QElapsedTimer elapsed; elapsed.start(); QTimer answer; answer.setInterval(10);
        connect(&answer,&QTimer::timeout,&window,[&]{
            auto *modal=QApplication::activeModalWidget();
            if(elapsed.elapsed()>10000) {
                timedOut=true; if(auto *dialog=qobject_cast<QDialog *>(modal)) dialog->reject(); return;
            }
            if(auto *picker=qobject_cast<QFileDialog *>(modal)) {
                if(declined || elapsed.elapsed()>10000) { timedOut=elapsed.elapsed()>10000; picker->reject(); return; }
                if(!pickerSeen) {
                    pickerSeen=true; picker->setDirectory(temp.path()); picker->selectNameFilter(picker->nameFilters()[1]);
                    // A conflicting suffix typed after choosing the format must
                    // still be normalized before the overwrite question.
                    auto *name=picker->findChild<QLineEdit *>("fileNameEdit"); QVERIFY(name); name->setText("retained.csv");
                }
                if(auto *buttons=picker->findChild<QDialogButtonBox *>())
                    if(auto *button=buttons->button(QDialogButtonBox::Save);button && button->isEnabled()) QTimer::singleShot(0,button,[button]{button->click();});
            } else if(auto *box=qobject_cast<QMessageBox *>(modal)) {
                confirmedExactPath=box->text().contains("retained.xlsx"); declined=true;
                if(auto *button=box->button(QMessageBox::No)) button->click();
                else if(auto *button=box->button(QMessageBox::Cancel)) button->click();
            }
        });
        auto *action=window.findChild<QAction *>("exportAction"); QVERIFY(action);
        answer.start(); action->trigger(); answer.stop();
        QVERIFY(!timedOut); QVERIFY(pickerSeen); QVERIFY(declined); QVERIFY(confirmedExactPath);
        QVERIFY(original.open(QIODevice::ReadOnly)); QCOMPARE(original.readAll(),QByteArray("retained"));
        QVERIFY(!QFile::exists(temp.filePath("retained.csv")));
    }
    void csvEncodingAndDelimiterOptionsDecodeBeforeReviewAndRejectInvalidBytes() {
        const bool originalDialogs=QApplication::testAttribute(Qt::AA_DontUseNativeDialogs);
        QApplication::setAttribute(Qt::AA_DontUseNativeDialogs,true);
        const auto restore=qScopeGuard([originalDialogs]{QApplication::setAttribute(Qt::AA_DontUseNativeDialogs,originalDialogs);});
        QTemporaryDir temp; const auto path=temp.filePath("data.csv"); QFile file(path);
        QVERIFY(file.open(QIODevice::WriteOnly)); file.write(QByteArray::fromHex("30303132333b636166e93b3d312b31")); file.close();
        StartupOptions options; options.automaticWorkbook=false;
        MainWindow window(options); window.show(); QSignalSpy ready(&window,&MainWindow::serviceReady);
        QTRY_COMPARE_WITH_TIMEOUT(ready.count(),1,10000);
        QSignalSpy opened(&window,&MainWindow::workbookReady); window.openBook(temp.filePath("csv.dhwb"),true);
        QTRY_COMPARE_WITH_TIMEOUT(opened.count(),1,10000);
        bool selectedOptions=false,reviewed=false,pickerInitialized=false,timedOut=false;
        QElapsedTimer elapsed; elapsed.start();
        QTimer answer; answer.setInterval(10);
        connect(&answer,&QTimer::timeout,&window,[&]{
            auto *modal=QApplication::activeModalWidget();
            if(elapsed.elapsed()>10000) {
                timedOut=true;
                if(auto *dialog=qobject_cast<QDialog *>(modal)) dialog->reject();
                return;
            }
            if(auto *picker=qobject_cast<QFileDialog *>(modal)) {
                if(!pickerInitialized) {
                    pickerInitialized=true; picker->setDirectory(temp.path());
                }
                // QFileSystemModel loads asynchronously. Reapply the absolute
                // filename until the chooser accepts it, rather than clicking
                // Open on a directory while the first selection is still loading.
                picker->selectFile(path);
                if(picker->selectedFiles()!=QStringList{path}) return;
                if(auto *buttons=picker->findChild<QDialogButtonBox *>())
                    if(auto *open=buttons->button(QDialogButtonBox::Open);open && open->isEnabled()) open->click();
            } else if(auto *dialog=qobject_cast<QDialog *>(modal);dialog && dialog->objectName()=="csvOptionsDialog") {
                auto *encoding=dialog->findChild<QComboBox *>("csvEncoding");
                auto *delimiter=dialog->findChild<QComboBox *>("csvDelimiter");
                encoding->setCurrentIndex(encoding->findData("cp1252")); delimiter->setCurrentIndex(delimiter->findData(";"));
                selectedOptions=true; dialog->accept();
            } else if(auto *box=qobject_cast<QMessageBox *>(modal)) {
                const auto review=box->text()+box->informativeText()+box->detailedText();
                reviewed=box->textFormat()==Qt::PlainText && review.contains("cp1252") && review.contains(QString::fromUtf8("café"));
                box->button(QMessageBox::Yes)->click();
            }
        });
        QAction *import=nullptr; for(auto *action:window.findChildren<QAction *>()) if(action->objectName()=="importAction") import=action;
        QVERIFY(import); QSignalSpy changed(&window,&MainWindow::operationFinished);
        answer.start(); import->trigger(); QTRY_COMPARE_WITH_TIMEOUT(changed.count(),1,10000); answer.stop();
        QVERIFY(!timedOut); QVERIFY(selectedOptions); QVERIFY(reviewed); QVERIFY(changed[0][0].toBool());
        QTRY_COMPARE_WITH_TIMEOUT(window.model()->data(window.model()->index(0,1)).toString(),QString::fromUtf8("café"),5000);
        QCOMPARE(window.model()->data(window.model()->index(0,0)).toString(),QString("00123"));
        QCOMPARE(window.model()->data(window.model()->index(0,2)).toString(),QString("=1+1"));
        QTRY_COMPARE_WITH_TIMEOUT(opened.count(),2,10000);
        window.importCsvBytes(QByteArray::fromHex("ff"));
        QTRY_VERIFY_WITH_TIMEOUT(window.findChild<QPlainTextEdit *>("operationIssues")->toPlainText().contains("invalid for utf-8"),5000);
        QCOMPARE(changed.count(),1);
    }
    void csvColumnTypesReviewConvertsHeadersNumbersBooleansAndDatesAtomically() {
        QTemporaryDir temp; StartupOptions options; options.automaticWorkbook=false;
        MainWindow window(options); window.show(); QSignalSpy ready(&window,&MainWindow::serviceReady);
        QTRY_COMPARE_WITH_TIMEOUT(ready.count(),1,10000);
        QSignalSpy opened(&window,&MainWindow::workbookReady); window.openBook(temp.filePath("types.dhwb"),true);
        QTRY_COMPARE_WITH_TIMEOUT(opened.count(),1,10000);
        int phase=0; bool reviewed=false; QTimer answer; answer.setInterval(10);
        connect(&answer,&QTimer::timeout,&window,[&]{
            auto *modal=QApplication::activeModalWidget();
            if(auto *box=qobject_cast<QMessageBox *>(modal)) {
                if(phase==0) { phase=1; box->findChild<QPushButton *>("csvTypesButton")->click(); }
                else if(phase==2) {
                    reviewed=box->informativeText().contains("1 ambiguous") && box->informativeText().contains("2026-03-04");
                    const auto screenshot=qEnvironmentVariable("DATAHORIZON_CSV_SCREENSHOT");
                    if(!screenshot.isEmpty()) box->grab().save(screenshot);
                    phase=3; box->button(QMessageBox::Yes)->click();
                }
            } else if(auto *dialog=qobject_cast<QDialog *>(modal);dialog && dialog->objectName()=="csvTypesDialog" && phase==1) {
                dialog->findChild<QCheckBox *>("csvHeader")->setChecked(true);
                auto *column=dialog->findChild<QSpinBox *>("csvTypeColumn"); auto *type=dialog->findChild<QComboBox *>("csvColumnType");
                column->setValue(2); type->setCurrentIndex(type->findData("number"));
                column->setValue(3); type->setCurrentIndex(type->findData("boolean"));
                column->setValue(4); type->setCurrentIndex(type->findData("date_mdy"));
                phase=2; dialog->accept();
            }
        });
        QSignalSpy changed(&window,&MainWindow::operationFinished);
        answer.start(); window.importCsvText("ID,Qty,Active,When,Formula\n00123,2,TRUE,03/04/2026,=1+1");
        QTRY_COMPARE_WITH_TIMEOUT(changed.count(),1,10000); answer.stop(); QVERIFY(reviewed); QVERIFY(changed[0][0].toBool());
        QTRY_COMPARE_WITH_TIMEOUT(opened.count(),2,10000);
        auto *model=window.model(); QTRY_COMPARE_WITH_TIMEOUT(model->data(model->index(1,3)).toString(),QString("2026-03-04"),5000);
        QCOMPARE(model->cell(model->index(0,1))["input_kind"].toString(),QString("text"));
        QCOMPARE(model->cell(model->index(1,1))["input_kind"].toString(),QString("number"));
        QCOMPARE(model->cell(model->index(1,2))["value"].toBool(),true);
        QCOMPARE(model->data(model->index(1,0)).toString(),QString("00123"));
        QCOMPARE(model->data(model->index(1,4)).toString(),QString("=1+1"));
        window.submit({{"action","undo"}}); QTRY_COMPARE_WITH_TIMEOUT(opened.count(),3,10000);
        QTRY_COMPARE_WITH_TIMEOUT(model->data(model->index(1,3)).toString(),QString(),5000);
        window.submit({{"action","redo"}}); QTRY_COMPARE_WITH_TIMEOUT(opened.count(),4,10000);
        QTRY_COMPARE_WITH_TIMEOUT(model->data(model->index(1,3)).toString(),QString("2026-03-04"),5000);
    }
    void csvInvalidConversionDisablesApplyAndCanBeCorrectedToText() {
        QTemporaryDir temp; StartupOptions options; options.automaticWorkbook=false;
        MainWindow window(options); window.show(); QSignalSpy ready(&window,&MainWindow::serviceReady);
        QTRY_COMPARE_WITH_TIMEOUT(ready.count(),1,10000);
        QSignalSpy opened(&window,&MainWindow::workbookReady); window.openBook(temp.filePath("invalid-types.dhwb"),true);
        QTRY_COMPARE_WITH_TIMEOUT(opened.count(),1,10000);
        QSignalSpy changed(&window,&MainWindow::operationFinished); int phase=0; bool blocked=false;
        QTimer answer; answer.setInterval(10);
        connect(&answer,&QTimer::timeout,&window,[&]{
            auto *modal=QApplication::activeModalWidget();
            if(auto *box=qobject_cast<QMessageBox *>(modal)) {
                if(phase==0) {phase=1; box->findChild<QPushButton *>("csvTypesButton")->click();}
                else if(phase==2) {
                    blocked=!box->button(QMessageBox::Yes)->isEnabled() && box->detailedText().contains("INVALID_INPUT") && changed.count()==0;
                    phase=3; box->findChild<QPushButton *>("csvTypesButton")->click();
                } else if(phase==4) {phase=5; box->button(QMessageBox::Yes)->click();}
            } else if(auto *dialog=qobject_cast<QDialog *>(modal);dialog && dialog->objectName()=="csvTypesDialog") {
                auto *type=dialog->findChild<QComboBox *>("csvColumnType");
                type->setCurrentIndex(type->findData(phase==1?"number":"text")); ++phase; dialog->accept();
            }
        });
        answer.start(); window.importCsvText("=1+1"); QTRY_COMPARE_WITH_TIMEOUT(changed.count(),1,10000); answer.stop();
        QVERIFY(blocked); QVERIFY(changed[0][0].toBool()); QTRY_COMPARE_WITH_TIMEOUT(opened.count(),2,10000);
        QTRY_COMPARE_WITH_TIMEOUT(window.model()->data(window.model()->index(0,0)).toString(),QString("=1+1"),5000);
        QCOMPARE(window.model()->cell(window.model()->index(0,0))["input_kind"].toString(),QString("text"));
    }
    void csvTypeDialogCannotApplyAfterProjectionChanges() {
        QTemporaryDir temp; StartupOptions options; options.automaticWorkbook=false;
        MainWindow window(options); window.show(); QSignalSpy ready(&window,&MainWindow::serviceReady);
        QTRY_COMPARE_WITH_TIMEOUT(ready.count(),1,10000);
        QSignalSpy opened(&window,&MainWindow::workbookReady); window.openBook(temp.filePath("stale-types.dhwb"),true);
        QTRY_COMPARE_WITH_TIMEOUT(opened.count(),1,10000);
        QSignalSpy changed(&window,&MainWindow::operationFinished); bool invalidated=false;
        QTimer answer; answer.setInterval(10);
        connect(&answer,&QTimer::timeout,&window,[&]{
            auto *modal=QApplication::activeModalWidget();
            if(auto *box=qobject_cast<QMessageBox *>(modal)) box->findChild<QPushButton *>("csvTypesButton")->click();
            else if(auto *dialog=qobject_cast<QDialog *>(modal);dialog && dialog->objectName()=="csvTypesDialog") {
                window.model()->setProjection({0,1}); invalidated=true; dialog->accept();
            }
        });
        answer.start(); window.importCsvText("7");
        QTRY_VERIFY_WITH_TIMEOUT(window.findChild<QPlainTextEdit *>("operationIssues")->toPlainText().contains("CSV import target changed"),5000);
        answer.stop(); QVERIFY(invalidated); QCOMPARE(changed.count(),0);
    }
    void csvPreviewCapturesItsTargetAndRejectsInvalidatedProjection() {
        QTemporaryDir temp; StartupOptions options; options.automaticWorkbook=false;
        MainWindow window(options); window.show();
        QSignalSpy ready(&window,&MainWindow::serviceReady); QTRY_COMPARE_WITH_TIMEOUT(ready.count(),1,10000);
        QSignalSpy opened(&window,&MainWindow::workbookReady); const auto path=temp.filePath("import.dhwb"); window.openBook(path,true);
        QTRY_COMPARE_WITH_TIMEOUT(opened.count(),1,10000);
        auto *model=window.model(); auto *grid=window.findChild<QTableView *>("grid");
        QTRY_VERIFY_WITH_TIMEOUT(model->loaded(model->index(0,0)),5000);
        QSignalSpy changed(&window,&MainWindow::operationFinished);
        answerNextDialog(QMessageBox::Yes); window.importCsvText("ID\n00123");
        grid->setCurrentIndex(model->index(4,1)); window.openBook(temp.filePath("wrong.dhwb"),true);
        QTRY_COMPARE_WITH_TIMEOUT(changed.count(),1,10000); QTRY_COMPARE_WITH_TIMEOUT(opened.count(),2,10000);
        QTRY_COMPARE_WITH_TIMEOUT(model->data(model->index(1,0)).toString(),QString("00123"),5000);
        QCOMPARE(model->data(model->index(0,0)).toString(),QString("ID")); QCOMPARE(model->data(model->index(4,1)).toString(),QString());
        QVERIFY(!QFile::exists(temp.filePath("wrong.dhwb")));
        window.importCsvText("wrong target"); model->setProjection({0,1});
        QTRY_VERIFY_WITH_TIMEOUT(window.findChild<QPlainTextEdit *>("operationIssues")->toPlainText().contains("CSV import target changed"),5000);
        QCOMPARE(changed.count(),1);
        window.openBook(path); QTRY_COMPARE_WITH_TIMEOUT(opened.count(),3,10000);
        QTRY_COMPARE_WITH_TIMEOUT(model->data(model->index(0,0)).toString(),QString("ID"),5000);
    }
    void csvImportResolvesActiveCellDraftBeforePreview() {
        QTemporaryDir temp; StartupOptions options; options.automaticWorkbook=false;
        MainWindow window(options); window.show(); QSignalSpy ready(&window,&MainWindow::serviceReady);
        QTRY_COMPARE_WITH_TIMEOUT(ready.count(),1,10000);
        QSignalSpy opened(&window,&MainWindow::workbookReady); window.openBook(temp.filePath("import-draft.dhwb"),true);
        QTRY_COMPARE_WITH_TIMEOUT(opened.count(),1,10000);
        auto *grid=window.findChild<QTableView *>("grid"); auto *source=window.findChild<QLineEdit *>("source");
        QTRY_VERIFY_WITH_TIMEOUT(window.model()->sourceReady(window.model()->index(0,0)),5000);
        grid->edit(window.model()->index(0,0)); QPointer<QLineEdit> editor=grid->findChild<QLineEdit *>(); QVERIFY(editor);
        QTest::keyClicks(editor,"retained input");
        QSignalSpy changed(&window,&MainWindow::operationFinished);
        answerNextDialog(QMessageBox::Cancel); window.importCsvText("replacement");
        QCOMPARE(changed.count(),0); QCOMPARE(opened.count(),1);
        QCOMPARE(source->text(),QString("retained input")); QVERIFY(source->isModified());
        QTRY_VERIFY_WITH_TIMEOUT(editor.isNull(),3000);
    }
    void csvFileSelectionCannotRetargetAfterExternalSheetDeletion() {
        const bool originalDialogs=QApplication::testAttribute(Qt::AA_DontUseNativeDialogs);
        QApplication::setAttribute(Qt::AA_DontUseNativeDialogs,true);
        const auto restoreDialogs=qScopeGuard([originalDialogs]{QApplication::setAttribute(Qt::AA_DontUseNativeDialogs,originalDialogs);});
        QTemporaryDir temp; StartupOptions options; options.automaticWorkbook=false;
        const auto csvPath=temp.filePath("input.csv"); QFile csv(csvPath); QVERIFY(csv.open(QIODevice::WriteOnly));
        csv.write("wrong sheet"); csv.close();
        MainWindow window(options); window.show(); QSignalSpy ready(&window,&MainWindow::serviceReady);
        QTRY_COMPARE_WITH_TIMEOUT(ready.count(),1,10000);
        const auto path=temp.filePath("import-file-target.dhwb");
        QSignalSpy opened(&window,&MainWindow::workbookReady); window.openBook(path,true);
        QTRY_COMPARE_WITH_TIMEOUT(opened.count(),1,10000);
        window.submit({{"action","sheet_add"},{"name","Other"}}); QTRY_COMPARE_WITH_TIMEOUT(opened.count(),2,10000);
        const auto original=window.model()->sheet();
        MainWindow writer(options); QSignalSpy writerReady(&writer,&MainWindow::serviceReady);
        QTRY_COMPARE_WITH_TIMEOUT(writerReady.count(),1,10000);
        QSignalSpy writerOpened(&writer,&MainWindow::workbookReady); writer.openBook(path);
        QTRY_COMPARE_WITH_TIMEOUT(writerOpened.count(),1,10000);
        bool pickerOpened=false,refreshed=false,fileChosen=false;
        QElapsedTimer elapsed; elapsed.start();
        QSignalSpy changed(&window,&MainWindow::operationFinished);
        connect(&writer,&MainWindow::workbookReady,&window,[&]{window.model()->externalChange();});
        connect(&window,&MainWindow::workbookReady,&window,[&]{
            if(window.model()->sheet()==original) return;
            refreshed=true;
            if(auto *dialog=qobject_cast<QFileDialog *>(QApplication::activeModalWidget())) {
                dialog->setDirectory(temp.path()); dialog->selectFile("input.csv");
            }
        });
        QTimer dialogs; dialogs.setInterval(10);
        connect(&dialogs,&QTimer::timeout,&window,[&]{
            if(auto *dialog=qobject_cast<QFileDialog *>(QApplication::activeModalWidget());dialog && !pickerOpened) {
                pickerOpened=true; writer.submit({{"action","sheet_delete"}});
            }
            if(auto *dialog=qobject_cast<QFileDialog *>(QApplication::activeModalWidget());dialog && refreshed && !fileChosen) {
                if(auto *name=dialog->findChild<QLineEdit *>("fileNameEdit")) name->setText(csvPath);
                auto *buttons=dialog->findChild<QDialogButtonBox *>();
                if(auto *open=buttons?buttons->button(QDialogButtonBox::Open):nullptr;open && open->isEnabled()) {
                    fileChosen=true; open->click();
                }
            }
            if(auto *box=qobject_cast<QMessageBox *>(QApplication::activeModalWidget())) {
                if(auto *yes=box->button(QMessageBox::Yes)) yes->click();
                else if(auto *ok=box->button(QMessageBox::Ok)) ok->click();
            }
            if(elapsed.elapsed()>15000)
                if(auto *dialog=qobject_cast<QDialog *>(QApplication::activeModalWidget())) dialog->reject();
        });
        dialogs.start();
        QAction *import=nullptr;
        for(auto *action:window.findChildren<QAction *>()) if(action->objectName()=="importAction") import=action;
        QVERIFY(import); import->trigger();
        QVERIFY(pickerOpened); QVERIFY(refreshed); QVERIFY(fileChosen);
        auto *issues=window.findChild<QPlainTextEdit *>("operationIssues");
        QTRY_VERIFY_WITH_TIMEOUT(issues->toPlainText().contains("Import target changed") || changed.count()>0,10000);
        QCOMPARE(changed.count(),0);
        QVERIFY(issues->toPlainText().contains("Import target changed"));
        QTRY_VERIFY_WITH_TIMEOUT(window.model()->loaded(window.model()->index(0,0)),5000);
        QCOMPARE(window.model()->data(window.model()->index(0,0)).toString(),QString());
    }
    void cutSpecialPasteDoesNotMoveInputs() {
        QTemporaryDir temp; StartupOptions options; options.automaticWorkbook=false;
        MainWindow window(options); window.show();
        QSignalSpy ready(&window,&MainWindow::serviceReady); QTRY_COMPARE_WITH_TIMEOUT(ready.count(),1,10000);
        QSignalSpy opened(&window,&MainWindow::workbookReady); window.openBook(temp.filePath("cut.dhwb"),true);
        QTRY_COMPARE_WITH_TIMEOUT(opened.count(),1,10000);
        window.submit({{"action","edit"},{"cells",QJsonArray{QJsonObject{{"row",1},{"col",1},{"source","source"}},QJsonObject{{"row",1},{"col",2},{"source","destination"}}}}});
        QTRY_COMPARE_WITH_TIMEOUT(opened.count(),2,10000);
        auto *model=window.model(); auto *grid=window.findChild<QTableView *>("grid");
        QTRY_COMPARE_WITH_TIMEOUT(model->data(model->index(0,1)).toString(),QString("destination"),5000);
        auto *mime=new QMimeData;
        mime->setData("application/x-datahorizon-cells",QJsonDocument(QJsonObject{{"cut",true},{"source_workbook",model->workbook()},
            {"source_sheet",model->sheet()},{"r1",1},{"c1",1},{"r2",1},{"c2",1},{"cut_state",model->state()}}).toJson());
        QApplication::clipboard()->setMimeData(mime); grid->setCurrentIndex(model->index(0,1)); grid->setFocus();
        for(auto *action:window.findChildren<QAction *>()) if(action->text()=="Paste formats only") action->trigger();
        QVERIFY(window.findChild<QPlainTextEdit *>("operationIssues")->toPlainText().contains("Cut supports full paste only"));
        QCOMPARE(model->data(model->index(0,0)).toString(),QString("source")); QCOMPARE(model->data(model->index(0,1)).toString(),QString("destination"));
    }
    void newerCopySupersedesAnEarlierDelayedCut_data() {
        QTest::addColumn<bool>("cutFinishesFirst");
        QTest::newRow("older-reply-first") << true;
        QTest::newRow("newer-reply-first") << false;
    }
    void newerCopySupersedesAnEarlierDelayedCut() {
        QFETCH(bool,cutFinishesFirst);
        QTemporaryDir temp; StartupOptions options; options.automaticWorkbook=false;
        MainWindow window(options); window.show(); QSignalSpy ready(&window,&MainWindow::serviceReady);
        QTRY_COMPARE_WITH_TIMEOUT(ready.count(),1,10000);
        QSignalSpy opened(&window,&MainWindow::workbookReady); window.openBook(temp.filePath("clipboard-order.dhwb"),true);
        QTRY_COMPARE_WITH_TIMEOUT(opened.count(),1,10000);
        window.submit({{"action","edit"},{"cells",QJsonArray{
            QJsonObject{{"row",1},{"col",1},{"source","cut source"}},QJsonObject{{"row",1},{"col",2},{"source","copy source"}}}}});
        QTRY_COMPARE_WITH_TIMEOUT(opened.count(),2,10000);
        auto *model=window.model(); auto *grid=window.findChild<QTableView *>("grid");
        QTRY_VERIFY_WITH_TIMEOUT(model->sourceReady(model->index(0,1)),5000);
        DelayedClipboardServer server(model->state(),model->sheet()); QVERIFY(server.listen(QHostAddress::LocalHost,0));
        auto *client=window.findChild<RestClient *>();
        client->configure("http://127.0.0.1:"+QString::number(server.serverPort()),"token");
        int completed=0;
        connect(client->findChild<QNetworkAccessManager *>(),&QNetworkAccessManager::finished,&window,[&](QNetworkReply *reply) {
            if(reply->url().path().endsWith("/clipboard")) ++completed;
        });
        QAction *cut=nullptr,*copy=nullptr;
        for(auto *action:window.findChildren<QAction *>()) {
            if(action->shortcut().matches(QKeySequence::Cut)==QKeySequence::ExactMatch) cut=action;
            if(action->shortcut().matches(QKeySequence::Copy)==QKeySequence::ExactMatch) copy=action;
        }
        QApplication::clipboard()->setText("prior clipboard");
        QVERIFY(cut); QVERIFY(copy); grid->setFocus(); cut->trigger();
        QTRY_COMPARE_WITH_TIMEOUT(server.requests.size(),1,3000);
        grid->setCurrentIndex(model->index(0,1)); copy->trigger();
        QTRY_COMPARE_WITH_TIMEOUT(server.requests.size(),2,3000);
        QCOMPARE(server.requests[0].range["c1"].toInt(),1); QCOMPARE(server.requests[1].range["c1"].toInt(),2);
        const auto status=window.findChild<QLabel *>("storageStatus")->text();
        if(cutFinishesFirst) {
            server.respond(0,"cut source"); QTRY_COMPARE_WITH_TIMEOUT(completed,1,3000);
            QCOMPARE(QApplication::clipboard()->text(),QString("prior clipboard"));
            QCOMPARE(window.findChild<QLabel *>("storageStatus")->text(),status);
        }
        server.respond(1,"copy source"); QTRY_COMPARE_WITH_TIMEOUT(completed,cutFinishesFirst?2:1,3000);
        QCOMPARE(QApplication::clipboard()->text(),QString("copy source"));
        const auto copied=QApplication::clipboard()->mimeData()->data("application/x-datahorizon-cells");
        const auto payload=QJsonDocument::fromJson(copied).object();
        QVERIFY(!payload["cut"].toBool()); QCOMPARE(payload["source_workbook"].toString(),model->workbook());
        QCOMPARE(payload["source_sheet"].toString(),model->sheet()); QCOMPARE(payload["cut_state"].toString(),model->state());
        QCOMPARE(payload["origin_col"].toInt(),2);
        if(!cutFinishesFirst) { server.respond(0,"cut source"); QTRY_COMPARE_WITH_TIMEOUT(completed,2,3000); }
        QCOMPARE(QApplication::clipboard()->text(),QString("copy source"));
        QCOMPARE(QApplication::clipboard()->mimeData()->data("application/x-datahorizon-cells"),copied);
        QCOMPARE(window.findChild<QLabel *>("storageStatus")->text(),status);
    }
    void clipboardContentChangesSupersedePendingGridReplies_data() {
        QTest::addColumn<QString>("change"); QTest::addColumn<bool>("rejected");
        for(const auto &change:{QString("external"),QString("source-copy"),QString("source-cut")}) {
            QTest::newRow(qPrintable(change+"-success")) << change << false;
            QTest::newRow(qPrintable(change+"-error")) << change << true;
        }
    }
    void clipboardContentChangesSupersedePendingGridReplies() {
        QFETCH(QString,change); QFETCH(bool,rejected);
        QTemporaryDir temp; StartupOptions options; options.automaticWorkbook=false;
        MainWindow window(options); window.show(); QSignalSpy ready(&window,&MainWindow::serviceReady);
        QTRY_COMPARE_WITH_TIMEOUT(ready.count(),1,10000);
        QSignalSpy opened(&window,&MainWindow::workbookReady); window.openBook(temp.filePath("clipboard-change.dhwb"),true);
        QTRY_COMPARE_WITH_TIMEOUT(opened.count(),1,10000);
        auto *model=window.model(); auto *grid=window.findChild<QTableView *>("grid"); auto *source=window.findChild<QLineEdit *>("source");
        QTRY_VERIFY_WITH_TIMEOUT(model->sourceReady(model->index(0,0)),5000);
        DelayedClipboardServer server(model->state(),model->sheet()); QVERIFY(server.listen(QHostAddress::LocalHost,0));
        auto *client=window.findChild<RestClient *>();
        client->configure("http://127.0.0.1:"+QString::number(server.serverPort()),"token");
        int completed=0;
        connect(client->findChild<QNetworkAccessManager *>(),&QNetworkAccessManager::finished,&window,[&](QNetworkReply *reply) {
            if(reply->url().path().endsWith("/clipboard")) ++completed;
        });
        QAction *cut=nullptr;
        for(auto *action:window.findChildren<QAction *>()) if(action->shortcut().matches(QKeySequence::Cut)==QKeySequence::ExactMatch) cut=action;
        QApplication::clipboard()->setText("before newer content");
        QVERIFY(cut); grid->setFocus(); cut->trigger(); QTRY_COMPARE_WITH_TIMEOUT(server.requests.size(),1,3000);
        if(change=="external") QApplication::clipboard()->setText("new clipboard content");
        else {
            source->setFocus(); source->setText("new clipboard content"); source->selectAll();
            if(change=="source-copy") source->copy(); else source->cut();
        }
        QCOMPARE(QApplication::clipboard()->text(),QString("new clipboard content"));
        QVERIFY(!QApplication::clipboard()->mimeData()->hasFormat("application/x-datahorizon-cells"));
        const auto status=window.findChild<QLabel *>("storageStatus")->text();
        auto *issues=window.findChild<QPlainTextEdit *>("operationIssues"); const auto previousIssues=issues->toPlainText();
        server.respond(0,"superseded cut",rejected); QTRY_COMPARE_WITH_TIMEOUT(completed,1,3000);
        QCOMPARE(QApplication::clipboard()->text(),QString("new clipboard content"));
        QVERIFY(!QApplication::clipboard()->mimeData()->hasFormat("application/x-datahorizon-cells"));
        QCOMPARE(window.findChild<QLabel *>("storageStatus")->text(),status); QCOMPARE(issues->toPlainText(),previousIssues);
    }
    void clipboardLimitsRejectBeforeCellConstructionOrSubmission() {
        QTemporaryDir temp; StartupOptions options; options.automaticWorkbook=false;
        MainWindow window(options); window.show(); QSignalSpy ready(&window,&MainWindow::serviceReady);
        QTRY_COMPARE_WITH_TIMEOUT(ready.count(),1,10000);
        QSignalSpy opened(&window,&MainWindow::workbookReady); window.openBook(temp.filePath("clipboard-limit.dhwb"),true);
        QTRY_COMPARE_WITH_TIMEOUT(opened.count(),1,10000);
        window.submit({{"action","edit"},{"cells",QJsonArray{QJsonObject{{"row",1},{"col",1},{"source","retained"}}}}});
        QTRY_COMPARE_WITH_TIMEOUT(opened.count(),2,10000);
        auto *model=window.model(); auto *grid=window.findChild<QTableView *>("grid");
        QTRY_COMPARE_WITH_TIMEOUT(model->data(model->index(0,0)).toString(),QString("retained"),5000);
        grid->setCurrentIndex(model->index(0,0)); grid->setFocus();
        QAction *paste=nullptr;
        for(auto *action:window.findChildren<QAction *>()) if(action->shortcut().matches(QKeySequence::Paste)==QKeySequence::ExactMatch) paste=action;
        QVERIFY(paste);
        auto *issues=window.findChild<QPlainTextEdit *>("operationIssues"); QSignalSpy changed(&window,&MainWindow::operationFinished);
        auto reject=[&](QMimeData *mime,const QString &message) {
            issues->clear(); QApplication::clipboard()->setMimeData(mime); paste->trigger();
            return issues->toPlainText().contains(message) && changed.count()==0;
        };
        auto *largeText=new QMimeData; largeText->setText(QString(4*1024*1024+1,'x'));
        QVERIFY(reject(largeText,"4 MiB"));
        auto *manyFields=new QMimeData; manyFields->setText(QString(10000,'\t'));
        QVERIFY(reject(manyFields,"10,000-cell"));
        auto *largeNative=new QMimeData; largeNative->setData("application/x-datahorizon-cells",QByteArray(4*1024*1024+1,'x'));
        QVERIFY(reject(largeNative,"4 MiB"));
        QJsonArray row; for(int i=0;i<10001;++i) row.append(QJsonObject{{"source","x"}});
        auto *manyNative=new QMimeData;
        manyNative->setData("application/x-datahorizon-cells",QJsonDocument(QJsonObject{{"rows",QJsonArray{row}}}).toJson(QJsonDocument::Compact));
        QVERIFY(reject(manyNative,"10,000-cell"));
        QCOMPARE(model->data(model->index(0,0)).toString(),QString("retained"));
    }
    void oversizedRequestsHaveAnAsynchronousKnownRejectionWithoutNetworkAccess() {
        QTcpServer server; QVERIFY(server.listen(QHostAddress::LocalHost,0));
        QSignalSpy connections(&server,&QTcpServer::newConnection);
        RestClient client; client.configure("http://127.0.0.1:"+QString::number(server.serverPort()),"token");
        bool called=false; QJsonObject receipt; QString message;
        client.request("/operations",{{"operation_id","oversized"},{"input",QString(4*1024*1024,'x')}},
                       [&](QJsonObject result,QString error){called=true;receipt=result;message=error;});
        QVERIFY(!called);
        QTRY_VERIFY_WITH_TIMEOUT(called,3000);
        QVERIFY(message.contains("4 MiB")); QVERIFY(RestClient::validReceipt(receipt,"oversized"));
        QCOMPARE(receipt["outcome"].toString(),QString("rejected")); QCOMPARE(connections.count(),0);
    }
    void localRequestsBypassProxiesAndNeverFollowRedirects() {
        QTcpServer origin, proxy;
        origin.setProxy(QNetworkProxy::NoProxy); proxy.setProxy(QNetworkProxy::NoProxy);
        QVERIFY(origin.listen(QHostAddress::LocalHost,0)); QVERIFY(proxy.listen(QHostAddress::LocalHost,0));
        QSignalSpy intercepted(&proxy,&QTcpServer::newConnection);
        auto serve=[this](QTcpServer &server,const QByteArray &response) {
            return connect(&server,&QTcpServer::newConnection,this,[&server,response] {
                auto *socket=server.nextPendingConnection(); connect(socket,&QTcpSocket::disconnected,socket,&QObject::deleteLater);
                connect(socket,&QTcpSocket::readyRead,socket,[socket,response] {
                    auto request=socket->property("request").toByteArray()+socket->readAll(); socket->setProperty("request",request);
                    const auto end=request.indexOf("\r\n\r\n"); if(end<0 || socket->property("handled").toBool()) return;
                    const auto length=QRegularExpression("Content-Length: (\\d+)",QRegularExpression::CaseInsensitiveOption).match(QString::fromLatin1(request.left(end))).captured(1).toInt();
                    if(request.size()<end+4+length) return;
                    socket->setProperty("handled",true); socket->write(response); socket->disconnectFromHost();
                });
            });
        };
        serve(proxy,"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nConnection: close\r\nContent-Length: 2\r\n\r\n{}");
        const auto previous=QNetworkProxy::applicationProxy();
        auto restore=qScopeGuard([previous]{QNetworkProxy::setApplicationProxy(previous);});
        QNetworkProxy::setApplicationProxy(QNetworkProxy(QNetworkProxy::HttpProxy,"127.0.0.1",proxy.serverPort()));
        RestClient client; client.configure("http://127.0.0.1:"+QString::number(origin.serverPort()),"private-test-token");
        auto direct=serve(origin,"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nConnection: close\r\nContent-Length: 11\r\n\r\n{\"ok\":true}");
        bool done=false; QJsonObject result; QString error;
        client.request("/direct",{},[&](QJsonObject value,QString message){result=value;error=message;done=true;},false);
        QTRY_VERIFY_WITH_TIMEOUT(done,3000);
        QCOMPARE(intercepted.count(),0); QVERIFY2(error.isEmpty(),qPrintable(error)); QVERIFY(result["ok"].toBool());
        disconnect(direct);
        const QByteArray fake="{\"operation_id\":\"redirected-operation\",\"outcome\":\"rolled_back\",\"code\":\"FAKE\",\"message\":\"Not a terminal receipt\"}";
        serve(origin,"HTTP/1.1 307 Temporary Redirect\r\nLocation: http://127.0.0.1:"+QByteArray::number(proxy.serverPort())+"/capture\r\nContent-Type: application/json\r\nConnection: close\r\nContent-Length: "+QByteArray::number(fake.size())+"\r\n\r\n"+fake);
        done=false;
        client.request("/redirect",{{"operation_id","redirected-operation"},{"private","workbook contents"}},[&](QJsonObject value,QString message){result=value;error=message;done=true;});
        QTRY_VERIFY_WITH_TIMEOUT(done,3000);
        QCOMPARE(intercepted.count(),0); QVERIFY(!error.isEmpty()); QVERIFY(result.isEmpty());
    }
    void invalidHttpAcknowledgementsAreErrors() {
        QTcpServer server; QVERIFY(server.listen(QHostAddress::LocalHost,0));
        QByteArray payload;
        QByteArray status="200 OK";
        connect(&server,&QTcpServer::newConnection,this,[&]{
            auto *socket=server.nextPendingConnection();
            connect(socket,&QTcpSocket::disconnected,socket,&QObject::deleteLater);
            connect(socket,&QTcpSocket::readyRead,socket,[socket,&payload,&status]{
                auto request=socket->property("request").toByteArray()+socket->readAll(); socket->setProperty("request",request);
                if(!request.contains("\r\n\r\n")) return;
                socket->write("HTTP/1.1 "+status+"\r\nContent-Type: application/json\r\nConnection: close\r\nContent-Length: "+QByteArray::number(payload.size())+"\r\n\r\n"+payload);
                socket->disconnectFromHost();
            });
        });
        RestClient client; client.configure("http://127.0.0.1:"+QString::number(server.serverPort()),"test");
        for(const auto &body:{QByteArray("not json"),QByteArray("[]"),QByteArray("null"),QByteArray("{}")}) {
            payload=body; bool done=false; QString error; QJsonObject result;
            client.request("/receipt",{},[&](QJsonObject value,QString message){result=value;error=message;done=true;},false);
            QTRY_VERIFY_WITH_TIMEOUT(done,3000);
            if(body!="{}") QVERIFY(!error.isEmpty());
            QVERIFY(!RestClient::validReceipt(result,"operation",true));
        }
        status="500 Server Error"; payload="{\"message\":\"\"}";
        bool failed=false; QString transportError;
        client.request("/failure",{},[&](QJsonObject,QString error){failed=true;transportError=error;},false);
        QTRY_VERIFY_WITH_TIMEOUT(failed,3000); QVERIFY(!transportError.isEmpty());
        QJsonObject receipt{{"operation_id","operation"},{"outcome","committed"},{"state","state:1"},{"changed_cells",0}};
        QVERIFY(RestClient::validReceipt(receipt,"operation",true));
        QVERIFY(!RestClient::validReceipt(receipt,"other",true)); receipt.remove("state"); QVERIFY(!RestClient::validReceipt(receipt,"operation",true));
    }
    void genericMutationRejectionKeepsThePendingOperation() {
        QTemporaryDir temp; StartupOptions options; options.automaticWorkbook=false;
        options.recoveryDirectory=temp.filePath("recovery");
        MainWindow window(options); window.show(); QSignalSpy ready(&window,&MainWindow::serviceReady);
        QTRY_COMPARE_WITH_TIMEOUT(ready.count(),1,10000);
        QSignalSpy opened(&window,&MainWindow::workbookReady); window.openBook(temp.filePath("generic-rejection.dhwb"),true);
        QTRY_COMPARE_WITH_TIMEOUT(opened.count(),1,10000);
        QTRY_VERIFY_WITH_TIMEOUT(window.model()->sourceReady(window.model()->index(0,0)),5000);
        QTcpServer server; QVERIFY(server.listen(QHostAddress::LocalHost,0)); int mutations=0;
        connect(&server,&QTcpServer::newConnection,this,[&]{
            auto *socket=server.nextPendingConnection(); connect(socket,&QTcpSocket::disconnected,socket,&QObject::deleteLater);
            connect(socket,&QTcpSocket::readyRead,socket,[&,socket]{
                const auto request=socket->property("request").toByteArray()+socket->readAll(); socket->setProperty("request",request);
                if(!request.contains("\r\n\r\n") || socket->property("handled").toBool()) return;
                socket->setProperty("handled",true);
                const bool mutation=request.startsWith("POST ") && request.contains("/operations HTTP/");
                if(mutation) ++mutations;
                const QByteArray body=mutation
                    ? "{\"code\":\"STORAGE_ERROR\",\"message\":\"database is locked\",\"outcome\":\"rejected\"}"
                    : "{\"code\":\"OUTCOME_UNKNOWN\",\"message\":\"No confirmed receipt\",\"outcome\":\"unknown\"}";
                socket->write("HTTP/1.1 409 Conflict\r\nContent-Type: application/json\r\nConnection: close\r\nContent-Length: "+QByteArray::number(body.size())+"\r\n\r\n"+body);
                socket->disconnectFromHost();
            });
        });
        window.findChild<RestClient *>()->configure("http://127.0.0.1:"+QString::number(server.serverPort()),"token");
        QSignalSpy changed(&window,&MainWindow::operationFinished);
        const QJsonObject operation{{"action","edit"},{"cells",QJsonArray{QJsonObject{{"row",1},{"col",1},{"source","7"}}}}};
        window.submit(operation); QTRY_COMPARE_WITH_TIMEOUT(changed.count(),1,5000); QVERIFY(!changed[0][0].toBool());
        QVERIFY(window.findChild<QLabel *>("storageStatus")->text().contains("Outcome unknown"));
        QVERIFY(!(window.model()->flags(window.model()->index(0,0))&Qt::ItemIsEditable));
        window.submit(operation); QCOMPARE(mutations,1);
        QDir recovery(options.recoveryDirectory); QCOMPARE(recovery.entryList({"operation-*.json"},QDir::Files).size(),1);
        answerNextDialog(QMessageBox::Yes); QVERIFY(window.close());
    }
    void windowsRecoveryPathsAreRecognized() {
        QVERIFY(MainWindow::isRecoveryPath("C:\\Users\\test\\DataHorizon\\recovery\\untitled.dhwb"));
        QVERIFY(MainWindow::isRecoveryPath("/tmp/recovery/untitled.dhwb"));
        QVERIFY(!MainWindow::isRecoveryPath("/tmp/ordinary.dhwb"));
    }
    void stoppedServiceCanRestartAndReopenStoredInputs() {
        QTemporaryDir temp; StartupOptions options; options.automaticWorkbook=false; options.recoveryDirectory=temp.filePath("recovery");
        MainWindow window(options); window.show();
        QSignalSpy ready(&window,&MainWindow::serviceReady); QTRY_COMPARE_WITH_TIMEOUT(ready.count(),1,10000);
        QSignalSpy opened(&window,&MainWindow::workbookReady); window.openBook(temp.filePath("restart.dhwb"),true);
        QTRY_COMPARE_WITH_TIMEOUT(opened.count(),1,10000);
        window.submit({{"action","edit"},{"cells",QJsonArray{QJsonObject{{"row",1},{"col",1},{"source","7"}}}}});
        QTRY_COMPARE_WITH_TIMEOUT(opened.count(),2,10000);
        auto *process=window.findChild<QProcess *>("ownedService"); QVERIFY(process); process->kill();
        QTRY_COMPARE_WITH_TIMEOUT(process->state(),QProcess::NotRunning,5000);
        window.findChild<QAction *>("restartServiceAction")->trigger();
        QTRY_COMPARE_WITH_TIMEOUT(ready.count(),2,10000); QTRY_COMPARE_WITH_TIMEOUT(opened.count(),3,10000);
        QTRY_COMPARE_WITH_TIMEOUT(window.model()->data(window.model()->index(0,0)).toString(),QString("7"),5000);
    }
    void unresolvedCrashCanCloseWithDurableRecoveryRecord() {
        QTemporaryDir temp; StartupOptions options; options.automaticWorkbook=false; options.recoveryDirectory=temp.filePath("recovery");
        options.handler=QString(DATAHORIZON_FIXTURES)+"/timeout_hook.py";
        MainWindow window(options); window.show();
        QSignalSpy ready(&window,&MainWindow::serviceReady); QTRY_COMPARE_WITH_TIMEOUT(ready.count(),1,10000);
        QSignalSpy opened(&window,&MainWindow::workbookReady); const auto path=temp.filePath("unknown.dhwb"); window.openBook(path,true);
        QTRY_COMPARE_WITH_TIMEOUT(opened.count(),1,10000);
        window.submit({{"action","edit"},{"cells",QJsonArray{QJsonObject{{"row",1},{"col",1},{"source","7"}}}}});
        auto *process=window.findChild<QProcess *>("ownedService"); process->kill();
        QTRY_COMPARE_WITH_TIMEOUT(process->state(),QProcess::NotRunning,5000);
        QDir recovery(options.recoveryDirectory); QTRY_COMPARE_WITH_TIMEOUT(recovery.entryList({"operation-*.json"},QDir::Files).size(),1,5000);
        const auto recordPath=recovery.filePath(recovery.entryList({"operation-*.json"},QDir::Files).first());
        QFile record(recordPath); QVERIFY(record.open(QIODevice::ReadOnly)); const auto info=QJsonDocument::fromJson(record.readAll()).object(); record.close();
        QCOMPARE(QFileInfo(info["workbook_path"].toString()).canonicalFilePath(),QFileInfo(path).canonicalFilePath()); QVERIFY(!info["operation_id"].toString().isEmpty());
        answerNextDialog(QMessageBox::Yes); QVERIFY(window.close()); QVERIFY(QFile::exists(recordPath));
    }
    void initialWorkbookIsOpenedOnlyOnceAcrossServiceRestart() {
        QTemporaryDir temp; StartupOptions options; options.automaticWorkbook=false;
        const auto first=temp.filePath("first.dhwb"), second=temp.filePath("second.dhwb");
        {
            MainWindow seed(options); QSignalSpy ready(&seed,&MainWindow::serviceReady);
            QTRY_COMPARE_WITH_TIMEOUT(ready.count(),1,10000);
            QSignalSpy opened(&seed,&MainWindow::workbookReady); seed.openBook(first,true);
            QTRY_COMPARE_WITH_TIMEOUT(opened.count(),1,10000);
        }
        options.initialWorkbook=first;
        MainWindow window(options); window.show();
        QSignalSpy opened(&window,&MainWindow::workbookReady); QTRY_COMPARE_WITH_TIMEOUT(opened.count(),1,10000);
        window.openBook(second,true); QTRY_COMPARE_WITH_TIMEOUT(opened.count(),2,10000);
        window.submit({{"action","edit"},{"cells",QJsonArray{QJsonObject{{"row",1},{"col",1},{"source","second workbook"}}}}});
        QTRY_COMPARE_WITH_TIMEOUT(opened.count(),3,10000);
        auto *process=window.findChild<QProcess *>("ownedService"); process->kill();
        QTRY_COMPARE_WITH_TIMEOUT(process->state(),QProcess::NotRunning,5000);
        window.findChild<QAction *>("restartServiceAction")->trigger();
        QTRY_COMPARE_WITH_TIMEOUT(opened.count(),4,10000);
        QTRY_COMPARE_WITH_TIMEOUT(window.model()->data(window.model()->index(0,0)).toString(),QString("second workbook"),5000);
        QVERIFY(window.windowTitle().contains("second.dhwb"));
    }
    void restartPreservesDraftAndRequiresReviewOfReopenedInput() {
        QTemporaryDir temp; StartupOptions options; options.automaticWorkbook=false;
        const auto path=temp.filePath("draft-restart.dhwb");
        MainWindow window(options); window.show();
        QSignalSpy ready(&window,&MainWindow::serviceReady); QTRY_COMPARE_WITH_TIMEOUT(ready.count(),1,10000);
        QSignalSpy opened(&window,&MainWindow::workbookReady); window.openBook(path,true);
        QTRY_COMPARE_WITH_TIMEOUT(opened.count(),1,10000);
        window.submit({{"action","edit"},{"cells",QJsonArray{QJsonObject{{"row",3},{"col",2},{"source","old input"},{"kind","text"}}}}});
        QTRY_COMPARE_WITH_TIMEOUT(opened.count(),2,10000);
        auto *grid=window.findChild<QTableView *>("grid"); auto *source=window.findChild<QLineEdit *>("source");
        grid->setCurrentIndex(window.model()->index(2,1));
        QTRY_COMPARE_WITH_TIMEOUT(source->text(),QString("old input"),5000);
        source->setFocus(); source->selectAll(); QTest::keyClicks(source,"retained draft");
        auto *process=window.findChild<QProcess *>("ownedService"); process->kill();
        QTRY_COMPARE_WITH_TIMEOUT(process->state(),QProcess::NotRunning,5000);
        QCOMPARE(source->text(),QString("retained draft"));
        {
            MainWindow writer(options); QSignalSpy writerReady(&writer,&MainWindow::serviceReady);
            QTRY_COMPARE_WITH_TIMEOUT(writerReady.count(),1,10000);
            QSignalSpy writerOpened(&writer,&MainWindow::workbookReady); writer.openBook(path);
            QTRY_COMPARE_WITH_TIMEOUT(writerOpened.count(),1,10000);
            writer.submit({{"action","edit"},{"cells",QJsonArray{QJsonObject{{"row",3},{"col",2},{"source","changed while stopped"}}}}});
            QTRY_COMPARE_WITH_TIMEOUT(writerOpened.count(),2,10000);
        }
        window.findChild<QAction *>("restartServiceAction")->trigger();
        QTRY_COMPARE_WITH_TIMEOUT(ready.count(),2,10000); QTRY_COMPARE_WITH_TIMEOUT(opened.count(),3,10000);
        QTRY_VERIFY_WITH_TIMEOUT(source->isEnabled(),5000);
        QCOMPARE(grid->currentIndex(),window.model()->index(2,1)); QCOMPARE(source->text(),QString("retained draft")); QVERIFY(source->isModified());
        QSignalSpy changed(&window,&MainWindow::operationFinished); QString reviewText;
        answerNextDialog(QMessageBox::Cancel,&reviewText); QTest::keyClick(source,Qt::Key_Return);
        QVERIFY(reviewText.contains("changed while stopped")); QVERIFY(reviewText.contains("B3"));
        QCOMPARE(changed.count(),0); QCOMPARE(source->text(),QString("retained draft"));
        answerNextDialog(QMessageBox::Yes); QTest::keyClick(source,Qt::Key_Return);
        QTRY_COMPARE_WITH_TIMEOUT(changed.count(),1,10000); QTRY_COMPARE_WITH_TIMEOUT(opened.count(),4,10000);
        QTRY_COMPARE_WITH_TIMEOUT(window.model()->data(window.model()->index(2,1)).toString(),QString("retained draft"),5000);
        QCOMPARE(window.model()->cell(window.model()->index(2,1))["input_kind"].toString(),QString("text"));
    }
    void emptyListHasNoEditorTarget() {
        QTemporaryDir temp; StartupOptions options; options.automaticWorkbook=false;
        MainWindow window(options); window.show(); QSignalSpy ready(&window,&MainWindow::serviceReady);
        QTRY_COMPARE_WITH_TIMEOUT(ready.count(),1,10000);
        QSignalSpy opened(&window,&MainWindow::workbookReady); const auto path=temp.filePath("empty-list.dhwb"); window.openBook(path,true);
        QTRY_COMPARE_WITH_TIMEOUT(opened.count(),1,10000);
        window.submit({{"action","edit"},{"cells",QJsonArray{QJsonObject{{"row",1},{"col",1},{"source","original"}}}}});
        QTRY_COMPARE_WITH_TIMEOUT(opened.count(),2,10000);
        auto *source=window.findChild<QLineEdit *>("source"); QTRY_COMPARE_WITH_TIMEOUT(source->text(),QString("original"),5000);
        window.showList({{"r1",1},{"r2",1},{"c1",1},{"c2",1},{"header",false},{"filters",QJsonArray{QJsonObject{{"col",1},{"op","="},{"value","no match"}}}}});
        QTRY_COMPARE_WITH_TIMEOUT(opened.count(),3,10000);
        QCOMPARE(window.model()->rowCount(),0); QVERIFY(!source->isEnabled()); QVERIFY(source->text().isEmpty());
        QSignalSpy changed(&window,&MainWindow::operationFinished);
        source->setText("hidden edit"); source->setModified(true); source->returnPressed();
        QCOMPARE(changed.count(),0);
        source->setModified(false); window.openBook(path); QTRY_COMPARE_WITH_TIMEOUT(opened.count(),4,10000);
        QTRY_COMPARE_WITH_TIMEOUT(window.model()->data(window.model()->index(0,0)).toString(),QString("original"),5000);
    }
    void copyAndCutSupportTenThousandCellSelections() {
        QTemporaryDir temp; StartupOptions options; options.automaticWorkbook=false;
        MainWindow window(options); window.show(); QSignalSpy ready(&window,&MainWindow::serviceReady);
        QTRY_COMPARE_WITH_TIMEOUT(ready.count(),1,10000);
        QSignalSpy opened(&window,&MainWindow::workbookReady); window.openBook(temp.filePath("bulk-copy.dhwb"),true);
        QTRY_COMPARE_WITH_TIMEOUT(opened.count(),1,10000);
        window.submit({{"action","edit"},{"cells",QJsonArray{QJsonObject{{"row",1},{"col",1},{"source","start"}},QJsonObject{{"row",10000},{"col",1},{"source","tail"}}}}});
        QTRY_COMPARE_WITH_TIMEOUT(opened.count(),2,10000);
        auto *model=window.model(); auto *grid=window.findChild<QTableView *>("grid");
        QTRY_VERIFY_WITH_TIMEOUT(model->loaded(model->index(0,0)),5000);
        grid->setCurrentIndex(model->index(0,0));
        grid->selectionModel()->select(QItemSelection(model->index(0,0),model->index(9999,0)),QItemSelectionModel::ClearAndSelect); grid->setFocus();
        QApplication::clipboard()->clear();
        for(auto *action:window.findChildren<QAction *>()) if(action->shortcut()==QKeySequence(QKeySequence::Copy)) action->trigger();
        auto clipboard=[] {const auto *mime=QApplication::clipboard()->mimeData(); return mime?QJsonDocument::fromJson(mime->data("application/x-datahorizon-cells")).object():QJsonObject{};};
        QTRY_COMPARE_WITH_TIMEOUT(clipboard()["rows"].toArray().size(),10000,5000);
        QCOMPARE(clipboard()["rows"].toArray().last().toArray().first().toObject()["source"].toString(),QString("tail"));
        for(auto *action:window.findChildren<QAction *>()) if(action->shortcut()==QKeySequence(QKeySequence::Cut)) action->trigger();
        QTRY_VERIFY_WITH_TIMEOUT(clipboard()["cut"].toBool(),5000);
        QCOMPARE(clipboard()["rows"].toArray().size(),10000);
        QCOMPARE(model->data(model->index(0,0)).toString(),QString("start"));
    }
    void longTextTilesRemainEditableWithoutOversizedReads() {
        QTemporaryDir temp; StartupOptions options; options.automaticWorkbook=false;
        MainWindow window(options); window.show(); QSignalSpy ready(&window,&MainWindow::serviceReady);
        QTRY_COMPARE_WITH_TIMEOUT(ready.count(),1,10000);
        QSignalSpy opened(&window,&MainWindow::workbookReady); window.openBook(temp.filePath("long-tile.dhwb"),true);
        QTRY_COMPARE_WITH_TIMEOUT(opened.count(),1,10000);
        const QString text(64000,'x'); QJsonArray cells;
        for(int row=1;row<=32;++row) cells.append(QJsonObject{{"row",row},{"col",1},{"source",text},{"kind","text"}});
        window.submit({{"action","edit"},{"cells",cells}}); QTRY_COMPARE_WITH_TIMEOUT(opened.count(),2,10000);
        auto *model=window.model(); auto *grid=window.findChild<QTableView *>("grid"); auto *source=window.findChild<QLineEdit *>("source");
        QTRY_VERIFY_WITH_TIMEOUT(model->loaded(model->index(31,0)),5000);
        QTRY_COMPARE_WITH_TIMEOUT(model->data(model->index(31,0),Qt::EditRole).toString(),text,5000);
        grid->setCurrentIndex(model->index(31,0)); QTRY_COMPARE_WITH_TIMEOUT(source->text(),text,5000);
        QVERIFY(model->flags(model->index(31,0))&Qt::ItemIsEditable);
        source->setFocus(); source->setCursorPosition(source->text().size()); QTest::keyClicks(source,"y"); QTest::keyClick(source,Qt::Key_Return);
        QTRY_COMPARE_WITH_TIMEOUT(opened.count(),3,10000);
        QTRY_COMPARE_WITH_TIMEOUT(model->data(model->index(31,0),Qt::EditRole).toString(),text+"y",5000);
        QVERIFY(!window.findChild<QPlainTextEdit *>("operationIssues")->toPlainText().contains("READ_LIMIT"));
    }
    void fullSourceReplyMustMatchViewportState() {
        QTcpServer server; QVERIFY(server.listen(QHostAddress::LocalHost,0));
        connect(&server,&QTcpServer::newConnection,this,[&]{
            auto *socket=server.nextPendingConnection(); connect(socket,&QTcpSocket::disconnected,socket,&QObject::deleteLater);
            connect(socket,&QTcpSocket::readyRead,socket,[socket]{
                const auto request=socket->property("request").toByteArray()+socket->readAll(); socket->setProperty("request",request);
                const int split=request.indexOf("\r\n\r\n"); if(split<0) return;
                const auto length=QRegularExpression("Content-Length: ([0-9]+)",QRegularExpression::CaseInsensitiveOption).match(QString::fromLatin1(request.left(split))).captured(1).toInt();
                if(request.size()<split+4+length) return;
                const bool preview=QJsonDocument::fromJson(request.mid(split+4,length)).object()["preview"].toBool();
                const auto body=QJsonDocument(QJsonObject{{"state",preview?"state:1":"state:2"},{"sheet","sheet"},
                    {"cells",QJsonArray{QJsonObject{{"row",1},{"col",1},{"source",preview?"preview":"different input"},{"source_omitted",preview},{"display","preview"}}}}}).toJson();
                socket->write("HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nConnection: close\r\nContent-Length: "+QByteArray::number(body.size())+"\r\n\r\n"+body); socket->disconnectFromHost();
            });
        });
        RestClient client; client.configure("http://127.0.0.1:"+QString::number(server.serverPort()),"test");
        SheetModel model(&client,nullptr,32,16); model.bind("book","sheet","state:1",false); QSignalSpy changed(&model,&SheetModel::externalChange);
        model.data(model.index(0,0)); QTRY_VERIFY_WITH_TIMEOUT(model.loaded(model.index(0,0)),3000);
        model.ensureSource(model.index(0,0)); QTRY_COMPARE_WITH_TIMEOUT(changed.count(),1,3000);
        QVERIFY(!model.sourceReady(model.index(0,0))); QVERIFY(!(model.flags(model.index(0,0))&Qt::ItemIsEditable));
    }
    void csvExportCannotReplaceNativeWorkbookOrItsAlias() {
        QTemporaryDir temp; StartupOptions options; options.automaticWorkbook=false;
        MainWindow window(options); window.show(); QSignalSpy ready(&window,&MainWindow::serviceReady);
        QTRY_COMPARE_WITH_TIMEOUT(ready.count(),1,10000);
        const auto path=temp.filePath("retained.dhwb");
        QSignalSpy opened(&window,&MainWindow::workbookReady); window.openBook(path,true);
        QTRY_COMPARE_WITH_TIMEOUT(opened.count(),1,10000);
        window.submit({{"action","edit"},{"cells",QJsonArray{QJsonObject{{"row",1},{"col",1},{"source","7"}}}}});
        QTRY_COMPARE_WITH_TIMEOUT(opened.count(),2,10000);
        auto *model=window.model(); auto *grid=window.findChild<QTableView *>("grid");
        QTRY_VERIFY_WITH_TIMEOUT(model->loaded(model->index(0,0)),5000);
        grid->setCurrentIndex(model->index(0,0));
        window.exportCsvTo(path); window.exportCsvTo(temp.path()+"/./retained.dhwb");
#ifdef Q_OS_UNIX
        const auto alias=temp.filePath("alias.csv"); QVERIFY(QFile::link(path,alias)); window.exportCsvTo(alias);
#endif
        QVERIFY(window.findChild<QPlainTextEdit *>("operationIssues")->toPlainText().contains("destination is the open native workbook"));
        QFile retained(path); QVERIFY(retained.open(QIODevice::ReadOnly)); QCOMPARE(retained.read(16),QByteArray("SQLite format 3\0",16)); retained.close();
        const auto csv=temp.filePath("values.csv"); window.exportCsvTo(csv);
        QTRY_VERIFY_WITH_TIMEOUT(QFile::exists(csv),5000);
        QFile output(csv); QVERIFY(output.open(QIODevice::ReadOnly)); QCOMPARE(output.readAll(),QByteArray("7\r\n"));
        window.openBook(path); QTRY_COMPARE_WITH_TIMEOUT(opened.count(),3,10000);
        QTRY_COMPARE_WITH_TIMEOUT(model->data(model->index(0,0)).toString(),QString("7"),5000);
    }
    void unchangedEditorsPreserveNativeInputsWithoutRunningHooks_data() {
        QTest::addColumn<bool>("inlineEditor"); QTest::addColumn<QString>("inputKind"); QTest::addColumn<QString>("input");
        QTest::addColumn<bool>("textFormat"); QTest::addColumn<bool>("present"); QTest::addColumn<bool>("valuesPaste");
        for(bool inlineEditor:{false,true}) {
            const auto prefix=inlineEditor?QString("cell-"):QString("formula-bar-");
            QTest::newRow(qPrintable(prefix+"null")) << inlineEditor << QString("null") << QString() << false << true << false;
            QTest::newRow(qPrintable(prefix+"number-with-text-format")) << inlineEditor << QString("number") << QString("7") << true << true << false;
            QTest::newRow(qPrintable(prefix+"boolean")) << inlineEditor << QString("boolean") << QString("TRUE") << false << true << false;
            QTest::newRow(qPrintable(prefix+"formula-with-text-format")) << inlineEditor << QString("formula") << QString("=SUM(1,2)") << true << true << false;
            QTest::newRow(qPrintable(prefix+"auto-number-with-text-format")) << inlineEditor << QString("auto") << QString("7") << true << true << false;
            QTest::newRow(qPrintable(prefix+"text")) << inlineEditor << QString("text") << QString("00123") << false << true << false;
            QTest::newRow(qPrintable(prefix+"absence")) << inlineEditor << QString("auto") << QString() << false << false << false;
            QTest::newRow(qPrintable(prefix+"values-pasted-null")) << inlineEditor << QString("null") << QString() << false << true << true;
        }
    }
    void unchangedEditorsPreserveNativeInputsWithoutRunningHooks() {
        QFETCH(bool,inlineEditor); QFETCH(QString,inputKind); QFETCH(QString,input);
        QFETCH(bool,textFormat); QFETCH(bool,present); QFETCH(bool,valuesPaste);
        QTemporaryDir temp;
        const auto handler=temp.filePath("count_changes.py"); QFile code(handler); QVERIFY(code.open(QIODevice::WriteOnly));
        code.write("def on_change(context, changes):\n"
            "    for change in changes:\n"
            "        if (change['row'], change['col']) == (1, 1):\n"
            "            value = context.value(change['sheet'], 1, 4) or 0\n"
            "            context.set(change['sheet'], 1, 4, str(value + 1))\n"); code.close();
        StartupOptions options; options.automaticWorkbook=false; options.handler=handler;
        MainWindow window(options); window.show(); QSignalSpy ready(&window,&MainWindow::serviceReady);
        QTRY_COMPARE_WITH_TIMEOUT(ready.count(),1,10000);
        QSignalSpy opened(&window,&MainWindow::workbookReady); window.openBook(temp.filePath("unchanged-input.dhwb"),true);
        QTRY_COMPARE_WITH_TIMEOUT(opened.count(),1,10000);
        QJsonArray cells{QJsonObject{{"row",1},{"col",4},{"source","0"}}};
        if(valuesPaste) cells.append(QJsonObject{{"row",2},{"col",1},{"source","=Z1"}});
        else if(present) cells.append(QJsonObject{{"row",1},{"col",1},{"source",input},{"kind",inputKind}});
        window.submit({{"action","edit"},{"cells",cells}}); int expectedOpened=2;
        QTRY_COMPARE_WITH_TIMEOUT(opened.count(),expectedOpened,10000);
        if(textFormat) {
            window.submit({{"action","edit"},{"cells",QJsonArray{QJsonObject{{"row",1},{"col",1},{"style",QJsonObject{{"format","text"}}}}}}});
            ++expectedOpened; QTRY_COMPARE_WITH_TIMEOUT(opened.count(),expectedOpened,10000);
        }
        auto *model=window.model(); auto *grid=window.findChild<QTableView *>("grid"); auto *source=window.findChild<QLineEdit *>("source");
        if(valuesPaste) {
            QTRY_VERIFY_WITH_TIMEOUT(model->sourceReady(model->index(1,0)),5000);
            QAction *copy=nullptr,*pasteValues=nullptr;
            for(auto *action:window.findChildren<QAction *>()) {
                if(action->shortcut().matches(QKeySequence::Copy)==QKeySequence::ExactMatch) copy=action;
                if(action->text()=="Paste values only") pasteValues=action;
            }
            QVERIFY(copy); QVERIFY(pasteValues); QApplication::clipboard()->clear();
            grid->setCurrentIndex(model->index(1,0)); grid->setFocus(); copy->trigger();
            QTRY_VERIFY_WITH_TIMEOUT(QApplication::clipboard()->mimeData()
                && QApplication::clipboard()->mimeData()->hasFormat("application/x-datahorizon-cells"),5000);
            grid->setCurrentIndex(model->index(0,0)); pasteValues->trigger();
            ++expectedOpened; QTRY_COMPARE_WITH_TIMEOUT(opened.count(),expectedOpened,10000);
        }
        grid->setCurrentIndex(model->index(0,0)); QTRY_VERIFY_WITH_TIMEOUT(source->isEnabled(),5000);
        const auto before=model->cell(model->index(0,0)); const auto state=model->state();
        const auto hookCount=model->cell(model->index(0,3))["value"].toDouble();
        QCOMPARE(before.contains("row"),present);
        if(present) { QCOMPARE(before["input_kind"].toString(),inputKind); QCOMPARE(before["source"].toString(),input); }
        QSignalSpy changed(&window,&MainWindow::operationFinished);
        if(inlineEditor) {
            grid->edit(model->index(0,0)); QPointer<QLineEdit> editor=grid->findChild<QLineEdit *>(); QVERIFY(editor);
            QVERIFY(!editor->isModified()); QTest::keyClick(editor,Qt::Key_Return); QTRY_VERIFY_WITH_TIMEOUT(editor.isNull(),3000);
        } else { source->setFocus(); QVERIFY(!source->isModified()); QTest::keyClick(source,Qt::Key_Return); }
        ++expectedOpened; QTRY_COMPARE_WITH_TIMEOUT(opened.count(),expectedOpened,10000);
        QCOMPARE(changed.count(),1); QVERIFY(changed[0][0].toBool());
        QTRY_VERIFY_WITH_TIMEOUT(model->loaded(model->index(0,0)),5000);
        QVERIFY(model->state()!=state); QCOMPARE(model->cell(model->index(0,0)),before);
        QCOMPARE(model->cell(model->index(0,3))["value"].toDouble(),hookCount);
        QCOMPARE(grid->currentIndex(),model->index(1,0));
        grid->setCurrentIndex(model->index(0,0)); QTRY_VERIFY_WITH_TIMEOUT(source->isEnabled(),5000);
        if(inlineEditor) {
            grid->edit(model->index(0,0)); QPointer<QLineEdit> editor=grid->findChild<QLineEdit *>(); QVERIFY(editor);
            editor->selectAll(); QTest::keyClicks(editor,"9"); QTest::keyClick(editor,Qt::Key_Return);
        } else { source->setFocus(); source->selectAll(); QTest::keyClicks(source,"9"); QTest::keyClick(source,Qt::Key_Return); }
        ++expectedOpened; QTRY_COMPARE_WITH_TIMEOUT(opened.count(),expectedOpened,10000);
        QCOMPARE(changed.count(),2); QVERIFY(changed[1][0].toBool());
        QTRY_VERIFY_WITH_TIMEOUT(model->loaded(model->index(0,0)),5000);
        const auto after=model->cell(model->index(0,0));
        QCOMPARE(after["source"].toString(),QString("9"));
        QCOMPARE(after["input_kind"].toString(),inputKind=="text" || textFormat?QString("text"):QString("auto"));
        QCOMPARE(after["kind"].toString(),inputKind=="text" || textFormat?QString("text"):QString("number"));
        QCOMPARE(model->cell(model->index(0,3))["value"].toDouble(),hookCount+1);
    }
    void guiEditsPreserveExplicitTextWhileAutoCellsStillInferTypes() {
        QTemporaryDir temp; StartupOptions options; options.automaticWorkbook=false;
        MainWindow window(options); window.show(); QSignalSpy ready(&window,&MainWindow::serviceReady);
        QTRY_COMPARE_WITH_TIMEOUT(ready.count(),1,10000);
        QSignalSpy opened(&window,&MainWindow::workbookReady); window.openBook(temp.filePath("typed-text.dhwb"),true);
        QTRY_COMPARE_WITH_TIMEOUT(opened.count(),1,10000);
        window.submit({{"action","edit"},{"cells",QJsonArray{QJsonObject{{"row",1},{"col",1},{"source","00123"},{"kind","text"}},
                                                          QJsonObject{{"row",2},{"col",1},{"source","auto text"}}}}});
        QTRY_COMPARE_WITH_TIMEOUT(opened.count(),2,10000);
        auto *model=window.model(); auto *source=window.findChild<QLineEdit *>("source");
        QTRY_COMPARE_WITH_TIMEOUT(source->text(),QString("00123"),5000);
        source->setFocus(); source->selectAll(); QTest::keyClicks(source,"00124"); QTest::keyClick(source,Qt::Key_Return);
        QTRY_COMPARE_WITH_TIMEOUT(opened.count(),3,10000);
        QTRY_COMPARE_WITH_TIMEOUT(model->data(model->index(0,0)).toString(),QString("00124"),5000);
        QCOMPARE(model->cell(model->index(0,0))["kind"].toString(),QString("text"));
        QVERIFY(model->setData(model->index(0,0),"=1+1",Qt::EditRole));
        QTRY_COMPARE_WITH_TIMEOUT(opened.count(),4,10000);
        QTRY_COMPARE_WITH_TIMEOUT(model->data(model->index(0,0)).toString(),QString("=1+1"),5000);
        QCOMPARE(model->cell(model->index(0,0))["kind"].toString(),QString("text"));
        QVERIFY(model->setData(model->index(1,0),"7",Qt::EditRole));
        QTRY_COMPARE_WITH_TIMEOUT(opened.count(),5,10000);
        QTRY_COMPARE_WITH_TIMEOUT(model->data(model->index(1,0)).toString(),QString("7"),5000);
        QCOMPARE(model->cell(model->index(1,0))["kind"].toString(),QString("number"));
    }
    void previousTransportSessionRepliesCannotReachCurrentUi() {
        QTcpServer server; QVERIFY(server.listen(QHostAddress::LocalHost,0));
        QPointer<QTcpSocket> delayed;
        auto reply=[](QTcpSocket *socket,const QByteArray &body){
            socket->write("HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nConnection: close\r\nContent-Length: "+QByteArray::number(body.size())+"\r\n\r\n"+body);
            socket->disconnectFromHost();
        };
        connect(&server,&QTcpServer::newConnection,this,[&]{
            auto *socket=server.nextPendingConnection(); connect(socket,&QTcpSocket::disconnected,socket,&QObject::deleteLater);
            connect(socket,&QTcpSocket::readyRead,socket,[&,socket]{
                auto request=socket->property("request").toByteArray()+socket->readAll(); socket->setProperty("request",request);
                if(!request.contains("\r\n\r\n") || socket->property("handled").toBool()) return;
                socket->setProperty("handled",true);
                if(request.startsWith("GET /old ")) delayed=socket;
                else reply(socket,"{\"value\":\"current\"}");
            });
        });
        RestClient client; const auto url="http://127.0.0.1:"+QString::number(server.serverPort()); client.configure(url,"old-token");
        QSignalSpy replies(client.findChild<QNetworkAccessManager *>(),&QNetworkAccessManager::finished);
        bool staleCallback=false,currentCallback=false;
        client.request("/old",{},[&](QJsonObject,QString){staleCallback=true;},false);
        QTRY_VERIFY_WITH_TIMEOUT(delayed,3000);
        client.configure(url,"new-token");
        client.request("/new",{},[&](QJsonObject result,QString error){currentCallback=error.isEmpty() && result["value"]=="current";},false);
        QTRY_VERIFY_WITH_TIMEOUT(currentCallback,3000);
        reply(delayed,"{\"value\":\"stale\"}");
        QTRY_COMPARE_WITH_TIMEOUT(replies.count(),2,3000);
        QVERIFY(!staleCallback);
    }
    void inCellDraftSurvivesServiceRestartAtItsOriginalTarget() {
        QTemporaryDir temp; StartupOptions options; options.automaticWorkbook=false;
        MainWindow window(options); window.show(); QSignalSpy ready(&window,&MainWindow::serviceReady);
        QTRY_COMPARE_WITH_TIMEOUT(ready.count(),1,10000);
        QSignalSpy opened(&window,&MainWindow::workbookReady); window.openBook(temp.filePath("inline-draft.dhwb"),true);
        QTRY_COMPARE_WITH_TIMEOUT(opened.count(),1,10000);
        window.submit({{"action","edit"},{"cells",QJsonArray{QJsonObject{{"row",3},{"col",2},{"source","old"},{"kind","text"}}}}});
        QTRY_COMPARE_WITH_TIMEOUT(opened.count(),2,10000);
        auto *model=window.model(); auto *grid=window.findChild<QTableView *>("grid"); auto *source=window.findChild<QLineEdit *>("source");
        QTRY_COMPARE_WITH_TIMEOUT(model->data(model->index(2,1)).toString(),QString("old"),5000);
        grid->setCurrentIndex(model->index(2,1)); grid->edit(model->index(2,1));
        QPointer<QLineEdit> editor=grid->findChild<QLineEdit *>(); QVERIFY(editor);
        editor->selectAll(); QTest::keyClicks(editor,"00124");
        answerNextDialog(QMessageBox::Cancel);
        for(auto *action:window.findChildren<QAction *>()) if(action->text()=="Refresh stored data") action->trigger();
        QCOMPARE(source->text(),QString("00124")); QVERIFY(source->isModified());
        QTest::keyClick(source,Qt::Key_Escape);
        QTRY_VERIFY_WITH_TIMEOUT(editor.isNull(),3000);
        grid->edit(model->index(2,1)); editor=grid->findChild<QLineEdit *>(); QVERIFY(editor);
        editor->selectAll(); QTest::keyClicks(editor,"00124");
        auto *process=window.findChild<QProcess *>("ownedService"); process->kill();
        QTRY_COMPARE_WITH_TIMEOUT(process->state(),QProcess::NotRunning,5000);
        QTRY_COMPARE_WITH_TIMEOUT(source->text(),QString("00124"),5000); QVERIFY(source->isModified());
        window.findChild<QAction *>("restartServiceAction")->trigger();
        QTRY_COMPARE_WITH_TIMEOUT(opened.count(),3,10000); QTRY_VERIFY_WITH_TIMEOUT(source->isEnabled(),5000);
        QCOMPARE(grid->currentIndex(),model->index(2,1)); QCOMPARE(source->text(),QString("00124"));
        answerNextDialog(QMessageBox::Yes); QTest::keyClick(source,Qt::Key_Return);
        QTRY_COMPARE_WITH_TIMEOUT(opened.count(),4,10000);
        QTRY_COMPARE_WITH_TIMEOUT(model->data(model->index(2,1)).toString(),QString("00124"),5000);
        QCOMPARE(model->cell(model->index(2,1))["kind"].toString(),QString("text"));
    }
    void reviewedOperationsCannotSilentlyUseNewStateOrSheet() {
        QTemporaryDir temp; StartupOptions options; options.automaticWorkbook=false;
        MainWindow window(options); QSignalSpy ready(&window,&MainWindow::serviceReady);
        QTRY_COMPARE_WITH_TIMEOUT(ready.count(),1,10000);
        QSignalSpy opened(&window,&MainWindow::workbookReady); window.openBook(temp.filePath("reviewed-target.dhwb"),true);
        QTRY_COMPARE_WITH_TIMEOUT(opened.count(),1,10000);
        const auto reviewed=window.model()->state(), sheet=window.model()->sheet();
        window.submit({{"action","edit"},{"cells",QJsonArray{QJsonObject{{"row",1},{"col",1},{"source","new"}}}}});
        QTRY_COMPARE_WITH_TIMEOUT(opened.count(),2,10000);
        QSignalSpy changed(&window,&MainWindow::operationFinished);
        window.submit({{"action","replace_all"},{"r1",1},{"r2",1},{"c1",1},{"c2",1},{"needle","new"},{"replacement","wrong"},{"expected_state",reviewed},{"sheet",sheet}});
        window.submit({{"action","edit"},{"sheet","different-sheet"},{"cells",QJsonArray{QJsonObject{{"row",1},{"col",1},{"source","wrong"}}}}});
        window.submit({{"action","edit"},{"workbook","different-workbook"},{"cells",QJsonArray{QJsonObject{{"row",1},{"col",1},{"source","wrong"}}}}});
        QCOMPARE(changed.count(),0);
        QVERIFY(window.findChild<QPlainTextEdit *>("operationIssues")->toPlainText().contains("Reviewed inputs changed"));
        QTRY_COMPARE_WITH_TIMEOUT(window.model()->data(window.model()->index(0,0)).toString(),QString("new"),5000);
    }
    void sheetDeletionConfirmationCannotSwitchToAnotherSheetAfterRefresh() {
        QTemporaryDir temp; StartupOptions options; options.automaticWorkbook=false;
        MainWindow window(options); window.show(); QSignalSpy ready(&window,&MainWindow::serviceReady);
        QTRY_COMPARE_WITH_TIMEOUT(ready.count(),1,10000);
        const auto path=temp.filePath("confirmed-sheet.dhwb");
        QSignalSpy opened(&window,&MainWindow::workbookReady); window.openBook(path,true);
        QTRY_COMPARE_WITH_TIMEOUT(opened.count(),1,10000);
        window.submit({{"action","sheet_add"},{"name","Other"}}); QTRY_COMPARE_WITH_TIMEOUT(opened.count(),2,10000);
        window.submit({{"action","sheet_add"},{"name","Third"}}); QTRY_COMPARE_WITH_TIMEOUT(opened.count(),3,10000);
        const auto original=window.model()->sheet();
        MainWindow writer(options); QSignalSpy writerReady(&writer,&MainWindow::serviceReady);
        QTRY_COMPARE_WITH_TIMEOUT(writerReady.count(),1,10000);
        QSignalSpy writerOpened(&writer,&MainWindow::workbookReady); writer.openBook(path);
        QTRY_COMPARE_WITH_TIMEOUT(writerOpened.count(),1,10000);
        bool refreshed=false,confirmed=false;
        connect(&writer,&MainWindow::workbookReady,&window,[&]{window.model()->externalChange();});
        connect(&window,&MainWindow::workbookReady,&window,[&]{
            if(window.model()->sheet()==original) return;
            refreshed=true;
            if(auto *box=qobject_cast<QMessageBox *>(QApplication::activeModalWidget())) {
                confirmed=true; box->button(QMessageBox::Yes)->click();
            }
        });
        QSignalSpy changed(&window,&MainWindow::operationFinished);
        QTimer::singleShot(0,&writer,[&]{writer.submit({{"action","sheet_delete"}});});
        QTimer::singleShot(10000,&window,[]{
            if(auto *box=qobject_cast<QMessageBox *>(QApplication::activeModalWidget())) box->button(QMessageBox::No)->click();
        });
        QAction *remove=nullptr;
        for(auto *action:window.findChildren<QAction *>()) if(action->text()=="Delete sheet…") remove=action;
        QVERIFY(remove); remove->trigger();
        QVERIFY(refreshed); QVERIFY(confirmed);
        QTRY_VERIFY_WITH_TIMEOUT(window.findChild<QPlainTextEdit *>("operationIssues")->toPlainText().contains("Reviewed inputs changed"),5000);
        QCOMPARE(changed.count(),0);
        auto *tabs=window.findChild<QTabBar *>("sheets"); QCOMPARE(tabs->count(),2);
        QCOMPARE(tabs->tabText(0),QString("Other")); QCOMPARE(tabs->tabText(1),QString("Third"));
    }
    void unmaintainedFormulaShowsReferenceErrorUntilReentered() {
        QTemporaryDir temp; StartupOptions options; options.automaticWorkbook=false;
        MainWindow window(options); window.show(); QSignalSpy ready(&window,&MainWindow::serviceReady);
        QTRY_COMPARE_WITH_TIMEOUT(ready.count(),1,10000);
        QSignalSpy opened(&window,&MainWindow::workbookReady); window.openBook(temp.filePath("unmaintained.dhwb"),true);
        QTRY_COMPARE_WITH_TIMEOUT(opened.count(),1,10000);
        window.submit({{"action","edit"},{"cells",QJsonArray{QJsonObject{{"row",1},{"col",2},{"source","=A5 ` 1"}}}}});
        QTRY_COMPARE_WITH_TIMEOUT(opened.count(),2,10000);
        window.submit({{"action","rows_insert"},{"start",1},{"count",1}}); QTRY_COMPARE_WITH_TIMEOUT(opened.count(),3,10000);
        auto *model=window.model(); const auto index=model->index(1,1);
        QTRY_COMPARE_WITH_TIMEOUT(model->data(index).toString(),QString("#REF!"),5000);
        QCOMPARE(model->cell(index)["kind"].toString(),QString("unmaintained"));
        QCOMPARE(model->data(index,Qt::ToolTipRole).toString(),QString("References in this formula were not maintained through a structural edit. Re-enter the formula."));
        QCOMPARE(model->data(index,Qt::ForegroundRole).value<QBrush>().color(),QColor("#b3261e"));
        model->data(index,Qt::EditRole); QTRY_VERIFY_WITH_TIMEOUT(model->sourceReady(index),5000);
        QVERIFY(model->setData(index,"=A5 ` 1",Qt::EditRole)); QTRY_COMPARE_WITH_TIMEOUT(opened.count(),4,10000);
        QTRY_COMPARE_WITH_TIMEOUT(model->cell(index)["kind"].toString(),QString("formula"),5000);
    }
    void missingDraftSheetRequiresExplicitDiscardBeforeNewEdits() {
        QTemporaryDir temp; StartupOptions options; options.automaticWorkbook=false;
        MainWindow window(options); window.show(); QSignalSpy ready(&window,&MainWindow::serviceReady);
        QTRY_COMPARE_WITH_TIMEOUT(ready.count(),1,10000);
        const auto path=temp.filePath("missing-draft-sheet.dhwb");
        QSignalSpy opened(&window,&MainWindow::workbookReady); window.openBook(path,true);
        QTRY_COMPARE_WITH_TIMEOUT(opened.count(),1,10000);
        window.submit({{"action","sheet_add"},{"name","Other"}}); QTRY_COMPARE_WITH_TIMEOUT(opened.count(),2,10000);
        window.submit({{"action","edit"},{"cells",QJsonArray{QJsonObject{{"row",3},{"col",2},{"source","old"},{"kind","text"}}}}});
        QTRY_COMPARE_WITH_TIMEOUT(opened.count(),3,10000);
        auto *grid=window.findChild<QTableView *>("grid"); auto *source=window.findChild<QLineEdit *>("source");
        grid->setCurrentIndex(window.model()->index(2,1)); QTRY_COMPARE_WITH_TIMEOUT(source->text(),QString("old"),5000);
        source->setFocus(); source->selectAll(); QTest::keyClicks(source,"retained draft");
        const auto original=window.model()->sheet(); auto *process=window.findChild<QProcess *>("ownedService"); process->kill();
        QTRY_COMPARE_WITH_TIMEOUT(process->state(),QProcess::NotRunning,5000);
        {
            MainWindow writer(options); QSignalSpy writerReady(&writer,&MainWindow::serviceReady);
            QTRY_COMPARE_WITH_TIMEOUT(writerReady.count(),1,10000);
            QSignalSpy writerOpened(&writer,&MainWindow::workbookReady); writer.openBook(path);
            QTRY_COMPARE_WITH_TIMEOUT(writerOpened.count(),1,10000);
            writer.submit({{"action","sheet_delete"}}); QTRY_COMPARE_WITH_TIMEOUT(writerOpened.count(),2,10000);
        }
        window.findChild<QAction *>("restartServiceAction")->trigger();
        QTRY_COMPARE_WITH_TIMEOUT(opened.count(),4,10000);
        QVERIFY(window.model()->sheet()!=original);
        QCOMPARE(source->text(),QString("retained draft")); QVERIFY(source->isModified());
        QSignalSpy changed(&window,&MainWindow::operationFinished);
        source->returnPressed(); QCOMPARE(changed.count(),0);
        QVERIFY(window.findChild<QPlainTextEdit *>("operationIssues")->toPlainText().contains("The draft target changed"));
        answerNextDialog(QMessageBox::Cancel);
        window.submit({{"action","edit"},{"cells",QJsonArray{QJsonObject{{"row",1},{"col",1},{"source","new"}}}}});
        QCOMPARE(changed.count(),0); QCOMPARE(source->text(),QString("retained draft"));
        QTRY_VERIFY_WITH_TIMEOUT(source->isEnabled(),5000); QTest::keyClick(source,Qt::Key_Escape);
        window.submit({{"action","edit"},{"cells",QJsonArray{QJsonObject{{"row",1},{"col",1},{"source","new"}}}}});
        QTRY_COMPARE_WITH_TIMEOUT(opened.count(),5,10000);
        QTRY_COMPARE_WITH_TIMEOUT(window.model()->data(window.model()->index(0,0)).toString(),QString("new"),5000);
    }
private:
    bool m_originalDialogAttribute=false;
    static QJsonObject syncRequest(RestClient *client,const QString &path,const QJsonObject &body,bool post=true) {
        auto response=std::make_shared<QJsonObject>();auto done=std::make_shared<bool>(false);
        client->request(path,body,[response,done](QJsonObject result,QString error){*response=result;if(!error.isEmpty())(*response)["_error"]=error;*done=true;},post);
        for(int i=0;i<500 && !*done;++i)QTest::qWait(10);
        if(!*done)(*response)["_error"]="timed out";
        return *response;
    }
    static void observeOwnedServiceStartup(MainWindow &owner, QJsonObject &service) {
        auto *process=owner.findChild<QProcess *>("ownedService");
        connect(process,&QProcess::readyReadStandardOutput,process,[process,&service,bytes=QByteArray{}]() mutable {
            if(!service.isEmpty()) return;
            bytes+=process->peek(process->bytesAvailable());
            const auto newline=bytes.indexOf('\n');
            if(newline>=0) service=QJsonDocument::fromJson(bytes.left(newline)).object();
        });
    }
    static void answerNextDialog(QMessageBox::StandardButton button, QString *message=nullptr,QString *details=nullptr) {
        auto *timer=new QTimer(qApp); timer->setInterval(10);
        connect(timer,&QTimer::timeout,timer,[timer,button,message,details]{
            if(auto *box=qobject_cast<QMessageBox *>(QApplication::activeModalWidget())) {
                if(message) *message=box->text();
                if(details)*details=box->detailedText()+box->informativeText();
                if(auto *target=box->button(button)) { timer->stop(); target->click(); timer->deleteLater(); }
            }
        });
        timer->start(); QTimer::singleShot(5000,timer,&QObject::deleteLater);
    }
};

QTEST_MAIN(DesktopTests)
#include "test_desktop.moc"
