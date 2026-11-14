# 用一个真实软件学 408 —— MeetLite 源码讲义

> 教材不是伪代码，是 5,500 行能跑在你机器上的代码。
> 每个知识点都能落到 `文件:函数`，能改、能打断点、能看着它坏。
> 对应你的课程材料：`学校/课程/计网`、`课程/数据库`、`已考完/算法复习`、`课程/软工`；操作系统与组成原理按 408 统考大纲补。

---

## 0 为什么这个软件适合当教材

它有 408 四门课各自最典型的形态：

| 课程 | 软件里的对应物 | 代码位置 |
|---|---|---|
| 数据结构 | 生产者-消费者队列、环形缓冲、哈希订阅表、B+ 树索引 | `server.py` `App`、`capture.py` `_process`、`store.py` `SCHEMA` |
| 操作系统 | 多线程 + 锁、子进程 + 管道、进程组回收、文件原子替换 | `server.py` `_transcribe_loop`/`_watch_parent`、`trash.py`、`platforms.py` |
| 计算机网络 | 自己实现 HTTP 服务端、multipart、SSE 长连接、Range 断点续传 | `server.py` `Handler`、`whisper_engine.transcribe`、`updater.py` |
| 计算机组成原理 | PCM 采样、字节序、4-bit 量化、缓存局部性、SIMD、内存带宽 | `audiodec.py`、`capture.py`、`llm.py`、`models.json` |
| 数据库（你在学） | 表设计、索引、事务、WAL 的反例 | `store.py` |
| 软件工程（你在学） | 分层、接口契约、版本与更新、错误处理 | 全项目 |

---

## 第 1 章 数据结构

### 1.1 队列：两种完全不同的用法

`server.py` 里有两个队列，长得一样，语义相反：

```python
self.jobs: queue.Queue = queue.Queue()          # 无界，任务队列
...
q: queue.Queue = queue.Queue(maxsize=500)       # 有界，消息邮箱
```

- `jobs` 是**任务队列**：`start_recording` 把 `(mid, start_ms, end_ms, pcm)` 丢进去，`_transcribe_loop` 单线程取出转写。这是**生产者-消费者**：音频线程产、ASR 线程消，中间用队列解耦，生产者永远不会被 8 秒的识别阻塞。
- 订阅队列是**消息邮箱**：每个打开 SSE 的浏览器标签页对应一个 `Queue(maxsize=500)`。

考点对接：队列的 FIFO 性质在这里不是装饰——它保证**转写结果按时间顺序落库**。如果换成栈，后录的段会先写进 `segments`，时间轴就乱了。

### 1.2 环形缓冲：`deque(maxlen=8)`

`capture.py` 判段时有个细节：VAD 从"检测到人声"那一刻开始录，但人声的**起音**（清辅音、p/t/k）往往在能量门限之下，会被切掉。

```python
self.preroll = deque(maxlen=8)   # 8 帧 × 50ms = 400ms 预卷
```

`deque(maxlen=N)` 就是**环形缓冲**：append 到第 9 个元素时自动从头部丢弃。空间 O(N) 固定，插入 O(1)，天然实现"最近 N 帧"。

考点对接：这是循环队列的一个变体——不比 `head/tail` 取模，但语义相同：**固定窗口的滑动历史**。手写循环队列时你会用 `q[(tail+1)%N]` 判满；`deque(maxlen)` 把这个判断封进了 C 实现。

### 1.3 哈希表：订阅表

```python
self._subs: dict[str, list[queue.Queue]] = {}   # meeting_id -> [q1, q2, ...]
```

一个会议可能同时被多个窗口订阅（你开了两个标签页）。用 `dict` 做**多值映射**：键是 `meeting_id`（字符串，`uuid4().hex[:12]`），值是队列列表。

考点对接：哈希表的**平均 O(1) 查找、最坏 O(n) 冲突链**。这里键是 12 位十六进制，实际冲突概率可以忽略；但如果键换成"会议标题"，中文标题哈希分布差 + 重名，冲突链会长——这正是哈希函数要选好的原因。

### 1.4 索引：为什么 `segments` 要建 `(meeting_id, start_ms)` 复合索引

