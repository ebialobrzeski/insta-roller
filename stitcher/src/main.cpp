// insta-stitcher: Insta360 camera file (.insv) -> equirectangular MP4 for a time span.
//
// Written against Insta360 MediaSDK 3.2.0 (Windows and Linux packages ship
// identical headers). Everything below relies only on these public symbols:
//
//   ins_stitcher.h
//     ins::InitEnv(), ins::SetLogLevel(), ins::SetLogPath(),
//     ins::SetModelFileRootDir(), ins::GetMediaFileInfo()
//     ins::VideoStitcher::
//       SetInputPath, SetOutputPath, SetOutputSize, SetOutputBitRate,
//       SetImageSequenceInfo, SetExportFrameSequence,
//       EnableFlowState, EnableDirectionLock, EnableStitchFusion,
//       EnableCuda, EnableH265Encoder, SetSoftwareCodecUsage,
//       SetImageProcessingAccelType, SetStitchType, SetCameraAccessoryType,
//       SetStitchProgressCallback, SetStitchStateCallback,
//       StartStitch, CancelStitch, GetFeatureStatusMap
//   ins_common.h
//     ins::MediaFileInfo, ins::STITCH_TYPE, ins::IMAGE_TYPE, ins::InsLogLevel,
//     ins::CameraAccessoryType, ins::ImageProcessingAccel, ins::GetVersion(),
//     stitch_process_callback (int process, int error),
//     stitch_error_callback   (int error, const char* errinfo)
//   example/main.cc (vendor demo): call order, "progress == 100 means done",
//     errors arrive only through the state callback, model dir must end with a
//     path separator, arguments must be UTF-8.
//
// Facts about the SDK that shape this tool:
//
//   * There is NO trim / time-range call on VideoStitcher (no SetTrimRange,
//     no seek, no start/end). A video export always covers the whole file.
//     The only way to make the SDK render a sub-range is the image sequence
//     export: SetImageSequenceInfo(dir, type) + SetExportFrameSequence(indices).
//     So a partial span is done as  SDK -> frames of that span -> FFmpeg -> MP4
//     ("frames" mode). A span that covers the whole file uses the SDK's own
//     video export ("direct" mode). "full" mode stitches the whole file with
//     the SDK and cuts the span out with FFmpeg afterwards.
//   * There is no GPU index call either; the header says one process = one GPU.
//     --gpu therefore sets CUDA_VISIBLE_DEVICES before InitEnv().
//   * Hardware encoding is the SDK default (SetSoftwareCodecUsage(false,false));
//     the library carries h264_nvenc / hevc_nvenc.
//
// Exit codes: 0 success, 1 runtime failure, 2 usage error. Errors go to stderr.
// stdout carries only "progress N%" lines (with --progress) and whatever the
// SDK itself prints.

#include <algorithm>
#include <atomic>
#include <cctype>
#include <cerrno>
#include <chrono>
#include <cmath>
#include <condition_variable>
#include <csignal>
#include <cstdint>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <filesystem>
#include <functional>
#include <initializer_list>
#include <iostream>
#include <map>
#include <memory>
#include <mutex>
#include <string>
#include <system_error>
#include <thread>
#include <utility>
#include <vector>

#ifdef _WIN32
#ifndef NOMINMAX
#define NOMINMAX
#endif
#ifndef WIN32_LEAN_AND_MEAN
#define WIN32_LEAN_AND_MEAN
#endif
#include <windows.h>
#include <shellapi.h>
#else
#include <fcntl.h>
#include <limits.h>
#include <sys/wait.h>
#include <unistd.h>
#endif

#include <ins_stitcher.h>

#ifdef _WIN32
// Same as the vendor demo: on dual-GPU laptops make the driver pick the
// discrete GPU for this process.
extern "C" {
__declspec(dllexport) unsigned long NvOptimusEnablement = 0x00000001;
__declspec(dllexport) int AmdPowerXpressRequestHighPerformance = 1;
}
#endif

namespace fs = std::filesystem;

