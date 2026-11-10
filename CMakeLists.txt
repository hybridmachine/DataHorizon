cmake_minimum_required(VERSION 3.24)
if(CMAKE_HOST_APPLE AND NOT DEFINED CMAKE_OSX_DEPLOYMENT_TARGET)
    set(CMAKE_OSX_DEPLOYMENT_TARGET "15.0" CACHE STRING "Minimum macOS version for this preview tier")
endif()
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
set(DATAHORIZON_RUNTIME_ROOT "" CACHE PATH "Verified, staged relocatable Python runtime (macOS ARM64)")
if(DATAHORIZON_RUNTIME_ROOT)
    if(NOT APPLE OR NOT EXISTS "${DATAHORIZON_RUNTIME_ROOT}/DATAHORIZON-RUNTIME.json"
            OR NOT EXISTS "${DATAHORIZON_RUNTIME_ROOT}/bin/python3")
        message(FATAL_ERROR "Use tools/stage_runtime.py to stage the supported macOS runtime first")
    endif()
endif()
set(DATAHORIZON_PYTHON_EXECUTABLE "${Python3_EXECUTABLE}")
if(WIN32)
    # An explicitly supplied native path can contain C++ escape sequences
    # (notably \x in the x64 directory). Qt accepts forward-slash paths.
    string(REPLACE "\\" "/" DATAHORIZON_PYTHON_EXECUTABLE "${Python3_EXECUTABLE}")
endif()
set(DATAHORIZON_ABSOLUTE_INSTALL_PREFIX false)
if(IS_ABSOLUTE "${CMAKE_INSTALL_PREFIX}")
    set(DATAHORIZON_ABSOLUTE_INSTALL_PREFIX true)
endif()
configure_file(cmake/toolchain.json.in build-toolchain.json @ONLY)
execute_process(COMMAND git rev-parse HEAD WORKING_DIRECTORY "${CMAKE_CURRENT_SOURCE_DIR}"
    OUTPUT_VARIABLE DATAHORIZON_SOURCE_COMMIT OUTPUT_STRIP_TRAILING_WHITESPACE ERROR_QUIET)
if(NOT DATAHORIZON_SOURCE_COMMIT)
    set(DATAHORIZON_SOURCE_COMMIT "unversioned")
endif()
configure_file(cmake/source.json.in source.json @ONLY)
include(CTest)

add_library(datahorizon_desktop STATIC
    desktop/restclient.cpp desktop/restclient.h desktop/servicepaths.cpp desktop/servicepaths.h
    desktop/sheetmodel.cpp desktop/sheetmodel.h desktop/viewportcalculations.h
    desktop/mainwindow.cpp desktop/mainwindow.h desktop/frozentoprow.h)
target_sources(datahorizon_desktop PRIVATE desktop/relationalworkbench.cpp desktop/relationalworkbench.h)
target_sources(datahorizon_desktop PRIVATE desktop/tablerecordeditor.cpp desktop/recordinputdialog.h)
target_sources(datahorizon_desktop PRIVATE desktop/tablerelationships.cpp)
target_link_libraries(datahorizon_desktop PUBLIC Qt6::Widgets Qt6::Network)
target_include_directories(datahorizon_desktop PUBLIC desktop)
target_compile_definitions(datahorizon_desktop PRIVATE
    DATAHORIZON_BUILD_DIR="${CMAKE_CURRENT_BINARY_DIR}"
    DATAHORIZON_BUILD_CONFIG="$<CONFIG>"
    DATAHORIZON_SERVICE_DIR="${CMAKE_CURRENT_SOURCE_DIR}/service"
    DATAHORIZON_PYTHON="${DATAHORIZON_PYTHON_EXECUTABLE}")

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
    add_custom_command(TARGET DataHorizon POST_BUILD
        COMMAND "${CMAKE_COMMAND}" "-DAPP_BUNDLE=$<TARGET_BUNDLE_DIR:DataHorizon>"
            -P "${CMAKE_CURRENT_SOURCE_DIR}/cmake/SignDevelopmentBundle.cmake"
        VERBATIM)
    # Resource-only updates can avoid relinking. Verify the complete envelope
    # on ordinary builds too, rather than only launching the bare executable.
    add_custom_target(development_bundle_signature ALL
        COMMAND "${CMAKE_COMMAND}" "-DAPP_BUNDLE=$<TARGET_BUNDLE_DIR:DataHorizon>"
            -P "${CMAKE_CURRENT_SOURCE_DIR}/cmake/SignDevelopmentBundle.cmake"
        DEPENDS DataHorizon VERBATIM)
elseif(WIN32)
    enable_language(RC)
    target_sources(DataHorizon PRIVATE desktop/icons/datahorizon.rc)
    target_include_directories(DataHorizon PRIVATE desktop/icons)
endif()

