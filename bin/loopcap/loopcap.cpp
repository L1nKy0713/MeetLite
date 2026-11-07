// loopcap — Windows 音频采集器（WASAPI loopback + 麦克风）
// 输出：16000 Hz / 单声道 / signed 16-bit little-endian 裸 PCM 到 stdout
// 用法：
//   loopcap.exe                 采集默认输出设备的回环 + 默认麦克风，混音
//   loopcap.exe --mode mic      仅麦克风
//   loopcap.exe --mode system   仅系统声（回环）
//   loopcap.exe --mode mix      两路混音（默认）
//   loopcap.exe --rate 16000    输出采样率
//   loopcap.exe --list          列出设备后退出
//   loopcap.exe --check         自检：能否取到默认端点，成功打印 ok 并退出 0
#include <windows.h>
#include <mmdeviceapi.h>
#include <audioclient.h>
#include <functiondiscoverykeys_devpkey.h>
#include <cstdarg>
#include <cstdlib>
#include <fcntl.h>
#include <io.h>
#include <cstdio>
#include <cstring>
#include <string>
#include <vector>
#include <atomic>
#include <thread>

#define SAFE_RELEASE(p) do { if (p) { (p)->Release(); (p) = nullptr; } } while (0)

static std::atomic<bool> g_stop(false);
static int g_outRate = 16000;

static void logf_(const char* fmt, ...) {
    va_list ap; va_start(ap, fmt);
    vfprintf(stderr, fmt, ap);
    va_end(ap);
    fputc('\n', stderr);
    fflush(stderr);
}

static std::string w2u(const wchar_t* w) {
    if (!w) return "";
    int n = WideCharToMultiByte(CP_UTF8, 0, w, -1, nullptr, 0, nullptr, nullptr);
    if (n <= 0) return "";
    std::string s(n - 1, '\0');
    WideCharToMultiByte(CP_UTF8, 0, w, -1, &s[0], n, nullptr, nullptr);
    return s;
}

// 单声道路由：把任意格式转 float mono，再线性重采样到 g_outRate
struct Resampler {
    double inRate = 48000.0;
    double pos = 0.0;
    float prev = 0.f;
    bool  started = false;
    std::vector<float> buf;

    explicit Resampler(double r) : inRate(r) {}

    void push(const float* x, int n) {
        if (n <= 0) return;
        if (!started) { started = true; prev = x[0]; pos = 0.0; }
        const double step = inRate / (double)g_outRate;
        while (pos + 1.0 < (double)n) {
            int i = (int)pos;
            double f = pos - (double)i;
            float a = (i < 0) ? prev : x[i];
            float b = x[i + 1];
            buf.push_back(a + (b - a) * (float)f);
            pos += step;
        }
        pos -= (double)n;
        prev = x[n - 1];
    }
    void drain(std::vector<float>& out) {
        out.insert(out.end(), buf.begin(), buf.end());
        buf.clear();
    }
};

struct Stream {
    IAudioClient*      client = nullptr;
    IAudioCaptureClient* cap = nullptr;
    WAVEFORMATEX*      fmt = nullptr;
    Resampler*         rs = nullptr;
    bool               isFloat = true;
    int                channels = 2;
    bool               loopback = false;