```sql
CREATE INDEX IF NOT EXISTS idx_seg_meeting ON segments(meeting_id, start_ms);
```

查询是"取某个会议的所有段，按开始时间排"：

```sql
SELECT * FROM segments WHERE meeting_id=? ORDER BY start_ms
```

没有索引 → 全表扫描 + 排序，O(n log n)。
有复合索引 → B+ 树先按 `meeting_id` 定位到一段连续叶子，**这段叶子本身已按 `start_ms` 有序**，所以 `ORDER BY` 直接免掉。

考点对接：**最左前缀原则**。`(meeting_id, start_ms)` 能加速 `WHERE meeting_id=?`，也能加速 `WHERE meeting_id=? ORDER BY start_ms`，但**不能**加速单独的 `WHERE start_ms > ?`。这就是复合索引列序不能随便换的原因。

### 1.5 复杂度：`_normalize` 为什么是 O(n) 两趟

```python
def _normalize(pcm, target=0.85, max_gain=6.0):
    peak = max(abs(s) for s in samples)   # 第一趟：找峰值
    gain = min(max_gain, target * 32767 / peak)
    return [int(s * gain) for s in samples]  # 第二趟：整体缩放
```

必须先看完全部样本才知道峰值 → 无法一趟完成（除非缓存倍增区间）。两趟 O(n) 优于一趟 O(n log n)（排序求分位数）。

---

## 第 2 章 操作系统

### 2.1 线程 vs 进程：这个软件两个都用

- **线程**：`ThreadingHTTPServer` 每个请求一个线程；`_transcribe_loop`、`_trash_tick`、`_watch_parent`、`_warm` 都是 `threading.Thread(daemon=True)`。
- **进程**：`whisper-server`、`llama-server`、`capture`（ffmpeg/loopcap）都是 `subprocess.Popen`。

为什么不全都用线程？Python 有 **GIL**：同一时刻只有一个线程执行字节码。推理是 C++ 的重计算，放在线程里也只在 C 层释放 GIL 时才能并行；而放成独立进程，可以**独立占满多核**（`llm_threads=6`）、独立崩溃、独立限内存。

考点对接：进程 = 资源分配单位（独立地址空间），线程 = CPU 调度单位（共享地址空间）。这里的选择标准是"要不要共享 Python 堆 + 要不要独立多核"。

### 2.2 锁：`RLock` 与 `Lock` 的区别

```python
# store.py
self._lock = threading.RLock()          # 可重入
# server.py
self._subs_lock = threading.Lock()      # 不可重入
```

- `RLock`：同一线程可重复 acquire（计数 +1），因为 `create_meeting` 内部可能调用另一个也加锁的方法。用 `Lock` 就会**自死锁**。
- `Lock`：订阅表只在"遍历列表、append/remove"时短暂持有，不会重入，用更轻的 `Lock`。

`sqlite3.connect(..., check_same_thread=False)` 是必须的：连接对象创建在主线程，但被多个请求线程使用。SQLite 的连接默认绑定创建线程；关掉检查后，**正确性完全由这把 `RLock` 保证**——这是"库把并发安全交给调用者"的典型设计。

### 2.3 生产者-消费者与背压

```python
def _transcribe_loop(self):
    while True:
        item = self.jobs.get()
        ...
```

`jobs` 无界 → 生产快于消费时内存会涨。真实系统这里应设 `maxsize`，满了让生产者阻塞（背压）或丢帧。**这是一个可以拿去做实验的缺陷**：把 `maxsize=4` 加上，观察长会议时 `capture` 线程是否被反压、音频是否丢。

### 2.4 子进程、管道、以及"为什么必须写 stdin"

```python
p = subprocess.Popen(argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE, ...)
```

`audiocap`（macOS 的 ScreenCaptureKit 采集器）内部有"stdin EOF 就退出"的看门狗。如果父进程不把 `stdin` 设成 `PIPE`，子进程继承的是 `/dev/null`，**立刻读到 EOF 就退出**——表现为"刚开始录就结束"。

考点对接：`fork` 后文件描述符被继承；`/dev/null` 的 read 立即返回 0 = EOF。这是"描述符继承"最实用的一课。

