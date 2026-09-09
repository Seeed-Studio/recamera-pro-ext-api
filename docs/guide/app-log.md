# 应用日志（app.log）：运行期环形轮转 + Web 查看

> 事实来源：`market/appmgr/supervisor.py`（`_app_log_pump` / `_write_log_chunk` /
> `_shift_log_generations` / `read_log_span` / `_log_tail` / `start` / `stop` /
> `_terminate_proc`）、`market/appmgr/server.py`（`do_v1_logs`）、
> `market/appmgr/paths.py`（`logdir`）。引用以**函数名**为主，行号会随重构漂移。

## 定位（先读这段）

每个托管应用的 stdout/stderr 由 appmgr 接管，写入 `<app_dir>/logs/app.log`。
appmgr 是进程主管（supervisor），应用子进程的 stdout/stderr **不再直接是一个文件
fd**，而是接到一根匿名管道上，由 appmgr 的后台 drain 线程把管道里的字节写进
`app.log` 并**在应用运行期间实时轮转**。

- 应用自己不写 `app.log`、也不持有它的 inode —— 所以 appmgr 能在运行期重命名/
  轮转它，真正实现环形存储。
- 设备长期运行也不再无限增长：单文件达上限就滚一代，最老的丢弃。
- Web 的"应用日志"页读的是 `app.log`（必要时跨到 `app.log.1` 补足），永远有完整
  窗口，不会被轮转切空。

## 配置（环境变量，免改代码）

| 变量 | 默认 | 含义 |
|---|---|---|
| `APPMGR_APP_LOG_MAX_BYTES` | `2097152`（2 MiB） | 单个日志文件上限。设 `0` 关闭轮转（pump 只追加、不限长） |
| `APPMGR_APP_LOG_BACKUPS` | `3` | 保留的历史代数。设 `1` 则只留 `app.log` + `.1` |

**占用上限**：

- 单应用：`(backups + 1) × max_bytes` = 默认 `4 × 2 MiB = 8 MiB`。
- 默认 9 个应用 → 全局日志 ≤ `72 MiB`。

文件布局（`<app_dir>/logs/`）：

```
app.log       当前文件，≤ 2 MiB（web 读这个，必要时补 .1）
app.log.1     上一代，≤ 2 MiB
app.log.2
app.log.3     最老；下一次轮转时被丢弃
```

## 写入链路

```
 应用进程 (leader)
   │ stdout/stderr → pipe 写端 (fd1)
   ▼
 os.pipe()
   │                         父进程 start() 创建后立即 os.close(write_fd)
   │                         （否则父进程持写端 → pipe 永不 EOF）
   ▼
 pump 线程 _app_log_pump(read_fd, logpath, max_bytes, backups)
   │ os.fdopen(read_fd,"rb",buffering=0)  ← 无缓冲，一有字节就读
   │ open(logpath,"ab",buffering=0)        ← 追加、无缓冲
   ▼
 pipe.read(16384)   阻塞到有数据 / EOF
   │
   ▼
 _write_log_chunk(logf, logpath, chunk, max_bytes, backups)
   │  max_bytes<=0  → 直接 write 整块（不封顶）
   │  否则按 max_bytes 切片：
   │     cur = getsize(app.log); room = max_bytes - cur
   │     room<=0 → 关文件、_shift_log_generations、重开空 app.log
   │     take = min(room, 剩余); write chunk[offset:offset+take]
   ▼
 app.log  写满 2 MiB 即滚一代，永不超限（连一个 chunk 的越界都不漏）
```

### 关键不变量

- **`app.log` 严格 ≤ `max_bytes`**：`_write_log_chunk` 写前算 `room`，一次只写
  `room` 字节，到边界就停、轮转、重开空文件。
- **单块可跨多次轮转**：一个 16 KiB 的 chunk 若卡在边界，会先写满旧文件、轮转、
  再写新文件，突发日志也不丢字节。
- **轮转零拷贝**：`_shift_log_generations` 只用 `os.replace` 重命名，不复制。
- **全 best-effort**：磁盘满 / 只读 / 卸载竞争 → `try/OSError` 跳过轮转继续追加，
  绝不阻断启动、绝不反向杀死被监管应用或 appmgr。

## 运行期轮转机制

`_shift_log_generations(logpath, backups)`（纯路径重命名，无 size 判断——判断在
`_write_log_chunk` 里做过了）：

```
unlink app.log.N            丢最老
app.log.(N-1) → .N
...
app.log.1   → .2
app.log     → .1            os.replace，原子
# app.log 此刻不存在；pump 立刻 open 一个新的空 app.log
```

### 为什么必须用 pipe，不能让子进程直接写文件

旧实现把 `app.log` 的 fd 直接当子进程 stdout，于是子进程持有该 inode 整个生命周期，
运行期重命名 `app.log` 也回收不了它仍在写的那块 → 单次超长运行仍会无限增长。改成
pipe 后，子进程不再碰 `app.log`，重命名即回收，运行期可真正环形。

### pipe 的 EOF 与生命周期

`close_fds=True`（Py3 默认）下，子进程的 fd1 = pipe 写端，而 ffmpeg 等 helper 会继承
该 stdout —— 所以 pipe 只有在**整个进程组退出**后才 EOF。这正是 `stop()` /
`_terminate_proc()` 在 `killpg` 之后才 join pump 的原因：组死了 → 所有写端持有者死了
→ pipe EOF → pump 把最后缓冲字节 drain 完再退出。

## Web 读取（`do_v1_logs`）

路由：`GET /api/app-center/v1/apps/<id>/logs?tail=N`（`server.py` 的 `do_v1_logs`，
经 nginx 暴露）。

行为：