    bool open(IMMDevice* dev, bool isLoopback, std::vector<std::string>& errs) {
        loopback = isLoopback;
        HRESULT hr = dev->Activate(__uuidof(IAudioClient), CLSCTX_ALL, nullptr, (void**)&client);
        if (FAILED(hr)) { errs.push_back("Activate 失败"); return false; }
        hr = client->GetMixFormat(&fmt);
        if (FAILED(hr)) { errs.push_back("GetMixFormat 失败"); return false; }

        DWORD flags = isLoopback ? AUDCLNT_STREAMFLAGS_LOOPBACK : 0;
        // 100ms 缓冲；事件驱动比轮询稳，但 loopback 在静音时不产出数据，用轮询
        hr = client->Initialize(AUDCLNT_SHAREMODE_SHARED, flags,
                                1000000, 0, fmt, nullptr);
        if (FAILED(hr)) { errs.push_back("Initialize 失败 (0x" + std::to_string((unsigned long)hr) + ")"); return false; }
        hr = client->GetService(__uuidof(IAudioCaptureClient), (void**)&cap);
        if (FAILED(hr)) { errs.push_back("GetService 失败"); return false; }
        isFloat = (fmt->wFormatTag == WAVE_FORMAT_IEEE_FLOAT);
        if (!isFloat && fmt->wFormatTag == WAVE_FORMAT_EXTENSIBLE && fmt->cbSize >= 22) {
            // KSDATAFORMAT_SUBTYPE_IEEE_FLOAT = {00000003-0000-0010-8000-00aa00389b71}
            const GUID* g = &((WAVEFORMATEXTENSIBLE*)fmt)->SubFormat;
            isFloat = (g->Data1 == 0x00000003 && g->Data2 == 0x0000 && g->Data3 == 0x0010 &&
                       g->Data4[0] == 0x80 && g->Data4[1] == 0x00 && g->Data4[2] == 0x00 &&
                       g->Data4[3] == 0xAA && g->Data4[4] == 0x00 && g->Data4[5] == 0x38 &&
                       g->Data4[6] == 0x9B && g->Data4[7] == 0x71);
        }
        channels = fmt->nChannels > 0 ? fmt->nChannels : 1;
        rs = new Resampler((double)fmt->nSamplesPerSec);
        return true;
    }

    // 抓当前所有包，转 mono float 交给重采样器
    void pump(std::vector<float>& mono) {
        if (!cap) return;
        UINT32 packet = 0;
        while (cap->GetNextPacketSize(&packet) == S_OK && packet > 0) {
            BYTE* data = nullptr; UINT32 frames = 0; DWORD sflags = 0;
            if (FAILED(cap->GetBuffer(&data, &frames, &sflags, nullptr, nullptr))) break;
            if (frames) {
                mono.clear();
                mono.reserve(frames);
                if (sflags & AUDCLNT_BUFFERFLAGS_SILENT) {
                    mono.assign(frames, 0.f);
                } else if (isFloat) {
                    const float* p = (const float*)data;
                    for (UINT32 i = 0; i < frames; i++) {
                        float s = 0.f;
                        for (int c = 0; c < channels; c++) s += p[i * channels + c];
                        mono.push_back(s / (float)channels);
                    }
                } else {
                    const short* p = (const short*)data;
                    for (UINT32 i = 0; i < frames; i++) {
                        float s = 0.f;
                        for (int c = 0; c < channels; c++) s += (float)p[i * channels + c] / 32768.f;
                        mono.push_back(s / (float)channels);
                    }
                }
                rs->push(mono.data(), (int)mono.size());
            }
            cap->ReleaseBuffer(frames);
        }
    }

    void close() {
        if (client) client->Stop();
        SAFE_RELEASE(cap);
        SAFE_RELEASE(client);
        if (fmt) { CoTaskMemFree(fmt); fmt = nullptr; }
        if (rs) { delete rs; rs = nullptr; }
    }
};

static IMMDevice* pick(IMMDeviceEnumerator* en, EDataFlow flow, const wchar_t* id) {
    IMMDevice* dev = nullptr;
    if (id && *id) {
        if (FAILED(en->GetDevice(id, &dev))) dev = nullptr;
        if (dev) return dev;
    }
    if (FAILED(en->GetDefaultAudioEndpoint(flow, eConsole, &dev))) dev = nullptr;
    return dev;
}

static std::wstring device_id_of(IMMDevice* dev) {
    LPWSTR id = nullptr;
    if (dev && SUCCEEDED(dev->GetId(&id)) && id) {
        std::wstring s(id); CoTaskMemFree(id); return s;
    }
    return L"";
}

static std::string json_escape(const std::string& in) {
    std::string o; o.reserve(in.size() + 8);
    for (unsigned char c : in) {
        if (c == '"' ) o += "\\\"";
        else if (c == '\\') o += "\\\\";
        else if (c < 0x20) { char b[8]; snprintf(b, sizeof b, "\\u%04x", c); o += b; }
        else o += (char)c;
    }
    return o;
}

