// SPDX-License-Identifier: GPL-3.0-only
#include "restclient.h"
#include <QCryptographicHash>
#include <QFile>
#include <QJsonDocument>
#include <QNetworkReply>
#include <QNetworkProxy>
#include <QNetworkRequest>
#include <QSaveFile>
#include <QTimer>
#include <QUrl>
#include <cmath>

namespace {
// A file that hashes what is read from it, in order. Any seek other than a
// rewind to the start invalidates the digest, so a mismatch is reported
// rather than a wrong digest trusted.
class HashingFile : public QFile {
public:
    using QFile::QFile;
    bool seek(qint64 position) override {
        if(position==0){m_hash.reset();m_hashed=0;m_valid=true;}
        else if(position!=m_hashed)m_valid=false;
        return QFile::seek(position);
    }
    bool complete() const { return m_valid && m_hashed==size(); }
    QString digest() const { return QString::fromLatin1(m_hash.result().toHex()); }
protected:
    qint64 readData(char *data, qint64 maximum) override {
        const auto read=QFile::readData(data,maximum);
        if(read>0){m_hash.addData(QByteArrayView(data,read));m_hashed+=read;}
        return read;
    }
private:
    QCryptographicHash m_hash{QCryptographicHash::Sha256};
    qint64 m_hashed=0;
    bool m_valid=true;
};

QJsonObject cancelledError() {
    return {{"code","CANCELLED"},{"message","Transfer cancelled"},{"outcome","rejected"}};
}
}

RestClient::RestClient(QObject *parent) : QObject(parent), m_network(this) {
    m_network.setProxy(QNetworkProxy::NoProxy);
    m_network.setRedirectPolicy(QNetworkRequest::ManualRedirectPolicy);
}

void RestClient::configure(const QString &url, const QString &token) {
    ++m_generation;
    m_url = url;
    m_token = token;
    emit configured();
}

QNetworkRequest RestClient::prepared(const QString &path, int timeoutMs) const {
    QNetworkRequest request{QUrl(m_url + path)};
    request.setRawHeader("Authorization", "Bearer " + m_token.toUtf8());
    request.setTransferTimeout(timeoutMs);
    return request;
}

QString RestClient::errorFrom(QNetworkReply *reply, const QByteArray &body, QJsonObject &object) {
    QJsonParseError parseError;
    const auto document=QJsonDocument::fromJson(body,&parseError);
    object=document.object();
    const auto status=reply->attribute(QNetworkRequest::HttpStatusCodeAttribute).toInt();
    if(status>=300 && status<400){object={};return "Service redirects are not allowed";}
    if(reply->error()!=QNetworkReply::NoError){
        const auto message=object.value("message").toString();
        return message.isEmpty()?reply->errorString():message;
    }
    if(parseError.error!=QJsonParseError::NoError || !document.isObject())return "Invalid JSON acknowledgement: expected an object";
    return {};
}

void RestClient::request(const QString &path, const QJsonObject &body, Callback callback, bool post, int timeoutMs) {
    const auto generation=m_generation;
    const auto payload=post ? QJsonDocument(body).toJson(QJsonDocument::Compact) : QByteArray();
    if (payload.size()>4*1024*1024) {
        QJsonObject rejected{{"code","REQUEST_LIMIT"},{"message","Request exceeds the 4 MiB limit. Reduce the selection or input size."},{"outcome","rejected"}};
        if (body.contains("operation_id")) rejected["operation_id"]=body["operation_id"];
        QTimer::singleShot(0,this,[this,generation,rejected,callback=std::move(callback)] {
            if (generation==m_generation) callback(rejected,rejected["message"].toString());
        });
        return;
    }
    auto request=prepared(path,timeoutMs);
    request.setHeader(QNetworkRequest::ContentTypeHeader, "application/json");
    auto *reply = post ? m_network.post(request, payload) : m_network.get(request);
    connect(reply, &QNetworkReply::finished, this, [this,reply,generation,callback = std::move(callback)] {
        if (generation!=m_generation) { reply->deleteLater(); return; }
        QJsonObject object;
        const auto error=errorFrom(reply,reply->readAll(),object);
        callback(object, error);
        reply->deleteLater();
    });
}

void RestClient::cancel(QNetworkReply *reply) {
    if(!reply || reply->isFinished())return;
    reply->setProperty("datahorizonCancelled",true);
    reply->abort();
}

