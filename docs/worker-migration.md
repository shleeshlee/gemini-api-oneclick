# 保留旧 worker 的逐账号迁移

新 worker 必须重新认证，不能恢复或搬运旧进程内存。候选登录失败时保留旧进程和路由；验证成功后只切换该账号的新请求。不要同时重启全部 worker。

## 修复范围

- Cookie 部署、写回、删除和重载使用共同凭据锁；重载读取文件与替换客户端在同一临界区。旧刷新结果校验实例身份。
- 运行期关闭的 worker 客户端不再由库隐式重新初始化。认证失败和关闭状态明确返回错误，由账号恢复入口处理。
- 流式错误用 `data: {"error": ...}` 发送，不伪装成普通回答及正常 stop。网关识别流内失败，不记成功；失败不写回 Cookie。
- 媒体响应头统一安全编码；上传临时文件在成功、异常和取消路径清理。
- `/worker/*` 管理接口使用 `WORKER_MANAGEMENT_KEY`，未设置时回退 API_KEY；均为空则拒绝管理请求。网关发送对应 Bearer 凭据。

## 存储与路由

新旧 worker 使用不同的 envs、state 和代码目录。候选只装载明确选择的账号，不复制整个 envs 目录。旧实例仍可能写回它自己的文件，这不会覆盖候选文件。迁移后的新 Cookie 更新发往所选 worker，原目录的同编号文件不再是该账号当前凭据来源。

网关代码可放在新目录；上线脚本通过 `GATEWAY_CONFIG_FILE`、`GATEWAY_ENVS_DIR`、`GATEWAY_STATE_DIR`、`GATEWAY_DATA_DIR` 指向原服务的配置、账号列表、状态和媒体数据，保留原有面板设置及图库。无需覆盖新代码目录中的示例配置。未设置这些变量时仍使用代码目录下的原默认路径。

网关 `state/worker-routes.json` 原子保存账号目的地、图片编辑会话和研究任务的来源。健康、使用量、恢复、部署、删除、日志均按账号选择 worker。切换接口为 `POST /gateway/worker-route/{num}`，需要面板认证，body 为 `{"worker_url":"http://127.0.0.1:17860"}`。地址必须列入 `WORKER_CANDIDATE_URLS`，或是已有目的地/默认 worker。接口先检查候选认证和模型，再发一条简短聊天，全部成功才保存并切换；失败不关闭旧客户端。

已有图片编辑会话的普通请求和后台任务都固定在原 worker，失败不换实例重放。研究结果查询按创建实例处理；首次升级前的旧研究任务可在已知 worker 中查找。未知或过期的图片编辑会话返回明确 409，不偷偷重建另一段对话。

旧版网关没有持久化图片会话路由和后台生成任务；首次升级无法补回已丢失的映射。升级网关前应让正在执行的网关请求/任务结束，旧编辑会话失去映射时需重新上传素材。网关重启不结束 worker 登录态，但不是请求层面的零中断保证。新版本之后记录的图片/研究归属可在网关重启后恢复。

## 候选启动和切换

代码先提交推送，在服务器独立目录拉取。沿用已有 Docker 镜像内依赖，挂载新代码；不修改当前 worker 的代码挂载目录。

```sh
python3 scripts/worker-rollout.py --source /path/to/candidate-code --serving /path/to/serving start \
  --runtime /path/to/candidate-runtime --slot 2 --exclude 7 --port 17860 --name gemini_worker_candidate
```

该命令拒绝覆盖已有 runtime、拒绝选择排除账号、拒绝共享服务中目录。只复制列出的凭据，文件 0600，候选只监听 127.0.0.1。代理配置沿用旧容器，自动续期仍关闭，缓存独立。

候选初始化最多包含 60 秒启动错峰及 60 秒初始化总时限，清理另有短时限。先检查候选健康，不要将启动容器当成认证成功。

```sh
python3 scripts/worker-rollout.py --source /path/to/candidate-code --serving /path/to/serving gateway \
  --pid CURRENT_GATEWAY_PID --worker-url http://127.0.0.1:17860
python3 scripts/worker-rollout.py --source /path/to/candidate-code --serving /path/to/serving switch \
  --slot 2 --worker-url http://127.0.0.1:17860
```

gateway 子命令复用原面板配置/账号目录/状态，先检查空闲和新模块可导入，只替换指定 gateway 进程，不对旧 worker 发停止/重启命令。检查之后仍可能有新请求进入，旧网关最多等待 660 秒排空，超时则结束旧网关再完成交接；不能承诺此时仍在途的请求不中断。新网关启动失败或未通过就绪检查时会结束候选网关、重新启动原网关，并验证恢复就绪。端口允许列表写入原配置，后续重启保持。

保留旧 worker，直到没有账号的新请求指向它、已有图片会话到期且研究任务与在途请求结束。新 worker 提供 `POST /worker/retire-slot/{num}` 阻止新请求/刷新/写回，保留客户端供已开始任务收尾；退休后不再接受新的续编辑请求，因此必须在编辑会话排空后使用。退休不会自动关闭进程，旧版本 worker 不支持此接口。

## 验证

```sh
python -m unittest discover -s tests -v
node tests/test_panel_login.cjs
```

回归覆盖凭据写回/部署/删除/取消交错、旧刷新污染、隐式重登、错误流、媒体响应头、临时文件、管理认证、候选失败不切换、账号路由持久化、普通与后台编辑归属、研究查询归属及部署目录隔离。完整 AST 快照测试使用 Python 3.14。