// 输出 JSON：[{index, flow, name, id}]，供 Python 端 json.loads
static int list_devices() {
    IMMDeviceEnumerator* en = nullptr;
    if (FAILED(CoCreateInstance(__uuidof(MMDeviceEnumerator), nullptr, CLSCTX_ALL,
                                __uuidof(IMMDeviceEnumerator), (void**)&en))) {
        logf_("无法创建设备枚举器");
        return 1;
    }
    EDataFlow flows[2] = { eRender, eCapture };
    int idx = 0;
    bool first = true;
    printf("[\n");
    for (int f = 0; f < 2; f++) {
        IMMDeviceCollection* col = nullptr;
        if (FAILED(en->EnumAudioEndpoints(flows[f], DEVICE_STATE_ACTIVE, &col))) continue;
        UINT32 n = 0; col->GetCount(&n);
        for (UINT32 i = 0; i < n; i++) {
            IMMDevice* d = nullptr;
            if (FAILED(col->Item(i, &d)) || !d) continue;
            IPropertyStore* ps = nullptr;
            std::string name = "(未命名)";
            if (SUCCEEDED(d->OpenPropertyStore(STGM_READ, &ps)) && ps) {
                PROPVARIANT v; PropVariantInit(&v);
                if (SUCCEEDED(ps->GetValue(PKEY_Device_FriendlyName, &v)) && v.vt == VT_LPWSTR)
                    name = w2u(v.pwszVal);
                PropVariantClear(&v);
                ps->Release();
            }
            std::string id = w2u(device_id_of(d).c_str());
            printf("%s{\"index\": %d, \"flow\": \"%s\", \"name\": \"%s\", \"id\": \"%s\"}",
                   first ? "" : ",\n", idx,
                   flows[f] == eRender ? "render" : "capture",
                   json_escape(name).c_str(), json_escape(id).c_str());
            first = false;
            idx++;
            d->Release();
        }
        SAFE_RELEASE(col);
    }
    printf("\n]\n");
    fflush(stdout);
    SAFE_RELEASE(en);
    return 0;
}

static int self_check() {
    IMMDeviceEnumerator* en = nullptr;
    HRESULT hr = CoCreateInstance(__uuidof(MMDeviceEnumerator), nullptr, CLSCTX_ALL,
                                  __uuidof(IMMDeviceEnumerator), (void**)&en);
    if (FAILED(hr)) { logf_("check: 枚举器创建失败 hr=0x%08lx", (unsigned long)hr); return 1; }
    IMMDevice* out = nullptr; IMMDevice* in = nullptr;
    bool okOut = SUCCEEDED(en->GetDefaultAudioEndpoint(eRender, eConsole, &out)) && out;
    bool okIn  = SUCCEEDED(en->GetDefaultAudioEndpoint(eCapture, eConsole, &in)) && in;
    logf_("check: 输出端点 %s，输入端点 %s", okOut ? "ok" : "缺失", okIn ? "ok" : "缺失");
    SAFE_RELEASE(out); SAFE_RELEASE(in); SAFE_RELEASE(en);
    if (okOut || okIn) { printf("ok\n"); fflush(stdout); return 0; }
    return 2;
}

