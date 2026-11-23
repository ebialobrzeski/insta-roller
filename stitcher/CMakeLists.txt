# insta-stitcher: command line stitcher on the Insta360 MediaSDK (3.2.0).
#
#   Windows (MSVC x64):
#     cmake -S stitcher -B stitcher/build -A x64 -DINSTA_MEDIA_SDK_DIR=C:/path/to/MediaSDK-win64
#     cmake --build stitcher/build --config Release
#
#   Linux (Ubuntu 22.04, GCC 11; SDK .deb installed):
#     cmake -S stitcher -B stitcher/build -DINSTA_MEDIA_SDK_DIR=/opt/MediaSDK-3.2.0-linux \
#           -DINSTA_MEDIA_SDK_INCLUDE_DIR=/path/to/MediaSDK-3.2.0-linux64/include
#     cmake --build stitcher/build -j
#
# SDK layouts this file understands (taken from the two 3.2.0 packages):
#   Windows zip : include/stitcher/*.h   lib/MediaSDK.lib   bin/*.dll   bin/models/
#   Linux       : <tarball>/include/*.h  and the .deb prefix with lib/libMediaSDK.so, bin/models/
cmake_minimum_required(VERSION 3.16)

project(insta_stitcher LANGUAGES CXX)

set(CMAKE_CXX_STANDARD 17)
set(CMAKE_CXX_STANDARD_REQUIRED ON)
set(CMAKE_CXX_EXTENSIONS OFF)

get_property(_multi_config GLOBAL PROPERTY GENERATOR_IS_MULTI_CONFIG)
if(NOT _multi_config AND NOT CMAKE_BUILD_TYPE)
    set(CMAKE_BUILD_TYPE Release CACHE STRING "Build type" FORCE)
endif()

set(INSTA_MEDIA_SDK_DIR "" CACHE PATH
    "MediaSDK folder: the extracted Windows package (include/, lib/, bin/) or the Linux install prefix (/opt/MediaSDK-<version>-linux)")
set(INSTA_MEDIA_SDK_INCLUDE_DIR "" CACHE PATH
    "Folder with ins_stitcher.h, only needed when it is not under INSTA_MEDIA_SDK_DIR (Linux: the include/ of the tarball)")
option(INSTA_STITCHER_COPY_RUNTIME
    "Copy the SDK runtime (Windows: every DLL of bin/, about 2.4 GB; both: models/) next to the executable" ON)

if(NOT INSTA_MEDIA_SDK_DIR)
    message(FATAL_ERROR
        "Set -DINSTA_MEDIA_SDK_DIR=<MediaSDK folder>.\n"
        "  Windows: the extracted MediaSDK-3.2.0-...-win64 zip (contains include/, lib/, bin/)\n"
        "  Linux:   the prefix the .deb installs into, e.g. /opt/MediaSDK-3.2.0-linux")
endif()
file(TO_CMAKE_PATH "${INSTA_MEDIA_SDK_DIR}" _sdk)

if(CMAKE_SIZEOF_VOID_P EQUAL 4)
    message(FATAL_ERROR "The MediaSDK is 64-bit only. On Windows configure with -A x64.")
endif()

# --- headers -----------------------------------------------------------------
find_path(INSTA_MEDIA_SDK_HEADERS ins_stitcher.h
    HINTS "${INSTA_MEDIA_SDK_INCLUDE_DIR}"
          "${_sdk}/include/stitcher"
          "${_sdk}/include"
          "${_sdk}"
    NO_DEFAULT_PATH)
if(NOT INSTA_MEDIA_SDK_HEADERS)
    message(FATAL_ERROR
        "ins_stitcher.h not found under ${_sdk}/include[/stitcher]. "
        "On Linux the headers are in the SDK tarball, not in the .deb: pass -DINSTA_MEDIA_SDK_INCLUDE_DIR=<tarball>/include")
endif()

# --- library -----------------------------------------------------------------
find_library(INSTA_MEDIA_SDK_LIBRARY NAMES MediaSDK
    HINTS "${_sdk}/lib" "${_sdk}/lib64" "${_sdk}/bin"
    NO_DEFAULT_PATH)
if(NOT INSTA_MEDIA_SDK_LIBRARY)
    message(FATAL_ERROR "MediaSDK library (MediaSDK.lib / libMediaSDK.so) not found under ${_sdk}/lib")
endif()
get_filename_component(_sdk_lib_dir "${INSTA_MEDIA_SDK_LIBRARY}" DIRECTORY)

