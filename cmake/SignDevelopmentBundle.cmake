if(NOT APPLE)
    return()
endif()
if(NOT DEFINED APP_BUNDLE OR NOT EXISTS "${APP_BUNDLE}/Contents/Info.plist")
    message(FATAL_ERROR "A complete development app bundle is required for signature verification")
endif()
find_program(CODESIGN_TOOL codesign REQUIRED)
execute_process(COMMAND "${CODESIGN_TOOL}" --verify --deep --strict "${APP_BUNDLE}"
    RESULT_VARIABLE signature_status OUTPUT_QUIET ERROR_QUIET)
if(NOT signature_status EQUAL 0)
    # This is a local development signature, not Developer ID distribution or
    # notarization. Re-seal resources after the build changes its executable/icons.
    execute_process(COMMAND "${CODESIGN_TOOL}" --force --deep --sign - "${APP_BUNDLE}"
        RESULT_VARIABLE sign_status ERROR_VARIABLE sign_error)
    if(NOT sign_status EQUAL 0)
        message(FATAL_ERROR "Development bundle signing failed: ${sign_error}")
    endif()
endif()
execute_process(COMMAND "${CODESIGN_TOOL}" --verify --deep --strict "${APP_BUNDLE}"
    RESULT_VARIABLE verified ERROR_VARIABLE verify_error)
if(NOT verified EQUAL 0)
    message(FATAL_ERROR "Development bundle signature is invalid: ${verify_error}")
endif()
message(STATUS "Verified ad-hoc development bundle: ${APP_BUNDLE}")