int main(int argc, char** argv) {
    std::string mode = "mix";
    std::wstring devOut, devIn;
    bool wantList = false, wantCheck = false;
    for (int i = 1; i < argc; i++) {
        std::string a = argv[i];
        if (a == "--list") wantList = true;
        else if (a == "--check") wantCheck = true;
        else if (a == "--mode" && i + 1 < argc) mode = argv[++i];
        else if (a == "--rate" && i + 1 < argc) g_outRate = atoi(argv[++i]);
        else if (a == "--device" && i + 1 < argc) { devOut = devIn = L"x"; }
        else if (a == "--out-device" && i + 1 < argc) { std::string s = argv[++i]; devOut.assign(s.begin(), s.end()); }
        else if (a == "--in-device" && i + 1 < argc) { std::string s = argv[++i]; devIn.assign(s.begin(), s.end()); }
    }
    if (g_outRate < 8000 || g_outRate > 192000) g_outRate = 16000;

    HRESULT hr = CoInitializeEx(nullptr, COINIT_MULTITHREADED);
    if (FAILED(hr)) { logf_("CoInitializeEx 失败"); return 1; }
    if (wantList) { int r = list_devices(); CoUninitialize(); return r; }
    if (wantCheck) { int r = self_check(); CoUninitialize(); return r; }

    _setmode(_fileno(stdout), _O_BINARY);
    _setmode(_fileno(stderr), _O_BINARY);

    IMMDeviceEnumerator* en = nullptr;
    hr = CoCreateInstance(__uuidof(MMDeviceEnumerator), nullptr, CLSCTX_ALL,
                          __uuidof(IMMDeviceEnumerator), (void**)&en);
    if (FAILED(hr)) { logf_("无法创建设备枚举器"); return 1; }

    std::vector<Stream*> streams;
    bool ok = false;
    const bool sys = (mode == "mix" || mode == "system");
    const bool mic = (mode == "mix" || mode == "mic");

    if (sys) {
        IMMDevice* d = pick(en, eRender, devOut == L"x" ? nullptr : devOut.c_str());
        if (d) {
            Stream* s = new Stream();
            std::vector<std::string> errs;
            if (s->open(d, true, errs)) streams.push_back(s);
            else { for (auto& e : errs) logf_("系统声: %s", e.c_str()); delete s; }
            d->Release();
        } else logf_("系统声: 没有可用的输出设备");
    }
    if (mic) {
        IMMDevice* d = pick(en, eCapture, devIn == L"x" ? nullptr : devIn.c_str());
        if (d) {
            Stream* s = new Stream();
            std::vector<std::string> errs;
            if (s->open(d, false, errs)) streams.push_back(s);
            else { for (auto& e : errs) logf_("麦克风: %s", e.c_str()); delete s; }
            d->Release();
        } else logf_("麦克风: 没有可用的输入设备");
    }
    SAFE_RELEASE(en);

    if (streams.empty()) { logf_("没有可采集的音源"); CoUninitialize(); return 2; }

    for (auto* s : streams) {
        HRESULT sh = s->client->Start();
        if (FAILED(sh)) logf_("Start 失败 hr=0x%08lx", (unsigned long)sh);
        else ok = true;
    }
    if (!ok) { for (auto* s : streams) s->close(); CoUninitialize(); return 3; }

    logf_("开始录音");

    // stdin 关闭即退出（父进程消失时 Windows 会关掉管道）
    std::thread watchdog([] {
        char c;
        while (!g_stop.load()) {
            int n = (int)fread(&c, 1, 1, stdin);
            if (n <= 0) { g_stop.store(true); break; }
        }
    });
    watchdog.detach();

    std::vector<float> mono;
    std::vector<float> mix;
    std::vector<short> pcm;
    mono.reserve(8192); mix.reserve(8192); pcm.reserve(8192);

    const int framesPerChunk = g_outRate / 20;   // 50ms
    int sinceWrite = 0;
    const double gain = (streams.size() > 1) ? 0.62 : 1.0;

    while (!g_stop.load()) {
        bool got = false;
        mix.clear();
        for (auto* s : streams) {
            s->pump(mono);
            std::vector<float> out;
            s->rs->drain(out);
            if (out.empty()) continue;
            got = true;
            if (mix.size() < out.size()) mix.resize(out.size(), 0.f);
            for (size_t i = 0; i < out.size(); i++) mix[i] += out[i] * (float)gain;
        }
        if (!got) { Sleep(8); continue; }

        pcm.clear();
        pcm.reserve(mix.size());
        for (float v : mix) {
            if (v > 1.f) v = 1.f; else if (v < -1.f) v = -1.f;
            pcm.push_back((short)(v * 32767.f));
        }
        if (!pcm.empty()) {
            fwrite(pcm.data(), sizeof(short), pcm.size(), stdout);
            sinceWrite += (int)pcm.size();
            if (sinceWrite >= framesPerChunk) { fflush(stdout); sinceWrite = 0; }
        }
        Sleep(5);
    }

    fflush(stdout);
    for (auto* s : streams) { s->close(); delete s; }
    CoUninitialize();
    logf_("结束录音");
    return 0;
}