同理，`stdout=PIPE` 让子进程的 PCM 字节流回到 Python；读端必须**持续读**，否则管道缓冲区（通常 64KB）写满后子进程 `write` 阻塞 → 采集卡死。

### 2.5 孤儿进程与 `_watch_parent`

```python
def _watch_parent():
    while True:
        if os.getppid() != start_ppid:
            # 父进程没了，自杀
            os._exit(0)
```

双击启动时，`.app` 外壳（Swift/WebView2）拉起 `server.py`。如果外壳被强杀，`server.py` 会变成 `ppid=1` 的**孤儿进程**，继续占端口。所以脚本自己每 2 秒比对一次 ppid。

考点对接：Unix 的孤儿被 `init`/`launchd` 收养；`os.getppid()` 变化是检测"父死"的经典手法（比 `signal(SIGCHLD)` 简单，比 `getppid` 缓存可靠）。

### 2.6 文件的原子替换：`os.replace`

```python
tmp = path.with_suffix(".part")
tmp.write_text(json.dumps(snap))
os.replace(tmp, path)      # 同一文件系统内，原子
```

`os.replace` 在 POSIX 上是 `rename(2)`：**要么旧文件、要么新文件，不会出现半个文件**。但如果先 `unlink` 再 `write`，中间断电 → 快照丢了。

考点对接：文件系统的一致性靠"重命名是原子操作"这一条。数据库的 WAL、编辑器的"先写 .swp 再 rename"都是同一招。

### 2.7 端口与 loopback

```python
srv = ThreadingHTTPServer(("127.0.0.1", port), Handler)
```

只绑 `127.0.0.1` 而不是 `0.0.0.0` → 外部网络无法连接，这是**最小暴露面**。

考点对接：`bind` 的地址决定监听范围；`0.0.0.0` = 所有网卡。端口 `8787`/随机端口由外壳传入，`SO_REUSEADDR`（Python 默认开）让重启后不必等 TIME_WAIT。

---

## 第 3 章 计算机网络

### 3.1 手写一个 HTTP 服务端，看清报文结构

```python
self.send_response(200)                       # 状态行 HTTP/1.1 200 OK
self.send_header("Content-Type", ct)          # 首部
self.send_header("Content-Length", str(len(data)))
self.end_headers()                            # 空行
self.wfile.write(data)                        # 实体
```

请求侧的关键是 `Content-Length`：

```python
n = int(self.headers.get("Content-Length") or 0)
body = self.rfile.read(n)
```

**没有 Content-Length 就不知道读多少字节**——HTTP/1.1 里这是定界问题；没有它就退化到"读到连接关闭"（HTTP/1.0 行为）。

考点对接：TCP 是字节流，没有消息边界。应用层必须自己定界：`Content-Length`（定长）、`Transfer-Encoding: chunked`（分块）、连接关闭（EOF）。SSE 用的是第四种：**用 `\n\n` 做应用层分隔**。

### 3.2 multipart/form-data：手工拼边界

```python
boundary = "----MeetLite" + uuid.uuid4().hex
body = (
    f"--{boundary}\r\n"
    f'Content-Disposition: form-data; name="file"; filename="chunk.wav"\r\n'
    f"Content-Type: audio/wav\r\n\r\n"
).encode() + wav + f"\r\n--{boundary}--\r\n".encode()
```

要点：
- **边界必须唯一**（uuid），否则音频数据里若出现同串会被误切；
- 部件之间 `\r\n--boundary`，结束是 `--boundary--`；
- 二进制部分**不能**做任何编码转换（不能按文本处理）。

考点对接：MIME 的 `Content-Type` 与 `boundary` 参数；这是"应用层协议如何传输二进制"的最小案例。

### 3.3 SSE：一个永不结束的 HTTP 响应

```python
self.send_response(200)
self.send_header("Content-Type", "text/event-stream; charset=utf-8")
self.send_header("Cache-Control", "no-cache, no-transform")
self.send_header("Connection", "close")
...
while True:
    ev = q.get(timeout=15)
    self.wfile.write(("data: " + json.dumps(ev) + "\n\n").encode())
    self.wfile.flush()
```

