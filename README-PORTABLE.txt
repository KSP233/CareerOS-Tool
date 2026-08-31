CareerOS Portable v0.2.0（未做数字签名）

使用方法：
1. 解压完整 ZIP，不要只把 CareerOS.exe 单独拖出来。
2. 双击 CareerOS.exe。
3. 第一次启动会在程序旁创建 CareerOS-data；发布包本身不包含开发者的数据库、简历、设置或 API 密钥。
4. 要搬到另一台 Windows 电脑，请退出 CareerOS 后复制整个文件夹。

重要说明：
- Resume：在已导入 DOCX 简历的结构化内容上安全修改。
- CV：本产品中的 CV 是针对岗位重新撰写的专业申请信，不是换名字的 Resume。
- 没有导入简历时不会显示 Match 分数，也不能运行 Analyze。
- Approve 后的 Resume/CV 会显示在 Applications，可双击打开。
- Settings > Storage & safety 可合并另一个 CareerOS 数据库；当前数据库会先备份，Settings 和 API 密钥不会合并。
- Auto apply 永久禁用；CareerOS 不会自动投递。
- 外部 API 必须使用 HTTPS；本地 AI 的 HTTP 地址只允许 localhost/回环地址。
- API 密钥使用 Windows DPAPI 绑定当前 Windows 账户。换电脑后需要重新输入。
- Settings > Local AI models 可经 Windows Package Manager 安装 Ollama，或在已安装的 Ollama 中下载 CareerOS 推荐的本地模型。安装/下载前会显示说明并要求确认；模型下载后，简历与职位内容只在本机处理。

Word/PDF：
- 安装 Microsoft Word 时，CareerOS 使用 Word 生成最终 PDF。
- 没有 Word 时使用内置 PDF 后备渲染器。

诊断：
在命令提示符运行 CareerOS.exe --self-test，可离线检查打包依赖；该检查不会读取或创建 CareerOS 用户数据。