- 调 `supervisor.read_log_span(app_id, 512*1024)`：先读 `app.log` 末尾 512 KiB；
  若不足 512 KiB，从 `app.log.1` 末尾补足。
- 取末尾 `tail` 行（默认 200，最大 2000）。
- 返回 `{id, lines[], text}`。

**跨轮转边界补足**的意义：请求恰好命中轮转后那一刻（`app.log` 刚被清空），若只读
`app.log` 会看到很少几行；`read_log_span` 从 `app.log.1` 补足，窗口始终满 512 KiB
（老的 `.1` 在前、新的 `app.log` 在后）。附带也兜住了轮转瞬间 `app.log` 短暂不存在
的亚毫秒空窗 —— `app.log` 缺失时直接用 `app.log.1` 填满。

### `read_log_span` vs `_log_tail`（刻意不同）

| 函数 | 读哪些文件 | 用途 | 为什么 |
|---|---|---|---|
| `read_log_span` | `app.log` + `app.log.1` | Web 日志查看 | 跨轮转补足，窗口始终满 |
| `_log_tail` | 只 `app.log` | 启动失败报错（`_startup_failure`） | **不跨文件**，避免把上一轮日志混进本次启动失败原因 |

`_log_tail` 故意单文件：启动失败信息只该反映"这一轮为什么没起来"，`app.log.1` 是
上一轮的，混进来会误导排查。

## 生命周期钩子（pump 的起止）

| 时机 | 动作 | 位置 |
|---|---|---|
| `start()` | `os.pipe()` → 子进程 stdout 接写端 → 父进程 `os.close(write_fd)` → 起 pump 线程并登记进 `_log_pumps[app_id]` | `start()` |
| 启动前 | `_rotate_app_log`：上一轮残留 `app.log` 超 cap 才挪到 `.1`（size-gated，小的留在原地由 pump 续写） | `start()` |
| 运行期 | pump 持续 `read→write→轮转` | `_app_log_pump` |
| `stop()` | `killpg` 整组 → `reap`/`drain_exits` → `_join_log_pump`（组死→EOF→drain 尾字节→join 2s） | `stop()` |
| `_terminate_proc()` | 同上，启动失败收尾时保证最后日志落盘 | `_terminate_proc()` |
| `_startup_failure()` | 子进程已退出时 `_join_log_pump(timeout=0.5)` 尽力 drain 崩溃 traceback，再 `_log_tail` 读末尾 1500 字节进报错信息 | `_startup_failure()` |

`_join_log_pump` 从 `_log_pumps` 弹出该线程并 `join`（超时即让 daemon 线程自生自灭，
不阻塞生命周期调用）。`_log_pumps` 是 appmgr 进程内字典，不跨进程。

## 竞态与边界（诚实标注）

- **轮转瞬间亚毫秒空窗**：`os.replace(app.log, .1)` 到 pump `open(app.log)` 之间，
  `app.log` 短暂不存在。web 请求恰好命中 → `read_log_span` 改读 `app.log.1` 填满，
  不会报错、不会空返回。可忽略。
- **pipe-leak 已规避**：父进程不在 `Popen` 后 `os.close(write_fd)` 的话，父进程一直
  持写端，子进程死了 pipe 也不 EOF，pump 永不退出。`start()` 里关了。
- **helper 持写端**：ffmpeg 继承了 app 的 stdout（pipe 写端），所以 leader 先死时
  pump 不会立即 EOF，要等 helper 也死。靠 `stop()`/`_terminate_proc()` kill 整个组
  解决；`_startup_failure` 在 leader 已退出但 helper 可能还在时只做 0.5s 尽力 join。
- **pump 是 daemon 线程**：appmgr 自身崩溃不会拖累已运行的应用（应用继续写 pipe；
  pipe 缓冲满会阻塞应用写，但 appmgr 通常在）。
- **轮转失败不致命**：`_shift_log_generations` 任一步 `OSError` 都跳过，pump 继续往
  当前 `app.log` 追加 —— 最坏情况是该次轮转没滚成、文件继续长，但不会让应用或
  appmgr 崩。

## 故障排查

| 现象 | 看哪里 |
|---|---|
| 应用启动失败、要看崩溃原因 | `app.log` 末尾（`_log_tail` 已拼进 `SupervisorError` 的 `last log:`），或 Web 日志页 |
| 日志没写进 `app.log` | pump 线程是否起了？`_log_pumps[app_id]`；管道是否被父进程误留写端（`start()` 必 `os.close(write_fd)`） |
| `app.log` 仍在无限增长 | `APPMGR_APP_LOG_MAX_BYTES` 是否被设成 `0`/负数；`backups` 是否 `<1`（此时超限直接删，不留历史） |
| Web 日志显示很少 | 是否正好命中轮转后；`read_log_span` 应已从 `.1` 补足——若仍少，查 `app.log.1` 是否存在/可读 |
| 想看更早的历史 | `app.log.2` / `app.log.3`（web 不读这两代，需 ssh 直接看） |

## 与旧实现的差异

| | 旧（直接 fd 追加） | 新（pipe + pump 实时轮转） |
|---|---|---|
| 子进程持有 `app.log` inode | 是 | 否（写 pipe） |
| 运行期可轮转 | 否（重命名回收不了 inode） | 是 |
| 长期运行是否无限增长 | 是 | 否，单文件 ≤ 2 MiB、应用 ≤ 8 MiB |
| Web 读取 | 只 `app.log` 末尾 512 KiB | `app.log` + 跨 `.1` 补足 512 KiB |
| 单次超长运行封顶 | 不能 | 不能（子进程 owns 期内仍受 pipe 缓冲约束，但落盘文件被轮转封顶） |
