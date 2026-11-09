// SPDX-License-Identifier: GPL-3.0-only
#include "exportwriter.h"
#include <QCoreApplication>
#include <QCryptographicHash>
#include <QFile>
#include <QJsonDocument>
#include <cstdio>

int main(int argc,char **argv) {
    QCoreApplication app(argc,argv);
    if(argc!=3)return 2;
    const QString native=QString::fromLocal8Bit(argv[1]),destination=QString::fromLocal8Bit(argv[2]);
    auto digest=[&]{QFile file(native);if(!file.open(QIODevice::ReadOnly))return QByteArray();QCryptographicHash hash(QCryptographicHash::Sha256);hash.addData(&file);return hash.result();};
    const auto before=digest();if(before.isEmpty())return 2;
    // The filesystem writer accepts bounded opaque export bytes. Interchange
    // consumers are verified separately; this probe exercises actual OS failures.
    const QJsonObject snapshot{{"file_base64",QString::fromLatin1(QByteArray(2*1024*1024,'x').toBase64())}};
    QString error;const bool saved=ExportWriter::save(destination,native,snapshot,error);
    const bool retained=before==digest();
    const QJsonObject report{{"export_succeeded",saved},{"native_hash_preserved",retained},{"error",error}};
    const auto output=QJsonDocument(report).toJson(QJsonDocument::Compact);std::puts(output.constData());
    return !saved && retained && !error.isEmpty()?0:1;
}
