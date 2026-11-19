# Worker Cookie 审计

Worker 自动审计 Gemini 会话 HTTP 请求及 Cookie 写回，不开启自动续期、不调用 RotateCookies、不恢复旧会话、不改变模型或认证策略。accounts 模式未启用此审计。

## 查看

在仓库目录运行：

```sh
python3 scripts/cookie-audit.py --slot 9
python3 scripts/cookie-audit.py --state-dir state --slot 9 --limit 100 --json
```

日志位于 `ONECLICK_STATE_DIR/cookie-audit/events.jsonl`（worker 默认 `/app/state`），宿主机对应 `state/cookie-audit/`。单文件 5 MiB，保留 5 个轮转文件，合计约 30 MiB；文件权限 0600。脚本不读取凭证或指纹密钥。

## 排查链

1. `credentials_loaded`：实际传入的两项 Cookie 指纹、当时文件指纹、与上次文件检查点是否一致。首次运行没有历史时结果为 null，不冒充一致。`client_id` 区分同一进程中的多次初始化，`boot_id` 区分进程。
2. `http_start` / `http_response` / `http_error`：每次 HTTP 请求独立 `request_id`，同一次初始化/API 调用/模型刷新使用共同 `operation_id`。包括请求前后 Cookie 容器指纹、按名称/作用域标识的新增/变化/删除、HTTP 状态、脱敏路由、耗时与该响应 Cookie 指纹。错误或取消前已接收到的 Cookie 变化也会记录。
3. `cookie_writeback`：written / unchanged / throttled / lock_busy / no_client / no_env_path / invalid_cookie_pair / incomplete_env / failed / request_failed。written 同时带内存候选值、旧文件、新文件的指纹；节流会记录剩余等待时间。写回策略仍只保存原有两项 Cookie，保持 600 秒间隔及原文件锁。
4. 下次启动再次记录 `credentials_loaded`，与最后观察或写回的文件版本比较。匹配只证明这两项凭据版本一致，不代表 Google 一定接受登录。

`api_complete.transport_success` 只表示 HTTP/ASGI 传输是否完成，不等于模型成功；HTTP 200 的 SSE 内部错误另记 `chat_stream_error`。成功流式请求也会产生 HTTP 与写回审计。

并发请求共享 Cookie 容器，因此 `changes` 是共享容器在请求期间观察到的差异；`overlapped=true` 时不能把全部差异归因于这一条响应，应结合 `response_cookies` 和时间顺序。会话重载后，旧请求仍保留旧 client_id；写回期间实例被替换会标 `client_changed_during_write`。

## 凭证保护与限制

Cookie 值只进入 HMAC-SHA256 计算，不写入日志。名称保留，domain/path 以作用域指纹表示；HTTP 查询串、正文、原始请求头、Set-Cookie、异常消息均不保存。独立随机密钥放在 `fingerprint.key`，权限 0600，随 state 卷持久化。不要将该密钥公开或提交仓库。更换密钥后，跨密钥的历史比较为未知，不能据此声称 Cookie 改了。

审计存储不可用会输出不含凭据的 warning，业务仍按原路径执行。审计只从启用时开始；过去被过滤的 Cookie 不能补回。HTTP 记录针对 Gemini 会话客户端，独立媒体下载器不在这个凭据审计范围内。

## 上线与验证

新增模块放在现有 `lib/gemini_webapi` 挂载下，不新增依赖或容器挂载。按仓库流程拉取后，需要重启 worker 才加载新代码。唯一 worker 重启会终止全部现有内存会话；在账号认证异常期间应先明确安排启用时间，不能把代码已拉取当作审计已启用。

离线测试不读取真实 env、不访问 Google：

```sh
python -m unittest discover -s tests -p test_cookie_audit.py -v
python -m unittest discover -s tests -v
```

旧媒体 AST 哈希快照使用 Python 3.14 的 AST 展示格式，全套快照测试应使用 3.14；新审计测试也在生产同系列 Python 3.12 上验证。此功能不修复既存的初始化重入死锁，但会在进入死锁前留下真实 HTTP 401 和登录跳转证据。
