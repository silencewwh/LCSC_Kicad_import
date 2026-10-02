# 立创元件桥 · LCSC KiCad Bridge

在 KiCad 中打开立创商城，点击商品列表「加入购物车」右侧的 **「导入」**，或商品详情页的 **「导入 KiCad」**，将该元件的符号、封装和可用的 3D 模型加入当前工程。

适用于 **KiCad 10.0+**，通过 IPC 插件入口连接原理图编辑器和 PCB 编辑器。器件统一保存在工程的 `LCSC/` 目录，并归入一个 `LCSC` 库。库表使用 `${KIPRJMOD}` 相对路径，便于和工程一起保存、移动。

界面采用 Chrome 风格的灰白顶栏与蓝色按钮。商品页默认占满窗口，顶栏提供导航、地址输入和工程选择；点击右侧「导入记录」图标展开详情。导入失败时自动显示记录。

## 使用

1. 在 KiCad 插件和内容管理器中，通过「从文件安装」安装 `dist/LCSC-KiCad-Bridge.zip`。
2. 在 KiCad 首选项中启用插件 API，重新打开编辑器。先保存工程，再点击工具栏上的「立创元件桥」。
3. 确认顶栏中的工程名称；点击名称或「选择工程」可指定 `.kicad_pro`。在浏览器中搜索商品。
4. 点击该行「加入购物车」右侧的 **「导入」**，也可进入详情页点击 **「导入 KiCad」**。完成后从工程库 `LCSC` 中选择符号或封装，名称以料号开头，方便搜索。

详见 [安装与使用](doc/LCSC_BROWSER_zh_CN.md)，包括 Windows 运行环境、工程目录和故障排查。

## 开发

```powershell
git submodule update --init --recursive
python tools/build_lcsc_plugin.py
```

构建脚本使用 Python 标准库，输出 `dist/LCSC-KiCad-Bridge.zip`，不联网、不更新子模块版本。当前版本为 **0.4.0**。界面的 HTML、CSS、JavaScript 和 Bootstrap 样式均由安装包本地加载，无需 CDN。

图标源文件为 `resources/bridge.svg`。修改后，在安装了 CairoSVG 的开发 Python 环境中运行 `python tools/render_icons.py`，生成 24 像素工具栏图标、48 像素备用图标和 64 像素 PCM 图标；常规打包直接使用这些 PNG。

主要模块：

| 模块 | 作用 |
| --- | --- |
| `plugins/lcsc_launch.py` | IPC 插件启动与 Python 运行环境 |
| `plugins/lcsc_browser.py` | 内置网页窗口、页面按钮与导入进度 |
| `plugins/lcsc_ui.py` | 本地界面与浏览窗口的状态同步 |
| `plugins/ui/` | Bootstrap 界面、导航栏与本地样式资源 |
| `plugins/lcsc_page.py` | 立创商品页判断与浏览规则 |
| `plugins/lcsc_web_bridge.py` | 页面内按钮、点击消息与进度反馈 |
| `plugins/lcsc_project_import.py` | 工程内下载、转换和库表登记 |
| `plugins/lcsc_download_cache.py` | 下载缓存、有效期和容量管理 |
| `plugins/lcsc_model_download.py` | STEP 模型分段并行下载和完整性检查 |
| `plugins/plugin.json` | 原理图和 PCB 的 IPC 插件入口 |
| `tools/build_lcsc_plugin.py` | 独立插件安装包构建 |

本项目使用的第三方代码及许可证见 [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md)。

**0.3.0** 已在 Windows 的 KiCad 10.0.6 Python/WebView2 环境中验证新版界面、网页按钮下载和重复导入。82 项自动化检查与 8 组离线原生界面检查通过，C2040 的符号、封装和 STEP/WRL 模型实测导入成功。

**0.3.1** 增加启动失败提示与本地错误日志，92 项自动化检查通过。Windows 日志位于 `%LOCALAPPDATA%\LCSCKiCadBridge\logs\launch-error.log`。安装后应重新打开编辑器，等待首次插件环境准备完成，再点击工具栏按钮。

**0.3.2** 修正网页子框架加载与页面按钮兼容性。料号输入进入中文商城搜索；按钮仅在商品数据确认后插入，后续复用已有控件。验证页不插入按钮，网站要求人工验证时请按页面提示完成。

**0.4.0** 将元件合并到工程内同一个 `LCSC` 库；OBJ 与 STEP 同时下载，较大的 STEP 文件支持最多 4 个线程分段下载，并缓存已下载数据。侧栏显示当前下载阶段，活动记录显示各阶段耗时。重复导入已有元件直接复用工程文件；旧版完整器件可本地复制到共享库，保留原文件和库项以兼容已有设计。
