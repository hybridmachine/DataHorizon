// SPDX-License-Identifier: GPL-3.0-only
#pragma once

#include <QJsonObject>
#include <QNetworkAccessManager>
#include <QObject>
#include <QPointer>
#include <functional>

class QNetworkReply;
class QSaveFile;

class RestClient : public QObject {
    Q_OBJECT
public:
    using Callback = std::function<void(QJsonObject, QString)>;
    using Progress = std::function<void(qint64, qint64)>;
    // Transfers abort after this long without any byte moving either way.
    static constexpr int TransferInactivityMs = 60000;
    explicit RestClient(QObject *parent = nullptr);
    void configure(const QString &url, const QString &token);
    // timeoutMs bounds inactivity: a request whose answer can take minutes
    // (a large apply) passes a longer value than the 15-second default.
    void request(const QString &path, const QJsonObject &body, Callback callback, bool post = true, int timeoutMs = 15000);
    // Streams filePath to PUT /v1/uploads/{id} without reading it into memory.
    // The callback receives the service's receipt, already checked against the
    // bytes and SHA-256 that were read from the file. The returned reply can be
    // passed to cancel(); the callback then reports a cancelled transfer.
    QPointer<QNetworkReply> upload(const QString &id, const QString &filePath, Callback callback, Progress progress = {});
    // Streams GET /v1/exports/{exportId} into an open destination as it
    // arrives. The callback receives {bytes, sha256} of what was written,
    // after checking the count against Content-Length; the caller compares
    // them with the export job's result before committing the destination.
    QPointer<QNetworkReply> download(const QString &exportId, QSaveFile *destination, Callback callback, Progress progress = {});
    // Aborts an upload or download started by this client; its callback runs
    // with {"code":"CANCELLED"} and a cancelled error.
    static void cancel(QNetworkReply *reply);
    QString url() const { return m_url; }
    quint64 generation() const { return m_generation; }
    static bool validReceipt(const QJsonObject &receipt, const QString &operation, bool committedOnly = false);
signals:
    void configured();
private:
    QNetworkRequest prepared(const QString &path, int timeoutMs) const;
    static QString errorFrom(QNetworkReply *reply, const QByteArray &body, QJsonObject &object);
    QNetworkAccessManager m_network;
    QString m_url, m_token;
    quint64 m_generation = 0;
};