关键三点：
1. **首部之后不结束**：`Content-Length` 不设，连接保持，浏览器按事件流解析；
2. **必须 flush**：不 flush 数据留在用户态缓冲，前端看不到实时转写；
3. **`q.get(timeout=15)` 发心跳** `: ping\n\n`——防止中间设备（代理/NAT）把空闲连接判死。注释行（以 `:` 开头）会被 EventSource 忽略。

前端一行：

```js
const es = new EventSource('/api/events?meeting_id=' + id);
```

考点对接：HTTP 是无状态请求-响应，SSE 用"响应不结束"实现服务端推送；与 WebSocket 相比，SSE 走纯 HTTP、单向（服务器→浏览器）、自动重连。

### 3.4 Range 断点续传

```python
req.add_header("Range", f"bytes={have}-")     # 从已下载位置继续
```

服务器若支持，返回 `206 Partial Content` + `Content-Range`；否则回 `200` 全量，客户端必须**丢掉半截文件重下**。

考点对接：HTTP/1.1 的 Range 请求、206 状态码；这也是多线程下载（把文件切段并发取）的基础。

再看测速择优：

```python
req = urllib.request.Request(url, headers={"Range": f"bytes=0-{nbytes-1}"})
```

只取前 256KB 测速——**小样本 RTT/吞吐估计**，避免为选镜像而下整包。

### 3.5 镜像、DNS、TLS、CDN 缓存

`updater.py` 并行拉 jsDelivr 4 个 CDN、raw.githubusercontent、api.github.com，取版本最高者。它踩的坑全是网络课的活教材：

- **DNS 解析失败 vs TCP 超时 vs TLS 握手失败**：`urllib` 抛的都是 `URLError`，但要区分必须看 `reason`；
- **CDN 缓存**：`raw.githubusercontent.com` 对同一 URL 缓存数分钟，`?t=` 查询串**不一定**能绕（边缘节点按路径缓存），于是出现"仓库里已是 1.2.0，raw 还返回旧内容"；
- **TLS 与代理**：走 `gh-proxy.com` 是"代理转发"，客户端 TLS 终止在代理，证书是代理的——这就是为什么企业网里 HTTPS 检查会换证书。

考点对接：`ping`/`traceroute` 到 `dig`/`nslookup` 的分层排查顺序：**DNS → TCP 三次握手 → TLS → HTTP 状态码 → 应用数据**。这正好接上你计网资料里"分析题模板 A：ping 全过程"。

### 3.6 子网与 NAT 的实践视角

`bind("127.0.0.1")` 属于 `127.0.0.0/8` 回环；`0.0.0.0` 代表"本机所有接口"。如果你在家里通过路由器访问另一台机器上的服务，就是 NAT 转换表 + 端口映射——对应你复习资料里"模板 10 NAT 转换表"。

---

## 第 4 章 计算机组成原理

### 4.1 采样、量化、字节序：PCM 是什么

```python
RATE = 16000      # 每秒 16000 个采样点
# s16le = signed 16-bit little-endian
```

- **采样**：奈奎斯特，16kHz 能无损表示 ≤8kHz；人声 300–3400Hz（电话带宽）足够；
- **量化**：16 bit → 65536 级，信噪比约 6.02×16 ≈ 96 dB；
- **字节序**：little-endian，低字节在前。`0x1234` 存成 `34 12`。跨平台（x86/ARM 都是小端，但网络协议是大端）必须明确。

`audiodec.py` 的重采样：

```python
def _resample_py(pcm, rate):
    # 线性插值：y = y0 + (y1-y0) * t
```

考点对接：采样率转换 = 重采样，最简单的线性插值只做算术平均，会有高频混叠；正规做法是先低通滤波再抽取（下采样）/插零再低通（上采样）。

### 4.2 4-bit 量化：Q4_K_M 到底省了什么

`models.json` 里 Qwen 是 `Q4_K_M`，1.28 GB；FP16 同参数量约 4 bit ×? —— 2B 参数 FP16 要 **4 GB**，Q4 只要 **约 1.2 GB**。