namespace {

const char* const kTool = "insta-stitcher";
constexpr int kExitOk = 0;
constexpr int kExitFailed = 1;
constexpr int kExitUsage = 2;

// ---------------------------------------------------------------------------
// Small helpers
// ---------------------------------------------------------------------------

void log_error(const std::string& msg) { std::cerr << kTool << ": error: " << msg << std::endl; }
void log_warn(const std::string& msg) { std::cerr << kTool << ": warning: " << msg << std::endl; }
void log_info(const std::string& msg) { std::cerr << kTool << ": " << msg << std::endl; }

// All strings inside the program are UTF-8 (the SDK requires UTF-8 paths).
fs::path to_path(const std::string& utf8) { return fs::u8path(utf8); }

std::string to_utf8(const fs::path& p) {
    const auto s = p.u8string();           // std::string in C++17, std::u8string in C++20
    return std::string(s.begin(), s.end());
}

void push(std::vector<std::string>& v, std::initializer_list<std::string> items) {
    v.insert(v.end(), items.begin(), items.end());
}

std::string lower(std::string s) {
    std::transform(s.begin(), s.end(), s.begin(), [](unsigned char c) { return static_cast<char>(std::tolower(c)); });
    return s;
}

std::string fmt_double(double v, int precision) {
    char buf[64];
    std::snprintf(buf, sizeof(buf), "%.*f", precision, v);
    return buf;
}

bool parse_int64(const std::string& text, int64_t& out) {
    if (text.empty()) return false;
    errno = 0;
    char* end = nullptr;
    const long long v = std::strtoll(text.c_str(), &end, 10);
    if (errno != 0 || end == text.c_str() || *end != '\0') return false;
    out = static_cast<int64_t>(v);
    return true;
}

// "80000000", "80000k", "80M", "80m" -> bits per second.
bool parse_bitrate(const std::string& text, int64_t& out) {
    if (text.empty()) return false;
    std::string digits = text;
    double scale = 1.0;
    const char last = static_cast<char>(std::tolower(static_cast<unsigned char>(text.back())));
    if (last == 'k') { scale = 1e3; digits.pop_back(); }
    else if (last == 'm') { scale = 1e6; digits.pop_back(); }
    if (digits.empty()) return false;
    errno = 0;
    char* end = nullptr;
    const double v = std::strtod(digits.c_str(), &end);
    if (errno != 0 || end == digits.c_str() || *end != '\0' || v <= 0) return false;
    out = static_cast<int64_t>(std::llround(v * scale));
    return out > 0;
}

#ifdef _WIN32
std::wstring widen(const std::string& s) {
    if (s.empty()) return std::wstring();
    const int n = MultiByteToWideChar(CP_UTF8, 0, s.data(), static_cast<int>(s.size()), nullptr, 0);
    std::wstring w(static_cast<size_t>(n > 0 ? n : 0), L'\0');
    if (n > 0) MultiByteToWideChar(CP_UTF8, 0, s.data(), static_cast<int>(s.size()), w.data(), n);
    return w;
}

std::string narrow(const std::wstring& w) {
    if (w.empty()) return std::string();
    const int n = WideCharToMultiByte(CP_UTF8, 0, w.data(), static_cast<int>(w.size()), nullptr, 0, nullptr, nullptr);
    std::string s(static_cast<size_t>(n > 0 ? n : 0), '\0');
    if (n > 0) WideCharToMultiByte(CP_UTF8, 0, w.data(), static_cast<int>(w.size()), s.data(), n, nullptr, nullptr);
    return s;
}

// Quoting rules of CommandLineToArgvW / the MSVC runtime.
std::wstring quote_arg(const std::wstring& arg) {
    if (!arg.empty() && arg.find_first_of(L" \t\n\v\"") == std::wstring::npos) return arg;
    std::wstring out = L"\"";
    size_t i = 0;
    for (;;) {
        size_t backslashes = 0;
        while (i < arg.size() && arg[i] == L'\\') { ++i; ++backslashes; }
        if (i == arg.size()) {
            out.append(backslashes * 2, L'\\');
            break;
        }
        if (arg[i] == L'"') out.append(backslashes * 2 + 1, L'\\');
        else out.append(backslashes, L'\\');
        out.push_back(arg[i]);
        ++i;
    }
    out.push_back(L'"');
    return out;
}
#endif

std::string env_utf8(const char* name) {
#ifdef _WIN32
    const std::wstring wname = widen(name);
    const DWORD n = GetEnvironmentVariableW(wname.c_str(), nullptr, 0);
    if (n == 0) return std::string();
    std::wstring value(n, L'\0');
    const DWORD got = GetEnvironmentVariableW(wname.c_str(), value.data(), n);
    value.resize(got);
    return narrow(value);
#else
    const char* v = std::getenv(name);
    return v ? std::string(v) : std::string();
#endif
}

void set_env(const char* name, const std::string& value) {
#ifdef _WIN32
    SetEnvironmentVariableW(widen(name).c_str(), widen(value).c_str());
    _putenv_s(name, value.c_str());        // the CRT keeps its own copy; CUDA reads either
#else
    setenv(name, value.c_str(), 1);
#endif
}

fs::path executable_dir() {
#ifdef _WIN32
    std::wstring buf(32768, L'\0');
    const DWORD n = GetModuleFileNameW(nullptr, buf.data(), static_cast<DWORD>(buf.size()));
    buf.resize(n);
    return fs::path(buf).parent_path();
#else
    char buf[PATH_MAX];
    const ssize_t n = readlink("/proc/self/exe", buf, sizeof(buf) - 1);
    if (n <= 0) return fs::current_path();
    buf[n] = '\0';
    return fs::path(buf).parent_path();
#endif
}

// The SDK joins "<dir>" + "<file>" itself, so directories are handed over with
// a trailing separator (required for the model dir, harmless elsewhere).
std::string with_trailing_separator(const fs::path& dir) {
    std::string s = to_utf8(dir);
    if (s.empty()) return s;
    if (s.back() != '/' && s.back() != '\\') s.push_back(static_cast<char>(fs::path::preferred_separator));
    return s;
}

// Runs a program and waits for it. The child's stdout is redirected to our
// stderr so that our stdout carries nothing but progress lines.
// Returns the exit code, or -1 when the program could not be started.
int run_process(const std::vector<std::string>& argv, const fs::path& cwd, std::string& start_error) {
#ifdef _WIN32
    std::wstring cmd;
    for (size_t i = 0; i < argv.size(); ++i) {
        if (i) cmd.push_back(L' ');
        cmd += quote_arg(widen(argv[i]));
    }

    SECURITY_ATTRIBUTES sa{};
    sa.nLength = sizeof(sa);
    sa.bInheritHandle = TRUE;
    HANDLE nul_in = CreateFileW(L"NUL", GENERIC_READ, FILE_SHARE_READ | FILE_SHARE_WRITE, &sa, OPEN_EXISTING, 0, nullptr);
    HANDLE nul_out = CreateFileW(L"NUL", GENERIC_WRITE, FILE_SHARE_READ | FILE_SHARE_WRITE, &sa, OPEN_EXISTING, 0, nullptr);
    HANDLE err_dup = nullptr;
    const HANDLE err = GetStdHandle(STD_ERROR_HANDLE);
    if (err != nullptr && err != INVALID_HANDLE_VALUE) {
        if (!DuplicateHandle(GetCurrentProcess(), err, GetCurrentProcess(), &err_dup, 0, TRUE, DUPLICATE_SAME_ACCESS))
            err_dup = nullptr;
    }
    const HANDLE child_err = err_dup ? err_dup : nul_out;

    STARTUPINFOW si{};
    si.cb = sizeof(si);
    si.dwFlags = STARTF_USESTDHANDLES;
    si.hStdInput = nul_in;
    si.hStdOutput = child_err;
    si.hStdError = child_err;
    PROCESS_INFORMATION pi{};
    const std::wstring wcwd = cwd.empty() ? std::wstring() : cwd.wstring();
    const BOOL ok = CreateProcessW(nullptr, cmd.data(), nullptr, nullptr, TRUE, 0, nullptr,
                                   wcwd.empty() ? nullptr : wcwd.c_str(), &si, &pi);
    const DWORD last_error = ok ? 0 : GetLastError();
    if (nul_in != INVALID_HANDLE_VALUE) CloseHandle(nul_in);
    if (nul_out != INVALID_HANDLE_VALUE) CloseHandle(nul_out);
    if (err_dup) CloseHandle(err_dup);
    if (!ok) {
        start_error = "cannot start '" + argv[0] + "' (Windows error " + std::to_string(last_error) + ")";
        return -1;
    }
    WaitForSingleObject(pi.hProcess, INFINITE);
    DWORD code = 1;
    GetExitCodeProcess(pi.hProcess, &code);
    CloseHandle(pi.hThread);
    CloseHandle(pi.hProcess);
    return static_cast<int>(code);
#else
    std::vector<char*> cargv;
    cargv.reserve(argv.size() + 1);
    for (const auto& a : argv) cargv.push_back(const_cast<char*>(a.c_str()));
    cargv.push_back(nullptr);
    const std::string cwd_s = cwd.string();

    const pid_t pid = fork();
    if (pid < 0) {
        start_error = std::string("fork failed: ") + std::strerror(errno);
        return -1;
    }
    if (pid == 0) {
        if (!cwd_s.empty() && chdir(cwd_s.c_str()) != 0) _exit(126);
        const int devnull = open("/dev/null", O_RDONLY);
        if (devnull >= 0) dup2(devnull, STDIN_FILENO);
        dup2(STDERR_FILENO, STDOUT_FILENO);
        execvp(cargv[0], cargv.data());
        _exit(127);
    }
    int status = 0;
    while (waitpid(pid, &status, 0) < 0) {
        if (errno != EINTR) {
            start_error = std::string("waitpid failed: ") + std::strerror(errno);
            return -1;
        }
    }
    if (WIFEXITED(status)) {
        if (WEXITSTATUS(status) == 127) {
            start_error = "cannot start '" + argv[0] + "'";
            return -1;
        }
        return WEXITSTATUS(status);
    }
    return 128 + (WIFSIGNALED(status) ? WTERMSIG(status) : 0);
#endif
}

volatile std::sig_atomic_t g_interrupted = 0;
void on_signal(int) { g_interrupted = 1; }

// ---------------------------------------------------------------------------
// Options
// ---------------------------------------------------------------------------

enum class Stabilize { None, FlowState, DirectionLock };
enum class TrimMode { Auto, Frames, Full };
enum class Codec { Auto, H264, H265 };

struct Options {
    std::string input;
    std::string output;
    int64_t start_ms = 0;
    int64_t duration_ms = -1;                 // -1: to the end of the file
    Stabilize stabilize = Stabilize::DirectionLock;
    int width = 0;
    int height = 0;
    int64_t bitrate = 0;                      // 0: encoder default
    int gpu = -1;
    bool progress = false;

