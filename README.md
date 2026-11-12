# EU4 本地 Wiki 查阅工具 / EU4 Local Wiki Viewer

欧陆风云4中文百科（[www.eu4cn.com](https://www.eu4cn.com)）离线镜像的桌面阅读器。
免安装、双击即用：内置本地 HTTP 服务器与 SQLite 全文搜索，
把静态抓取的镜像变成一个可搜索、可导航的阅读应用。

An unofficial desktop reader for the Chinese EU4 community wiki (eu4cn.com).
Zero-install: bundled local HTTP server + full-text search over a static mirror.

## 下载使用 / Download

从 [Releases](https://github.com/yexlinyusx/EU4-Local-Wiki.exe/releases/latest) 下载 [`EU4WikiOffline-v1.1.exe`](https://github.com/yexlinyusx/EU4-Local-Wiki.exe/releases/download/v1.1/EU4WikiOffline-v1.1.exe)（约 1 GB，全部数据已内置）：

1. 适用于 Windows 10/11，双击即用，无需安装任何环境
2. 首次启动自动提取搜索索引（约 15 秒，一次性），之后每次秒开
3. 数据为 2024-11 快照（游戏 1.36/1.37 时代），收录 25,369 页、4.5 万余张图片
4. 完全离线可用；关闭窗口即退出，不留浏览历史

## 功能 / Features

- 🔍 **全文搜索**：中文按字切分索引，两个字的词也能毫秒级命中；标题精确匹配置顶；
  页头搜索框实时下拉建议（Ctrl+K 聚焦）
- 📖 **自建阅读界面**：左侧自动生成目录（滚动高亮当前小节）、宽幅正文排版、深浅色主题
- 🧭 **导航**：返回 / 前进、随机条目、命名空间链接自动映射、断链显示友好提示页
- 🧱 **渲染**：信息框、数据表格、事件框（图片横幅随文字自适应拉伸）、模板代码块、中英文混排
- 📦 **两种形态**：单文件版（数据封在 exe 内，单文件分享）/ 文件夹版（镜像以文件形式存放）
- 🔌 **完全离线**：零外部依赖，渲染资源 100% 本地；WebView2 InPrivate 模式不留浏览痕迹

## 源码结构 / Layout

```
├── app.py              主程序：HTTP 服务器 + 阅读界面 + 桌面窗口
├── wikilib.py          正文提取 / 目录生成 / 中文检索预处理
├── build_index.py      镜像 → SQLite FTS5 全文索引（多进程）
├── build_singlefile.py 单文件版打包（数据 zip 追加到 exe 尾部）
├── make_icon.py        应用图标生成
├── app.ico             应用图标
└── 使用说明.txt         面向使用者的完整说明
```

## 自行构建 / Build from source

```bash
pip install pywebview pyinstaller

# 1) 准备镜像（任意 HTTrack / wget 抓取的 eu4cn.com 静态镜像，需含 首页.html 与 wiki/）
# 2) 构建全文索引
python build_index.py --root <镜像根目录> --out <输出目录>

# 3) 构建文件夹版 exe
python -m PyInstaller --noconsole --onefile --name "EU4百科离线版" \
       --icon app.ico --collect-all webview app.py

# 4) 构建单文件版 exe（数据包追加到 exe 尾部；按需修改脚本顶部路径常量）
python build_singlefile.py
```

## 数据来源与版权 / Data attribution

- 词条内容来自 [欧陆风云4百科](https://www.eu4cn.com)（社区维基，多数条目译自官方 EU4 Wiki），
  著作权归各编者所有，本项目仅提供**离线阅读工具**，不主张对 Wiki 内容的任何权利。
- 程序代码为工具代码，不含游戏本体资源。

## 已知限制 / Known limitations

- 个别依赖原站 CSS 的 Wiki 模板排版可能略有差异
- FontAwesome 字体未随镜像抓取，个别小图标显示为空白
- 红链（镜像未收录的页面）显示"离线版未收录"并给出近似建议
- 编辑 / 查看历史 / 登录等在线功能不可用（离线快照的固有限制）
