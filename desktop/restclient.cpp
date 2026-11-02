// SPDX-License-Identifier: GPL-3.0-only
#include "restclient.h"
#include <QJsonDocument>
#include <QNetworkReply>
#include <QNetworkProxy>
#include <QNetworkRequest>
#include <QTimer>
#include <cmath>

RestClient::RestClient(QObject *parent) : QObject(parent), m_network(this) {
    m_network.setProxy(QNetworkProxy::NoProxy);
    m_network.setRedirectPolicy(QNetworkRequest::ManualRedirectPolicy);
}

void RestClient::configure(const QString &url, const QString &token) {
    ++m_generation;
    m_url = url;
    m_token = token;
}

void RestClient::request(const QString &path, const QJsonObject &body, Callback callback, bool post) {
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
    QNetworkRequest request{QUrl(m_url + path)};
    request.setRawHeader("Authorization", "Bearer " + m_token.toUtf8());
    request.setHeader(QNetworkRequest::ContentTypeHeader, "application/json");
    request.setTransferTimeout(15000);
    auto *reply = post ? m_network.post(request, payload) : m_network.get(request);
    connect(reply, &QNetworkReply::finished, this, [this,reply,generation,callback = std::move(callback)] {
        if (generation!=m_generation) { reply->deleteLater(); return; }
        QJsonParseError parseError;
        const auto document = QJsonDocument::fromJson(reply->readAll(), &parseError);
        auto object = document.object();
        QString error;
        const auto status=reply->attribute(QNetworkRequest::HttpStatusCodeAttribute).toInt();
        if (status>=300 && status<400) {
            object={};
            error="Service redirects are not allowed";
        } else if (reply->error()!=QNetworkReply::NoError) {
            error=object.value("message").toString();
            if (error.isEmpty()) error=reply->errorString();
        }
        if (error.isEmpty() && (parseError.error != QJsonParseError::NoError || !document.isObject()))
            error = "Invalid JSON acknowledgement: expected an object";
        callback(object, error);
        reply->deleteLater();
    });
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