    Codec codec = Codec::Auto;
    TrimMode trim_mode = TrimMode::Auto;
    ins::IMAGE_TYPE intermediate = ins::IMAGE_TYPE::JPEG;
    ins::STITCH_TYPE stitch_type = ins::STITCH_TYPE::OPTFLOW;
    ins::CameraAccessoryType accessory = ins::CameraAccessoryType::kNormal;
    bool stitch_fusion = false;
    bool no_cuda = false;
    bool soft_encode = false;
    bool soft_decode = false;
    bool no_audio = false;
    bool keep_temp = false;
    bool verbose = false;
    int64_t stall_timeout_s = 600;            // 0: never give up
    std::string ffmpeg;
    std::string models_dir;
    std::string temp_dir;
    std::string log_file;
};

const char* const kUsage =
    "Usage:\n"
    "  insta-stitcher --input <file.insv> --output <out.mp4>\n"
    "                 [--start-ms N] [--duration-ms M]\n"
    "                 [--stabilize none|flowstate|direction-lock]\n"
    "                 [--width W --height H] [--bitrate BPS] [--gpu INDEX] [--progress]\n"
    "\n"
    "Required:\n"
    "  --input PATH          camera file (.insv; one file holds both lenses on X4/X5)\n"
    "  --output PATH         equirectangular MP4 to write (2:1)\n"
    "\n"
    "Time span:\n"
    "  --start-ms N          start of the span in the file, milliseconds (default 0)\n"
    "  --duration-ms M       length of the span, milliseconds (default: to the end)\n"
    "\n"
    "Picture:\n"
    "  --stabilize MODE      none            no stabilisation, picture tied to the camera\n"
    "                        flowstate       FlowState stabilisation, view follows the camera heading\n"
    "                        direction-lock  FlowState + direction lock, level horizon, fixed heading (default)\n"
    "  --width W --height H  output size, must be 2:1 and even (default: native, 2 x lens width)\n"
    "  --bitrate BPS         video bitrate, e.g. 80000000 or 80M (default: encoder default)\n"
    "  --codec h264|h265     default: h264 up to 4096 px wide, h265 above\n"
    "  --stitch-type T       template|optflow|dynamic|ai (default optflow)\n"
    "  --stitch-fusion       remove the colour difference across the seam (Studio: Image Fusion)\n"
    "  --accessory N         ins::CameraAccessoryType value, -1 = auto detect (default 0, bare camera)\n"
    "\n"
    "Hardware:\n"
    "  --gpu INDEX           GPU to use (sets CUDA_VISIBLE_DEVICES; the SDK has no GPU selection call)\n"
    "  --no-cuda             do not use CUDA for stitching\n"
    "  --soft-encode         software encoder instead of NVENC\n"
    "  --soft-decode         software decoder instead of the hardware one\n"
    "\n"
    "Trimming (the SDK has no time range call, see README):\n"
    "  --trim-mode MODE      auto    whole file: SDK video export; partial span: frames (default)\n"
    "                        frames  SDK exports the span as images, FFmpeg encodes them\n"
    "                        full    SDK stitches the whole file, FFmpeg cuts the span out\n"
    "  --intermediate jpg|png  image type for frames mode (default jpg)\n"
    "  --ffmpeg PATH         FFmpeg executable (default: $INSTA_ROLLER_FFMPEG, then 'ffmpeg' on PATH)\n"
    "  --temp-dir DIR        where intermediate files go (default: next to the output)\n"
    "  --keep-temp           do not delete intermediate files\n"
    "  --no-audio            do not copy the audio of the span\n"
    "\n"
    "Other:\n"
    "  --progress            print 'progress N%' lines to stdout\n"
    "  --models-dir DIR      SDK model directory (default: <exe dir>/models)\n"
    "  --stall-timeout S     give up after S seconds without progress (default 600, 0 = never)\n"
    "  --log-file PATH       also write the SDK log to this file\n"
    "  --verbose             SDK log level INFO and feature status report\n"
    "  --version             print tool and SDK version\n"
    "  --help                this text\n";

struct UsageError {
    std::string message;
};

Options parse_args(const std::vector<std::string>& args, bool& want_help, bool& want_version) {
    Options o;
    want_help = want_version = false;
    bool have_width = false, have_height = false;

    for (size_t i = 1; i < args.size(); ++i) {
        std::string name = args[i];
        std::string inline_value;
        bool has_inline = false;
        if (name.rfind("--", 0) == 0) {
            const size_t eq = name.find('=');
            if (eq != std::string::npos) {
                inline_value = name.substr(eq + 1);
                name = name.substr(0, eq);
                has_inline = true;
            }
        }
        auto value = [&]() -> std::string {
            if (has_inline) return inline_value;
            if (i + 1 >= args.size()) throw UsageError{"option '" + name + "' needs a value"};
            return args[++i];
        };
        auto flag = [&]() {
            if (has_inline) throw UsageError{"option '" + name + "' takes no value"};
        };
        auto integer = [&](int64_t min_value, int64_t max_value) -> int64_t {
            const std::string text = value();
            int64_t v = 0;
            if (!parse_int64(text, v) || v < min_value || v > max_value)
                throw UsageError{"option '" + name + "': '" + text + "' is not an integer in [" +
                                 std::to_string(min_value) + ", " + std::to_string(max_value) + "]"};
            return v;
        };
        const int64_t kMaxMs = int64_t(1000) * 3600 * 24 * 7;

        if (name == "--help" || name == "-h") { flag(); want_help = true; }
        else if (name == "--version") { flag(); want_version = true; }
        else if (name == "--input" || name == "-i") o.input = value();
        else if (name == "--output" || name == "-o") o.output = value();
        else if (name == "--start-ms") o.start_ms = integer(0, kMaxMs);
        else if (name == "--duration-ms") o.duration_ms = integer(1, kMaxMs);
        else if (name == "--stabilize") {
            const std::string v = lower(value());
            if (v == "none" || v == "off") o.stabilize = Stabilize::None;
            else if (v == "flowstate") o.stabilize = Stabilize::FlowState;
            else if (v == "direction-lock" || v == "directionlock" || v == "direction_lock") o.stabilize = Stabilize::DirectionLock;
            else throw UsageError{"option '--stabilize': '" + v + "' is not one of none, flowstate, direction-lock"};
        }
        else if (name == "--width") { o.width = static_cast<int>(integer(2, 16384)); have_width = true; }
        else if (name == "--height") { o.height = static_cast<int>(integer(2, 8192)); have_height = true; }
        else if (name == "--bitrate") {
            const std::string v = value();
            if (!parse_bitrate(v, o.bitrate)) throw UsageError{"option '--bitrate': '" + v + "' is not a bitrate (e.g. 80000000, 80M)"};
        }
        else if (name == "--gpu") o.gpu = static_cast<int>(integer(0, 63));
        else if (name == "--progress") { flag(); o.progress = true; }
        else if (name == "--codec") {
            const std::string v = lower(value());
            if (v == "h264" || v == "avc") o.codec = Codec::H264;
            else if (v == "h265" || v == "hevc") o.codec = Codec::H265;
            else if (v == "auto") o.codec = Codec::Auto;
            else throw UsageError{"option '--codec': '" + v + "' is not one of auto, h264, h265"};
        }
        else if (name == "--trim-mode") {
            const std::string v = lower(value());
            if (v == "auto") o.trim_mode = TrimMode::Auto;
            else if (v == "frames") o.trim_mode = TrimMode::Frames;
            else if (v == "full") o.trim_mode = TrimMode::Full;
            else throw UsageError{"option '--trim-mode': '" + v + "' is not one of auto, frames, full"};
        }
        else if (name == "--intermediate") {
            const std::string v = lower(value());
            if (v == "jpg" || v == "jpeg") o.intermediate = ins::IMAGE_TYPE::JPEG;
            else if (v == "png") o.intermediate = ins::IMAGE_TYPE::PNG;
            else throw UsageError{"option '--intermediate': '" + v + "' is not one of jpg, png"};
        }
        else if (name == "--stitch-type") {
            const std::string v = lower(value());
            if (v == "template") o.stitch_type = ins::STITCH_TYPE::TEMPLATE;
            else if (v == "optflow") o.stitch_type = ins::STITCH_TYPE::OPTFLOW;
            else if (v == "dynamic" || v == "dynamicstitch") o.stitch_type = ins::STITCH_TYPE::DYNAMICSTITCH;
            else if (v == "ai" || v == "aistitch" || v == "aiflow") o.stitch_type = ins::STITCH_TYPE::AIFLOW;
            else throw UsageError{"option '--stitch-type': '" + v + "' is not one of template, optflow, dynamic, ai"};
        }
        else if (name == "--accessory") o.accessory = static_cast<ins::CameraAccessoryType>(integer(-1, 100));
        else if (name == "--stitch-fusion") { flag(); o.stitch_fusion = true; }
        else if (name == "--no-cuda") { flag(); o.no_cuda = true; }
        else if (name == "--soft-encode") { flag(); o.soft_encode = true; }
        else if (name == "--soft-decode") { flag(); o.soft_decode = true; }
        else if (name == "--no-audio") { flag(); o.no_audio = true; }
        else if (name == "--keep-temp") { flag(); o.keep_temp = true; }
        else if (name == "--verbose" || name == "-v") { flag(); o.verbose = true; }
        else if (name == "--stall-timeout") o.stall_timeout_s = integer(0, 86400);
        else if (name == "--ffmpeg") o.ffmpeg = value();
        else if (name == "--models-dir") o.models_dir = value();
        else if (name == "--temp-dir") o.temp_dir = value();
        else if (name == "--log-file") o.log_file = value();
        else throw UsageError{"unknown option '" + args[i] + "'"};
    }

    if (want_help || want_version) return o;
    if (o.input.empty()) throw UsageError{"--input is required"};
    if (o.output.empty()) throw UsageError{"--output is required"};
    if (have_width != have_height) {
        if (have_width) o.height = o.width / 2;
        else o.width = o.height * 2;
    }
    if (o.width || o.height) {
        if (o.width % 2 || o.height % 2) throw UsageError{"--width and --height must be even"};
        if (o.width != o.height * 2)
            throw UsageError{"output must be 2:1 (equirectangular), got " + std::to_string(o.width) + "x" + std::to_string(o.height)};
    }
    return o;
}

// ---------------------------------------------------------------------------
// Progress
// ---------------------------------------------------------------------------

class Progress {
public:
    explicit Progress(bool enabled) : enabled_(enabled) {}