message(STATUS "MediaSDK headers : ${INSTA_MEDIA_SDK_HEADERS}")
message(STATUS "MediaSDK library : ${INSTA_MEDIA_SDK_LIBRARY}")

# --- target ------------------------------------------------------------------
add_executable(insta-stitcher src/main.cpp)
target_include_directories(insta-stitcher PRIVATE "${INSTA_MEDIA_SDK_HEADERS}")
target_link_libraries(insta-stitcher PRIVATE "${INSTA_MEDIA_SDK_LIBRARY}")

if(WIN32)
    # The SDK headers test "#if WIN32" (not _WIN32) to pick __declspec(dllimport).
    target_compile_definitions(insta-stitcher PRIVATE WIN32=1 UNICODE _UNICODE NOMINMAX WIN32_LEAN_AND_MEAN _CRT_SECURE_NO_WARNINGS)
    target_link_libraries(insta-stitcher PRIVATE shell32)
endif()

if(MSVC)
    target_compile_options(insta-stitcher PRIVATE /utf-8 /W4 /permissive- /EHsc)
    # std::string / std::vector / std::function cross the DLL boundary, and
    # MediaSDK.dll is a release build against the release CRT. A debug CRT or
    # debug iterators on our side would corrupt memory, so every configuration
    # (also Debug) uses /MD and release iterators.
    set_property(TARGET insta-stitcher PROPERTY MSVC_RUNTIME_LIBRARY "MultiThreadedDLL")
    target_compile_definitions(insta-stitcher PRIVATE _ITERATOR_DEBUG_LEVEL=0)
    # The SDK ships an older msvcp140.dll that ends up next to the exe. Code built
    # with VS 2022 17.10+ would crash on the first std::mutex lock against it.
    target_compile_definitions(insta-stitcher PRIVATE _DISABLE_CONSTEXPR_MUTEX_CONSTRUCTOR)
else()
    target_compile_options(insta-stitcher PRIVATE -Wall -Wextra)
    find_package(Threads REQUIRED)
    target_link_libraries(insta-stitcher PRIVATE Threads::Threads)
    # Same as the vendor example: DT_RPATH (not DT_RUNPATH) so the SDK's own
    # dependencies are found too, without LD_LIBRARY_PATH.
    set_target_properties(insta-stitcher PROPERTIES
        BUILD_RPATH   "${_sdk_lib_dir}"
        INSTALL_RPATH "${_sdk_lib_dir}")
    target_link_options(insta-stitcher PRIVATE "LINKER:--disable-new-dtags")
endif()

# --- runtime files next to the executable -------------------------------------
# The SDK loads its DLLs from the executable's folder (or PATH) and its models
# from <exe>/models by default (see example/main.cc and README.en.txt).
if(INSTA_STITCHER_COPY_RUNTIME)
    if(CMAKE_VERSION VERSION_LESS 3.26)
        set(_copy_dir copy_directory)                    # copies every time
    else()
        set(_copy_dir copy_directory_if_different)       # 2.4 GB: only what changed
    endif()

    if(WIN32)
        if(IS_DIRECTORY "${_sdk}/bin")
            # bin/ holds MediaSDK.dll, CUDA, TensorRT, OpenCV, the MSVC runtime
            # and models/. All of it is needed at run time.
            add_custom_command(TARGET insta-stitcher POST_BUILD
                COMMAND ${CMAKE_COMMAND} -E ${_copy_dir} "${_sdk}/bin" "$<TARGET_FILE_DIR:insta-stitcher>"
                COMMENT "Copying the MediaSDK runtime (DLLs and models) next to insta-stitcher"
                VERBATIM)
        else()
            message(WARNING "${_sdk}/bin not found: the SDK DLLs and models are not copied. "
                            "Extract bin/ from the SDK zip or put it on PATH and pass --models-dir.")
        endif()
    else()
        set(_models "")
        foreach(_candidate "${_sdk}/bin/models" "${_sdk}/models")
            if(NOT _models AND IS_DIRECTORY "${_candidate}")
                set(_models "${_candidate}")
            endif()
        endforeach()
        if(_models)
            add_custom_command(TARGET insta-stitcher POST_BUILD
                COMMAND ${CMAKE_COMMAND} -E ${_copy_dir} "${_models}" "$<TARGET_FILE_DIR:insta-stitcher>/models"
                COMMENT "Copying the MediaSDK models next to insta-stitcher"
                VERBATIM)
        else()
            message(WARNING "No models/ folder under ${_sdk}: pass --models-dir at run time.")
        endif()
    endif()
endif()

install(TARGETS insta-stitcher RUNTIME DESTINATION bin)