QPointer<QNetworkReply> RestClient::upload(const QString &id, const QString &filePath, Callback callback, Progress progress) {
    auto *file=new HashingFile(filePath,this);
    if(!file->open(QIODevice::ReadOnly) || file->size()<=0){
        const QJsonObject failed{{"code","INVALID_UPLOAD"},{"message",file->isOpen()?"The file is empty; nothing was uploaded":"The file could not be read: "+file->errorString()},{"outcome","rejected"}};
        file->deleteLater();
        QTimer::singleShot(0,this,[failed,callback=std::move(callback)]{callback(failed,failed["message"].toString());});
        return {};
    }
    const auto size=file->size();
    auto request=prepared("/v1/uploads/"+QString::fromLatin1(QUrl::toPercentEncoding(id)),TransferInactivityMs);
    request.setHeader(QNetworkRequest::ContentTypeHeader,"application/octet-stream");
    request.setHeader(QNetworkRequest::ContentLengthHeader,size);
    const auto generation=m_generation;
    auto *reply=m_network.put(request,file);
    file->setParent(reply);
    if(progress)connect(reply,&QNetworkReply::uploadProgress,this,[progress,size](qint64 sent,qint64){progress(sent,size);});
    connect(reply,&QNetworkReply::finished,this,[this,reply,file,id,size,generation,callback=std::move(callback)]{
        reply->deleteLater();
        if(reply->property("datahorizonCancelled").toBool()){const auto failed=cancelledError();callback(failed,failed["message"].toString());return;}
        if(generation!=m_generation){callback({{"code","SERVICE_CHANGED"},{"outcome","rejected"}},"The service session changed during the upload");return;}
        QJsonObject receipt;
        auto error=errorFrom(reply,reply->readAll(),receipt);
        if(error.isEmpty() && (receipt["upload_id"].toString()!=id || receipt["bytes"].toDouble()!=double(size)
                               || !file->complete() || receipt["sha256"].toString()!=file->digest())){
            receipt={{"code","INTEGRITY"},{"upload_id",id},{"outcome","rejected"}};
            error="The service received different bytes than were read from the file";
        }
        callback(receipt,error);
    });
    return reply;
}

QPointer<QNetworkReply> RestClient::download(const QString &exportId, QSaveFile *destination, Callback callback, Progress progress) {
    auto request=prepared("/v1/exports/"+QString::fromLatin1(QUrl::toPercentEncoding(exportId)),TransferInactivityMs);
    const auto generation=m_generation;
    auto *reply=m_network.get(request);
    struct State { QCryptographicHash hash{QCryptographicHash::Sha256}; qint64 written=0; QByteArray errorBody; QString writeError; };
    auto state=std::make_shared<State>();
    connect(reply,&QNetworkReply::readyRead,this,[reply,destination,state,progress]{
        const auto status=reply->attribute(QNetworkRequest::HttpStatusCodeAttribute).toInt();
        const auto chunk=reply->readAll();
        if(status!=200){
            // An error body is JSON and small; keep a bounded copy for decoding.
            if(state->errorBody.size()<1024*1024)state->errorBody+=chunk.left(1024*1024-state->errorBody.size());
            return;
        }
        if(!state->writeError.isEmpty())return;
        if(destination->write(chunk)!=chunk.size()){
            state->writeError="Export destination failed: "+destination->errorString();
            reply->abort();return;
        }
        state->hash.addData(chunk);state->written+=chunk.size();
        if(progress)progress(state->written,reply->header(QNetworkRequest::ContentLengthHeader).toLongLong());
    });
    connect(reply,&QNetworkReply::finished,this,[this,reply,state,exportId,generation,callback=std::move(callback)]{
        reply->deleteLater();
        if(reply->property("datahorizonCancelled").toBool()){const auto failed=cancelledError();callback(failed,failed["message"].toString());return;}
        if(!state->writeError.isEmpty()){callback({{"code","DESTINATION_FAILED"},{"outcome","rejected"}},state->writeError);return;}
        if(generation!=m_generation){callback({{"code","SERVICE_CHANGED"},{"outcome","rejected"}},"The service session changed during the download");return;}
        const auto status=reply->attribute(QNetworkRequest::HttpStatusCodeAttribute).toInt();
        if(status!=200 || reply->error()!=QNetworkReply::NoError){
            QJsonObject object;
            auto error=errorFrom(reply,state->errorBody+reply->readAll(),object);
            if(error.isEmpty())error="Download failed with HTTP status "+QString::number(status);
            callback(object,error);return;
        }
        const auto length=reply->header(QNetworkRequest::ContentLengthHeader);
        if(!length.isValid() || length.toLongLong()!=state->written){
            callback({{"code","INTEGRITY"},{"export_id",exportId},{"outcome","rejected"}},"The download ended before its Content-Length");return;
        }
        callback({{"export_id",exportId},{"bytes",double(state->written)},{"sha256",QString::fromLatin1(state->hash.result().toHex())}},{});
    });
    return reply;
}

bool RestClient::validReceipt(const QJsonObject &receipt, const QString &operation, bool committedOnly) {
    if (receipt["operation_id"].toString() != operation || operation.isEmpty()) return false;
    if (receipt["outcome"] == "committed") {
        const auto count = receipt["changed_cells"];
        return receipt["state"].isString() && !receipt["state"].toString().isEmpty() && count.isDouble()
            && count.toDouble() >= 0 && std::isfinite(count.toDouble()) && std::floor(count.toDouble()) == count.toDouble();
    }
    return !committedOnly && (receipt["outcome"] == "rolled_back" || receipt["outcome"] == "rejected")
        && receipt["code"].isString() && !receipt["code"].toString().isEmpty() && receipt["message"].isString();
}
