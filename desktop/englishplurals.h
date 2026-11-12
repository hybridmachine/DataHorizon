// SPDX-License-Identifier: GPL-3.0-only
#pragma once
#include <QCoreApplication>
#include <QTranslator>

// English numerus forms for tr("%n item(s)", nullptr, n) without a compiled
// translation: "(s)" is dropped for one and becomes "s" otherwise. Qt then
// replaces %n. A translator installed later (a real .qm) takes precedence.
class EnglishPlurals : public QTranslator {
public:
    using QTranslator::QTranslator;
    bool isEmpty() const override { return false; }
    QString translate(const char *, const char *sourceText, const char *, int n) const override {
        if(n<0 || !sourceText) return {};
        const auto source=QString::fromUtf8(sourceText);
        if(!source.contains(QStringLiteral("%n")) || !source.contains(QStringLiteral("(s)"))) return {};
        return QString(source).replace(QStringLiteral("(s)"),n==1?QString():QStringLiteral("s"));
    }
};

inline void installEnglishPlurals(QCoreApplication *app) {
    static EnglishPlurals *plurals=nullptr;
    if(plurals) return;
    plurals=new EnglishPlurals(app);
    QCoreApplication::installTranslator(plurals);
}
