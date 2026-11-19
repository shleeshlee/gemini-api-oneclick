# Worker 登录失败与单账号恢复

Worker 初始化遇到错误后结束本次尝试，由面板展示结果。失败不会递归初始化，也不会由后续聊天请求暗中触发重新登录。

| 情况 | Worker 状态 | 面板与请求结果 |
| --- | --- | --- |
| Gemini 页面跳到其他域名 | auth_failed | 需要重新认证；不从登录页提取 Gemini 会话字段 |
| 初始化收到 HTTP 401 / 403 | auth_failed | 保留实际 HTTP 状态；面板显示认证失败 |
| 账号状态为 UNAUTHENTICATED | auth_failed | 显示需要重新认证 |
| 初始化超过总时限 | init_timeout | 显示初始化超时；该账号请求返回 504 |
| 其他初始化异常 | init_failed | 展示失败，不一直显示登录中 |

总时限为 60 秒，覆盖登录、初始化 RPC 与能力探测。取消后的连接清理另有短时限。启动时原有的 5–60 秒错峰等待发生在初始化计时之前。初始化内部 RPC 直接把错误交回初始化调用者，避免持有初始化锁时再次尝试获取同一把锁。

面板的“恢复此账号”调用 `/worker/reload-slot/{num}`。Worker 返回 HTTP 202，表示已受理，最终结果通过健康检查更新。每个账号最多有一个初始化任务；重复恢复不会另开任务。恢复只关闭并重建所选账号客户端。更新 Cookie 同样异步验证，面板先显示“Cookie 已保存，正在验证登录”；初始化中的账号拒绝再次覆盖 Cookie。删除账号先取消并等待该账号任务清理。

这些修改不会开启 `auto_refresh` 或 RotateCookies，也不能使 Google 已拒绝的凭据自动恢复有效。重启进程会销毁内存客户端，新进程仍需初始化；此次修复保证失败有结果、有时限、能单独恢复，并通过 Cookie 审计保留后续排查证据。

## 加载修改

按仓库流程推送并在服务器拉取。当前 worker 和 gateway 进程需要重新加载代码，面板需要刷新。首次加载 worker 修改仍会终止全部现有内存会话；单账号恢复只在新代码已经加载后生效。不能将提交、推送或拉取成功当作线上修复已生效。

## 离线验证

测试使用模拟响应与临时凭据，不连接 Google、不操作真实账号：

```sh
python -m unittest discover -s tests -p test_login_recovery.py -v
python -m unittest discover -s tests -v
node tests/test_panel_login.cjs
```

完整测试中的旧媒体 AST 快照需要 Python 3.14。面板测试验证认证失败、超时、初始化中和错误文本转义。