- 权重从 16 bit 压到 4 bit（带 per-block scale），**内存带宽需求降到 1/4**；
- 推理是**访存密集型**（memory-bound），不是算力密集型：每读一个权重只做一次乘加，瓶颈在"从内存搬到 ALU"的速度；
- 所以量化几乎线性提速，而算力（FLOPS）远未打满。

`whisper` 的 `q5_0` 同理（5 bit）。这就是"为什么你的笔记本能跑，而不是需要 A100"。

考点对接：存储层次（寄存器→L1→L2→L3→主存→SSD）的**带宽/延迟差几个数量级**；模型推理的性能模型 `时间 ≈ 参数量 × 位宽 / 内存带宽`。

### 4.3 缓存局部性与分块（map-reduce）

`summarize.py` 把长转写切成 3000 字符块、重叠 200，逐块摘要再归并。这是**算法层的分块**，和 CPU 缓存行（64B）的"分块利用局部性"是同一个思想：一次只处理能放进"工作集"的规模。

上下文窗口 8192 token 就是"能放进缓存的工作集"；超出就得分块。

### 4.4 多线程与 SIMD

`llama-server -t 6` 用 6 个线程做矩阵乘，每个线程内部用 **NEON/AVX** 做向量化（一条指令算 8/16 个 float）。whisper.cpp 同样。

考点对接：SIMD = 单指令多数据；`-t` 是线程级并行（MIMD），两者叠加。超线程 ≠ 双核：共享执行单元，通常只提升 20–30%。

### 4.5 端到端时延的分解

`tools/bench.py` 实测：55.7s 音频 → 识别 15.9s（RTF 0.287）→ 纪要 59.4s / 914 字。

RTF（real-time factor）= 处理时间 / 音频时长。< 1 表示能实时。

时延链：**采集（缓冲 + VAD 静音判定 1.2s）→ 编码 WAV → HTTP multipart → 识别 → 分块摘要（多次 LLM 调用）→ 落库**。每一段都可以单独测，这就是"性能分析先分解再优化"。

---

## 第 5 章 数据库（你在学的那门）

### 5.1 表设计：为什么不用一张大表

```sql
meetings(id, title, created_at, duration_ms, status)
segments(id, meeting_id, start_ms, end_ms, text, lang)
summaries(meeting_id, template, content)
notes(meeting_id, content, updated_at)
```

一张大表会有两个问题：**更新异常**（改会议标题要改 N 行）与**空值冗余**（没纪要的会议也占 content 列）。拆表后 `meetings` 是主表，`segments` 是 1:N 明细，`summaries/notes` 是 1:1 扩展。

考点对接：**范式**。这里至少到 2NF/3NF——非主属性完全依赖主键、不传递依赖。注意 `summaries` 用 `meeting_id` 做主键 = 一对一，不是 1:N。

### 5.2 `update_meeting` 的动态 SQL 与注入

```python
cols = ", ".join(f"{k}=?" for k in fields)
sql = f"UPDATE meetings SET {cols} WHERE id=?"
```

列名无法参数化（参数化只作用于**值**），所以拼列名是必要的；安全性靠**白名单**：`fields` 的键来自内部代码，不是用户输入。

考点对接：SQL 注入的两种位置——值（用 `?` 参数化解决）与标识符（只能白名单）。如果这里把 `k` 换成请求里的键名，立刻可注入。

### 5.3 事务与锁：SQLite 的 `check_same_thread=False` + `RLock`

`sqlite3` 默认 autocommit（每条语句一个事务）。这里没有显式 `BEGIN`，靠"单条语句原子" + 应用层 `RLock` 串行化。

**这是一个真实缺陷，正好当反例**：整个项目 `grep PRAGMA` 零命中——没设 `journal_mode=WAL`、没有 `busy_timeout`。后果：
- 读写互斥（默认 rollback journal），长写会阻塞读；
- 多进程（比如你同时开了两个 MeetLite 实例指向同一数据目录）容易 `database is locked`。

**改进实验**（强烈建议你亲手做）：

```python
self._conn.execute("PRAGMA journal_mode=WAL")
self._conn.execute("PRAGMA synchronous=NORMAL")
self._conn.execute("PRAGMA busy_timeout=5000")
```

