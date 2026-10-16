# File Sender

File Sender 是一个 AstrBot 文件发送插件，用于在 OneBot v11（aiocqhttp）适配器下通过 NapCat HTTP API 发送 AstrBot 本地文件。

当前版本仅支持 NapCat 作为 OneBot 协议端。后续版本计划扩展支持更多 OneBot 协议端。

## 功能特性

- 支持 AstrBot 与 NapCat 分容器、分机器部署时发送本地文件
- 支持私聊文件、群文件、QQ群临时文件发送
- 支持 `/sendfile` 指令手动发送文件
- 支持 LLM 工具 `send_local_file` 与 `upload_file_to_user`
- 支持 `astrbot_file_write_tool` 写出的 workspace 相对路径，例如 `test.txt`
- 支持拦截当前 OneBot 会话中其他插件通过标准结果链或 `context.send_message` 发送的本地文件，并改走 NapCat 上传流程
- 支持多个上传请求并发处理，每个文件上传使用独立 stream_id
- 支持 NapCat HTTPS 自签名证书：可配置 PEM CA 证书路径，或在内网环境关闭证书校验

## 支持平台

仅支持 OneBot v11（aiocqhttp），且当前仅适配 NapCat HTTP API。

其他 OneBot 协议端暂未支持。后续版本会逐步扩展更多 OneBot 端。

多适配器部署时，插件只会在 OneBot v11（aiocqhttp）会话中引导 LLM 使用文件发送工具；非 OneBot 会话不会调用本插件，会继续走 AstrBot 默认流程。

## 配置说明

### enable_llm_tool

是否允许 LLM 自动调用文件发送工具。

建议需要让模型生成并发送文件时开启。

### onebot_cross_device_api_base_url

NapCat HTTP 服务地址，例如：

```text
http://napcat:3000
```

这里必须填写 NapCat 的 HTTP 地址，不是 AstrBot 地址。

### onebot_cross_device_api_token

NapCat HTTP 服务启用 token 时填写；未启用 token 可留空。

### onebot_cross_device_stream_chunk_size

文件分片上传块大小，默认 `524288` 字节。

### onebot_cross_device_ssl_verify

是否校验 NapCat HTTPS 证书，默认 `true`。

当 NapCat 使用自签名证书时，可配合 `onebot_cross_device_ssl_ca_bundle` 指定 PEM CA 证书文件，或在内网可信环境下关闭本项跳过证书校验（不推荐用于公网）。

### onebot_cross_device_ssl_ca_bundle

NapCat 自签名 CA 证书路径，可选。

填写 PEM 格式 CA 证书文件的绝对路径，配合 `onebot_cross_device_ssl_verify=true` 使用即可信任自签名证书。留空则使用系统默认 CA。

## 使用指令

```text
/sendfile <文件路径> [文件名] [target_type] [target_user_id] [target_group_id]
```

参数说明：

- `文件路径`：必填。可填写绝对路径，或当前会话 workspace 下的相对路径。
- `文件名`：可选。发送时展示的文件名。
- `target_type`：可选，支持 `auto`、`private`、`group`、`temp`。
- `target_user_id`：可选。私聊目标 QQ 号。
- `target_group_id`：可选。群文件或 QQ 群临时文件目标群号。

目标类型：

- `auto`：按当前会话自动选择私聊或群文件。
- `private`：发送私聊文件，使用 `target_user_id`。
- `group`：发送群文件，使用 `target_group_id`。
- `temp`：发送 QQ 群临时文件，按群文件接口上传到 `target_group_id`。

示例：

```text
/sendfile test.txt 测试文件.txt
/sendfile /AstrBot/data/workspaces/_FriendMessage_<用户ID>/test.txt 测试文件.txt private <用户ID>
/sendfile /tmp/demo.zip demo.zip group "" <群号>
/sendfile /tmp/demo.zip demo.zip temp "" <群号>
```

## LLM 工具

开启 `enable_llm_tool` 后，插件会注册两个工具：

- `send_local_file`
- `upload_file_to_user`

两个工具参数相同：

- `file_path`：必填，本地文件路径或 workspace 相对路径
- `file_name`：可选，发送时展示的文件名
- `target_type`：可选，`auto/private/group/temp`
- `target_user_id`：可选，OneBot 用户 ID
- `target_group_id`：可选，OneBot 群号

当 LLM 使用 `astrbot_file_write_tool` 写出 `test.txt` 这类相对路径时，可以直接把 `test.txt` 传给 File Sender。插件会优先从当前会话 workspace 中查找该文件。

## 工作流程

1. AstrBot 读取本地文件。
2. 插件调用 NapCat `upload_file_stream` 分片上传。
3. NapCat 返回协议端可访问的文件路径。
4. 私聊文件调用 `upload_private_file`。
5. 群文件和 QQ 群临时文件调用 `upload_group_file`。

更新日志见 [CHANGELOG.md](./CHANGELOG.md)。

## 常见问题

### 提示未配置 onebot_cross_device_api_base_url

请在插件配置中填写 NapCat HTTP 服务地址，例如 `http://napcat:3000`。

### 提示 ENOENT

如果日志中出现 NapCat 读取 `/AstrBot/data/workspaces/...` 或 `/tmp/...` 失败，通常说明文件被内置 `send_message_to_user` 的 file 消息直接发送了，而不是通过 File Sender 上传。

请确认：

- 当前会话是 OneBot v11（aiocqhttp）。
- 已开启 `enable_llm_tool`。
- LLM 调用的是 `send_local_file` 或 `upload_file_to_user`。

### 非 OneBot 会话能用吗？

不能。本插件只支持 OneBot v11（aiocqhttp）。非 OneBot 会话不会调用本插件，会继续走 AstrBot 默认流程。

### NapCat 使用自签名 HTTPS 证书时怎么办？

在插件配置中：

- `onebot_cross_device_ssl_verify` 保持为 `true`。
- `onebot_cross_device_ssl_ca_bundle` 填写 NapCat 自签名 CA 证书（PEM）的绝对路径，例如 `/etc/napcat/ca.pem`。

如果在内网可信环境下，也可以将 `onebot_cross_device_ssl_verify` 关闭跳过证书校验，但不要在公网环境下使用。

### 提示 HTTPS 证书校验失败

错误信息形如 `NapCat HTTPS 证书校验失败：...`。说明 NapCat 提供了自签名或不被系统信任的证书。请：

1. 导出 NapCat 使用的 PEM 格式 CA 证书。
2. 在插件配置中把 `onebot_cross_device_ssl_ca_bundle` 指向该证书。
3. 重启 AstrBot 让配置生效。

如果证书格式不是 PEM，会提示 `加载 NapCat CA 证书失败`，请用 `openssl x509 -in ca.crt -out ca.pem -outform PEM` 转码后重试。