if(BUILD_TESTING)
    if(APPLE)
        add_test(NAME development_bundle_signature COMMAND "${Python3_EXECUTABLE}"
            "${CMAKE_CURRENT_SOURCE_DIR}/tests/verify_development_bundle.py"
            --bundle "$<TARGET_BUNDLE_DIR:DataHorizon>"
            --script "${CMAKE_CURRENT_SOURCE_DIR}/cmake/SignDevelopmentBundle.cmake")
        set_tests_properties(development_bundle_signature PROPERTIES TIMEOUT 30)
    endif()
    add_executable(desktop_benchmark tests/benchmark_desktop.cpp)
    target_link_libraries(desktop_benchmark PRIVATE datahorizon_desktop Qt6::Test)
    if(WIN32)
        target_link_libraries(desktop_benchmark PRIVATE psapi)
    endif()
    add_executable(native_acceptance tests/native_acceptance.cpp)
    target_link_libraries(native_acceptance PRIVATE datahorizon_desktop Qt6::Test)
    add_executable(savefile_crash tests/savefile_crash.cpp)
    target_link_libraries(savefile_crash PRIVATE Qt6::Core)
    add_executable(export_failure_probe tests/export_failure_probe.cpp)
    target_include_directories(export_failure_probe PRIVATE desktop)
    target_link_libraries(export_failure_probe PRIVATE Qt6::Core)
    add_executable(desktop_tests tests/test_desktop.cpp)
    add_dependencies(desktop_tests savefile_crash)
    target_link_libraries(desktop_tests PRIVATE datahorizon_desktop Qt6::Test)
    target_compile_definitions(desktop_tests PRIVATE DATAHORIZON_FIXTURES="${CMAKE_CURRENT_SOURCE_DIR}/tests/fixtures")
    # Windows GUI-subsystem runners can lose stdout. Keep a Qt text log even
    # when CTest cannot capture the process's console output.
    add_test(NAME desktop COMMAND desktop_tests -o desktop-tests.txt,txt)
    # These cover the complete process suites, including spawned services and
    # modal-dialog automation. Individual operation deadlines remain unchanged.
    set_tests_properties(desktop PROPERTIES ENVIRONMENT "QT_QPA_PLATFORM=offscreen;DATAHORIZON_VIEW_STATE=${CMAKE_CURRENT_BINARY_DIR}/test-views.ini" TIMEOUT 600)
    add_test(NAME service COMMAND "${Python3_EXECUTABLE}" -m unittest discover -s tests -v)
    set_tests_properties(service PROPERTIES WORKING_DIRECTORY "${CMAKE_CURRENT_SOURCE_DIR}"
        ENVIRONMENT "PYTHONPATH=${CMAKE_CURRENT_SOURCE_DIR}/service" TIMEOUT 180)
    if(WIN32)
        set_tests_properties(service PROPERTIES TIMEOUT 600)
    endif()
endif()

install(TARGETS DataHorizon BUNDLE DESTINATION . RUNTIME DESTINATION bin)
if(APPLE)
    install(DIRECTORY service/datahorizon DESTINATION DataHorizon.app/Contents/Resources/service
        PATTERN __pycache__ EXCLUDE)
    install(FILES "${CMAKE_CURRENT_BINARY_DIR}/source.json" DESTINATION DataHorizon.app/Contents/Resources)
    install(FILES COPYING LICENSES.md DESTINATION DataHorizon.app/Contents/Resources/licenses)
    install(DIRECTORY packaging/python/ DESTINATION DataHorizon.app/Contents/Resources/licenses/python)
    if(Qt6_VERSION VERSION_EQUAL "6.12.0")
        install(DIRECTORY packaging/qt/6.12.0/ DESTINATION DataHorizon.app/Contents/Resources/licenses/qt-6.12.0)
    elseif(DATAHORIZON_RUNTIME_ROOT)
        message(FATAL_ERROR "This distribution tier requires Qt 6.12.0 and its matching dependency materials")
    endif()
    if(DATAHORIZON_RUNTIME_ROOT)
        install(DIRECTORY "${DATAHORIZON_RUNTIME_ROOT}/" DESTINATION DataHorizon.app/Contents/Resources/runtime USE_SOURCE_PERMISSIONS
            PATTERN __pycache__ EXCLUDE)
    endif()
else()
    install(DIRECTORY service/datahorizon DESTINATION share/datahorizon/service
        PATTERN __pycache__ EXCLUDE)
    install(FILES COPYING LICENSES.md DESTINATION share/datahorizon/licenses)
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
if(APPLE AND DATAHORIZON_RUNTIME_ROOT)
    # Use a distinct name on case-insensitive filesystems. A native launcher's
    # embedded signature survives filesystem copies unlike script xattrs.
    add_executable(datahorizon_cli_launcher desktop/cli_main.cpp)
    set_target_properties(datahorizon_cli_launcher PROPERTIES OUTPUT_NAME datahorizon-cli)
    install(TARGETS datahorizon_cli_launcher RUNTIME DESTINATION DataHorizon.app/Contents/MacOS)
    install(CODE "execute_process(COMMAND \"${CMAKE_COMMAND}\" \"-DAPP_BUNDLE=\${CMAKE_INSTALL_PREFIX}/DataHorizon.app\" -P \"${CMAKE_CURRENT_SOURCE_DIR}/cmake/SignDevelopmentBundle.cmake\" COMMAND_ERROR_IS_FATAL ANY)")
endif()
