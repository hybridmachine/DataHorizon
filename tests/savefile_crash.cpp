// SPDX-License-Identifier: GPL-3.0-only
#include <QCoreApplication>
#include <QSaveFile>
#include <cstdlib>

int main(int argc, char **argv) {
    QCoreApplication app(argc,argv);
    const auto args=app.arguments();
    if(args.size()!=3) return 1;
    QSaveFile file(args[1]); file.setDirectWriteFallback(false);
    if(!file.open(QIODevice::WriteOnly) || file.write("replacement")!=11 || !file.flush()) return 2;
    if(args[2]=="after" && !file.commit()) return 3;
    std::_Exit(73); // Intentionally bypass destructors before/after atomic replacement.
}
