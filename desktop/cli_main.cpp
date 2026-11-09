// SPDX-License-Identifier: GPL-3.0-only
// A Mach-O launcher retains its embedded signature across copies/disk images.
#include <mach-o/dyld.h>
#include <unistd.h>
#include <cstdio>
#include <filesystem>
#include <string>
#include <vector>

int main(int argc,char **argv) {
    uint32_t size=0;
    _NSGetExecutablePath(nullptr,&size);
    std::vector<char> buffer(size);
    if(_NSGetExecutablePath(buffer.data(),&size)!=0) {
        std::fputs("Cannot locate the installed DataHorizon CLI.\n",stderr);return 127;
    }
    std::error_code error;
    const auto executable=std::filesystem::canonical(buffer.data(),error);
    if(error){std::fputs("Cannot resolve the installed DataHorizon CLI.\n",stderr);return 127;}
    const auto resources=executable.parent_path().parent_path()/"Resources";
    std::vector<std::string> arguments{(resources/"runtime/bin/python3").string(),"-I","-B",
        (resources/"service/datahorizon/launch_cli.py").string()};
    for(int index=1;index<argc;++index)arguments.emplace_back(argv[index]);
    std::vector<char *> values;
    for(auto &argument:arguments)values.push_back(argument.data());
    values.push_back(nullptr);
    execv(values.front(),values.data());
    std::perror("Cannot start the installed DataHorizon runtime");return 127;
}
