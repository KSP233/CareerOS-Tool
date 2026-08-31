CareerOS 本地求职工作区 v0.3.0

源码运行：双击 START.cmd；诊断运行：双击 START-DEBUG.cmd。
移动版：解压完整 ZIP 后双击 CareerOS.exe，不要单独移动 EXE。

核心流程

- 先导入可编辑的 DOCX 简历。CareerOS 按 Word 中段落与表格的真实顺序解析并保存本地快照；不会覆盖用户原文件。
- 未导入简历时，职位 Match 显示“—”，不会用搜索关键词伪造候选人评分，也不能执行 Analyze。
- Resume Draft 在所选原始简历快照的结构化内容上做受限修改；AI 只能修改允许的 bullet/skills ID，不能添加不存在的经历、技能、数字或 Summary。
- CV Draft 在本产品中指岗位定制的专业申请信：使用简历、Settings 中用户确认的信息、Additional Materials 与职位要求重新撰写；不是改名后的 Resume。
- Resume/CV 草稿分开显示。选中 CV 时右侧只显示信件 PDF 预览；选中 Resume 时显示 Original、Generated 与 Compare。
- 草稿默认打开 Generated Preview。Approve 后，Resume 与 CV 会在 Applications 对应列显示，可双击打开；CareerOS 不会自动投递。
- Resume 编辑器使用工作副本、折叠式章节、Undo/Redo 与真实 PDF 预览。Save 前不会修改保存版本。
- Summary 功能已停用：不提供编辑、AI 操作或输出；旧 JSON 字段只为读取历史数据保留。
- “Apply layout to selected draft”只重新排版，不调用 AI，也不修改文字。

数据与隐私

- 默认数据目录：%LOCALAPPDATA%\CareerOS\data。可在 Settings 中复制到新位置，原目录保留为备份。
- Settings > Storage & safety > Merge another CareerOS database 可安全合并两个数据库；当前库先备份，职位 ID 会重新映射。Settings 与 API 密钥不会被合并。
- 移动版首次启动才在程序旁创建 CareerOS-data；发布 ZIP 不包含开发者数据库、简历、Settings、日志或 API 密钥。
- API 密钥使用 Windows DPAPI，绑定当前 Windows 账户。移动到另一台电脑后需要重新输入。
- Auto 只允许 localhost/回环地址上的本地 AI。外部 API 必须使用 HTTPS，并且每次发送前显示确认。
- Additional Materials 单文件最多 25 MB；PDF 页数、Office 解压大小和提取文本长度有安全上限。
- Form Fill Assistant 只生成由用户手动运行的填表脚本；不会点击、上传、回答筛选题或提交表单。

生成与 Word

- 安装 Microsoft Word 时，PDF 通过隐藏的 Word 实例生成，并先写入唯一临时文件再原子发布，降低 Windows PDF 锁冲突。
- 没有 Word或 Word 导出失败时，使用与 Word 布局接近的内置 PDF 后备渲染器。
- “Preserve-original Resume”功能已完全移除；所有 Resume 使用 CareerOS 受控结构化渲染器。
- 每个草稿记录生成时的原始简历文本快照。之后导入新 DOCX，不会改变旧草稿重新生成时所用的简历。

数据库与备份

- 数据库文件名为 careeros.db（旧 applypilot.db 会以 SQLite backup API 复制迁移并保留原文件）。
- 数据库合并前会在 backups 文件夹生成时间戳备份。
- 合并只复制来源 CareerOS 数据目录内部的文件；伪造的外部路径会被拒绝。

安全边界

- 程序没有自动启动、系统服务、管理员提权、下载执行器、远程控制或自动投递功能。
- EXE 当前未做 Authenticode 数字签名；正式对外发布前仍建议签名。ZIP 内提供 SHA256SUMS.txt 用于检测文件损坏或替换。
- 数字签名暂缓不影响其余功能修复，但 Windows 仍可能显示“未知发布者”。
