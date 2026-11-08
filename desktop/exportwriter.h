// SPDX-License-Identifier: GPL-3.0-only
#pragma once
#include <QFileInfo>
#include <QJsonObject>
#include <QSaveFile>
#include <filesystem>

namespace ExportWriter {
inline bool sameFile(const QString &left, const QString &right) {
    std::error_code error;
    return std::filesystem::equivalent(QFileInfo(left).filesystemFilePath(),
                                      QFileInfo(right).filesystemFilePath(), error);
}

// The sink parameter permits deterministic write/commit failure verification.
template<class Sink>
bool write(const QString &path, const QString &native, const QByteArray &bytes,
           Sink &file, QString &error) {
    if (sameFile(path,native)) {
        error="Export destination resolves to the native workbook. Choose another file.";
        return false;
    }
    if (bytes.isEmpty()) { error="Empty or invalid spreadsheet export response; destination retained."; return false; }
    if (!file.open(QIODevice::WriteOnly) || file.write(bytes)!=bytes.size() || !file.commit()) {
        error="Spreadsheet destination failed: "+file.errorString();
        return false;
    }
    return true;
}

inline bool save(const QString &path, const QString &native, const QJsonObject &snapshot, QString &error) {
    const auto bytes=QByteArray::fromBase64(snapshot["file_base64"].toString().toLatin1(),
                                         QByteArray::AbortOnBase64DecodingErrors);
    QSaveFile file(path);
    return write(path,native,bytes,file,error);
}
}
