cmake_minimum_required(VERSION 3.24)
project(DataHorizon VERSION 0.1.0 LANGUAGES CXX)

set(CMAKE_CXX_STANDARD 17)
set(CMAKE_CXX_STANDARD_REQUIRED ON)
set(CMAKE_AUTOMOC ON)
if(MSVC)
    add_compile_options(/W4 /utf-8)
else()
    add_compile_options(-Wall -Wextra -Wpedantic)
endif()
find_package(Qt6 6.8 REQUIRED COMPONENTS Widgets Network Test)
find_package(Python3 3.11 REQUIRED COMPONENTS Interpreter)
include(CTest)

add_library(datahorizon_desktop STATIC
    desktop/restclient.cpp desktop/restclient.h
    desktop/sheetmodel.cpp desktop/sheetmodel.h
    desktop/mainwindow.cpp desktop/mainwindow.h)
target_link_libraries(datahorizon_desktop PUBLIC Qt6::Widgets Qt6::Network)
target_include_directories(datahorizon_desktop PUBLIC desktop)
target_compile_definitions(datahorizon_desktop PRIVATE
    DATAHORIZON_SERVICE_DIR="${CMAKE_CURRENT_SOURCE_DIR}/service"
    DATAHORIZON_PYTHON="${Python3_EXECUTABLE}")

qt_add_executable(DataHorizon WIN32 MACOSX_BUNDLE desktop/main.cpp)
target_link_libraries(DataHorizon PRIVATE datahorizon_desktop)
qt_add_resources(DataHorizon application_icon
    PREFIX "/icons"
    BASE desktop/icons
    FILES desktop/icons/datahorizon.png)
set_target_properties(DataHorizon PROPERTIES
    MACOSX_BUNDLE_GUI_IDENTIFIER org.datahorizon.preview
    MACOSX_BUNDLE_BUNDLE_NAME DataHorizon)
if(APPLE)
    target_sources(DataHorizon PRIVATE desktop/icons/datahorizon.icns)
    set_source_files_properties(desktop/icons/datahorizon.icns PROPERTIES
        MACOSX_PACKAGE_LOCATION Resources)
    set_target_properties(DataHorizon PROPERTIES
        MACOSX_BUNDLE_ICON_FILE datahorizon.icns)
elseif(WIN32)
    enable_language(RC)
    target_sources(DataHorizon PRIVATE desktop/icons/datahorizon.rc)
    target_include_directories(DataHorizon PRIVATE desktop/icons)
endif()

if(BUILD_TESTING)
    add_executable(desktop_benchmark tests/benchmark_desktop.cpp)
    target_link_libraries(desktop_benchmark PRIVATE datahorizon_desktop Qt6::Test)
    add_executable(desktop_tests tests/test_desktop.cpp)
    target_link_libraries(desktop_tests PRIVATE datahorizon_desktop Qt6::Test)
    target_compile_definitions(desktop_tests PRIVATE DATAHORIZON_FIXTURES="${CMAKE_CURRENT_SOURCE_DIR}/tests/fixtures")
    add_test(NAME desktop COMMAND desktop_tests)
    set_tests_properties(desktop PROPERTIES ENVIRONMENT "QT_QPA_PLATFORM=offscreen" TIMEOUT 90)
    add_test(NAME service COMMAND "${Python3_EXECUTABLE}" -m unittest discover -s tests -v)
    set_tests_properties(service PROPERTIES WORKING_DIRECTORY "${CMAKE_CURRENT_SOURCE_DIR}"
        ENVIRONMENT "PYTHONPATH=${CMAKE_CURRENT_SOURCE_DIR}/service" TIMEOUT 45)
    if(WIN32)
        # Allow the complete multi-process suites to finish on hosted Windows.
        set_tests_properties(desktop PROPERTIES TIMEOUT 180)
        set_tests_properties(service PROPERTIES TIMEOUT 120)
    endif()
endif()

install(TARGETS DataHorizon BUNDLE DESTINATION . RUNTIME DESTINATION bin)
if(APPLE)
    install(DIRECTORY service/datahorizon DESTINATION DataHorizon.app/Contents/Resources/service
        PATTERN __pycache__ EXCLUDE)
    install(FILES desktop/COPYING DESTINATION DataHorizon.app/Contents/Resources/licenses RENAME GUI-GPLv3.txt)
else()
    install(DIRECTORY service/datahorizon DESTINATION share/datahorizon/service
        PATTERN __pycache__ EXCLUDE)
    install(FILES desktop/COPYING DESTINATION share/datahorizon/licenses RENAME GUI-GPLv3.txt)
endif()
if(UNIX AND NOT APPLE)
    install(FILES desktop/datahorizon.desktop DESTINATION share/applications)
    foreach(icon_size 16 24 32 48 64 128 256 512)
        install(FILES desktop/icons/png/datahorizon-${icon_size}.png
            DESTINATION share/icons/hicolor/${icon_size}x${icon_size}/apps
            RENAME datahorizon.png)
    endforeach()
    install(FILES desktop/icons/datahorizon.png
        DESTINATION share/icons/hicolor/1024x1024/apps)
endif()
qt_generate_deploy_app_script(TARGET DataHorizon OUTPUT_SCRIPT deploy_script
    NO_UNSUPPORTED_PLATFORM_ERROR)
install(SCRIPT "${deploy_script}")
