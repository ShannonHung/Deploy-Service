
# File Descriptor 是什麼？

Linux 有一句很經典：

> Everything is a file.

在 Linux 裡：
- socket
- pipe
- terminal
- 檔案
- SSH connection

本質上都會被表示成：File Descriptor (FD)
FD 就是一個：小整數 ID

例如：
```bash
0 = stdin
1 = stdout
2 = stderr
3 = socket
4 = socket
5 = file
```

SSH 連線其實也是 FD

當你：`await asyncssh.connect(...)`

底層其實：
```bash 
TCP socket
↓
Linux socket fd
↓
event loop 監控它
``` 

所以：100 個 SSH connection ≈ 100+ 個 file descriptors
如果：stdout pipe, stderr pipe, internal pipe, DNS socket 一起算進去。
可能：100 requests ≈ 300~600 FDs 都有可能。

FD 為什麼會耗盡？
Linux 每個 process 有 FD 上限，可以用下面的指令
```
# 查詢 單一 process 最多只能開多少 FD
ulimit -n 

# 觀察目前 shell 有多少 FD
ls /proc/$$/fd
``` 

# Event Loop 

FD 跟 Event Loop 是綁在一起的，因為event loop 本質上就是在監控 FD
- 你 SSH connection 越多：event loop 要監控的 socket 越多
- 為什麼會 Event Loop 過載？因為每個 SSH task 都包含：
  - socket read/write
  - stdout buffering
  - stderr buffering
  - timeout task
  - coroutine state
  - Future objects
  - retry/callback

這些都會吃：
- memory
- CPU scheduling
- event loop queue

