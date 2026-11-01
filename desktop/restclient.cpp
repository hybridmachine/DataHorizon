// SPDX-License-Identifier: GPL-3.0-only
#include "restclient.h"
#include <QJsonDocument>
#include <QNetworkReply>
#include <QNetworkRequest>
#include <QTimer>

RestClient::RestClient(QObject *parent) : QObject(parent), m_network(this) {}

void RestClient::configure(const QString &url, const QString &token) {
    m_url = url;
    m_token = token;
}

void RestClient::request(const QString &path, const QJsonObject &body, Callback callback, bool post) {
    QNetworkRequest request{QUrl(m_url + path)};
    request.setRawHeader("Authorization", "Bearer " + m_token.toUtf8());
    request.setHeader(QNetworkRequest::ContentTypeHeader, "application/json");
    request.setTransferTimeout(15000);
    auto *reply = post ? m_network.post(request, QJsonDocument(body).toJson(QJsonDocument::Compact)) : m_network.get(request);
    connect(reply, &QNetworkReply::finished, this, [reply, callback = std::move(callback)] {
        auto object = QJsonDocument::fromJson(reply->readAll()).object();
        const auto error = reply->error() == QNetworkReply::NoError ? QString{} :
            object.value("message").toString(reply->errorString());
        callback(object, error);
        reply->deleteLater();
    });
}
