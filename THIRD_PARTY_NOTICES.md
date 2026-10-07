# 第三方组件

## qBittorrent / libtorrent

- qBittorrent 上游、版权与许可（GPL-2.0-or-later）：https://github.com/qbittorrent/qBittorrent/tree/release-5.2.4
- libtorrent：https://github.com/arvidn/libtorrent （BSD-3-Clause）。
- 安装器固定 qBittorrent 5.2.4 / libtorrent 2.0.15，使用第三方 `userdocs/qbittorrent-nox-static` 静态构建，不是上游官方 Linux 二进制。
- 构建脚本、依赖及源码说明：https://github.com/userdocs/qbittorrent-nox-static
- 发布：https://github.com/userdocs/qbittorrent-nox-static/releases/tag/release-5.2.4_v2.0.15
- ARM64 文件：`aarch64-qbittorrent-nox`
- SHA-256：`f12e821d5782c39e3093d788689182e9ac823fa01b3a82d6c00fdb993e4428d8`

源码包不携带 qBittorrent 二进制，安装时下载并核对固定哈希。重新分发含核心的包时，必须同时遵守 qBittorrent、Qt、libtorrent、OpenSSL 及静态链接依赖的许可、版权通知和对应源码提供义务。

## aria2

- 上游项目：https://github.com/aria2/aria2
- 本安装器使用版本：1.37.0。
- 许可：GNU GPL v2 或更高版本，原文与版权见 https://github.com/aria2/aria2/blob/release-1.37.0/COPYING 。
- 对应上游源码：https://github.com/aria2/aria2/tree/release-1.37.0 。

## 静态构建

安装器从第三方 `abcfy2/aria2-static-build` 发布页下载 Linux ARM64 静态构建，并不代表 aria2 官方发布的二进制或小米官方插件。

- 构建项目、脚本与依赖信息：https://github.com/abcfy2/aria2-static-build
- 固定发布：https://github.com/abcfy2/aria2-static-build/releases/tag/1.37.0
- 文件：`aria2-aarch64-linux-musl_static.zip`
- SHA-256：`0c681a89a40e0f82d1f5137608e86257eb0af201459c002941ea098f2b8c26b6`

当前源码安装包不携带二进制核心；安装时从上述来源下载。重新分发包含核心的安装包时，应遵守 aria2 及静态链接依赖的许可、版权通知和对应源码提供义务，不应仅转发剥离许可信息的二进制。

网页客户端桥接和安装流程基于本工作区已有的小米智能存储插件适配代码。与迅雷没有官方关联，也不使用迅雷商标或私有下载接口。
