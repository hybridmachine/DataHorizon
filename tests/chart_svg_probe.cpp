// SPDX-License-Identifier: GPL-3.0-only
//
// Charts stage 1 experiment: loads renderer SVG through Qt SVG, rasterises it,
// times loading and painting, and measures real text widths. Not installed and
// not part of the desktop. See docs/design/charts-stage-1-plan.md.

#include <QCommandLineParser>
#include <QDir>
#include <QElapsedTimer>
#include <QFile>
#include <QFileInfo>
#include <QFont>
#include <QFontInfo>
#include <QFontMetricsF>
#include <QGuiApplication>
#include <QImage>
#include <QJsonArray>
#include <QJsonDocument>
#include <QJsonObject>
#include <QPainter>
#include <QSvgRenderer>
#include <QTextStream>

#include <algorithm>
#include <vector>

namespace {

const int kRuns = 20;

struct Shot {
    bool valid = false;
    double loadMs = 0;
    double paintMs = 0;
    QImage image;
};

double median(std::vector<double> samples)
{
    std::sort(samples.begin(), samples.end());
    const size_t n = samples.size();
    return n % 2 ? samples[n / 2] : (samples[n / 2 - 1] + samples[n / 2]) / 2;
}

Shot shoot(const QByteArray &svg, int runs)
{
    Shot shot;
    std::vector<double> loads, paints;
    QElapsedTimer timer;
    for (int i = 0; i < runs; ++i) {
        timer.start();
        QSvgRenderer renderer(svg);
        loads.push_back(timer.nsecsElapsed() / 1e6);
        shot.valid = renderer.isValid();
        if (!shot.valid)
            return shot;
        QSize size = renderer.defaultSize();
        if (size.isEmpty())
            size = QSize(1, 1);
        QImage image(size, QImage::Format_ARGB32_Premultiplied);
        image.fill(Qt::white);
        timer.start();
        {
            QPainter painter(&image);
            painter.setRenderHint(QPainter::Antialiasing);
            renderer.render(&painter);
        }
        paints.push_back(timer.nsecsElapsed() / 1e6);
        shot.image = image;
    }
    shot.loadMs = median(loads);
    shot.paintMs = median(paints);
    return shot;
}

// The renderer writes font-family="sans-serif"; measure with the same request
// so the family Qt resolves it to is the one Qt SVG draws with.
QFont fontFor(const QString &family, double pixels)
{
    QFont font(family);
    font.setPixelSize(qMax(1, qRound(pixels)));
    return font;
}

double advance(const QString &family, double pixels, const QString &text)
{
    const QFont font = fontFor(family, pixels);
    return QFontMetricsF(font).horizontalAdvance(text) * pixels / font.pixelSize();
}

bool readJson(const QString &path, QJsonDocument *document)
{
    QFile file(path);
    if (!file.open(QIODevice::ReadOnly))
        return false;
    QJsonParseError error;
    *document = QJsonDocument::fromJson(file.readAll(), &error);
    return error.error == QJsonParseError::NoError;
}

bool writeJson(const QString &path, const QJsonObject &object)
{
    QFile file(path);
    if (!file.open(QIODevice::WriteOnly | QIODevice::Truncate))
        return false;
    const QByteArray bytes = QJsonDocument(object).toJson(QJsonDocument::Indented);
    return file.write(bytes) == bytes.size();
}

int fail(const QString &message)
{
    QTextStream(stderr) << "chart_svg_probe: " << message << "\n";
    return 2;
}

int selfTest()
{
    const QByteArray half =
        "<svg xmlns=\"http://www.w3.org/2000/svg\" version=\"1.2\" baseProfile=\"tiny\" "
        "width=\"100\" height=\"100\" viewBox=\"0 0 100 100\">"
        "<rect x=\"0\" y=\"0\" width=\"50\" height=\"100\" fill=\"#ff0000\"/></svg>";
    const Shot good = shoot(half, 1);
    if (!good.valid || good.image.size() != QSize(100, 100))
        return 1;
    if (good.image.pixelColor(25, 50) != QColor(255, 0, 0) || good.image.pixelColor(75, 50) != QColor(255, 255, 255))
        return 1;
    if (shoot("<svg xmlns=\"http://www.w3.org/2000/svg\"><rect", 1).valid)
        return 1;
    return 0;
}

int emitMetrics(const QString &family, const QString &path)
{
    const double pixels = 1000;
    QJsonObject advances;
    auto add = [&](int from, int to) {
        for (int code = from; code <= to; ++code) {
            const QString text{QChar(code)};
            advances.insert(text, advance(family, pixels, text) / pixels);
        }
    };
    add(0x20, 0x7e);
    add(0xa0, 0xff);
    QJsonObject table{{"family", family},
                      {"resolved_family", QFontInfo(fontFor(family, pixels)).family()},
                      {"advances", advances}};
    return writeJson(path, table) ? 0 : fail("cannot write " + path);
}

int measure(const QString &svgDir, const QString &outDir, const QString &labelsPath, const QString &reportPath,
            const QString &substitute)
{
    QDir input(svgDir);
    if (!input.exists())
        return fail("no such directory " + svgDir);
    if (!QDir().mkpath(outDir))
        return fail("cannot create " + outDir);
    QJsonDocument labelsDocument;
    if (!readJson(labelsPath, &labelsDocument) || !labelsDocument.isArray())
        return fail("cannot read labels " + labelsPath);

    QJsonArray charts;
    const QStringList files = input.entryList({"*.svg"}, QDir::Files, QDir::Name);
    for (const QString &name : files) {
        QFile file(input.filePath(name));
        if (!file.open(QIODevice::ReadOnly))
            return fail("cannot read " + file.fileName());
        const Shot shot = shoot(file.readAll(), kRuns);
        QString png;
        if (shot.valid) {
            png = QDir(outDir).filePath(QFileInfo(name).completeBaseName() + ".png");
            if (!shot.image.save(png))
                return fail("cannot write " + png);
        }
        charts.append(QJsonObject{{"file", name}, {"valid", shot.valid}, {"load_ms", shot.loadMs},
                                  {"paint_ms", shot.paintMs}, {"png", png}});
    }

    const QString requested = QStringLiteral("sans-serif");
    QJsonArray labels;
    for (const QJsonValue &value : labelsDocument.array()) {
        const QJsonObject label = value.toObject();
        const QString text = label.value("text").toString();
        const double pixels = label.value("size_px").toDouble();
        QJsonObject row{{"text", text}, {"size_px", pixels}, {"estimate", label.value("estimate")},
                        {"default_px", advance(requested, pixels, text)}};
        row.insert("substituted_px", substitute.isEmpty() ? QJsonValue() : QJsonValue(advance(substitute, pixels, text)));
        labels.append(row);
    }

    QJsonObject report{{"qt", QString::fromLatin1(qVersion())},
                       {"platform", QGuiApplication::platformName()},
                       {"default_font", QFontInfo(fontFor(requested, 12)).family()},
                       {"charts", charts},
                       {"labels", labels}};
    report.insert("substituted_font",
                  substitute.isEmpty() ? QJsonValue() : QJsonValue(QFontInfo(fontFor(substitute, 12)).family()));
    return writeJson(reportPath, report) ? 0 : fail("cannot write " + reportPath);
}

} // namespace