然后对比：并发读写吞吐、崩溃后 `-wal` 文件的恢复行为。这就是"日志式恢复"的活体实验。

考点对接：WAL 把随机写变成顺序追加；`synchronous=NORMAL` 在 WAL 下只保证"进程崩溃不丢"，断电可能丢最后几个事务——**durability 与性能的权衡**。

### 5.4 回收站：软删除的另一种实现

用户要"删除保留 7 天"。实现不是加 `deleted_at` 列，而是**导出 JSON 快照**再硬删（`trash.py` + `store.snapshot/restore_snapshot`）。

为什么？因为是"整条会议"删除，涉及 4 张表的级联；快照 = 一个可读文件，恢复时能"原 id 被占用就换新 id"。

考点对接：软删除（标记）vs 归档（导出）。前者省 IO 但要改所有查询加 `WHERE deleted_at IS NULL`；后者实现简单但恢复要重建外键。

---

## 第 6 章 软件工程（你在学的那门）

- **分层**：`config`（配置）/ `platforms`（平台差异）/ `store`（持久化）/ `capture`（采集）/ `whisper_engine`+`llm`（推理）/ `summarize`（业务）/ `server`（接口）/ `updater`（运维）。**平台差异全部收在 `platforms.py` 一个文件**，这是移植的关键决策。
- **接口契约**：`capture` 只要输出"16k mono s16le 的字节流"，mac 用 Swift 的 audiocap、Windows 用 C++ 的 loopcap.exe，上层代码一行不改。这就是"面向接口编程"的实际收益。
- **错误处理**：`_route` 全局 `except Exception → 500`，`BrokenPipeError` 单独吞掉（浏览器关闭 SSE 时必然发生，不是错误）。
- **版本与更新**：`APP_VERSION` + `update.json` + 三镜像 + sha256 校验 + 原子替换 + 回滚。
- **测试**：`/api/*` 全部可用 `curl` 驱动；`tools/bench.py` 是性能回归。

---

## 7 两周学习路线（每天 1–2 小时）

| 天 | 读 | 做 |
|---|---|---|
| 1 | `store.py` 全 221 行 | 用 `sqlite3` CLI 打开库，`EXPLAIN QUERY PLAN` 看索引是否命中 |
| 2 | `server.py` `Handler` 部分 | `curl -v` 打自己接口，抄下完整请求/响应报文 |
| 3 | `server.py` `_sse` + 前端 `EventSource` | 打开页面 → `curl -N http://127.0.0.1:PORT/api/events` 看事件流 |
| 4 | `capture.py` 的 VAD | 改 `vad_rms` 到 0.05，观察切段变化 |
| 5 | `audiodec.py` | 用 Python 生成 440Hz 正弦 wav，重采样后画波形 |
| 6 | `llm.py` | 用 `curl` 直接打 llama-server，对比 `--reasoning-budget 0` 有无 |
| 7 | `updater.py` | 用本地 HTTP 服务器模拟镜像，故意改错 sha 看是否拒绝 |
| 8 | 操作系统：改 `jobs` 为 `maxsize=4` | 录长音频，观察背压/丢帧 |
| 9 | 数据库：加 WAL PRAGMA | 两进程并发写，对比 before/after |
| 10 | 计网：`tcpdump`/`nettop` | 抓一次上传，看 multipart 在 TCP 里的样子 |
| 11 | 组成原理：`models.json` | 算 Q4 与 FP16 的带宽差，和实测 RTF 对照 |
| 12 | 全项目 | 自己加一个功能（如"按说话人分段"的骨架） |

---

## 8 三个"改坏它"的实验（最能学到东西）

1. **把 `_transcribe_loop` 改成多线程消费** → 观察 `segments` 顺序错乱、`store` 锁竞争。理解"顺序"是队列给的，不是数据库给的。
2. **把 `_sse` 的 `flush()` 删掉** → 前端转写不再实时，缓冲区满才一次性出现。理解用户态缓冲。
3. **把 `os.replace` 换成 `write_text` 直接写** → 在写入过程中 `kill -9`，看快照是否变成半个 JSON。理解原子性。

---

*本讲义对应代码版本 1.2.0；行号会随版本漂移，请以函数名为锚点搜索。*