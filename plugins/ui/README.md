# 本地界面资源

`index.html#sidebar` 显示工程与导入工作台，`index.html#toolbar` 显示浏览器导航。两者由 KiCad 内的独立 WebView 加载，页面不访问网络，也不执行导入操作。

框架使用 Bootstrap 5.3.3 的官方压缩 CSS，已包含在 `vendor/bootstrap.min.css` 中。无需 CDN 或 Bootstrap JavaScript；折叠记录使用 HTML 原生 `details`。第三方许可见 `vendor/LICENSE.bootstrap`。界面 SVG 图标为本项目绘制。

本地消息通道是 `window.lcscShell.postMessage()`，消息为 JSON：

```json
{"version": 1, "action": "navigate", "value": "C2040"}
```

支持 `ready`、`navigate`、`back`、`forward`、`home`、`reload`、`choose-project`。外壳不提供 `import` 动作。

宿主调用 `window.LCSCBridgeUI.update(model)` 更新工程、地址、导航可用性、状态、导入结果与记录。所有来自模型的显示值使用 `textContent`、`value` 或属性赋值，不作为 HTML 插入。状态使用 `aria-live`，交互控件支持键盘，动画遵循系统减少动态效果设置。
