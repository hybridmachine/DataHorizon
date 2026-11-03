// SPDX-License-Identifier: GPL-3.0-only
#pragma once

#include <QJsonObject>
#include <QNetworkAccessManager>
#include <QObject>
#include <functional>

class RestClient : public QObject {
    Q_OBJECT
public:
    using Callback = std::function<void(QJsonObject, QString)>;
    explicit RestClient(QObject *parent = nullptr);
    void configure(const QString &url, const QString &token);
    void request(const QString &path, const QJsonObject &body, Callback callback, bool post = true);
    QString url() const { return m_url; }
    static bool validReceipt(const QJsonObject &receipt, const QString &operation, bool committedOnly = false);
private:
    QNetworkAccessManager m_network;
    QString m_url, m_token;
    quint64 m_generation = 0;
};