    // percent of the current stage, mapped into [lo, hi] of the whole job
    void stage(int lo, int hi) {
        std::lock_guard<std::mutex> lock(mutex_);
        lo_ = lo;
        hi_ = hi;
    }
    void report(int stage_percent) {
        std::lock_guard<std::mutex> lock(mutex_);
        stage_percent = std::max(0, std::min(100, stage_percent));
        const int total = lo_ + (hi_ - lo_) * stage_percent / 100;
        if (total <= last_) return;
        last_ = total;
        if (enabled_) std::cout << "progress " << total << "%" << std::endl;
    }

private:
    const bool enabled_;
    std::mutex mutex_;
    int lo_ = 0, hi_ = 100, last_ = -1;
};

// ---------------------------------------------------------------------------
// One SDK run
// ---------------------------------------------------------------------------

struct SdkJob {
    bool image_sequence = false;
    std::string output_file;                  // video export
    std::string sequence_dir;                 // image sequence export (trailing separator)
    std::vector<uint64_t> frames;             // frame indices, zero based
    int width = 0, height = 0;
    bool h265 = false;
};

const char* sdk_error_name(int code) {
    switch (static_cast<ins::SDKErrorCode>(code)) {
        case ins::SDKErrorCode::E_SUCCESS: return "E_SUCCESS";
        case ins::SDKErrorCode::E_OPEN_FILE: return "E_OPEN_FILE (cannot open the input file)";
        case ins::SDKErrorCode::E_PARSE_METADATA: return "E_PARSE_METADATA (cannot parse the file metadata / trailer)";
        case ins::SDKErrorCode::E_CREATE_OFFSCREEN: return "E_CREATE_OFFSCREEN (cannot create the offscreen render target)";
        case ins::SDKErrorCode::E_CREATE_RENDER_MODEL: return "E_CREATE_RENDER_MODEL";
        case ins::SDKErrorCode::E_FRAME_PARSE: return "E_FRAME_PARSE (cannot read a video frame)";
        case ins::SDKErrorCode::E_CREATE_RENDER_SOURCE: return "E_CREATE_RENDER_SOURCE";
        case ins::SDKErrorCode::E_UPDATE_RENDER_SOURCE: return "E_UPDATE_RENDER_SOURCE";
        case ins::SDKErrorCode::E_RENDER_FRAME: return "E_RENDER_FRAME";
        case ins::SDKErrorCode::E_SAVE_FRAME: return "E_SAVE_FRAME (cannot save an output image)";
        case ins::SDKErrorCode::E_VIDEO_FRAME_EXPORTOR: return "E_VIDEO_FRAME_EXPORTOR (cannot create the video exporter / encoder)";
        case ins::SDKErrorCode::E_FILE_TYPE_UNSUPPORT: return "E_FILE_TYPE_UNSUPPORT (input file type is not supported)";
        case ins::SDKErrorCode::E_INTERNAL_ERROR: return "E_INTERNAL_ERROR";
        case ins::SDKErrorCode::E_UNKNOWN: return "E_UNKNOWN";
    }
    return "unlisted SDK error";
}

// Configures a VideoStitcher, starts it and waits for the finish (progress 100)
// or error callback. Returns false with `error` filled in on failure.
bool run_sdk(const Options& opt, const SdkJob& job, Progress& progress, std::string& error) {
    struct State {
        std::mutex mutex;
        std::condition_variable cond;
        bool finished = false;
        bool failed = false;
        std::string failure;
        std::chrono::steady_clock::time_point last_activity = std::chrono::steady_clock::now();
        int last_percent = -1;
    };
    // Shared, not on the stack: a late callback from an SDK thread must never
    // touch a dead frame.
    auto state = std::make_shared<State>();
    Progress* const progress_ptr = &progress;

    auto stitcher = std::make_shared<ins::VideoStitcher>();
    stitcher->SetInputPath({opt.input});
    if (job.image_sequence) {
        stitcher->SetExportFrameSequence(job.frames);
        stitcher->SetImageSequenceInfo(job.sequence_dir, opt.intermediate);
    } else {
        stitcher->SetOutputPath(job.output_file);
        if (job.h265) stitcher->EnableH265Encoder(true);
        if (opt.bitrate > 0) stitcher->SetOutputBitRate(opt.bitrate);
    }
    stitcher->SetStitchType(opt.stitch_type);
    stitcher->EnableCuda(!opt.no_cuda);
    stitcher->SetImageProcessingAccelType(opt.no_cuda ? ins::ImageProcessingAccel::kCPU : ins::ImageProcessingAccel::kAuto);
    stitcher->EnableStitchFusion(opt.stitch_fusion);
    stitcher->SetOutputSize(job.width, job.height);
    stitcher->SetCameraAccessoryType(opt.accessory);
    stitcher->SetSoftwareCodecUsage(opt.soft_encode, opt.soft_decode);

    // Stabilisation mapping (ins_stitcher.h): direction lock depends on
    // FlowState, so it is only ever switched on together with it.
    const bool flowstate = opt.stabilize != Stabilize::None;
    const bool direction_lock = opt.stabilize == Stabilize::DirectionLock;
    stitcher->EnableFlowState(flowstate);
    stitcher->EnableDirectionLock(direction_lock);

    stitcher->SetStitchProgressCallback([state, progress_ptr](int percent, int err) {
        bool notify = false;
        {
            std::lock_guard<std::mutex> lock(state->mutex);
            if (state->finished || state->failed) return;
            if (percent != state->last_percent) {
                state->last_percent = percent;
                state->last_activity = std::chrono::steady_clock::now();
            }
            if (err != 0) {
                state->failed = true;
                state->failure = std::string("SDK reported error ") + std::to_string(err) + " (" + sdk_error_name(err) + ") during stitching";
                notify = true;
            } else if (percent >= 100) {
                state->finished = true;
                notify = true;
            }
        }
        // 100 is reported by the caller once the output has been checked.
        if (!notify) progress_ptr->report(std::min(percent, 99));
        if (notify) state->cond.notify_all();
    });

    stitcher->SetStitchStateCallback([state](int err, const char* info) {
        const std::string text = info ? info : "";
        if (err == 0) {
            // "error or state change": code 0 is not a failure. Keep it visible.
            if (!text.empty()) std::cerr << kTool << ": SDK state: " << text << std::endl;
            return;
        }
        {
            std::lock_guard<std::mutex> lock(state->mutex);
            if (state->finished || state->failed) return;
            state->failed = true;
            state->failure = "SDK error " + std::to_string(err) + " (" + sdk_error_name(err) + ")" + (text.empty() ? "" : ": " + text);
        }
        state->cond.notify_all();
    });

    {
        std::lock_guard<std::mutex> lock(state->mutex);
        state->last_activity = std::chrono::steady_clock::now();
    }
    stitcher->StartStitch();                  // asynchronous

    bool cancelled = false;
    {
        std::unique_lock<std::mutex> lock(state->mutex);
        while (!state->finished && !state->failed) {
            state->cond.wait_for(lock, std::chrono::milliseconds(200));
            if (state->finished || state->failed) break;
            if (g_interrupted) {
                state->failed = true;
                state->failure = "interrupted";
                cancelled = true;
            } else if (opt.stall_timeout_s > 0 &&
                       std::chrono::steady_clock::now() - state->last_activity > std::chrono::seconds(opt.stall_timeout_s)) {
                state->failed = true;
                state->failure = "no progress from the SDK for " + std::to_string(opt.stall_timeout_s) +
                                 " s (last progress " + std::to_string(std::max(0, state->last_percent)) + "%), giving up";
                cancelled = true;
            }
        }
    }
    if (cancelled) stitcher->CancelStitch();

    bool ok;
    {
        std::lock_guard<std::mutex> lock(state->mutex);
        ok = state->finished && !state->failed;
        if (!ok) error = state->failure.empty() ? "stitching failed" : state->failure;
    }

    if (ok) {
        // -1 unknown, 0 off, 1 on, 2 skipped (auto), 3 failed
        const std::map<std::string, int> features = stitcher->GetFeatureStatusMap();
        for (const auto& [name, status] : features) {
            const std::string key = lower(name);
            const bool stab = key.find("flow") != std::string::npos || key.find("direction") != std::string::npos ||
                              key.find("stab") != std::string::npos;
            if (status == 3) log_warn("SDK feature '" + name + "' failed at run time");
            else if (status == 2 && stab && flowstate)
                log_warn("SDK skipped '" + name + "' (no gyro data in the file?); the output is NOT stabilised as requested");
            else if (opt.verbose) log_info("SDK feature " + name + " = " + std::to_string(status));
        }
    }
    stitcher.reset();                         // frees decoder, encoder and VRAM before FFmpeg starts
    return ok;
}

// ---------------------------------------------------------------------------
// FFmpeg side (frames and full modes)
// ---------------------------------------------------------------------------

std::string ffmpeg_program(const Options& opt) {
    if (!opt.ffmpeg.empty()) return opt.ffmpeg;
    const std::string env = env_utf8("INSTA_ROLLER_FFMPEG");
    return env.empty() ? std::string("ffmpeg") : env;
}

// 29.97 -> "30000/1001", 30 -> "30", anything else with six decimals.
std::string fps_argument(double fps) {
    for (int base : {24, 30, 48, 50, 60, 100, 120, 240}) {
        if (std::fabs(fps - base * 1000.0 / 1001.0) < 0.005) return std::to_string(base * 1000) + "/1001";
    }
    if (std::fabs(fps - std::round(fps)) < 0.005) return std::to_string(static_cast<int>(std::lround(fps)));
    return fmt_double(fps, 6);
}

std::vector<std::string> encoder_args(const Options& opt, bool h265, bool hardware, double fps) {
    std::vector<std::string> a;
    auto add = [&a](std::initializer_list<std::string> items) { push(a, items); };
    const std::string rate = std::to_string(opt.bitrate);
    if (hardware) {
        add({"-c:v", h265 ? "hevc_nvenc" : "h264_nvenc", "-preset", "p5", "-rc", "vbr"});
        if (opt.bitrate > 0) {
            add({"-b:v", rate, "-maxrate", std::to_string(opt.bitrate * 3 / 2), "-bufsize", std::to_string(opt.bitrate * 2)});
        } else {
            add({"-cq", "21", "-b:v", "0"});
        }
    } else {
        add({"-c:v", h265 ? "libx265" : "libx264", "-preset", h265 ? "fast" : "veryfast"});
        if (opt.bitrate > 0) add({"-b:v", rate});
        else add({"-crf", h265 ? "20" : "18"});
    }
    if (h265) add({"-tag:v", "hvc1"});
    add({"-g", std::to_string(std::max<long>(1, std::lround(fps)))});   // a keyframe every second
    add({"-pix_fmt", "yuv420p", "-color_range", "tv", "-colorspace", "bt709", "-color_primaries", "bt709", "-color_trc", "bt709"});
    return a;
}

// Runs FFmpeg with NVENC first and a software encoder as fallback.
// `before_codec` holds inputs, maps and filters; `after_codec` the rest.
bool run_ffmpeg(const Options& opt, const std::vector<std::string>& before_codec, const std::vector<std::string>& after_codec,
                bool h265, double fps, const fs::path& cwd, const fs::path& output, std::string& error) {
    const std::string program = ffmpeg_program(opt);
    const std::vector<bool> attempts = opt.soft_encode ? std::vector<bool>{false} : std::vector<bool>{true, false};
    for (size_t i = 0; i < attempts.size(); ++i) {
        const bool hardware = attempts[i];
        std::vector<std::string> cmd = {program, "-hide_banner", "-nostdin", "-y", "-loglevel", opt.verbose ? "warning" : "error"};
        cmd.insert(cmd.end(), before_codec.begin(), before_codec.end());
        const std::vector<std::string> enc = encoder_args(opt, h265, hardware, fps);
        cmd.insert(cmd.end(), enc.begin(), enc.end());
        cmd.insert(cmd.end(), after_codec.begin(), after_codec.end());
        cmd.push_back(to_utf8(output));
        if (opt.verbose) {
            std::string line;
            for (const auto& c : cmd) line += c + " ";
            log_info("running: " + line);
        }
        std::string start_error;
        const int code = run_process(cmd, cwd, start_error);
        if (code == -1) {
            error = start_error + ". FFmpeg is needed for a partial span: pass --ffmpeg, set INSTA_ROLLER_FFMPEG or put ffmpeg on PATH";
            return false;
        }
        std::error_code ec;
        if (code == 0 && fs::exists(output, ec) && fs::file_size(output, ec) > 0) return true;
        if (g_interrupted) { error = "interrupted"; return false; }
        if (hardware && i + 1 < attempts.size()) {
            log_warn("FFmpeg with NVENC failed (exit " + std::to_string(code) + "), retrying with a software encoder");
            continue;
        }
        error = "FFmpeg failed with exit code " + std::to_string(code);
        return false;
    }
    return false;
}

struct NumberedFile {
    uint64_t number;
    fs::path path;
};

// The header does not specify how the SDK names the images of a sequence, so
// nothing is assumed: take every image in the directory and order by the last
// number in the file name (then by name).
std::vector<NumberedFile> collect_images(const fs::path& dir, const std::string& extension) {
    std::vector<NumberedFile> files;
    std::error_code ec;
    for (fs::recursive_directory_iterator it(dir, ec), end; !ec && it != end; it.increment(ec)) {
        if (!it->is_regular_file(ec)) continue;
        const std::string ext = lower(to_utf8(it->path().extension()));
        const bool match = extension == ".jpg" ? (ext == ".jpg" || ext == ".jpeg") : ext == extension;
        if (!match) continue;
        const std::string stem = to_utf8(it->path().stem());
        uint64_t number = 0;
        size_t last = stem.size();
        while (last > 0 && !std::isdigit(static_cast<unsigned char>(stem[last - 1]))) --last;
        size_t first = last;
        while (first > 0 && std::isdigit(static_cast<unsigned char>(stem[first - 1]))) --first;
        if (last > first) number = std::strtoull(stem.substr(first, std::min<size_t>(last - first, 18)).c_str(), nullptr, 10);
        files.push_back({number, it->path()});
    }
    std::sort(files.begin(), files.end(), [](const NumberedFile& a, const NumberedFile& b) {
        return a.number != b.number ? a.number < b.number : a.path < b.path;
    });
    return files;
}

// Deletes a path when it leaves the scope unless told to keep it.
class TempPath {
public:
    TempPath(fs::path path, bool keep) : path_(std::move(path)), keep_(keep) {}
    ~TempPath() {
        if (keep_ || path_.empty()) return;
        std::error_code ec;
        fs::remove_all(path_, ec);
    }
    TempPath(const TempPath&) = delete;
    TempPath& operator=(const TempPath&) = delete;
    const fs::path& path() const { return path_; }

private:
    fs::path path_;
    bool keep_;
};

// ---------------------------------------------------------------------------
// The job
// ---------------------------------------------------------------------------

int run(const Options& opt_in) {
    Options opt = opt_in;
    std::error_code ec;

    const fs::path input = fs::absolute(to_path(opt.input), ec);
    if (ec || !fs::is_regular_file(input, ec)) {
        log_error("input file not found: " + opt.input);
        return kExitFailed;
    }
    const fs::path output = fs::absolute(to_path(opt.output), ec);
    if (ec) {
        log_error("bad output path: " + opt.output);
        return kExitFailed;
    }
    if (fs::equivalent(input, output, ec)) {
        log_error("output would overwrite the input");
        return kExitFailed;
    }
    if (lower(to_utf8(output.extension())) != ".mp4") log_warn("output does not end in .mp4; an MP4 file is written anyway");
    if (output.has_parent_path()) {
        fs::create_directories(output.parent_path(), ec);
        if (ec) {
            log_error("cannot create output directory " + to_utf8(output.parent_path()) + ": " + ec.message());
            return kExitFailed;
        }
    }
    opt.input = to_utf8(input);
    opt.output = to_utf8(output);

    // --- SDK start-up ------------------------------------------------------
    if (opt.gpu >= 0) set_env("CUDA_VISIBLE_DEVICES", std::to_string(opt.gpu));
    ins::SetLogLevel(opt.verbose ? ins::InsLogLevel::INFO : ins::InsLogLevel::ERR);
    ins::InitEnv();
    if (!opt.log_file.empty()) {
        const fs::path log_path = fs::absolute(to_path(opt.log_file), ec);
        if (log_path.has_parent_path()) fs::create_directories(log_path.parent_path(), ec);   // the SDK does not create it
        ins::SetLogPath(to_utf8(log_path));
    }
    const fs::path models = opt.models_dir.empty() ? executable_dir() / "models" : fs::absolute(to_path(opt.models_dir), ec);
    if (!fs::is_directory(models, ec)) {
        const std::string msg = "model directory not found: " + to_utf8(models) + " (model based SDK features will be skipped)";
        if (!opt.models_dir.empty() || opt.stitch_type == ins::STITCH_TYPE::AIFLOW) {
            log_error(msg);
            return kExitFailed;
        }
        if (opt.verbose) log_warn(msg);
    }
    ins::SetModelFileRootDir(with_trailing_separator(models));

    // --- What is in the file ----------------------------------------------
    ins::MediaFileInfo info{};
    if (!ins::GetMediaFileInfo({opt.input}, info)) {
        log_error("the SDK cannot read " + opt.input + " (not an Insta360 camera file, or the file is damaged / incomplete)");
        return kExitFailed;
    }
    if (info.width <= 0 || info.height <= 0 || info.fps <= 0 || info.duration_ms <= 0) {
        log_error("the SDK returned no usable video properties for " + opt.input);
        return kExitFailed;
    }
    const double fps = info.fps;
    const int64_t total_frames = std::max<int64_t>(1, std::llround(info.duration_ms * fps / 1000.0));

    // Native equirect size. X4/X5 files hold two square lens streams (2880 or
    // 3840 px); files that already report a 2:1 frame keep their width.
    int width = opt.width, height = opt.height;
    if (width == 0) {
        width = info.width * 2 > info.height * 3 ? info.width : info.width * 2;
        width -= width % 4;
        height = width / 2;
    }

    // --- Span -> frame indices --------------------------------------------
    if (opt.start_ms >= info.duration_ms) {
        log_error("--start-ms " + std::to_string(opt.start_ms) + " is past the end of the file (" + std::to_string(info.duration_ms) + " ms)");
        return kExitFailed;
    }
    int64_t first_frame = std::llround(opt.start_ms * fps / 1000.0);
    first_frame = std::min(first_frame, total_frames - 1);
    int64_t frame_count = opt.duration_ms < 0 ? total_frames - first_frame
                                               : std::max<int64_t>(1, std::llround(opt.duration_ms * fps / 1000.0));
    if (first_frame + frame_count > total_frames) {
        const int64_t over = first_frame + frame_count - total_frames;
        if (over > 1) log_warn("the span ends " + std::to_string(over) + " frames past the end of the file; it is cut at the end of the file");
        frame_count = total_frames - first_frame;
    }
    const bool whole_file = first_frame == 0 && frame_count >= total_frames;
    const double span_start_s = first_frame / fps;
    const double span_length_s = frame_count / fps;

    enum class Mode { Direct, Frames, Full };
    Mode mode = Mode::Direct;
    if (!whole_file) mode = opt.trim_mode == TrimMode::Full ? Mode::Full : Mode::Frames;
    else if (opt.trim_mode == TrimMode::Frames) mode = Mode::Frames;

    bool h265 = opt.codec == Codec::H265 || (opt.codec == Codec::Auto && width > 4096);
    if (opt.codec == Codec::H264 && width > 4096)
        log_warn("H.264 hardware encoders stop at 4096 px width; " + std::to_string(width) + " px will most likely fail, use --codec h265");

    log_info(std::string("SDK ") + ins::GetVersion() + ", input " + std::to_string(info.width) + "x" + std::to_string(info.height) +
             " @ " + fmt_double(fps, 3) + " fps, " + fmt_double(info.duration_ms / 1000.0, 2) + " s; output " +
             std::to_string(width) + "x" + std::to_string(height) + (h265 ? " H.265" : " H.264") + ", frames " +
             std::to_string(first_frame) + ".." + std::to_string(first_frame + frame_count - 1) + ", mode " +
             (mode == Mode::Direct ? "direct" : mode == Mode::Frames ? "frames" : "full") + ", stabilize " +
             (opt.stabilize == Stabilize::None ? "none" : opt.stabilize == Stabilize::FlowState ? "flowstate" : "direction-lock"));

    Progress progress(opt.progress);
    std::string error;
    fs::remove(output, ec);                   // never leave a stale result behind a failure

    auto fail = [&](const std::string& message) {
        std::error_code ignore;
        fs::remove(output, ignore);
        log_error(message);
        return kExitFailed;
    };
    const fs::path temp_base = opt.temp_dir.empty() ? output.parent_path() : fs::absolute(to_path(opt.temp_dir), ec);
    if (mode != Mode::Direct) {
        fs::create_directories(temp_base, ec);
        if (ec) return fail("cannot create temp directory " + to_utf8(temp_base) + ": " + ec.message());
    }

    if (mode == Mode::Direct) {
        SdkJob job;
        job.output_file = opt.output;
        job.width = width;
        job.height = height;
        job.h265 = h265;
        if (!run_sdk(opt, job, progress, error)) return fail(error);
    }
    else if (mode == Mode::Frames) {
        const bool png = opt.intermediate == ins::IMAGE_TYPE::PNG;
        const std::string ext = png ? ".png" : ".jpg";
        TempPath frames_dir(temp_base / (to_utf8(output.filename()) + ".frames"), opt.keep_temp);
        fs::remove_all(frames_dir.path(), ec);
        fs::create_directories(frames_dir.path(), ec);
        if (ec) return fail("cannot create " + to_utf8(frames_dir.path()) + ": " + ec.message());

        // Rough disk need: about 0.4 byte per pixel for JPEG, 2 for PNG.
        const double need = double(width) * height * (png ? 2.0 : 0.4) * double(frame_count);
        const fs::space_info space = fs::space(frames_dir.path(), ec);
        if (!ec && double(space.available) < need)
            log_warn("frames mode needs roughly " + fmt_double(need / 1e9, 1) + " GB of temporary space in " +
                     to_utf8(frames_dir.path()) + ", only " + fmt_double(double(space.available) / 1e9, 1) + " GB is free");

        SdkJob job;
        job.image_sequence = true;
        job.sequence_dir = with_trailing_separator(frames_dir.path());
        job.frames.reserve(static_cast<size_t>(frame_count));
        for (int64_t i = 0; i < frame_count; ++i) job.frames.push_back(static_cast<uint64_t>(first_frame + i));
        job.width = width;
        job.height = height;
        progress.stage(0, 85);
        if (!run_sdk(opt, job, progress, error)) return fail(error);

        std::vector<NumberedFile> images = collect_images(frames_dir.path(), ext);
        if (images.empty()) return fail("the SDK reported success but wrote no " + ext + " images to " + to_utf8(frames_dir.path()));
        if (static_cast<int64_t>(images.size()) != frame_count)
            log_warn("asked the SDK for " + std::to_string(frame_count) + " frames, got " + std::to_string(images.size()) +
                     "; the output is " + fmt_double(images.size() / fps, 3) + " s long");

        // Give the images a gapless name FFmpeg's image2 reader understands.
        const fs::path seq_dir = frames_dir.path() / "seq";
        fs::create_directories(seq_dir, ec);
        for (size_t i = 0; i < images.size(); ++i) {
            char name[32];
            std::snprintf(name, sizeof(name), "f_%08zu", i);
            fs::rename(images[i].path, seq_dir / (std::string(name) + ext), ec);
            if (ec) return fail("cannot rename " + to_utf8(images[i].path) + ": " + ec.message());
        }

        // Relative pattern + working directory: a '%' somewhere in the cache
        // path cannot break the image2 pattern this way.
        std::vector<std::string> before = {"-framerate", fps_argument(fps), "-start_number", "0", "-i", "f_%08d" + ext};
        if (!opt.no_audio)
            push(before, {"-ss", fmt_double(span_start_s, 6), "-t", fmt_double(images.size() / fps, 6), "-i", opt.input});
        push(before, {"-map", "0:v:0"});
        if (!opt.no_audio) push(before, {"-map", "1:a:0?"});
        // JPEG is full range BT.601; PNG is RGB. Either way: limited range BT.709 out.
        const std::string size = std::to_string(width) + ":" + std::to_string(height);
        push(before, {"-vf", "scale=" + size + (png ? "" : ":in_range=full") +
                                                ":out_range=tv:out_color_matrix=bt709:flags=bicubic,format=yuv420p"});
        std::vector<std::string> after = {"-frames:v", std::to_string(images.size())};
        if (!opt.no_audio) push(after, {"-c:a", "aac", "-b:a", "192k"});
        push(after, {"-movflags", "+faststart"});
        progress.stage(85, 100);
        progress.report(0);
        if (!run_ffmpeg(opt, before, after, h265, fps, seq_dir, output, error)) return fail(error);
    }
    else {  // Mode::Full
        TempPath full(temp_base / (to_utf8(output.stem()) + ".full.tmp.mp4"), opt.keep_temp);
        fs::remove(full.path(), ec);
        SdkJob job;
        job.output_file = to_utf8(full.path());
        job.width = width;
        job.height = height;
        job.h265 = h265;
        progress.stage(0, 85);
        if (!run_sdk(opt, job, progress, error)) return fail(error);
        if (!fs::exists(full.path(), ec) || fs::file_size(full.path(), ec) == 0)
            return fail("the SDK reported success but wrote no video to " + to_utf8(full.path()));

        // -ss in front of -i plus re-encoding: frame accurate cut.
        std::vector<std::string> before = {"-ss", fmt_double(span_start_s, 6), "-i", to_utf8(full.path()), "-map", "0:v:0"};
        if (!opt.no_audio) push(before, {"-map", "0:a:0?"});
        std::vector<std::string> after = {"-frames:v", std::to_string(frame_count), "-t", fmt_double(span_length_s, 6)};
        if (!opt.no_audio) push(after, {"-c:a", "aac", "-b:a", "192k"});
        push(after, {"-movflags", "+faststart"});
        progress.stage(85, 100);
        progress.report(0);
        if (!run_ffmpeg(opt, before, after, h265, fps, fs::path(), output, error)) return fail(error);
    }

    // The SDK signals the end with progress 100; give the muxer a moment in
    // case the file is closed slightly after that.
    bool present = false;
    for (int i = 0; i < 50 && !present; ++i) {
        present = fs::exists(output, ec) && fs::file_size(output, ec) > 0;
        if (!present) std::this_thread::sleep_for(std::chrono::milliseconds(100));
    }
    if (!present) return fail("stitching finished but the output file is missing or empty: " + opt.output);

    progress.stage(0, 100);
    progress.report(100);
    return kExitOk;
}

std::vector<std::string> utf8_arguments(int argc, char** argv) {
    std::vector<std::string> args;
#ifdef _WIN32
    // argv of main() is in the ANSI code page and loses non-ASCII characters;
    // rebuild it from the Unicode command line (as the vendor demo does).
    (void)argc;
    (void)argv;
    int wargc = 0;
    LPWSTR* wargv = CommandLineToArgvW(GetCommandLineW(), &wargc);
    if (wargv) {
        for (int i = 0; i < wargc; ++i) args.push_back(narrow(wargv[i]));
        LocalFree(wargv);
    }
#else
    for (int i = 0; i < argc; ++i) args.emplace_back(argv[i]);
#endif
    return args;
}

}  // namespace

int main(int argc, char** argv) {
#ifdef _WIN32
    SetConsoleOutputCP(CP_UTF8);
#endif
    std::signal(SIGINT, on_signal);
    std::signal(SIGTERM, on_signal);

    const std::vector<std::string> args = utf8_arguments(argc, argv);
    Options options;
    bool want_help = false, want_version = false;
    try {
        options = parse_args(args, want_help, want_version);
    } catch (const UsageError& e) {
        log_error(e.message);
        std::cerr << "Run '" << kTool << " --help' for the list of options." << std::endl;
        return kExitUsage;
    }
    if (want_help) {
        std::cout << kUsage;
        return kExitOk;
    }
    if (want_version) {
        std::cout << kTool << " 1.0 (Insta360 MediaSDK " << ins::GetVersion() << ")" << std::endl;
        return kExitOk;
    }
    try {
        return run(options);
    } catch (const std::exception& e) {
        log_error(std::string("unexpected failure: ") + e.what());
    } catch (...) {
        log_error("unexpected failure");
    }
    return kExitFailed;
}