int main(int argc, char **argv)
{
    QGuiApplication app(argc, argv);
    QCommandLineParser parser;
    parser.addOptions({{"self-test", "Check Qt SVG rendering and exit."},
                       {"svg-dir", "Directory of SVG files to load.", "dir"},
                       {"out", "Directory for PNG output.", "dir"},
                       {"labels", "JSON list of labels to measure.", "file"},
                       {"report", "JSON report to write.", "file"},
                       {"font", "A second font family to measure with.", "family"},
                       {"emit-metrics", "Write a per-character advance table for a family.", "family"}});
    parser.addPositionalArgument("table", "Output file for --emit-metrics.");
    if (!parser.parse(app.arguments()))
        return fail(parser.errorText());

    if (parser.isSet("self-test"))
        return selfTest();
    if (parser.isSet("emit-metrics")) {
        if (parser.positionalArguments().size() != 1)
            return fail("--emit-metrics needs a family and one output file");
        return emitMetrics(parser.value("emit-metrics"), parser.positionalArguments().first());
    }
    for (const char *required : {"svg-dir", "out", "labels", "report"}) {
        if (!parser.isSet(required))
            return fail(QStringLiteral("missing --%1").arg(required));
    }
    return measure(parser.value("svg-dir"), parser.value("out"), parser.value("labels"), parser.value("report"),
                   parser.value("font"));
}
