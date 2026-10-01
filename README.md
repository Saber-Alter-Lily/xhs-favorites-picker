# XHS Favorites Picker

一个本地运行的小红书收藏挑选、图片保存和阅读工具。

**把收藏夹里真正想留下的内容，挑出来保存到电脑。**

[下载 v0.6.2](releases/xhs-favorites-picker-v0.6.2.zip?raw=1)

## 怎么用

支持 **Windows 10/11**，需要安装：

- Google Chrome
- Python 3.10+
- Node.js 22+

第一次使用：

1. 下载并解压
2. 双击 `setup.bat`
3. 按提示登录一次小红书

以后直接双击：

```text
run.bat
```

## 主要功能

- **我的收藏**：浏览、同步、多选后批量保存
- **快速保存**：粘贴分享文案或链接直接保存
- **本地阅读**：查看已经下载到 `downloads/` 的作品和图片
- **作者订阅**：只检查后续新增作品，不默认下载全部历史内容

## 从 v0.4 / v0.5 升级

**不要删除旧目录。**

下载新版并解压，运行：

```text
upgrade_from_v04_v05.bat
```

选择旧安装目录即可。

原来的：

```text
downloads/
.redbook-cookies.json
.chrome-debug-profile/
.subscriptions.json
.favorites-cache.json
```

都会保留，旧图片不需要重新下载。

## 数据在哪里

已下载图片默认在：

```text
downloads/
```

登录状态、订阅和收藏缓存也都只保存在本机。

**不要把 `.redbook-cookies.json` 或 `.chrome-debug-profile/` 分享给别人。**

## 使用提示

本项目使用非官方接口，仅用于个人本地管理自己能够正常访问的内容。

程序加入了请求限速、验证码/频控熔断和本地接口保护，但无法保证账号绝对不会触发平台风控。建议低频、人工选择式使用，不要用于大规模自动采集。

第三方依赖见 [THIRD_PARTY.md](THIRD_PARTY.md)。

MIT License。
